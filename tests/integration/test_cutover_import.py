"""Atomic, idempotent cutover import and pre-opening daily history."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from app.application.queries.spending import SpendingQueries
from app.application.services.cutover_import import BudgetCutoverImport
from app.application.services.ledger import JournalService
from app.domain.errors import CutoverImportError, DatabaseError
from app.domain.ports.repositories import UnitOfWork
from app.infrastructure.database.budget_models import (
    FinancialAccountModel,
    JournalEntryModel,
    WalletModel,
)
from app.infrastructure.database.engine import Database
from app.infrastructure.database.import_models import (
    ImportBatchModel,
    ImportRowModel,
    LegacyDailyActivityModel,
)
from app.infrastructure.database.spending_queries import (
    SqlAlchemySpendingReadRepository,
)
from sqlalchemy import func, select


def _payload(*, duplicate_category: bool = False) -> dict[str, object]:
    categories: list[dict[str, object]] = [
        {"source_key": "category:food", "name": "測試餐飲"}
    ]
    if duplicate_category:
        categories.append({"source_key": "category:duplicate", "name": "測試餐飲"})
    return {
        "format_version": 1,
        "source_kind": "test_sheet_readonly",
        "source_locator_hash": "a" * 64,
        "observed_at": "2026-08-14T15:10:00+00:00",
        "cutoff_at": "2026-08-14T15:10:00+00:00",
        "accounts": [
            {
                "source_key": "account:test",
                "institution_code": "test_bank",
                "display_name": "測試帳戶",
                "currency": "TWD",
                "masked_reference": "••••1234",
                "balance_minor": 100000,
                "wallets": [
                    {"name": "測試預備金", "balance_minor": 120000},
                    {"name": "測試已超支", "balance_minor": -20000},
                ],
            }
        ],
        "categories": categories,
        "targets": [
            {
                "source_key": "target:food",
                "year": 2026,
                "month": 8,
                "currency": "TWD",
                "category_key": "category:food",
                "target_minor": 50000,
                "buffer_minor": 10000,
                "funding_account_key": "account:test",
                "funding_wallet_name": "測試預備金",
            }
        ],
        "subscriptions": [],
        "salary_template": None,
        "legacy_daily": [
            {
                "source_key": "daily:1",
                "activity_date": "2026-08-14",
                "sequence": 1,
                "amount_minor": 12300,
                "currency": "TWD",
                "payment_method": "credit_card",
                "note": "測試午餐",
            }
        ],
        "pending": [
            {
                "source_key": "pending:card",
                "entity_kind": "card_cycle",
                "reason_code": "card_mapping_required",
                "payload": {"bank": "測試銀行"},
            }
        ],
    }


def _write(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _service(factory: Callable[[], UnitOfWork]) -> BudgetCutoverImport:
    return BudgetCutoverImport(
        factory,
        JournalService(factory),
        clock=lambda: datetime(2026, 8, 14, 15, 11, tzinfo=UTC),
    )


def test_preview_is_read_only_and_apply_preserves_signed_wallets(
    tmp_path: Path,
    migrated_database: Database,
    b7_factory: Callable[[], UnitOfWork],
) -> None:
    path = _write(tmp_path / "cutover.json", _payload())
    service = _service(b7_factory)

    preview = service.preview(path)
    with migrated_database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ImportBatchModel)) == 0

    result = service.apply(path, approved_sha256=preview.content_sha256)
    replay = service.apply(path, approved_sha256=preview.content_sha256)

    assert result.created is True
    assert replay.created is False
    assert replay.batch.id == result.batch.id
    assert result.preview.account_count == 1
    assert result.preview.legacy_daily_totals[0].amount_minor == 12300
    with migrated_database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ImportBatchModel)) == 1
        assert session.scalar(select(func.count()).select_from(ImportRowModel)) == 8
        assert session.scalar(select(func.count()).select_from(JournalEntryModel)) == 1
        account = session.scalar(select(FinancialAccountModel))
        assert account is not None
        balances = {
            row.name: row.id
            for row in session.scalars(
                select(WalletModel).where(
                    WalletModel.financial_account_id == account.id
                )
            )
        }
    with b7_factory() as unit:
        assert unit.budget_ledger.wallet_balance(balances["測試預備金"]) == 120000
        assert unit.budget_ledger.wallet_balance(balances["測試已超支"]) == -20000


def test_legacy_history_is_visible_but_not_part_of_posted_totals(
    tmp_path: Path,
    migrated_database: Database,
    b7_factory: Callable[[], UnitOfWork],
) -> None:
    path = _write(tmp_path / "cutover.json", _payload())
    service = _service(b7_factory)
    preview = service.preview(path)
    service.apply(path, approved_sha256=preview.content_sha256)
    queries = SpendingQueries(
        SqlAlchemySpendingReadRepository(migrated_database.session_factory),
        local_timezone="Asia/Taipei",
    )

    view = queries.daily(day=date(2026, 8, 14))

    assert view.rows == ()
    assert view.totals == ()
    assert len(view.legacy_rows) == 1
    assert view.legacy_rows[0].note == "測試午餐"
    assert view.legacy_totals[0].amount_minor == 12300


def test_changed_hash_and_partial_failure_write_nothing(
    tmp_path: Path,
    migrated_database: Database,
    b7_factory: Callable[[], UnitOfWork],
) -> None:
    path = _write(tmp_path / "cutover.json", _payload(duplicate_category=True))
    service = _service(b7_factory)
    preview = service.preview(path)

    with pytest.raises(CutoverImportError, match="file changed"):
        service.apply(path, approved_sha256="0" * 64)
    with pytest.raises(DatabaseError):
        service.apply(path, approved_sha256=preview.content_sha256)

    with migrated_database.session_factory() as session:
        for model in (
            ImportBatchModel,
            ImportRowModel,
            LegacyDailyActivityModel,
            FinancialAccountModel,
            WalletModel,
            JournalEntryModel,
        ):
            assert session.scalar(select(func.count()).select_from(model)) == 0

"""Ledger migration upgrade, downgrade, and data-preservation tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from alembic import command
from app.domain.enums import TransactionType
from app.domain.models import EmailSnapshot, TransactionDraft
from app.infrastructure.config.settings import AppSettings
from app.infrastructure.database.engine import create_database
from app.infrastructure.database.migrations import (
    build_alembic_config,
    inspect_migration_status,
    upgrade_database,
)
from app.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

LEDGER_TABLES = {
    "financial_accounts",
    "wallets",
    "ledger_accounts",
    "journal_entries",
    "journal_postings",
}


def _seed_gmail_rows(settings: AppSettings) -> tuple[int, int]:
    database = create_database(settings)
    try:
        with SqlAlchemyUnitOfWork(database.session_factory) as unit:
            account = unit.accounts.get_or_create("user@example.test")
            outcome = unit.emails.upsert_from_gmail(
                EmailSnapshot(
                    gmail_account_id=account.id,
                    gmail_message_id="gmail-ledger-001",
                    gmail_thread_id="thread-ledger-001",
                    gmail_history_id="500",
                    sender="notice@example-bank.test",
                    recipients=("user@example.test",),
                    subject="消費通知",
                    gmail_internal_date_ms=1_754_000_000_000,
                    received_at=datetime(2026, 8, 1, 3, 0, tzinfo=UTC),
                    body_text="已去識別化的測試內容",
                )
            )
            unit.transactions.add(
                TransactionDraft(
                    email_id=outcome.email.id,
                    source_key="row-1",
                    transaction_type=TransactionType.PURCHASE,
                    confidence=Decimal("0.9"),
                    parser_name="tests.fake",
                    parser_version="1",
                    amount_minor=12800,
                    currency="TWD",
                )
            )
            unit.commit()
        with database.engine.connect() as connection:
            emails = connection.scalar(text("SELECT COUNT(*) FROM emails")) or 0
            transactions = (
                connection.scalar(text("SELECT COUNT(*) FROM transactions")) or 0
            )
        return emails, transactions
    finally:
        database.engine.dispose()


def _row_counts(settings: AppSettings) -> tuple[int, int]:
    database = create_database(settings)
    try:
        with database.engine.connect() as connection:
            emails = connection.scalar(text("SELECT COUNT(*) FROM emails")) or 0
            transactions = (
                connection.scalar(text("SELECT COUNT(*) FROM transactions")) or 0
            )
        return emails, transactions
    finally:
        database.engine.dispose()


def test_upgrade_from_the_pre_budget_schema_preserves_gmail_rows(
    database_settings: AppSettings,
) -> None:
    config = build_alembic_config(database_settings.database_path)
    upgrade_database(database_settings)
    command.downgrade(config, "0004")
    seeded = _seed_gmail_rows(database_settings)

    command.upgrade(config, "0005")

    status = inspect_migration_status(database_settings)
    database = create_database(database_settings)
    try:
        tables = set(inspect(database.engine).get_table_names())
        with database.engine.connect() as connection:
            foreign_keys = connection.scalar(text("PRAGMA foreign_keys"))
            ledger_rows = connection.scalar(
                text("SELECT COUNT(*) FROM journal_entries")
            )
    finally:
        database.engine.dispose()

    assert status.current_revision == "0005"
    assert tables >= LEDGER_TABLES
    assert foreign_keys == 1
    assert ledger_rows == 0
    assert _row_counts(database_settings) == seeded


def test_ledger_downgrade_removes_only_ledger_tables(
    database_settings: AppSettings,
) -> None:
    config = build_alembic_config(database_settings.database_path)
    upgrade_database(database_settings)
    seeded = _seed_gmail_rows(database_settings)

    command.downgrade(config, "0004")

    database = create_database(database_settings)
    try:
        tables = set(inspect(database.engine).get_table_names())
    finally:
        database.engine.dispose()

    assert not (LEDGER_TABLES & tables)
    assert {"emails", "transactions", "supplementary_card_settings"} <= tables
    assert _row_counts(database_settings) == seeded
    assert upgrade_database(database_settings).is_current


def test_ledger_foreign_keys_use_restrict_and_indexes_exist(
    database_settings: AppSettings,
) -> None:
    upgrade_database(database_settings)
    database = create_database(database_settings)
    try:
        inspector = inspect(database.engine)
        # PRAGMA is authoritative: SQLAlchemy reflection does not report the
        # ON DELETE clause of a column-level foreign key added by ALTER TABLE.
        with database.engine.connect() as connection:
            deletes = {
                (table, row[3]): row[6]
                for table in sorted(LEDGER_TABLES)
                for row in connection.exec_driver_sql(
                    f"PRAGMA foreign_key_list({table})"
                ).all()
            }
        wallet_indexes = {item["name"] for item in inspector.get_indexes("wallets")}
        posting_indexes = {
            item["name"] for item in inspector.get_indexes("journal_postings")
        }
        account_indexes = {
            item["name"] for item in inspector.get_indexes("financial_accounts")
        }
        entry_checks = {
            item["name"] for item in inspector.get_check_constraints("journal_entries")
        }
    finally:
        database.engine.dispose()

    assert deletes
    assert set(deletes.values()) == {"RESTRICT"}
    assert "uq_wallets_active_unallocated" in wallet_indexes
    assert "uq_financial_accounts_active_identity" in account_indexes
    assert {
        "ix_journal_postings_account_wallet",
        "ix_journal_postings_journal_entry_id",
        "ix_journal_postings_wallet_id",
    } <= posting_indexes
    assert "ck_journal_entries_journal_entry_reversal_not_self" in entry_checks


B2_TABLES = {"cards", "financial_events", "financial_event_source_links"}


def _seed_journal_rows(settings: AppSettings) -> tuple[int, int]:
    """Insert one opening entry and its reversal without using the new ORM."""
    database = create_database(settings)
    try:
        with database.engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO financial_accounts (institution_code, display_name,"
                " account_type, currency, is_active, version, created_at, updated_at)"
                " VALUES ('test_bank','測試帳戶','checking','TWD',1,1,"
                "'2026-08-07 13:00:00','2026-08-07 13:00:00')"
            )
            connection.exec_driver_sql(
                "INSERT INTO wallets (financial_account_id, name, purpose, is_active,"
                " version, created_at, updated_at) VALUES (1,'Unallocated',"
                "'unallocated',1,1,'2026-08-07 13:00:00','2026-08-07 13:00:00')"
            )
            for code, kind, account in (
                ("asset:test_bank:1", "asset", "1"),
                ("equity:opening:TWD", "equity", "NULL"),
            ):
                connection.exec_driver_sql(
                    "INSERT INTO ledger_accounts (code, name, kind, currency,"
                    f" financial_account_id, created_at, updated_at) VALUES ('{code}',"
                    f"'n','{kind}','TWD',{account},'2026-08-07 13:00:00',"
                    "'2026-08-07 13:00:00')"
                )
            for entry_type, key, reversal in (
                ("opening_balance", "opening_balance:financial_account:1", "NULL"),
                ("reversal", "reversal:journal_entry:1", "1"),
            ):
                connection.exec_driver_sql(
                    "INSERT INTO journal_entries (entry_type, effective_at,"
                    " recorded_at, idempotency_key, reversal_of_id, description,"
                    f" created_at, updated_at) VALUES ('{entry_type}',"
                    f"'2026-08-07 13:30:00.000000','2026-08-08 02:00:00.000000',"
                    f"'{key}',{reversal},'','2026-08-07 13:00:00',"
                    "'2026-08-07 13:00:00')"
                )
            for entry, ledger, wallet, amount in (
                (1, 1, "1", 500000),
                (1, 2, "NULL", -500000),
                (2, 1, "1", -500000),
                (2, 2, "NULL", 500000),
            ):
                connection.exec_driver_sql(
                    "INSERT INTO journal_postings (journal_entry_id,"
                    " ledger_account_id, wallet_id, amount_minor, currency,"
                    f" created_at, updated_at) VALUES ({entry},{ledger},{wallet},"
                    f"{amount},'TWD','2026-08-07 13:00:00','2026-08-07 13:00:00')"
                )
        with database.engine.connect() as connection:
            entries = connection.scalar(text("SELECT COUNT(*) FROM journal_entries"))
            postings = connection.scalar(text("SELECT COUNT(*) FROM journal_postings"))
        return entries or 0, postings or 0
    finally:
        database.engine.dispose()


def _journal_snapshot(settings: AppSettings) -> dict[str, Any]:
    database = create_database(settings)
    try:
        with database.engine.connect() as connection:
            return {
                "entries": connection.exec_driver_sql(
                    "SELECT id, entry_type, effective_at, recorded_at,"
                    " idempotency_key, reversal_of_id FROM journal_entries ORDER BY id"
                ).all(),
                "postings": connection.scalar(
                    text("SELECT COUNT(*) FROM journal_postings")
                ),
                "postings_fk": sorted(
                    (row[2], row[3], row[6])
                    for row in connection.exec_driver_sql(
                        "PRAGMA foreign_key_list(journal_postings)"
                    ).all()
                ),
                "indexes": sorted(
                    row[1]
                    for row in connection.exec_driver_sql(
                        "PRAGMA index_list(journal_entries)"
                    ).all()
                ),
                "fk_check": connection.exec_driver_sql(
                    "PRAGMA foreign_key_check"
                ).all(),
            }
    finally:
        database.engine.dispose()


def test_b2_upgrade_preserves_every_journal_row_index_and_foreign_key(
    database_settings: AppSettings,
) -> None:
    config = build_alembic_config(database_settings.database_path)
    upgrade_database(database_settings)
    command.downgrade(config, "0005")
    seeded = _seed_journal_rows(database_settings)
    before = _journal_snapshot(database_settings)

    command.upgrade(config, "0006")

    after = _journal_snapshot(database_settings)
    database = create_database(database_settings)
    try:
        tables = set(inspect(database.engine).get_table_names())
        with database.engine.connect() as connection:
            entry_fk = sorted(
                (row[2], row[3], row[6])
                for row in connection.exec_driver_sql(
                    "PRAGMA foreign_key_list(journal_entries)"
                ).all()
            )
            triggers = sorted(
                row[0]
                for row in connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                ).all()
            )
    finally:
        database.engine.dispose()

    assert tables >= B2_TABLES
    assert after["entries"] == before["entries"]
    assert after["postings"] == before["postings"] == seeded[1]
    assert after["postings_fk"] == before["postings_fk"]
    assert after["fk_check"] == []
    assert set(before["indexes"]) < set(after["indexes"])
    assert "ix_journal_entries_financial_event_id" in after["indexes"]
    assert ("financial_events", "financial_event_id", "RESTRICT") in entry_fk
    assert triggers == [
        "journal_entry_reversal_pairing_insert",
        "journal_entry_reversal_pairing_update",
    ]
    assert inspect_migration_status(database_settings).current_revision == "0006"


def test_b2_reversal_pairing_is_enforced_by_the_database(
    database_settings: AppSettings,
) -> None:
    """Revision 0006 enforces the pairing with triggers, before 0007's check."""
    config = build_alembic_config(database_settings.database_path)
    upgrade_database(database_settings)
    command.downgrade(config, "0006")
    database = create_database(database_settings)
    insert = (
        "INSERT INTO journal_entries (entry_type, effective_at, recorded_at,"
        " idempotency_key, reversal_of_id, description, created_at, updated_at)"
        " VALUES (?, '2026-01-01', '2026-01-01', ?, ?, '', '2026-01-01','2026-01-01')"
    )
    try:
        with database.engine.begin() as connection:
            connection.exec_driver_sql(insert, ("opening_balance", "opening", None))
        for entry_type, key, reversal in (
            ("adjustment", "mislabelled", 1),
            ("reversal", "target-less", None),
        ):
            with (
                database.engine.begin() as connection,
                pytest.raises(IntegrityError, match="reversal pairing violated"),
            ):
                connection.exec_driver_sql(insert, (entry_type, key, reversal))
        with database.engine.begin() as connection:
            connection.exec_driver_sql(insert, ("reversal", "reversal:1", 1))
    finally:
        database.engine.dispose()


def test_b2_downgrade_removes_only_b2_objects(
    database_settings: AppSettings,
) -> None:
    config = build_alembic_config(database_settings.database_path)
    upgrade_database(database_settings)
    command.downgrade(config, "0005")
    seeded = _seed_journal_rows(database_settings)
    command.upgrade(config, "0006")
    before = _journal_snapshot(database_settings)

    command.downgrade(config, "0005")

    after = _journal_snapshot(database_settings)
    database = create_database(database_settings)
    try:
        tables = set(inspect(database.engine).get_table_names())
        with database.engine.connect() as connection:
            columns = [
                row[1]
                for row in connection.exec_driver_sql(
                    "PRAGMA table_info(journal_entries)"
                ).all()
            ]
            triggers = connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            ).all()
    finally:
        database.engine.dispose()

    assert not (B2_TABLES & tables)
    assert tables >= LEDGER_TABLES
    assert "financial_event_id" not in columns
    assert triggers == []
    assert after["entries"] == before["entries"]
    assert after["postings"] == seeded[1]
    assert after["postings_fk"] == before["postings_fk"]
    assert after["fk_check"] == []
    assert upgrade_database(database_settings).is_current


def test_b2_upgrade_refuses_pre_existing_pairing_violations(
    database_settings: AppSettings,
) -> None:
    """A trigger cannot validate old rows, so the upgrade checks them first."""
    config = build_alembic_config(database_settings.database_path)
    upgrade_database(database_settings)
    command.downgrade(config, "0005")
    _seed_journal_rows(database_settings)
    database = create_database(database_settings)
    try:
        with database.engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE journal_entries SET entry_type = 'adjustment' WHERE id = 2"
            )
    finally:
        database.engine.dispose()

    with pytest.raises(RuntimeError, match="violate the reversal pairing"):
        command.upgrade(config, "0006")

    assert inspect_migration_status(database_settings).current_revision == "0005"
    database = create_database(database_settings)
    try:
        tables = set(inspect(database.engine).get_table_names())
    finally:
        database.engine.dispose()
    assert not (B2_TABLES & tables)

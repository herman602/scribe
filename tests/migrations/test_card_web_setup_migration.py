"""Revision 0017 preserves explicit card-to-import decisions."""

from __future__ import annotations

import json

import pytest
from alembic import command
from app.domain.budget_enums import CardHolderRole, CardImportAssignmentKind
from app.domain.budget_models import CardDraft, CardImportAssignmentDraft
from app.infrastructure.config.settings import AppSettings
from app.infrastructure.database.engine import create_database
from app.infrastructure.database.migrations import (
    build_alembic_config,
    inspect_migration_status,
    upgrade_database,
)
from app.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork
from sqlalchemy import inspect, text


def test_0017_has_restrict_links_and_refuses_to_erase_a_mapping(
    database_settings: AppSettings,
) -> None:
    upgrade_database(database_settings)
    database = create_database(database_settings)
    try:
        inspector = inspect(database.engine)
        foreign_keys = inspector.get_foreign_keys("card_import_assignments")
        assert {item["referred_table"] for item in foreign_keys} == {
            "cards",
            "import_rows",
        }
        assert {item["options"]["ondelete"] for item in foreign_keys} == {"RESTRICT"}

        with SqlAlchemyUnitOfWork(database.session_factory) as unit:
            gmail = unit.accounts.get_or_create("user@example.test")
            card = unit.cards.add(
                CardDraft(
                    gmail_account_id=gmail.id,
                    issuer_code="test_bank",
                    display_name="測試卡",
                    last_four="4321",
                    holder_role=CardHolderRole.PRIMARY,
                )
            )
            batch = unit.import_batches.add(
                source_kind="synthetic",
                format_version=1,
                content_sha256="1" * 64,
                source_locator_hash="2" * 64,
                observed_at=gmail.created_at,
                cutoff_at=gmail.created_at,
                row_count=1,
                warning_count=1,
                applied_at=gmail.created_at,
            )
            row_id = unit.import_rows.add(
                batch_id=batch.id,
                entity_kind="card_cycle",
                source_row_key="cycle",
                normalized_payload_json=json.dumps(
                    {"statement_day": 1, "due_day": 16, "autopay_day": 17}
                ),
                status="pending",
                reason_code="card_mapping_required",
            )
            unit.card_import_assignments.add(
                CardImportAssignmentDraft(
                    import_row_id=row_id,
                    card_id=card.id,
                    assignment_kind=CardImportAssignmentKind.CARD_CYCLE,
                    statement_day=1,
                    due_day=16,
                    autopay_day=17,
                )
            )
            unit.commit()
    finally:
        database.engine.dispose()

    with pytest.raises(RuntimeError, match="assignments=1"):
        command.downgrade(build_alembic_config(database_settings.database_path), "0016")

    assert inspect_migration_status(database_settings).current_revision == "0022"
    database = create_database(database_settings)
    try:
        with database.engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT COUNT(*) FROM card_import_assignments"))
                == 1
            )
            assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        database.engine.dispose()


def test_0017_round_trips_when_there_are_no_decisions(
    database_settings: AppSettings,
) -> None:
    upgrade_database(database_settings)
    config = build_alembic_config(database_settings.database_path)
    command.downgrade(config, "0016")
    assert inspect_migration_status(database_settings).current_revision == "0016"
    command.upgrade(config, "0017")
    assert inspect_migration_status(database_settings).current_revision == "0017"

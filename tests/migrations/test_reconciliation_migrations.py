"""Revision 0014 upgrade, refusing downgrade, and data-preservation tests.

Every seeded row is anonymous: test institution codes, invented amounts, and
no card number, merchant, or Email content anywhere.
"""

from __future__ import annotations

from typing import Any

import pytest
from alembic import command
from app.infrastructure.config.settings import AppSettings
from app.infrastructure.database.engine import create_database
from app.infrastructure.database.migrations import (
    build_alembic_config,
    inspect_migration_status,
    upgrade_database,
)
from sqlalchemy import inspect, text

from tests.migrations.test_scheduled_transfer_migrations import (
    _SEED_STATEMENTS,
    B4_TRIGGERS,
    B5_TRIGGERS,
    B6_TRIGGERS,
)

B7_TABLES = {"balance_snapshots"}
B7_TRIGGERS = {"balance_snapshot_no_update", "balance_snapshot_no_delete"}
B7_ENTRY_TYPES = {"income", "external_movement"}
B7_INDEXES = {"ix_balance_snapshots_account_observed_at"}
B7_EVENT_INDEXES = {
    "ix_financial_events_matching_candidates",
    "ix_financial_events_daily_activity",
}
#: Revision 0007's rule, which B7 keeps exactly as it is (ADR-B063).
SOURCE_LINK_INDEXES = {
    "uq_source_links_one_confirmed_per_transaction",
    "ix_source_links_transaction_status",
    "ix_source_links_event_status",
}
_PRESERVED_TABLES = (
    "budget_categories",
    "budget_periods",
    "event_allocations",
    "financial_accounts",
    "financial_events",
    "funding_commitments",
    "journal_entries",
    "journal_postings",
    "salary_allocation_run_items",
    "salary_allocation_runs",
    "salary_plan_template_items",
    "salary_plan_templates",
    "wallets",
)


def _config_at(settings: AppSettings, revision: str) -> Any:
    config = build_alembic_config(settings.database_path)
    upgrade_database(settings)
    command.downgrade(config, revision)
    return config


def _seed_history(settings: AppSettings) -> dict[str, int]:
    """Seed realistic B4, B5, and B6 rows the upgrade must preserve."""
    database = create_database(settings)
    try:
        with database.engine.begin() as connection:
            for statement in _SEED_STATEMENTS:
                connection.exec_driver_sql(statement)
    finally:
        database.engine.dispose()
    return _row_counts(settings)


def _row_counts(settings: AppSettings) -> dict[str, int]:
    database = create_database(settings)
    try:
        with database.engine.connect() as connection:
            return {
                table: connection.scalar(text(f"SELECT COUNT(*) FROM {table}")) or 0
                for table in _PRESERVED_TABLES
            }
    finally:
        database.engine.dispose()


def _snapshot(settings: AppSettings) -> dict[str, Any]:
    database = create_database(settings)
    try:
        inspector = inspect(database.engine)
        with database.engine.connect() as connection:
            return {
                "tables": set(inspector.get_table_names()),
                "triggers": {
                    row[0]
                    for row in connection.exec_driver_sql(
                        "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                    ).all()
                },
                "entry_types": {
                    row[0]
                    for row in connection.exec_driver_sql(
                        "SELECT code FROM journal_entry_types"
                    ).all()
                },
                "integrity": connection.scalar(text("PRAGMA integrity_check")),
                "fk_check": connection.exec_driver_sql(
                    "PRAGMA foreign_key_check"
                ).all(),
                "foreign_keys": connection.scalar(text("PRAGMA foreign_keys")),
                "journal_effective": [
                    row[0]
                    for row in connection.exec_driver_sql(
                        "SELECT effective_at FROM journal_entries ORDER BY id"
                    ).all()
                ],
            }
    finally:
        database.engine.dispose()


def _indexes(settings: AppSettings, table: str) -> set[str]:
    database = create_database(settings)
    try:
        with database.engine.connect() as connection:
            return {
                row[1]
                for row in connection.exec_driver_sql(
                    f"PRAGMA index_list({table})"
                ).all()
            }
    finally:
        database.engine.dispose()


def _foreign_keys(settings: AppSettings, table: str) -> list[tuple[str, str, str]]:
    database = create_database(settings)
    try:
        with database.engine.connect() as connection:
            return sorted(
                (row[2], row[3], row[6])
                for row in connection.exec_driver_sql(
                    f"PRAGMA foreign_key_list({table})"
                ).all()
            )
    finally:
        database.engine.dispose()


def _seed_snapshot(settings: AppSettings) -> None:
    """Insert one observation, which a downgrade must refuse to destroy."""
    database = create_database(settings)
    try:
        with database.engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO balance_snapshots (financial_account_id,"
                " observed_balance_minor, observed_at, source, idempotency_key,"
                " created_at, updated_at) VALUES (1, 12345, '2026-08-09 04:30:00',"
                " 'manual_bank_app', 'balance_snapshot:seed', '2026-08-09',"
                " '2026-08-09')"
            )
    finally:
        database.engine.dispose()


def _seed_entry(settings: AppSettings, entry_type: str) -> None:
    """Insert one posted entry of a B7 type, with no user data in it."""
    database = create_database(settings)
    try:
        with database.engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO journal_entries (entry_type, effective_at, recorded_at,"
                " idempotency_key, description, created_at, updated_at) VALUES"
                f" ('{entry_type}', '2026-08-09 04:30:00', '2026-08-09 05:00:00',"
                f" 'test:{entry_type}', '', '2026-08-09', '2026-08-09')"
            )
    finally:
        database.engine.dispose()


def test_a_new_database_reaches_0014_with_every_object_it_needs(
    database_settings: AppSettings,
) -> None:
    status = upgrade_database(database_settings)

    assert status.is_current
    assert status.current_revision == "0022"
    state = _snapshot(database_settings)
    assert state["tables"] >= B7_TABLES
    assert state["triggers"] >= B7_TRIGGERS | B6_TRIGGERS | B5_TRIGGERS | B4_TRIGGERS
    assert state["entry_types"] >= B7_ENTRY_TYPES | {"card_payment", "adjustment"}
    assert state["integrity"] == "ok"
    assert state["fk_check"] == []
    assert state["foreign_keys"] == 1
    assert _indexes(database_settings, "balance_snapshots") >= B7_INDEXES
    assert _indexes(database_settings, "financial_events") >= B7_EVENT_INDEXES
    assert (
        _indexes(database_settings, "financial_event_source_links")
        >= SOURCE_LINK_INDEXES
    )


def test_the_snapshot_foreign_key_restricts_deletion(
    database_settings: AppSettings,
) -> None:
    upgrade_database(database_settings)

    entries = _foreign_keys(database_settings, "balance_snapshots")

    assert entries
    for _column, _referenced, on_delete in entries:
        assert on_delete == "RESTRICT"


def test_upgrading_0013_to_0014_preserves_every_existing_row(
    database_settings: AppSettings,
) -> None:
    config = _config_at(database_settings, "0013")
    seeded = _seed_history(database_settings)
    before = _snapshot(database_settings)

    command.upgrade(config, "0014")

    after = _snapshot(database_settings)
    assert not (B7_TABLES & before["tables"])
    assert after["tables"] >= B7_TABLES
    assert after["triggers"] >= B7_TRIGGERS | B6_TRIGGERS | B5_TRIGGERS | B4_TRIGGERS
    assert not (B7_ENTRY_TYPES & before["entry_types"])
    assert after["entry_types"] == before["entry_types"] | B7_ENTRY_TYPES
    assert after["journal_effective"] == before["journal_effective"]
    assert after["integrity"] == "ok"
    assert after["fk_check"] == []
    assert after["foreign_keys"] == 1
    assert _row_counts(database_settings) == seeded
    assert inspect_migration_status(database_settings).current_revision == "0014"


def test_0013_to_0014_and_back_round_trips_when_nothing_used_it(
    database_settings: AppSettings,
) -> None:
    config = _config_at(database_settings, "0013")
    seeded = _seed_history(database_settings)
    before = _snapshot(database_settings)

    command.upgrade(config, "0014")
    command.downgrade(config, "0013")

    middle = _snapshot(database_settings)
    assert not (B7_TABLES & middle["tables"])
    assert not (B7_TRIGGERS & middle["triggers"])
    assert middle["entry_types"] == before["entry_types"]
    assert middle["integrity"] == "ok"
    assert middle["fk_check"] == []
    assert _row_counts(database_settings) == seeded
    assert inspect_migration_status(database_settings).current_revision == "0013"

    command.upgrade(config, "0014")

    after = _snapshot(database_settings)
    assert after["tables"] >= B7_TABLES
    assert after["triggers"] >= B7_TRIGGERS | B6_TRIGGERS
    assert after["entry_types"] == before["entry_types"] | B7_ENTRY_TYPES
    assert after["journal_effective"] == before["journal_effective"]
    assert _row_counts(database_settings) == seeded
    assert inspect_migration_status(database_settings).current_revision == "0014"


def test_a_snapshot_row_refuses_update_and_delete(
    database_settings: AppSettings,
) -> None:
    upgrade_database(database_settings)
    _seed_history(database_settings)
    _seed_snapshot(database_settings)
    database = create_database(database_settings)
    try:
        for statement in (
            "UPDATE balance_snapshots SET observed_balance_minor = 1 WHERE id = 1",
            "DELETE FROM balance_snapshots WHERE id = 1",
        ):
            with (
                pytest.raises(Exception, match="immutable"),
                database.engine.begin() as connection,
            ):
                connection.exec_driver_sql(statement)
        with database.engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT observed_balance_minor FROM balance_snapshots")
                )
                == 12345
            )
    finally:
        database.engine.dispose()


def test_a_snapshot_may_be_zero_or_negative(
    database_settings: AppSettings,
) -> None:
    upgrade_database(database_settings)
    _seed_history(database_settings)
    database = create_database(database_settings)
    try:
        with database.engine.begin() as connection:
            for index, balance in enumerate((0, -5000)):
                connection.exec_driver_sql(
                    "INSERT INTO balance_snapshots (financial_account_id,"
                    " observed_balance_minor, observed_at, source, idempotency_key,"
                    f" created_at, updated_at) VALUES (1, {balance},"
                    " '2026-08-09 04:30:00', 'manual_bank_app',"
                    f" 'balance_snapshot:seed-{index}', '2026-08-09', '2026-08-09')"
                )
        with database.engine.connect() as connection:
            observations = connection.scalar(
                text("SELECT COUNT(*) FROM balance_snapshots")
            )
            assert observations == 2
    finally:
        database.engine.dispose()


def test_downgrade_refuses_once_an_observation_exists_and_changes_nothing(
    database_settings: AppSettings,
) -> None:
    config = _config_at(database_settings, "0013")
    _seed_history(database_settings)
    command.upgrade(config, "0014")
    _seed_snapshot(database_settings)
    before = _snapshot(database_settings)

    with pytest.raises(Exception) as error:
        command.downgrade(config, "0013")

    message = str(error.value)
    assert "balance_snapshots=1" in message
    # Only counts: no balance, account name, merchant, or note may appear.
    assert "12345" not in message
    assert "測試" not in message
    after = _snapshot(database_settings)
    assert after["tables"] == before["tables"]
    assert after["triggers"] == before["triggers"]
    assert after["entry_types"] == before["entry_types"]
    assert inspect_migration_status(database_settings).current_revision == "0014"


@pytest.mark.parametrize("entry_type", sorted(B7_ENTRY_TYPES))
def test_downgrade_refuses_once_a_new_entry_type_is_posted(
    database_settings: AppSettings, entry_type: str
) -> None:
    config = _config_at(database_settings, "0013")
    _seed_history(database_settings)
    command.upgrade(config, "0014")
    _seed_entry(database_settings, entry_type)

    with pytest.raises(Exception) as error:
        command.downgrade(config, "0013")

    assert f"journal_entries.{entry_type}=1" in str(error.value)
    assert inspect_migration_status(database_settings).current_revision == "0014"


def test_a_failing_migration_rolls_schema_data_and_revision_back(
    database_settings: AppSettings,
) -> None:
    """The ADR-B027 protocol must undo everything when the check fails."""
    config = _config_at(database_settings, "0013")
    seeded = _seed_history(database_settings)
    before = _snapshot(database_settings)

    def explode(_connection: Any) -> None:
        raise RuntimeError("injected failure before commit")

    config.attributes["verify_before_commit"] = explode
    with pytest.raises(RuntimeError, match="injected failure"):
        command.upgrade(config, "0014")

    after = _snapshot(database_settings)
    assert not (B7_TABLES & after["tables"])
    assert not (B7_TRIGGERS & after["triggers"])
    assert after["entry_types"] == before["entry_types"]
    assert after["integrity"] == "ok"
    assert after["fk_check"] == []
    assert _row_counts(database_settings) == seeded
    assert inspect_migration_status(database_settings).current_revision == "0013"

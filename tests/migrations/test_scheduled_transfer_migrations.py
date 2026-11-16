"""Revision 0013 upgrade, refusing downgrade, and data-preservation tests.

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

B6_TABLES = {"scheduled_transfers", "scheduled_transfer_commitments"}
B6_TRIGGERS = {
    "scheduled_transfer_instruction_is_immutable",
    "scheduled_transfer_commitment_no_update",
    "scheduled_transfer_commitment_no_delete",
    "scheduled_transfer_commitment_within_requirement",
}
B5_TRIGGERS = {
    "salary_run_snapshot_is_immutable",
    "salary_run_item_snapshot_is_immutable",
}
B4_TRIGGERS = {
    "subscription_price_no_overlap_insert",
    "subscription_price_no_overlap_update",
    "subscription_no_overlapping_interval_insert",
    "subscription_no_overlapping_interval_update",
}
B6_ENTRY_TYPES = {"card_payment"}
B6_INDEXES = {
    "ix_scheduled_transfers_status_due",
    "ix_scheduled_transfers_lease_expires_at",
    "ix_scheduled_transfers_card_id",
    "ix_scheduled_transfers_source_account_id",
    "ix_scheduled_transfers_source_wallet_id",
    "ix_scheduled_transfers_destination_account_id",
    "ix_scheduled_transfers_destination_wallet_id",
    "uq_scheduled_transfers_executed_entry",
    "uq_scheduled_transfers_reversal_entry",
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
    "subscription_price_periods",
    "subscriptions",
    "wallets",
)


def _config_at(settings: AppSettings, revision: str) -> Any:
    config = build_alembic_config(settings.database_path)
    upgrade_database(settings)
    command.downgrade(config, revision)
    return config


def _seed_history(settings: AppSettings) -> dict[str, int]:
    """Seed B1-B5 rows, a reversal, and a refund the downgrade must protect."""
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


def _schema_objects(settings: AppSettings) -> dict[str, Any]:
    database = create_database(settings)
    try:
        with database.engine.connect() as connection:
            return {
                "indexes": {
                    table: sorted(
                        row[1]
                        for row in connection.exec_driver_sql(
                            f"PRAGMA index_list({table})"
                        ).all()
                    )
                    for table in sorted(B6_TABLES)
                },
                "foreign_keys": {
                    table: sorted(
                        (row[2], row[3], row[6])
                        for row in connection.exec_driver_sql(
                            f"PRAGMA foreign_key_list({table})"
                        ).all()
                    )
                    for table in sorted(B6_TABLES)
                },
            }
    finally:
        database.engine.dispose()


def _seed_schedule(settings: AppSettings, *, covered: bool = True) -> None:
    """Insert one scheduled movement, and optionally its coverage link."""
    database = create_database(settings)
    try:
        with database.engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO scheduled_transfers (purpose, source_account_id,"
                " source_wallet_id, destination_account_id, destination_wallet_id,"
                " amount_minor, currency, scheduled_local_date,"
                " scheduled_local_time, timezone, due_at_utc, execution_policy,"
                " status, idempotency_key, attempt_count, version, created_at,"
                " updated_at) VALUES ('clearing_funding', 2, 2, 1, 1, 8000, 'TWD',"
                " '2026-09-01', '06:00:00', 'Asia/Taipei', '2026-08-31 22:00:00',"
                " 'assume_at_due_time', 'scheduled',"
                " 'scheduled_transfer:create:seed', 0, 1, '2026-08-09',"
                " '2026-08-09')"
            )
            if covered:
                connection.exec_driver_sql(
                    "INSERT INTO scheduled_transfer_commitments"
                    " (scheduled_transfer_id, funding_commitment_id, amount_minor,"
                    " created_at) VALUES (1, 1, 8000, '2026-08-09')"
                )
    finally:
        database.engine.dispose()


def _seed_card_payment_entry(settings: AppSettings) -> None:
    """Insert one posted `card_payment` entry with balanced postings."""
    database = create_database(settings)
    try:
        with database.engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO journal_entries (entry_type, effective_at, recorded_at,"
                " idempotency_key, description, created_at, updated_at) VALUES"
                " ('card_payment', '2026-08-31 22:00:00', '2026-09-01 01:00:00',"
                " 'scheduled_transfer:execute:99', '測試扣繳', '2026-09-01',"
                " '2026-09-01')"
            )
    finally:
        database.engine.dispose()


def test_a_new_database_reaches_0013_with_every_object_it_needs(
    database_settings: AppSettings,
) -> None:
    status = upgrade_database(database_settings)

    assert status.is_current
    assert status.current_revision == "0022"
    state = _snapshot(database_settings)
    assert state["tables"] >= B6_TABLES
    assert state["triggers"] >= B6_TRIGGERS | B5_TRIGGERS | B4_TRIGGERS
    assert state["entry_types"] >= B6_ENTRY_TYPES | {"funding_transfer"}
    assert state["integrity"] == "ok"
    assert state["fk_check"] == []
    assert state["foreign_keys"] == 1
    indexes = _schema_objects(database_settings)["indexes"]
    assert set(indexes["scheduled_transfers"]) >= B6_INDEXES


def test_every_new_foreign_key_restricts_deletion(
    database_settings: AppSettings,
) -> None:
    upgrade_database(database_settings)

    foreign_keys = _schema_objects(database_settings)["foreign_keys"]

    assert foreign_keys["scheduled_transfers"]
    assert foreign_keys["scheduled_transfer_commitments"]
    for table, entries in foreign_keys.items():
        for _column, _referenced, on_delete in entries:
            assert on_delete == "RESTRICT", table


def test_upgrading_0012_to_0013_preserves_every_existing_row(
    database_settings: AppSettings,
) -> None:
    config = _config_at(database_settings, "0012")
    seeded = _seed_history(database_settings)
    before = _snapshot(database_settings)

    command.upgrade(config, "0013")

    after = _snapshot(database_settings)
    assert not (B6_TABLES & before["tables"])
    assert after["tables"] >= B6_TABLES
    assert after["triggers"] >= B6_TRIGGERS | B5_TRIGGERS | B4_TRIGGERS
    assert not (B6_ENTRY_TYPES & before["entry_types"])
    assert after["entry_types"] == before["entry_types"] | B6_ENTRY_TYPES
    assert after["journal_effective"] == before["journal_effective"]
    assert after["integrity"] == "ok"
    assert after["fk_check"] == []
    assert after["foreign_keys"] == 1
    assert _row_counts(database_settings) == seeded
    assert inspect_migration_status(database_settings).current_revision == "0013"


def test_0012_to_0013_and_back_round_trips_when_nothing_used_it(
    database_settings: AppSettings,
) -> None:
    config = _config_at(database_settings, "0012")
    seeded = _seed_history(database_settings)
    before = _snapshot(database_settings)

    command.upgrade(config, "0013")
    command.downgrade(config, "0012")

    middle = _snapshot(database_settings)
    assert not (B6_TABLES & middle["tables"])
    assert not (B6_TRIGGERS & middle["triggers"])
    assert middle["entry_types"] == before["entry_types"]
    assert middle["integrity"] == "ok"
    assert middle["fk_check"] == []
    assert _row_counts(database_settings) == seeded
    assert inspect_migration_status(database_settings).current_revision == "0012"

    command.upgrade(config, "0013")

    after = _snapshot(database_settings)
    assert after["tables"] >= B6_TABLES
    assert after["triggers"] >= B6_TRIGGERS
    assert after["entry_types"] == before["entry_types"] | B6_ENTRY_TYPES
    assert after["journal_effective"] == before["journal_effective"]
    assert _row_counts(database_settings) == seeded
    assert inspect_migration_status(database_settings).current_revision == "0013"


def test_downgrade_refuses_once_a_schedule_exists_and_changes_nothing(
    database_settings: AppSettings,
) -> None:
    config = _config_at(database_settings, "0012")
    _seed_history(database_settings)
    command.upgrade(config, "0013")
    _seed_schedule(database_settings)
    before = _snapshot(database_settings)
    before_schema = _schema_objects(database_settings)
    before_counts = _row_counts(database_settings)

    with pytest.raises(Exception, match="refuses to downgrade") as failure:
        command.downgrade(config, "0012")

    message = str(failure.value)
    assert "scheduled_transfers=1" in message
    assert "scheduled_transfer_commitments=1" in message
    # Only counts: no amount, account, wallet, card, or timezone escapes.
    assert "8000" not in message
    assert "Asia/Taipei" not in message
    assert _snapshot(database_settings) == before
    assert _schema_objects(database_settings) == before_schema
    assert _row_counts(database_settings) == before_counts
    assert inspect_migration_status(database_settings).current_revision == "0013"


def test_downgrade_refuses_once_a_card_payment_is_posted(
    database_settings: AppSettings,
) -> None:
    config = _config_at(database_settings, "0012")
    _seed_history(database_settings)
    command.upgrade(config, "0013")
    _seed_card_payment_entry(database_settings)
    before = _snapshot(database_settings)

    with pytest.raises(Exception, match="refuses to downgrade") as failure:
        command.downgrade(config, "0012")

    assert "journal_entries=1" in str(failure.value)
    assert _snapshot(database_settings) == before
    assert inspect_migration_status(database_settings).current_revision == "0013"


def test_a_link_row_alone_also_blocks_the_downgrade(
    database_settings: AppSettings,
) -> None:
    config = _config_at(database_settings, "0012")
    _seed_history(database_settings)
    command.upgrade(config, "0013")
    _seed_schedule(database_settings, covered=False)

    with pytest.raises(Exception, match="scheduled_transfers=1"):
        command.downgrade(config, "0012")

    assert inspect_migration_status(database_settings).current_revision == "0013"


_SEED_STATEMENTS = (
    "INSERT INTO budget_categories (name, is_active, version, created_at,"
    " updated_at) VALUES ('測試分類', 1, 1, '2026-08-09', '2026-08-09')",
    "INSERT INTO financial_accounts (institution_code, display_name,"
    " account_type, currency, is_active, version, created_at, updated_at) VALUES"
    " ('test_bank_a','測試主要帳戶','checking','TWD',1,1,'2026-08-09','2026-08-09')",
    "INSERT INTO financial_accounts (institution_code, display_name,"
    " account_type, currency, is_active, version, created_at, updated_at) VALUES"
    " ('test_bank_b','測試次要帳戶','checking','TWD',1,1,'2026-08-09','2026-08-09')",
    "INSERT INTO wallets (financial_account_id, name, purpose, is_active,"
    " version, created_at, updated_at) VALUES (1,'Unallocated','unallocated',1,1,"
    "'2026-08-09','2026-08-09')",
    "INSERT INTO wallets (financial_account_id, name, purpose, is_active,"
    " version, created_at, updated_at) VALUES (2,'Unallocated','unallocated',1,1,"
    "'2026-08-09','2026-08-09')",
    "INSERT INTO ledger_accounts (code, name, kind, currency,"
    " financial_account_id, created_at, updated_at) VALUES"
    " ('asset:test_bank_a:1','測試主要帳戶','asset','TWD',1,'2026-08-09',"
    "'2026-08-09')",
    "INSERT INTO subscriptions (name, budget_category_id, funding_wallet_id,"
    " currency, billing_cadence, cadence_months, is_active, version, created_at,"
    " updated_at) VALUES ('測試影音', 1, 1, 'TWD', 'monthly', 1, 1, 1,"
    " '2026-08-09', '2026-08-09')",
    "INSERT INTO subscription_price_periods (subscription_id, amount_minor,"
    " effective_from, version, created_at, updated_at) VALUES"
    " (1, 39000, '2026-01-01', 1, '2026-08-09', '2026-08-09')",
    "INSERT INTO financial_events (origin, event_type, payment_method,"
    " recognition_basis, occurred_at, amount_minor, currency, status, version,"
    " idempotency_key, created_at, updated_at) VALUES"
    " ('manual','purchase','credit_card','observed_manual','2026-08-05',8000,"
    "'TWD','posted',1,'test:purchase','2026-08-09','2026-08-09')",
    "INSERT INTO event_allocations (financial_event_id, funding_wallet_id,"
    " amount_minor, currency, allocation_source, created_at, updated_at) VALUES"
    " (1, 2, 8000, 'TWD', 'manual', '2026-08-09', '2026-08-09')",
    "INSERT INTO funding_commitments (financial_event_id, event_allocation_id,"
    " source_wallet_id, settlement_account_id, amount_minor, released_minor,"
    " currency, fulfillment_mode, status, created_at, updated_at) VALUES"
    " (1, 1, 2, 1, 8000, 0, 'TWD', 'cross_bank_transfer', 'open', '2026-08-09',"
    " '2026-08-09')",
    "INSERT INTO journal_entries (entry_type, effective_at, recorded_at,"
    " idempotency_key, description, created_at, updated_at) VALUES"
    " ('expense', '2026-08-05 10:00:00', '2026-08-05 11:00:00', 'test:entry',"
    " '測試支出', '2026-08-09', '2026-08-09')",
    "INSERT INTO journal_entries (entry_type, effective_at, recorded_at,"
    " idempotency_key, reversal_of_id, description, created_at, updated_at)"
    " VALUES ('reversal', '2026-08-05 10:00:00', '2026-08-06 11:00:00',"
    " 'reversal:journal_entry:1', 1, '測試沖銷', '2026-08-09', '2026-08-09')",
    "INSERT INTO journal_postings (journal_entry_id, ledger_account_id,"
    " wallet_id, amount_minor, currency, created_at, updated_at) VALUES"
    " (1, 1, 1, -8000, 'TWD', '2026-08-09', '2026-08-09')",
    "INSERT INTO journal_postings (journal_entry_id, ledger_account_id,"
    " wallet_id, amount_minor, currency, created_at, updated_at) VALUES"
    " (2, 1, 1, 8000, 'TWD', '2026-08-09', '2026-08-09')",
    "INSERT INTO budget_periods (year, month, currency, status, version,"
    " created_at, updated_at) VALUES (2026, 8, 'TWD', 'open', 1, '2026-08-09',"
    " '2026-08-09')",
    "INSERT INTO salary_plan_templates (name, currency, source_account_id,"
    " is_active, version, created_at, updated_at) VALUES ('測試薪資範本', 'TWD',"
    " 1, 1, 1, '2026-08-09', '2026-08-09')",
    "INSERT INTO salary_plan_template_items (template_id, name,"
    " destination_wallet_id, calculation_mode, fixed_amount_minor, display_order,"
    " is_active, version, created_at, updated_at) VALUES (1, '測試項目', 1,"
    " 'fixed', 10000, 0, 1, 1, '2026-08-09', '2026-08-09')",
    "INSERT INTO financial_events (origin, event_type, payment_method,"
    " recognition_basis, occurred_at, amount_minor, currency, status, version,"
    " idempotency_key, created_at, updated_at) VALUES"
    " ('manual','deposit','bank_account','observed_manual','2026-08-05',100000,"
    "'TWD','posted',1,'salary_receipt:seed','2026-08-09','2026-08-09')",
    "INSERT INTO salary_allocation_runs (template_id, template_name_snapshot,"
    " template_version, budget_period_id, salary_event_id, source_account_id,"
    " source_wallet_id, currency, generation, salary_amount_minor,"
    " planned_amount_minor, remainder_minor,"
    " subscription_evidence_schema_version, subscription_evidence_json, status,"
    " generated_at, version, created_at, updated_at) VALUES (1, '測試薪資範本',"
    " 1, 1, 2, 1, 1, 'TWD', 1, 100000, 10000, 90000, 1, '{}', 'draft',"
    " '2026-08-09', 1, '2026-08-09', '2026-08-09')",
    "INSERT INTO salary_allocation_run_items (run_id, template_item_id,"
    " template_item_version, name_snapshot, destination_wallet_id,"
    " destination_account_id, destination_wallet_name_snapshot,"
    " destination_account_name_snapshot, calculation_mode, amount_minor,"
    " currency, display_order, fulfillment_mode, status,"
    " calculation_evidence_json, version, created_at, updated_at) VALUES"
    " (1, 1, 1, '測試項目', 1, 1, 'Unallocated', '測試主要帳戶', 'fixed', 10000,"
    " 'TWD', 0, 'reserve_only', 'draft', '{}', 1, '2026-08-09', '2026-08-09')",
)

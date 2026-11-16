"""Revision 0011 upgrade, refusing downgrade, and data-preservation tests."""

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
from sqlalchemy import Connection, inspect, text
from sqlalchemy.exc import IntegrityError

B5_TABLES = {
    "salary_plan_templates",
    "salary_plan_template_items",
    "salary_allocation_runs",
    "salary_allocation_run_items",
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
B4_INDEXES = {
    "uq_subscriptions_active_name",
    "ix_subscriptions_name_activity",
    "ix_subscriptions_currency_active",
}
B5_ENTRY_TYPES = {"salary_receipt", "funding_transfer"}
_PRESERVED_TABLES = (
    "emails",
    "transactions",
    "financial_events",
    "financial_event_source_links",
    "journal_entries",
    "journal_postings",
    "budget_categories",
    "budget_periods",
    "budget_targets",
    "event_allocations",
    "funding_commitments",
    # `journal_entry_types` is deliberately absent: 0011 legitimately adds two
    # registry rows (ADR-B028), and `_snapshot` compares that set exactly.
    "subscriptions",
    "subscription_price_periods",
    "financial_accounts",
    "wallets",
)


def _seed_existing_rows(settings: AppSettings) -> dict[str, int]:
    """Seed Gmail, ledger, B3, and B4 rows that revision 0011 must not touch."""
    database = create_database(settings)
    try:
        with SqlAlchemyUnitOfWork(database.session_factory) as unit:
            account = unit.accounts.get_or_create("user@example.test")
            outcome = unit.emails.upsert_from_gmail(
                EmailSnapshot(
                    gmail_account_id=account.id,
                    gmail_message_id="gmail-salary-001",
                    gmail_thread_id="thread-salary-001",
                    gmail_history_id="900",
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
        with database.engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO budget_categories (name, is_active, version, created_at,"
                " updated_at) VALUES ('測試分類', 1, 1, '2026-08-09', '2026-08-09')"
            )
            connection.exec_driver_sql(
                "INSERT INTO financial_accounts (institution_code, display_name,"
                " account_type, currency, is_active, version, created_at, updated_at)"
                " VALUES ('test_bank','測試支票帳戶','checking','TWD',1,1,"
                "'2026-08-09','2026-08-09')"
            )
            connection.exec_driver_sql(
                "INSERT INTO wallets (financial_account_id, name, purpose, is_active,"
                " version, created_at, updated_at) VALUES (1,'Unallocated',"
                "'unallocated',1,1,'2026-08-09','2026-08-09')"
            )
            connection.exec_driver_sql(
                "INSERT INTO subscriptions (name, budget_category_id,"
                " funding_wallet_id, currency, billing_cadence, cadence_months,"
                " is_active, version, created_at, updated_at) VALUES ('測試影音',"
                " 1, 1, 'TWD', 'monthly', 1, 1, 1, '2026-08-09', '2026-08-09')"
            )
            connection.exec_driver_sql(
                "INSERT INTO subscription_price_periods (subscription_id,"
                " amount_minor, effective_from, version, created_at, updated_at)"
                " VALUES (1, 39000, '2026-01-01', 1, '2026-08-09', '2026-08-09')"
            )
        return _row_counts(settings)
    finally:
        database.engine.dispose()


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
                "subscription_indexes": {
                    row[1]
                    for row in connection.exec_driver_sql(
                        "PRAGMA index_list(subscriptions)"
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
                    for table in sorted(B5_TABLES)
                },
                "foreign_keys": {
                    table: sorted(
                        (row[2], row[3], row[6])
                        for row in connection.exec_driver_sql(
                            f"PRAGMA foreign_key_list({table})"
                        ).all()
                    )
                    for table in sorted(B5_TABLES)
                },
                "sql": sorted(
                    row[0]
                    for row in connection.exec_driver_sql(
                        "SELECT sql FROM sqlite_master WHERE name LIKE 'salary%'"
                        " AND sql IS NOT NULL"
                    ).all()
                ),
            }
    finally:
        database.engine.dispose()


def _config_at(settings: AppSettings, revision: str) -> Any:
    config = build_alembic_config(settings.database_path)
    upgrade_database(settings)
    command.downgrade(config, revision)
    return config


def _seed_salary_rows(settings: AppSettings) -> None:
    """Insert one template, item, run, and run item directly."""
    database = create_database(settings)
    try:
        with database.engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO financial_events (origin, event_type, payment_method,"
                " recognition_basis, occurred_at, amount_minor, currency, status,"
                " version, idempotency_key, created_at, updated_at) VALUES"
                " ('manual','deposit','bank_account','observed_manual','2026-08-05',"
                " 100000,'TWD','posted',1,'salary_receipt:seed','2026-08-09',"
                "'2026-08-09')"
            )
            connection.exec_driver_sql(
                "INSERT INTO budget_periods (year, month, currency, status, version,"
                " created_at, updated_at) VALUES"
                " (2026, 8, 'TWD', 'open', 1, '2026-08-09', '2026-08-09')"
            )
            connection.exec_driver_sql(
                "INSERT INTO salary_plan_templates (name, currency,"
                " source_account_id, is_active, version, created_at, updated_at)"
                " VALUES ('測試薪資範本', 'TWD', 1, 1, 1, '2026-08-09', '2026-08-09')"
            )
            connection.exec_driver_sql(
                "INSERT INTO salary_plan_template_items (template_id, name,"
                " destination_wallet_id, calculation_mode, fixed_amount_minor,"
                " display_order, is_active, version, created_at, updated_at) VALUES"
                " (1, '測試項目', 1, 'fixed', 10000, 0, 1, 1, '2026-08-09',"
                " '2026-08-09')"
            )
            connection.exec_driver_sql(
                "INSERT INTO salary_allocation_runs (template_id,"
                " template_name_snapshot, template_version, budget_period_id,"
                " salary_event_id, source_account_id, source_wallet_id, currency,"
                " generation, salary_amount_minor, planned_amount_minor,"
                " remainder_minor, subscription_evidence_schema_version,"
                " subscription_evidence_json, status, generated_at, version,"
                " created_at, updated_at) VALUES (1, '測試薪資範本', 1, 1, 1, 1, 1,"
                " 'TWD', 1, 100000, 10000, 90000, 1, '{}', 'draft', '2026-08-09',"
                " 1, '2026-08-09', '2026-08-09')"
            )
            connection.exec_driver_sql(
                "INSERT INTO salary_allocation_run_items (run_id, template_item_id,"
                " template_item_version, name_snapshot, destination_wallet_id,"
                " destination_account_id, destination_wallet_name_snapshot,"
                " destination_account_name_snapshot, calculation_mode, amount_minor,"
                " currency, display_order, fulfillment_mode, status,"
                " calculation_evidence_json, version, created_at, updated_at) VALUES"
                " (1, 1, 1, '測試項目', 1, 1, 'Unallocated', '測試支票帳戶', 'fixed',"
                " 10000, 'TWD', 0, 'reserve_only', 'draft', '{}', 1, '2026-08-09',"
                " '2026-08-09')"
            )
    finally:
        database.engine.dispose()


def test_upgrading_0010_to_0011_preserves_every_existing_row(
    database_settings: AppSettings,
) -> None:
    config = _config_at(database_settings, "0010")
    seeded = _seed_existing_rows(database_settings)
    before = _snapshot(database_settings)

    command.upgrade(config, "0011")

    after = _snapshot(database_settings)
    assert not (B5_TABLES & before["tables"])
    assert after["tables"] >= B5_TABLES
    assert after["triggers"] >= B5_TRIGGERS
    # B4's triggers and indexes survive untouched.
    assert after["triggers"] >= B4_TRIGGERS
    assert after["subscription_indexes"] == before["subscription_indexes"]
    assert after["subscription_indexes"] >= B4_INDEXES
    assert not (B5_ENTRY_TYPES & before["entry_types"])
    assert after["entry_types"] == before["entry_types"] | B5_ENTRY_TYPES
    assert after["integrity"] == "ok"
    assert after["fk_check"] == []
    assert after["foreign_keys"] == 1
    assert _row_counts(database_settings) == seeded
    assert inspect_migration_status(database_settings).current_revision == "0011"


def test_a_new_database_reaches_the_head_from_nothing(
    database_settings: AppSettings,
) -> None:
    status = upgrade_database(database_settings)

    assert status.is_current
    assert status.current_revision == "0022"
    state = _snapshot(database_settings)
    assert state["tables"] >= B5_TABLES
    assert state["triggers"] >= B5_TRIGGERS | B4_TRIGGERS
    assert state["entry_types"] >= B5_ENTRY_TYPES
    assert state["integrity"] == "ok"
    assert state["fk_check"] == []


def test_0011_creates_the_indexes_and_restrict_foreign_keys_it_needs(
    database_settings: AppSettings,
) -> None:
    upgrade_database(database_settings)
    objects = _schema_objects(database_settings)

    assert {
        "uq_salary_plan_templates_active_identity",
        "ix_salary_plan_templates_source_account_id",
        "ix_salary_plan_templates_currency_active",
    } <= set(objects["indexes"]["salary_plan_templates"])
    assert {
        "uq_salary_template_items_active_name",
        "uq_salary_template_items_active_destination",
        "ix_salary_plan_template_items_template_id",
    } <= set(objects["indexes"]["salary_plan_template_items"])
    assert {
        "uq_salary_runs_live_period",
        "uq_salary_runs_live_salary_event",
        "uq_salary_runs_approval_entry",
        "ix_salary_allocation_runs_status",
    } <= set(objects["indexes"]["salary_allocation_runs"])
    assert {
        "uq_salary_run_items_transfer_entry",
        "ix_salary_allocation_run_items_run_id",
    } <= set(objects["indexes"]["salary_allocation_run_items"])

    # Financial history is never cascade-deleted.
    for table, keys in objects["foreign_keys"].items():
        assert keys, table
        assert all(rule == "RESTRICT" for _, _, rule in keys), table
    assert objects["foreign_keys"]["salary_plan_templates"] == [
        ("financial_accounts", "source_account_id", "RESTRICT")
    ]


def test_an_empty_database_round_trips_0011_to_0010_and_back(
    database_settings: AppSettings,
) -> None:
    config = _config_at(database_settings, "0010")
    seeded = _seed_existing_rows(database_settings)
    command.upgrade(config, "0011")
    first = _schema_objects(database_settings)

    command.downgrade(config, "0010")
    rolled = _snapshot(database_settings)
    command.upgrade(config, "0011")

    assert not (B5_TABLES & rolled["tables"])
    assert not (B5_TRIGGERS & rolled["triggers"])
    assert not (B5_ENTRY_TYPES & rolled["entry_types"])
    assert rolled["subscription_indexes"] >= B4_INDEXES
    assert rolled["triggers"] >= B4_TRIGGERS
    assert rolled["integrity"] == "ok"
    assert rolled["fk_check"] == []
    assert rolled["foreign_keys"] == 1
    assert _schema_objects(database_settings) == first
    assert _row_counts(database_settings) == seeded
    assert inspect_migration_status(database_settings).current_revision == "0011"


def test_a_downgrade_is_refused_once_salary_rows_exist(
    database_settings: AppSettings,
) -> None:
    """Dropping these tables would delete a recorded plan, so it must refuse."""
    config = _config_at(database_settings, "0010")
    _seed_existing_rows(database_settings)
    command.upgrade(config, "0011")
    _seed_salary_rows(database_settings)
    before = _snapshot(database_settings)
    before_objects = _schema_objects(database_settings)
    before_counts = _row_counts(database_settings)

    command.downgrade(config, "0011")
    with pytest.raises(Exception, match="salary history already exists") as failure:
        command.downgrade(config, "0010")

    message = str(failure.value)
    # Counts only: a template name and an amount are user data.
    assert "salary_plan_templates=1" in message
    assert "測試薪資範本" not in message
    assert "100000" not in message
    after = _snapshot(database_settings)
    assert inspect_migration_status(database_settings).current_revision == "0011"
    assert after["tables"] == before["tables"]
    assert after["triggers"] == before["triggers"]
    assert after["entry_types"] == before["entry_types"]
    assert after["integrity"] == "ok"
    assert after["fk_check"] == []
    assert _schema_objects(database_settings) == before_objects
    assert _row_counts(database_settings) == before_counts


def test_a_downgrade_is_refused_once_a_salary_entry_type_was_posted(
    database_settings: AppSettings,
) -> None:
    """Removing a registry row would orphan the entry that already uses it."""
    config = _config_at(database_settings, "0010")
    _seed_existing_rows(database_settings)
    command.upgrade(config, "0011")
    database = create_database(database_settings)
    try:
        with database.engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO journal_entries (entry_type, effective_at, recorded_at,"
                " idempotency_key, description, created_at, updated_at) VALUES"
                " ('salary_receipt', '2026-08-05', '2026-08-05', 'k', '',"
                " '2026-08-09', '2026-08-09')"
            )
    finally:
        database.engine.dispose()

    command.downgrade(config, "0011")
    with pytest.raises(Exception, match="journal_entries=1"):
        command.downgrade(config, "0010")

    assert inspect_migration_status(database_settings).current_revision == "0011"
    state = _snapshot(database_settings)
    assert state["entry_types"] >= B5_ENTRY_TYPES
    assert state["tables"] >= B5_TABLES


def test_a_failure_after_0011_rolls_back_schema_data_and_revision(
    database_settings: AppSettings,
) -> None:
    """Foreign keys are unenforced during a migration, so the check must bite."""
    config = _config_at(database_settings, "0010")
    seeded = _seed_existing_rows(database_settings)

    def _explode(connection: Connection) -> None:
        connection.exec_driver_sql(
            "INSERT INTO salary_plan_templates (name, currency, source_account_id,"
            " is_active, version, created_at, updated_at) VALUES ('會被回滾', 'TWD',"
            " 1, 1, 1, '2026-08-09', '2026-08-09')"
        )
        raise RuntimeError("migration failed after applying schema")

    config.attributes["verify_before_commit"] = _explode
    try:
        with pytest.raises(RuntimeError, match="failed after applying schema"):
            command.upgrade(config, "0011")
    finally:
        config.attributes.pop("verify_before_commit", None)

    after = _snapshot(database_settings)
    assert inspect_migration_status(database_settings).current_revision == "0010"
    assert not (B5_TABLES & after["tables"])
    assert not (B5_TRIGGERS & after["triggers"])
    assert not (B5_ENTRY_TYPES & after["entry_types"])
    assert after["foreign_keys"] == 1
    assert _row_counts(database_settings) == seeded


def test_an_orphan_created_while_enforcement_is_off_blocks_0011(
    database_settings: AppSettings,
) -> None:
    config = _config_at(database_settings, "0010")
    seeded = _seed_existing_rows(database_settings)

    def _orphan(connection: Connection) -> None:
        connection.exec_driver_sql(
            "INSERT INTO salary_plan_templates (name, currency, source_account_id,"
            " is_active, version, created_at, updated_at) VALUES ('孤兒', 'TWD',"
            " 9999, 1, 1, '2026-08-09', '2026-08-09')"
        )

    config.attributes["verify_before_commit"] = _orphan
    try:
        with pytest.raises(RuntimeError, match="foreign key violations"):
            command.upgrade(config, "0011")
    finally:
        config.attributes.pop("verify_before_commit", None)

    after = _snapshot(database_settings)
    assert inspect_migration_status(database_settings).current_revision == "0010"
    assert not (B5_TABLES & after["tables"])
    assert after["foreign_keys"] == 1
    assert _row_counts(database_settings) == seeded


def test_the_frozen_column_triggers_are_enforced_by_the_database_itself(
    database_settings: AppSettings,
) -> None:
    upgrade_database(database_settings)
    _seed_existing_rows(database_settings)
    _seed_salary_rows(database_settings)
    database = create_database(database_settings)
    try:
        for statement in (
            "UPDATE salary_allocation_runs SET planned_amount_minor = 1 WHERE id = 1",
            "UPDATE salary_allocation_runs SET currency = 'JPY' WHERE id = 1",
            "UPDATE salary_allocation_run_items SET amount_minor = 1 WHERE id = 1",
            "UPDATE salary_allocation_run_items SET fulfillment_mode ="
            " 'cross_bank_transfer' WHERE id = 1",
        ):
            with (
                database.engine.begin() as connection,
                pytest.raises(IntegrityError, match="snapshot cannot be changed"),
            ):
                connection.exec_driver_sql(statement)
        # The mutable columns still move.
        with database.engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE salary_allocation_runs SET status = 'cancelled',"
                " cancelled_at = '2026-08-09', version = 2 WHERE id = 1"
            )
            row = connection.exec_driver_sql(
                "SELECT status, planned_amount_minor FROM salary_allocation_runs"
                " WHERE id = 1"
            ).one()
    finally:
        database.engine.dispose()

    assert tuple(row) == ("cancelled", 10000)


def test_alembic_reports_no_difference_between_head_and_the_models(
    database_settings: AppSettings,
) -> None:
    upgrade_database(database_settings)
    config = build_alembic_config(database_settings.database_path)

    command.check(config)


# --- 0012: which draft a generation replaced, and on whose request ----------


B5B_COLUMNS = {"regenerated_from_run_id", "request_fingerprint"}
B5B_INDEX = "uq_salary_runs_regenerated_from"
B5_RUN_INDEXES = {
    "ix_salary_allocation_runs_budget_period_id",
    "ix_salary_allocation_runs_source_wallet_id",
    "ix_salary_allocation_runs_status",
    "uq_salary_runs_approval_entry",
    "uq_salary_runs_live_period",
    "uq_salary_runs_live_salary_event",
}


def _run_columns(settings: AppSettings) -> set[str]:
    database = create_database(settings)
    try:
        with database.engine.connect() as connection:
            return {
                row[1]
                for row in connection.exec_driver_sql(
                    "PRAGMA table_info(salary_allocation_runs)"
                ).all()
            }
    finally:
        database.engine.dispose()


def _run_indexes(settings: AppSettings) -> set[str]:
    database = create_database(settings)
    try:
        with database.engine.connect() as connection:
            return {
                row[1]
                for row in connection.exec_driver_sql(
                    "PRAGMA index_list(salary_allocation_runs)"
                ).all()
            }
    finally:
        database.engine.dispose()


def _run_rows(settings: AppSettings) -> list[tuple[Any, ...]]:
    database = create_database(settings)
    try:
        with database.engine.connect() as connection:
            return [
                tuple(row)
                for row in connection.exec_driver_sql(
                    "SELECT id, template_id, generation, salary_amount_minor,"
                    " planned_amount_minor, remainder_minor, status,"
                    " subscription_evidence_json, created_at, updated_at"
                    " FROM salary_allocation_runs ORDER BY id"
                ).all()
            ]
    finally:
        database.engine.dispose()


def test_upgrading_0011_to_0012_keeps_every_salary_row_and_index(
    database_settings: AppSettings,
) -> None:
    config = _config_at(database_settings, "0010")
    _seed_existing_rows(database_settings)
    command.upgrade(config, "0011")
    _seed_salary_rows(database_settings)
    # Recorded after the salary rows, which also add an event and a period.
    seeded = _row_counts(database_settings)
    before_rows = _run_rows(database_settings)
    assert not (B5B_COLUMNS & _run_columns(database_settings))

    command.upgrade(config, "0012")

    state = _snapshot(database_settings)
    assert _run_columns(database_settings) >= B5B_COLUMNS
    assert B5_RUN_INDEXES | {B5B_INDEX} <= _run_indexes(database_settings)
    assert state["triggers"] >= B5_TRIGGERS
    assert state["integrity"] == "ok"
    assert state["fk_check"] == []
    assert state["foreign_keys"] == 1
    assert _run_rows(database_settings) == before_rows
    assert _row_counts(database_settings) == seeded
    assert inspect_migration_status(database_settings).current_revision == "0012"


def test_0012_round_trips_with_real_salary_rows(
    database_settings: AppSettings,
) -> None:
    config = _config_at(database_settings, "0010")
    _seed_existing_rows(database_settings)
    command.upgrade(config, "0011")
    _seed_salary_rows(database_settings)
    seeded = _row_counts(database_settings)
    command.upgrade(config, "0012")
    upgraded_rows = _run_rows(database_settings)
    upgraded_indexes = _run_indexes(database_settings)

    command.downgrade(config, "0011")
    rolled_columns = _run_columns(database_settings)
    rolled_indexes = _run_indexes(database_settings)
    rolled = _snapshot(database_settings)
    command.upgrade(config, "0012")

    assert not (B5B_COLUMNS & rolled_columns)
    assert rolled_indexes >= B5_RUN_INDEXES
    assert B5B_INDEX not in rolled_indexes
    assert rolled["triggers"] >= B5_TRIGGERS
    assert rolled["integrity"] == "ok"
    assert rolled["fk_check"] == []
    assert _run_rows(database_settings) == upgraded_rows
    assert _run_indexes(database_settings) == upgraded_indexes
    assert _row_counts(database_settings) == seeded
    assert inspect_migration_status(database_settings).current_revision == "0012"


def _record_provenance(settings: AppSettings) -> None:
    """Write a second generation that records the draft it replaced."""
    database = create_database(settings)
    try:
        with database.engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE salary_allocation_runs SET status = 'cancelled',"
                " cancelled_at = '2026-08-09' WHERE id = 1"
            )
            connection.exec_driver_sql(
                "INSERT INTO salary_allocation_runs (template_id,"
                " template_name_snapshot, template_version, budget_period_id,"
                " salary_event_id, source_account_id, source_wallet_id, currency,"
                " generation, salary_amount_minor, planned_amount_minor,"
                " remainder_minor, subscription_evidence_schema_version,"
                " subscription_evidence_json, status, generated_at, version,"
                " regenerated_from_run_id, request_fingerprint, created_at,"
                " updated_at) VALUES (1, '測試薪資範本', 1, 1, 1, 1, 1, 'TWD', 2,"
                " 100000, 20000, 80000, 1, '{}', 'draft', '2026-08-09', 1, 1,"
                " '{\"manual\":[]}', '2026-08-09', '2026-08-09')"
            )
    finally:
        database.engine.dispose()


def test_a_generation_may_not_replace_itself_or_share_a_predecessor(
    database_settings: AppSettings,
) -> None:
    """Checked on insert: the frozen trigger already blocks every update."""
    upgrade_database(database_settings)
    _seed_existing_rows(database_settings)
    _seed_salary_rows(database_settings)
    _record_provenance(database_settings)
    insert = (
        "INSERT INTO salary_allocation_runs (id, template_id,"
        " template_name_snapshot, template_version, budget_period_id,"
        " salary_event_id, source_account_id, source_wallet_id, currency,"
        " generation, salary_amount_minor, planned_amount_minor,"
        " remainder_minor, subscription_evidence_schema_version,"
        " subscription_evidence_json, status, generated_at, cancelled_at, version,"
        " regenerated_from_run_id, request_fingerprint, created_at, updated_at)"
        " VALUES (?, 1, '測試薪資範本', 1, 1, 1, 1, 1, 'TWD', ?, 100000, 20000,"
        " 80000, 1, '{}', 'cancelled', '2026-08-09', '2026-08-09', 1, ?, ?,"
        " '2026-08-09', '2026-08-09')"
    )
    database = create_database(database_settings)
    try:
        # A generation can never say it replaced itself.
        with (
            database.engine.begin() as connection,
            pytest.raises(IntegrityError, match="CHECK constraint failed"),
        ):
            connection.exec_driver_sql(insert, (9, 3, 9, "{}"))
        # Two generations can never claim the same predecessor.
        with (
            database.engine.begin() as connection,
            pytest.raises(IntegrityError, match="UNIQUE constraint failed"),
        ):
            connection.exec_driver_sql(insert, (10, 4, 1, "{}"))
        # Provenance without a fingerprint, or the other way round, is refused.
        with (
            database.engine.begin() as connection,
            pytest.raises(IntegrityError, match="CHECK constraint failed"),
        ):
            connection.exec_driver_sql(insert, (11, 5, 2, None))
        with (
            database.engine.begin() as connection,
            pytest.raises(IntegrityError, match="CHECK constraint failed"),
        ):
            connection.exec_driver_sql(insert, (12, 6, None, "{}"))
    finally:
        database.engine.dispose()


def test_the_frozen_trigger_covers_the_provenance_columns(
    database_settings: AppSettings,
) -> None:
    upgrade_database(database_settings)
    _seed_existing_rows(database_settings)
    _seed_salary_rows(database_settings)
    _record_provenance(database_settings)
    database = create_database(database_settings)
    try:
        for statement in (
            "UPDATE salary_allocation_runs SET regenerated_from_run_id = NULL,"
            " request_fingerprint = NULL WHERE id = 2",
            "UPDATE salary_allocation_runs SET request_fingerprint = '{}' WHERE id = 2",
        ):
            with (
                database.engine.begin() as connection,
                pytest.raises(IntegrityError, match="snapshot cannot be changed"),
            ):
                connection.exec_driver_sql(statement)
    finally:
        database.engine.dispose()


def test_a_downgrade_is_refused_once_a_regeneration_was_recorded(
    database_settings: AppSettings,
) -> None:
    config = _config_at(database_settings, "0010")
    _seed_existing_rows(database_settings)
    command.upgrade(config, "0011")
    _seed_salary_rows(database_settings)
    command.upgrade(config, "0012")
    _record_provenance(database_settings)
    before_rows = _run_rows(database_settings)
    before_columns = _run_columns(database_settings)

    with pytest.raises(Exception, match="record which draft they replaced") as failure:
        command.downgrade(config, "0011")

    message = str(failure.value)
    assert "1 salary generation(s)" in message
    # Counts only: no template name and no amount belongs in a migration error.
    assert "測試薪資範本" not in message
    assert "100000" not in message
    assert inspect_migration_status(database_settings).current_revision == "0012"
    assert _run_columns(database_settings) == before_columns
    assert _run_rows(database_settings) == before_rows
    assert _snapshot(database_settings)["integrity"] == "ok"

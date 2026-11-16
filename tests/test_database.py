"""Migration, schema, and SQLite connection-policy tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic import command
from app.domain.errors import MigrationError
from app.infrastructure.config.settings import AppSettings
from app.infrastructure.database.engine import Database, create_database
from app.infrastructure.database.migrations import (
    build_alembic_config,
    inspect_migration_status,
    upgrade_database,
)
from sqlalchemy import inspect, text

EXPECTED_TABLES = {
    "alembic_version",
    "app_settings",
    "balance_snapshots",
    "classification_results",
    "budget_categories",
    "budget_periods",
    "budget_targets",
    "cards",
    "card_import_assignments",
    "dedicated_wallet_allocations",
    "import_batches",
    "import_rows",
    "legacy_daily_activities",
    "email_attachments",
    "event_allocations",
    "emails",
    "financial_accounts",
    "financial_event_source_links",
    "financial_events",
    "funding_commitments",
    "gmail_accounts",
    "gmail_label_operations",
    "journal_entries",
    "journal_entry_types",
    "journal_postings",
    "ledger_accounts",
    "parser_runs",
    "salary_allocation_run_items",
    "salary_allocation_runs",
    "salary_plan_template_items",
    "salary_plan_templates",
    "scheduled_transfer_commitments",
    "scheduled_transfers",
    "sender_rules",
    "subscription_price_periods",
    "subscriptions",
    "supplementary_card_settings",
    "sync_run_items",
    "sync_runs",
    "sync_states",
    "transactions",
    "wallets",
}


def test_initial_migration_creates_complete_schema_and_indexes(
    migrated_database: Database,
) -> None:
    inspector = inspect(migrated_database.engine)

    assert set(inspector.get_table_names()) == EXPECTED_TABLES
    email_indexes = {item["name"] for item in inspector.get_indexes("emails")}
    transaction_indexes = {
        item["name"] for item in inspector.get_indexes("transactions")
    }
    sync_run_indexes = {item["name"] for item in inspector.get_indexes("sync_runs")}
    assert {
        "ix_emails_received_at",
        "ix_emails_sender",
        "ix_emails_classification_status",
    } <= email_indexes
    assert {
        "ix_transactions_transaction_at",
        "ix_transactions_merchant",
        "ix_transactions_currency",
        "ix_transactions_amount_minor",
    } <= transaction_indexes
    assert "uq_sync_runs_one_running_per_account" in sync_run_indexes
    supplementary_indexes = {
        item["name"] for item in inspector.get_indexes("supplementary_card_settings")
    }
    assert "ix_supplementary_card_settings_lookup" in supplementary_indexes
    sender_checks = {
        item["name"] for item in inspector.get_check_constraints("sender_rules")
    }
    transaction_checks = {
        item["name"] for item in inspector.get_check_constraints("transactions")
    }
    assert "ck_sender_rules_sender_enabled_boolean" in sender_checks
    assert "ck_transactions_transaction_manually_confirmed_boolean" in (
        transaction_checks
    )


def test_sqlite_connection_policy_and_migration_revision(
    migrated_database: Database,
    database_settings: AppSettings,
) -> None:
    assert migrated_database.engine.hide_parameters
    with migrated_database.engine.connect() as connection:
        foreign_keys = connection.scalar(text("PRAGMA foreign_keys"))
        journal_mode = connection.scalar(text("PRAGMA journal_mode"))
        synchronous = connection.scalar(text("PRAGMA synchronous"))
        busy_timeout = connection.scalar(text("PRAGMA busy_timeout"))

    status = inspect_migration_status(database_settings)

    assert foreign_keys == 1
    assert journal_mode == "wal"
    assert synchronous == 2
    assert busy_timeout == 5000
    assert status.current_revision == "0022"
    assert status.is_current
    assert upgrade_database(database_settings).is_current
    command.check(build_alembic_config(database_settings.database_path))
    if os.name == "posix":
        assert database_settings.database_path.stat().st_mode & 0o777 == 0o600


def test_missing_database_reports_migration_error(tmp_path: Path) -> None:
    settings = AppSettings(
        data_dir=tmp_path / "missing",
        database_path=tmp_path / "missing/app.sqlite3",
        log_dir=tmp_path / "missing/logs",
    )

    with pytest.raises(MigrationError, match="database not found"):
        inspect_migration_status(settings)


def test_initial_migration_can_downgrade_and_upgrade_again(
    database_settings: AppSettings,
) -> None:
    upgrade_database(database_settings)
    config = build_alembic_config(database_settings.database_path)

    command.downgrade(config, "base")
    database = create_database(database_settings)
    try:
        assert "emails" not in inspect(database.engine).get_table_names()
    finally:
        database.engine.dispose()

    assert upgrade_database(database_settings).is_current

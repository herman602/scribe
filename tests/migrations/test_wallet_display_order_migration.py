"""Revision 0018 seeds a sheet arrangement without disturbing any balance."""

from __future__ import annotations

from alembic import command
from app.infrastructure.config.settings import AppSettings
from app.infrastructure.database.engine import create_database
from app.infrastructure.database.migrations import (
    build_alembic_config,
    inspect_migration_status,
    upgrade_database,
)
from sqlalchemy import inspect, text

_ACCOUNT = """
INSERT INTO financial_accounts
    (id, institution_code, display_name, account_type, currency, is_active,
     version, created_at, updated_at)
VALUES (1, 'test_bank', '測試支票帳戶', 'checking', 'TWD', 1, 1,
        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
"""

#: Written in the order the wallets were created, with the residual envelope
#: first, which is exactly the shape revision 0017 left behind.
_WALLETS = """
INSERT INTO wallets
    (id, financial_account_id, name, purpose, is_active, version,
     created_at, updated_at)
VALUES
    (1, 1, 'Unallocated', 'unallocated', 1, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
    (2, 1, '緊急預備金', 'general', 1, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
    (3, 1, '日常預備金', 'general', 1, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
    (4, 1, '利息', 'general', 1, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
"""


def _wallet_names(settings: AppSettings) -> list[str]:
    database = create_database(settings)
    try:
        with database.engine.connect() as connection:
            return [
                str(name)
                for name in connection.scalars(
                    text("SELECT name FROM wallets ORDER BY display_order, id")
                ).all()
            ]
    finally:
        database.engine.dispose()


def test_0018_seeds_creation_order_and_sends_the_residual_last(
    database_settings: AppSettings,
) -> None:
    upgrade_database(database_settings)
    config = build_alembic_config(database_settings.database_path)
    command.downgrade(config, "0017")

    database = create_database(database_settings)
    try:
        with database.engine.begin() as connection:
            connection.execute(text(_ACCOUNT))
            connection.execute(text(_WALLETS))
    finally:
        database.engine.dispose()

    command.upgrade(config, "0018")

    assert inspect_migration_status(database_settings).current_revision == "0018"
    # Creation order is preserved exactly, and the residual envelope — which
    # the arrangement being reproduced has no row for — moves to the end.
    assert _wallet_names(database_settings) == [
        "緊急預備金",
        "日常預備金",
        "利息",
        "Unallocated",
    ]

    database = create_database(database_settings)
    try:
        with database.engine.connect() as connection:
            assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert "display_order" in {
            column["name"] for column in inspect(database.engine).get_columns("wallets")
        }
    finally:
        database.engine.dispose()


def test_0018_round_trips_because_an_arrangement_is_presentation_only(
    database_settings: AppSettings,
) -> None:
    upgrade_database(database_settings)
    config = build_alembic_config(database_settings.database_path)

    command.downgrade(config, "0017")
    assert inspect_migration_status(database_settings).current_revision == "0017"

    command.upgrade(config, "0018")
    assert inspect_migration_status(database_settings).current_revision == "0018"

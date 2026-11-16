"""Revision 0019 seeds the sheet's column order from creation order."""

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

#: Deliberately written so that institution code and display name disagree
#: with creation order, which is what revision 0018 sorted by.
_ACCOUNTS = """
INSERT INTO financial_accounts
    (id, institution_code, display_name, account_type, currency, is_active,
     version, created_at, updated_at)
VALUES
    (1, 'zeta_bank', '第一個開的', 'checking', 'TWD', 1, 1,
     CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
    (2, 'alpha_bank', '第二個開的', 'checking', 'TWD', 1, 1,
     CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
"""


def test_0019_seeds_creation_order_rather_than_how_a_bank_is_spelled(
    database_settings: AppSettings,
) -> None:
    upgrade_database(database_settings)
    config = build_alembic_config(database_settings.database_path)
    command.downgrade(config, "0018")

    database = create_database(database_settings)
    try:
        with database.engine.begin() as connection:
            connection.execute(text(_ACCOUNTS))
    finally:
        database.engine.dispose()

    command.upgrade(config, "0019")

    assert inspect_migration_status(database_settings).current_revision == "0019"
    database = create_database(database_settings)
    try:
        with database.engine.connect() as connection:
            names = [
                str(name)
                for name in connection.scalars(
                    text(
                        "SELECT display_name FROM financial_accounts "
                        "ORDER BY display_order, id"
                    )
                ).all()
            ]
            assert names == ["第一個開的", "第二個開的"]
            assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert "display_order" in {
            column["name"]
            for column in inspect(database.engine).get_columns("financial_accounts")
        }
    finally:
        database.engine.dispose()


def test_0019_round_trips_because_an_arrangement_is_presentation_only(
    database_settings: AppSettings,
) -> None:
    upgrade_database(database_settings)
    config = build_alembic_config(database_settings.database_path)

    command.downgrade(config, "0018")
    assert inspect_migration_status(database_settings).current_revision == "0018"

    command.upgrade(config, "0019")
    assert inspect_migration_status(database_settings).current_revision == "0019"

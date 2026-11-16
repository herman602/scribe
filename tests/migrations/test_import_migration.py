"""Revision 0016 keeps pre-opening evidence immutable and downgrade-safe."""

from __future__ import annotations

import pytest
from alembic import command
from app.infrastructure.config.settings import AppSettings
from app.infrastructure.database.engine import Database
from app.infrastructure.database.migrations import (
    build_alembic_config,
    inspect_migration_status,
)
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError


def test_0016_schema_has_restrict_links_and_immutable_triggers(
    migrated_database: Database,
) -> None:
    inspector = inspect(migrated_database.engine)
    foreign_keys = {
        item["referred_table"]: item["options"].get("ondelete")
        for item in inspector.get_foreign_keys("legacy_daily_activities")
    }
    with migrated_database.engine.connect() as connection:
        triggers = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='trigger' AND name LIKE '%daily_activities%'"
                )
            )
        }

    assert foreign_keys == {"import_rows": "RESTRICT"}
    assert triggers == {
        "legacy_daily_activities_no_update",
        "legacy_daily_activities_no_delete",
    }


def test_0016_refuses_to_mutate_or_downgrade_import_history(
    migrated_database: Database,
    database_settings: AppSettings,
) -> None:
    with migrated_database.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO import_batches "
                "(source_kind,format_version,content_sha256,source_locator_hash,"
                "observed_at,cutoff_at,status,row_count,warning_count,applied_at) "
                "VALUES ('test',1,:hash,:locator,:at,:at,'applied',1,0,:at)"
            ),
            {"hash": "a" * 64, "locator": "b" * 64, "at": "2026-08-14 00:00:00"},
        )
        connection.execute(
            text(
                "INSERT INTO import_rows "
                "(batch_id,entity_kind,source_row_key,normalized_payload_json,status) "
                "VALUES (1,'legacy_daily_activity','row:1','{}','historical')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO legacy_daily_activities "
                "(import_row_id,activity_date,sequence,amount_minor,currency,"
                "payment_method,inclusion_mode) "
                "VALUES (1,'2026-08-01',1,100,'TWD','cash','pre_opening_history')"
            )
        )
    with (
        pytest.raises(IntegrityError, match="immutable"),
        migrated_database.engine.begin() as connection,
    ):
        connection.execute(
            text("UPDATE legacy_daily_activities SET amount_minor=200 WHERE id=1")
        )

    with pytest.raises(Exception, match="refuses to downgrade"):
        command.downgrade(build_alembic_config(database_settings.database_path), "0015")

    assert inspect_migration_status(database_settings).current_revision == "0022"
    with migrated_database.engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT amount_minor FROM legacy_daily_activities"))
            == 100
        )

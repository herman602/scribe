"""Verified backups and an explicit restore (ADR-B098)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from app.domain.errors import DatabaseError, MigrationError
from app.infrastructure.config.settings import AppSettings
from app.infrastructure.database.backup import (
    DatabaseFacts,
    check_restorable,
    create_backup,
    list_backups,
    read_facts,
    restore_backup,
)
from app.infrastructure.database.engine import Database, create_database
from app.infrastructure.database.locking import (
    DatabaseInUseError,
    SharedDatabaseLock,
    exclusive_database_lock,
)
from app.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork


def _accounts(path: Path) -> int:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return int(
            connection.execute("SELECT COUNT(*) FROM financial_accounts").fetchone()[0]
        )
    finally:
        connection.close()


def _add_account(database: Database, name: str) -> None:
    from app.domain.budget_enums import FinancialAccountType
    from app.domain.budget_models import FinancialAccountDraft

    with SqlAlchemyUnitOfWork(database.session_factory) as unit:
        unit.financial_accounts.add(
            FinancialAccountDraft(
                institution_code="test_bank",
                display_name=name,
                account_type=FinancialAccountType.CHECKING,
                currency="TWD",
            )
        )
        unit.commit()


def test_a_backup_is_verified_against_the_database_it_came_from(
    migrated_database: Database, database_settings: AppSettings, tmp_path: Path
) -> None:
    _add_account(migrated_database, "測試帳戶")

    result = create_backup(database_settings.database_path, tmp_path / "backups")

    assert result.verified
    assert result.copy == result.live
    assert result.copy.revision is not None
    assert result.copy.is_sound
    assert result.path.stat().st_mode & 0o777 == 0o600


def test_a_backup_captures_writes_still_in_the_write_ahead_log(
    migrated_database: Database, database_settings: AppSettings, tmp_path: Path
) -> None:
    """A plain file copy would miss these; the backup API must not."""
    _add_account(migrated_database, "只存在於 WAL 的帳戶")

    result = create_backup(database_settings.database_path, tmp_path / "backups")

    assert _accounts(result.path) == _accounts(database_settings.database_path) == 1


def test_a_manifest_records_the_revision_and_app_version(
    migrated_database: Database, database_settings: AppSettings, tmp_path: Path
) -> None:
    del migrated_database
    result = create_backup(database_settings.database_path, tmp_path / "backups")

    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["app_version"]
    assert manifest["live"]["revision"] == result.live.revision
    assert manifest["matches_live"] is True


def test_backups_are_listed_newest_first(
    migrated_database: Database, database_settings: AppSettings, tmp_path: Path
) -> None:
    del migrated_database
    from datetime import UTC, datetime

    directory = tmp_path / "backups"
    older = create_backup(
        database_settings.database_path,
        directory,
        now=datetime(2026, 8, 1, tzinfo=UTC),
    )
    newer = create_backup(
        database_settings.database_path,
        directory,
        now=datetime(2026, 8, 2, tzinfo=UTC),
    )

    listed = list_backups(directory)

    assert set(listed) == {older.path, newer.path}
    assert listed[0].stat().st_mtime >= listed[1].stat().st_mtime


def test_restoring_brings_back_the_state_the_backup_held(
    migrated_database: Database, database_settings: AppSettings, tmp_path: Path
) -> None:
    _add_account(migrated_database, "備份時就有的")
    backup = create_backup(database_settings.database_path, tmp_path / "backups")
    _add_account(migrated_database, "備份之後才建的")
    assert _accounts(database_settings.database_path) == 2
    migrated_database.engine.dispose()

    rollback = restore_backup(
        backup.path, database_settings.database_path, tmp_path / "backups"
    )

    assert _accounts(database_settings.database_path) == 1
    # The replaced database is kept, so a restore is itself undoable.
    assert _accounts(rollback) == 2


def test_restoring_refuses_a_backup_that_fails_its_own_checks(
    migrated_database: Database, database_settings: AppSettings, tmp_path: Path
) -> None:
    del migrated_database
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"this is not a database")

    with pytest.raises(DatabaseError):
        restore_backup(corrupt, database_settings.database_path, tmp_path / "backups")


def test_reading_a_missing_database_is_refused(tmp_path: Path) -> None:
    with pytest.raises(DatabaseError, match="no database"):
        read_facts(tmp_path / "absent.sqlite3")


def test_an_unbalanced_backup_is_refused_before_anything_is_replaced(
    migrated_database: Database, database_settings: AppSettings, tmp_path: Path
) -> None:
    """SQLite calls a broken ledger valid; the restore must not (ADR-B098)."""
    _add_account(migrated_database, "原本的")
    backup = create_backup(database_settings.database_path, tmp_path / "backups")
    migrated_database.engine.dispose()

    planted = sqlite3.connect(backup.path)
    try:
        planted.execute(
            "INSERT INTO journal_entries (id, entry_type, effective_at, "
            "recorded_at, idempotency_key, description, created_at, updated_at) "
            "VALUES (9999,'adjustment','2026-08-01','2026-08-01','probe','probe',"
            "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
        )
        planted.execute(
            "INSERT INTO ledger_accounts (id, code, name, kind, currency, "
            "created_at, updated_at) VALUES (9999,'probe','probe','asset','TWD',"
            "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
        )
        planted.execute(
            "INSERT INTO journal_postings (journal_entry_id, ledger_account_id, "
            "amount_minor, currency, created_at, updated_at) "
            "VALUES (9999, 9999, 12345, 'TWD', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        planted.commit()
    finally:
        planted.close()

    before = read_facts(database_settings.database_path)
    with pytest.raises(DatabaseError, match="does not balance"):
        restore_backup(
            backup.path, database_settings.database_path, tmp_path / "backups"
        )

    # The live database must be untouched, not restored-then-reported.
    assert read_facts(database_settings.database_path) == before


def test_a_backup_of_an_unbalanced_database_is_not_reported_as_verified(
    migrated_database: Database, database_settings: AppSettings, tmp_path: Path
) -> None:
    del migrated_database
    facts = DatabaseFacts(
        revision="0019",
        integrity="ok",
        foreign_key_violations=0,
        counts={},
        currency_totals={"TWD": 5},
    )

    assert facts.is_sound
    assert not facts.journal_balances
    assert check_restorable(facts, expected_revision="0019")


def test_a_restore_refuses_a_revision_this_build_does_not_run(
    migrated_database: Database, database_settings: AppSettings, tmp_path: Path
) -> None:
    del migrated_database
    backup = create_backup(database_settings.database_path, tmp_path / "backups")

    with pytest.raises(DatabaseError, match="is not the"):
        restore_backup(
            backup.path,
            database_settings.database_path,
            tmp_path / "backups",
            expected_revision="9999",
        )


def test_a_restore_is_refused_while_another_process_holds_the_database(
    migrated_database: Database, database_settings: AppSettings, tmp_path: Path
) -> None:
    """The serving process holds a shared claim for its whole life (ADR-B100)."""
    _add_account(migrated_database, "服務持有中")
    backup = create_backup(database_settings.database_path, tmp_path / "backups")
    before = read_facts(database_settings.database_path)

    with (
        SharedDatabaseLock(database_settings.database_path),
        pytest.raises(DatabaseInUseError),
    ):
        restore_backup(
            backup.path, database_settings.database_path, tmp_path / "backups"
        )

    assert read_facts(database_settings.database_path) == before


def test_a_checkpointed_database_with_an_open_connection_is_still_protected(
    migrated_database: Database, database_settings: AppSettings, tmp_path: Path
) -> None:
    """An empty write-ahead log proves nothing; only the lock does.

    This is the case the previous heuristic missed entirely: after a
    checkpoint the sidecar is zero bytes and `BEGIN EXCLUSIVE` succeeds, while
    a connection is still open.
    """
    _add_account(migrated_database, "已 checkpoint")
    backup = create_backup(database_settings.database_path, tmp_path / "backups")

    connection = sqlite3.connect(database_settings.database_path)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        wal = database_settings.database_path.with_name(
            database_settings.database_path.name + "-wal"
        )
        assert not wal.is_file() or wal.stat().st_size == 0

        with (
            SharedDatabaseLock(database_settings.database_path),
            pytest.raises(DatabaseInUseError),
        ):
            restore_backup(
                backup.path, database_settings.database_path, tmp_path / "backups"
            )
    finally:
        connection.close()


def test_a_failed_restore_leaves_the_write_ahead_log_alone(
    migrated_database: Database, database_settings: AppSettings, tmp_path: Path
) -> None:
    """Deleting sidecars on failure would discard committed, un-checkpointed data."""
    _add_account(migrated_database, "尚未 checkpoint 的資料")
    backup = create_backup(database_settings.database_path, tmp_path / "backups")
    migrated_database.engine.dispose()
    wal = database_settings.database_path.with_name(
        database_settings.database_path.name + "-wal"
    )
    wal.write_bytes(b"pretend this holds committed frames")

    with pytest.raises(DatabaseError, match="is not the"):
        restore_backup(
            backup.path,
            database_settings.database_path,
            tmp_path / "backups",
            expected_revision="9999",
        )

    assert wal.is_file(), "a refused restore must not touch the live sidecars"


def test_a_successful_restore_clears_the_sidecars_of_the_file_it_replaced(
    migrated_database: Database, database_settings: AppSettings, tmp_path: Path
) -> None:
    _add_account(migrated_database, "原本的")
    backup = create_backup(database_settings.database_path, tmp_path / "backups")
    migrated_database.engine.dispose()

    restore_backup(backup.path, database_settings.database_path, tmp_path / "backups")

    for suffix in ("-wal", "-shm"):
        sidecar = database_settings.database_path.with_name(
            database_settings.database_path.name + suffix
        )
        assert not sidecar.exists()


def test_any_process_that_opens_the_database_claims_it(
    migrated_database: Database, database_settings: AppSettings, tmp_path: Path
) -> None:
    """The claim lives in `create_database`, so no entry point can forget it.

    This is the case that slipped through when only the Web application took
    the lock: the scheduled sync and every CLI command opened the database
    unprotected.
    """
    del migrated_database
    backup = create_backup(database_settings.database_path, tmp_path / "backups")
    before = read_facts(database_settings.database_path)

    other = create_database(database_settings)
    try:
        with pytest.raises(DatabaseInUseError):
            restore_backup(
                backup.path, database_settings.database_path, tmp_path / "backups"
            )
    finally:
        other.engine.dispose()

    assert read_facts(database_settings.database_path) == before


def test_disposing_an_engine_releases_the_claim(
    migrated_database: Database, database_settings: AppSettings, tmp_path: Path
) -> None:
    """Otherwise one finished command would block every later restore."""
    backup = create_backup(database_settings.database_path, tmp_path / "backups")
    # The fixture's own engine is a reader too, so it must let go as well.
    migrated_database.engine.dispose()

    create_database(database_settings).engine.dispose()

    # No exception: each claim went with the engine that took it.
    restore_backup(backup.path, database_settings.database_path, tmp_path / "backups")


def test_a_migration_cannot_write_while_a_restore_holds_the_database(
    migrated_database: Database, database_settings: AppSettings
) -> None:
    """Alembic builds its own engine, so it must claim the file too.

    Without this the schema was upgraded and only *then* reported as failed,
    leaving a database that had been half-replaced and half-migrated.
    """
    from alembic import command
    from app.infrastructure.database.migrations import (
        build_alembic_config,
        inspect_migration_status,
        upgrade_database,
    )

    migrated_database.engine.dispose()
    command.downgrade(build_alembic_config(database_settings.database_path), "0018")
    before = inspect_migration_status(database_settings).current_revision

    with (
        exclusive_database_lock(database_settings.database_path),
        pytest.raises(MigrationError),
    ):
        upgrade_database(database_settings)

    assert inspect_migration_status(database_settings).current_revision == before


def test_a_migration_runs_normally_when_nothing_holds_the_database(
    migrated_database: Database, database_settings: AppSettings
) -> None:
    from alembic import command
    from app.infrastructure.database.migrations import (
        build_alembic_config,
        inspect_migration_status,
        upgrade_database,
    )

    migrated_database.engine.dispose()
    command.downgrade(build_alembic_config(database_settings.database_path), "0018")

    upgrade_database(database_settings)

    status = inspect_migration_status(database_settings)
    assert status.current_revision == status.head_revision

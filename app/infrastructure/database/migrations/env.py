"""Alembic migration environment.

Migrations follow the protocol accepted in ADR-B027. SQLite cannot alter a
table constraint in place, and ``journal_entries`` cannot be rebuilt while
foreign keys are enforced: ``DROP TABLE`` performs an implicit row-by-row
delete that violates the table's own ``reversal_of_id`` ON DELETE RESTRICT
foreign key, ``PRAGMA defer_foreign_keys`` does not help because RESTRICT is
always immediate, and ``PRAGMA foreign_keys`` is a no-op inside a transaction.

Every online migration therefore runs on one dedicated connection that:

1. confirms no transaction is open;
2. disables foreign keys and reads the value back to confirm it is ``0``;
3. opens the migration with ``BEGIN IMMEDIATE`` so no other writer interleaves;
4. applies schema changes, data moves, and the Alembic revision update inside
   that single transaction;
5. runs a complete ``PRAGMA foreign_key_check`` before committing;
6. raises on any violation so the transaction rolls back and the revision does
   not advance;
7. restores ``PRAGMA foreign_keys=ON`` and confirms it is ``1``.

Because enforcement is off for the duration, the pre-commit check is mandatory
rather than advisory. Offline ``--sql`` mode cannot execute or verify these
pragmas, so it is refused instead of emitting a script that skips them.

``transactional_ddl`` must be set explicitly: Alembic's SQLite implementation
defaults it to ``False``, which makes ``begin_transaction()`` a no-op context
manager. The pre-commit foreign key check would then run after the schema had
already been committed, and a failure could not roll the migration back.
"""

from __future__ import annotations

import os
import sqlite3
from logging.config import fileConfig
from pathlib import Path
from typing import Any

from alembic import context
from app.infrastructure.database import (  # noqa: F401
    budget_models,
    import_models,
    models,
)
from app.infrastructure.database.base import Base
from app.infrastructure.database.locking import SharedDatabaseLock
from sqlalchemy import URL, Connection, engine_from_config, event, pool
from sqlalchemy.engine import make_url

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


class MigrationTargetError(RuntimeError):
    """Raised when a migration run has not named the database it may touch."""


class ForeignKeyViolationError(RuntimeError):
    """A migration left rows that violate a foreign key constraint."""


class OfflineMigrationUnsupportedError(RuntimeError):
    """Offline --sql mode cannot honour the ADR-B027 rebuild protocol."""


def run_migrations_offline() -> None:
    """Refuse offline mode, which cannot verify the required pragmas."""
    raise OfflineMigrationUnsupportedError(
        "Offline --sql migrations are not supported: the SQLite rebuild "
        "protocol in ADR-B027 must disable foreign keys outside a "
        "transaction and verify PRAGMA foreign_key_check before committing. "
        "Run migrations against the database instead."
    )


def _set_pragma(raw: sqlite3.Connection, pragma: str, value: str) -> None:
    """Apply one pragma on the raw connection, outside any transaction."""
    raw.execute(f"PRAGMA {pragma} = {value}")


def _require_pragma(raw: sqlite3.Connection, pragma: str, expected: int) -> None:
    """Read one pragma back and refuse to continue on an unexpected value."""
    row = raw.execute(f"PRAGMA {pragma}").fetchone()
    observed = None if row is None else row[0]
    if observed != expected:
        raise RuntimeError(
            f"PRAGMA {pragma} is {observed!r}; expected {expected!r} before migrating"
        )


def _reject_foreign_key_violations(connection: Connection) -> None:
    # ``config.attributes["verify_before_commit"]`` is Alembic's supported
    # extension point. Migration tests use it to inject a failure, or an
    # orphan row, at the last moment the transaction can still be rolled back.
    """Fail the migration while its transaction can still be rolled back."""
    violations = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
    if violations:
        tables = sorted({str(row[0]) for row in violations})
        raise ForeignKeyViolationError(
            f"migration left {len(violations)} foreign key violations in: "
            + ", ".join(tables)
        )


def resolve_database_url() -> str:
    """Return the database this run may touch, or refuse to guess one.

    Programmatic callers set ``sqlalchemy.url`` themselves. The command line
    must name its target through ``-x database=<path>`` or
    ``SCRIBE_DATABASE_PATH``: a default here would send a bare
    ``alembic upgrade head`` straight at whatever database is configured,
    which is how real data was migrated by accident once already.
    """
    configured = config.get_main_option("sqlalchemy.url", None)
    if configured:
        return configured
    explicit = context.get_x_argument(as_dictionary=True).get("database")
    path = explicit or os.environ.get("SCRIBE_DATABASE_PATH")
    if not path:
        raise MigrationTargetError(
            "no migration target was named. Use `python -m app init` for the "
            "configured database, or pass -x database=<path> or set "
            "SCRIBE_DATABASE_PATH to migrate a specific file."
        )
    resolved = Path(path).expanduser().resolve()
    return URL.create("sqlite+pysqlite", database=str(resolved)).render_as_string(
        hide_password=False
    )


def _database_file(url: str) -> Path:
    """Return the file one SQLAlchemy SQLite URL points at."""
    return Path(make_url(url).database or "")


def run_migrations_online() -> None:
    """Run migrations under the ADR-B027 rebuild protocol.

    Alembic builds its own engine, so it does not pass through
    `create_database` and would otherwise write schema changes while a restore
    held the database exclusively. The claim is taken here, the boundary every
    online migration crosses — `init`, `upgrade_database`, and the Alembic CLI
    alike (ADR-B100).
    """
    section = dict(config.get_section(config.config_ini_section, {}))
    url = resolve_database_url()
    section["sqlalchemy.url"] = url
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    @event.listens_for(connectable, "connect")
    def _prepare_migration_connection(
        dbapi_connection: sqlite3.Connection, connection_record: Any
    ) -> None:
        """Apply the rebuild pragmas before any transaction can begin."""
        del connection_record
        # pysqlite would otherwise emit its own BEGIN, which makes every
        # pragma below a no-op and prevents BEGIN IMMEDIATE.
        dbapi_connection.isolation_level = None
        _set_pragma(dbapi_connection, "synchronous", "FULL")
        _set_pragma(dbapi_connection, "busy_timeout", "5000")
        _set_pragma(dbapi_connection, "journal_mode", "WAL")
        _set_pragma(dbapi_connection, "foreign_keys", "OFF")

    @event.listens_for(connectable, "begin")
    def _begin_immediate(connection: Connection) -> None:
        """Take the write lock up front so no other writer interleaves."""
        connection.exec_driver_sql("BEGIN IMMEDIATE")

    with SharedDatabaseLock(_database_file(url)), connectable.connect() as connection:
        raw = connection.connection.dbapi_connection
        if not isinstance(raw, sqlite3.Connection):  # pragma: no cover
            raise RuntimeError("the migration protocol requires SQLite")
        if connection.in_transaction():  # pragma: no cover - defensive
            raise RuntimeError("a transaction was open before migrating")
        _require_pragma(raw, "foreign_keys", 0)
        try:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                render_as_batch=True,
                compare_type=True,
                # SQLite defaults transactional_ddl to False, which would make
                # begin_transaction() a no-op and defeat the rollback below.
                transactional_ddl=True,
                transaction_per_migration=False,
            )
            with context.begin_transaction():
                context.run_migrations()
                verify = config.attributes.get("verify_before_commit")
                if verify is not None:
                    verify(connection)
                _reject_foreign_key_violations(connection)
        finally:
            if connection.in_transaction():
                connection.rollback()
            _set_pragma(raw, "foreign_keys", "ON")
            _require_pragma(raw, "foreign_keys", 1)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

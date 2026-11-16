"""Programmatic Alembic migration operations."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import URL
from sqlalchemy.exc import SQLAlchemyError

from app.domain.errors import MigrationError
from app.infrastructure.config.settings import AppSettings
from app.infrastructure.database.engine import create_database

_MIGRATIONS_DIRECTORY = Path(__file__).resolve().with_name("migrations")


@dataclass(frozen=True)
class MigrationStatus:
    """Current and expected Alembic revisions."""

    current_revision: str | None
    head_revision: str

    @property
    def is_current(self) -> bool:
        """Return whether the database is at the single migration head."""
        return self.current_revision == self.head_revision


def build_alembic_config(database_path: Path) -> Config:
    """Create an Alembic config targeting one explicit SQLite file."""
    config = Config()
    absolute_path = database_path.expanduser().resolve()
    url = URL.create("sqlite+pysqlite", database=str(absolute_path))
    config.set_main_option("script_location", str(_MIGRATIONS_DIRECTORY))
    config.set_main_option("sqlalchemy.url", url.render_as_string(hide_password=False))
    return config


def upgrade_database(settings: AppSettings) -> MigrationStatus:
    """Upgrade the configured database to the latest schema revision."""
    database_path = settings.database_path.expanduser().resolve()
    database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        command.upgrade(build_alembic_config(database_path), "head")
        database_path.touch(mode=0o600, exist_ok=True)
        os.chmod(database_path, 0o600)
        return inspect_migration_status(settings)
    except Exception as error:
        if isinstance(error, MigrationError):
            raise
        raise MigrationError("database migration failed") from error


def inspect_migration_status(settings: AppSettings) -> MigrationStatus:
    """Inspect the database revision without applying schema changes."""
    database_path = settings.database_path.expanduser().resolve()
    if not database_path.is_file():
        raise MigrationError(f"database not found: {database_path}")
    database = create_database(settings)
    try:
        script = ScriptDirectory.from_config(
            build_alembic_config(settings.database_path)
        )
        heads = script.get_heads()
        if len(heads) != 1:
            raise MigrationError("expected exactly one Alembic migration head")
        with database.engine.connect() as connection:
            current = MigrationContext.configure(connection).get_current_revision()
        return MigrationStatus(current_revision=current, head_revision=heads[0])
    except (OSError, SQLAlchemyError) as error:
        raise MigrationError("database migration status check failed") from error
    finally:
        database.engine.dispose()

"""The Alembic command line must never pick a database on its own."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest
from app.infrastructure.config.settings import AppSettings
from app.infrastructure.database.migrations import (
    build_alembic_config,
    inspect_migration_status,
    upgrade_database,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
#: The path a bare `alembic upgrade head` used to reach from this repository.
CONFIGURED_DATABASE = REPOSITORY_ROOT / "data" / "app.sqlite3"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _configured_state() -> tuple[str, str | None] | None:
    """Return the real database's checksum and revision, if it exists here."""
    if not CONFIGURED_DATABASE.exists():
        return None
    settings = AppSettings(
        data_dir=CONFIGURED_DATABASE.parent, database_path=CONFIGURED_DATABASE
    )
    return _digest(CONFIGURED_DATABASE), inspect_migration_status(
        settings
    ).current_revision


def _alembic(
    *arguments: str, environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, **environment},
        check=False,
    )


@pytest.fixture
def configured(tmp_path: Path) -> Path:
    """Return a migrated database standing in for the user's real one."""
    settings = AppSettings(data_dir=tmp_path, database_path=tmp_path / "app.sqlite3")
    upgrade_database(settings)
    return settings.database_path


def test_a_bare_alembic_command_refuses_to_pick_a_database(configured: Path) -> None:
    """The trap this closes: `alembic upgrade head` hitting real data."""
    before = _digest(configured)

    real_before = _configured_state()

    result = _alembic("upgrade", "head", environment={"SCRIBE_DATABASE_PATH": ""})

    assert result.returncode != 0
    assert "no migration target was named" in result.stderr
    assert _digest(configured) == before
    # The database this repository is configured for is the one that was
    # migrated by accident before; it must not move a byte or a revision.
    assert _configured_state() == real_before


def test_the_environment_variable_selects_the_scratch_database(
    tmp_path: Path, configured: Path
) -> None:
    """A redirected run must migrate the scratch file and nothing else."""
    scratch = tmp_path / "scratch.sqlite3"
    before = _digest(configured)

    real_before = _configured_state()

    result = _alembic(
        "upgrade", "head", environment={"SCRIBE_DATABASE_PATH": str(scratch)}
    )

    assert result.returncode == 0, result.stderr
    assert _configured_state() == real_before
    assert scratch.exists()
    scratch_settings = AppSettings(data_dir=tmp_path, database_path=scratch)
    assert inspect_migration_status(scratch_settings).is_current
    # The configured database is byte-for-byte untouched.
    assert _digest(configured) == before


def test_an_explicit_x_argument_selects_the_scratch_database(
    tmp_path: Path, configured: Path
) -> None:
    scratch = tmp_path / "explicit.sqlite3"
    before = _digest(configured)

    result = _alembic(
        "-x",
        f"database={scratch}",
        "upgrade",
        "head",
        environment={"SCRIBE_DATABASE_PATH": ""},
    )

    assert result.returncode == 0, result.stderr
    assert inspect_migration_status(
        AppSettings(data_dir=tmp_path, database_path=scratch)
    ).is_current
    assert _digest(configured) == before


def test_programmatic_migrations_still_carry_their_own_target(
    tmp_path: Path,
) -> None:
    """The application path names the file, so it never consults the env."""
    target = tmp_path / "programmatic.sqlite3"
    settings = AppSettings(data_dir=tmp_path, database_path=target)

    config = build_alembic_config(target)

    assert config.get_main_option("sqlalchemy.url", "").endswith(str(target.resolve()))
    assert upgrade_database(settings).is_current

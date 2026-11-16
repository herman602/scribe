"""Exercise an installed wheel from an empty directory with synthetic state."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import sysconfig
from collections.abc import Mapping, Sequence
from pathlib import Path


def isolated_environment(
    workdir: Path,
    *,
    inherited: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return an environment whose Scribe paths all point inside ``workdir``."""
    source = os.environ if inherited is None else inherited
    env = {key: value for key, value in source.items() if not key.startswith("SCRIBE_")}
    data_dir = workdir / "data"
    env.update(
        {
            "SCRIBE_DATA_DIR": str(data_dir),
            "SCRIBE_DATABASE_PATH": str(data_dir / "app.sqlite3"),
            "SCRIBE_GMAIL_CREDENTIALS_PATH": str(workdir / "credentials.json"),
            "SCRIBE_GMAIL_TOKEN_PATH": str(data_dir / "token.json"),
            "SCRIBE_LOG_DIR": str(data_dir / "logs"),
            "SCRIBE_HOST": "127.0.0.1",
            "SCRIBE_PORT": "8000",
            "SCRIBE_EXTERNAL_ACCESS_ENABLED": "false",
            "SCRIBE_LAN_ACCESS_ENABLED": "false",
        }
    )
    return env


def validate_doctor_report(document: Mapping[str, object]) -> None:
    """Accept only missing OAuth failures; every local foundation check must pass."""
    raw_checks = document.get("checks")
    if not isinstance(raw_checks, list):
        raise AssertionError("doctor output has no checks list")
    checks: dict[str, str] = {}
    for item in raw_checks:
        if not isinstance(item, dict):
            raise AssertionError("doctor output contains a malformed check")
        name = item.get("name")
        status = item.get("status")
        if not isinstance(name, str) or not isinstance(status, str):
            raise AssertionError("doctor check has no stable name/status")
        checks[name] = status

    required_passes = {
        "Python version",
        "config.toml",
        "data directory writable",
        "SQLite connection",
        "migration status",
    }
    for name in required_passes:
        if checks.get(name) != "PASS":
            raise AssertionError(f"doctor did not pass {name}")
    expected_oauth = {
        "Gmail credentials": "FAIL",
        "Gmail token": "FAIL",
        "Gmail API connection": "WARN",
    }
    for name, status in expected_oauth.items():
        if checks.get(name) != status:
            raise AssertionError(f"doctor returned an unexpected {name} status")
    unexpected = {
        name: status
        for name, status in checks.items()
        if status == "FAIL" and name not in expected_oauth
    }
    if unexpected:
        raise AssertionError(
            "doctor returned unexpected local failures: "
            + ", ".join(sorted(unexpected))
        )


def _run(
    command: Sequence[str],
    *,
    workdir: Path,
    env: Mapping[str, str],
    expected_codes: set[int] | None = None,
) -> subprocess.CompletedProcess[str]:
    accepted_codes = {0} if expected_codes is None else expected_codes
    result = subprocess.run(
        command,
        cwd=workdir,
        env=dict(env),
        capture_output=True,
        text=True,
        check=False,
    )
    combined = f"{result.stdout}\n{result.stderr}"
    if "Traceback (most recent call last)" in combined:
        raise AssertionError(f"command emitted a traceback: {command[1:2]}")
    if result.returncode not in accepted_codes:
        raise AssertionError(
            f"command returned {result.returncode}: {command[1:2]}\n{combined}"
        )
    return result


def _console_script() -> Path:
    candidate = Path(sysconfig.get_path("scripts")) / "scribe"
    if not candidate.is_file():
        raise AssertionError("installed scribe console script was not found")
    return candidate


def _assert_installed_import(forbidden_root: Path | None) -> None:
    import app

    origin = Path(app.__file__).resolve()
    if forbidden_root is not None and origin.is_relative_to(forbidden_root.resolve()):
        raise AssertionError("app imported from the checkout instead of the wheel")


def _backup_and_restore(
    console: Path, *, workdir: Path, env: Mapping[str, str], database_path: Path
) -> None:
    from app.infrastructure.database.backup import read_facts

    backup_dir = workdir / "smoke-backups"
    backup_result = _run(
        [str(console), "backup", "--directory", str(backup_dir)],
        workdir=workdir,
        env=env,
    )
    backup_lines = [
        line.removeprefix("backup=")
        for line in backup_result.stdout.splitlines()
        if line.startswith("backup=")
    ]
    if len(backup_lines) != 1:
        raise AssertionError("backup command did not identify exactly one backup")
    backup_path = Path(backup_lines[0])
    before = read_facts(backup_path)

    connection = sqlite3.connect(database_path)
    try:
        connection.execute("CREATE TABLE distribution_smoke_marker(value TEXT)")
        connection.execute(
            "INSERT INTO distribution_smoke_marker(value) VALUES ('synthetic')"
        )
        connection.commit()
    finally:
        connection.close()

    _run(
        [
            str(console),
            "restore",
            str(backup_path),
            "--apply",
            "--confirm",
            "REPLACE",
        ],
        workdir=workdir,
        env=env,
    )
    after = read_facts(database_path)
    if after != before:
        raise AssertionError("restored database facts differ from the backup")
    connection = sqlite3.connect(database_path)
    try:
        marker = connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='distribution_smoke_marker'"
        ).fetchone()
    finally:
        connection.close()
    if marker != (0,):
        raise AssertionError("restore left the post-backup smoke marker in place")


def _assert_web_assets() -> None:
    from app.bootstrap import build_web_application
    from app.infrastructure.config.settings import AppSettings
    from app.presentation.web import app as web_module

    package_root = Path(web_module.__file__).resolve().parent
    templates = tuple((package_root / "templates").rglob("*.html"))
    static_files = tuple(
        path for path in (package_root / "static").rglob("*") if path.is_file()
    )
    if not templates or not static_files:
        raise AssertionError("installed templates or static assets are empty")
    if not (package_root / "templates" / "base.html").is_file():
        raise AssertionError("installed base template is missing")
    if not (package_root / "static" / "styles.css").is_file():
        raise AssertionError("installed stylesheet is missing")
    application = build_web_application(AppSettings())
    route_paths = {getattr(route, "path", None) for route in application.routes}
    if "/" not in route_paths or "/static" not in route_paths:
        raise AssertionError("installed web application did not mount its assets")


def run_smoke(workdir: Path, *, forbidden_import_root: Path | None = None) -> None:
    """Run the installed-distribution acceptance flow in one empty directory."""
    workdir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any(workdir.iterdir()):
        raise AssertionError("smoke workdir must start empty")
    os.chdir(workdir)
    env = isolated_environment(workdir)
    console = _console_script()
    _assert_installed_import(forbidden_import_root)

    _run([str(console), "--help"], workdir=workdir, env=env)
    _run([str(console), "init"], workdir=workdir, env=env)
    database_path = workdir / "data" / "app.sqlite3"
    if not database_path.is_file() or not (workdir / "config.toml").is_file():
        raise AssertionError("scribe init did not create its local foundation")

    from app.infrastructure.config.settings import AppSettings
    from app.infrastructure.database.migrations import inspect_migration_status

    status = inspect_migration_status(AppSettings())
    if not status.is_current:
        raise AssertionError("installed database is not at the wheel migration head")

    doctor = _run(
        [str(console), "doctor", "--json"],
        workdir=workdir,
        env=env,
        expected_codes={1},
    )
    validate_doctor_report(json.loads(doctor.stdout))
    _backup_and_restore(
        console,
        workdir=workdir,
        env=env,
        database_path=database_path,
    )
    _assert_web_assets()


def _arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--forbid-import-root", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for a freshly installed wheel rehearsal."""
    arguments = _arguments(argv)
    try:
        run_smoke(
            arguments.workdir,
            forbidden_import_root=arguments.forbid_import_root,
        )
    except (AssertionError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"installed distribution smoke failed: {error}", file=sys.stderr)
        return 1
    print("installed distribution smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

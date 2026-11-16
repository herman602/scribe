"""Local foundation diagnostics."""

from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.domain.errors import (
    GmailAuthenticationError,
    GmailConnectionError,
    GmailRateLimitError,
    MigrationError,
)
from app.domain.gmail import TokenState, gmail_oauth_scopes
from app.infrastructure.config.settings import AppSettings
from app.infrastructure.database.engine import create_database
from app.infrastructure.database.migrations import inspect_migration_status
from app.infrastructure.gmail.gateway import GoogleGmailGateway
from app.infrastructure.gmail.oauth import GoogleOAuthAuthenticator
from app.infrastructure.gmail.token_store import GoogleTokenStore


class CheckStatus(StrEnum):
    """Doctor check severity."""

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(frozen=True)
class DoctorCheck:
    """One user-readable diagnostic result."""

    name: str
    status: CheckStatus
    detail: str


def _path_check(name: str, path: Path, *, required: bool) -> DoctorCheck:
    if path.exists():
        return DoctorCheck(name, CheckStatus.PASS, str(path))
    status = CheckStatus.FAIL if required else CheckStatus.WARN
    return DoctorCheck(name, status, f"not found: {path}")


def _writable_directory_check(path: Path) -> DoctorCheck:
    if not path.is_dir():
        return DoctorCheck(
            "data directory writable",
            CheckStatus.FAIL,
            f"directory not found: {path}",
        )
    try:
        with tempfile.NamedTemporaryFile(dir=path):
            pass
    except OSError as error:
        return DoctorCheck(
            "data directory writable",
            CheckStatus.FAIL,
            f"{path}: {error}",
        )
    return DoctorCheck("data directory writable", CheckStatus.PASS, str(path))


def run_foundation_checks(settings: AppSettings) -> tuple[DoctorCheck, ...]:
    """Run local runtime, database, and pre-authentication checks."""
    python_ok = sys.version_info >= (3, 12)
    checks = [
        DoctorCheck(
            "Python version",
            CheckStatus.PASS if python_ok else CheckStatus.FAIL,
            ".".join(str(part) for part in sys.version_info[:3]),
        ),
        _path_check("config.toml", Path("config.toml"), required=True),
        _writable_directory_check(settings.data_dir),
        *_database_checks(settings),
        *_gmail_checks(settings),
    ]
    return tuple(checks)


def _database_checks(settings: AppSettings) -> tuple[DoctorCheck, DoctorCheck]:
    if not settings.database_path.is_file():
        detail = f"not found: {settings.database_path}; run `python -m app init`"
        return (
            DoctorCheck("SQLite connection", CheckStatus.FAIL, detail),
            DoctorCheck("migration status", CheckStatus.FAIL, detail),
        )

    database = create_database(settings)
    try:
        with database.engine.connect() as connection:
            integrity = connection.scalar(text("PRAGMA integrity_check"))
        connection_check = DoctorCheck(
            "SQLite connection",
            CheckStatus.PASS if integrity == "ok" else CheckStatus.FAIL,
            f"{settings.database_path}; integrity={integrity}",
        )
    except SQLAlchemyError as error:
        connection_check = DoctorCheck(
            "SQLite connection",
            CheckStatus.FAIL,
            f"{settings.database_path}: {error.__class__.__name__}",
        )
    finally:
        database.engine.dispose()

    try:
        status = inspect_migration_status(settings)
        migration_check = DoctorCheck(
            "migration status",
            CheckStatus.PASS if status.is_current else CheckStatus.FAIL,
            f"current={status.current_revision or 'none'}, head={status.head_revision}",
        )
    except MigrationError as error:
        migration_check = DoctorCheck(
            "migration status",
            CheckStatus.FAIL,
            str(error),
        )
    return connection_check, migration_check


def _gmail_checks(
    settings: AppSettings,
) -> tuple[DoctorCheck, DoctorCheck, DoctorCheck]:
    required_scopes = gmail_oauth_scopes(
        labeling_enabled=settings.gmail_labeling_enabled
    )
    token_store = GoogleTokenStore(settings.gmail_token_path, required_scopes)
    token_description = (
        "gmail.modify token" if settings.gmail_labeling_enabled else "read-only token"
    )
    authenticator = GoogleOAuthAuthenticator(
        settings.gmail_credentials_path,
        token_store,
    )
    try:
        authenticator.validate_client_secrets()
        credentials_check = _private_file_check(
            "Gmail credentials",
            settings.gmail_credentials_path,
            valid_detail="valid Desktop OAuth client",
        )
    except GmailAuthenticationError as error:
        credentials_check = DoctorCheck(
            "Gmail credentials",
            CheckStatus.FAIL,
            str(error),
        )

    initial_status = authenticator.token_status()
    if initial_status.state is TokenState.MISSING:
        return (
            credentials_check,
            DoctorCheck(
                "Gmail token",
                CheckStatus.FAIL,
                "not found; run `uv run python -m app auth`",
            ),
            DoctorCheck(
                "Gmail API connection",
                CheckStatus.WARN,
                "skipped until OAuth authorization",
            ),
        )
    if initial_status.state is TokenState.INVALID:
        return (
            credentials_check,
            DoctorCheck(
                "Gmail token",
                CheckStatus.FAIL,
                initial_status.detail or "token is invalid",
            ),
            DoctorCheck(
                "Gmail API connection",
                CheckStatus.WARN,
                "skipped because token is invalid",
            ),
        )

    try:
        credentials = authenticator.credentials_for_api()
        profile = GoogleGmailGateway(
            credentials,
            request_timeout_seconds=settings.gmail_request_timeout_seconds,
            max_attempts=settings.gmail_retry_max_attempts,
            backoff_base_seconds=settings.gmail_backoff_base_seconds,
            backoff_max_seconds=settings.gmail_backoff_max_seconds,
        ).get_profile()
        token_check = _private_file_check(
            "Gmail token",
            settings.gmail_token_path,
            valid_detail=f"valid {token_description}",
        )
        connection_check = DoctorCheck(
            "Gmail API connection",
            CheckStatus.PASS,
            f"authenticated as {profile.email_address}",
        )
    except GmailAuthenticationError as error:
        token_check = DoctorCheck("Gmail token", CheckStatus.FAIL, str(error))
        connection_check = DoctorCheck(
            "Gmail API connection",
            CheckStatus.FAIL,
            "authentication failed",
        )
    except GmailRateLimitError:
        token_check = DoctorCheck(
            "Gmail token",
            CheckStatus.PASS,
            f"valid {token_description}",
        )
        connection_check = DoctorCheck(
            "Gmail API connection",
            CheckStatus.WARN,
            "rate limited; retry later",
        )
    except GmailConnectionError:
        token_check = DoctorCheck(
            "Gmail token",
            CheckStatus.PASS,
            "valid read-only token",
        )
        connection_check = DoctorCheck(
            "Gmail API connection",
            CheckStatus.FAIL,
            "connection failed",
        )
    return credentials_check, token_check, connection_check


def _private_file_check(
    name: str,
    path: Path,
    *,
    valid_detail: str,
) -> DoctorCheck:
    if os.name == "posix" and path.stat().st_mode & 0o077:
        return DoctorCheck(
            name,
            CheckStatus.WARN,
            f"{valid_detail}; permissions should be 0600",
        )
    return DoctorCheck(name, CheckStatus.PASS, valid_detail)

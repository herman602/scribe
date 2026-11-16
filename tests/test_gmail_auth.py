"""Gmail OAuth, token storage, gateway, and use-case tests."""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httplib2  # type: ignore[import-untyped]
import pytest
from app.application.services.gmail_auth_health import GmailAuthHealthService
from app.application.services.gmail_authentication import AuthenticateGmail
from app.bootstrap import build_incremental_sync_service
from app.domain.errors import (
    GmailAuthenticationError,
    GmailConnectionError,
    GmailRateLimitError,
)
from app.domain.gmail import (
    GMAIL_MODIFY_SCOPES,
    GMAIL_READONLY_SCOPE,
    GMAIL_READONLY_SCOPES,
    GmailAccountProfile,
    TokenState,
    TokenStatus,
)
from app.infrastructure.config.doctor import CheckStatus, run_foundation_checks
from app.infrastructure.config.initializer import initialize_local_environment
from app.infrastructure.config.settings import AppSettings
from app.infrastructure.database.engine import Database
from app.infrastructure.database.migrations import upgrade_database
from app.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork
from app.infrastructure.gmail.gateway import GoogleGmailGateway
from app.infrastructure.gmail.oauth import GoogleOAuthAuthenticator, OAuthFlow
from app.infrastructure.gmail.token_store import GoogleTokenStore
from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError  # type: ignore[import-untyped]
from sqlalchemy import text

_TOKEN_URI = "https://oauth2.googleapis.test/token"


def _credentials(
    *,
    expiry: datetime | None = None,
    scopes: tuple[str, ...] = GMAIL_READONLY_SCOPES,
    token: str = "fixture-access-token",
    refresh_token: str = "fixture-refresh-token",
) -> Credentials:
    return Credentials(  # type: ignore[no-untyped-call]
        token=token,
        refresh_token=refresh_token,
        token_uri=_TOKEN_URI,
        client_id="fixture-client-id",
        client_secret="fixture-client-secret",
        scopes=scopes,
        expiry=expiry or datetime.now(UTC) + timedelta(hours=1),
    )


def _write_client_secrets(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "fixture-client-id",
                    "client_secret": "fixture-client-secret",
                    "auth_uri": "https://accounts.google.test/auth",
                    "token_uri": _TOKEN_URI,
                    "redirect_uris": ["http://localhost"],
                }
            }
        ),
        encoding="utf-8",
    )
    if os.name == "posix":
        path.chmod(0o600)


class _FakeRequest:
    def __init__(self, response: Any) -> None:
        self._response = response

    def execute(self) -> Any:
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class _FakeUsers:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.user_id: str | None = None

    def getProfile(self, *, userId: str) -> _FakeRequest:
        self.user_id = userId
        return _FakeRequest(self.response)


class _FakeService:
    def __init__(self, response: Any) -> None:
        self.users_resource = _FakeUsers(response)

    def users(self) -> _FakeUsers:
        return self.users_resource


class _FakeFlow:
    def __init__(self, credentials: Credentials) -> None:
        self.credentials = credentials
        self.arguments: dict[str, Any] | None = None

    def run_local_server(
        self,
        *,
        host: str,
        port: int,
        open_browser: bool,
        authorization_prompt_message: str,
        success_message: str,
        timeout_seconds: int,
        prompt: str,
    ) -> Credentials:
        self.arguments = {
            "host": host,
            "port": port,
            "open_browser": open_browser,
            "authorization_prompt_message": authorization_prompt_message,
            "success_message": success_message,
            "timeout_seconds": timeout_seconds,
            "prompt": prompt,
        }
        return self.credentials


class _FakeGateway:
    def __init__(self, profile: GmailAccountProfile) -> None:
        self._profile = profile

    def get_profile(self) -> GmailAccountProfile:
        return self._profile


def _profile() -> GmailAccountProfile:
    return GmailAccountProfile(
        email_address="user@example.com",
        messages_total=12,
        threads_total=7,
        history_id="12345",
    )


def test_token_store_round_trip_is_atomic_private_and_read_only(
    tmp_path: Path,
) -> None:
    token_path = tmp_path / "private/token.json"
    store = GoogleTokenStore(token_path)

    store.save(_credentials())
    loaded = store.load()

    assert loaded is not None
    assert loaded.token == "fixture-access-token"
    assert tuple(loaded.scopes or ()) == GMAIL_READONLY_SCOPES
    assert store.status().state is TokenState.VALID
    assert list(token_path.parent.glob("*.tmp")) == []
    if os.name == "posix":
        assert token_path.stat().st_mode & 0o777 == 0o600


def test_token_store_rejects_broader_or_malformed_tokens(tmp_path: Path) -> None:
    token_path = tmp_path / "token.json"
    store = GoogleTokenStore(token_path)
    broader = (
        GMAIL_READONLY_SCOPE,
        "https://www.googleapis.com/auth/gmail.modify",
    )

    with pytest.raises(GmailAuthenticationError, match=r"gmail\.readonly"):
        store.save(_credentials(scopes=broader))

    token_path.write_text("{not-json", encoding="utf-8")
    assert store.status().state is TokenState.INVALID
    with pytest.raises(GmailAuthenticationError, match="unreadable"):
        store.load()


def test_token_store_accepts_only_configured_modify_scope(tmp_path: Path) -> None:
    token_path = tmp_path / "token.json"
    store = GoogleTokenStore(token_path, GMAIL_MODIFY_SCOPES)

    store.save(_credentials(scopes=GMAIL_MODIFY_SCOPES))

    loaded = store.load()
    assert loaded is not None
    assert tuple(loaded.scopes or ()) == GMAIL_MODIFY_SCOPES
    with pytest.raises(GmailAuthenticationError, match=r"gmail\.modify"):
        store.save(_credentials())


def test_expired_token_is_reported_as_refreshable(tmp_path: Path) -> None:
    store = GoogleTokenStore(tmp_path / "token.json")
    store.save(_credentials(expiry=datetime.now(UTC) - timedelta(minutes=5)))

    status = store.status()

    assert status.state is TokenState.EXPIRED_REFRESHABLE
    assert status.expires_at is not None
    assert status.expires_at.tzinfo is UTC


def test_oauth_interactive_flow_uses_loopback_and_persists_token(
    tmp_path: Path,
) -> None:
    credentials_path = tmp_path / "credentials.json"
    token_store = GoogleTokenStore(tmp_path / "data/token.json")
    _write_client_secrets(credentials_path)
    flow = _FakeFlow(_credentials())
    captured_scopes: tuple[str, ...] | None = None

    def flow_factory(path: Path, scopes: Sequence[str]) -> OAuthFlow:
        nonlocal captured_scopes
        assert path == credentials_path
        captured_scopes = tuple(scopes)
        return flow

    authenticator = GoogleOAuthAuthenticator(
        credentials_path,
        token_store,
        flow_factory=flow_factory,
        gateway_factory=lambda credentials: _FakeGateway(_profile()),
    )

    profile = authenticator.authorize()

    assert profile == _profile()
    assert captured_scopes == GMAIL_READONLY_SCOPES
    assert flow.arguments is not None
    assert flow.arguments["host"] == "127.0.0.1"
    assert flow.arguments["port"] == 0
    assert flow.arguments["timeout_seconds"] == 300
    assert flow.arguments["prompt"] == "consent"
    assert token_store.status().state is TokenState.VALID


def test_oauth_reuses_valid_token_without_interactive_flow(tmp_path: Path) -> None:
    credentials_path = tmp_path / "credentials.json"
    token_store = GoogleTokenStore(tmp_path / "token.json")
    _write_client_secrets(credentials_path)
    token_store.save(_credentials())

    def unexpected_flow(path: Path, scopes: Sequence[str]) -> OAuthFlow:
        del path, scopes
        raise AssertionError("interactive OAuth should not run")

    authenticator = GoogleOAuthAuthenticator(
        credentials_path,
        token_store,
        flow_factory=unexpected_flow,
        gateway_factory=lambda credentials: _FakeGateway(_profile()),
    )

    assert authenticator.authorize() == _profile()


def test_forced_reauthorization_replaces_even_a_valid_saved_token(
    tmp_path: Path,
) -> None:
    credentials_path = tmp_path / "credentials.json"
    token_store = GoogleTokenStore(tmp_path / "token.json")
    _write_client_secrets(credentials_path)
    token_store.save(_credentials())
    replacement = _credentials(
        token="fresh-consent-access-token",
        refresh_token="fresh-consent-refresh-token",
    )
    flow = _FakeFlow(replacement)
    authenticator = GoogleOAuthAuthenticator(
        credentials_path,
        token_store,
        flow_factory=lambda path, scopes: flow,
        gateway_factory=lambda credentials: _FakeGateway(_profile()),
    )

    assert authenticator.authorize(force_reauthorization=True) == _profile()
    assert flow.arguments is not None
    assert flow.arguments["prompt"] == "consent"
    saved = token_store.load()
    assert saved is not None
    assert saved.token == "fresh-consent-access-token"
    assert saved.refresh_token == "fresh-consent-refresh-token"


def test_expired_credentials_are_refreshed_and_saved(tmp_path: Path) -> None:
    credentials_path = tmp_path / "credentials.json"
    token_store = GoogleTokenStore(tmp_path / "token.json")
    _write_client_secrets(credentials_path)
    token_store.save(_credentials(expiry=datetime.now(UTC) - timedelta(minutes=5)))
    refresh_calls = 0

    def refresh(credentials: Credentials) -> None:
        nonlocal refresh_calls
        refresh_calls += 1
        credentials.token = "refreshed-fixture-token"
        credentials.expiry = datetime.now(UTC) + timedelta(hours=1)

    authenticator = GoogleOAuthAuthenticator(
        credentials_path,
        token_store,
        credential_refresher=refresh,
        gateway_factory=lambda credentials: _FakeGateway(_profile()),
    )

    credentials = authenticator.credentials_for_api()

    assert refresh_calls == 1
    assert credentials.token == "refreshed-fixture-token"
    saved = token_store.load()
    assert saved is not None
    assert saved.token == "refreshed-fixture-token"


def test_explicit_authorize_recovers_from_a_revoked_refresh_token(
    tmp_path: Path,
) -> None:
    credentials_path = tmp_path / "credentials.json"
    token_store = GoogleTokenStore(tmp_path / "token.json")
    _write_client_secrets(credentials_path)
    token_store.save(_credentials(expiry=datetime.now(UTC) - timedelta(minutes=5)))
    replacement = _credentials()
    replacement.token = "replacement-access-token"
    flow = _FakeFlow(replacement)
    refresh_calls = 0

    def fail_refresh(credentials: Credentials) -> None:
        nonlocal refresh_calls
        refresh_calls += 1
        del credentials
        raise OSError("fixture revoked refresh token")

    authenticator = GoogleOAuthAuthenticator(
        credentials_path,
        token_store,
        flow_factory=lambda path, scopes: flow,
        credential_refresher=fail_refresh,
        gateway_factory=lambda credentials: _FakeGateway(_profile()),
    )

    assert authenticator.authorize() == _profile()
    assert refresh_calls == 1
    assert flow.arguments is not None
    saved = token_store.load()
    assert saved is not None
    assert saved.token == "replacement-access-token"


def test_failed_interactive_recovery_does_not_overwrite_the_old_token(
    tmp_path: Path,
) -> None:
    credentials_path = tmp_path / "credentials.json"
    token_path = tmp_path / "token.json"
    token_store = GoogleTokenStore(token_path)
    _write_client_secrets(credentials_path)
    token_store.save(_credentials(expiry=datetime.now(UTC) - timedelta(minutes=5)))
    original = token_path.read_bytes()

    def fail_refresh(credentials: Credentials) -> None:
        del credentials
        raise OSError("fixture revoked refresh token")

    def fail_flow(path: Path, scopes: Sequence[str]) -> OAuthFlow:
        del path, scopes
        raise OSError("fixture browser flow failure")

    authenticator = GoogleOAuthAuthenticator(
        credentials_path,
        token_store,
        flow_factory=fail_flow,
        credential_refresher=fail_refresh,
    )

    with pytest.raises(GmailAuthenticationError, match="OAuth flow failed"):
        authenticator.authorize()
    assert token_path.read_bytes() == original


def test_refresh_failure_becomes_authentication_error(tmp_path: Path) -> None:
    credentials_path = tmp_path / "credentials.json"
    token_store = GoogleTokenStore(tmp_path / "token.json")
    _write_client_secrets(credentials_path)
    token_store.save(_credentials(expiry=datetime.now(UTC) - timedelta(minutes=5)))

    def fail_refresh(credentials: Credentials) -> None:
        del credentials
        raise OSError("fixture network failure")

    authenticator = GoogleOAuthAuthenticator(
        credentials_path,
        token_store,
        credential_refresher=fail_refresh,
    )

    with pytest.raises(GmailAuthenticationError, match="refresh failed"):
        authenticator.credentials_for_api()


def test_oauth_rejects_web_client_configuration(tmp_path: Path) -> None:
    credentials_path = tmp_path / "credentials.json"
    credentials_path.write_text('{"web": {}}', encoding="utf-8")
    authenticator = GoogleOAuthAuthenticator(
        credentials_path,
        GoogleTokenStore(tmp_path / "token.json"),
    )

    with pytest.raises(GmailAuthenticationError, match="Desktop app"):
        authenticator.validate_client_secrets()


def test_gateway_returns_typed_profile_and_uses_me() -> None:
    service = _FakeService(
        {
            "emailAddress": "User@Example.COM",
            "messagesTotal": 20,
            "threadsTotal": 10,
            "historyId": "999",
        }
    )
    gateway = GoogleGmailGateway(
        _credentials(),
        max_attempts=1,
        service_factory=lambda credentials: service,
    )

    profile = gateway.get_profile()

    assert profile.email_address == "user@example.com"
    assert profile.messages_total == 20
    assert profile.history_id == "999"
    assert service.users_resource.user_id == "me"


@pytest.mark.parametrize(
    ("status", "content", "error_type"),
    [
        (401, b"{}", GmailAuthenticationError),
        (429, b"{}", GmailRateLimitError),
        (
            403,
            b'{"error":{"errors":[{"reason":"userRateLimitExceeded"}]}}',
            GmailRateLimitError,
        ),
        (500, b"{}", GmailConnectionError),
    ],
)
def test_gateway_translates_http_errors(
    status: int,
    content: bytes,
    error_type: type[Exception],
) -> None:
    response = httplib2.Response({"status": str(status)})
    error = HttpError(response, content, uri="https://gmail.googleapis.test")
    service = _FakeService(error)
    gateway = GoogleGmailGateway(
        _credentials(),
        max_attempts=1,
        service_factory=lambda credentials: service,
    )

    with pytest.raises(error_type):
        gateway.get_profile()


def test_gateway_rejects_incomplete_profile() -> None:
    gateway = GoogleGmailGateway(
        _credentials(),
        service_factory=lambda credentials: _FakeService({"emailAddress": "x"}),
    )

    with pytest.raises(GmailConnectionError, match="incomplete"):
        gateway.get_profile()


def test_authentication_use_case_persists_account_identity(
    migrated_database: Database,
) -> None:
    class FakeAuth:
        def authorize(
            self, *, force_reauthorization: bool = False
        ) -> GmailAccountProfile:
            assert force_reauthorization is True
            return _profile()

        def token_status(self) -> TokenStatus:
            return TokenStatus(TokenState.VALID)

    health = GmailAuthHealthService(
        lambda: SqlAlchemyUnitOfWork(migrated_database.session_factory),
        now=lambda: datetime(2026, 8, 22, 12, tzinfo=UTC),
    )
    service = AuthenticateGmail(
        FakeAuth(),
        lambda: SqlAlchemyUnitOfWork(migrated_database.session_factory),
        health,
    )

    first = service.execute()
    second = service.execute()

    assert first.account.id == second.account.id
    assert first.account.profile_email == "user@example.com"
    assert first.profile.history_id == "12345"
    authorization = health.read()
    assert authorization is not None
    assert authorization.authorized_at == datetime(2026, 8, 22, 12, tzinfo=UTC)


def test_sync_preflight_persists_auth_failure_before_a_run_exists(
    migrated_database: Database,
    database_settings: AppSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject(self: GoogleOAuthAuthenticator) -> Credentials:
        del self
        raise GmailAuthenticationError("refresh failed")

    monkeypatch.setattr(GoogleOAuthAuthenticator, "credentials_for_api", reject)

    with pytest.raises(GmailAuthenticationError, match="refresh failed"):
        build_incremental_sync_service(database_settings)

    health = GmailAuthHealthService(
        lambda: SqlAlchemyUnitOfWork(migrated_database.session_factory)
    ).read()
    assert health is not None
    assert health.state.value == "reauthorization_required"
    with migrated_database.session_factory() as session:
        assert session.execute(text("SELECT COUNT(*) FROM sync_runs")).scalar_one() == 0


def test_doctor_validates_token_and_mocked_gmail_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = AppSettings()
    initialize_local_environment(settings)
    upgrade_database(settings)
    _write_client_secrets(settings.gmail_credentials_path)
    GoogleTokenStore(settings.gmail_token_path).save(_credentials())
    monkeypatch.setattr(
        GoogleGmailGateway,
        "get_profile",
        lambda self: _profile(),
    )

    checks = run_foundation_checks(settings)
    statuses = {check.name: check.status for check in checks}

    assert statuses["Gmail credentials"] is CheckStatus.PASS
    assert statuses["Gmail token"] is CheckStatus.PASS
    assert statuses["Gmail API connection"] is CheckStatus.PASS

"""Desktop OAuth flow and credential refresh orchestration."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlparse

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore[import-untyped]

from app.domain.errors import GmailAuthenticationError
from app.domain.gmail import GmailAccountProfile, GmailProfileGateway, TokenStatus
from app.infrastructure.gmail.gateway import GoogleGmailGateway
from app.infrastructure.gmail.token_store import GoogleTokenStore


class OAuthFlow(Protocol):
    """Testable subset of InstalledAppFlow."""

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
    ) -> Credentials: ...


type OAuthFlowFactory = Callable[[Path, Sequence[str]], OAuthFlow]
type GatewayFactory = Callable[[Credentials], GmailProfileGateway]
type CredentialRefresher = Callable[[Credentials], None]


def _create_flow(credentials_path: Path, scopes: Sequence[str]) -> OAuthFlow:
    return cast(
        OAuthFlow,
        InstalledAppFlow.from_client_secrets_file(
            str(credentials_path),
            scopes=scopes,
            autogenerate_code_verifier=True,
        ),
    )


def _refresh_credentials(credentials: Credentials) -> None:
    credentials.refresh(Request())  # type: ignore[no-untyped-call]


class GoogleOAuthAuthenticator:
    """Authorize a desktop user and expose credentials to local adapters."""

    def __init__(
        self,
        credentials_path: Path,
        token_store: GoogleTokenStore,
        *,
        flow_factory: OAuthFlowFactory = _create_flow,
        gateway_factory: GatewayFactory = GoogleGmailGateway,
        credential_refresher: CredentialRefresher = _refresh_credentials,
    ) -> None:
        self._credentials_path = credentials_path
        self._token_store = token_store
        self._flow_factory = flow_factory
        self._gateway_factory = gateway_factory
        self._credential_refresher = credential_refresher

    def authorize(self, *, force_reauthorization: bool = False) -> GmailAccountProfile:
        """Reuse, refresh, or interactively obtain configured Gmail credentials."""
        self.validate_client_secrets()
        if force_reauthorization:
            # The explicit `auth` command is also the renewal path for OAuth
            # clients in Google's Testing publishing status. Reusing a valid
            # access token here would reset Scribe's local authorization date
            # without renewing the underlying refresh token.
            renewed_credentials = self._run_interactive_flow()
            self._token_store.save(renewed_credentials)
            return self._gateway_factory(renewed_credentials).get_profile()

        credentials: Credentials | None
        try:
            credentials = self._token_store.load()
        except GmailAuthenticationError:
            credentials = None

        if credentials is None:
            credentials = self._run_interactive_flow()
            self._token_store.save(credentials)
        elif not credentials.valid:
            if not (credentials.expired and credentials.refresh_token):
                credentials = self._run_interactive_flow()
            else:
                try:
                    self._refresh(credentials)
                except GmailAuthenticationError:
                    # `authorize` is the explicit interactive recovery command.
                    # A revoked/expired refresh token must not trap that command
                    # in the same non-interactive failure used by API clients.
                    # Keep the saved token untouched until the new flow succeeds.
                    credentials = self._run_interactive_flow()
            self._token_store.save(credentials)

        return self._gateway_factory(credentials).get_profile()

    def credentials_for_api(self) -> Credentials:
        """Return valid saved credentials without starting interactive consent."""
        credentials = self._token_store.load()
        if credentials is None:
            raise GmailAuthenticationError(
                "OAuth token not found; run `uv run python -m app auth`"
            )
        if credentials.valid:
            return credentials
        if credentials.expired and credentials.refresh_token:
            self._refresh(credentials)
            self._token_store.save(credentials)
            return credentials
        raise GmailAuthenticationError(
            "OAuth token cannot be refreshed; run `uv run python -m app auth`"
        )

    def token_status(self) -> TokenStatus:
        """Return a non-sensitive local token status."""
        return self._token_store.status()

    def validate_client_secrets(self) -> None:
        """Validate that credentials.json is a desktop installed-app config."""
        try:
            raw = json.loads(self._credentials_path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise GmailAuthenticationError(
                f"Gmail OAuth credentials not found: {self._credentials_path}"
            ) from error
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise GmailAuthenticationError(
                "Gmail OAuth credentials file is unreadable"
            ) from error
        if not isinstance(raw, dict) or not isinstance(raw.get("installed"), dict):
            raise GmailAuthenticationError(
                "credentials.json must use the Desktop app client type"
            )
        installed = raw["installed"]
        required = {"client_id", "client_secret", "auth_uri", "token_uri"}
        if not required <= installed.keys() or not all(
            isinstance(installed[key], str) and installed[key].strip()
            for key in required
        ):
            raise GmailAuthenticationError(
                "Desktop OAuth credentials are missing required fields"
            )
        redirects = installed.get("redirect_uris")
        if not isinstance(redirects, list) or not any(
            _is_loopback_redirect(value) for value in redirects
        ):
            raise GmailAuthenticationError(
                "Desktop OAuth credentials need a loopback redirect URI"
            )

    def _run_interactive_flow(self) -> Credentials:
        try:
            flow = self._flow_factory(
                self._credentials_path,
                self._token_store.required_scopes,
            )
            access_description = (
                "Gmail read and Label access"
                if any(
                    scope.endswith("/gmail.modify")
                    for scope in self._token_store.required_scopes
                )
                else "read-only Gmail access"
            )
            return flow.run_local_server(
                host="127.0.0.1",
                port=0,
                open_browser=True,
                authorization_prompt_message=(
                    f"Open this URL to authorize {access_description}:\n{{url}}"
                ),
                success_message=(
                    "Scribe authorization completed. You may close this window."
                ),
                timeout_seconds=300,
                # Force a fresh consent grant so Google returns a renewed
                # refresh token even when the user has authorized Scribe before.
                prompt="consent",
            )
        except Exception as error:
            raise GmailAuthenticationError("Gmail OAuth flow failed") from error

    def _refresh(self, credentials: Credentials) -> None:
        try:
            self._credential_refresher(credentials)
        except Exception as error:
            raise GmailAuthenticationError(
                "Gmail OAuth token refresh failed; run `uv run python -m app auth`"
            ) from error


def _is_loopback_redirect(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    return parsed.scheme == "http" and parsed.hostname in {
        "localhost",
        "127.0.0.1",
        "::1",
    }

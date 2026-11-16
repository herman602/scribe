"""Atomic, least-scope OAuth token file storage."""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from google.oauth2.credentials import Credentials

from app.domain.errors import GmailAuthenticationError
from app.domain.gmail import GMAIL_READONLY_SCOPES, TokenState, TokenStatus


class GoogleTokenStore:
    """Load and atomically save authorized-user credentials."""

    def __init__(
        self,
        token_path: Path,
        required_scopes: Sequence[str] = GMAIL_READONLY_SCOPES,
    ) -> None:
        if not required_scopes or any(not scope.strip() for scope in required_scopes):
            raise ValueError("OAuth required scopes cannot be empty")
        self._token_path = token_path
        self._required_scopes = tuple(required_scopes)

    @property
    def token_path(self) -> Path:
        """Return the configured token location."""
        return self._token_path

    @property
    def required_scopes(self) -> tuple[str, ...]:
        """Return the exact scopes accepted by this store."""
        return self._required_scopes

    def load(self) -> Credentials | None:
        """Load a token while rejecting malformed or broader-scope files."""
        if not self._token_path.is_file():
            return None
        try:
            raw = json.loads(self._token_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise GmailAuthenticationError("OAuth token file is unreadable") from error
        if not isinstance(raw, dict):
            raise GmailAuthenticationError("OAuth token file has an invalid format")
        self._require_exact_scope(raw.get("scopes"))
        try:
            credentials = Credentials.from_authorized_user_info(  # type: ignore[no-untyped-call]
                raw,
                self._required_scopes,
            )
            return cast(Credentials, credentials)
        except (TypeError, ValueError) as error:
            raise GmailAuthenticationError("OAuth token data is invalid") from error

    def save(self, credentials: Credentials) -> None:
        """Atomically persist credentials with POSIX mode 0600."""
        self._require_exact_scope(credentials.scopes)
        try:
            serialized = cast(
                str,
                credentials.to_json(),  # type: ignore[no-untyped-call]
            )
            parsed = json.loads(serialized)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise GmailAuthenticationError(
                "OAuth token could not be serialized"
            ) from error
        if not isinstance(parsed, dict):
            raise GmailAuthenticationError("OAuth token serialization was invalid")
        parsed["scopes"] = list(self._required_scopes)
        payload = json.dumps(
            parsed,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

        parent = self._token_path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = parent / f".{self._token_path.name}.{uuid4().hex}.tmp"
        descriptor: int | None = None
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = None
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._token_path)
            os.chmod(self._token_path, 0o600)
        except OSError as error:
            if descriptor is not None:
                os.close(descriptor)
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
            raise GmailAuthenticationError("OAuth token could not be saved") from error

    def status(self) -> TokenStatus:
        """Return a non-sensitive local token diagnostic."""
        try:
            credentials = self.load()
        except GmailAuthenticationError:
            return TokenStatus(TokenState.INVALID, detail="token file is invalid")
        if credentials is None:
            return TokenStatus(TokenState.MISSING, detail="token file not found")
        expiry = _aware_utc(credentials.expiry)
        if credentials.valid:
            return TokenStatus(TokenState.VALID, expires_at=expiry)
        if credentials.expired and credentials.refresh_token:
            return TokenStatus(TokenState.EXPIRED_REFRESHABLE, expires_at=expiry)
        return TokenStatus(
            TokenState.INVALID,
            expires_at=expiry,
            detail="token cannot be refreshed",
        )

    def _require_exact_scope(self, scopes: Any) -> None:
        if isinstance(scopes, str):
            normalized = frozenset(scopes.split())
        elif isinstance(scopes, (list, tuple, set, frozenset)) and all(
            isinstance(scope, str) for scope in scopes
        ):
            normalized = frozenset(scopes)
        else:
            raise GmailAuthenticationError("OAuth token scopes are missing")
        if normalized != frozenset(self._required_scopes):
            expected = ", ".join(
                scope.rsplit("/", maxsplit=1)[-1] for scope in self._required_scopes
            )
            raise GmailAuthenticationError(
                f"OAuth token must contain exactly: {expected}"
            )


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)

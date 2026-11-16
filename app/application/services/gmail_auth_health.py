"""Persist and interpret non-secret Gmail OAuth health."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from app.domain.gmail import (
    GmailAuthorizationHealth,
    GmailAuthorizationNotice,
    GmailAuthorizationNoticeKind,
    GmailAuthorizationState,
)
from app.domain.ports.repositories import UnitOfWork
from app.domain.types import JsonValue

type UnitOfWorkFactory = Callable[[], UnitOfWork]

GMAIL_AUTH_HEALTH_SETTING_KEY = "gmail.oauth_health"
GMAIL_AUTH_HEALTH_SCHEMA_VERSION = 1
_TESTING_LIFETIME = timedelta(days=7)
_TESTING_WARNING_LEAD = timedelta(days=1)


class GmailAuthHealthService:
    """Record OAuth outcomes and derive a dashboard-safe notice."""

    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._now = now

    def mark_authorized(self) -> GmailAuthorizationHealth:
        """Record a successful explicit authorization and clear failures."""
        observed_at = _as_utc(self._now())
        health = GmailAuthorizationHealth(
            state=GmailAuthorizationState.HEALTHY,
            authorized_at=observed_at,
            failure_at=None,
            failure_reason_code=None,
        )
        self._write(health)
        return health

    def mark_reauthorization_required(
        self, *, reason_code: str = "credentials_unavailable"
    ) -> GmailAuthorizationHealth:
        """Persist the first failure even when no sync run could be created."""
        current = self.read()
        health = GmailAuthorizationHealth(
            state=GmailAuthorizationState.REAUTHORIZATION_REQUIRED,
            authorized_at=None if current is None else current.authorized_at,
            failure_at=(
                current.failure_at
                if current is not None and current.failure_at is not None
                else _as_utc(self._now())
            ),
            failure_reason_code=_safe_reason_code(reason_code),
        )
        self._write(health)
        return health

    def read(self) -> GmailAuthorizationHealth | None:
        """Read a valid health record; malformed old settings are ignored."""
        with self._unit_of_work_factory() as unit:
            setting = unit.app_settings.get(GMAIL_AUTH_HEALTH_SETTING_KEY)
        if (
            setting is None
            or setting.schema_version != GMAIL_AUTH_HEALTH_SCHEMA_VERSION
        ):
            return None
        return _decode_health(setting.value)

    def notice(self, *, testing_mode: bool) -> GmailAuthorizationNotice | None:
        """Return the one highest-priority notice for a dashboard request."""
        health = self.read()
        if health is None:
            return None
        if health.state is GmailAuthorizationState.REAUTHORIZATION_REQUIRED:
            return GmailAuthorizationNotice(
                kind=GmailAuthorizationNoticeKind.REAUTHORIZATION_REQUIRED,
                authorized_at=health.authorized_at,
                due_at=None,
                failure_at=health.failure_at,
            )
        if not testing_mode or health.authorized_at is None:
            return None
        due_at = health.authorized_at + _TESTING_LIFETIME
        now = _as_utc(self._now())
        if now >= due_at:
            kind = GmailAuthorizationNoticeKind.TESTING_EXPIRED
        elif now >= due_at - _TESTING_WARNING_LEAD:
            kind = GmailAuthorizationNoticeKind.TESTING_EXPIRES_TOMORROW
        else:
            return None
        return GmailAuthorizationNotice(
            kind=kind,
            authorized_at=health.authorized_at,
            due_at=due_at,
            failure_at=None,
        )

    def _write(self, health: GmailAuthorizationHealth) -> None:
        value: dict[str, JsonValue] = {
            "state": health.state.value,
            "authorized_at": _encode_datetime(health.authorized_at),
            "failure_at": _encode_datetime(health.failure_at),
            "failure_reason_code": health.failure_reason_code,
        }
        with self._unit_of_work_factory() as unit:
            unit.app_settings.set(
                GMAIL_AUTH_HEALTH_SETTING_KEY,
                value,
                schema_version=GMAIL_AUTH_HEALTH_SCHEMA_VERSION,
            )
            unit.commit()


def _decode_health(value: JsonValue) -> GmailAuthorizationHealth | None:
    if not isinstance(value, dict):
        return None
    raw_state = value.get("state")
    if not isinstance(raw_state, str):
        return None
    try:
        state = GmailAuthorizationState(raw_state)
    except ValueError:
        return None
    authorized_at = _decode_datetime(value.get("authorized_at"))
    failure_at = _decode_datetime(value.get("failure_at"))
    reason = value.get("failure_reason_code")
    if reason is not None and not isinstance(reason, str):
        return None
    if state is GmailAuthorizationState.HEALTHY and failure_at is not None:
        return None
    if state is GmailAuthorizationState.REAUTHORIZATION_REQUIRED and failure_at is None:
        return None
    return GmailAuthorizationHealth(
        state=state,
        authorized_at=authorized_at,
        failure_at=failure_at,
        failure_reason_code=reason,
    )


def _decode_datetime(value: JsonValue | None) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _encode_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("authorization health timestamps must include a timezone")
    return value.astimezone(UTC)


def _safe_reason_code(value: str) -> str:
    normalized = "".join(
        character if character.isalnum() or character == "_" else "_"
        for character in value.strip().lower()
    )
    return (normalized or "credentials_unavailable")[:100]

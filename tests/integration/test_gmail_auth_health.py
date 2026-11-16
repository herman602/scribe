"""Durable Gmail authorization health and reminder behavior."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.application.services.gmail_auth_health import GmailAuthHealthService
from app.domain.gmail import (
    GmailAuthorizationNoticeKind,
    GmailAuthorizationState,
)
from app.infrastructure.database.engine import Database
from app.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork


def _service(database: Database, now: datetime) -> GmailAuthHealthService:
    return GmailAuthHealthService(
        lambda: SqlAlchemyUnitOfWork(database.session_factory),
        now=lambda: now,
    )


def test_authorization_date_drives_the_testing_mode_warning(
    migrated_database: Database,
) -> None:
    authorized_at = datetime(2026, 8, 22, 12, tzinfo=UTC)
    _service(migrated_database, authorized_at).mark_authorized()

    before_warning = _service(
        migrated_database, authorized_at + timedelta(days=5, hours=23)
    )
    warning = _service(migrated_database, authorized_at + timedelta(days=6)).notice(
        testing_mode=True
    )
    expired = _service(migrated_database, authorized_at + timedelta(days=7)).notice(
        testing_mode=True
    )

    assert before_warning.notice(testing_mode=True) is None
    assert warning is not None
    assert warning.kind is GmailAuthorizationNoticeKind.TESTING_EXPIRES_TOMORROW
    assert warning.due_at == authorized_at + timedelta(days=7)
    assert expired is not None
    assert expired.kind is GmailAuthorizationNoticeKind.TESTING_EXPIRED


def test_production_mode_never_invents_a_seven_day_expiry(
    migrated_database: Database,
) -> None:
    authorized_at = datetime(2026, 8, 1, tzinfo=UTC)
    service = _service(migrated_database, authorized_at)
    service.mark_authorized()

    much_later = _service(migrated_database, authorized_at + timedelta(days=90))

    assert much_later.notice(testing_mode=False) is None


def test_refresh_failure_is_durable_and_reauthorization_clears_it(
    migrated_database: Database,
) -> None:
    authorized_at = datetime(2026, 8, 15, tzinfo=UTC)
    failed_at = datetime(2026, 8, 22, tzinfo=UTC)
    _service(migrated_database, authorized_at).mark_authorized()

    failed = _service(migrated_database, failed_at)
    first = failed.mark_reauthorization_required(reason_code="refresh_failed")
    repeated = _service(
        migrated_database, failed_at + timedelta(hours=3)
    ).mark_reauthorization_required(reason_code="refresh_failed")
    notice = failed.notice(testing_mode=False)

    assert first.state is GmailAuthorizationState.REAUTHORIZATION_REQUIRED
    assert first.authorized_at == authorized_at
    assert repeated.failure_at == failed_at
    assert notice is not None
    assert notice.kind is GmailAuthorizationNoticeKind.REAUTHORIZATION_REQUIRED

    recovered_at = failed_at + timedelta(hours=4)
    recovered = _service(migrated_database, recovered_at)
    recovered.mark_authorized()

    assert recovered.notice(testing_mode=True) is None
    health = recovered.read()
    assert health is not None
    assert health.state is GmailAuthorizationState.HEALTHY

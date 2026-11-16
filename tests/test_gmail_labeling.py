"""Processed Gmail Label outbox and application-service tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.application.services.gmail_incremental_sync import _collapse_changes
from app.application.services.gmail_labeling import ManageProcessedGmailLabels
from app.domain.enums import (
    ClassificationStatus,
    GmailLabelOperationStatus,
    ProcessingStatus,
    SyncItemOperation,
)
from app.domain.errors import GmailLabelError
from app.domain.gmail import GmailAccountProfile, GmailHistoryChange
from app.domain.models import EmailSnapshot
from app.infrastructure.database.engine import Database
from app.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork


class _LabelGateway:
    def __init__(self, error: GmailLabelError | None = None) -> None:
        self.error = error
        self.batches: list[tuple[tuple[str, ...], str]] = []

    def get_profile(self) -> GmailAccountProfile:
        return GmailAccountProfile("user@example.com", 1, 1, "100")

    def ensure_label(self, label_name: str) -> str:
        assert label_name == "Scribe/已處理"
        return "Label_42"

    def add_label_to_messages(
        self, message_ids: tuple[str, ...], label_id: str
    ) -> None:
        if self.error is not None:
            raise self.error
        self.batches.append((message_ids, label_id))


def _processed_email(database: Database) -> tuple[int, int]:
    with SqlAlchemyUnitOfWork(database.session_factory) as unit:
        account = unit.accounts.get_or_create("user@example.com")
        outcome = unit.emails.upsert_from_gmail(
            EmailSnapshot(
                gmail_account_id=account.id,
                gmail_message_id="message-1",
                gmail_thread_id="thread-1",
                gmail_history_id="100",
                sender="notice@bank.test",
                recipients=("user@example.com",),
                subject="消費通知",
                received_at=datetime(2026, 8, 1, tzinfo=UTC),
                gmail_internal_date_ms=1_754_003_200_000,
                labels=("INBOX",),
            )
        )
        unit.emails.apply_processing_result(
            outcome.email.id,
            status=ProcessingStatus.PROCESSED,
            classification=ClassificationStatus.CONFIRMED,
            confidence=Decimal("0.95"),
            reason="fixture",
            parser_name="fixture",
            parser_version="1",
            purge_body=False,
        )
        unit.commit()
        return account.id, outcome.email.id


def test_backfill_labels_processed_messages_idempotently(
    migrated_database: Database,
) -> None:
    account_id, _ = _processed_email(migrated_database)
    gateway = _LabelGateway()
    service = ManageProcessedGmailLabels(
        gateway,
        lambda: SqlAlchemyUnitOfWork(migrated_database.session_factory),
        label_name="Scribe/已處理",
    )

    first = service.backfill()
    second = service.backfill()

    assert first.queued_count == 1
    assert first.succeeded_count == 1
    assert first.pending_count == 0
    assert second.queued_count == 0
    assert gateway.batches == [(("message-1",), "Label_42")]
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        setting = unit.app_settings.get(f"gmail_processed_label.{account_id}")
    assert setting is not None
    assert setting.value == {"name": "Scribe/已處理", "id": "Label_42"}


def test_retryable_delivery_failure_stays_pending(
    migrated_database: Database,
) -> None:
    account_id, email_id = _processed_email(migrated_database)
    gateway = _LabelGateway(GmailLabelError("temporary", retryable=True))
    service = ManageProcessedGmailLabels(
        gateway,
        lambda: SqlAlchemyUnitOfWork(migrated_database.session_factory),
        label_name="Scribe/已處理",
    )

    result = service.backfill()

    assert result.error_message == "temporary"
    assert result.pending_count == 1
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        operation = unit.gmail_label_operations.enqueue(
            email_id=email_id, label_name="Scribe/已處理"
        )
    assert operation.status is GmailLabelOperationStatus.PENDING
    assert operation.attempt_count == 1
    assert operation.error_code == "GmailLabelError"
    assert operation.gmail_account_id == account_id


def test_incremental_history_ignores_only_the_application_label() -> None:
    changes = (
        GmailHistoryChange("m1", "t1", labels_added=("Label_42",)),
        GmailHistoryChange("m2", "t2", labels_added=("INBOX",)),
        GmailHistoryChange("m3", "t3", message_added=True, labels_added=("Label_42",)),
    )

    collapsed = _collapse_changes(changes, frozenset({"Label_42"}))

    assert collapsed == (
        ("m2", SyncItemOperation.UPDATE),
        ("m3", SyncItemOperation.FETCH),
    )

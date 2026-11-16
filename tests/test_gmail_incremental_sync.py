"""Application-level incremental history and reconciliation tests."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from app.application.services.email_processing import PrepareEmailProcessing
from app.application.services.gmail_full_sync import FullGmailSync
from app.application.services.gmail_incremental_sync import IncrementalGmailSync
from app.application.services.gmail_message_persistence import PersistGmailMessage
from app.domain.enums import (
    BodyRetentionPolicy,
    EmailSourceStatus,
    FullSyncStatus,
    RunStatus,
    SenderMatchType,
    SyncMode,
)
from app.domain.errors import (
    GmailHistoryExpiredError,
    GmailMessageFetchError,
    GmailMessageNotFoundError,
    InitialSyncRequiredError,
)
from app.domain.gmail import (
    GmailAccountProfile,
    GmailAttachmentBytes,
    GmailHeader,
    GmailHistoryChange,
    GmailHistoryPage,
    GmailMessageMetadata,
    GmailMessagePage,
    GmailMessagePart,
    GmailMessageRef,
    GmailRawMessage,
    GmailThread,
    GmailThreadMessage,
)
from app.domain.models import EmailSnapshot, SenderRuleDraft
from app.domain.parsing import ExtractionLimits, SecretValue
from app.infrastructure.attachments.pdf import LocalPdfExtractor
from app.infrastructure.database.engine import Database
from app.infrastructure.database.models import EmailModel, SyncRunModel
from app.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork
from app.infrastructure.gmail.mime import GmailMimeDecoder
from app.infrastructure.parsers import (
    ClassificationPipeline,
    DefaultParserRegistry,
    GenericTransactionParser,
)
from sqlalchemy import select


@dataclass
class _FakeGateway:
    profile_history_id: str
    history_pages: dict[str | None, GmailHistoryPage | Exception]
    metadata: dict[str, GmailMessageMetadata | Exception] = field(default_factory=dict)
    messages: dict[str, GmailRawMessage | Exception] = field(default_factory=dict)
    full_pages: dict[str | None, GmailMessagePage | Exception] = field(
        default_factory=dict
    )
    threads: dict[str, GmailThread] = field(default_factory=dict)
    history_calls: list[str | None] = field(default_factory=list)
    metadata_calls: list[str] = field(default_factory=list)
    message_calls: list[str] = field(default_factory=list)
    list_calls: list[str | None] = field(default_factory=list)

    def get_profile(self) -> GmailAccountProfile:
        return GmailAccountProfile(
            email_address="user@example.com",
            messages_total=10,
            threads_total=5,
            history_id=self.profile_history_id,
        )

    def list_history(
        self,
        *,
        start_history_id: str,
        page_token: str | None,
        max_results: int,
    ) -> GmailHistoryPage:
        assert start_history_id == "100"
        assert max_results == 2
        self.history_calls.append(page_token)
        value = self.history_pages[page_token]
        if isinstance(value, Exception):
            raise value
        return value

    def get_message_metadata(self, message_id: str) -> GmailMessageMetadata:
        self.metadata_calls.append(message_id)
        value = self.metadata[message_id]
        if isinstance(value, Exception):
            raise value
        return value

    def get_message(self, message_id: str) -> GmailRawMessage:
        self.message_calls.append(message_id)
        value = self.messages[message_id]
        if isinstance(value, Exception):
            raise value
        return value

    def list_messages(
        self,
        *,
        query: str | None,
        page_token: str | None,
        max_results: int,
        include_spam_trash: bool,
    ) -> GmailMessagePage:
        assert query == "{from:notice@bank.example}"
        assert max_results == 2
        assert not include_spam_trash
        self.list_calls.append(page_token)
        value = self.full_pages[page_token]
        if isinstance(value, Exception):
            raise value
        return value

    def get_thread(self, thread_id: str) -> GmailThread:
        return self.threads[thread_id]

    def get_attachment(
        self, message_id: str, attachment_id: str
    ) -> GmailAttachmentBytes:
        del message_id, attachment_id
        raise AssertionError("fixture messages contain no attachments")


class _NoSecrets:
    def get(self, reference: str) -> SecretValue | None:
        del reference
        return None

    def set(self, reference: str, value: SecretValue) -> None:
        del reference, value

    def delete(self, reference: str) -> None:
        del reference


def _message(
    message_id: str,
    *,
    sender: str = "notice@bank.example",
) -> GmailRawMessage:
    body = (
        "Purchase notification\n"
        "Transaction time: 2026/07/20 10:00\n"
        "Merchant: FIXTURE STORE\n"
        "Amount: USD 12.99"
    )
    headers = (
        GmailHeader("From", f"Fixture Bank <{sender}>"),
        GmailHeader("To", "user@example.com"),
        GmailHeader("Subject", f"Fixture {message_id}"),
        GmailHeader("Date", "Mon, 27 Jul 2026 10:30:00 +0800"),
        GmailHeader("Content-Type", "text/plain; charset=utf-8"),
    )
    return GmailRawMessage(
        message_id=message_id,
        thread_id=f"thread-{message_id}",
        history_id=f"history-{message_id}",
        internal_date_ms=1_721_000_000_000,
        snippet="fixture",
        label_ids=("INBOX",),
        payload=GmailMessagePart(
            part_id="",
            mime_type="text/plain",
            filename="",
            headers=headers,
            body_size=len(body.encode()),
            body_data=body.encode(),
            attachment_id=None,
        ),
    )


def _metadata(
    message_id: str,
    *,
    sender: str = "notice@bank.example",
) -> GmailMessageMetadata:
    return GmailMessageMetadata(
        message_id=message_id,
        thread_id=f"thread-{message_id}",
        history_id=f"history-{message_id}",
        sender_header=sender,
        label_ids=("INBOX",),
    )


def _history(
    *changes: GmailHistoryChange,
    latest: str = "110",
    next_page_token: str | None = None,
) -> GmailHistoryPage:
    return GmailHistoryPage(
        changes=changes,
        next_page_token=next_page_token,
        latest_history_id=latest,
    )


def _configure_baseline(database: Database, *, with_cursor: bool = True) -> int:
    with SqlAlchemyUnitOfWork(database.session_factory) as unit:
        account = unit.accounts.get_or_create("user@example.com")
        rule = unit.sender_rules.upsert(
            SenderRuleDraft(
                gmail_account_id=account.id,
                institution_key="fixture_bank",
                institution_name="測試銀行",
                match_type=SenderMatchType.EXACT_ADDRESS,
                match_value="notice@bank.example",
            )
        )
        state = unit.sync_states.get_or_create(account.id)
        unit.sync_states.save(
            replace(
                state,
                committed_history_id="100" if with_cursor else None,
                baseline_history_id="100" if with_cursor else None,
                initial_query="{from:notice@bank.example}",
                full_sync_status=(
                    FullSyncStatus.SUCCEEDED if with_cursor else FullSyncStatus.IDLE
                ),
            ),
            expected_version=state.version,
        )
        unit.commit()
        return rule.id


def _seed_email(database: Database, rule_id: int, message_id: str) -> None:
    with SqlAlchemyUnitOfWork(database.session_factory) as unit:
        account = unit.accounts.get_by_profile_email("user@example.com")
        assert account is not None
        unit.emails.upsert_from_gmail(
            EmailSnapshot(
                gmail_account_id=account.id,
                gmail_message_id=message_id,
                gmail_thread_id=f"thread-{message_id}",
                gmail_history_id="99",
                sender="notice@bank.example",
                recipients=("user@example.com",),
                subject="Fixture existing",
                gmail_internal_date_ms=1_720_000_000_000,
                snippet="fixture",
                received_at=datetime(2026, 7, 19, tzinfo=UTC),
                body_text="fixture",
                body_summary="fixture",
                has_html_body=False,
                labels=("INBOX",),
                matched_sender_rule_id=rule_id,
                body_policy_applied=BodyRetentionPolicy.FULL_TEXT,
            )
        )
        unit.commit()


def _service(database: Database, gateway: _FakeGateway) -> IncrementalGmailSync:
    def unit_factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(database.session_factory)

    processing = PrepareEmailProcessing(
        pipeline=ClassificationPipeline(
            DefaultParserRegistry((GenericTransactionParser(),)),
            confirmed_threshold=Decimal("0.90"),
        ),
        attachment_extractor=LocalPdfExtractor(),
        secret_store=_NoSecrets(),
        extraction_limits=ExtractionLimits(
            max_bytes=1024 * 1024,
            max_pages=10,
            max_text_chars=100_000,
            max_password_attempts=3,
            max_seconds=5,
        ),
    )
    decoder = GmailMimeDecoder()
    persistence = PersistGmailMessage(
        processing,
        unit_factory,
        body_retention_policy=BodyRetentionPolicy.FULL_TEXT,
        body_summary_chars=1000,
    )
    reconciliation = FullGmailSync(
        gateway,
        decoder,
        processing,
        unit_factory,
        max_results=2,
        work_batch_size=2,
        body_retention_policy=BodyRetentionPolicy.FULL_TEXT,
    )
    return IncrementalGmailSync(
        gateway,
        decoder,
        persistence,
        reconciliation,
        unit_factory,
        max_results=2,
        work_batch_size=2,
        clock=lambda: datetime(2026, 7, 28, tzinfo=UTC),
    )


def test_incremental_applies_add_label_gate_and_soft_delete(
    migrated_database: Database,
) -> None:
    rule_id = _configure_baseline(migrated_database)
    _seed_email(migrated_database, rule_id, "old")
    gateway = _FakeGateway(
        profile_history_id="110",
        history_pages={
            None: _history(
                GmailHistoryChange(
                    "new",
                    "thread-new",
                    message_added=True,
                ),
                GmailHistoryChange(
                    "new",
                    "thread-new",
                    labels_added=("STARRED",),
                ),
                GmailHistoryChange(
                    "unknown",
                    "thread-unknown",
                    labels_added=("INBOX",),
                ),
                GmailHistoryChange(
                    "old",
                    "thread-old",
                    message_deleted=True,
                ),
            )
        },
        metadata={
            "new": _metadata("new"),
            "unknown": _metadata("unknown", sender="other@example.test"),
        },
        messages={"new": _message("new")},
    )

    result = _service(migrated_database, gateway).execute()

    assert result.status is RunStatus.SUCCEEDED
    assert result.mode is SyncMode.INCREMENTAL
    assert result.new_count == 1
    assert result.deleted_count == 1
    assert result.end_history_id == "110"
    assert gateway.metadata_calls == ["new", "unknown"]
    assert gateway.message_calls == ["new"]
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        account = unit.accounts.get_by_profile_email("user@example.com")
        assert account is not None
        state = unit.sync_states.get_or_create(account.id)
        old = unit.emails.get_by_gmail_id(account.id, "old")
        assert state.committed_history_id == "110"
        assert old is not None
        assert old.source_status is EmailSourceStatus.DELETED


def test_item_failure_retains_committed_history_cursor(
    migrated_database: Database,
) -> None:
    _configure_baseline(migrated_database)
    gateway = _FakeGateway(
        profile_history_id="111",
        history_pages={
            None: _history(
                GmailHistoryChange("bad", "thread-bad", message_added=True),
                latest="111",
            )
        },
        metadata={"bad": _metadata("bad")},
        messages={"bad": GmailMessageFetchError("fixture disappeared")},
    )

    result = _service(migrated_database, gateway).execute()

    assert result.status is RunStatus.PARTIAL
    assert result.failed_count == 1
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        account = unit.accounts.get_by_profile_email("user@example.com")
        assert account is not None
        state = unit.sync_states.get_or_create(account.id)
        assert state.committed_history_id == "100"
        assert state.reconciliation_required


def test_expired_history_runs_complete_reconciliation_without_database_reset(
    migrated_database: Database,
) -> None:
    rule_id = _configure_baseline(migrated_database)
    _seed_email(migrated_database, rule_id, "stale")
    gateway = _FakeGateway(
        profile_history_id="200",
        history_pages={
            None: GmailHistoryExpiredError("fixture history expired"),
        },
        messages={"kept": _message("kept")},
        full_pages={
            None: GmailMessagePage(
                messages=(GmailMessageRef("kept", "thread-kept"),),
                next_page_token=None,
                result_size_estimate=1,
            )
        },
        threads={
            "thread-kept": GmailThread(
                thread_id="thread-kept",
                messages=(
                    GmailThreadMessage(
                        message_id="kept",
                        thread_id="thread-kept",
                        sender_header="notice@bank.example",
                    ),
                ),
            )
        },
    )

    result = _service(migrated_database, gateway).execute()

    assert result.reconciliation_fallback
    assert result.mode is SyncMode.RECONCILIATION
    assert result.deleted_count == 1
    assert result.end_history_id == "200"
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        account = unit.accounts.get_by_profile_email("user@example.com")
        assert account is not None
        stale = unit.emails.get_by_gmail_id(account.id, "stale")
        kept = unit.emails.get_by_gmail_id(account.id, "kept")
        state = unit.sync_states.get_or_create(account.id)
        assert stale is not None
        assert stale.source_status is EmailSourceStatus.DELETED
        assert kept is not None
        assert state.committed_history_id == "200"
        assert not state.reconciliation_required
    with migrated_database.session_factory() as session:
        modes = list(
            session.scalars(select(SyncRunModel.mode).order_by(SyncRunModel.created_at))
        )
    assert modes == [SyncMode.INCREMENTAL.value, SyncMode.RECONCILIATION.value]


def test_incremental_requires_initial_full_sync(migrated_database: Database) -> None:
    _configure_baseline(migrated_database, with_cursor=False)
    gateway = _FakeGateway(profile_history_id="1", history_pages={})

    with pytest.raises(InitialSyncRequiredError, match="--full"):
        _service(migrated_database, gateway).execute()


def test_history_delete_for_unknown_message_is_skipped(
    migrated_database: Database,
) -> None:
    _configure_baseline(migrated_database)
    gateway = _FakeGateway(
        profile_history_id="112",
        history_pages={
            None: _history(
                GmailHistoryChange(
                    "unknown-deleted",
                    "thread-unknown",
                    message_deleted=True,
                ),
                latest="112",
            )
        },
    )

    result = _service(migrated_database, gateway).execute()

    assert result.status is RunStatus.SUCCEEDED
    assert result.deleted_count == 0
    assert result.end_history_id == "112"
    with migrated_database.session_factory() as session:
        assert session.scalar(select(EmailModel)) is None


def test_history_pagination_resumes_and_restarts_rejected_saved_token(
    migrated_database: Database,
) -> None:
    _configure_baseline(migrated_database)
    interrupted = _FakeGateway(
        profile_history_id="120",
        history_pages={
            None: _history(
                GmailHistoryChange("m1", "thread-m1", message_added=True),
                latest="119",
                next_page_token="page-2",
            ),
            "page-2": GmailMessageFetchError(
                "fixture outage",
                retryable=True,
            ),
        },
    )
    with pytest.raises(GmailMessageFetchError):
        _service(migrated_database, interrupted).execute()

    resumed = _FakeGateway(
        profile_history_id="120",
        history_pages={
            "page-2": GmailMessageFetchError("saved token rejected"),
            None: _history(
                GmailHistoryChange("m1", "thread-m1", message_added=True),
                latest="119",
                next_page_token="page-2",
            ),
        },
        metadata={"m1": _metadata("m1"), "m2": _metadata("m2")},
        messages={"m1": _message("m1"), "m2": _message("m2")},
    )
    page_two = _history(
        GmailHistoryChange("m2", "thread-m2", message_added=True),
        latest="120",
    )

    class _SecondPageGateway(_FakeGateway):
        def list_history(
            self,
            *,
            start_history_id: str,
            page_token: str | None,
            max_results: int,
        ) -> GmailHistoryPage:
            if page_token == "page-2" and self.history_calls.count("page-2") >= 1:
                self.history_calls.append(page_token)
                return page_two
            return super().list_history(
                start_history_id=start_history_id,
                page_token=page_token,
                max_results=max_results,
            )

    resumed_gateway = _SecondPageGateway(**resumed.__dict__)
    result = _service(migrated_database, resumed_gateway).execute()

    assert resumed_gateway.history_calls == ["page-2", None, "page-2"]
    assert result.pages_processed == 2
    assert result.discovered_count == 2
    assert result.end_history_id == "120"


def test_message_disappearing_after_history_event_becomes_soft_delete(
    migrated_database: Database,
) -> None:
    rule_id = _configure_baseline(migrated_database)
    _seed_email(migrated_database, rule_id, "race")
    gateway = _FakeGateway(
        profile_history_id="113",
        history_pages={
            None: _history(
                GmailHistoryChange(
                    "race",
                    "thread-race",
                    labels_removed=("INBOX",),
                ),
                latest="113",
            )
        },
        metadata={"race": GmailMessageNotFoundError("fixture disappeared")},
    )

    result = _service(migrated_database, gateway).execute()

    assert result.deleted_count == 1
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        account = unit.accounts.get_by_profile_email("user@example.com")
        assert account is not None
        email = unit.emails.get_by_gmail_id(account.id, "race")
        assert email is not None
        assert email.source_status is EmailSourceStatus.DELETED

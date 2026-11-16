"""Application-level Gmail full-sync tests with a mocked gateway."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from app.application.services.email_processing import PrepareEmailProcessing
from app.application.services.financial_event_intake import (
    FinancialEventIntake,
    intake_idempotency_key,
)
from app.application.services.gmail_full_sync import (
    FullGmailSync,
    FullSyncRequest,
)
from app.domain.budget_enums import SourceDiscrepancy
from app.domain.enums import (
    BodyRetentionPolicy,
    FullSyncStatus,
    RunStatus,
    SenderMatchType,
    TransactionType,
)
from app.domain.errors import (
    GmailConnectionError,
    GmailMessageFetchError,
    SyncAlreadyRunningError,
)
from app.domain.gmail import (
    GmailAccountProfile,
    GmailAttachmentBytes,
    GmailHeader,
    GmailMessagePage,
    GmailMessagePart,
    GmailMessageRef,
    GmailRawMessage,
    GmailThread,
    GmailThreadMessage,
)
from app.domain.models import SenderRuleDraft, TransactionReviewDraft
from app.domain.parsing import ExtractionLimits, SecretValue
from app.infrastructure.attachments.pdf import LocalPdfExtractor
from app.infrastructure.database.engine import Database
from app.infrastructure.database.models import (
    ClassificationResultModel,
    EmailModel,
    ParserRunModel,
    SyncRunModel,
    TransactionModel,
)
from app.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork
from app.infrastructure.gmail.mime import GmailMimeDecoder
from app.infrastructure.parsers import (
    ClassificationPipeline,
    DefaultParserRegistry,
    FubonTransactionParser,
    GenericTransactionParser,
)
from sqlalchemy import func, select


@dataclass
class _FakeGateway:
    profile_history_id: str
    pages: dict[str | None, GmailMessagePage | Exception]
    threads: dict[str, GmailThread]
    messages: dict[str, GmailRawMessage | Exception]
    expected_query: str | None = "{from:notice@bank.example} (newer_than:2y)"
    attachments: dict[str, bytes] = field(default_factory=dict)
    list_calls: list[str | None] = field(default_factory=list)
    thread_calls: list[str] = field(default_factory=list)
    message_calls: list[str] = field(default_factory=list)
    attachment_calls: list[tuple[str, str]] = field(default_factory=list)

    def get_profile(self) -> GmailAccountProfile:
        return GmailAccountProfile(
            email_address="user@example.com",
            messages_total=100,
            threads_total=50,
            history_id=self.profile_history_id,
        )

    def list_messages(
        self,
        *,
        query: str | None,
        page_token: str | None,
        max_results: int,
        include_spam_trash: bool,
    ) -> GmailMessagePage:
        assert query == self.expected_query
        assert max_results == 2
        assert not include_spam_trash
        self.list_calls.append(page_token)
        value = self.pages[page_token]
        if isinstance(value, Exception):
            raise value
        return value

    def get_thread(self, thread_id: str) -> GmailThread:
        self.thread_calls.append(thread_id)
        return self.threads[thread_id]

    def get_message(self, message_id: str) -> GmailRawMessage:
        self.message_calls.append(message_id)
        value = self.messages[message_id]
        if isinstance(value, Exception):
            raise value
        return value

    def get_attachment(
        self, message_id: str, attachment_id: str
    ) -> GmailAttachmentBytes:
        self.attachment_calls.append((message_id, attachment_id))
        return GmailAttachmentBytes(self.attachments[attachment_id])


def _part(
    *,
    part_id: str,
    mime_type: str,
    headers: tuple[GmailHeader, ...] = (),
    content: bytes | None = None,
    attachment_id: str | None = None,
    filename: str = "",
    parts: tuple[GmailMessagePart, ...] = (),
) -> GmailMessagePart:
    return GmailMessagePart(
        part_id=part_id,
        mime_type=mime_type,
        filename=filename,
        headers=headers,
        body_size=len(content or b""),
        body_data=content,
        attachment_id=attachment_id,
        parts=parts,
    )


def _message(
    message_id: str,
    thread_id: str,
    body: str,
    *,
    attachment_id: str | None = None,
) -> GmailRawMessage:
    headers = (
        GmailHeader("From", "Bank Notice <notice@bank.example>"),
        GmailHeader("To", "user@example.com"),
        GmailHeader("Subject", f"Fixture {message_id}"),
        GmailHeader("Date", "Mon, 27 Jul 2026 10:30:00 +0800"),
    )
    if attachment_id:
        payload = _part(
            part_id="",
            mime_type="multipart/mixed",
            headers=headers,
            parts=(
                _part(
                    part_id="0",
                    mime_type="text/plain",
                    content=body.encode(),
                    headers=(GmailHeader("Content-Type", "text/plain; charset=utf-8"),),
                ),
                _part(
                    part_id="1",
                    mime_type="application/pdf",
                    filename="statement.pdf",
                    attachment_id=attachment_id,
                ),
            ),
        )
    else:
        payload = _part(
            part_id="",
            mime_type="text/plain",
            headers=(
                *headers,
                GmailHeader("Content-Type", "text/plain; charset=utf-8"),
            ),
            content=body.encode(),
        )
    return GmailRawMessage(
        message_id=message_id,
        thread_id=thread_id,
        history_id=f"h-{message_id}",
        internal_date_ms=1_721_000_000_000,
        snippet=f"snippet {message_id}",
        label_ids=("INBOX",),
        payload=payload,
    )


def _thread(thread_id: str, *message_ids: str) -> GmailThread:
    return GmailThread(
        thread_id=thread_id,
        messages=tuple(
            GmailThreadMessage(
                message_id=message_id,
                thread_id=thread_id,
                sender_header=(
                    "notice@bank.example"
                    if message_id != "other"
                    else "marketing@example.test"
                ),
            )
            for message_id in message_ids
        ),
    )


def _configure_rule(database: Database) -> None:
    with SqlAlchemyUnitOfWork(database.session_factory) as unit:
        account = unit.accounts.get_or_create("user@example.com")
        unit.sender_rules.upsert(
            SenderRuleDraft(
                gmail_account_id=account.id,
                institution_key="fixture_bank",
                institution_name="測試銀行",
                match_type=SenderMatchType.EXACT_ADDRESS,
                match_value="notice@bank.example",
            )
        )
        unit.commit()


def _service(
    database: Database,
    gateway: _FakeGateway,
    *,
    intake: FinancialEventIntake | None = None,
) -> FullGmailSync:
    class _NoSecrets:
        def get(self, reference: str) -> SecretValue | None:
            del reference
            return None

        def set(self, reference: str, value: SecretValue) -> None:
            del reference, value

        def delete(self, reference: str) -> None:
            del reference

    processing = PrepareEmailProcessing(
        pipeline=ClassificationPipeline(
            DefaultParserRegistry(
                (FubonTransactionParser(), GenericTransactionParser())
            ),
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
    return FullGmailSync(
        gateway,
        GmailMimeDecoder(),
        processing,
        lambda: SqlAlchemyUnitOfWork(database.session_factory),
        max_results=2,
        work_batch_size=2,
        body_retention_policy=BodyRetentionPolicy.FULL_TEXT,
        intake=intake,
    )


def test_full_sync_resumes_page_checkpoint_and_expands_threads(
    migrated_database: Database,
) -> None:
    _configure_rule(migrated_database)
    first_page = GmailMessagePage(
        messages=(GmailMessageRef("m1", "t1"),),
        next_page_token="page-2",
        result_size_estimate=2,
    )
    interrupted = _FakeGateway(
        profile_history_id="100",
        pages={
            None: first_page,
            "page-2": GmailConnectionError("fixture outage"),
        },
        threads={"t1": _thread("t1", "m1", "m2", "other")},
        messages={},
    )

    with pytest.raises(GmailConnectionError):
        _service(migrated_database, interrupted).execute(
            FullSyncRequest(custom_query="newer_than:2y")
        )

    resumed = _FakeGateway(
        profile_history_id="200",
        pages={
            "page-2": GmailMessagePage(
                messages=(GmailMessageRef("m3", "t2"),),
                next_page_token=None,
                result_size_estimate=2,
            )
        },
        threads={"t2": _thread("t2", "m3")},
        messages={
            "m1": _message("m1", "t1", "first"),
            "m2": _message("m2", "t1", "second", attachment_id="pdf-1"),
            "m3": _message("m3", "t2", "third"),
        },
        attachments={"pdf-1": b"%PDF-1.7 encrypted fixture"},
    )
    result = _service(migrated_database, resumed).execute(
        FullSyncRequest(custom_query="newer_than:2y")
    )

    assert interrupted.list_calls == [None, "page-2"]
    assert interrupted.thread_calls == ["t1"]
    assert resumed.list_calls == ["page-2"]
    assert resumed.thread_calls == ["t2"]
    assert set(resumed.message_calls) == {"m1", "m2", "m3"}
    assert result.status is RunStatus.SUCCEEDED
    assert result.pages_processed == 2
    assert result.discovered_count == 3
    assert result.new_count == 3
    assert result.failed_count == 0
    assert result.baseline_history_id == "100"

    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        account = unit.accounts.get_by_profile_email("user@example.com")
        assert account is not None
        state = unit.sync_states.get_or_create(account.id)
        emails = [
            unit.emails.get_by_gmail_id(account.id, item) for item in ("m1", "m2", "m3")
        ]
        assert state.full_sync_status is FullSyncStatus.SUCCEEDED
        assert state.committed_history_id == "100"
        assert emails[0] is not None and emails[0].gmail_thread_id == "t1"
        assert emails[1] is not None
        attachments = unit.attachments.list_for_email(emails[1].id)
        assert attachments[0].is_pdf
        assert attachments[0].gmail_attachment_id == "pdf-1"
    with migrated_database.engine.connect() as connection:
        run_count = connection.scalar(select(func.count()).select_from(SyncRunModel))
    assert run_count == 1


def test_full_sync_honors_persisted_cancel_before_next_gmail_page(
    migrated_database: Database,
) -> None:
    _configure_rule(migrated_database)
    interrupted = _FakeGateway(
        profile_history_id="100",
        pages={None: GmailConnectionError("fixture outage")},
        threads={},
        messages={},
    )
    request = FullSyncRequest(custom_query="newer_than:2y")
    with pytest.raises(GmailConnectionError):
        _service(migrated_database, interrupted).execute(request)

    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        account = unit.accounts.get_by_profile_email("user@example.com")
        assert account is not None
        run = unit.sync_runs.find_any_running(account.id)
        assert run is not None
        assert unit.sync_runs.request_cancel(run.id)
        unit.commit()

    resumed = _FakeGateway(
        profile_history_id="200",
        pages={},
        threads={},
        messages={},
    )
    result = _service(migrated_database, resumed).execute(request)

    assert result.status is RunStatus.CANCELLED
    assert resumed.list_calls == []
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        persisted = unit.sync_runs.get(result.run_id)
        assert persisted is not None
        assert persisted.status is RunStatus.CANCELLED
        assert persisted.worker_id is None


def test_single_message_failure_is_persisted_without_aborting_other_items(
    migrated_database: Database,
) -> None:
    _configure_rule(migrated_database)
    gateway = _FakeGateway(
        profile_history_id="300",
        pages={
            None: GmailMessagePage(
                messages=(
                    GmailMessageRef("good", "t1"),
                    GmailMessageRef("bad", "t2"),
                ),
                next_page_token=None,
                result_size_estimate=2,
            )
        },
        threads={
            "t1": _thread("t1", "good"),
            "t2": _thread("t2", "bad"),
        },
        messages={
            "good": _message("good", "t1", "safe fixture"),
            "bad": GmailMessageFetchError("fixture disappeared"),
        },
    )

    result = _service(migrated_database, gateway).execute(
        FullSyncRequest(custom_query="newer_than:2y")
    )

    assert result.status is RunStatus.PARTIAL
    assert result.new_count == 1
    assert result.failed_count == 1
    with migrated_database.engine.connect() as connection:
        email_count = connection.scalar(select(func.count()).select_from(EmailModel))
    assert email_count == 1


def test_full_sync_persists_parser_audit_and_transactions(
    migrated_database: Database,
) -> None:
    _configure_rule(migrated_database)
    gateway = _FakeGateway(
        profile_history_id="350",
        pages={
            None: GmailMessagePage(
                messages=(GmailMessageRef("purchase", "t1"),),
                next_page_token=None,
                result_size_estimate=1,
            )
        },
        threads={"t1": _thread("t1", "purchase")},
        messages={
            "purchase": _message(
                "purchase",
                "t1",
                "Purchase notification\n\n"
                "Transaction time: 2026/07/20 10:00\n"
                "Merchant: FIXTURE STORE\n"
                "Amount: USD 12.99\n"
                "Card ending: 1234",
            )
        },
    )

    result = _service(migrated_database, gateway).execute(
        FullSyncRequest(custom_query="newer_than:2y")
    )

    assert result.status is RunStatus.SUCCEEDED
    with migrated_database.session_factory() as session:
        run = session.scalar(select(SyncRunModel))
        email = session.scalar(select(EmailModel))
        transaction = session.scalar(select(TransactionModel))
        assert run is not None and run.parsed_count == 1
        assert run.parser_failed_count == 0
        assert email is not None
        assert email.processing_status == "processed"
        assert transaction is not None
        assert transaction.amount_minor == 1299
        assert session.scalar(select(func.count()).select_from(ParserRunModel)) == 1
        assert (
            session.scalar(select(func.count()).select_from(ClassificationResultModel))
            == 7
        )


def test_rejected_saved_page_token_restarts_listing_with_deduplication(
    migrated_database: Database,
) -> None:
    _configure_rule(migrated_database)
    first = _FakeGateway(
        profile_history_id="400",
        pages={
            None: GmailMessagePage(
                messages=(GmailMessageRef("m1", "t1"),),
                next_page_token="expired-token",
                result_size_estimate=1,
            ),
            "expired-token": GmailConnectionError("fixture interruption"),
        },
        threads={"t1": _thread("t1", "m1")},
        messages={},
    )
    with pytest.raises(GmailConnectionError):
        _service(migrated_database, first).execute(
            FullSyncRequest(custom_query="newer_than:2y")
        )

    resumed = _FakeGateway(
        profile_history_id="401",
        pages={
            "expired-token": GmailMessageFetchError("saved page token rejected"),
            None: GmailMessagePage(
                messages=(GmailMessageRef("m1", "t1"),),
                next_page_token=None,
                result_size_estimate=1,
            ),
        },
        threads={"t1": _thread("t1", "m1")},
        messages={"m1": _message("m1", "t1", "fixture")},
    )

    result = _service(migrated_database, resumed).execute(
        FullSyncRequest(custom_query="newer_than:2y")
    )

    assert resumed.list_calls == ["expired-token", None]
    assert result.status is RunStatus.SUCCEEDED
    assert result.discovered_count == 1
    assert result.new_count == 1


def test_explicit_all_senders_uses_complete_message_listing_without_threads(
    migrated_database: Database,
) -> None:
    gateway = _FakeGateway(
        profile_history_id="500",
        pages={
            None: GmailMessagePage(
                messages=(GmailMessageRef("ordinary", "t1"),),
                next_page_token=None,
                result_size_estimate=1,
            )
        },
        threads={},
        messages={"ordinary": _message("ordinary", "t1", "ordinary local fixture")},
        expected_query=None,
    )

    result = _service(migrated_database, gateway).execute(
        FullSyncRequest(all_senders=True)
    )

    assert result.status is RunStatus.SUCCEEDED
    assert not result.thread_expansion_enabled
    assert gateway.thread_calls == []
    assert result.new_count == 1
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        account = unit.accounts.get_by_profile_email("user@example.com")
        assert account is not None
        scope = unit.app_settings.get(f"sync_scope.{account.id}")
        assert scope is not None
        assert scope.value == {"all_senders": True}


def test_scope_change_requires_explicit_restart(
    migrated_database: Database,
) -> None:
    _configure_rule(migrated_database)
    interrupted = _FakeGateway(
        profile_history_id="600",
        pages={None: GmailConnectionError("fixture interruption")},
        threads={},
        messages={},
    )
    with pytest.raises(GmailConnectionError):
        _service(migrated_database, interrupted).execute(
            FullSyncRequest(custom_query="newer_than:2y")
        )

    changed = _FakeGateway(
        profile_history_id="601",
        pages={
            None: GmailMessagePage(
                messages=(),
                next_page_token=None,
                result_size_estimate=0,
            )
        },
        threads={},
        messages={},
        expected_query="{from:notice@bank.example} (newer_than:1y)",
    )
    with pytest.raises(SyncAlreadyRunningError, match="--restart"):
        _service(migrated_database, changed).execute(
            FullSyncRequest(custom_query="newer_than:1y")
        )

    result = _service(migrated_database, changed).execute(
        FullSyncRequest(custom_query="newer_than:1y", restart=True)
    )

    assert result.status is RunStatus.SUCCEEDED
    with migrated_database.engine.connect() as connection:
        statuses = list(
            connection.scalars(
                select(SyncRunModel.status).order_by(SyncRunModel.created_at)
            )
        )
    assert statuses == [RunStatus.FAILED.value, RunStatus.SUCCEEDED.value]


def test_full_sync_reparse_refreshes_a_bridged_financial_event(
    migrated_database: Database,
) -> None:
    """Regression: full and reconciliation syncs must reconcile bridged events.

    Only incremental sync and manual reparse used to run the bridge
    reconciliation, so a full or post-history-expiry run left a bridged event
    with no record that its source observation had changed. Here the reparse
    supersedes the confirmed transaction, which must surface as a discrepancy
    rather than silently leaving the event looking current.
    """
    _configure_rule(migrated_database)

    def _factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(migrated_database.session_factory)

    intake = FinancialEventIntake(_factory)
    gateway = _FakeGateway(
        profile_history_id="100",
        pages={
            None: GmailMessagePage(
                messages=(GmailMessageRef("m1", "t1"),),
                next_page_token=None,
                result_size_estimate=1,
            )
        },
        threads={"t1": _thread("t1", "m1")},
        messages={"m1": _message("m1", "t1", "消費金額 NT$1,280")},
    )
    _service(migrated_database, gateway, intake=intake).execute(
        FullSyncRequest(custom_query="newer_than:2y")
    )

    with _factory() as unit:
        email = unit.emails.get_by_gmail_id(1, "m1")
        assert email is not None
        transaction = unit.transactions.list_for_email(email.id)[0]
        # The generic parser leaves no transaction time, and intake never
        # substitutes one, so the reviewer supplies it explicitly.
        unit.transactions.apply_review(
            TransactionReviewDraft(
                transaction_id=transaction.id,
                expected_updated_at=transaction.updated_at,
                override_fields=frozenset({"transaction_at"}),
                transaction_type=TransactionType.PURCHASE,
                merchant=None,
                amount_minor=None,
                currency=None,
                transaction_at=datetime(2026, 7, 27, 2, 30, tzinfo=UTC),
                manually_confirmed=True,
                manually_ignored=False,
                user_note=None,
            )
        )
        intake.stage(unit, (transaction.id,))
        unit.commit()
        staged = unit.financial_events.get_by_idempotency_key(
            intake_idempotency_key(transaction.id)
        )
        assert staged is not None
        original_amount = staged.amount_minor

    # A different amount yields a different parser source key, so the bridged
    # transaction is superseded rather than updated in place.
    gateway.messages["m1"] = _message("m1", "t1", "消費金額 NT$9,999")
    gateway.pages[None] = GmailMessagePage(
        messages=(GmailMessageRef("m1", "t1"),),
        next_page_token=None,
        result_size_estimate=1,
    )
    _service(migrated_database, gateway, intake=intake).execute(
        FullSyncRequest(custom_query="newer_than:2y", restart=True)
    )

    with _factory() as unit:
        link = unit.source_links.get_confirmed_for_transaction(transaction.id)
        assert link is not None
        assert SourceDiscrepancy.SOURCE_NO_LONGER_CONFIRMED in link.discrepancies
        untouched = unit.financial_events.get_by_idempotency_key(
            intake_idempotency_key(transaction.id)
        )
        assert untouched is not None
        assert untouched.amount_minor == original_amount

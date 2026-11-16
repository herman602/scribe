"""File-backed repository and unit-of-work integration tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app.domain.enums import (
    EmailSourceStatus,
    FullSyncStatus,
    RunStatus,
    SenderMatchType,
    SyncMode,
    TransactionType,
)
from app.domain.errors import (
    ConcurrentUpdateError,
    DatabaseError,
    SyncAlreadyRunningError,
)
from app.domain.models import (
    AttachmentSnapshot,
    EmailSnapshot,
    SenderRuleDraft,
    TransactionDraft,
)
from app.infrastructure.database.engine import Database
from app.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork
from sqlalchemy import text


def _create_email(database: Database, gmail_message_id: str = "gmail-001") -> int:
    with SqlAlchemyUnitOfWork(database.session_factory) as unit:
        account = unit.accounts.get_or_create("User@Example.COM")
        outcome = unit.emails.upsert_from_gmail(
            EmailSnapshot(
                gmail_account_id=account.id,
                gmail_message_id=gmail_message_id,
                gmail_thread_id="thread-001",
                gmail_history_id="100",
                sender="notify@example-bank.test",
                recipients=("user@example.com",),
                subject="消費通知",
                gmail_internal_date_ms=1_721_000_000_000,
                received_at=datetime(2026, 7, 27, 8, 30, tzinfo=UTC),
                body_text="已去識別化的測試內容",
                labels=("INBOX", "IMPORTANT", "INBOX"),
            )
        )
        unit.commit()
        return outcome.email.id


def test_account_and_email_upsert_are_idempotent(
    migrated_database: Database,
) -> None:
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        account = unit.accounts.get_or_create(" User@Example.COM ")
        first = unit.emails.upsert_from_gmail(
            EmailSnapshot(
                gmail_account_id=account.id,
                gmail_message_id="gmail-001",
                gmail_thread_id="thread-001",
                gmail_history_id="100",
                sender="notice@bank.test",
                recipients=("b@example.com", "a@example.com", "a@example.com"),
                subject="原始主旨",
                gmail_internal_date_ms=1_721_000_000_000,
                labels=("INBOX", "IMPORTANT", "INBOX"),
            )
        )
        second = unit.emails.upsert_from_gmail(
            EmailSnapshot(
                gmail_account_id=account.id,
                gmail_message_id="gmail-001",
                gmail_thread_id="thread-002",
                gmail_history_id="101",
                sender="notice@bank.test",
                recipients=("a@example.com",),
                subject="更新主旨",
                gmail_internal_date_ms=1_721_000_000_500,
                labels=("INBOX",),
            )
        )
        unit.commit()

    assert account.profile_email == "user@example.com"
    assert first.created
    assert not second.created
    assert first.email.id == second.email.id
    assert second.email.subject == "更新主旨"
    assert second.email.gmail_thread_id == "thread-002"
    assert second.email.labels == ("INBOX",)


def test_one_email_can_have_multiple_integer_minor_unit_transactions(
    migrated_database: Database,
) -> None:
    email_id = _create_email(migrated_database)
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        first = unit.transactions.add(
            TransactionDraft(
                email_id=email_id,
                source_key="row-1",
                transaction_type=TransactionType.PURCHASE,
                merchant="測試商店",
                amount_minor=128000,
                currency="TWD",
                card_last_four="1234",
                confidence=Decimal("0.95"),
                parser_name="fixture",
                parser_version="1.0",
            )
        )
        second = unit.transactions.add(
            TransactionDraft(
                email_id=email_id,
                source_key="row-2",
                transaction_type=TransactionType.REFUND,
                amount_minor=1299,
                currency="USD",
                confidence=Decimal("0.80"),
                parser_name="fixture",
                parser_version="1.0",
            )
        )
        unit.commit()
        transactions = unit.transactions.list_for_email(email_id)

    assert [item.id for item in transactions] == [first.id, second.id]
    assert [(item.amount_minor, item.currency) for item in transactions] == [
        (128000, "TWD"),
        (1299, "USD"),
    ]


def test_transaction_constraints_are_checked_before_persistence(
    migrated_database: Database,
) -> None:
    email_id = _create_email(migrated_database)
    with (
        SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit,
        pytest.raises(ValueError, match="currency"),
    ):
        unit.transactions.add(
            TransactionDraft(
                email_id=email_id,
                source_key="invalid",
                transaction_type=TransactionType.PURCHASE,
                amount_minor=100,
                currency="twd",
                confidence=Decimal("0.8"),
                parser_name="fixture",
                parser_version="1.0",
            )
        )


def test_unique_source_key_failure_is_a_database_error(
    migrated_database: Database,
) -> None:
    email_id = _create_email(migrated_database)
    draft = TransactionDraft(
        email_id=email_id,
        source_key="stable-key",
        transaction_type=TransactionType.PURCHASE,
        confidence=Decimal("0.9"),
        parser_name="fixture",
        parser_version="1.0",
    )
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        unit.transactions.add(draft)
        unit.commit()
    with (
        SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit,
        pytest.raises(DatabaseError, match="database write failed"),
    ):
        unit.transactions.add(draft)


def test_foreign_keys_are_enforced_and_translated(
    migrated_database: Database,
) -> None:
    with (
        SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit,
        pytest.raises(DatabaseError, match="database write failed"),
    ):
        unit.emails.upsert_from_gmail(
            EmailSnapshot(
                gmail_account_id=999_999,
                gmail_message_id="orphan",
                gmail_thread_id="thread",
                gmail_history_id="100",
                sender="notice@bank.test",
                recipients=("user@example.com",),
                subject="不可成為孤兒的 Email",
                gmail_internal_date_ms=1_721_000_000_000,
            )
        )


def test_soft_delete_preserves_email_and_transactions(
    migrated_database: Database,
) -> None:
    email_id = _create_email(migrated_database)
    deleted_at = datetime(2026, 7, 27, 9, tzinfo=UTC)
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        email = unit.emails.get(email_id)
        assert email is not None
        changed = unit.emails.mark_source_deleted(
            email.gmail_account_id,
            email.gmail_message_id,
            deleted_at,
        )
        unit.commit()
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        deleted = unit.emails.get(email_id)

    assert changed
    assert deleted is not None
    assert deleted.source_status is EmailSourceStatus.DELETED


def test_sync_state_uses_optimistic_concurrency(
    migrated_database: Database,
) -> None:
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        account = unit.accounts.get_or_create("sync@example.com")
        initial = unit.sync_states.get_or_create(account.id)
        unit.commit()

    stale = initial
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        current = unit.sync_states.get_or_create(account.id)
        updated = unit.sync_states.save(
            replace(
                current,
                committed_history_id="200",
                full_sync_status=FullSyncStatus.SUCCEEDED,
            ),
            expected_version=current.version,
        )
        unit.commit()

    assert updated.version == 2
    assert updated.committed_history_id == "200"
    with (
        SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit,
        pytest.raises(ConcurrentUpdateError),
    ):
        unit.sync_states.save(
            replace(stale, committed_history_id="stale"),
            expected_version=stale.version,
        )


def test_unit_of_work_rolls_back_without_explicit_commit(
    migrated_database: Database,
) -> None:
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        unit.app_settings.set("dashboard.page_size", 50)

    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        assert unit.app_settings.get("dashboard.page_size") is None


def test_app_setting_json_is_serialized_canonically(
    migrated_database: Database,
) -> None:
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        setting = unit.app_settings.set(
            "dashboard.filters",
            {"z": 1, "a": ["TWD", "USD"]},
            schema_version=2,
        )
        unit.commit()
    with migrated_database.engine.connect() as connection:
        raw_json = connection.scalar(
            text("SELECT value_json FROM app_settings WHERE key = 'dashboard.filters'")
        )

    assert setting.schema_version == 2
    assert raw_json == '{"a":["TWD","USD"],"z":1}'


@pytest.mark.parametrize(
    "key",
    ["gmail.token", "pdf-password", "oauth_client_secret", "api.credential"],
)
def test_app_settings_reject_secret_keys(
    migrated_database: Database,
    key: str,
) -> None:
    with (
        SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit,
        pytest.raises(ValueError, match="secrets"),
    ):
        unit.app_settings.set(key, "must-not-be-stored")


def test_sender_rules_are_normalized_updated_and_soft_disabled(
    migrated_database: Database,
) -> None:
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        account = unit.accounts.get_or_create("rules@example.com")
        first = unit.sender_rules.upsert(
            SenderRuleDraft(
                gmail_account_id=account.id,
                institution_key="fubon",
                institution_name="富邦",
                match_type=SenderMatchType.EXACT_ADDRESS,
                match_value="NOTICE@BANK.EXAMPLE",
            )
        )
        updated = unit.sender_rules.upsert(
            SenderRuleDraft(
                gmail_account_id=account.id,
                institution_key="fubon",
                institution_name="富邦銀行",
                match_type=SenderMatchType.EXACT_ADDRESS,
                match_value="notice@bank.example",
                priority=10,
            )
        )
        disabled = unit.sender_rules.disable(first.id)
        unit.commit()

    assert first.match_value_normalized == "notice@bank.example"
    assert updated.id == first.id
    assert updated.version == 2
    assert updated.priority == 10
    assert disabled is not None and not disabled.enabled
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        assert unit.sender_rules.list_enabled(account.id) == ()
        assert unit.accounts.get_by_profile_email("RULES@example.com") == account
        assert unit.accounts.list_all() == (account,)


def test_supplementary_card_settings_are_account_and_bank_scoped(
    migrated_database: Database,
) -> None:
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        first_account = unit.accounts.get_or_create("first@example.test")
        second_account = unit.accounts.get_or_create("second@example.test")
        first = unit.supplementary_card_settings.mark(
            account_id=first_account.id,
            institution_key="fubon",
            card_last_four="1234",
        )
        repeated = unit.supplementary_card_settings.mark(
            account_id=first_account.id,
            institution_key="fubon",
            card_last_four="1234",
        )
        unit.supplementary_card_settings.mark(
            account_id=first_account.id,
            institution_key="cathay",
            card_last_four="1234",
        )
        unit.supplementary_card_settings.mark(
            account_id=second_account.id,
            institution_key="fubon",
            card_last_four="1234",
        )
        unit.commit()

    assert first.id == repeated.id
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        assert unit.supplementary_card_settings.is_marked(
            account_id=first_account.id,
            institution_key="fubon",
            card_last_four="1234",
        )
        assert unit.supplementary_card_settings.unmark(
            account_id=first_account.id,
            institution_key="fubon",
            card_last_four="1234",
        )
        assert not unit.supplementary_card_settings.is_marked(
            account_id=first_account.id,
            institution_key="fubon",
            card_last_four="1234",
        )
        assert unit.supplementary_card_settings.is_marked(
            account_id=first_account.id,
            institution_key="cathay",
            card_last_four="1234",
        )
        assert unit.supplementary_card_settings.is_marked(
            account_id=second_account.id,
            institution_key="fubon",
            card_last_four="1234",
        )


@pytest.mark.parametrize("suffix", ["123", "12345", "12A4", "    "])
def test_supplementary_card_settings_reject_invalid_suffixes(
    migrated_database: Database,
    suffix: str,
) -> None:
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        account = unit.accounts.get_or_create("cards@example.test")
        with pytest.raises(ValueError, match="four digits"):
            unit.supplementary_card_settings.mark(
                account_id=account.id,
                institution_key="fubon",
                card_last_four=suffix,
            )


def test_attachment_reconciliation_preserves_unchanged_identity(
    migrated_database: Database,
) -> None:
    email_id = _create_email(migrated_database)
    original = AttachmentSnapshot(
        mime_part_id="1",
        filename="notice.pdf",
        mime_type="application/pdf",
        size_bytes=8,
        sha256="a" * 64,
        is_pdf=True,
        gmail_attachment_id="attachment-1",
    )
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        first = unit.attachments.replace_for_email(email_id, (original,))
        unit.commit()
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        second = unit.attachments.replace_for_email(email_id, (original,))
        unit.commit()
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        removed = unit.attachments.replace_for_email(email_id, ())
        unit.commit()

    assert first[0].id == second[0].id
    assert first[0].sha256 == "a" * 64
    assert removed == ()


def test_sync_run_queue_deduplicates_and_tracks_progress(
    migrated_database: Database,
) -> None:
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        account = unit.accounts.get_or_create("queue@example.com")
        run = unit.sync_runs.create(
            account_id=account.id,
            mode=SyncMode.FULL,
            query="{from:notice@bank.example}",
            start_history_id="100",
        )
        assert unit.sync_runs.enqueue_message_ids(run.id, ("m2", "m1", "m1")) == 2
        assert unit.sync_runs.enqueue_message_ids(run.id, ("m1",)) == 0
        checkpoint = unit.sync_runs.checkpoint_page(run.id, next_page_token=None)
        items = unit.sync_runs.list_processable(run.id, limit=10)
        unit.sync_runs.mark_succeeded(items[0].id, created=True)
        unit.sync_runs.mark_failed(
            items[1].id,
            error_code="EmailDecodeError",
            error_message="fixture failure",
            retryable=False,
        )
        finished = unit.sync_runs.finish(
            run.id,
            status=RunStatus.PARTIAL,
            end_history_id=None,
            error_code="item_failures",
        )
        unit.commit()

    assert checkpoint.pages_processed == 1
    assert checkpoint.discovered_count == 2
    assert [item.gmail_message_id for item in items] == ["m1", "m2"]
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        persisted = unit.sync_runs.get(run.id)
        assert persisted is not None
        assert persisted.status is RunStatus.PARTIAL
        assert persisted.new_count == 1
        assert unit.sync_runs.failed_count(run.id) == 1
        assert unit.sync_runs.find_any_running(account.id) is None
    assert finished.status is RunStatus.PARTIAL


def test_sync_run_lease_is_atomic_expires_and_accepts_cancellation(
    migrated_database: Database,
) -> None:
    now = datetime(2026, 7, 28, 8, tzinfo=UTC)
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        account = unit.accounts.get_or_create("lease@example.com")
        run = unit.sync_runs.create(
            account_id=account.id,
            mode=SyncMode.INCREMENTAL,
            query=None,
            start_history_id="100",
        )
        unit.commit()

    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        assert unit.sync_runs.acquire_lease(
            run.id,
            worker_id="worker-a",
            now=now,
            expires_at=now + timedelta(minutes=5),
        )
        unit.commit()
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        assert not unit.sync_runs.acquire_lease(
            run.id,
            worker_id="worker-b",
            now=now + timedelta(minutes=1),
            expires_at=now + timedelta(minutes=6),
        )
        assert unit.sync_runs.request_cancel(run.id)
        unit.commit()
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        assert unit.sync_runs.cancellation_requested(run.id)
        assert unit.sync_runs.acquire_lease(
            run.id,
            worker_id="worker-b",
            now=now + timedelta(minutes=6),
            expires_at=now + timedelta(minutes=11),
        )
        unit.sync_runs.release_lease(run.id, worker_id="worker-a")
        unit.commit()
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        persisted = unit.sync_runs.get(run.id)

    assert persisted is not None
    assert persisted.worker_id == "worker-b"
    assert persisted.cancel_requested


def test_database_rejects_two_running_syncs_for_one_account(
    migrated_database: Database,
) -> None:
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        account = unit.accounts.get_or_create("single-sync@example.com")
        first = unit.sync_runs.create(
            account_id=account.id,
            mode=SyncMode.FULL,
            query=None,
            start_history_id="100",
        )
        unit.commit()

    with (
        SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit,
        pytest.raises(SyncAlreadyRunningError),
    ):
        unit.sync_runs.create(
            account_id=account.id,
            mode=SyncMode.INCREMENTAL,
            query=None,
            start_history_id="100",
        )

    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        unit.sync_runs.finish(
            first.id,
            status=RunStatus.CANCELLED,
            end_history_id=None,
        )
        replacement = unit.sync_runs.create(
            account_id=account.id,
            mode=SyncMode.INCREMENTAL,
            query=None,
            start_history_id="100",
        )
        unit.commit()

    assert replacement.status is RunStatus.RUNNING

"""Offline review/reparse service tests."""

from __future__ import annotations

from decimal import Decimal

import pytest
from app.application.services.stored_email_reparse import ReparseStoredEmail
from app.application.services.transaction_confirmation import ConfirmTransactions
from app.application.services.transaction_review import (
    ReviewTransaction,
    ReviewTransactionRequest,
)
from app.domain.enums import ClassificationStatus, TransactionType
from app.domain.models import EmailSnapshot, TransactionDraft
from app.domain.parsing import (
    Evidence,
    PipelineResult,
    StageResult,
    TransactionCandidate,
)
from app.infrastructure.database.engine import Database
from app.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork


class _ChangedPipeline:
    def evaluate(self, email: object) -> PipelineResult:
        del email
        stage = StageResult(
            stage_name="fixture",
            stage_version="2.0",
            verdict=ClassificationStatus.CONFIRMED,
            confidence=Decimal("0.96"),
            reason_code="fixture_changed",
            reason="Anonymized fixture was reparsed",
            extracted_fields={},
        )
        return PipelineResult(
            classification=ClassificationStatus.CONFIRMED,
            confidence=Decimal("0.96"),
            reason="Anonymized fixture was reparsed",
            parser_name="fixture",
            parser_version="2.0",
            stages=(stage,),
            candidates=(
                TransactionCandidate(
                    source_key="stable",
                    transaction_type=TransactionType.PURCHASE,
                    merchant="NEW PARSED MERCHANT",
                    amount_minor=25000,
                    currency="TWD",
                    confidence=Decimal("0.96"),
                    evidence=(
                        Evidence(
                            field="amount",
                            source="body",
                            excerpt="anonymized amount",
                        ),
                    ),
                ),
            ),
            warnings=(),
        )


def test_batch_confirmation_is_atomic_deduplicated_and_idempotent(
    migrated_database: Database,
) -> None:
    def unit_of_work_factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(migrated_database.session_factory)

    with unit_of_work_factory() as unit:
        account = unit.accounts.get_or_create("confirm@example.test")
        email = unit.emails.upsert_from_gmail(
            EmailSnapshot(
                gmail_account_id=account.id,
                gmail_message_id="confirm-email",
                gmail_thread_id="confirm-thread",
                gmail_history_id="701",
                sender="notice@example.test",
                recipients=("confirm@example.test",),
                subject="Anonymized purchase",
                gmail_internal_date_ms=1_721_000_000_000,
                body_text="Anonymized body",
            )
        ).email
        transaction = unit.transactions.add(
            TransactionDraft(
                email_id=email.id,
                source_key="confirm-stable",
                transaction_type=TransactionType.PURCHASE,
                merchant="UNCHANGED MERCHANT",
                amount_minor=10000,
                currency="TWD",
                confidence=Decimal("0.90"),
                parser_name="fixture",
                parser_version="1.0",
            )
        )
        ignored = unit.transactions.add(
            TransactionDraft(
                email_id=email.id,
                source_key="ignored-stable",
                transaction_type=TransactionType.PURCHASE,
                merchant="IGNORED MERCHANT",
                amount_minor=20000,
                currency="TWD",
                confidence=Decimal("0.90"),
                parser_name="fixture",
                parser_version="1.0",
            )
        )
        unit.commit()

    ReviewTransaction(unit_of_work_factory).execute(
        ReviewTransactionRequest(
            transaction_id=ignored.id,
            expected_updated_at=ignored.updated_at,
            override_fields=frozenset(),
            transaction_type=ignored.transaction_type,
            merchant=ignored.merchant,
            amount_minor=ignored.amount_minor,
            currency=ignored.currency,
            transaction_at=ignored.transaction_at,
            manually_confirmed=False,
            manually_ignored=True,
            user_note=None,
        )
    )
    service = ConfirmTransactions(unit_of_work_factory)
    first = service.execute((transaction.id, transaction.id, ignored.id))
    second = service.execute((transaction.id,))

    assert first.requested_count == 2
    assert first.confirmed_count == 1
    assert second.requested_count == 1
    assert second.confirmed_count == 0
    with unit_of_work_factory() as unit:
        confirmed = unit.transactions.get(transaction.id)
        ignored_after_confirmation = unit.transactions.get(ignored.id)
    assert confirmed is not None
    assert confirmed.manually_confirmed
    assert confirmed.merchant == "UNCHANGED MERCHANT"
    assert confirmed.amount_minor == 10000
    assert ignored_after_confirmation is not None
    assert ignored_after_confirmation.manually_ignored
    assert not ignored_after_confirmation.manually_confirmed
    with pytest.raises(ValueError, match="請至少選擇"):
        service.execute(())


def test_offline_reparse_refreshes_parsed_values_but_preserves_manual_override(
    migrated_database: Database,
) -> None:
    def unit_of_work_factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(migrated_database.session_factory)

    with unit_of_work_factory() as unit:
        account = unit.accounts.get_or_create("review@example.test")
        email = unit.emails.upsert_from_gmail(
            EmailSnapshot(
                gmail_account_id=account.id,
                gmail_message_id="reparse-email",
                gmail_thread_id="reparse-thread",
                gmail_history_id="700",
                sender="notice@example.test",
                recipients=("review@example.test",),
                subject="Anonymized purchase",
                gmail_internal_date_ms=1_721_000_000_000,
                body_text="Anonymized body",
            )
        ).email
        transaction = unit.transactions.add(
            TransactionDraft(
                email_id=email.id,
                source_key="stable",
                transaction_type=TransactionType.PURCHASE,
                merchant="OLD PARSED MERCHANT",
                amount_minor=10000,
                currency="TWD",
                confidence=Decimal("0.80"),
                parser_name="fixture",
                parser_version="1.0",
            )
        )
        unit.commit()

    ReviewTransaction(unit_of_work_factory).execute(
        ReviewTransactionRequest(
            transaction_id=transaction.id,
            expected_updated_at=transaction.updated_at,
            override_fields=frozenset({"merchant"}),
            transaction_type=TransactionType.PURCHASE,
            merchant="MANUAL MERCHANT",
            amount_minor=10000,
            currency="TWD",
            transaction_at=None,
            manually_confirmed=True,
            manually_ignored=False,
            user_note="checked",
        )
    )
    result = ReparseStoredEmail(
        _ChangedPipeline(),
        unit_of_work_factory,
    ).execute(email.id)

    with unit_of_work_factory() as unit:
        refreshed = unit.transactions.get(transaction.id)
        stored_email = unit.emails.get(email.id)

    assert result.parser_version == "2.0"
    assert refreshed is not None
    assert refreshed.merchant == "MANUAL MERCHANT"
    assert refreshed.amount_minor == 25000
    assert refreshed.manually_confirmed
    assert stored_email is not None
    assert stored_email.classification_status is ClassificationStatus.CONFIRMED

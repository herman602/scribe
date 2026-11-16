"""B7 acceptance tests for matching a bank notice to a typed expense."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from app.application.services.financial_event_intake import (
    IntakeResult,
    intake_idempotency_key,
)
from app.application.services.manual_money import ManualExpenseRequest
from app.domain.budget_enums import (
    FinancialEventOrigin,
    FinancialEventStatus,
    PaymentMethod,
    SourceLinkReason,
    SourceLinkStatus,
)
from app.domain.enums import SenderMatchType, TransactionType
from app.domain.errors import SourceLinkError
from app.domain.models import EmailSnapshot, SenderRuleDraft, TransactionDraft

from tests.b7_support import B7_NOW, B7Services, B7World

MERCHANT = "測試咖啡"


def _seed_notice(
    b7: B7Services,
    world: B7World,
    *,
    message_id: str = "gmail-b7-001",
    merchant: str | None = MERCHANT,
    amount_minor: int = 45_000,
    currency: str = "TWD",
    offset: timedelta = timedelta(0),
    card_last_four: str | None = "4321",
    account_last_four: str | None = None,
    source_key: str = "row-1",
) -> int:
    """Create one confirmed parsed transaction that describes a card purchase."""
    with b7.factory() as unit:
        rule = unit.sender_rules.upsert(
            SenderRuleDraft(
                gmail_account_id=world.gmail_account_id,
                institution_key="test_bank",
                institution_name="測試銀行",
                match_type=SenderMatchType.EXACT_ADDRESS,
                match_value="notice@example-bank.test",
            )
        )
        outcome = unit.emails.upsert_from_gmail(
            EmailSnapshot(
                gmail_account_id=world.gmail_account_id,
                gmail_message_id=message_id,
                gmail_thread_id="thread-b7",
                gmail_history_id="500",
                sender="notice@example-bank.test",
                recipients=("user@example.test",),
                subject="消費通知",
                gmail_internal_date_ms=1_754_000_000_000,
                matched_sender_rule_id=rule.id,
            )
        )
        transaction = unit.transactions.add(
            TransactionDraft(
                email_id=outcome.email.id,
                source_key=source_key,
                transaction_type=TransactionType.PURCHASE,
                confidence=Decimal("0.95"),
                parser_name="tests.fake",
                parser_version="1",
                merchant=merchant,
                amount_minor=amount_minor,
                currency=currency,
                card_last_four=card_last_four,
                account_last_four=account_last_four,
                transaction_at=B7_NOW + offset,
            )
        )
        unit.transactions.confirm_pending(frozenset({transaction.id}))
        unit.commit()
        return transaction.id


def _manual_card_expense(
    b7: B7Services,
    world: B7World,
    *,
    merchant: str = MERCHANT,
    amount_minor: int = 45_000,
    token: str = "card-token",
    offset: timedelta = timedelta(0),
) -> int:
    result = b7.expenses.execute(
        ManualExpenseRequest(
            payment_method=PaymentMethod.CREDIT_CARD,
            occurred_at=B7_NOW + offset,
            merchant=merchant,
            amount_minor=amount_minor,
            currency="TWD",
            funding_wallet_id=world.bank_unallocated_id,
            client_token=token,
            budget_category_id=world.food_id,
            card_id=world.card_id,
        )
    )
    return result.event.id


def _stage(b7: B7Services, transaction_id: int) -> IntakeResult:
    with b7.factory() as unit:
        result = b7.intake.stage(unit, (transaction_id,))
        unit.commit()
    return result


def _events(b7: B7Services) -> int:
    with b7.factory() as unit:
        return len(
            unit.financial_events.list_by_status(frozenset(FinancialEventStatus))
        )


def test_one_exact_candidate_links_without_creating_a_second_event(
    b7: B7Services, b7_world: B7World
) -> None:
    event_id = _manual_card_expense(b7, b7_world)
    transaction_id = _seed_notice(b7, b7_world)

    result = _stage(b7, transaction_id)

    assert result.matched_count == 1
    assert result.matched[0].event_id == event_id
    assert result.matched[0].score_bps == 10_000
    assert result.staged_count == 0
    assert _events(b7) == 1
    with b7.factory() as unit:
        links = unit.source_links.list_for_transaction(transaction_id)
        assert len(links) == 1
        assert links[0].status is SourceLinkStatus.CONFIRMED
        assert SourceLinkReason.AUTO_EXACT_MATCH in links[0].reasons
        assert (
            unit.financial_events.get_by_idempotency_key(
                intake_idempotency_key(transaction_id)
            )
            is None
        )


def test_a_match_creates_no_journal_entry_of_its_own(
    b7: B7Services, b7_world: B7World
) -> None:
    event_id = _manual_card_expense(b7, b7_world)
    transaction_id = _seed_notice(b7, b7_world)

    _stage(b7, transaction_id)

    with b7.factory() as unit:
        entry = unit.journal.get_by_financial_event(event_id)
        assert entry is not None
        # Exactly the entry the manual expense already posted; matching adds
        # provenance only.
        assert entry.idempotency_key.startswith("allocation:financial_event:")


def test_a_weaker_single_candidate_stays_pending(
    b7: B7Services, b7_world: B7World
) -> None:
    """A different merchant scores below the maximum, so a person decides."""
    _manual_card_expense(b7, b7_world, merchant="另一家咖啡")
    transaction_id = _seed_notice(b7, b7_world)

    result = _stage(b7, transaction_id)

    assert result.pending_count == 1
    assert result.staged_count == 0
    assert _events(b7) == 1
    with b7.factory() as unit:
        links = unit.source_links.list_for_transaction(transaction_id)
        assert [link.status for link in links] == [SourceLinkStatus.CANDIDATE]


def test_two_candidates_never_link_automatically(
    b7: B7Services, b7_world: B7World
) -> None:
    _manual_card_expense(b7, b7_world, token="first")
    _manual_card_expense(b7, b7_world, token="second", offset=timedelta(minutes=5))
    transaction_id = _seed_notice(b7, b7_world)

    result = _stage(b7, transaction_id)

    assert result.pending_count == 1
    assert len(result.pending[0].candidate_event_ids) == 2
    with b7.factory() as unit:
        links = unit.source_links.list_for_transaction(transaction_id)
        assert len(links) == 2
        assert all(link.status is SourceLinkStatus.CANDIDATE for link in links)


def test_retrying_a_pending_transaction_creates_no_duplicates(
    b7: B7Services, b7_world: B7World
) -> None:
    _manual_card_expense(b7, b7_world, merchant="另一家咖啡")
    transaction_id = _seed_notice(b7, b7_world)

    first = _stage(b7, transaction_id)
    second = _stage(b7, transaction_id)

    assert first.pending_count == 1
    assert second.pending_count == 1
    assert _events(b7) == 1
    with b7.factory() as unit:
        assert len(unit.source_links.list_for_transaction(transaction_id)) == 1


def test_a_rejected_decision_keeps_blocking_direct_intake(
    b7: B7Services, b7_world: B7World
) -> None:
    _manual_card_expense(b7, b7_world, merchant="另一家咖啡")
    transaction_id = _seed_notice(b7, b7_world)
    pending = _stage(b7, transaction_id)
    assert pending.pending_count == 1
    with b7.factory() as unit:
        link_id = unit.source_links.list_for_transaction(transaction_id)[0].id
    b7.review.reject(link_id)

    result = _stage(b7, transaction_id)

    assert result.pending_count == 1
    assert result.staged_count == 0
    assert _events(b7) == 1


def test_a_bank_account_notice_never_matches_a_card_expense(
    b7: B7Services, b7_world: B7World
) -> None:
    """An account debit and a card purchase are different instruments."""
    _manual_card_expense(b7, b7_world)
    transaction_id = _seed_notice(
        b7, b7_world, card_last_four=None, account_last_four="7788"
    )

    result = _stage(b7, transaction_id)

    assert result.staged_count == 1
    assert result.matched_count == 0
    assert result.pending_count == 0


def test_a_card_notice_without_a_last_four_still_goes_to_review(
    b7: B7Services, b7_world: B7World
) -> None:
    """Missing digits mean the card is unknown, not that it was a bank debit.

    Treating it as a bank notification is what created a duplicate event for a
    purchase the user had already typed in.
    """
    event_id = _manual_card_expense(b7, b7_world)
    transaction_id = _seed_notice(b7, b7_world, card_last_four=None)

    result = _stage(b7, transaction_id)

    assert result.pending_count == 1
    assert result.staged_count == 0
    assert result.matched_count == 0
    assert result.pending[0].candidate_event_ids == (event_id,)
    assert _events(b7) == 1
    with b7.factory() as unit:
        links = unit.source_links.list_for_transaction(transaction_id)
        assert [link.status for link in links] == [SourceLinkStatus.CANDIDATE]
        # Neither side can claim the card, so the maximum reachable score is
        # 7,000 and auto-linking is impossible by arithmetic, not by luck.
        assert links[0].match_score_bps == 7_000
        assert SourceLinkReason.EXACT_CARD not in links[0].reasons
        assert (
            unit.financial_events.get_by_idempotency_key(
                intake_idempotency_key(transaction_id)
            )
            is None
        )


def test_a_card_notice_without_a_last_four_can_be_confirmed_by_a_person(
    b7: B7Services, b7_world: B7World
) -> None:
    event_id = _manual_card_expense(b7, b7_world)
    transaction_id = _seed_notice(b7, b7_world, card_last_four=None)
    _stage(b7, transaction_id)
    with b7.factory() as unit:
        link_id = unit.source_links.list_for_transaction(transaction_id)[0].id

    confirmed = b7.review.confirm(link_id)

    assert confirmed.status is SourceLinkStatus.CONFIRMED
    assert confirmed.financial_event_id == event_id
    assert _events(b7) == 1


def test_a_bank_account_notice_cannot_be_confirmed_against_a_card_expense(
    b7: B7Services, b7_world: B7World
) -> None:
    event_id = _manual_card_expense(b7, b7_world)
    transaction_id = _seed_notice(b7, b7_world, card_last_four=None)
    _stage(b7, transaction_id)
    with b7.factory() as unit:
        link_id = unit.source_links.list_for_transaction(transaction_id)[0].id
        # The parser is rerun and now recognizes it as an account debit.
        transaction = unit.transactions.get(transaction_id)
        assert transaction is not None
        unit.transactions.reconcile(
            transaction.email_id,
            (
                TransactionDraft(
                    email_id=transaction.email_id,
                    source_key="row-1",
                    transaction_type=TransactionType.PURCHASE,
                    confidence=Decimal("0.95"),
                    parser_name="tests.fake",
                    parser_version="2",
                    merchant=MERCHANT,
                    amount_minor=45_000,
                    currency="TWD",
                    account_last_four="7788",
                    transaction_at=B7_NOW,
                ),
            ),
        )
        unit.commit()
    assert event_id

    with pytest.raises(SourceLinkError):
        b7.review.confirm(link_id)


def test_confirming_a_candidate_records_the_decision(
    b7: B7Services, b7_world: B7World
) -> None:
    event_id = _manual_card_expense(b7, b7_world, merchant="另一家咖啡")
    transaction_id = _seed_notice(b7, b7_world)
    _stage(b7, transaction_id)
    with b7.factory() as unit:
        link_id = unit.source_links.list_for_transaction(transaction_id)[0].id

    confirmed = b7.review.confirm(link_id)

    assert confirmed.status is SourceLinkStatus.CONFIRMED
    assert SourceLinkReason.USER_CONFIRMED_MATCH in confirmed.reasons
    assert confirmed.financial_event_id == event_id
    assert _events(b7) == 1


def test_confirming_rejects_every_sibling_proposal(
    b7: B7Services, b7_world: B7World
) -> None:
    _manual_card_expense(b7, b7_world, token="first", merchant="另一家咖啡")
    _manual_card_expense(
        b7, b7_world, token="second", merchant="第三家咖啡", offset=timedelta(minutes=5)
    )
    transaction_id = _seed_notice(b7, b7_world)
    _stage(b7, transaction_id)
    with b7.factory() as unit:
        link_ids = [
            link.id for link in unit.source_links.list_for_transaction(transaction_id)
        ]

    b7.review.confirm(link_ids[0])

    with b7.factory() as unit:
        statuses = {
            link.id: link.status
            for link in unit.source_links.list_for_transaction(transaction_id)
        }
    assert statuses[link_ids[0]] is SourceLinkStatus.CONFIRMED
    assert statuses[link_ids[1]] is SourceLinkStatus.REJECTED


def test_a_second_confirmation_of_a_decided_link_is_refused(
    b7: B7Services, b7_world: B7World
) -> None:
    _manual_card_expense(b7, b7_world, merchant="另一家咖啡")
    transaction_id = _seed_notice(b7, b7_world)
    _stage(b7, transaction_id)
    with b7.factory() as unit:
        link_id = unit.source_links.list_for_transaction(transaction_id)[0].id
    b7.review.reject(link_id)

    with pytest.raises(SourceLinkError):
        b7.review.confirm(link_id)


def test_declaring_separate_creates_one_needs_allocation_event(
    b7: B7Services, b7_world: B7World
) -> None:
    _manual_card_expense(b7, b7_world, merchant="另一家咖啡")
    transaction_id = _seed_notice(b7, b7_world)
    _stage(b7, transaction_id)

    result = b7.review.declare_separate(transaction_id)

    assert result.staged.event_id is not None
    assert len(result.rejected_link_ids) == 1
    with b7.factory() as unit:
        event = unit.financial_events.get(result.staged.event_id)
        assert event is not None
        assert event.origin is FinancialEventOrigin.GMAIL_TRANSACTION
        assert event.status is FinancialEventStatus.NEEDS_ALLOCATION
        assert unit.journal.get_by_financial_event(event.id) is None
        statuses = [
            link.status
            for link in unit.source_links.list_for_transaction(transaction_id)
        ]
        assert SourceLinkStatus.CONFIRMED in statuses
        assert SourceLinkStatus.REJECTED in statuses
        confirmed = unit.source_links.get_confirmed_for_transaction(transaction_id)
        assert confirmed is not None
        assert SourceLinkReason.USER_DECLARED_SEPARATE in confirmed.reasons


def test_a_parser_rerun_never_rewrites_a_matched_manual_event(
    b7: B7Services, b7_world: B7World
) -> None:
    event_id = _manual_card_expense(b7, b7_world)
    transaction_id = _seed_notice(b7, b7_world)
    _stage(b7, transaction_id)

    with b7.factory() as unit:
        transaction = unit.transactions.get(transaction_id)
        assert transaction is not None
        email_id = transaction.email_id
        # A rerun re-parses the same row with a different amount.
        unit.transactions.reconcile(
            email_id,
            (
                TransactionDraft(
                    email_id=email_id,
                    source_key="row-1",
                    transaction_type=TransactionType.PURCHASE,
                    confidence=Decimal("0.95"),
                    parser_name="tests.fake",
                    parser_version="2",
                    merchant="改名的咖啡",
                    amount_minor=99_000,
                    currency="TWD",
                    card_last_four="4321",
                    transaction_at=B7_NOW,
                ),
            ),
        )
        b7.intake.refresh_for_email(unit, email_id)
        unit.commit()

    with b7.factory() as unit:
        event = unit.financial_events.get(event_id)
        assert event is not None
        assert event.amount_minor == 45_000
        assert event.merchant == MERCHANT
        assert len(unit.allocations.list_for_event(event_id)) == 1
        link = unit.source_links.get_confirmed_for_transaction(transaction_id)
        assert link is not None
        assert SourceLinkReason.AUTO_EXACT_MATCH in link.reasons


def test_a_backfill_run_behaves_like_a_direct_run(
    b7: B7Services, b7_world: B7World
) -> None:
    _manual_card_expense(b7, b7_world)
    transaction_id = _seed_notice(b7, b7_world)

    result = b7.intake.backfill(dry_run=False)

    assert result.matched_count == 1
    assert result.matched[0].transaction_id == transaction_id
    assert _events(b7) == 1


def test_a_dry_run_reports_the_decision_without_writing(
    b7: B7Services, b7_world: B7World
) -> None:
    _manual_card_expense(b7, b7_world, merchant="另一家咖啡")
    transaction_id = _seed_notice(b7, b7_world)

    result = b7.intake.backfill(dry_run=True)

    assert result.pending_count == 1
    with b7.factory() as unit:
        assert unit.source_links.list_for_transaction(transaction_id) == ()


def test_retrying_a_matched_transaction_never_creates_a_second_event(
    b7: B7Services, b7_world: B7World
) -> None:
    """The confirmed decision is what a later run reads (ADR-B062)."""
    event_id = _manual_card_expense(b7, b7_world)
    transaction_id = _seed_notice(b7, b7_world)
    first = _stage(b7, transaction_id)

    second = _stage(b7, transaction_id)

    assert first.matched_count == 1
    assert second.matched_count == 1
    assert second.matched[0].event_id == event_id
    assert second.staged_count == 0
    assert _events(b7) == 1
    with b7.factory() as unit:
        assert len(unit.source_links.list_for_transaction(transaction_id)) == 1

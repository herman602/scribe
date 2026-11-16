"""Deterministic source-matching and intake rule tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.domain.budget_enums import (
    CardConfigurationStatus,
    FinancialEventStatus,
    FinancialEventType,
    PaymentMethod,
    SourceLinkReason,
    SourceLinkStatus,
)
from app.domain.budget_matching import (
    MAX_SCORE_BPS,
    MatchObservation,
    candidate_link_draft,
    merchant_tokens,
    normalize_merchant,
    score_match,
)
from app.domain.budget_rules import (
    card_configuration_status,
    ensure_single_card_currency,
    event_accepts_source_refresh,
    event_type_for_transaction_type,
    payment_method_for_transaction,
)
from app.domain.enums import TransactionType
from app.domain.errors import CardConfigurationError

OCCURRED_AT = datetime(2026, 8, 7, 13, 30, tzinfo=UTC)


def _observation(**overrides: object) -> MatchObservation:
    values: dict[str, object] = {
        "amount_minor": 128000,
        "currency": "TWD",
        "event_type": FinancialEventType.PURCHASE,
        "occurred_at": OCCURRED_AT,
        "card_id": 1,
        "merchant": "Test Coffee",
    }
    values.update(overrides)
    return MatchObservation(**values)  # type: ignore[arg-type]


def test_merchant_normalization_is_unicode_and_punctuation_safe() -> None:
    fullwidth = "\uff34\uff45\uff53\uff54"  # NFKC folds this to "Test"
    assert normalize_merchant(f"  {fullwidth}  Coffee, Inc. ") == "test coffee inc"
    assert normalize_merchant("TEST coffee") == normalize_merchant("test COFFEE")
    assert normalize_merchant(None) == ""
    assert normalize_merchant("   ") == ""
    assert merchant_tokens("Test Coffee") == frozenset({"test", "coffee"})
    assert merchant_tokens(None) == frozenset()


def test_amount_currency_type_and_card_are_mandatory_gates() -> None:
    source = _observation()

    for override in (
        {"amount_minor": 128001},
        {"currency": "JPY"},
        {"event_type": FinancialEventType.REFUND},
        {"card_id": 2},
        {"occurred_at": OCCURRED_AT + timedelta(hours=73)},
    ):
        score = score_match(source, _observation(**override))
        assert not score.eligible
        assert score.score_bps == 0
        assert score.reasons == ()


def test_a_complete_agreement_reaches_the_auto_link_threshold() -> None:
    score = score_match(_observation(), _observation())

    assert score.eligible
    assert score.score_bps == MAX_SCORE_BPS
    assert score.meets_auto_link_threshold
    assert set(score.reasons) == {
        SourceLinkReason.EXACT_CARD,
        SourceLinkReason.COMPATIBLE_EVENT_TYPE,
        SourceLinkReason.TIME_WITHIN_24H,
        SourceLinkReason.EXACT_MERCHANT,
    }


def test_weaker_evidence_scores_lower_and_never_auto_links() -> None:
    distant = score_match(
        _observation(), _observation(occurred_at=OCCURRED_AT + timedelta(hours=48))
    )
    unknown_card = score_match(_observation(card_id=None), _observation(card_id=None))
    token_only = score_match(
        _observation(merchant="Test Coffee"),
        _observation(merchant="Test Coffee Taipei"),
    )
    no_merchant = score_match(
        _observation(merchant=None), _observation(merchant="Test Coffee")
    )
    unrelated = score_match(
        _observation(merchant="Test Coffee"), _observation(merchant="Other Bakery")
    )

    assert distant.score_bps == 8500
    assert SourceLinkReason.TIME_WITHIN_72H in distant.reasons
    assert unknown_card.score_bps == 7000
    assert SourceLinkReason.EXACT_CARD not in unknown_card.reasons
    assert token_only.score_bps == 9000
    assert SourceLinkReason.TOKEN_MERCHANT in token_only.reasons
    assert no_merchant.score_bps == 7500
    assert unrelated.score_bps == 7500
    for score in (distant, unknown_card, token_only, no_merchant, unrelated):
        assert score.eligible
        assert not score.meets_auto_link_threshold


def test_a_scored_comparison_can_only_ever_become_a_candidate() -> None:
    score = score_match(_observation(), _observation())

    draft = candidate_link_draft(financial_event_id=3, transaction_id=9, score=score)

    assert draft.status is SourceLinkStatus.CANDIDATE
    assert draft.match_score_bps == MAX_SCORE_BPS
    assert draft.reviewed_at is None
    with pytest.raises(ValueError, match="ineligible"):
        candidate_link_draft(
            financial_event_id=3,
            transaction_id=9,
            score=score_match(_observation(), _observation(currency="JPY")),
        )


def test_transaction_types_map_without_inventing_an_adjustment() -> None:
    assert (
        event_type_for_transaction_type(TransactionType.PURCHASE)
        is FinancialEventType.PURCHASE
    )
    assert (
        event_type_for_transaction_type(TransactionType.REFUND)
        is FinancialEventType.REFUND
    )
    assert event_type_for_transaction_type(TransactionType.UNKNOWN) is None


def test_payment_method_uses_only_observed_identifiers() -> None:
    assert (
        payment_method_for_transaction(card_last_four="4321", account_last_four=None)
        is PaymentMethod.CREDIT_CARD
    )
    assert (
        payment_method_for_transaction(card_last_four=None, account_last_four="8765")
        is PaymentMethod.BANK_ACCOUNT
    )
    assert (
        payment_method_for_transaction(card_last_four=None, account_last_four=None)
        is PaymentMethod.OTHER
    )


def test_only_unposted_events_accept_a_parser_refresh() -> None:
    for status in (
        FinancialEventStatus.DRAFT,
        FinancialEventStatus.NEEDS_ALLOCATION,
        FinancialEventStatus.READY,
    ):
        assert event_accepts_source_refresh(status)
    for status in (
        FinancialEventStatus.POSTED,
        FinancialEventStatus.REVERSED,
        FinancialEventStatus.IGNORED,
    ):
        assert not event_accepts_source_refresh(status)


def test_card_readiness_requires_both_settlement_sides() -> None:
    assert (
        card_configuration_status(liability_account_id=1, settlement_account_id=2)
        is CardConfigurationStatus.READY
    )
    for liability, settlement in ((1, None), (None, 2), (None, None)):
        assert (
            card_configuration_status(
                liability_account_id=liability, settlement_account_id=settlement
            )
            is CardConfigurationStatus.INCOMPLETE
        )


def test_a_card_cannot_mix_currencies() -> None:
    ensure_single_card_currency(())
    ensure_single_card_currency(("TWD", "TWD", "TWD"))

    with pytest.raises(CardConfigurationError, match="one currency"):
        ensure_single_card_currency(("TWD", "JPY"))

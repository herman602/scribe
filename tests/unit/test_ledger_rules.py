"""Framework-independent ledger invariant tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from app.domain.budget_enums import (
    CommitmentFulfillmentMode,
    FinancialAccountType,
    JournalEntryType,
    LedgerAccountKind,
)
from app.domain.budget_models import (
    BudgetTargetDraft,
    FundingCommitmentDraft,
    JournalPosting,
    LedgerAccount,
    Money,
    OpeningWalletSplit,
    PostingDraft,
    normalize_currency,
    normalize_masked_reference,
    normalize_name,
    validate_minor_units,
)
from app.domain.budget_rules import (
    currency_totals,
    distribute_proportionally,
    draft_signatures,
    ensure_allocation_total,
    ensure_balanced,
    ensure_exact_reversal,
    ensure_expected_wallet_balance,
    ensure_reversal_shape,
    ensure_same_account_reallocation,
    ensure_single_allocation_currency,
    expense_ledger_account_code,
    fulfillment_mode_for,
    holds_wallets,
    is_wallet_managed,
    ledger_kind_for_account_type,
    negated_postings,
    opening_equity_code,
    opening_posting_amount,
    physical_ledger_account_code,
    posted_signatures,
    resolve_display_arrangement,
    wallet_reallocation_delta_minor,
    wallet_reallocation_description,
)
from app.domain.errors import (
    CurrencyMismatchError,
    DisplayOrderingError,
    IncompleteAllocationError,
    InvalidReversalError,
    UnbalancedJournalError,
    WalletReallocationError,
)


def _ledger_account(
    *,
    kind: LedgerAccountKind,
    financial_account_id: int | None,
) -> LedgerAccount:
    moment = datetime(2026, 8, 7, 13, 0, tzinfo=UTC)
    return LedgerAccount(
        id=1,
        code="asset:test_bank:1",
        name="Test account",
        kind=kind,
        currency="TWD",
        financial_account_id=financial_account_id,
        budget_category_id=None,
        created_at=moment,
        updated_at=moment,
    )


def _posting(amount_minor: int, currency: str = "TWD") -> PostingDraft:
    return PostingDraft(
        ledger_account_id=1,
        amount=Money(amount_minor, currency),
        wallet_id=None,
    )


def test_money_rejects_float_and_boolean_amounts() -> None:
    with pytest.raises(TypeError):
        Money(12.99, "USD")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Money(True, "TWD")


def test_money_normalizes_currency_and_negates_exactly() -> None:
    amount = Money(128000, " twd ")

    assert amount.currency == "TWD"
    assert amount.negated() == Money(-128000, "TWD")
    assert amount.negated().negated() == amount


def test_opening_wallet_split_accepts_signed_integers_only() -> None:
    assert OpeningWalletSplit("透支錢包", -50_000).amount_minor == -50_000
    assert OpeningWalletSplit("空錢包", 0).amount_minor == 0

    for invalid in (True, 100.0, "100"):
        with pytest.raises(TypeError, match="integer minor units"):
            OpeningWalletSplit("不合法", invalid)  # type: ignore[arg-type]


def test_currency_code_must_be_three_letters() -> None:
    assert normalize_currency("usd") == "USD"
    for value in ("TW", "TWDD", "12D", ""):
        with pytest.raises(ValueError, match="ISO 4217"):
            normalize_currency(value)


def test_masked_reference_rejects_full_account_numbers() -> None:
    assert normalize_masked_reference("****1234") == "****1234"
    assert normalize_masked_reference("  ") is None
    assert normalize_masked_reference(None) is None
    with pytest.raises(ValueError, match="full account number"):
        normalize_masked_reference("12345678901234")
    with pytest.raises(ValueError, match="too long"):
        normalize_masked_reference("*" * 40)


def test_name_normalization_is_bounded_and_non_empty() -> None:
    assert normalize_name("  Rent  ", label="wallet name", max_length=10) == "Rent"
    with pytest.raises(ValueError, match="must not be empty"):
        normalize_name("   ", label="wallet name", max_length=10)
    with pytest.raises(ValueError, match="at most"):
        normalize_name("x" * 11, label="wallet name", max_length=10)


def test_two_currency_draft_is_rejected_unless_each_currency_balances() -> None:
    unbalanced = (
        _posting(1000),
        _posting(-1000),
        _posting(500, "JPY"),
        _posting(-400, "JPY"),
    )

    with pytest.raises(UnbalancedJournalError, match="JPY"):
        ensure_balanced(unbalanced)

    ensure_balanced(
        (
            _posting(1000),
            _posting(-1000),
            _posting(500, "JPY"),
            _posting(-500, "JPY"),
        )
    )


def test_currency_totals_never_mix_currencies() -> None:
    totals = currency_totals((_posting(1000), _posting(700, "JPY")))

    assert totals == {"TWD": 1000, "JPY": 700}


def test_zero_amount_and_single_sided_entries_are_rejected() -> None:
    with pytest.raises(UnbalancedJournalError, match="nonzero"):
        ensure_balanced((_posting(0), _posting(0)))
    with pytest.raises(UnbalancedJournalError, match="at least two postings"):
        ensure_balanced(())
    with pytest.raises(UnbalancedJournalError, match="at least two postings"):
        ensure_balanced((_posting(1000),))
    with pytest.raises(UnbalancedJournalError, match="balance in every currency"):
        ensure_balanced((_posting(1000), _posting(-900)))


def test_negated_postings_preserve_account_and_wallet() -> None:
    original = (
        PostingDraft(ledger_account_id=7, amount=Money(2500, "TWD"), wallet_id=3),
        PostingDraft(ledger_account_id=9, amount=Money(-2500, "TWD")),
    )

    reversed_postings = negated_postings(original)

    assert reversed_postings[0].wallet_id == 3
    assert reversed_postings[0].amount == Money(-2500, "TWD")
    assert reversed_postings[1].amount == Money(2500, "TWD")
    ensure_balanced(reversed_postings)


def test_account_type_maps_to_ledger_kind_and_wallet_support() -> None:
    assert (
        ledger_kind_for_account_type(FinancialAccountType.CARD_LIABILITY)
        is LedgerAccountKind.LIABILITY
    )
    assert not holds_wallets(FinancialAccountType.CARD_LIABILITY)
    for account_type in (
        FinancialAccountType.CHECKING,
        FinancialAccountType.SAVINGS,
        FinancialAccountType.CASH,
        FinancialAccountType.OTHER,
    ):
        assert ledger_kind_for_account_type(account_type) is LedgerAccountKind.ASSET
        assert holds_wallets(account_type)


def test_only_physical_asset_ledger_accounts_are_wallet_managed() -> None:
    assert is_wallet_managed(
        _ledger_account(kind=LedgerAccountKind.ASSET, financial_account_id=1)
    )
    assert not is_wallet_managed(
        _ledger_account(kind=LedgerAccountKind.ASSET, financial_account_id=None)
    )
    assert not is_wallet_managed(
        _ledger_account(kind=LedgerAccountKind.LIABILITY, financial_account_id=1)
    )


def test_ledger_codes_are_deterministic() -> None:
    assert (
        physical_ledger_account_code(LedgerAccountKind.ASSET, "test_bank", 4)
        == "asset:test_bank:4"
    )
    assert opening_equity_code("TWD") == "equity:opening:TWD"


def test_opening_position_sides_depend_on_ledger_kind() -> None:
    asset_side, asset_equity = opening_posting_amount(
        LedgerAccountKind.ASSET, Money(500000, "TWD")
    )
    liability_side, liability_equity = opening_posting_amount(
        LedgerAccountKind.LIABILITY, Money(500000, "TWD")
    )

    assert asset_side == Money(500000, "TWD")
    assert asset_equity == Money(-500000, "TWD")
    assert liability_side == Money(-500000, "TWD")
    assert liability_equity == Money(500000, "TWD")


def test_opening_position_requires_a_positive_balance() -> None:
    for amount in (0, -1):
        with pytest.raises(ValueError, match="positive balance"):
            opening_posting_amount(LedgerAccountKind.ASSET, Money(amount, "TWD"))


def _posted(
    amount_minor: int,
    *,
    ledger_account_id: int = 1,
    wallet_id: int | None = None,
    currency: str = "TWD",
    budget_category_id: int | None = None,
) -> JournalPosting:
    moment = datetime(2026, 8, 7, 13, 0, tzinfo=UTC)
    return JournalPosting(
        id=0,
        journal_entry_id=1,
        ledger_account_id=ledger_account_id,
        wallet_id=wallet_id,
        amount_minor=amount_minor,
        currency=currency,
        budget_category_id=budget_category_id,
        created_at=moment,
        updated_at=moment,
    )


def test_posting_signatures_ignore_order_and_separate_wallets() -> None:
    unordered = (
        PostingDraft(ledger_account_id=1, amount=Money(-1000, "TWD"), wallet_id=2),
        PostingDraft(ledger_account_id=1, amount=Money(1000, "TWD"), wallet_id=3),
    )

    assert draft_signatures(unordered) == draft_signatures(tuple(reversed(unordered)))
    assert draft_signatures(
        (PostingDraft(ledger_account_id=1, amount=Money(1000, "TWD")),)
    ) != draft_signatures(
        (PostingDraft(ledger_account_id=1, amount=Money(1000, "TWD"), wallet_id=1),)
    )


def test_posted_signatures_can_be_negated_for_comparison() -> None:
    posted = (_posted(1000, wallet_id=2), _posted(-1000, wallet_id=3))

    assert posted_signatures(posted, negated=True) == draft_signatures(
        (
            PostingDraft(ledger_account_id=1, amount=Money(-1000, "TWD"), wallet_id=2),
            PostingDraft(ledger_account_id=1, amount=Money(1000, "TWD"), wallet_id=3),
        )
    )
    assert posted_signatures(posted) != posted_signatures(posted, negated=True)


def test_reversal_shape_pairs_entry_type_with_its_target() -> None:
    ensure_reversal_shape(JournalEntryType.REVERSAL, 7)
    ensure_reversal_shape(JournalEntryType.ADJUSTMENT, None)

    with pytest.raises(InvalidReversalError, match="must name the entry it reverses"):
        ensure_reversal_shape(JournalEntryType.REVERSAL, None)
    with pytest.raises(InvalidReversalError, match="only a reversal entry"):
        ensure_reversal_shape(JournalEntryType.ADJUSTMENT, 7)
    with pytest.raises(InvalidReversalError, match="only a reversal entry"):
        ensure_reversal_shape(JournalEntryType.OPENING_BALANCE, 7)


def test_a_reversal_must_post_the_exact_opposite() -> None:
    original = (
        _posted(500000, ledger_account_id=1, wallet_id=2),
        _posted(-500000, ledger_account_id=9),
    )

    ensure_exact_reversal(original, negated_postings(_drafts_from(original)))

    for wrong in (
        (
            PostingDraft(
                ledger_account_id=1, amount=Money(-400000, "TWD"), wallet_id=2
            ),
            PostingDraft(ledger_account_id=9, amount=Money(400000, "TWD")),
        ),
        (
            PostingDraft(
                ledger_account_id=1, amount=Money(-500000, "TWD"), wallet_id=3
            ),
            PostingDraft(ledger_account_id=9, amount=Money(500000, "TWD")),
        ),
        (
            PostingDraft(ledger_account_id=1, amount=Money(-500000, "TWD")),
            PostingDraft(ledger_account_id=9, amount=Money(500000, "TWD")),
        ),
        (
            PostingDraft(ledger_account_id=1, amount=Money(500000, "TWD"), wallet_id=2),
            PostingDraft(ledger_account_id=9, amount=Money(-500000, "TWD")),
        ),
    ):
        with pytest.raises(InvalidReversalError, match="exact opposite"):
            ensure_exact_reversal(original, wrong)


def _drafts_from(postings: tuple[JournalPosting, ...]) -> tuple[PostingDraft, ...]:
    return tuple(
        PostingDraft(
            ledger_account_id=posting.ledger_account_id,
            amount=Money(posting.amount_minor, posting.currency),
            wallet_id=posting.wallet_id,
        )
        for posting in postings
    )


def test_fulfillment_mode_uses_account_identity_not_bank_names() -> None:
    same_account = fulfillment_mode_for(
        funding_account_id=1,
        settlement_account_id=1,
        funding_institution_code="test_bank",
        settlement_institution_code="test_bank",
    )
    same_bank = fulfillment_mode_for(
        funding_account_id=1,
        settlement_account_id=2,
        funding_institution_code="test_bank",
        settlement_institution_code="test_bank",
    )
    other_bank = fulfillment_mode_for(
        funding_account_id=1,
        settlement_account_id=2,
        funding_institution_code="test_bank",
        settlement_institution_code="other_bank",
    )
    # Two accounts at banks that merely share a display name are still two
    # accounts; identity decides, so this must not collapse to reserve_only.
    assert same_account is CommitmentFulfillmentMode.RESERVE_ONLY
    assert same_bank is CommitmentFulfillmentMode.ACCOUNT_TRANSFER
    assert other_bank is CommitmentFulfillmentMode.CROSS_BANK_TRANSFER


def test_allocations_must_total_the_event_exactly() -> None:
    ensure_allocation_total(event_amount_minor=128000, allocated_minor=128000)

    for allocated in (127999, 128001):
        with pytest.raises(IncompleteAllocationError, match="total exactly"):
            ensure_allocation_total(
                event_amount_minor=128000, allocated_minor=allocated
            )


def test_every_allocation_uses_the_event_currency() -> None:
    ensure_single_allocation_currency(event_currency="TWD", currencies=())
    ensure_single_allocation_currency(event_currency="TWD", currencies=("TWD", "TWD"))

    with pytest.raises(CurrencyMismatchError, match="event's currency"):
        ensure_single_allocation_currency(
            event_currency="TWD", currencies=("TWD", "JPY")
        )


def test_proportional_distribution_is_exact_and_deterministic() -> None:
    halves = distribute_proportionally(100, (500, 500))
    thirds = distribute_proportionally(100, (100, 100, 100))
    weighted = distribute_proportionally(1000, (7000, 3000))
    uneven = distribute_proportionally(10, (100, 100, 100, 100, 100, 100))

    assert halves == (50, 50)
    assert thirds == (34, 33, 33)
    assert sum(thirds) == 100
    assert weighted == (700, 300)
    assert uneven == (2, 2, 2, 2, 1, 1)
    assert sum(uneven) == 10
    # The remainder must not depend on call ordering.
    assert distribute_proportionally(100, (100, 100, 100)) == thirds


def test_distribution_rejects_impossible_requests() -> None:
    for amount in (0, -1):
        with pytest.raises(ValueError, match="must be positive"):
            distribute_proportionally(amount, (100,))
    with pytest.raises(ValueError, match="weights must all be positive"):
        distribute_proportionally(10, ())
    with pytest.raises(ValueError, match="weights must all be positive"):
        distribute_proportionally(10, (100, 0))
    # A refund can never exceed the allocations it is spread across.
    with pytest.raises(ValueError, match="more than the original"):
        distribute_proportionally(200, (100, 50))


def test_expense_ledger_codes_are_deterministic_per_category_and_currency() -> None:
    assert expense_ledger_account_code(7, "TWD") == "expense:category:7:TWD"
    assert expense_ledger_account_code(7, "JPY") != expense_ledger_account_code(
        7, "TWD"
    )


def test_every_new_money_input_rejects_float_and_bool() -> None:
    """Minor units are integers everywhere; bool is an int subclass trap."""
    for value in (12.5, True, "1000"):
        with pytest.raises(TypeError, match="integer minor units"):
            validate_minor_units(value, label="budget target")  # type: ignore[arg-type]

    assert validate_minor_units(0, label="budget target") == 0
    assert validate_minor_units(1000, label="budget target") == 1000
    with pytest.raises(ValueError, match="must not be negative"):
        validate_minor_units(-1, label="budget target")
    with pytest.raises(ValueError, match="at least 1 minor units"):
        validate_minor_units(0, label="commitment amount", minimum=1)


def test_budget_target_and_commitment_drafts_validate_their_amounts() -> None:
    for amount in (12.5, True):
        with pytest.raises(TypeError, match="integer minor units"):
            BudgetTargetDraft(
                budget_period_id=1,
                budget_category_id=1,
                target_minor=amount,  # type: ignore[arg-type]
                buffer_minor=0,
                funding_wallet_id=1,
            )
        with pytest.raises(TypeError, match="integer minor units"):
            BudgetTargetDraft(
                budget_period_id=1,
                budget_category_id=1,
                target_minor=0,
                buffer_minor=amount,  # type: ignore[arg-type]
                funding_wallet_id=1,
            )
        with pytest.raises(TypeError, match="integer minor units"):
            FundingCommitmentDraft(
                financial_event_id=1,
                event_allocation_id=1,
                source_wallet_id=1,
                settlement_account_id=1,
                amount_minor=amount,  # type: ignore[arg-type]
                currency="TWD",
                fulfillment_mode=CommitmentFulfillmentMode.RESERVE_ONLY,
            )

    with pytest.raises(ValueError, match="must not be negative"):
        BudgetTargetDraft(
            budget_period_id=1,
            budget_category_id=1,
            target_minor=-1,
            buffer_minor=0,
            funding_wallet_id=1,
        )
    with pytest.raises(ValueError, match="at least 1 minor units"):
        FundingCommitmentDraft(
            financial_event_id=1,
            event_allocation_id=1,
            source_wallet_id=1,
            settlement_account_id=1,
            amount_minor=0,
            currency="TWD",
            fulfillment_mode=CommitmentFulfillmentMode.RESERVE_ONLY,
        )
    # currency is normalized at the domain boundary, like every other amount
    commitment = FundingCommitmentDraft(
        financial_event_id=1,
        event_allocation_id=1,
        source_wallet_id=1,
        settlement_account_id=1,
        amount_minor=1000,
        currency=" twd ",
        fulfillment_mode=CommitmentFulfillmentMode.RESERVE_ONLY,
    )
    assert commitment.currency == "TWD"


def test_posting_identity_and_reversal_carry_the_category() -> None:
    with_category = (
        PostingDraft(
            ledger_account_id=1, amount=Money(1000, "TWD"), budget_category_id=7
        ),
    )
    without_category = (PostingDraft(ledger_account_id=1, amount=Money(1000, "TWD")),)
    other_category = (
        PostingDraft(
            ledger_account_id=1, amount=Money(1000, "TWD"), budget_category_id=8
        ),
    )

    assert draft_signatures(with_category) != draft_signatures(without_category)
    assert draft_signatures(with_category) != draft_signatures(other_category)
    assert negated_postings(with_category)[0].budget_category_id == 7
    ensure_exact_reversal(
        (_posted(1000, ledger_account_id=1, budget_category_id=7),),
        negated_postings(with_category),
    )
    with pytest.raises(InvalidReversalError, match="exact opposite"):
        ensure_exact_reversal(
            (_posted(1000, ledger_account_id=1, budget_category_id=7),),
            negated_postings(without_category),
        )


def test_a_reallocation_stays_inside_one_account() -> None:
    ensure_same_account_reallocation(source_account_id=4, destination_account_id=4)

    with pytest.raises(WalletReallocationError, match="one financial account"):
        ensure_same_account_reallocation(source_account_id=4, destination_account_id=5)


def test_a_reallocation_refuses_a_balance_the_page_never_showed() -> None:
    ensure_expected_wallet_balance(expected_minor=-500, actual_minor=-500)

    with pytest.raises(WalletReallocationError, match="reload and retry"):
        ensure_expected_wallet_balance(expected_minor=300_000, actual_minor=290_000)


def test_a_reallocation_delta_is_signed_and_may_empty_an_envelope() -> None:
    assert (
        wallet_reallocation_delta_minor(expected_minor=300_000, target_minor=400_000)
        == 100_000
    )
    assert (
        wallet_reallocation_delta_minor(expected_minor=300_000, target_minor=-50_000)
        == -350_000
    )
    assert wallet_reallocation_delta_minor(expected_minor=0, target_minor=0) == 0


def test_a_reallocation_is_described_by_the_direction_money_moved() -> None:
    assert (
        wallet_reallocation_description(
            edited_wallet_name="飲食",
            counterpart_wallet_name="尚未分配",
            delta_minor=100_000,
        )
        == "重分配 尚未分配 - 飲食"
    )
    assert (
        wallet_reallocation_description(
            edited_wallet_name="飲食",
            counterpart_wallet_name="尚未分配",
            delta_minor=-100_000,
        )
        == "重分配 飲食 - 尚未分配"
    )


def test_an_arrangement_orders_every_item_on_its_axis() -> None:
    assert resolve_display_arrangement(
        item_ids=[7, 8, 9],
        requested_positions={7: 3, 8: 1, 9: 2},
    ) == (8, 9, 7)


def test_an_arrangement_breaks_a_repeated_position_by_current_order() -> None:
    assert resolve_display_arrangement(
        item_ids=[7, 8, 9],
        requested_positions={7: 2, 8: 1, 9: 1},
    ) == (8, 9, 7)


def test_an_arrangement_that_omits_or_invents_an_item_is_refused() -> None:
    with pytest.raises(DisplayOrderingError, match="exactly once"):
        resolve_display_arrangement(
            item_ids=[7, 8, 9], requested_positions={7: 1, 8: 2}
        )
    with pytest.raises(DisplayOrderingError, match="exactly once"):
        resolve_display_arrangement(
            item_ids=[7, 8], requested_positions={7: 1, 8: 2, 9: 3}
        )


def test_an_arrangement_rejects_a_position_that_is_not_a_whole_number() -> None:
    with pytest.raises(TypeError, match="whole number"):
        resolve_display_arrangement(item_ids=[7], requested_positions={7: True})

"""Pure tests for the B7 domain rules, keys, and value objects."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
from app.domain.budget_enums import (
    BankMovementKind,
    FinancialEventOrigin,
    FinancialEventStatus,
    FinancialEventType,
    JournalEntryType,
    PaymentMethod,
)
from app.domain.budget_models import (
    BalanceSnapshotDraft,
    normalize_note,
    validate_signed_minor_units,
)
from app.domain.budget_rules import (
    SPENDING_BANK_MOVEMENTS,
    balance_snapshot_key,
    bank_movement_entry_type,
    bank_movement_event_type,
    bank_movement_payment_method,
    ensure_allocatable_event_type,
    ensure_distinct_transfer_wallets,
    ensure_manual_expense_payment_method,
    ensure_reversible_event,
    external_clearing_ledger_code,
    interest_income_ledger_code,
    local_day_bounds,
    manual_expense_event_key,
    reconciliation_adjustment_event_key,
    reconciliation_difference_minor,
    reconciliation_equity_ledger_code,
)
from app.domain.errors import (
    BankMovementError,
    EventReversalError,
    ManualExpenseError,
    UnsupportedEventTypeError,
)

TAIPEI = ZoneInfo("Asia/Taipei")


def test_a_local_day_is_bounded_in_the_users_timezone() -> None:
    start, end = local_day_bounds(date(2026, 8, 9), TAIPEI)

    assert start == datetime(2026, 8, 8, 16, 0, tzinfo=UTC)
    assert end == datetime(2026, 8, 9, 16, 0, tzinfo=UTC)


def test_the_day_range_is_half_open_so_midnight_belongs_to_one_day() -> None:
    _, first_end = local_day_bounds(date(2026, 8, 9), TAIPEI)
    second_start, _ = local_day_bounds(date(2026, 8, 10), TAIPEI)

    assert first_end == second_start


@pytest.mark.parametrize(
    ("kind", "event_type", "entry_type"),
    [
        (
            BankMovementKind.INTEREST_INCOME,
            FinancialEventType.INTEREST_INCOME,
            JournalEntryType.INCOME,
        ),
        (
            BankMovementKind.EXTERNAL_DEPOSIT,
            FinancialEventType.DEPOSIT,
            JournalEntryType.EXTERNAL_MOVEMENT,
        ),
        (
            BankMovementKind.EXTERNAL_WITHDRAWAL,
            FinancialEventType.WITHDRAWAL,
            JournalEntryType.EXTERNAL_MOVEMENT,
        ),
        (
            BankMovementKind.CASH_WITHDRAWAL_SPENDING,
            FinancialEventType.WITHDRAWAL,
            JournalEntryType.EXPENSE,
        ),
        (
            BankMovementKind.DIRECT_DEBIT,
            FinancialEventType.DIRECT_DEBIT,
            JournalEntryType.EXPENSE,
        ),
        (BankMovementKind.FEE, FinancialEventType.FEE, JournalEntryType.EXPENSE),
        (
            BankMovementKind.BILL_PAYMENT,
            FinancialEventType.BILL_PAYMENT,
            JournalEntryType.EXPENSE,
        ),
    ],
)
def test_each_bank_movement_keeps_its_own_shape(
    kind: BankMovementKind,
    event_type: FinancialEventType,
    entry_type: JournalEntryType,
) -> None:
    assert bank_movement_event_type(kind) is event_type
    assert bank_movement_entry_type(kind) is entry_type
    assert entry_type is not JournalEntryType.ADJUSTMENT


def test_only_the_four_spending_movements_need_a_category() -> None:
    assert {
        BankMovementKind.DIRECT_DEBIT,
        BankMovementKind.FEE,
        BankMovementKind.BILL_PAYMENT,
        BankMovementKind.CASH_WITHDRAWAL_SPENDING,
    } == SPENDING_BANK_MOVEMENTS


def test_only_consumed_cash_withdrawal_is_reported_as_cash() -> None:
    assert (
        bank_movement_payment_method(BankMovementKind.CASH_WITHDRAWAL_SPENDING)
        is PaymentMethod.CASH
    )
    assert (
        bank_movement_payment_method(BankMovementKind.EXTERNAL_WITHDRAWAL)
        is PaymentMethod.BANK_ACCOUNT
    )


def test_only_a_cash_withdrawal_can_use_the_spending_allocation_rule() -> None:
    ensure_allocatable_event_type(FinancialEventType.WITHDRAWAL, PaymentMethod.CASH)

    with pytest.raises(UnsupportedEventTypeError):
        ensure_allocatable_event_type(
            FinancialEventType.WITHDRAWAL, PaymentMethod.BANK_ACCOUNT
        )


@pytest.mark.parametrize(
    "method",
    [PaymentMethod.BANK_ACCOUNT, PaymentMethod.TRANSFER, PaymentMethod.OTHER],
)
def test_a_manual_expense_is_cash_or_card_only(method: PaymentMethod) -> None:
    with pytest.raises(ManualExpenseError):
        ensure_manual_expense_payment_method(method)


def test_cash_and_card_are_accepted() -> None:
    ensure_manual_expense_payment_method(PaymentMethod.CASH)
    ensure_manual_expense_payment_method(PaymentMethod.CREDIT_CARD)


@pytest.mark.parametrize(
    "status",
    [
        FinancialEventStatus.DRAFT,
        FinancialEventStatus.NEEDS_ALLOCATION,
        FinancialEventStatus.READY,
        FinancialEventStatus.REVERSED,
        FinancialEventStatus.IGNORED,
    ],
)
def test_only_a_posted_event_may_be_reversed(status: FinancialEventStatus) -> None:
    with pytest.raises(EventReversalError):
        ensure_reversible_event(origin=FinancialEventOrigin.MANUAL, status=status)


def test_a_gmail_event_is_never_reversed_here() -> None:
    with pytest.raises(EventReversalError):
        ensure_reversible_event(
            origin=FinancialEventOrigin.GMAIL_TRANSACTION,
            status=FinancialEventStatus.POSTED,
        )


def test_a_posted_manual_event_is_reversible() -> None:
    ensure_reversible_event(
        origin=FinancialEventOrigin.MANUAL, status=FinancialEventStatus.POSTED
    )


def test_a_transfer_needs_two_different_wallets() -> None:
    ensure_distinct_transfer_wallets(source_wallet_id=1, destination_wallet_id=2)
    with pytest.raises(BankMovementError):
        ensure_distinct_transfer_wallets(source_wallet_id=1, destination_wallet_id=1)


def test_the_difference_is_observed_minus_ledger_and_keeps_its_sign() -> None:
    assert (
        reconciliation_difference_minor(
            observed_balance_minor=1_050_000, ledger_balance_minor=1_000_000
        )
        == 50_000
    )
    assert (
        reconciliation_difference_minor(
            observed_balance_minor=940_000, ledger_balance_minor=1_000_000
        )
        == -60_000
    )
    assert (
        reconciliation_difference_minor(
            observed_balance_minor=0, ledger_balance_minor=0
        )
        == 0
    )


def test_ledger_codes_are_deterministic_per_currency() -> None:
    assert interest_income_ledger_code("TWD") == "income:interest:TWD"
    assert external_clearing_ledger_code("JPY") == "clearing:external:JPY"
    assert reconciliation_equity_ledger_code("USD") == "equity:reconciliation:USD"


def test_idempotency_keys_require_a_token() -> None:
    assert manual_expense_event_key("abc") == "manual_expense:abc"
    assert balance_snapshot_key("abc") == "balance_snapshot:abc"
    assert (
        reconciliation_adjustment_event_key(7, "abc")
        == "reconciliation_adjustment:7:abc"
    )
    for builder in (manual_expense_event_key, balance_snapshot_key):
        with pytest.raises(ValueError):
            builder("   ")
    with pytest.raises(ValueError):
        reconciliation_adjustment_event_key(7, "   ")


def test_an_adjustment_key_names_the_snapshot_it_settles() -> None:
    """Two observations must never share one adjustment token (ADR-B067)."""
    assert reconciliation_adjustment_event_key(
        1, "token"
    ) != reconciliation_adjustment_event_key(2, "token")
    for snapshot_id in (0, -1, True):
        with pytest.raises((TypeError, ValueError)):
            reconciliation_adjustment_event_key(snapshot_id, "token")


@pytest.mark.parametrize("value", [True, False, 1.5, "5"])
def test_a_signed_balance_still_rejects_bool_and_float(value: object) -> None:
    with pytest.raises(TypeError):
        validate_signed_minor_units(value, label="observed balance")  # type: ignore[arg-type]


def test_a_signed_balance_accepts_zero_and_negatives() -> None:
    assert validate_signed_minor_units(0, label="observed balance") == 0
    assert validate_signed_minor_units(-5, label="observed balance") == -5


def test_a_note_is_trimmed_bounded_and_optional() -> None:
    assert normalize_note("  hi  ", label="note", max_length=10) == "hi"
    assert normalize_note("   ", label="note", max_length=10) is None
    assert normalize_note(None, label="note", max_length=10) is None
    with pytest.raises(ValueError):
        normalize_note("x" * 11, label="note", max_length=10)


def test_a_snapshot_draft_refuses_a_naive_instant_and_an_empty_token() -> None:
    with pytest.raises(ValueError):
        BalanceSnapshotDraft(
            financial_account_id=1,
            observed_balance_minor=0,
            observed_at=datetime(2026, 8, 9, 12, 0),
            idempotency_key="token",
        )
    with pytest.raises(ValueError):
        BalanceSnapshotDraft(
            financial_account_id=1,
            observed_balance_minor=0,
            observed_at=datetime(2026, 8, 9, 12, 0, tzinfo=UTC),
            idempotency_key="  ",
        )

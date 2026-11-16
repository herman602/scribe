"""Ledger invariants that never depend on a framework or database."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.domain.budget_enums import (
    BankMovementKind,
    CardConfigurationStatus,
    CommitmentFulfillmentMode,
    CommitmentStatus,
    FinancialAccountType,
    FinancialEventOrigin,
    FinancialEventStatus,
    FinancialEventType,
    JournalEntryType,
    LedgerAccountKind,
    PaymentMethod,
    RecognitionBasis,
    SalaryRunItemStatus,
    SalaryRunStatus,
    ScheduledExecutionPolicy,
    ScheduledTransferPurpose,
    ScheduledTransferStatus,
    SubscriptionCadence,
    SubscriptionRequirementReason,
)
from app.domain.budget_models import (
    SALARY_EVIDENCE_SCHEMA_VERSION,
    FixedSalaryItemEvidence,
    JournalPosting,
    LedgerAccount,
    ManualSalaryItemEvidence,
    Money,
    PostingDraft,
    SalaryItemEvidence,
    SalarySubscriptionEvidence,
    Subscription,
    SubscriptionRequirementItem,
    SubscriptionSalaryItemEvidence,
    validate_calendar_date,
    validate_minor_units,
    validate_signed_minor_units,
    validate_wall_clock,
)
from app.domain.enums import TransactionType
from app.domain.errors import (
    AllocationRedoError,
    BankMovementError,
    BudgetPeriodError,
    CardConfigurationError,
    CurrencyMismatchError,
    DisplayOrderingError,
    EventReversalError,
    FinancialEventError,
    IncompleteAllocationError,
    InvalidReversalError,
    ManualExpenseError,
    SalaryAllocationError,
    SalaryEvidenceError,
    ScheduledTransferShapeError,
    ScheduledTransferStateError,
    ScheduledTransferTimeError,
    SubscriptionCadenceError,
    UnbalancedJournalError,
    UnsupportedEventTypeError,
    WalletReallocationError,
)

type PostingSignature = tuple[int, int, int, int, str]

_NO_WALLET = -1
_NO_CATEGORY = -1

#: Name given to the envelope every asset account is created with. It is
#: shown to the user beside their own wallet names, so it is written the way
#: the rest of the interface is; the purpose enum, never this string, is what
#: any logic tests.
UNALLOCATED_WALLET_NAME = "尚未分配"
OPENING_EQUITY_NAME = "Opening balance equity"

_ASSET_ACCOUNT_TYPES = frozenset(
    {
        FinancialAccountType.CHECKING,
        FinancialAccountType.SAVINGS,
        FinancialAccountType.CASH,
        FinancialAccountType.OTHER,
    }
)


def ledger_kind_for_account_type(
    account_type: FinancialAccountType,
) -> LedgerAccountKind:
    """Return the double-entry kind representing one physical account."""
    if account_type is FinancialAccountType.CARD_LIABILITY:
        return LedgerAccountKind.LIABILITY
    return LedgerAccountKind.ASSET


def holds_wallets(account_type: FinancialAccountType) -> bool:
    """Return whether an account type is divided into local wallets."""
    return account_type in _ASSET_ACCOUNT_TYPES


def is_wallet_managed(ledger_account: LedgerAccount) -> bool:
    """Return whether every posting to this ledger account needs a wallet."""
    return (
        ledger_account.financial_account_id is not None
        and ledger_account.kind is LedgerAccountKind.ASSET
    )


def physical_ledger_account_code(
    kind: LedgerAccountKind,
    institution_code: str,
    financial_account_id: int,
) -> str:
    """Return the deterministic code of one account's ledger identity."""
    return f"{kind.value}:{institution_code}:{financial_account_id}"


def opening_equity_code(currency: str) -> str:
    """Return the deterministic opening-equity code for one currency."""
    return f"equity:opening:{currency}"


def currency_totals(postings: Iterable[PostingDraft]) -> Mapping[str, int]:
    """Return the signed minor-unit total of each currency in a draft."""
    totals: dict[str, int] = {}
    for posting in postings:
        amount = posting.amount
        totals[amount.currency] = totals.get(amount.currency, 0) + amount.amount_minor
    return totals


def ensure_balanced(postings: tuple[PostingDraft, ...]) -> None:
    """Reject an entry whose postings do not net to zero in every currency."""
    if not postings:
        raise UnbalancedJournalError("a journal entry requires at least two postings")
    if any(posting.amount.amount_minor == 0 for posting in postings):
        raise UnbalancedJournalError("journal postings must have a nonzero amount")
    if len(postings) < 2:
        raise UnbalancedJournalError("a journal entry requires at least two postings")
    unbalanced = sorted(
        currency for currency, total in currency_totals(postings).items() if total != 0
    )
    if unbalanced:
        raise UnbalancedJournalError(
            "journal postings must balance in every currency: " + ", ".join(unbalanced)
        )


def negated_postings(postings: Iterable[PostingDraft]) -> tuple[PostingDraft, ...]:
    """Return exactly opposite postings used to reverse a posted entry."""
    return tuple(
        PostingDraft(
            ledger_account_id=posting.ledger_account_id,
            amount=posting.amount.negated(),
            wallet_id=posting.wallet_id,
            budget_category_id=posting.budget_category_id,
        )
        for posting in postings
    )


def draft_signatures(
    postings: Iterable[PostingDraft],
) -> tuple[PostingSignature, ...]:
    """Return one order-independent signature per requested posting."""
    return tuple(
        sorted(
            (
                posting.ledger_account_id,
                _NO_WALLET if posting.wallet_id is None else posting.wallet_id,
                _NO_CATEGORY
                if posting.budget_category_id is None
                else posting.budget_category_id,
                posting.amount.amount_minor,
                posting.amount.currency,
            )
            for posting in postings
        )
    )


def posted_signatures(
    postings: Iterable[JournalPosting],
    *,
    negated: bool = False,
) -> tuple[PostingSignature, ...]:
    """Return one order-independent signature per already posted line."""
    return tuple(
        sorted(
            (
                posting.ledger_account_id,
                _NO_WALLET if posting.wallet_id is None else posting.wallet_id,
                _NO_CATEGORY
                if posting.budget_category_id is None
                else posting.budget_category_id,
                -posting.amount_minor if negated else posting.amount_minor,
                posting.currency,
            )
            for posting in postings
        )
    )


def ensure_reversal_shape(
    entry_type: JournalEntryType, reversal_of_id: int | None
) -> None:
    """Reject an entry whose type and reversal link disagree."""
    if entry_type is JournalEntryType.REVERSAL and reversal_of_id is None:
        raise InvalidReversalError("a reversal must name the entry it reverses")
    if entry_type is not JournalEntryType.REVERSAL and reversal_of_id is not None:
        raise InvalidReversalError("only a reversal entry may name a reversed entry")


def ensure_exact_reversal(
    original: tuple[JournalPosting, ...], postings: tuple[PostingDraft, ...]
) -> None:
    """Reject a reversal whose postings are not the exact opposite."""
    if posted_signatures(original, negated=True) != draft_signatures(postings):
        raise InvalidReversalError(
            "a reversal must post the exact opposite of the reversed entry"
        )


def opening_posting_amount(
    kind: LedgerAccountKind, balance: Money
) -> tuple[Money, Money]:
    """Return the physical and opening-equity sides of an opening position.

    An asset opening debits the asset and credits opening equity; a liability
    opening credits the liability and debits opening equity. The service never
    inserts a one-sided balance.
    """
    if balance.amount_minor <= 0:
        raise ValueError("an opening position requires a positive balance")
    if kind is LedgerAccountKind.LIABILITY:
        return balance.negated(), balance
    return balance, balance.negated()


_EVENT_TYPE_BY_TRANSACTION_TYPE = {
    TransactionType.PURCHASE: FinancialEventType.PURCHASE,
    TransactionType.REFUND: FinancialEventType.REFUND,
    TransactionType.TRANSFER: FinancialEventType.TRANSFER,
    TransactionType.WITHDRAWAL: FinancialEventType.WITHDRAWAL,
    TransactionType.DEPOSIT: FinancialEventType.DEPOSIT,
    TransactionType.BILL_PAYMENT: FinancialEventType.BILL_PAYMENT,
    TransactionType.SUBSCRIPTION: FinancialEventType.SUBSCRIPTION,
    TransactionType.FEE: FinancialEventType.FEE,
}

_REFRESHABLE_EVENT_STATUSES = frozenset(
    {
        FinancialEventStatus.DRAFT,
        FinancialEventStatus.NEEDS_ALLOCATION,
        FinancialEventStatus.READY,
    }
)


def event_type_for_transaction_type(
    transaction_type: TransactionType,
) -> FinancialEventType | None:
    """Map a parsed transaction type, or return None when it is not decidable.

    ``unknown`` is deliberately unmapped. ``adjustment`` is reserved for a
    genuine reconciliation correction (ADR-B023) and is never invented here.
    """
    return _EVENT_TYPE_BY_TRANSACTION_TYPE.get(transaction_type)


def payment_method_for_transaction(
    *, card_last_four: str | None, account_last_four: str | None
) -> PaymentMethod:
    """Choose a payment method from the identifiers the parser actually saw."""
    if card_last_four is not None:
        return PaymentMethod.CREDIT_CARD
    if account_last_four is not None:
        return PaymentMethod.BANK_ACCOUNT
    return PaymentMethod.OTHER


def event_accepts_source_refresh(status: FinancialEventStatus) -> bool:
    """Return whether a parser rerun may still refresh this event's values.

    A posted, reversed, or ignored event is never changed automatically; the
    rerun records a discrepancy for review instead.
    """
    return status in _REFRESHABLE_EVENT_STATUSES


def card_configuration_status(
    *, liability_account_id: int | None, settlement_account_id: int | None
) -> CardConfigurationStatus:
    """Derive card readiness; it is never set directly by a user form."""
    if liability_account_id is not None and settlement_account_id is not None:
        return CardConfigurationStatus.READY
    return CardConfigurationStatus.INCOMPLETE


def ensure_single_card_currency(currencies: tuple[str, ...]) -> None:
    """Reject a card whose configured accounts do not share one currency."""
    distinct = {currency for currency in currencies}
    if len(distinct) > 1:
        raise CardConfigurationError(
            "a card's liability, settlement, and funding accounts must share "
            "one currency"
        )


EXPENSE_LEDGER_PREFIX = "expense"


def expense_ledger_account_code(budget_category_id: int, currency: str) -> str:
    """Return the deterministic code of one category's expense identity."""
    return f"{EXPENSE_LEDGER_PREFIX}:category:{budget_category_id}:{currency}"


def fulfillment_mode_for(
    *,
    funding_account_id: int,
    settlement_account_id: int,
    funding_institution_code: str,
    settlement_institution_code: str,
) -> CommitmentFulfillmentMode:
    """Classify a card commitment from account identity, never bank names."""
    if funding_account_id == settlement_account_id:
        return CommitmentFulfillmentMode.RESERVE_ONLY
    if funding_institution_code == settlement_institution_code:
        return CommitmentFulfillmentMode.ACCOUNT_TRANSFER
    return CommitmentFulfillmentMode.CROSS_BANK_TRANSFER


#: Event types B3 knows how to post. Everything else moves money without
#: being spending, so it needs its own posting rule before it can be allocated
#: — a transfer or a deposit recorded as an expense would silently corrupt both
#: the category totals and the wallet it was taken from.
SPENDING_EVENT_TYPES = frozenset(
    {
        FinancialEventType.PURCHASE,
        FinancialEventType.FEE,
        FinancialEventType.SUBSCRIPTION,
        FinancialEventType.BILL_PAYMENT,
        FinancialEventType.DIRECT_DEBIT,
    }
)
REFUND_EVENT_TYPES = frozenset({FinancialEventType.REFUND})
ALLOCATABLE_EVENT_TYPES = SPENDING_EVENT_TYPES | REFUND_EVENT_TYPES


def ensure_allocatable_event_type(
    event_type: FinancialEventType, payment_method: PaymentMethod
) -> None:
    """Refuse to post an event whose economic direction is ambiguous.

    A manual cash withdrawal is the one deliberate exception to the ordinary
    spending type allow-list: the user treats all withdrawn cash as consumed
    immediately, so it debits expense and credits the named bank wallet. A
    bank-account withdrawal remains an external movement and cannot be quietly
    reinterpreted as spending.
    """
    if (
        event_type is FinancialEventType.WITHDRAWAL
        and payment_method is PaymentMethod.CASH
    ):
        return
    if event_type not in ALLOCATABLE_EVENT_TYPES:
        raise UnsupportedEventTypeError(
            f"a {event_type.value} event needs its own posting rule "
            "and cannot be allocated as spending"
        )


def ensure_refund_within_original(
    *, refund_minor: int, already_refunded_minor: int, original_minor: int
) -> None:
    """Keep every refund of one expense inside the expense that was made.

    Checking a single refund against the original is not enough: two partial
    refunds can each pass that test and together reverse more than was ever
    spent, leaving the category and the card liability over-credited.
    """
    if already_refunded_minor + refund_minor > original_minor:
        raise FinancialEventError(
            "refunds of one event cannot exceed the event they refund"
        )


def ensure_allocation_total(*, event_amount_minor: int, allocated_minor: int) -> None:
    """Reject an allocation set that does not cover its event exactly."""
    if allocated_minor != event_amount_minor:
        raise IncompleteAllocationError(
            "allocations must total exactly the event amount"
        )


def ensure_single_allocation_currency(
    *, event_currency: str, currencies: tuple[str, ...]
) -> None:
    """Reject a split that mixes currencies or leaves the event's currency."""
    for currency in currencies:
        if currency != event_currency:
            raise CurrencyMismatchError(
                "every allocation must use the event's currency"
            )


def distribute_proportionally(
    total_minor: int, weights: tuple[int, ...]
) -> tuple[int, ...]:
    """Split one magnitude across weights with a deterministic remainder.

    ``weights`` are minor-unit amounts, such as the original allocations a
    partial refund is being spread across, so ``total_minor`` may never exceed
    their sum. Largest-remainder assignment keeps the parts exact: the shares
    always sum back to ``total_minor``, and ties resolve by original order so
    the result never depends on dictionary or row ordering.
    """
    if total_minor <= 0:
        raise ValueError("a distributed amount must be positive")
    if not weights or any(weight <= 0 for weight in weights):
        raise ValueError("distribution weights must all be positive")
    weight_total = sum(weights)
    if total_minor > weight_total:
        raise ValueError("cannot distribute more than the original amount")
    shares = [total_minor * weight // weight_total for weight in weights]
    remainder = total_minor - sum(shares)
    order = sorted(
        range(len(weights)),
        key=lambda index: (
            -((total_minor * weights[index]) % weight_total),
            index,
        ),
    )
    for position in order[:remainder]:
        shares[position] += 1
    return tuple(shares)


def parse_period_label(label: str) -> tuple[int, int]:
    """Return the year and month behind a ``yyyy-mm`` route or CLI label."""
    year_text, _, month_text = label.partition("-")
    if not year_text.isdigit() or not month_text.isdigit():
        raise BudgetPeriodError("a budget period is written as yyyy-mm")
    year, month = int(year_text), int(month_text)
    if len(year_text) != 4 or not 1 <= month <= 12:
        raise BudgetPeriodError("a budget period is written as yyyy-mm")
    return year, month


def period_bounds(
    year: int, month: int, timezone: ZoneInfo
) -> tuple[datetime, datetime]:
    """Return the UTC half-open range covering one local calendar month.

    Entries are stored in UTC but a month is a local idea, so the boundary is
    built in the user's timezone first. The range is half-open so a posting at
    midnight on the first belongs to exactly one month.
    """
    start = datetime(year, month, 1, tzinfo=timezone)
    end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=timezone)
    return start.astimezone(UTC), end.astimezone(UTC)


def month_start(year: int, month: int) -> date:
    """Return the first day of one calendar month.

    A subscription requirement is a whole-month idea, so every comparison in
    this module is made against this single point rather than against an
    arbitrary day inside the month.
    """
    if not 1 <= month <= 12:
        raise BudgetPeriodError("a budget period is written as yyyy-mm")
    return date(year, month, 1)


def cycle_position(
    *,
    year: int,
    month: int,
    cadence: SubscriptionCadence,
    cadence_months: int,
    billing_month: int | None,
    active_from: date | None,
) -> int:
    """Return which month of its billing cycle one calendar month is.

    The anchor is stored configuration, never the date the calculation
    happens to run on: a yearly cycle starts in its billing month, and a
    custom cycle starts in the month the subscription became active. Changing
    a price therefore cannot move the cycle.
    """
    if cadence is SubscriptionCadence.MONTHLY:
        return 0
    if cadence is SubscriptionCadence.YEARLY:
        if billing_month is None:
            raise SubscriptionCadenceError(
                "a yearly subscription needs its billing month as anchor"
            )
        return (month - billing_month) % cadence_months
    if active_from is None:
        raise SubscriptionCadenceError(
            "a custom cadence needs an active-from month as anchor"
        )
    elapsed = (year - active_from.year) * 12 + (month - active_from.month)
    return elapsed % cadence_months


def cycle_share_minor(*, amount_minor: int, cadence_months: int, position: int) -> int:
    """Return one month's share of a full cycle price, in exact minor units.

    The remainder is handed to the earliest positions of the cycle, one minor
    unit each, so the shares of a complete cycle always sum back to the cycle
    price. No float or Decimal rounding is involved at any point.
    """
    if cadence_months < 1:
        raise SubscriptionCadenceError("a billing cycle spans at least one month")
    if not 0 <= position < cadence_months:
        raise SubscriptionCadenceError("a cycle position lies inside its own cycle")
    if amount_minor < 0:
        raise ValueError("a subscription price must not be negative")
    base, remainder = divmod(amount_minor, cadence_months)
    return base + (1 if position < remainder else 0)


def cycle_month_shares(*, amount_minor: int, cadence_months: int) -> tuple[int, ...]:
    """Return every month's share of one complete cycle, in cycle order."""
    return tuple(
        cycle_share_minor(
            amount_minor=amount_minor, cadence_months=cadence_months, position=position
        )
        for position in range(cadence_months)
    )


def price_period_covers(
    *, effective_from: date, effective_to: date | None, selected_month: date
) -> bool:
    """Return whether one half-open ``[from, to)`` price covers a month."""
    return effective_from <= selected_month and (
        effective_to is None or selected_month < effective_to
    )


def effective_ranges_overlap(
    *,
    first_from: date,
    first_to: date | None,
    second_from: date,
    second_to: date | None,
) -> bool:
    """Return whether two half-open ranges claim any month in common.

    Adjacency is deliberately legal: ``[Jan, Apr)`` and ``[Apr, …)`` share no
    month, because the upper bound is excluded.
    """
    return (first_to is None or second_from < first_to) and (
        second_to is None or first_from < second_to
    )


#: Configuration a stored price is read through. Changing any of these
#: reinterprets every price period the subscription already has, so a month
#: that was calculated last year would silently produce a different amount,
#: in a different currency, category, or wallet.
FROZEN_CONFIGURATION_FIELDS = (
    "currency",
    "billing_cadence",
    "cadence_months",
    "billing_month",
    "budget_category_id",
    "funding_wallet_id",
)


def frozen_configuration_changes(
    current: Subscription,
    *,
    currency: str,
    billing_cadence: SubscriptionCadence,
    cadence_months: int,
    billing_month: int | None,
    budget_category_id: int,
    funding_wallet_id: int,
    active_from: date | None,
) -> tuple[str, ...]:
    """Return the history-rewriting fields one edit would change.

    A price period stores an amount and a range, not the configuration it was
    entered under, so these fields stay fixed once a price exists. ``name``,
    ``billing_day``, and the activity range remain correctable, because they
    never change how a stored amount is read. ``active_from`` joins the frozen
    set only for a custom cadence, where it is the cycle anchor and would move
    every month's position.
    """
    requested = {
        "currency": currency,
        "billing_cadence": billing_cadence,
        "cadence_months": cadence_months,
        "billing_month": billing_month,
        "budget_category_id": budget_category_id,
        "funding_wallet_id": funding_wallet_id,
    }
    changed = [
        field
        for field in FROZEN_CONFIGURATION_FIELDS
        if getattr(current, field) != requested[field]
    ]
    if (
        current.billing_cadence is SubscriptionCadence.CUSTOM
        and active_from != current.active_from
    ):
        changed.append("active_from")
    return tuple(changed)


def activity_ranges_overlap(
    *,
    first_from: date | None,
    first_to: date | None,
    second_from: date | None,
    second_to: date | None,
) -> bool:
    """Return whether two activity intervals claim any month in common.

    Both bounds are optional here, unlike a price range: a missing
    ``active_from`` means "since always" and a missing ``active_to`` means
    "still running". Adjacency stays legal, because the upper bound is
    excluded, so an interval ending in July and one starting in July never
    share a month.
    """
    first_ends_before_second = (
        first_to is not None and second_from is not None and first_to <= second_from
    )
    second_ends_before_first = (
        second_to is not None and first_from is not None and second_to <= first_from
    )
    return not first_ends_before_second and not second_ends_before_first


def month_activity_reason(
    *, active_from: date | None, active_to: date | None, selected_month: date
) -> SubscriptionRequirementReason | None:
    """Return why a month falls outside the activity range, or None.

    Deactivation is expressed as an ``active_to`` month rather than as a flag
    the calculation reads, so deactivating a subscription today can never
    rewrite what it required last year.
    """
    if active_from is not None and selected_month < active_from:
        return SubscriptionRequirementReason.NOT_YET_ACTIVE
    if active_to is not None and selected_month >= active_to:
        return SubscriptionRequirementReason.NO_LONGER_ACTIVE
    return None


def salary_fulfillment_mode_for(
    *,
    source_account_id: int,
    destination_account_id: int,
    source_institution_code: str,
    destination_institution_code: str,
) -> CommitmentFulfillmentMode:
    """Classify one salary line from account identity, never bank names.

    Two accounts at one institution are still two real accounts, so moving
    money between them is a transfer the user has to make. Only one and the
    same physical account can be rearranged by reclassifying wallets.
    """
    return fulfillment_mode_for(
        funding_account_id=source_account_id,
        settlement_account_id=destination_account_id,
        funding_institution_code=source_institution_code,
        settlement_institution_code=destination_institution_code,
    )


def moves_between_accounts(mode: CommitmentFulfillmentMode) -> bool:
    """Return whether a line needs a real bank transfer to be fulfilled."""
    return mode is not CommitmentFulfillmentMode.RESERVE_ONLY


def salary_planned_total(amounts: Iterable[int]) -> int:
    """Return the exact integer total one plan asks the salary to cover."""
    total = 0
    for amount in amounts:
        total += validate_minor_units(amount, label="salary item amount")
    return total


def salary_remainder_minor(
    *, salary_amount_minor: int, planned_amount_minor: int
) -> int:
    """Return what is left in the salary account after a plan is applied.

    A plan larger than the salary is refused rather than clipped: silently
    funding less than the plan says would make every destination wrong at once
    and leave no record of which line was cut.
    """
    salary = validate_minor_units(salary_amount_minor, label="salary amount", minimum=1)
    planned = validate_minor_units(planned_amount_minor, label="planned amount")
    if planned > salary:
        raise SalaryAllocationError(
            "the planned amount is larger than this salary; lower a line or "
            "choose another salary before generating the month"
        )
    return salary - planned


_RUN_TRANSITIONS: Mapping[SalaryRunStatus, frozenset[SalaryRunStatus]] = {
    SalaryRunStatus.DRAFT: frozenset(
        {
            SalaryRunStatus.APPROVED,
            SalaryRunStatus.PARTIALLY_FUNDED,
            SalaryRunStatus.FUNDED,
            SalaryRunStatus.CANCELLED,
        }
    ),
    SalaryRunStatus.APPROVED: frozenset(
        {SalaryRunStatus.PARTIALLY_FUNDED, SalaryRunStatus.FUNDED}
    ),
    SalaryRunStatus.PARTIALLY_FUNDED: frozenset({SalaryRunStatus.FUNDED}),
    SalaryRunStatus.FUNDED: frozenset(),
    SalaryRunStatus.CANCELLED: frozenset(),
}

_RUN_ITEM_TRANSITIONS: Mapping[SalaryRunItemStatus, frozenset[SalaryRunItemStatus]] = {
    SalaryRunItemStatus.DRAFT: frozenset(
        {
            SalaryRunItemStatus.NOT_REQUIRED,
            SalaryRunItemStatus.ALLOCATED,
            SalaryRunItemStatus.WAITING_TRANSFER,
        }
    ),
    SalaryRunItemStatus.NOT_REQUIRED: frozenset(),
    SalaryRunItemStatus.ALLOCATED: frozenset(),
    SalaryRunItemStatus.WAITING_TRANSFER: frozenset({SalaryRunItemStatus.TRANSFERRED}),
    SalaryRunItemStatus.TRANSFERRED: frozenset(),
}


def ensure_run_transition(current: SalaryRunStatus, target: SalaryRunStatus) -> None:
    """Reject a salary run status change the lifecycle does not allow."""
    if target not in _RUN_TRANSITIONS[current]:
        raise SalaryAllocationError(
            f"a {current.value} salary run cannot become {target.value}"
        )


def ensure_run_item_transition(
    current: SalaryRunItemStatus, target: SalaryRunItemStatus
) -> None:
    """Reject a salary item status change the lifecycle does not allow."""
    if target not in _RUN_ITEM_TRANSITIONS[current]:
        raise SalaryAllocationError(
            f"a {current.value} salary item cannot become {target.value}"
        )


def approved_item_status(
    *, amount_minor: int, mode: CommitmentFulfillmentMode
) -> SalaryRunItemStatus:
    """Decide what approval does to one line, from its amount and its path.

    A zero line still keeps its snapshot and evidence, but it must not create
    a posting or a reservation, so it settles as ``not_required``.
    """
    if amount_minor == 0:
        return SalaryRunItemStatus.NOT_REQUIRED
    if moves_between_accounts(mode):
        return SalaryRunItemStatus.WAITING_TRANSFER
    return SalaryRunItemStatus.ALLOCATED


def derive_run_status(
    statuses: Iterable[SalaryRunItemStatus],
) -> SalaryRunStatus:
    """Derive an approved run's status from what its lines have reached.

    A plan whose lines all stay inside the salary account is funded the moment
    it is approved, because nothing has to leave the bank. A plan with nothing
    but cross-account lines is only approved: no money has moved yet.
    """
    observed = tuple(statuses)
    waiting = any(status is SalaryRunItemStatus.WAITING_TRANSFER for status in observed)
    settled = any(
        status in {SalaryRunItemStatus.ALLOCATED, SalaryRunItemStatus.TRANSFERRED}
        for status in observed
    )
    if not waiting:
        return SalaryRunStatus.FUNDED
    return SalaryRunStatus.PARTIALLY_FUNDED if settled else SalaryRunStatus.APPROVED


SALARY_EVENT_ALREADY_USED = "already_used"
SALARY_EVENT_NOT_POSTED = "not_posted"
SALARY_EVENT_WRONG_TYPE = "wrong_event_type"
SALARY_EVENT_WRONG_CURRENCY = "wrong_currency"
SALARY_EVENT_NOT_POSITIVE = "not_a_positive_amount"
SALARY_EVENT_NO_ENTRY = "no_journal_entry"
SALARY_EVENT_WRONG_ENTRY_TYPE = "wrong_entry_type"
SALARY_EVENT_NO_MATCHING_DEPOSIT = "no_matching_deposit"
SALARY_EVENT_COUNTER_NOT_INCOME = "counter_side_is_not_income"

#: Entry types that move money without being income. An opening position, an
#: internal reclassification, a correction, or a reversal can all end up
#: increasing a bank account, so none of them may be mistaken for a salary.
_NON_INCOME_ENTRY_TYPES = frozenset(
    {
        JournalEntryType.OPENING_BALANCE,
        JournalEntryType.INTERNAL_TRANSFER,
        JournalEntryType.ADJUSTMENT,
        JournalEntryType.REVERSAL,
        JournalEntryType.FUNDING_TRANSFER,
        JournalEntryType.CARD_PAYMENT,
        JournalEntryType.BUDGET_CLOSE,
    }
)


@dataclass(frozen=True)
class SalaryPostingView:
    """One posted line, reduced to what deciding "is this a salary" needs."""

    ledger_account_id: int
    ledger_kind: LedgerAccountKind
    financial_account_id: int | None
    wallet_id: int | None
    amount_minor: int
    currency: str
    budget_category_id: int | None = None


def salary_event_problem(
    *,
    event_type: FinancialEventType,
    status: FinancialEventStatus,
    currency: str,
    amount_minor: int,
    expected_currency: str,
    source_account_id: int,
    source_wallet_id: int,
    entry_type: JournalEntryType | None,
    postings: tuple[SalaryPostingView, ...],
    already_used: bool,
) -> str | None:
    """Return the first stable reason this event cannot fund a run, or None.

    Structure decides, not wording: the event must have raised exactly the
    salary account's Unallocated wallet by exactly its own amount, against an
    income account and nothing else. A merchant name, an Email subject, or a
    bank display name is never consulted.
    """
    if already_used:
        return SALARY_EVENT_ALREADY_USED
    if status is not FinancialEventStatus.POSTED:
        return SALARY_EVENT_NOT_POSTED
    if event_type is not FinancialEventType.DEPOSIT:
        return SALARY_EVENT_WRONG_TYPE
    if currency != expected_currency:
        return SALARY_EVENT_WRONG_CURRENCY
    if amount_minor <= 0:
        return SALARY_EVENT_NOT_POSITIVE
    if entry_type is None:
        return SALARY_EVENT_NO_ENTRY
    if entry_type in _NON_INCOME_ENTRY_TYPES:
        return SALARY_EVENT_WRONG_ENTRY_TYPE
    if any(posting.currency != expected_currency for posting in postings):
        return SALARY_EVENT_WRONG_CURRENCY
    assets = tuple(
        posting
        for posting in postings
        if posting.ledger_kind is LedgerAccountKind.ASSET
    )
    if len(assets) != 1:
        return SALARY_EVENT_NO_MATCHING_DEPOSIT
    deposit = assets[0]
    if (
        deposit.financial_account_id != source_account_id
        or deposit.wallet_id != source_wallet_id
        or deposit.amount_minor != amount_minor
    ):
        return SALARY_EVENT_NO_MATCHING_DEPOSIT
    counter = tuple(posting for posting in postings if posting is not deposit)
    if not counter or any(
        posting.ledger_kind is not LedgerAccountKind.INCOME for posting in counter
    ):
        return SALARY_EVENT_COUNTER_NOT_INCOME
    if sum(posting.amount_minor for posting in counter) != -amount_minor:
        return SALARY_EVENT_COUNTER_NOT_INCOME
    return None


SALARY_RECEIPT_DIFFERENT_MONEY = "different_money"
SALARY_RECEIPT_NOT_A_SALARY_EVENT = "not_a_salary_event"
SALARY_RECEIPT_NO_ENTRY = "no_journal_entry"
SALARY_RECEIPT_WRONG_ENTRY_TYPE = "wrong_entry_type"
SALARY_RECEIPT_UNLINKED_ENTRY = "entry_belongs_to_another_event"
SALARY_RECEIPT_WRONG_EFFECTIVE_AT = "wrong_effective_time"
SALARY_RECEIPT_NO_INCOME_ACCOUNT = "no_salary_income_account"
SALARY_RECEIPT_WRONG_POSTINGS = "wrong_postings"

#: The exact shape `RecordSalaryReceipt` writes. A replay has to prove the
#: stored rows really are that, because an idempotency key is only a claim.
_SALARY_RECEIPT_IDENTITY = (
    FinancialEventOrigin.MANUAL,
    FinancialEventType.DEPOSIT,
    PaymentMethod.BANK_ACCOUNT,
    RecognitionBasis.OBSERVED_MANUAL,
    FinancialEventStatus.POSTED,
)


def salary_receipt_replay_problem(
    *,
    origin: FinancialEventOrigin,
    event_type: FinancialEventType,
    payment_method: PaymentMethod,
    recognition_basis: RecognitionBasis,
    status: FinancialEventStatus,
    currency: str,
    amount_minor: int,
    occurred_at: datetime,
    event_id: int,
    expected_currency: str,
    expected_amount_minor: int,
    expected_occurred_at: datetime,
    source_ledger_account_id: int,
    source_wallet_id: int,
    income_ledger_account_id: int | None,
    entry_type: JournalEntryType | None,
    entry_effective_at: datetime | None,
    entry_financial_event_id: int | None,
    postings: tuple[SalaryPostingView, ...],
) -> str | None:
    """Return why a stored receipt is not this request's, or None if it is.

    Matching an idempotency key proves only that something once used the token.
    Replaying on that alone would let any event of any type occupy a salary
    token and make the caller believe a salary was recorded, so the stored
    event *and* its entry are re-verified in full. Every amount compared here
    has already passed the domain's integer validator, so ``True`` and a float
    can never compare equal to a stored integer.
    """
    if (
        amount_minor != expected_amount_minor
        or currency != expected_currency
        or occurred_at != expected_occurred_at
    ):
        return SALARY_RECEIPT_DIFFERENT_MONEY
    if (
        origin,
        event_type,
        payment_method,
        recognition_basis,
        status,
    ) != _SALARY_RECEIPT_IDENTITY:
        return SALARY_RECEIPT_NOT_A_SALARY_EVENT
    if entry_type is None:
        return SALARY_RECEIPT_NO_ENTRY
    if entry_type is not JournalEntryType.SALARY_RECEIPT:
        return SALARY_RECEIPT_WRONG_ENTRY_TYPE
    if entry_financial_event_id != event_id:
        return SALARY_RECEIPT_UNLINKED_ENTRY
    if entry_effective_at != expected_occurred_at:
        return SALARY_RECEIPT_WRONG_EFFECTIVE_AT
    if income_ledger_account_id is None:
        return SALARY_RECEIPT_NO_INCOME_ACCOUNT
    if len(postings) != 2:
        return SALARY_RECEIPT_WRONG_POSTINGS
    deposit, income = postings
    if deposit.ledger_account_id != source_ledger_account_id:
        deposit, income = income, deposit
    if (
        deposit.ledger_account_id != source_ledger_account_id
        or deposit.wallet_id != source_wallet_id
        or deposit.amount_minor != expected_amount_minor
        or deposit.currency != expected_currency
        or deposit.budget_category_id is not None
    ):
        return SALARY_RECEIPT_WRONG_POSTINGS
    if (
        income.ledger_account_id != income_ledger_account_id
        or income.wallet_id is not None
        or income.amount_minor != -expected_amount_minor
        or income.currency != expected_currency
        or income.budget_category_id is not None
    ):
        return SALARY_RECEIPT_WRONG_POSTINGS
    return None


def salary_regeneration_fingerprint(
    *,
    previous_run_id: int,
    expected_version: int,
    manual_amounts: Iterable[tuple[int, int]],
) -> str:
    """Return the canonical fingerprint one regenerate request is identified by.

    Provenance alone says which draft a generation replaced; it cannot say
    whether *this* request is the one that produced it. The fingerprint covers
    everything the caller chose, so a resubmission is recognized and a
    different payload is a conflict instead of a silent replay (ADR-B049).
    """
    return _canonical(
        {
            "previous_run_id": previous_run_id,
            "expected_version": expected_version,
            "manual": sorted(
                [
                    [
                        validate_minor_units(item_id, label="template item id"),
                        validate_minor_units(amount, label="manual salary amount"),
                    ]
                    for item_id, amount in manual_amounts
                ]
            ),
        }
    )


def salary_income_ledger_code(currency: str) -> str:
    """Return the deterministic code of one currency's salary income identity."""
    return f"income:salary:{currency}"


def salary_receipt_event_key(client_token: str) -> str:
    """Return the stable event key one salary-receipt submission owns."""
    return f"salary_receipt:{client_token}"


def salary_receipt_journal_key(financial_event_id: int) -> str:
    """Return the stable journal key one recorded salary deposit owns."""
    return f"salary_receipt:financial_event:{financial_event_id}"


def salary_approval_journal_key(run_id: int) -> str:
    """Return the stable journal key one run's wallet reclassification owns."""
    return f"salary_allocation:run:{run_id}"


def salary_transfer_journal_key(run_item_id: int) -> str:
    """Return the stable journal key one confirmed bank transfer owns."""
    return f"salary_transfer:run_item:{run_item_id}"


def _evidence_int(payload: Mapping[str, object], key: str) -> int:
    """Read one integer out of a stored snapshot, refusing anything else.

    ``bool`` is rejected for the same reason money rejects it, and a float that
    survived a hand-edited row would otherwise reintroduce inexact money into a
    document whose whole purpose is to stay exact.
    """
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SalaryEvidenceError(f"{key} in a frozen snapshot must be an integer")
    return value


def _evidence_optional_int(payload: Mapping[str, object], key: str) -> int | None:
    value = payload.get(key)
    return None if value is None else _evidence_int(payload, key)


def _evidence_str(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise SalaryEvidenceError(f"{key} in a frozen snapshot must be text")
    return value


def _evidence_optional_str(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    return None if value is None else _evidence_str(payload, key)


def _evidence_bool(payload: Mapping[str, object], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise SalaryEvidenceError(f"{key} in a frozen snapshot must be true or false")
    return value


def _month_text(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def _month_value(payload: Mapping[str, object], key: str) -> date | None:
    text = _evidence_optional_str(payload, key)
    if text is None:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as error:
        raise SalaryEvidenceError(
            f"{key} in a frozen snapshot must be a calendar date"
        ) from error


def _canonical(payload: object) -> str:
    """Serialize one snapshot the same way every time it is written."""
    return json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )


def _decoded(document: str) -> Mapping[str, object]:
    try:
        payload = json.loads(document)
    except json.JSONDecodeError as error:
        raise SalaryEvidenceError("a frozen snapshot is not valid JSON") from error
    if not isinstance(payload, dict):
        raise SalaryEvidenceError("a frozen snapshot must be a JSON object")
    return payload


def _requirement_item_payload(item: SubscriptionRequirementItem) -> dict[str, object]:
    return {
        "subscription_id": item.subscription_id,
        "name": item.name,
        "currency": item.currency,
        "year": item.year,
        "month": item.month,
        "funding_wallet_id": item.funding_wallet_id,
        "funding_wallet_name": item.funding_wallet_name,
        "budget_category_id": item.budget_category_id,
        "budget_category_name": item.budget_category_name,
        "cadence": item.cadence.value,
        "cadence_months": item.cadence_months,
        "cycle_position": item.cycle_position,
        "price_period_id": item.price_period_id,
        "cycle_price_minor": item.cycle_price_minor,
        "contribution_minor": item.contribution_minor,
        "is_active": item.is_active,
        "active_from": _month_text(item.active_from),
        "active_to": _month_text(item.active_to),
        "effective_from": _month_text(item.effective_from),
        "effective_to": _month_text(item.effective_to),
        "reason": item.reason.value,
    }


def _requirement_item(payload: Mapping[str, object]) -> SubscriptionRequirementItem:
    try:
        cadence = SubscriptionCadence(_evidence_str(payload, "cadence"))
        reason = SubscriptionRequirementReason(_evidence_str(payload, "reason"))
    except ValueError as error:
        raise SalaryEvidenceError(
            "a frozen snapshot names a cadence or reason this version does not know"
        ) from error
    return SubscriptionRequirementItem(
        subscription_id=_evidence_int(payload, "subscription_id"),
        name=_evidence_str(payload, "name"),
        currency=_evidence_str(payload, "currency"),
        year=_evidence_int(payload, "year"),
        month=_evidence_int(payload, "month"),
        funding_wallet_id=_evidence_int(payload, "funding_wallet_id"),
        funding_wallet_name=_evidence_str(payload, "funding_wallet_name"),
        budget_category_id=_evidence_int(payload, "budget_category_id"),
        budget_category_name=_evidence_str(payload, "budget_category_name"),
        cadence=cadence,
        cadence_months=_evidence_int(payload, "cadence_months"),
        cycle_position=_evidence_optional_int(payload, "cycle_position"),
        price_period_id=_evidence_optional_int(payload, "price_period_id"),
        cycle_price_minor=_evidence_optional_int(payload, "cycle_price_minor"),
        contribution_minor=_evidence_int(payload, "contribution_minor"),
        is_active=_evidence_bool(payload, "is_active"),
        active_from=_month_value(payload, "active_from"),
        active_to=_month_value(payload, "active_to"),
        effective_from=_month_value(payload, "effective_from"),
        effective_to=_month_value(payload, "effective_to"),
        reason=reason,
    )


def serialize_subscription_evidence(evidence: SalarySubscriptionEvidence) -> str:
    """Return the canonical document one run stores for its whole month."""
    return _canonical(
        {
            "schema_version": evidence.schema_version,
            "year": evidence.year,
            "month": evidence.month,
            "currency": evidence.currency,
            "items": [
                _requirement_item_payload(item)
                for item in sorted(
                    evidence.items, key=lambda item: item.subscription_id
                )
            ],
        }
    )


def deserialize_subscription_evidence(document: str) -> SalarySubscriptionEvidence:
    """Rebuild one frozen month of subscription evidence, amounts still exact."""
    payload = _decoded(document)
    version = _evidence_int(payload, "schema_version")
    if version != SALARY_EVIDENCE_SCHEMA_VERSION:
        raise SalaryEvidenceError(
            f"this snapshot was written under schema version {version}"
        )
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise SalaryEvidenceError("a frozen snapshot must list its items")
    items: list[SubscriptionRequirementItem] = []
    for entry in raw_items:
        if not isinstance(entry, dict):
            raise SalaryEvidenceError("every frozen evidence item is a JSON object")
        items.append(_requirement_item(entry))
    return SalarySubscriptionEvidence(
        schema_version=version,
        year=_evidence_int(payload, "year"),
        month=_evidence_int(payload, "month"),
        currency=_evidence_str(payload, "currency"),
        items=tuple(items),
    )


_FIXED_EVIDENCE_KIND = "fixed"
_MANUAL_EVIDENCE_KIND = "manual"
_SUBSCRIPTION_EVIDENCE_KIND = "subscription_requirement"


def serialize_item_evidence(evidence: SalaryItemEvidence) -> str:
    """Return the canonical document one frozen line stores for its amount."""
    common = {
        "schema_version": evidence.schema_version,
        "template_item_id": evidence.template_item_id,
        "template_item_version": evidence.template_item_version,
    }
    if isinstance(evidence, FixedSalaryItemEvidence):
        return _canonical(
            {
                **common,
                "kind": _FIXED_EVIDENCE_KIND,
                "fixed_amount_minor": evidence.fixed_amount_minor,
            }
        )
    if isinstance(evidence, ManualSalaryItemEvidence):
        return _canonical(
            {
                **common,
                "kind": _MANUAL_EVIDENCE_KIND,
                "entered_amount_minor": evidence.entered_amount_minor,
            }
        )
    return _canonical(
        {
            **common,
            "kind": _SUBSCRIPTION_EVIDENCE_KIND,
            "year": evidence.year,
            "month": evidence.month,
            "currency": evidence.currency,
            "destination_wallet_id": evidence.destination_wallet_id,
            "subscription_ids": sorted(evidence.subscription_ids),
            "contribution_total_minor": evidence.contribution_total_minor,
        }
    )


def deserialize_item_evidence(document: str) -> SalaryItemEvidence:
    """Rebuild one frozen line's evidence, refusing anything inexact."""
    payload = _decoded(document)
    version = _evidence_int(payload, "schema_version")
    if version != SALARY_EVIDENCE_SCHEMA_VERSION:
        raise SalaryEvidenceError(
            f"this snapshot was written under schema version {version}"
        )
    kind = _evidence_str(payload, "kind")
    template_item_id = _evidence_int(payload, "template_item_id")
    template_item_version = _evidence_int(payload, "template_item_version")
    if kind == _FIXED_EVIDENCE_KIND:
        return FixedSalaryItemEvidence(
            schema_version=version,
            template_item_id=template_item_id,
            template_item_version=template_item_version,
            fixed_amount_minor=_evidence_int(payload, "fixed_amount_minor"),
        )
    if kind == _MANUAL_EVIDENCE_KIND:
        return ManualSalaryItemEvidence(
            schema_version=version,
            template_item_id=template_item_id,
            template_item_version=template_item_version,
            entered_amount_minor=_evidence_int(payload, "entered_amount_minor"),
        )
    if kind != _SUBSCRIPTION_EVIDENCE_KIND:
        raise SalaryEvidenceError(
            "a frozen line names a calculation kind we do not know"
        )
    raw_ids = payload.get("subscription_ids")
    if not isinstance(raw_ids, list):
        raise SalaryEvidenceError("a frozen subscription line lists its subscriptions")
    identifiers: list[int] = []
    for entry in raw_ids:
        if isinstance(entry, bool) or not isinstance(entry, int):
            raise SalaryEvidenceError("a frozen subscription id must be an integer")
        identifiers.append(entry)
    return SubscriptionSalaryItemEvidence(
        schema_version=version,
        template_item_id=template_item_id,
        template_item_version=template_item_version,
        year=_evidence_int(payload, "year"),
        month=_evidence_int(payload, "month"),
        currency=_evidence_str(payload, "currency"),
        destination_wallet_id=_evidence_int(payload, "destination_wallet_id"),
        subscription_ids=tuple(identifiers),
        contribution_total_minor=_evidence_int(payload, "contribution_total_minor"),
    )


#: Schedules whose money is promised to a bank but has not moved yet. A failed
#: schedule belongs here: the instruction stands until it is cancelled.
ACTIVE_SCHEDULE_STATUSES = frozenset(
    {
        ScheduledTransferStatus.SCHEDULED,
        ScheduledTransferStatus.PROCESSING,
        ScheduledTransferStatus.FAILED,
    }
)
#: Schedules whose movement the ledger already records.
EXECUTED_SCHEDULE_STATUSES = frozenset({ScheduledTransferStatus.ASSUMED_EXECUTED})
#: Schedules that hold nothing and cover nothing.
RELEASED_SCHEDULE_STATUSES = frozenset(
    {ScheduledTransferStatus.CANCELLED, ScheduledTransferStatus.REVERSED}
)

_SCHEDULE_TRANSITIONS: Mapping[
    ScheduledTransferStatus, frozenset[ScheduledTransferStatus]
] = {
    ScheduledTransferStatus.SCHEDULED: frozenset(
        {ScheduledTransferStatus.PROCESSING, ScheduledTransferStatus.CANCELLED}
    ),
    # A claim may be taken over once its lease expires, so `processing` accepts
    # `processing` again; the conditional claim, not this table, decides who wins.
    ScheduledTransferStatus.PROCESSING: frozenset(
        {
            ScheduledTransferStatus.PROCESSING,
            ScheduledTransferStatus.ASSUMED_EXECUTED,
            ScheduledTransferStatus.SCHEDULED,
            ScheduledTransferStatus.FAILED,
        }
    ),
    ScheduledTransferStatus.FAILED: frozenset(
        {
            ScheduledTransferStatus.SCHEDULED,
            ScheduledTransferStatus.PROCESSING,
            ScheduledTransferStatus.CANCELLED,
        }
    ),
    ScheduledTransferStatus.ASSUMED_EXECUTED: frozenset(
        {ScheduledTransferStatus.REVERSED}
    ),
    ScheduledTransferStatus.CANCELLED: frozenset(),
    ScheduledTransferStatus.REVERSED: frozenset(),
}


def ensure_schedule_transition(
    current: ScheduledTransferStatus, target: ScheduledTransferStatus
) -> None:
    """Reject a schedule lifecycle change the state machine does not allow.

    Cancelling a `processing` row is refused here rather than in a route: the
    worker may already be posting its entry, and a cancellation racing a
    posting would leave a movement in the ledger that the schedule denies.
    """
    if target not in _SCHEDULE_TRANSITIONS[current]:
        raise ScheduledTransferStateError(
            f"a {current.value} scheduled transfer cannot become {target.value}"
        )


def resolve_due_at(*, local_date: date, local_time: time, timezone: str) -> datetime:
    """Return the UTC instant one local policy time names, or refuse it.

    ``replace(tzinfo=ZoneInfo(...))`` alone is not enough. A daylight-saving
    gap has no such wall-clock instant at all, and a daylight-saving overlap
    has two of them; either would silently pick one answer and store a due
    time that is an hour away from what the user chose. Both are refused, so a
    schedule always names exactly one instant.
    """
    zone = ZoneInfo(timezone)
    naive = datetime.combine(
        validate_calendar_date(local_date, label="scheduled date"),
        validate_wall_clock(local_time, label="scheduled time"),
    )
    earlier = naive.replace(tzinfo=zone, fold=0)
    later = naive.replace(tzinfo=zone, fold=1)
    if earlier.utcoffset() != later.utcoffset():
        round_trip = earlier.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
        if round_trip != naive:
            raise ScheduledTransferTimeError(
                "this local time does not exist in that timezone on that date; "
                "choose another time"
            )
        raise ScheduledTransferTimeError(
            "this local time happens twice in that timezone on that date; "
            "choose another time"
        )
    return earlier.astimezone(UTC)


def derive_commitment_status(
    *, required_minor: int, active_minor: int, executed_minor: int
) -> CommitmentStatus:
    """Decide one commitment's status from what covers it, and nothing else.

    ``required`` is ``amount_minor - released_minor``; ``active`` is what
    schedules still intend to move; ``executed`` is what they already moved.
    Every service refreshes a commitment through this one function, so a
    schedule, a cancellation, a reversal, and a refund can never leave two
    services disagreeing about the same row (ADR-B059).
    """
    required = validate_minor_units(required_minor, label="commitment requirement")
    active = validate_minor_units(active_minor, label="active coverage")
    executed = validate_minor_units(executed_minor, label="executed coverage")
    if required == 0:
        return CommitmentStatus.CANCELLED
    if executed >= required:
        return CommitmentStatus.FUNDED
    if active + executed >= required:
        return CommitmentStatus.SCHEDULED
    if active + executed > 0:
        return CommitmentStatus.PARTIALLY_SCHEDULED
    return CommitmentStatus.OPEN


@dataclass(frozen=True)
class ScheduleCoverage:
    """One commitment share a schedule covers, reduced to what posting needs."""

    funding_commitment_id: int
    source_wallet_id: int
    amount_minor: int


def schedule_wallet_debits(
    *,
    amount_minor: int,
    source_wallet_id: int,
    coverage: tuple[ScheduleCoverage, ...],
) -> tuple[tuple[int, int], ...]:
    """Return which wallet each part of one scheduled movement leaves from.

    A covered share leaves the wallet that reserved it, so paying a card from
    two budget wallets debits both. Whatever the links do not cover leaves the
    schedule's own source wallet. Shares of one wallet are merged, so the entry
    holds one line per wallet and two identical requests produce the same
    posting signature.
    """
    total = validate_minor_units(amount_minor, label="scheduled amount", minimum=1)
    covered = 0
    debits: dict[int, int] = {}
    for share in coverage:
        amount = validate_minor_units(
            share.amount_minor, label="covered commitment amount", minimum=1
        )
        covered += amount
        debits[share.source_wallet_id] = debits.get(share.source_wallet_id, 0) + amount
    if covered > total:
        raise ScheduledTransferShapeError(
            "a schedule cannot cover more than the amount it moves"
        )
    residual = total - covered
    if residual:
        debits[source_wallet_id] = debits.get(source_wallet_id, 0) + residual
    return tuple(sorted(debits.items()))


def ensure_clearing_coverage_exact(*, amount_minor: int, covered_minor: int) -> None:
    """A clearing transfer moves exactly the commitments it names.

    Unlike an autopay, a clearing transfer has no residual concept: money that
    reaches the clearing hub without a commitment behind it would be reserved
    twice — once by the commitment it was meant for and once by the schedule.
    """
    if covered_minor != amount_minor:
        raise ScheduledTransferShapeError(
            "a clearing transfer must cover exactly the commitments it funds"
        )


def retry_delay_seconds(*, attempt: int, base_seconds: int, max_seconds: int) -> int:
    """Return the bounded exponential backoff before one retry.

    ``attempt`` is the number of attempts already made. The result is capped so
    a permanently unhappy schedule polls at a fixed, low rate rather than
    growing without bound.
    """
    if attempt < 1:
        raise ValueError("a retry follows at least one attempt")
    if base_seconds < 1 or max_seconds < base_seconds:
        raise ValueError("retry backoff needs a positive base below its maximum")
    exponent = min(attempt - 1, 32)
    return int(min(max_seconds, base_seconds * (2**exponent)))


def scheduled_transfer_create_key(client_token: str) -> str:
    """Return the stable key one schedule-creation submission owns."""
    token = client_token.strip()
    if not token:
        raise ValueError("a client idempotency token is required")
    return f"scheduled_transfer:create:{token}"


def scheduled_transfer_execute_key(scheduled_transfer_id: int) -> str:
    """Return the stable journal key one assumed execution owns."""
    return f"scheduled_transfer:execute:{scheduled_transfer_id}"


def scheduled_transfer_reverse_key(scheduled_transfer_id: int) -> str:
    """Return the stable journal key one reversal of that execution owns."""
    return f"scheduled_transfer:reverse:{scheduled_transfer_id}"


def scheduled_transfer_fingerprint(
    *,
    purpose: ScheduledTransferPurpose,
    source_account_id: int,
    source_wallet_id: int,
    destination_account_id: int,
    destination_wallet_id: int | None,
    card_id: int | None,
    amount_minor: int,
    currency: str,
    scheduled_local_date: date,
    scheduled_local_time: time,
    timezone: str,
    due_at_utc: datetime,
    execution_policy: ScheduledExecutionPolicy,
    links: Iterable[tuple[int, int]],
) -> str:
    """Return the canonical identity of one schedule-creation request.

    A matching token proves only that something once used it. Comparing the
    whole payload — including the sorted commitment links — is what makes a
    resubmitted form replay and a different request a conflict.
    """
    return _canonical(
        {
            "purpose": purpose.value,
            "source_account_id": source_account_id,
            "source_wallet_id": source_wallet_id,
            "destination_account_id": destination_account_id,
            "destination_wallet_id": destination_wallet_id,
            "card_id": card_id,
            "amount_minor": validate_minor_units(
                amount_minor, label="scheduled amount", minimum=1
            ),
            "currency": currency,
            "scheduled_local_date": scheduled_local_date.isoformat(),
            "scheduled_local_time": scheduled_local_time.isoformat(),
            "timezone": timezone,
            "due_at_utc": due_at_utc.astimezone(UTC).isoformat(),
            "execution_policy": execution_policy.value,
            "links": sorted(
                [
                    [
                        validate_minor_units(
                            commitment_id, label="commitment id", minimum=1
                        ),
                        validate_minor_units(
                            amount, label="covered commitment amount", minimum=1
                        ),
                    ]
                    for commitment_id, amount in links
                ]
            ),
        }
    )


@dataclass(frozen=True)
class WalletCommitmentView:
    """One commitment of a wallet, reduced to what reservation arithmetic needs."""

    commitment_id: int
    required_minor: int
    active_covered_minor: int
    executed_covered_minor: int


@dataclass(frozen=True)
class WalletScheduleDebitView:
    """What one active schedule will actually take out of one wallet."""

    scheduled_transfer_id: int
    debit_minor: int


def wallet_reserved_minor(
    *,
    salary_waiting_minor: int,
    commitments: Iterable[WalletCommitmentView],
    schedule_debits: Iterable[WalletScheduleDebitView],
) -> int:
    """Return everything one wallet still holds back, counted exactly once.

    The three parts are disjoint by construction (ADR-B059). A commitment
    contributes only the part of its requirement that no schedule covers; an
    active schedule contributes the debit it will really take. Adding a
    commitment and the schedule that funds it in full would reserve the same
    money twice and make ``available`` too low; counting only one of them would
    make it too high the moment a schedule is cancelled.
    """
    total = validate_minor_units(salary_waiting_minor, label="waiting salary")
    for commitment in commitments:
        uncovered = commitment.required_minor - (
            commitment.active_covered_minor + commitment.executed_covered_minor
        )
        total += max(0, uncovered)
    for schedule in schedule_debits:
        total += validate_minor_units(schedule.debit_minor, label="scheduled debit")
    return total


#: Payment methods a hand-entered expense may use. A bank movement is its own
#: workflow, so it is not offered here (ADR-B065).
MANUAL_EXPENSE_PAYMENT_METHODS = frozenset(
    {PaymentMethod.CASH, PaymentMethod.CREDIT_CARD}
)

#: The posting shape each already-observed bank movement uses. `transfer` and
#: `refund` are absent because they are their own commands with their own
#: invariants, and `adjustment` is absent because it belongs to reconciliation.
_BANK_MOVEMENT_EVENT_TYPES: Mapping[BankMovementKind, FinancialEventType] = {
    BankMovementKind.INTEREST_INCOME: FinancialEventType.INTEREST_INCOME,
    BankMovementKind.EXTERNAL_DEPOSIT: FinancialEventType.DEPOSIT,
    BankMovementKind.EXTERNAL_WITHDRAWAL: FinancialEventType.WITHDRAWAL,
    BankMovementKind.CASH_WITHDRAWAL_SPENDING: FinancialEventType.WITHDRAWAL,
    BankMovementKind.DIRECT_DEBIT: FinancialEventType.DIRECT_DEBIT,
    BankMovementKind.FEE: FinancialEventType.FEE,
    BankMovementKind.BILL_PAYMENT: FinancialEventType.BILL_PAYMENT,
}

#: The movements that are spending and therefore need a category and the whole
#: B3 allocation path rather than a two-line entry of their own.
SPENDING_BANK_MOVEMENTS = frozenset(
    {
        BankMovementKind.DIRECT_DEBIT,
        BankMovementKind.FEE,
        BankMovementKind.BILL_PAYMENT,
        BankMovementKind.CASH_WITHDRAWAL_SPENDING,
    }
)

#: Statuses a manual event may be reversed from. Only a posted event has an
#: entry to cancel; anything else has moved no money to give back.
_REVERSIBLE_EVENT_STATUSES = frozenset({FinancialEventStatus.POSTED})


def bank_movement_event_type(kind: BankMovementKind) -> FinancialEventType:
    """Return the canonical event type one observed bank movement records."""
    return _BANK_MOVEMENT_EVENT_TYPES[kind]


def bank_movement_payment_method(kind: BankMovementKind) -> PaymentMethod:
    """Return how a movement contributes to daily instrument totals."""
    if kind is BankMovementKind.CASH_WITHDRAWAL_SPENDING:
        return PaymentMethod.CASH
    return PaymentMethod.BANK_ACCOUNT


def bank_movement_entry_type(kind: BankMovementKind) -> JournalEntryType:
    """Return the entry type one observed bank movement posts under.

    Interest is income, an unexplained deposit or withdrawal is an external
    movement, and a direct debit, fee, or bill payment is spending. None of
    them is an adjustment: an adjustment says the cause is unknown, and these
    all name their cause (ADR-B065).
    """
    if kind is BankMovementKind.INTEREST_INCOME:
        return JournalEntryType.INCOME
    if kind in SPENDING_BANK_MOVEMENTS:
        return JournalEntryType.EXPENSE
    return JournalEntryType.EXTERNAL_MOVEMENT


def ensure_manual_expense_payment_method(payment_method: PaymentMethod) -> None:
    """Refuse a hand-entered expense paid in a way B7 does not post."""
    if payment_method not in MANUAL_EXPENSE_PAYMENT_METHODS:
        raise ManualExpenseError(
            "a manual expense is paid in cash or by credit card; record a bank "
            "movement instead"
        )


def ensure_reversible_event(
    *, origin: FinancialEventOrigin, status: FinancialEventStatus
) -> None:
    """Refuse to reverse anything but a posted, hand-entered event.

    A Gmail-origin event is the bank's own statement of what happened, so it is
    corrected by re-reading the source rather than by cancelling it here.
    """
    if origin is not FinancialEventOrigin.MANUAL:
        raise EventReversalError("only a manually recorded event can be reversed here")
    if status not in _REVERSIBLE_EVENT_STATUSES:
        raise EventReversalError(
            f"a {status.value} event has nothing posted to reverse"
        )


def ensure_redoable_allocation(
    *,
    status: FinancialEventStatus,
    event_type: FinancialEventType,
    payment_method: PaymentMethod,
    has_allocation: bool,
) -> None:
    """Refuse to redo anything but a posted event's own spending decision.

    This deliberately ignores origin, where reversal does not (ADR-B103).
    Reversing an event denies that the money moved at all, which a bank's own
    notification is not Scribe's to deny. An allocation is the opposite kind
    of statement: which wallet funds the expense and which category it lands
    in are decisions the user made, and a decision can be remade. Only the
    entry carrying those decisions is reversed; the event's amount, merchant,
    time, and card are never touched.

    Three things are therefore not redoable, and each would do real damage:

    A deposit, interest credit, transfer, or reconciliation adjustment holds
    no allocation at all. Its entry records money entering or leaving the
    bank, not a choice about which wallet pays, so reversing it would cancel
    a real movement and leave the event stranded in a queue whose form cannot
    accept it.

    A refund releases part of the original purchase's reservation as it
    posts. Reversing the refund's entry does not take that release back, so
    redoing one could release the same reservation twice.
    """
    if status is not FinancialEventStatus.POSTED:
        raise AllocationRedoError(f"a {status.value} event has no allocation to redo")
    if not has_allocation:
        raise AllocationRedoError(
            "this event records an external movement rather than a spending "
            "decision, so it has no allocation to redo"
        )
    if event_type in REFUND_EVENT_TYPES:
        raise AllocationRedoError(
            "a refund gives back part of the original purchase's reservation "
            "as it posts, and reversing its entry would not take that back"
        )
    ensure_allocatable_event_type(event_type, payment_method)


def interest_income_ledger_code(currency: str) -> str:
    """Return the deterministic code of one currency's interest identity."""
    return f"income:interest:{currency}"


def external_clearing_ledger_code(currency: str) -> str:
    """Return the deterministic code money entering or leaving Scribe uses.

    A deposit whose origin is outside every tracked account still needs a
    second side, and calling that side income would make it look like earnings.
    """
    return f"clearing:external:{currency}"


def reconciliation_equity_ledger_code(currency: str) -> str:
    """Return the deterministic code one currency's reconciliation gap uses."""
    return f"equity:reconciliation:{currency}"


def manual_expense_event_key(client_token: str) -> str:
    """Return the stable event key one manual-expense submission owns."""
    return f"manual_expense:{_require_token(client_token)}"


def bank_movement_event_key(client_token: str) -> str:
    """Return the stable event key one bank-movement submission owns."""
    return f"bank_movement:{_require_token(client_token)}"


def bank_movement_journal_key(financial_event_id: int) -> str:
    """Return the stable journal key one recorded bank movement owns."""
    return f"bank_movement:financial_event:{financial_event_id}"


def account_transfer_event_key(client_token: str) -> str:
    """Return the stable event key one tracked wallet-to-wallet move owns."""
    return f"account_transfer:{_require_token(client_token)}"


def account_transfer_journal_key(financial_event_id: int) -> str:
    """Return the stable journal key one tracked transfer owns."""
    return f"account_transfer:financial_event:{financial_event_id}"


def manual_refund_event_key(client_token: str) -> str:
    """Return the stable event key one hand-entered refund owns."""
    return f"manual_refund:{_require_token(client_token)}"


def event_reversal_journal_key(journal_entry_id: int) -> str:
    """Return the stable journal key the reversal of one entry owns."""
    return f"event_reversal:journal_entry:{journal_entry_id}"


def allocation_redo_journal_key(journal_entry_id: int) -> str:
    """Return the stable journal key a redone allocation's reversal owns.

    Kept distinct from `event_reversal_journal_key` so that redoing an
    allocation and cancelling an event can never be mistaken for a replay of
    each other: the same entry may legitimately be reversed by one and then,
    later, by the other.
    """
    return f"allocation_redo:journal_entry:{journal_entry_id}"


def balance_snapshot_key(client_token: str) -> str:
    """Return the stable key one observed-balance submission owns."""
    return f"balance_snapshot:{_require_token(client_token)}"


def reconciliation_adjustment_event_key(snapshot_id: int, client_token: str) -> str:
    """Return the stable event key one reasoned adjustment owns.

    The snapshot is part of the key, not only part of the request. Two
    observations can hold the same account, balance, and instant and still be
    two different pieces of evidence; a token keyed on the reason alone would
    let a resubmission aimed at the second one be answered with the adjustment
    written for the first, and report a difference as handled when nothing had
    been posted for it (ADR-B067).
    """
    identifier = validate_minor_units(snapshot_id, label="snapshot id", minimum=1)
    return f"reconciliation_adjustment:{identifier}:{_require_token(client_token)}"


def reconciliation_adjustment_journal_key(financial_event_id: int) -> str:
    """Return the stable journal key one posted adjustment owns."""
    return f"reconciliation_adjustment:financial_event:{financial_event_id}"


def _require_token(client_token: str) -> str:
    """Return a non-blank client token, or refuse to invent one."""
    token = client_token.strip()
    if not token:
        raise ValueError("a client idempotency token is required")
    return token


def local_day_bounds(day: date, timezone: ZoneInfo) -> tuple[datetime, datetime]:
    """Return the UTC half-open range covering one local calendar day.

    Entries are stored in UTC but "today" is a local idea, so the boundary is
    built in the user's timezone first. Eight hours of a Taipei day would
    otherwise land in the wrong day's total.
    """
    start = datetime(day.year, day.month, day.day, tzinfo=timezone)
    end = start + timedelta(days=1)
    return start.astimezone(UTC), end.astimezone(UTC)


def reconciliation_difference_minor(
    *, observed_balance_minor: int, ledger_balance_minor: int
) -> int:
    """Return observed minus ledger, signed, in exact minor units.

    The sign is the whole point: a positive difference means the bank holds
    money the ledger has not recorded, and a negative one means the ledger
    believes in money the bank does not have. Swapping the operands would post
    every correction in the wrong direction.
    """
    observed = validate_signed_minor_units(
        observed_balance_minor, label="observed balance"
    )
    ledger = validate_signed_minor_units(ledger_balance_minor, label="ledger balance")
    return observed - ledger


def ensure_distinct_transfer_wallets(
    *, source_wallet_id: int, destination_wallet_id: int
) -> None:
    """Refuse a transfer that would move money to where it already is."""
    if source_wallet_id == destination_wallet_id:
        raise BankMovementError("a transfer needs two different wallets")


def wallet_reallocation_event_key(client_token: str) -> str:
    """Return the stable event key one edited wallet cell owns."""
    return f"wallet_reallocation:{_require_token(client_token)}"


def resolve_display_arrangement(
    *, item_ids: Sequence[int], requested_positions: Mapping[int, int]
) -> tuple[int, ...]:
    """Return one sheet axis in the order that was asked for.

    The same rule arranges the accounts of the sheet and the wallets of one
    account, because both are an axis a person orders deliberately.

    Every item must appear exactly once. A partial submission is refused rather
    than completed with a guess, because the page it came from no longer
    describes the axis — an item added since it loaded would otherwise be
    silently pushed to an end nobody chose.

    Positions are ordinary integers rather than a strict permutation: someone
    numbering by hand may repeat or skip a number, and a tie is broken by the
    order the items already read in, which is the arrangement they were
    looking at. The stored result is renumbered from that sequence, so gaps
    and duplicates never accumulate.
    """
    if len(set(item_ids)) != len(item_ids):  # pragma: no cover - repository
        raise DisplayOrderingError("an axis listed one item twice")
    if set(requested_positions) != set(item_ids):
        raise DisplayOrderingError(
            "an arrangement names every item on the axis exactly once; reload and retry"
        )
    for position in requested_positions.values():
        if isinstance(position, bool) or not isinstance(position, int):
            raise TypeError("a position must be a whole number")
    return tuple(
        sorted(
            item_ids,
            key=lambda item_id: (
                requested_positions[item_id],
                item_ids.index(item_id),
            ),
        )
    )


def wallet_reallocation_description(
    *, edited_wallet_name: str, counterpart_wallet_name: str, delta_minor: int
) -> str:
    """Return the description one edited cell posts, named by its direction.

    A reallocation reads later as an ordinary internal transfer, so it says
    which envelope gave and which received rather than which cell was typed
    into. `delta_minor` is signed and never zero here: the caller stops before
    posting when nothing moved.
    """
    if delta_minor > 0:
        source, destination = counterpart_wallet_name, edited_wallet_name
    else:
        source, destination = edited_wallet_name, counterpart_wallet_name
    return f"重分配 {source} - {destination}"


def ensure_same_account_reallocation(
    *, source_account_id: int, destination_account_id: int
) -> None:
    """Refuse a cell edit that would silently change an account's total.

    Typing a number into one wallet says how that account's money is divided,
    never how much of it there is. Letting the counterpart live in another
    account would move real money between two banks on the strength of an
    edit that displays as a split, and the column total the user was reading
    would no longer be the number they agreed to (ADR-B086).
    """
    if source_account_id != destination_account_id:
        raise WalletReallocationError(
            "a wallet reallocation stays inside one financial account"
        )


def ensure_expected_wallet_balance(*, expected_minor: int, actual_minor: int) -> None:
    """Refuse an edit computed against a wallet total that has since moved.

    The submitted number is an intended result, not a delta, so a stale page
    would post the difference against the wrong starting point and quietly
    undo whatever changed in between.
    """
    expected = validate_signed_minor_units(expected_minor, label="expected balance")
    actual = validate_signed_minor_units(actual_minor, label="wallet balance")
    if expected != actual:
        raise WalletReallocationError(
            "this wallet changed since the page was loaded; reload and retry"
        )


def wallet_reallocation_delta_minor(*, expected_minor: int, target_minor: int) -> int:
    """Return the signed amount one edited cell asks to move into the wallet.

    The result is deliberately signed and may be negative: an envelope can be
    reduced, and B7 already keeps negative wallet balances rather than
    clamping them (ADR-B075).
    """
    expected = validate_signed_minor_units(expected_minor, label="expected balance")
    target = validate_signed_minor_units(target_minor, label="target balance")
    return target - expected


def sweepable_minor(*, target_minor: int, net_spend_minor: int) -> int:
    """Return the unspent part of a target, never a negative or an overspend.

    The buffer is deliberately absent: it is a cushion that stays where it is,
    not part of the month's spendable target (ADR-B031).
    """
    return max(0, target_minor - max(0, net_spend_minor))


def remaining_buffer_minor(
    *, buffer_minor: int, target_minor: int, net_spend_minor: int
) -> int:
    """Return the cushion still intact after overspending has eaten into it.

    A buffer that has already absorbed an overspend is not there any more.
    Withholding the full amount at month end would hold back money the wallet
    no longer contains and silently shrink what other categories can sweep.
    """
    overspend = max(0, net_spend_minor - target_minor)
    return max(0, buffer_minor - overspend)

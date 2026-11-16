"""Framework-independent ledger value objects and records."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time

from app.domain.budget_enums import (
    AllocationSource,
    BalanceSnapshotSource,
    BudgetPeriodStatus,
    CardConfigurationStatus,
    CardHolderRole,
    CardImportAssignmentKind,
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
    SalaryCalculationMode,
    SalaryRunItemStatus,
    SalaryRunStatus,
    ScheduledExecutionPolicy,
    ScheduledTransferPurpose,
    ScheduledTransferStatus,
    SourceDiscrepancy,
    SourceLinkReason,
    SourceLinkStatus,
    SubscriptionCadence,
    SubscriptionRequirementReason,
    WalletPurpose,
)

_CURRENCY_PATTERN = re.compile(r"[A-Z]{3}")
_LAST_FOUR_PATTERN = re.compile(r"[0-9]{4}")
_MASKED_REFERENCE_DIGIT_RUN = re.compile(r"\d{5,}")
_MAX_MASKED_REFERENCE_LENGTH = 32
MAX_CADENCE_MONTHS = 120
MAX_SUBSCRIPTION_NAME_LENGTH = 200


def normalize_currency(currency: str) -> str:
    """Return an uppercase ISO 4217 code or reject an unusable value."""
    code = currency.strip().upper()
    if _CURRENCY_PATTERN.fullmatch(code) is None:
        raise ValueError("currency must be a three-letter ISO 4217 code")
    return code


def normalize_masked_reference(reference: str | None) -> str | None:
    """Return a short masked account hint that cannot hold a full number."""
    if reference is None:
        return None
    value = reference.strip()
    if not value:
        return None
    if len(value) > _MAX_MASKED_REFERENCE_LENGTH:
        raise ValueError("masked reference is too long to be a masked value")
    if _MASKED_REFERENCE_DIGIT_RUN.search(value) is not None:
        raise ValueError("masked reference must not contain a full account number")
    return value


def validate_minor_units(value: int, *, label: str, minimum: int = 0) -> int:
    """Return an integer minor-unit amount at or above ``minimum``.

    ``bool`` is rejected explicitly: it is a subclass of ``int``, so a stray
    flag would otherwise be stored as 0 or 1 money.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be integer minor units")
    if value < minimum:
        raise ValueError(
            f"{label} must be at least {minimum} minor units"
            if minimum
            else f"{label} must not be negative"
        )
    return value


def validate_signed_minor_units(value: int, *, label: str) -> int:
    """Return an integer minor-unit amount that may be zero or negative.

    An observed bank balance is the one money value B7 may not clamp: a real
    account can be empty or overdrawn, and refusing to record that would make
    the only observation the user has unrecordable. ``bool`` is still rejected,
    for the same reason every other money input rejects it.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be integer minor units")
    return value


def normalize_note(value: str | None, *, label: str, max_length: int) -> str | None:
    """Return a trimmed, bounded note, or None when nothing was written."""
    if value is None:
        return None
    note = value.strip()
    if not note:
        return None
    if len(note) > max_length:
        raise ValueError(f"{label} must be at most {max_length} characters")
    return note


def validate_event_magnitude(amount_minor: int) -> int:
    """Return a positive integer magnitude; direction comes from event type."""
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
        raise TypeError("event amounts must be integer minor units")
    if amount_minor <= 0:
        raise ValueError("event amounts must be a positive magnitude")
    return amount_minor


def normalize_last_four(value: str) -> str:
    """Return exactly four digits; a full card number is never accepted."""
    digits = value.strip()
    if _LAST_FOUR_PATTERN.fullmatch(digits) is None:
        raise ValueError("card last four must be exactly four digits")
    return digits


def validate_cadence_months(cadence: SubscriptionCadence, cadence_months: int) -> int:
    """Return the fixed month interval one cadence is allowed to describe.

    ``bool`` is rejected for the same reason money rejects it: it is an
    ``int`` subclass, so a stray flag would become a one-month cycle.
    """
    if isinstance(cadence_months, bool) or not isinstance(cadence_months, int):
        raise TypeError("cadence months must be an integer number of months")
    if cadence is SubscriptionCadence.MONTHLY and cadence_months != 1:
        raise ValueError("a monthly subscription has a one-month cycle")
    if cadence is SubscriptionCadence.YEARLY and cadence_months != 12:
        raise ValueError("a yearly subscription has a twelve-month cycle")
    if cadence is SubscriptionCadence.CUSTOM and not (
        2 <= cadence_months <= MAX_CADENCE_MONTHS
    ):
        raise ValueError(
            "a custom cadence needs a fixed interval of 2 to "
            f"{MAX_CADENCE_MONTHS} months"
        )
    return cadence_months


def validate_month_start(value: date | None, *, label: str) -> date | None:
    """Return a boundary that names a whole month, never a part of one.

    B4 deliberately does not prorate by day, so every activity and price
    boundary is the first day of a month. That makes the price effective in
    one calendar month unambiguous instead of depending on which day is
    inspected. ``datetime`` is rejected explicitly because it subclasses
    ``date`` and would smuggle a time of day into a calendar-only value.
    """
    if value is None:
        return None
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError(f"{label} must be a calendar date")
    if value.day != 1:
        raise ValueError(f"{label} must be the first day of a month")
    return value


def validate_billing_month(value: int | None) -> int | None:
    """Return the 1-12 anchor month, or None when a cadence has no anchor."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("billing month must be an integer")
    if not 1 <= value <= 12:
        raise ValueError("billing month must be between 1 and 12")
    return value


def validate_billing_day(value: int | None) -> int | None:
    """Return the 1-31 charge day kept as evidence, never used to prorate."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("billing day must be an integer")
    if not 1 <= value <= 31:
        raise ValueError("billing day must be between 1 and 31")
    return value


def normalize_name(value: str, *, label: str, max_length: int) -> str:
    """Return a trimmed, bounded, non-empty display name."""
    name = value.strip()
    if not name:
        raise ValueError(f"{label} must not be empty")
    if len(name) > max_length:
        raise ValueError(f"{label} must be at most {max_length} characters")
    return name


@dataclass(frozen=True)
class Money:
    """An exact integer minor-unit amount in exactly one currency."""

    amount_minor: int
    currency: str

    def __post_init__(self) -> None:
        """Reject float input and normalize the currency code."""
        if isinstance(self.amount_minor, bool) or not isinstance(
            self.amount_minor, int
        ):
            raise TypeError("money amounts must be integer minor units")
        object.__setattr__(self, "currency", normalize_currency(self.currency))

    def negated(self) -> Money:
        """Return the opposite signed amount in the same currency."""
        return Money(-self.amount_minor, self.currency)


@dataclass(frozen=True)
class FinancialAccountDraft:
    """Validated values used to open one physical balance container."""

    institution_code: str
    display_name: str
    account_type: FinancialAccountType
    currency: str
    masked_reference: str | None = None


@dataclass(frozen=True)
class FinancialAccount:
    """Persisted physical account without ORM state."""

    id: int
    institution_code: str
    display_name: str
    account_type: FinancialAccountType
    currency: str
    masked_reference: str | None
    is_active: bool
    version: int
    created_at: datetime
    updated_at: datetime
    display_order: int = 0


@dataclass(frozen=True)
class WalletDraft:
    """Validated values used to create one envelope inside an asset account."""

    financial_account_id: int
    name: str
    purpose: WalletPurpose = WalletPurpose.GENERAL


@dataclass(frozen=True)
class Wallet:
    """Persisted virtual envelope inside one physical asset account."""

    id: int
    financial_account_id: int
    name: str
    purpose: WalletPurpose
    is_active: bool
    version: int
    created_at: datetime
    updated_at: datetime
    display_order: int = 0


@dataclass(frozen=True)
class LedgerAccountDraft:
    """Validated values used to create one double-entry ledger account."""

    code: str
    name: str
    kind: LedgerAccountKind
    currency: str
    financial_account_id: int | None = None
    budget_category_id: int | None = None


@dataclass(frozen=True)
class LedgerAccount:
    """Persisted double-entry account, optionally backed by a real account."""

    id: int
    code: str
    name: str
    kind: LedgerAccountKind
    currency: str
    financial_account_id: int | None
    budget_category_id: int | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class PostingDraft:
    """One signed line of a balanced journal entry."""

    ledger_account_id: int
    amount: Money
    wallet_id: int | None = None
    budget_category_id: int | None = None


@dataclass(frozen=True)
class JournalEntryDraft:
    """A complete immutable entry offered for posting."""

    entry_type: JournalEntryType
    effective_at: datetime
    recorded_at: datetime
    idempotency_key: str
    description: str
    postings: tuple[PostingDraft, ...]
    reversal_of_id: int | None = None
    financial_event_id: int | None = None


@dataclass(frozen=True)
class JournalPosting:
    """Persisted signed line of one posted journal entry."""

    id: int
    journal_entry_id: int
    ledger_account_id: int
    wallet_id: int | None
    amount_minor: int
    currency: str
    budget_category_id: int | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class JournalEntry:
    """Persisted immutable entry and its balanced postings."""

    id: int
    entry_type: JournalEntryType
    effective_at: datetime
    recorded_at: datetime
    idempotency_key: str
    reversal_of_id: int | None
    description: str
    financial_event_id: int | None
    postings: tuple[JournalPosting, ...]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class OpeningWalletSplit:
    """One signed wallet share of a reconciled opening balance.

    A wallet is an envelope inside the physical account, so one envelope may
    already be overdrawn at cutover even while the bank account as a whole is
    positive.  Zero is also meaningful configuration, although the journal
    service omits it because postings themselves must be nonzero.
    """

    wallet_name: str
    amount_minor: int

    def __post_init__(self) -> None:
        """Reject float and boolean values without clamping the sign."""
        object.__setattr__(
            self,
            "amount_minor",
            validate_signed_minor_units(
                self.amount_minor, label="opening wallet share"
            ),
        )


@dataclass(frozen=True)
class WalletBalance:
    """Calculated ledger balance of one wallet."""

    wallet_id: int
    name: str
    purpose: WalletPurpose
    is_active: bool
    currency: str
    balance_minor: int


@dataclass(frozen=True)
class FinancialAccountBalance:
    """Calculated ledger balance of one account and its wallet composition."""

    financial_account_id: int
    institution_code: str
    display_name: str
    account_type: FinancialAccountType
    currency: str
    ledger_account_code: str
    balance_minor: int
    wallets: tuple[WalletBalance, ...]


@dataclass(frozen=True)
class CardIdentity:
    """The only card identity Scribe stores: institution plus last four."""

    gmail_account_id: int
    issuer_code: str
    last_four: str


@dataclass(frozen=True)
class CardDraft:
    """Validated values used to register one locally configured card."""

    gmail_account_id: int
    issuer_code: str
    display_name: str
    last_four: str
    holder_role: CardHolderRole = CardHolderRole.UNKNOWN


@dataclass(frozen=True)
class CardConfigurationDraft:
    """Explicit local settlement configuration for one existing card."""

    card_id: int
    expected_version: int
    display_name: str
    holder_role: CardHolderRole
    liability_account_id: int | None
    settlement_account_id: int | None
    default_funding_wallet_id: int | None


@dataclass(frozen=True)
class Card:
    """Persisted card configuration; never a full card number."""

    id: int
    gmail_account_id: int
    issuer_code: str
    display_name: str
    last_four: str
    holder_role: CardHolderRole
    configuration_status: CardConfigurationStatus
    liability_account_id: int | None
    settlement_account_id: int | None
    default_funding_wallet_id: int | None
    active_from: datetime | None
    active_to: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime

    @property
    def is_ready(self) -> bool:
        """Return whether this card may fund a posted expense in later work."""
        return self.configuration_status is CardConfigurationStatus.READY


@dataclass(frozen=True)
class CardImportAssignmentDraft:
    """Explicitly attach one immutable imported operational hint to a card."""

    import_row_id: int
    card_id: int
    assignment_kind: CardImportAssignmentKind
    statement_day: int | None = None
    due_day: int | None = None
    autopay_day: int | None = None

    def __post_init__(self) -> None:
        """Validate the two assignment shapes without interpreting bank names."""
        if self.assignment_kind is CardImportAssignmentKind.CARD_CYCLE:
            for field_name in ("statement_day", "due_day", "autopay_day"):
                value = getattr(self, field_name)
                if value is None:
                    raise ValueError(f"a card cycle requires {field_name}")
                validate_billing_day(value)
        elif any(
            value is not None
            for value in (self.statement_day, self.due_day, self.autopay_day)
        ):
            raise ValueError("a transfer reference cannot carry card-cycle days")


@dataclass(frozen=True)
class CardImportAssignment:
    """Persisted, user-approved mapping from B9 evidence to one card."""

    id: int
    import_row_id: int
    card_id: int
    assignment_kind: CardImportAssignmentKind
    statement_day: int | None
    due_day: int | None
    autopay_day: int | None
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class FinancialEventDraft:
    """Validated values for one canonical economic event.

    ``amount_minor`` is a positive magnitude; direction comes from event type.
    """

    origin: FinancialEventOrigin
    event_type: FinancialEventType
    payment_method: PaymentMethod
    recognition_basis: RecognitionBasis
    occurred_at: datetime
    amount_minor: int
    currency: str
    idempotency_key: str
    merchant: str | None = None
    card_id: int | None = None
    status: FinancialEventStatus = FinancialEventStatus.NEEDS_ALLOCATION
    note: str | None = None
    source_import_key: str | None = None
    refund_of_event_id: int | None = None

    def __post_init__(self) -> None:
        """Reject float, zero, and negative amounts at the domain boundary."""
        object.__setattr__(
            self, "amount_minor", validate_event_magnitude(self.amount_minor)
        )
        object.__setattr__(self, "currency", normalize_currency(self.currency))


@dataclass(frozen=True)
class FinancialEventUpdate:
    """Parser-owned values refreshed on an event that is not yet posted."""

    event_type: FinancialEventType
    payment_method: PaymentMethod
    occurred_at: datetime
    amount_minor: int
    currency: str
    merchant: str | None
    card_id: int | None

    def __post_init__(self) -> None:
        """Apply the same positive-magnitude rule as a new event."""
        object.__setattr__(
            self, "amount_minor", validate_event_magnitude(self.amount_minor)
        )
        object.__setattr__(self, "currency", normalize_currency(self.currency))


MAX_FINANCIAL_EVENT_NOTE_LENGTH = 500


@dataclass(frozen=True)
class FinancialEventNoteUpdate:
    """One optimistic edit of user-owned, non-accounting event metadata."""

    event_id: int
    expected_version: int
    note: str | None

    def __post_init__(self) -> None:
        """Normalize the note while rejecting invalid identifiers and versions."""
        if isinstance(self.event_id, bool) or self.event_id < 1:
            raise ValueError("financial event id must be a positive integer")
        if isinstance(self.expected_version, bool) or self.expected_version < 1:
            raise ValueError("financial event version must be a positive integer")
        object.__setattr__(
            self,
            "note",
            normalize_note(
                self.note,
                label="financial event note",
                max_length=MAX_FINANCIAL_EVENT_NOTE_LENGTH,
            ),
        )


@dataclass(frozen=True)
class FinancialEvent:
    """Persisted canonical event without ORM state."""

    id: int
    origin: FinancialEventOrigin
    source_import_key: str | None
    event_type: FinancialEventType
    payment_method: PaymentMethod
    recognition_basis: RecognitionBasis
    occurred_at: datetime
    merchant: str | None
    amount_minor: int
    currency: str
    card_id: int | None
    status: FinancialEventStatus
    note: str | None
    version: int
    idempotency_key: str
    refund_of_event_id: int | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class SourceLinkDraft:
    """Validated provenance between one event and one parsed transaction."""

    financial_event_id: int
    transaction_id: int
    status: SourceLinkStatus
    match_score_bps: int
    reasons: tuple[SourceLinkReason, ...] = ()
    reviewed_at: datetime | None = None


@dataclass(frozen=True)
class FinancialEventSourceLink:
    """Persisted provenance row; it never creates a journal entry."""

    id: int
    financial_event_id: int
    transaction_id: int
    status: SourceLinkStatus
    match_score_bps: int
    reasons: tuple[SourceLinkReason, ...]
    discrepancies: tuple[SourceDiscrepancy, ...]
    reviewed_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class BudgetCategoryDraft:
    """Validated values used to create one spending purpose."""

    name: str
    parent_id: int | None = None


@dataclass(frozen=True)
class BudgetCategory:
    """Persisted hierarchical, currency-neutral spending purpose."""

    id: int
    name: str
    parent_id: int | None
    is_active: bool
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class BudgetCategoryUpdate:
    """One optimistic rename, re-parent, or activation change."""

    category_id: int
    expected_version: int
    name: str
    parent_id: int | None
    is_active: bool


@dataclass(frozen=True)
class BudgetPeriod:
    """Persisted calendar month of budget in exactly one currency."""

    id: int
    year: int
    month: int
    currency: str
    status: BudgetPeriodStatus
    closed_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime

    @property
    def label(self) -> str:
        """Return the ``yyyy-mm`` label used by routes and the CLI."""
        return f"{self.year:04d}-{self.month:02d}"


@dataclass(frozen=True)
class BudgetTargetDraft:
    """Validated target, buffer, and wallets for one category month."""

    budget_period_id: int
    budget_category_id: int
    target_minor: int
    buffer_minor: int
    funding_wallet_id: int
    month_end_destination_wallet_id: int | None = None
    expected_version: int | None = None

    def __post_init__(self) -> None:
        """Reject float, bool, and negative targets at the domain boundary."""
        object.__setattr__(
            self,
            "target_minor",
            validate_minor_units(self.target_minor, label="budget target"),
        )
        object.__setattr__(
            self,
            "buffer_minor",
            validate_minor_units(self.buffer_minor, label="budget buffer"),
        )


@dataclass(frozen=True)
class BudgetTarget:
    """Persisted monthly target with its explicit funding wallet."""

    id: int
    budget_period_id: int
    budget_category_id: int
    target_minor: int
    buffer_minor: int
    funding_wallet_id: int
    month_end_destination_wallet_id: int | None
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class EventAllocationDraft:
    """One requested share of a financial event."""

    financial_event_id: int
    funding_wallet_id: int
    amount_minor: int
    currency: str
    budget_category_id: int | None = None
    allocation_source: AllocationSource = AllocationSource.MANUAL

    def __post_init__(self) -> None:
        """Apply the same positive-magnitude rule as a financial event."""
        object.__setattr__(
            self, "amount_minor", validate_event_magnitude(self.amount_minor)
        )
        object.__setattr__(self, "currency", normalize_currency(self.currency))


@dataclass(frozen=True)
class EventAllocation:
    """Persisted share of one event assigned to a category and wallet."""

    id: int
    financial_event_id: int
    budget_category_id: int | None
    funding_wallet_id: int
    amount_minor: int
    currency: str
    allocation_source: AllocationSource
    created_at: datetime
    updated_at: datetime
    #: The entry that carried this decision, so the rows in force can be told
    #: from the ones a redo superseded (ADR-B103).
    journal_entry_id: int | None = None


@dataclass(frozen=True)
class DedicatedWalletAllocationDraft:
    """Move one card allocation from its purpose wallet into a clearing hub.

    Presence of this record is the historical spending treatment: the expense
    is funded from a dedicated envelope instead of consuming the calendar
    month's general spending allowance.  The destination is stored rather
    than inferred from a wallet name so renames never rewrite history.
    """

    event_allocation_id: int
    clearing_wallet_id: int

    def __post_init__(self) -> None:
        """Reject booleans and invalid identifiers at the domain boundary."""
        for label, value in (
            ("event allocation id", self.event_allocation_id),
            ("clearing wallet id", self.clearing_wallet_id),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{label} must be a positive integer")


@dataclass(frozen=True)
class DedicatedWalletAllocation:
    """Persisted immutable evidence of one dedicated-wallet reclassification."""

    id: int
    event_allocation_id: int
    clearing_wallet_id: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class FundingCommitmentDraft:
    """Money reserved for one allocation, with an explicit path."""

    financial_event_id: int
    event_allocation_id: int
    source_wallet_id: int
    settlement_account_id: int
    amount_minor: int
    currency: str
    fulfillment_mode: CommitmentFulfillmentMode
    status: CommitmentStatus = CommitmentStatus.OPEN

    def __post_init__(self) -> None:
        """A reservation is a positive magnitude in one normalized currency."""
        object.__setattr__(
            self,
            "amount_minor",
            validate_minor_units(
                self.amount_minor, label="commitment amount", minimum=1
            ),
        )
        object.__setattr__(self, "currency", normalize_currency(self.currency))


def _validated_subscription_shape(
    *,
    name: str,
    currency: str,
    billing_cadence: SubscriptionCadence,
    cadence_months: int,
    billing_month: int | None,
    billing_day: int | None,
    active_from: date | None,
    active_to: date | None,
) -> tuple[str, str, int, int | None, int | None, date | None, date | None]:
    """Validate one subscription's shape for both creation and correction.

    A yearly cycle is anchored by its billing month, and a custom cycle by the
    month it became active, so neither can be placed by guessing at today's
    date. Both anchors are required rather than inferred.
    """
    checked_name = normalize_name(
        name, label="subscription name", max_length=MAX_SUBSCRIPTION_NAME_LENGTH
    )
    code = normalize_currency(currency)
    months = validate_cadence_months(billing_cadence, cadence_months)
    anchor_month = validate_billing_month(billing_month)
    charge_day = validate_billing_day(billing_day)
    start = validate_month_start(active_from, label="active from")
    end = validate_month_start(active_to, label="active to")
    if billing_cadence is SubscriptionCadence.YEARLY and anchor_month is None:
        raise ValueError("a yearly subscription needs its billing month as anchor")
    if billing_cadence is SubscriptionCadence.CUSTOM and start is None:
        raise ValueError("a custom cadence needs an active-from month as anchor")
    if start is not None and end is not None and end <= start:
        raise ValueError("active to must be later than active from")
    return checked_name, code, months, anchor_month, charge_day, start, end


@dataclass(frozen=True)
class SubscriptionDraft:
    """Validated values used to register one recurring service."""

    name: str
    budget_category_id: int
    funding_wallet_id: int
    currency: str
    billing_cadence: SubscriptionCadence
    cadence_months: int
    billing_month: int | None = None
    billing_day: int | None = None
    active_from: date | None = None
    active_to: date | None = None

    def __post_init__(self) -> None:
        """Reject an unusable cadence, anchor, or activity range up front."""
        shape = _validated_subscription_shape(
            name=self.name,
            currency=self.currency,
            billing_cadence=self.billing_cadence,
            cadence_months=self.cadence_months,
            billing_month=self.billing_month,
            billing_day=self.billing_day,
            active_from=self.active_from,
            active_to=self.active_to,
        )
        for field, value in zip(
            (
                "name",
                "currency",
                "cadence_months",
                "billing_month",
                "billing_day",
                "active_from",
                "active_to",
            ),
            shape,
            strict=True,
        ):
            object.__setattr__(self, field, value)


@dataclass(frozen=True)
class SubscriptionUpdate:
    """One optimistic correction, activation, or deactivation."""

    subscription_id: int
    expected_version: int
    name: str
    budget_category_id: int
    funding_wallet_id: int
    currency: str
    billing_cadence: SubscriptionCadence
    cadence_months: int
    billing_month: int | None
    billing_day: int | None
    active_from: date | None
    active_to: date | None
    is_active: bool

    def __post_init__(self) -> None:
        """Apply the creation rules, plus the rule that ending needs a date.

        Deactivating without saying from when would leave every historical
        month undecidable, so a deactivated subscription always carries the
        month its contribution stopped.
        """
        shape = _validated_subscription_shape(
            name=self.name,
            currency=self.currency,
            billing_cadence=self.billing_cadence,
            cadence_months=self.cadence_months,
            billing_month=self.billing_month,
            billing_day=self.billing_day,
            active_from=self.active_from,
            active_to=self.active_to,
        )
        for field, value in zip(
            (
                "name",
                "currency",
                "cadence_months",
                "billing_month",
                "billing_day",
                "active_from",
                "active_to",
            ),
            shape,
            strict=True,
        ):
            object.__setattr__(self, field, value)
        if not self.is_active and self.active_to is None:
            raise ValueError(
                "a deactivated subscription must name the month it stopped"
            )


@dataclass(frozen=True)
class Subscription:
    """Persisted canonical subscription without ORM state."""

    id: int
    name: str
    budget_category_id: int
    funding_wallet_id: int
    currency: str
    billing_cadence: SubscriptionCadence
    cadence_months: int
    billing_month: int | None
    billing_day: int | None
    active_from: date | None
    active_to: date | None
    is_active: bool
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class SubscriptionPricePeriodDraft:
    """One effective-dated price, half-open as ``[from, to)``."""

    subscription_id: int
    amount_minor: int
    effective_from: date
    effective_to: date | None = None

    def __post_init__(self) -> None:
        """A price may be zero but never negative, float, bool, or text."""
        object.__setattr__(
            self,
            "amount_minor",
            validate_minor_units(self.amount_minor, label="subscription price"),
        )
        start = validate_month_start(self.effective_from, label="effective from")
        end = validate_month_start(self.effective_to, label="effective to")
        if start is None:
            raise ValueError("effective from must be a calendar date")
        if end is not None and end <= start:
            raise ValueError("effective to must be later than effective from")
        object.__setattr__(self, "effective_from", start)
        object.__setattr__(self, "effective_to", end)


@dataclass(frozen=True)
class SubscriptionPricePeriodUpdate:
    """One optimistic correction of an existing effective-dated price."""

    price_period_id: int
    expected_version: int
    amount_minor: int
    effective_from: date
    effective_to: date | None

    def __post_init__(self) -> None:
        """Apply the same range and minor-unit rules as a new price."""
        object.__setattr__(
            self,
            "amount_minor",
            validate_minor_units(self.amount_minor, label="subscription price"),
        )
        start = validate_month_start(self.effective_from, label="effective from")
        end = validate_month_start(self.effective_to, label="effective to")
        if start is None:
            raise ValueError("effective from must be a calendar date")
        if end is not None and end <= start:
            raise ValueError("effective to must be later than effective from")
        object.__setattr__(self, "effective_from", start)
        object.__setattr__(self, "effective_to", end)


@dataclass(frozen=True)
class SubscriptionPricePeriod:
    """Persisted price valid over one half-open month range."""

    id: int
    subscription_id: int
    amount_minor: int
    effective_from: date
    effective_to: date | None
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class SubscriptionRequirementItem:
    """One subscription's audited contribution to one calendar month.

    Every field a later milestone would need to freeze this calculation is
    present, including the exact price period the amount came from and a
    stable reason code for a zero contribution.
    """

    subscription_id: int
    name: str
    currency: str
    year: int
    month: int
    funding_wallet_id: int
    funding_wallet_name: str
    budget_category_id: int
    budget_category_name: str
    cadence: SubscriptionCadence
    cadence_months: int
    cycle_position: int | None
    price_period_id: int | None
    cycle_price_minor: int | None
    contribution_minor: int
    is_active: bool
    active_from: date | None
    active_to: date | None
    effective_from: date | None
    effective_to: date | None
    reason: SubscriptionRequirementReason

    @property
    def label(self) -> str:
        """Return the ``yyyy-mm`` month this evidence was calculated for."""
        return f"{self.year:04d}-{self.month:02d}"

    @property
    def contributes(self) -> bool:
        """Return whether this month's funding requirement is above zero."""
        return self.contribution_minor > 0


@dataclass(frozen=True)
class SubscriptionRequirement:
    """One calendar month of funding requirement in exactly one currency."""

    year: int
    month: int
    currency: str
    items: tuple[SubscriptionRequirementItem, ...]

    def __post_init__(self) -> None:
        """Refuse to hold two currencies, so the total can never mix them."""
        object.__setattr__(self, "currency", normalize_currency(self.currency))
        for item in self.items:
            if item.currency != self.currency:
                raise ValueError(
                    "a subscription requirement holds exactly one currency"
                )

    @property
    def label(self) -> str:
        """Return the ``yyyy-mm`` label used by routes and the CLI."""
        return f"{self.year:04d}-{self.month:02d}"

    @property
    def total_minor(self) -> int:
        """Return the month's requirement; one currency only, by construction."""
        return sum(item.contribution_minor for item in self.items)


MAX_SALARY_NAME_LENGTH = 200

#: Version of the frozen snapshot documents a salary run stores. A later
#: milestone that needs a different shape adds a version rather than
#: reinterpreting rows written under this one.
SALARY_EVIDENCE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class SalaryPlanTemplateDraft:
    """Validated values used to create one salary distribution template."""

    name: str
    currency: str
    source_account_id: int

    def __post_init__(self) -> None:
        """Normalize the name and currency at the domain boundary."""
        object.__setattr__(
            self,
            "name",
            normalize_name(
                self.name, label="template name", max_length=MAX_SALARY_NAME_LENGTH
            ),
        )
        object.__setattr__(self, "currency", normalize_currency(self.currency))


@dataclass(frozen=True)
class SalaryPlanTemplateUpdate:
    """One optimistic rename or activation change of a template.

    ``currency`` and ``source_account_id`` are deliberately absent: once a
    template has an item or a run, moving it to another salary account would
    reinterpret every frozen line it has already produced (ADR-B042).
    """

    template_id: int
    expected_version: int
    name: str
    is_active: bool

    def __post_init__(self) -> None:
        """Apply the same name rule as a new template."""
        object.__setattr__(
            self,
            "name",
            normalize_name(
                self.name, label="template name", max_length=MAX_SALARY_NAME_LENGTH
            ),
        )


@dataclass(frozen=True)
class SalaryPlanTemplate:
    """Persisted current salary distribution template without ORM state."""

    id: int
    name: str
    currency: str
    source_account_id: int
    is_active: bool
    version: int
    created_at: datetime
    updated_at: datetime


def _validated_item_shape(
    *, calculation_mode: SalaryCalculationMode, fixed_amount_minor: int | None
) -> int | None:
    """Reject an item whose mode and stored amount disagree.

    A fixed line carries the amount it always asks for; a calculated or manual
    line must not, because a stale number left behind would silently become the
    amount a later month freezes.
    """
    if calculation_mode is SalaryCalculationMode.FIXED:
        if fixed_amount_minor is None:
            raise ValueError("a fixed salary item needs its amount")
        return validate_minor_units(fixed_amount_minor, label="fixed salary amount")
    if fixed_amount_minor is not None:
        raise ValueError(
            "only a fixed salary item stores an amount; a calculated or manual "
            "item decides its amount when the month is generated"
        )
    return None


@dataclass(frozen=True)
class SalaryPlanTemplateItemDraft:
    """Validated values used to add one destination to a template."""

    template_id: int
    name: str
    destination_wallet_id: int
    calculation_mode: SalaryCalculationMode
    budget_category_id: int | None = None
    fixed_amount_minor: int | None = None
    display_order: int = 0

    def __post_init__(self) -> None:
        """Reject an unusable mode, amount, or ordering up front."""
        object.__setattr__(
            self,
            "name",
            normalize_name(
                self.name, label="salary item name", max_length=MAX_SALARY_NAME_LENGTH
            ),
        )
        object.__setattr__(
            self,
            "fixed_amount_minor",
            _validated_item_shape(
                calculation_mode=self.calculation_mode,
                fixed_amount_minor=self.fixed_amount_minor,
            ),
        )
        object.__setattr__(
            self,
            "display_order",
            validate_minor_units(self.display_order, label="display order"),
        )


@dataclass(frozen=True)
class SalaryPlanTemplateItemUpdate:
    """One optimistic correction of an existing template item."""

    template_item_id: int
    expected_version: int
    name: str
    destination_wallet_id: int
    calculation_mode: SalaryCalculationMode
    budget_category_id: int | None
    fixed_amount_minor: int | None
    display_order: int
    is_active: bool

    def __post_init__(self) -> None:
        """Apply the same shape rules as a new item."""
        object.__setattr__(
            self,
            "name",
            normalize_name(
                self.name, label="salary item name", max_length=MAX_SALARY_NAME_LENGTH
            ),
        )
        object.__setattr__(
            self,
            "fixed_amount_minor",
            _validated_item_shape(
                calculation_mode=self.calculation_mode,
                fixed_amount_minor=self.fixed_amount_minor,
            ),
        )
        object.__setattr__(
            self,
            "display_order",
            validate_minor_units(self.display_order, label="display order"),
        )


@dataclass(frozen=True)
class SalaryPlanTemplateItem:
    """Persisted destination of one template, without ORM state."""

    id: int
    template_id: int
    name: str
    destination_wallet_id: int
    budget_category_id: int | None
    calculation_mode: SalaryCalculationMode
    fixed_amount_minor: int | None
    display_order: int
    is_active: bool
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ManualSalaryAmount:
    """One amount the user typed for a manual template item.

    A manual item has no stored amount, so the month cannot be generated
    without an explicit value. Zero is legal; a missing value is not, because
    defaulting it to zero would silently underfund a destination.
    """

    template_item_id: int
    amount_minor: int

    def __post_init__(self) -> None:
        """Reject float, bool, and negative manual entries at the boundary."""
        object.__setattr__(
            self,
            "amount_minor",
            validate_minor_units(self.amount_minor, label="manual salary amount"),
        )


@dataclass(frozen=True)
class SalarySubscriptionEvidence:
    """The whole subscription requirement one salary run froze.

    Every item B4 produced is kept, including the ones contributing zero and
    their reason codes, so a historical run explains itself without asking the
    register what it says today (ADR-B045).
    """

    schema_version: int
    year: int
    month: int
    currency: str
    items: tuple[SubscriptionRequirementItem, ...]

    def __post_init__(self) -> None:
        """Refuse to hold two currencies or an unknown schema version."""
        object.__setattr__(self, "currency", normalize_currency(self.currency))
        for item in self.items:
            if item.currency != self.currency:
                raise ValueError("a salary evidence snapshot holds one currency")

    @property
    def label(self) -> str:
        """Return the ``yyyy-mm`` month this evidence was frozen for."""
        return f"{self.year:04d}-{self.month:02d}"

    @property
    def total_minor(self) -> int:
        """Return the frozen month total; one currency only, by construction."""
        return sum(item.contribution_minor for item in self.items)

    def contributions_for_wallet(self, wallet_id: int) -> int:
        """Return what this snapshot asks one funding wallet to receive."""
        return sum(
            item.contribution_minor
            for item in self.items
            if item.funding_wallet_id == wallet_id
        )


@dataclass(frozen=True)
class FixedSalaryItemEvidence:
    """Why one fixed line asks for the amount it asks for."""

    schema_version: int
    template_item_id: int
    template_item_version: int
    fixed_amount_minor: int


@dataclass(frozen=True)
class ManualSalaryItemEvidence:
    """The exact amount a person typed for one manual line."""

    schema_version: int
    template_item_id: int
    template_item_version: int
    entered_amount_minor: int


@dataclass(frozen=True)
class SubscriptionSalaryItemEvidence:
    """Which frozen subscription contributions one line adds up.

    The per-subscription detail stays in the run-level snapshot, which is the
    authority; this records only which of them this line claimed.
    """

    schema_version: int
    template_item_id: int
    template_item_version: int
    year: int
    month: int
    currency: str
    destination_wallet_id: int
    subscription_ids: tuple[int, ...]
    contribution_total_minor: int


type SalaryItemEvidence = (
    FixedSalaryItemEvidence | ManualSalaryItemEvidence | SubscriptionSalaryItemEvidence
)


@dataclass(frozen=True)
class SalaryAllocationRunDraft:
    """One complete frozen generation offered for insertion.

    Every value is already decided when the draft is built, because a run is
    written once and never recalculated; only its status and fulfillment
    columns move afterwards.
    """

    template_id: int
    template_name_snapshot: str
    template_version: int
    budget_period_id: int
    salary_event_id: int
    source_account_id: int
    source_wallet_id: int
    currency: str
    generation: int
    salary_amount_minor: int
    planned_amount_minor: int
    remainder_minor: int
    subscription_evidence_schema_version: int
    subscription_evidence_json: str
    generated_at: datetime
    #: Set together, or not at all: a first generation replaces nothing, and a
    #: regeneration records both the draft it replaced and the fingerprint of
    #: the request that replaced it (ADR-B049).
    regenerated_from_run_id: int | None = None
    request_fingerprint: str | None = None

    def __post_init__(self) -> None:
        """Reject inexact money and a plan that does not add up."""
        object.__setattr__(self, "currency", normalize_currency(self.currency))
        object.__setattr__(
            self,
            "salary_amount_minor",
            validate_minor_units(self.salary_amount_minor, label="salary", minimum=1),
        )
        object.__setattr__(
            self,
            "planned_amount_minor",
            validate_minor_units(self.planned_amount_minor, label="planned amount"),
        )
        object.__setattr__(
            self,
            "remainder_minor",
            validate_minor_units(self.remainder_minor, label="remainder"),
        )
        if self.planned_amount_minor + self.remainder_minor != self.salary_amount_minor:
            raise ValueError("a salary run's plan and remainder must equal the salary")
        if (self.regenerated_from_run_id is None) != (self.request_fingerprint is None):
            raise ValueError(
                "a regenerated run records both the draft it replaced and the "
                "request that replaced it"
            )


@dataclass(frozen=True)
class SalaryAllocationRunItemDraft:
    """One complete frozen line offered for insertion."""

    run_id: int
    template_item_id: int
    template_item_version: int
    name_snapshot: str
    destination_wallet_id: int
    destination_account_id: int
    destination_wallet_name_snapshot: str
    destination_account_name_snapshot: str
    budget_category_id: int | None
    budget_category_name_snapshot: str | None
    calculation_mode: SalaryCalculationMode
    amount_minor: int
    currency: str
    display_order: int
    fulfillment_mode: CommitmentFulfillmentMode
    calculation_evidence_json: str

    def __post_init__(self) -> None:
        """A frozen line holds an exact, non-negative amount in one currency."""
        object.__setattr__(self, "currency", normalize_currency(self.currency))
        object.__setattr__(
            self,
            "amount_minor",
            validate_minor_units(self.amount_minor, label="salary item amount"),
        )
        object.__setattr__(
            self,
            "display_order",
            validate_minor_units(self.display_order, label="display order"),
        )


@dataclass(frozen=True)
class SalaryAllocationRun:
    """Persisted frozen distribution of one salary into one calendar month."""

    id: int
    template_id: int
    template_name_snapshot: str
    template_version: int
    budget_period_id: int
    salary_event_id: int
    source_account_id: int
    source_wallet_id: int
    currency: str
    generation: int
    salary_amount_minor: int
    planned_amount_minor: int
    remainder_minor: int
    subscription_evidence_schema_version: int
    subscription_evidence_json: str
    status: SalaryRunStatus
    approval_entry_id: int | None
    regenerated_from_run_id: int | None
    request_fingerprint: str | None
    generated_at: datetime
    approved_at: datetime | None
    cancelled_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime

    @property
    def is_live(self) -> bool:
        """Return whether this generation still owns its template and month."""
        return self.status is not SalaryRunStatus.CANCELLED


@dataclass(frozen=True)
class SalaryAllocationRunItem:
    """Persisted frozen line of one salary run.

    Every name is a snapshot rather than a join, so renaming a wallet, an
    account, or a category later cannot rewrite what this month said.
    """

    id: int
    run_id: int
    template_item_id: int
    template_item_version: int
    name_snapshot: str
    destination_wallet_id: int
    destination_account_id: int
    destination_wallet_name_snapshot: str
    destination_account_name_snapshot: str
    budget_category_id: int | None
    budget_category_name_snapshot: str | None
    calculation_mode: SalaryCalculationMode
    amount_minor: int
    currency: str
    display_order: int
    fulfillment_mode: CommitmentFulfillmentMode
    status: SalaryRunItemStatus
    calculation_evidence_json: str
    transfer_entry_id: int | None
    transferred_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime

    @property
    def reserves_source_wallet(self) -> bool:
        """Return whether this line still holds money back in the source."""
        return self.status is SalaryRunItemStatus.WAITING_TRANSFER


MAX_SCHEDULE_ERROR_CODE_LENGTH = 64
MAX_SCHEDULE_ERROR_MESSAGE_LENGTH = 200
MAX_LEASE_OWNER_LENGTH = 64
MAX_IDEMPOTENCY_KEY_LENGTH = 200


def validate_aware(value: datetime, *, label: str) -> datetime:
    """Return a timezone-aware instant, or refuse to guess an offset.

    A naive instant would be read as UTC by the database adapter and as local
    time by a person, and the two differ by eight hours here.
    """
    if isinstance(value, bool) or not isinstance(value, datetime):
        raise TypeError(f"{label} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value


def validate_wall_clock(value: time, *, label: str) -> time:
    """Return a whole-minute local time carrying no offset of its own.

    A schedule's time is a wall-clock policy read through its stored timezone
    (ADR-B050). A ``time`` with its own ``tzinfo`` would carry a second,
    contradictory answer, and seconds would imply a precision the policy does
    not have.
    """
    if not isinstance(value, time):
        raise TypeError(f"{label} must be a time of day")
    if value.tzinfo is not None:
        raise ValueError(f"{label} is a wall-clock time and carries no timezone")
    if value.second or value.microsecond:
        raise ValueError(f"{label} must name a whole minute")
    return value


def validate_calendar_date(value: date, *, label: str) -> date:
    """Return a calendar date, refusing a ``datetime`` wearing its clothes."""
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError(f"{label} must be a calendar date")
    return value


@dataclass(frozen=True)
class ScheduledTransferDraft:
    """One complete assumed bank movement offered for insertion.

    Every economic value is decided before the row exists, because it freezes
    on insert (ADR-B053); only lifecycle, lease, error, and timestamp columns
    move afterwards.
    """

    purpose: ScheduledTransferPurpose
    source_account_id: int
    source_wallet_id: int
    destination_account_id: int
    amount_minor: int
    currency: str
    scheduled_local_date: date
    scheduled_local_time: time
    timezone: str
    due_at_utc: datetime
    idempotency_key: str
    destination_wallet_id: int | None = None
    card_id: int | None = None
    execution_policy: ScheduledExecutionPolicy = (
        ScheduledExecutionPolicy.ASSUME_AT_DUE_TIME
    )

    def __post_init__(self) -> None:
        """Reject inexact money, a naive instant, and a contradictory shape."""
        object.__setattr__(self, "currency", normalize_currency(self.currency))
        object.__setattr__(
            self,
            "amount_minor",
            validate_minor_units(
                self.amount_minor, label="scheduled transfer amount", minimum=1
            ),
        )
        object.__setattr__(
            self,
            "scheduled_local_date",
            validate_calendar_date(self.scheduled_local_date, label="scheduled date"),
        )
        object.__setattr__(
            self,
            "scheduled_local_time",
            validate_wall_clock(self.scheduled_local_time, label="scheduled time"),
        )
        object.__setattr__(
            self, "due_at_utc", validate_aware(self.due_at_utc, label="due time")
        )
        if not self.timezone.strip():
            raise ValueError("a schedule names the timezone its time is read in")
        if not self.idempotency_key.strip():
            raise ValueError("a schedule needs a client idempotency token")
        if self.source_account_id == self.destination_account_id:
            raise ValueError("a transfer needs two different accounts")
        if self.purpose is ScheduledTransferPurpose.CLEARING_FUNDING:
            if self.card_id is not None:
                raise ValueError("a clearing transfer does not name a card")
            if self.destination_wallet_id is None:
                raise ValueError("a clearing transfer needs a destination wallet")
        else:
            if self.card_id is None:
                raise ValueError("a card autopay names the card it pays")
            if self.destination_wallet_id is not None:
                raise ValueError("a card liability holds no wallet")


@dataclass(frozen=True)
class ScheduledTransfer:
    """Persisted assumed bank movement without ORM state."""

    id: int
    purpose: ScheduledTransferPurpose
    source_account_id: int
    source_wallet_id: int
    destination_account_id: int
    destination_wallet_id: int | None
    card_id: int | None
    amount_minor: int
    currency: str
    scheduled_local_date: date
    scheduled_local_time: time
    timezone: str
    due_at_utc: datetime
    execution_policy: ScheduledExecutionPolicy
    status: ScheduledTransferStatus
    idempotency_key: str
    executed_entry_id: int | None
    reversal_entry_id: int | None
    lease_owner: str | None
    lease_expires_at: datetime | None
    attempt_count: int
    last_attempt_at: datetime | None
    next_attempt_at: datetime | None
    processed_at: datetime | None
    cancelled_at: datetime | None
    reversed_at: datetime | None
    last_error_code: str | None
    last_error_message: str | None
    version: int
    created_at: datetime
    updated_at: datetime

    @property
    def is_active(self) -> bool:
        """Return whether this schedule still intends to move real money.

        A failed schedule is active: the instruction the bank was given has not
        been withdrawn, so the money stays reserved until it is cancelled
        (ADR-B059).
        """
        return self.status in _ACTIVE_SCHEDULE_STATUSES

    @property
    def is_executed(self) -> bool:
        """Return whether the ledger already records this movement."""
        return self.status is ScheduledTransferStatus.ASSUMED_EXECUTED


@dataclass(frozen=True)
class ScheduledTransferCommitmentDraft:
    """One commitment share that a scheduled movement will settle."""

    scheduled_transfer_id: int
    funding_commitment_id: int
    amount_minor: int

    def __post_init__(self) -> None:
        """A covered share is a positive, exact integer amount."""
        object.__setattr__(
            self,
            "amount_minor",
            validate_minor_units(
                self.amount_minor, label="covered commitment amount", minimum=1
            ),
        )


@dataclass(frozen=True)
class ScheduledTransferCommitment:
    """Persisted immutable link between one schedule and one commitment."""

    scheduled_transfer_id: int
    funding_commitment_id: int
    amount_minor: int
    created_at: datetime


#: Schedules whose money has not moved but is still promised to a bank.
_ACTIVE_SCHEDULE_STATUSES = frozenset(
    {
        ScheduledTransferStatus.SCHEDULED,
        ScheduledTransferStatus.PROCESSING,
        ScheduledTransferStatus.FAILED,
    }
)


MAX_SNAPSHOT_NOTE_LENGTH = 200


@dataclass(frozen=True)
class BalanceSnapshotDraft:
    """One observation of what a bank account actually held, offered once.

    The balance is signed and may be zero (ADR-B066): an account can be empty
    or overdrawn, and a snapshot records what was seen rather than what the
    ledger expected.
    """

    financial_account_id: int
    observed_balance_minor: int
    observed_at: datetime
    idempotency_key: str
    source: BalanceSnapshotSource = BalanceSnapshotSource.MANUAL_BANK_APP
    note: str | None = None

    def __post_init__(self) -> None:
        """Reject float, bool, a naive instant, and an unbounded note."""
        object.__setattr__(
            self,
            "observed_balance_minor",
            validate_signed_minor_units(
                self.observed_balance_minor, label="observed balance"
            ),
        )
        object.__setattr__(
            self, "observed_at", validate_aware(self.observed_at, label="observed time")
        )
        object.__setattr__(
            self,
            "note",
            normalize_note(
                self.note, label="snapshot note", max_length=MAX_SNAPSHOT_NOTE_LENGTH
            ),
        )
        if not self.idempotency_key.strip():
            raise ValueError("a snapshot needs a client idempotency token")


@dataclass(frozen=True)
class BalanceSnapshot:
    """Persisted immutable observation of one account's real balance."""

    id: int
    financial_account_id: int
    observed_balance_minor: int
    observed_at: datetime
    source: BalanceSnapshotSource
    note: str | None
    idempotency_key: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class FundingCommitment:
    """Persisted reservation; it never moves money on its own."""

    id: int
    financial_event_id: int
    event_allocation_id: int
    source_wallet_id: int
    settlement_account_id: int
    amount_minor: int
    released_minor: int
    currency: str
    fulfillment_mode: CommitmentFulfillmentMode
    status: CommitmentStatus
    created_at: datetime
    updated_at: datetime

    @property
    def outstanding_minor(self) -> int:
        """Return the still-reserved amount after any release."""
        return self.amount_minor - self.released_minor

"""Closed value sets for the local accounting ledger."""

from enum import StrEnum


class FinancialAccountType(StrEnum):
    """Kind of real-world balance container held at an institution."""

    CHECKING = "checking"
    SAVINGS = "savings"
    CASH = "cash"
    CARD_LIABILITY = "card_liability"
    OTHER = "other"


class WalletPurpose(StrEnum):
    """Role of one virtual envelope inside a physical asset account."""

    GENERAL = "general"
    BUDGET = "budget"
    CLEARING_HUB = "clearing_hub"
    UNALLOCATED = "unallocated"


class LedgerAccountKind(StrEnum):
    """Double-entry classification of a ledger account."""

    ASSET = "asset"
    LIABILITY = "liability"
    EXPENSE = "expense"
    INCOME = "income"
    EQUITY = "equity"
    CLEARING = "clearing"


class CardHolderRole(StrEnum):
    """Whether a locally configured card is primary or supplementary."""

    PRIMARY = "primary"
    SUPPLEMENTARY = "supplementary"
    UNKNOWN = "unknown"


class CardImportAssignmentKind(StrEnum):
    """Which reviewed B9 hint a card has explicitly adopted."""

    CARD_CYCLE = "card_cycle"
    SCHEDULED_TRANSFER_REFERENCE = "scheduled_transfer_reference"


class CardConfigurationStatus(StrEnum):
    """Whether a card has the accounts required before it can post."""

    INCOMPLETE = "incomplete"
    READY = "ready"


class FinancialEventOrigin(StrEnum):
    """Where a canonical economic event came from."""

    GMAIL_TRANSACTION = "gmail_transaction"
    MANUAL = "manual"
    IMPORT = "import"
    SYSTEM = "system"


class PaymentMethod(StrEnum):
    """How an economic event was paid."""

    CASH = "cash"
    CREDIT_CARD = "credit_card"
    BANK_ACCOUNT = "bank_account"
    TRANSFER = "transfer"
    OTHER = "other"


class RecognitionBasis(StrEnum):
    """Evidence that caused an event to be recognized."""

    OBSERVED_MANUAL = "observed_manual"
    GMAIL_NOTIFICATION = "gmail_notification"
    IMPORTED = "imported"
    SCHEDULED_ASSUMPTION = "scheduled_assumption"


class FinancialEventType(StrEnum):
    """Economic nature of one canonical event.

    The amount is always a positive magnitude; direction comes from this type.
    """

    PURCHASE = "purchase"
    REFUND = "refund"
    TRANSFER = "transfer"
    WITHDRAWAL = "withdrawal"
    DEPOSIT = "deposit"
    BILL_PAYMENT = "bill_payment"
    SUBSCRIPTION = "subscription"
    FEE = "fee"
    INTEREST_INCOME = "interest_income"
    DIRECT_DEBIT = "direct_debit"
    ADJUSTMENT = "adjustment"


class FinancialEventStatus(StrEnum):
    """Review and posting lifecycle of one canonical event."""

    DRAFT = "draft"
    NEEDS_ALLOCATION = "needs_allocation"
    READY = "ready"
    POSTED = "posted"
    IGNORED = "ignored"
    REVERSED = "reversed"


class SourceLinkStatus(StrEnum):
    """Review state of one event-to-parsed-transaction link."""

    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class SourceLinkReason(StrEnum):
    """Stable, non-sensitive code explaining why a source link exists.

    Reason codes never contain amounts, merchant evidence, card numbers, or
    Email content. They are persisted so review decisions stay auditable
    without a second copy of the source observation.
    """

    DIRECT_GMAIL_INTAKE = "direct_gmail_intake"
    CARD_UNRESOLVED = "card_unresolved"
    CARD_AMBIGUOUS = "card_ambiguous"
    CARD_NOT_CONFIGURED = "card_not_configured"
    EXACT_CARD = "exact_card"
    COMPATIBLE_EVENT_TYPE = "compatible_event_type"
    TIME_WITHIN_24H = "time_within_24h"
    TIME_WITHIN_72H = "time_within_72h"
    EXACT_MERCHANT = "exact_merchant"
    TOKEN_MERCHANT = "token_merchant"
    #: B7 provenance decisions. These say *who* decided, not what was compared,
    #: so a scored comparison can never write one of them by itself.
    AUTO_EXACT_MATCH = "auto_exact_match"
    USER_CONFIRMED_MATCH = "user_confirmed_match"
    USER_REJECTED_MATCH = "user_rejected_match"
    USER_DECLARED_SEPARATE = "user_declared_separate"
    SOURCE_CHANGED = "source_changed"


class SourceDiscrepancy(StrEnum):
    """Stable code naming one field whose source observation later changed."""

    AMOUNT = "amount_minor"
    CURRENCY = "currency"
    MERCHANT = "merchant"
    OCCURRED_AT = "occurred_at"
    EVENT_TYPE = "event_type"
    CARD = "card"
    SOURCE_NO_LONGER_CONFIRMED = "source_no_longer_confirmed"


class IntakeSkipReason(StrEnum):
    """Stable code explaining why one transaction was not staged."""

    NOT_FOUND = "not_found"
    NOT_CONFIRMED = "not_confirmed"
    MANUALLY_IGNORED = "manually_ignored"
    SUPERSEDED = "superseded"
    SOURCE_DELETED = "source_deleted"
    MISSING_AMOUNT = "missing_amount"
    MISSING_CURRENCY = "missing_currency"
    MISSING_OCCURRED_AT = "missing_occurred_at"
    UNSUPPORTED_TRANSACTION_TYPE = "unsupported_transaction_type"
    NON_POSITIVE_AMOUNT = "non_positive_amount"
    ALREADY_STAGED = "already_staged"


class JournalEntryType(StrEnum):
    """Reason one immutable balanced entry was posted.

    Since revision 0007 these values are rows in the ``journal_entry_types``
    reference table (ADR-B028). A later milestone adds a type by inserting one
    row and extending this enum; values are never invented at runtime.
    """

    OPENING_BALANCE = "opening_balance"
    INTERNAL_TRANSFER = "internal_transfer"
    ADJUSTMENT = "adjustment"
    REVERSAL = "reversal"
    EXPENSE = "expense"
    REFUND = "refund"
    BUDGET_CLOSE = "budget_close"
    SALARY_RECEIPT = "salary_receipt"
    FUNDING_TRANSFER = "funding_transfer"
    CARD_PAYMENT = "card_payment"
    INCOME = "income"
    EXTERNAL_MOVEMENT = "external_movement"


class BalanceSnapshotSource(StrEnum):
    """Where one observed bank balance was read from.

    B7 has exactly one source: a person reading their bank's own app. A later
    milestone that reads a balance from an API adds a value here rather than
    reinterpreting rows written under this one.
    """

    MANUAL_BANK_APP = "manual_bank_app"


class BankMovementKind(StrEnum):
    """Which posting shape one already-observed bank movement uses.

    Every kind creates a canonical manual event (ADR-B065); the kind decides
    which accounts move, not whether the movement is real. ``adjustment`` is
    deliberately absent: it belongs to reconciliation alone.
    """

    INTEREST_INCOME = "interest_income"
    EXTERNAL_DEPOSIT = "external_deposit"
    EXTERNAL_WITHDRAWAL = "external_withdrawal"
    CASH_WITHDRAWAL_SPENDING = "cash_withdrawal_spending"
    DIRECT_DEBIT = "direct_debit"
    FEE = "fee"
    BILL_PAYMENT = "bill_payment"


class BudgetPeriodStatus(StrEnum):
    """Whether a calendar month still accepts postings against its budget."""

    OPEN = "open"
    CLOSED = "closed"


class AllocationSource(StrEnum):
    """How one allocation was decided."""

    RULE = "rule"
    MANUAL = "manual"
    IMPORT = "import"


class CommitmentFulfillmentMode(StrEnum):
    """How reserved money must reach the card's settlement account.

    The mode is decided from account identity, never by comparing bank display
    names (ADR-B029).
    """

    RESERVE_ONLY = "reserve_only"
    ACCOUNT_TRANSFER = "account_transfer"
    CROSS_BANK_TRANSFER = "cross_bank_transfer"


class SubscriptionCadence(StrEnum):
    """How often one subscription is actually charged."""

    MONTHLY = "monthly"
    YEARLY = "yearly"
    CUSTOM = "custom"


class SubscriptionRequirementReason(StrEnum):
    """Stable code explaining one month's contribution, including a zero.

    Every requirement item carries exactly one of these, so a subscription
    that contributes nothing still explains itself instead of disappearing
    from the page or from historical evidence.
    """

    MONTHLY_PRICE = "monthly_price"
    CYCLE_SHARE = "cycle_share"
    ZERO_PRICE = "zero_price"
    NO_PRICE_FOR_MONTH = "no_price_for_month"
    NOT_YET_ACTIVE = "not_yet_active"
    NO_LONGER_ACTIVE = "no_longer_active"


class SalaryCalculationMode(StrEnum):
    """How one salary template item decides the amount it asks for."""

    FIXED = "fixed"
    SUBSCRIPTION_REQUIREMENT = "subscription_requirement"
    MANUAL = "manual"


class SalaryRunStatus(StrEnum):
    """Lifecycle of one month's generated salary distribution.

    ``approved`` means every non-zero item is still waiting for a real bank
    transfer; ``funded`` means none of them is.
    """

    DRAFT = "draft"
    APPROVED = "approved"
    PARTIALLY_FUNDED = "partially_funded"
    FUNDED = "funded"
    CANCELLED = "cancelled"


class SalaryRunItemStatus(StrEnum):
    """Lifecycle of one frozen line of a salary run.

    ``waiting_transfer`` is itself the reservation a cross-account salary line
    holds against its source wallet (ADR-B043); no separate commitment row
    exists for it.
    """

    DRAFT = "draft"
    NOT_REQUIRED = "not_required"
    ALLOCATED = "allocated"
    WAITING_TRANSFER = "waiting_transfer"
    TRANSFERRED = "transferred"


class ScheduledTransferPurpose(StrEnum):
    """Which of the two posting shapes one scheduled movement uses.

    The lifecycle is shared, the entry is not (ADR-B051): clearing funding
    moves an asset wallet into another asset wallet, and card autopay moves
    asset wallets against a card liability.
    """

    CLEARING_FUNDING = "clearing_funding"
    CARD_AUTOPAY = "card_autopay"


class ScheduledTransferStatus(StrEnum):
    """Lifecycle of one assumed bank movement.

    ``assumed_executed`` is a local policy conclusion, never a bank
    confirmation (ADR-B050). ``failed`` still holds the money back, because the
    instruction the user gave the bank has not been withdrawn.
    """

    SCHEDULED = "scheduled"
    PROCESSING = "processing"
    ASSUMED_EXECUTED = "assumed_executed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REVERSED = "reversed"


class ScheduledExecutionPolicy(StrEnum):
    """How a schedule decides that it has happened.

    Only one policy exists: at the stored due instant, Scribe assumes the
    movement took place. A later policy that waits for observed evidence would
    be added here rather than by reinterpreting these rows.
    """

    ASSUME_AT_DUE_TIME = "assume_at_due_time"


class CommitmentStatus(StrEnum):
    """Lifecycle of one funding commitment; never its fulfillment path.

    ``partially_scheduled`` is accepted from the start so a later scheduling
    milestone does not have to migrate this financial table again.
    """

    OPEN = "open"
    PARTIALLY_SCHEDULED = "partially_scheduled"
    SCHEDULED = "scheduled"
    FUNDED = "funded"
    CANCELLED = "cancelled"

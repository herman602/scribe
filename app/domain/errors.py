"""Stable, user-recognizable application error taxonomy."""


class ScribeError(Exception):
    """Base class for user-recognizable application failures."""

    retryable = False


class SystemServiceError(ScribeError):
    """A local operating-system service could not be managed safely."""

    def __init__(self, message: str, *, action: str) -> None:
        super().__init__(message)
        self.action = action


class DatabaseError(ScribeError):
    """A database operation failed."""


class MigrationError(DatabaseError):
    """The database schema could not be migrated or inspected."""


class ConcurrentUpdateError(DatabaseError):
    """An optimistic concurrency check rejected stale state."""

    retryable = True


class GmailAuthenticationError(ScribeError):
    """OAuth credentials are missing, invalid, expired, or revoked."""


class GmailRateLimitError(ScribeError):
    """Gmail rejected a request due to a retryable quota or rate limit."""

    retryable = True

    def __init__(
        self, message: str, *, retry_after_seconds: float | None = None
    ) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class GmailConnectionError(ScribeError):
    """Gmail could not be reached or returned an unusable response."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class GmailMessageFetchError(ScribeError):
    """One Gmail message, thread, or attachment could not be fetched."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class GmailMessageNotFoundError(GmailMessageFetchError):
    """A history event referenced a message that no longer exists."""


class GmailLabelError(ScribeError):
    """A Gmail user-label lookup, creation, or message mutation failed."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class GmailHistoryExpiredError(ScribeError):
    """Gmail no longer accepts the committed mailbox history cursor."""


class EmailDecodeError(ScribeError):
    """A Gmail MIME payload could not be decoded safely."""


class ParserError(ScribeError):
    """A transaction parser failed without producing partial candidates."""


class PdfPasswordRequiredError(ScribeError):
    """An encrypted PDF has no locally configured password."""


class PdfDecryptionError(ScribeError):
    """An encrypted PDF password or encryption method was rejected."""


class PdfExtractionError(ScribeError):
    """A local PDF could not be parsed or produced no usable text."""


class AttachmentLimitError(PdfExtractionError):
    """A PDF exceeded configured byte, page, text, or time limits."""


class SecretStoreError(ScribeError):
    """The OS credential store is unavailable or rejected an operation."""


class SenderRuleConfigurationError(ScribeError):
    """Sender rules are absent, ambiguous, or unsafe."""


class SyncAlreadyRunningError(ScribeError):
    """Another process currently owns the mailbox sync lease."""


class SyncInterruptedError(ScribeError):
    """A foreground sync stopped after preserving its durable checkpoint."""


class SyncCancelledError(ScribeError):
    """A cooperative cancellation request stopped sync at a safe boundary."""


class InitialSyncRequiredError(ScribeError):
    """Incremental sync cannot start before a complete initial baseline."""


class LedgerError(ScribeError):
    """A double-entry accounting invariant rejected a requested change."""


class UnbalancedJournalError(LedgerError):
    """Journal postings do not net to zero in every currency."""


class CurrencyMismatchError(LedgerError):
    """A posting currency differs from its ledger account currency."""


class WalletAssignmentError(LedgerError):
    """A posting wallet is missing, inactive, or held by another account."""


class LedgerConfigurationError(LedgerError):
    """An account, wallet, or ledger account is missing or misconfigured."""


class IdempotencyConflictError(LedgerError):
    """A journal idempotency key already describes a different operation."""


class InvalidReversalError(LedgerError):
    """A reversal does not exactly cancel the entry it claims to reverse."""


class CardConfigurationError(LedgerError):
    """A card's issuer identity or settlement configuration is unusable."""


class FinancialEventError(LedgerError):
    """A canonical financial event could not be created or refreshed."""


class PreOpeningEventError(FinancialEventError):
    """An event already represented by the opening position cannot be posted."""


class SourceLinkError(LedgerError):
    """A parsed transaction cannot be linked to this financial event."""


class IncompleteAllocationError(LedgerError):
    """Allocations do not cover their financial event exactly."""


class UnsupportedEventTypeError(LedgerError):
    """Raised when an event's direction has no posting rule yet."""


class BudgetPeriodError(LedgerError):
    """A budget period, target, or close request is not usable."""


class SubscriptionError(LedgerError):
    """A subscription register change is missing, unusable, or unsafe."""


class SubscriptionCadenceError(SubscriptionError):
    """A cadence cannot place one calendar month inside its billing cycle."""


class SubscriptionOverlapError(SubscriptionError):
    """Two records of one subscription would claim the same month.

    Raised both for two price periods of one subscription and for two
    activity intervals of one service, because either would make a month
    resolve to more than one amount.
    """


class OpeningPositionError(LedgerError):
    """An opening balance and its wallet split do not agree."""


class SalaryAllocationError(LedgerError):
    """A salary template, run, or run item change is unusable or unsafe."""


class SalaryPlanError(SalaryAllocationError):
    """A salary template or template item is missing or misconfigured."""


class SalaryEventError(SalaryAllocationError):
    """A financial event cannot serve as the salary a run distributes."""


class SalaryEvidenceError(SalaryAllocationError):
    """A frozen salary snapshot is missing, malformed, or not integer money."""


class InsufficientAvailableFundsError(SalaryAllocationError):
    """A wallet cannot cover a plan once existing reservations are counted."""


class ManualExpenseError(LedgerError):
    """A hand-entered cash or card expense is incomplete or unusable."""


class BankMovementError(LedgerError):
    """An already-observed bank movement cannot be recorded as described."""


class WalletReallocationError(LedgerError):
    """An edited wallet cell does not describe a move inside one account."""


class DisplayOrderingError(LedgerError):
    """A submitted arrangement does not describe the axis it claims to."""


class EventReversalError(LedgerError):
    """A financial event is not in a state that may be reversed."""


class AllocationRedoError(LedgerError):
    """A financial event's allocation is not in a state that may be redone."""


class BalanceSnapshotError(LedgerError):
    """An observed bank balance is missing, malformed, or not this one."""


class ReconciliationError(LedgerError):
    """A reconciliation difference cannot be adjusted as requested."""


class StaleReconciliationError(ReconciliationError):
    """The difference changed after the reconciliation page was loaded."""


class ScheduledTransferError(LedgerError):
    """A scheduled bank movement could not be created, moved, or executed."""


class ScheduledTransferShapeError(ScheduledTransferError):
    """A schedule's accounts, wallets, card, or coverage do not agree."""


class ScheduledTransferStateError(ScheduledTransferError):
    """A schedule's lifecycle does not allow the requested transition."""


class ScheduledTransferTimeError(ScheduledTransferError):
    """A local due time does not exist, or exists twice, in its timezone."""


class ScheduledTransferCoverageError(ScheduledTransferError):
    """A schedule would cover more of a commitment than it still requires."""


class ScheduledTransferOverdueError(ScheduledTransferError):
    """A schedule would be created already due, so it would run immediately."""


class CutoverImportError(LedgerError):
    """A reviewed local cutover cannot be applied safely."""

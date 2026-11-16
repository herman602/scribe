"""Closed domain value sets persisted by the application."""

from enum import StrEnum


class BodyRetentionPolicy(StrEnum):
    """Supported email body retention policies."""

    FULL_TEXT = "full_text"
    SUMMARY_ONLY = "summary_only"
    PURGE_AFTER_PARSE = "purge_after_parse"


class ClassificationStatus(StrEnum):
    """Final and stage-level transaction classifications."""

    CONFIRMED = "confirmed"
    LIKELY = "likely"
    UNCERTAIN = "uncertain"
    NOT_TRANSACTION = "not_transaction"
    IGNORED = "ignored"


class ProcessingStatus(StrEnum):
    """Email processing lifecycle."""

    PENDING = "pending"
    PROCESSED = "processed"
    FAILED = "failed"


class EmailSourceStatus(StrEnum):
    """Whether the source message still exists in Gmail."""

    ACTIVE = "active"
    DELETED = "deleted"


class TransactionSourceStatus(StrEnum):
    """Whether a parsed transaction remains present in latest parser output."""

    ACTIVE = "active"
    SUPERSEDED = "superseded"


class TransactionType(StrEnum):
    """Supported transaction categories."""

    PURCHASE = "purchase"
    REFUND = "refund"
    TRANSFER = "transfer"
    WITHDRAWAL = "withdrawal"
    DEPOSIT = "deposit"
    BILL_PAYMENT = "bill_payment"
    SUBSCRIPTION = "subscription"
    FEE = "fee"
    UNKNOWN = "unknown"


class SenderMatchType(StrEnum):
    """Supported sender-rule matching strategies."""

    EXACT_ADDRESS = "exact_address"
    DOMAIN_SUFFIX = "domain_suffix"


class AttachmentStatus(StrEnum):
    """Attachment extraction lifecycle."""

    PENDING = "pending"
    EXTRACTED = "extracted"
    PASSWORD_REQUIRED = "password_required"
    WRONG_PASSWORD = "wrong_password"
    UNSUPPORTED_ENCRYPTION = "unsupported_encryption"
    IMAGE_ONLY = "image_only"
    TOO_LARGE = "too_large"
    FAILED = "failed"


class FullSyncStatus(StrEnum):
    """Durable full-sync checkpoint state."""

    IDLE = "idle"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class SyncMode(StrEnum):
    """Mailbox synchronization modes."""

    FULL = "full"
    INCREMENTAL = "incremental"
    RECONCILIATION = "reconciliation"


class RunStatus(StrEnum):
    """Long-running operation state."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SyncItemOperation(StrEnum):
    """Per-message synchronization operation."""

    FETCH = "fetch"
    UPDATE = "update"
    DELETE = "delete"


class SyncItemStatus(StrEnum):
    """Per-message work-item state."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class ParserTrigger(StrEnum):
    """Reason a parser run was started."""

    SYNC = "sync"
    MANUAL = "manual"
    RETRY = "retry"
    VERSION_UPGRADE = "version_upgrade"


class ParserRunStatus(StrEnum):
    """Parser attempt state."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class GmailLabelOperationStatus(StrEnum):
    """Durable state for one idempotent Gmail Label mutation."""

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"

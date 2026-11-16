"""Immutable records crossing the database infrastructure boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.domain.enums import (
    AttachmentStatus,
    BodyRetentionPolicy,
    ClassificationStatus,
    EmailSourceStatus,
    FullSyncStatus,
    GmailLabelOperationStatus,
    ProcessingStatus,
    RunStatus,
    SenderMatchType,
    SyncItemOperation,
    SyncItemStatus,
    SyncMode,
    TransactionSourceStatus,
    TransactionType,
)
from app.domain.types import JsonValue


@dataclass(frozen=True)
class GmailAccount:
    """A Gmail profile known to the local database."""

    id: int
    profile_email: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class SenderRule:
    """A mutable, account-scoped sender allowlist entry."""

    id: int
    gmail_account_id: int
    institution_key: str
    institution_name: str
    match_type: SenderMatchType
    match_value_normalized: str
    parser_name: str | None
    priority: int
    enabled: bool
    gmail_query_enabled: bool
    version: int
    created_at: datetime
    updated_at: datetime
    pdf_password_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class SenderRuleDraft:
    """Validated values used to create or update a sender rule."""

    gmail_account_id: int
    institution_key: str
    institution_name: str
    match_type: SenderMatchType
    match_value: str
    parser_name: str | None = None
    priority: int = 100
    gmail_query_enabled: bool = True


@dataclass(frozen=True)
class SupplementaryCardSetting:
    """A local bank and card-suffix pair explicitly marked as supplementary."""

    id: int
    gmail_account_id: int
    institution_key: str
    card_last_four: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class EmailSnapshot:
    """Decoded Gmail message values accepted for persistence."""

    gmail_account_id: int
    gmail_message_id: str
    gmail_thread_id: str
    gmail_history_id: str
    sender: str
    recipients: tuple[str, ...]
    subject: str
    gmail_internal_date_ms: int
    snippet: str = ""
    received_at: datetime | None = None
    body_text: str | None = None
    body_summary: str | None = None
    has_html_body: bool = False
    labels: tuple[str, ...] = ()
    matched_sender_rule_id: int | None = None
    body_policy_applied: BodyRetentionPolicy = BodyRetentionPolicy.FULL_TEXT


@dataclass(frozen=True)
class EmailMessage:
    """Persisted email record without ORM state."""

    id: int
    gmail_account_id: int
    gmail_message_id: str
    gmail_thread_id: str
    gmail_history_id: str
    sender: str
    recipients: tuple[str, ...]
    subject: str
    received_at: datetime | None
    gmail_internal_date_ms: int
    snippet: str
    body_text: str | None
    body_summary: str | None
    has_html_body: bool
    labels: tuple[str, ...]
    matched_sender_rule_id: int | None
    body_policy_applied: BodyRetentionPolicy
    classification_status: ClassificationStatus
    classification_confidence: Decimal | None
    classification_reason: str | None
    parser_name: str | None
    parser_version: str | None
    processing_status: ProcessingStatus
    source_status: EmailSourceStatus
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class EmailUpsertOutcome:
    """Result of an idempotent Gmail-message upsert."""

    email: EmailMessage
    created: bool


@dataclass(frozen=True)
class AttachmentSnapshot:
    """Attachment metadata discovered while decoding one Gmail message."""

    mime_part_id: str
    filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    is_pdf: bool
    gmail_attachment_id: str | None = None


@dataclass(frozen=True)
class ProcessedAttachmentSnapshot:
    """Attachment metadata plus bounded local extraction output."""

    attachment: AttachmentSnapshot
    is_encrypted: bool
    extraction_status: AttachmentStatus
    extracted_text: str | None = None
    raw_encrypted_content: bytes | None = None
    extractor_name: str | None = None
    extractor_version: str | None = None
    warnings: tuple[JsonValue, ...] = ()
    error_code: str | None = None
    error_message: str | None = None
    password_ref_used: str | None = None


@dataclass(frozen=True)
class EmailAttachment:
    """Persisted attachment metadata; plaintext bytes are never retained."""

    id: int
    email_id: int
    gmail_attachment_id: str | None
    mime_part_id: str
    filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    is_pdf: bool
    is_encrypted: bool
    extraction_status: AttachmentStatus
    extracted_text: str | None
    raw_encrypted_content: bytes | None
    extractor_name: str | None
    extractor_version: str | None
    warnings: tuple[JsonValue, ...]
    error_code: str | None
    error_message: str | None
    password_ref_used: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class TransactionDraft:
    """Initial parsed and effective values for one transaction."""

    email_id: int
    source_key: str
    transaction_type: TransactionType
    confidence: Decimal
    parser_name: str
    parser_version: str
    merchant: str | None = None
    original_merchant_text: str | None = None
    amount_minor: int | None = None
    currency: str | None = None
    card_last_four: str | None = None
    account_last_four: str | None = None
    transaction_at: datetime | None = None
    posted_at: datetime | None = None
    authorization_code: str | None = None
    extraction_evidence: tuple[JsonValue, ...] = ()
    parse_warnings: tuple[JsonValue, ...] = ()


@dataclass(frozen=True)
class Transaction:
    """Persisted effective transaction record."""

    id: int
    email_id: int
    source_key: str
    transaction_type: TransactionType
    merchant: str | None
    amount_minor: int | None
    currency: str | None
    card_last_four: str | None
    account_last_four: str | None
    transaction_at: datetime | None
    posted_at: datetime | None
    confidence: Decimal
    parser_name: str
    parser_version: str
    manually_confirmed: bool
    manually_ignored: bool
    user_note: str | None
    source_status: TransactionSourceStatus
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class TransactionReviewDraft:
    """Validated manual review values and explicitly overridden fields."""

    transaction_id: int
    expected_updated_at: datetime
    override_fields: frozenset[str]
    transaction_type: TransactionType
    merchant: str | None
    amount_minor: int | None
    currency: str | None
    transaction_at: datetime | None
    manually_confirmed: bool
    manually_ignored: bool
    user_note: str | None


@dataclass(frozen=True)
class SyncState:
    """Durable mailbox cursor and resumable full-sync checkpoint."""

    gmail_account_id: int
    committed_history_id: str | None
    baseline_history_id: str | None
    initial_query: str | None
    query_fingerprint: str | None
    query_semantics_version: int
    full_sync_next_page_token: str | None
    full_sync_status: FullSyncStatus
    reconciliation_required: bool
    last_successful_sync_at: datetime | None
    updated_at: datetime
    version: int


@dataclass(frozen=True)
class SyncRun:
    """Auditable state for one resumable synchronization operation."""

    id: str
    gmail_account_id: int
    mode: SyncMode
    status: RunStatus
    query: str | None
    start_history_id: str | None
    end_history_id: str | None
    next_page_token: str | None
    pages_processed: int
    discovered_count: int
    new_count: int
    updated_count: int
    deleted_count: int
    parsed_count: int
    parser_failed_count: int
    retried_count: int
    cancel_requested: bool
    worker_id: str | None
    lease_expires_at: datetime | None
    error_code: str | None
    error_message: str | None
    started_at: datetime | None
    heartbeat_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class SyncRunItem:
    """One idempotent message-level unit of synchronization work."""

    id: int
    sync_run_id: str
    gmail_message_id: str
    operation: SyncItemOperation
    status: SyncItemStatus
    attempt_count: int
    error_code: str | None
    error_message: str | None
    retryable: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class AppSetting:
    """Non-secret application preference stored as JSON."""

    key: str
    value: JsonValue
    schema_version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class GmailLabelOperation:
    """One durable, message-level request to add an application Label."""

    id: int
    gmail_account_id: int
    email_id: int
    gmail_message_id: str
    label_name: str
    status: GmailLabelOperationStatus
    attempt_count: int
    error_code: str | None
    error_message: str | None
    last_attempt_at: datetime | None
    created_at: datetime
    updated_at: datetime

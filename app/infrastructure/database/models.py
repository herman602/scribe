"""SQLAlchemy mappings for the accepted local schema."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import (
    AttachmentStatus,
    BodyRetentionPolicy,
    ClassificationStatus,
    EmailSourceStatus,
    FullSyncStatus,
    GmailLabelOperationStatus,
    ParserRunStatus,
    ParserTrigger,
    ProcessingStatus,
    RunStatus,
    SenderMatchType,
    SyncItemOperation,
    SyncItemStatus,
    SyncMode,
    TransactionSourceStatus,
    TransactionType,
)
from app.infrastructure.database.base import Base, TimestampMixin
from app.infrastructure.database.types import UTCDateTime


def _enum_check(column: str, values: type[Any]) -> str:
    allowed = ", ".join(f"'{item.value}'" for item in values)
    return f"{column} IN ({allowed})"


class GmailAccountModel(TimestampMixin, Base):
    """Gmail profile identity; OAuth secrets live outside SQLite."""

    __tablename__ = "gmail_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)


class SenderRuleModel(TimestampMixin, Base):
    """Configurable institution sender matching rule."""

    __tablename__ = "sender_rules"
    __table_args__ = (
        UniqueConstraint(
            "gmail_account_id",
            "match_type",
            "match_value_normalized",
            name="sender_match",
        ),
        CheckConstraint(
            _enum_check("match_type", SenderMatchType),
            name="sender_match_type",
        ),
        CheckConstraint("priority >= 0", name="sender_priority_nonnegative"),
        CheckConstraint("enabled IN (0, 1)", name="sender_enabled_boolean"),
        CheckConstraint(
            "gmail_query_enabled IN (0, 1)",
            name="sender_gmail_query_enabled_boolean",
        ),
        CheckConstraint("version >= 1", name="sender_version_positive"),
        Index(
            "ix_sender_rules_account_enabled_priority",
            "gmail_account_id",
            "enabled",
            "priority",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    gmail_account_id: Mapped[int] = mapped_column(
        ForeignKey("gmail_accounts.id", ondelete="CASCADE"), nullable=False
    )
    institution_key: Mapped[str] = mapped_column(String(100), nullable=False)
    institution_name: Mapped[str] = mapped_column(String(200), nullable=False)
    match_type: Mapped[str] = mapped_column(String(32), nullable=False)
    match_value_normalized: Mapped[str] = mapped_column(String(320), nullable=False)
    parser_name: Mapped[str | None] = mapped_column(String(200))
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    gmail_query_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    pdf_password_refs_json: Mapped[list[Any]] = mapped_column(
        JSON, nullable=False, default=list
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class SupplementaryCardSettingModel(TimestampMixin, Base):
    """Account-scoped bank and card-suffix mapping marked as supplementary."""

    __tablename__ = "supplementary_card_settings"
    __table_args__ = (
        UniqueConstraint(
            "gmail_account_id",
            "institution_key",
            "card_last_four",
            name="supplementary_card_identity",
        ),
        CheckConstraint(
            "length(card_last_four) = 4 AND card_last_four GLOB '[0-9][0-9][0-9][0-9]'",
            name="supplementary_card_last_four",
        ),
        Index(
            "ix_supplementary_card_settings_lookup",
            "gmail_account_id",
            "institution_key",
            "card_last_four",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    gmail_account_id: Mapped[int] = mapped_column(
        ForeignKey("gmail_accounts.id", ondelete="CASCADE"), nullable=False
    )
    institution_key: Mapped[str] = mapped_column(String(100), nullable=False)
    card_last_four: Mapped[str] = mapped_column(String(4), nullable=False)


class EmailModel(TimestampMixin, Base):
    """Decoded Gmail message and current classification summary."""

    __tablename__ = "emails"
    __table_args__ = (
        UniqueConstraint("gmail_account_id", "gmail_message_id", name="gmail_message"),
        CheckConstraint(
            _enum_check("classification_status", ClassificationStatus),
            name="email_classification",
        ),
        CheckConstraint(
            "classification_confidence IS NULL OR "
            "(classification_confidence >= 0 AND classification_confidence <= 1)",
            name="email_confidence_range",
        ),
        CheckConstraint(
            _enum_check("processing_status", ProcessingStatus),
            name="email_processing",
        ),
        CheckConstraint(
            _enum_check("source_status", EmailSourceStatus),
            name="email_source",
        ),
        CheckConstraint(
            _enum_check("body_policy_applied", BodyRetentionPolicy),
            name="email_body_policy",
        ),
        CheckConstraint(
            "has_html_body IN (0, 1)",
            name="email_has_html_body_boolean",
        ),
        Index("ix_emails_received_at", "received_at"),
        Index("ix_emails_sender", "sender"),
        Index("ix_emails_classification_status", "classification_status"),
        Index("ix_emails_gmail_thread_id", "gmail_thread_id"),
        Index("ix_emails_processing_status", "processing_status"),
        Index(
            "ix_emails_account_history",
            "gmail_account_id",
            "gmail_history_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    gmail_account_id: Mapped[int] = mapped_column(
        ForeignKey("gmail_accounts.id", ondelete="CASCADE"), nullable=False
    )
    gmail_message_id: Mapped[str] = mapped_column(String(100), nullable=False)
    gmail_thread_id: Mapped[str] = mapped_column(String(100), nullable=False)
    gmail_history_id: Mapped[str] = mapped_column(String(100), nullable=False)
    sender: Mapped[str] = mapped_column(String(500), nullable=False)
    recipients_json: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    subject: Mapped[str] = mapped_column(Text, nullable=False, default="")
    matched_sender_rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("sender_rules.id", ondelete="SET NULL")
    )
    received_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    gmail_internal_date_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    snippet: Mapped[str] = mapped_column(Text, nullable=False, default="")
    body_text: Mapped[str | None] = mapped_column(Text)
    body_summary: Mapped[str | None] = mapped_column(Text)
    has_html_body: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    labels_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    classification_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=ClassificationStatus.UNCERTAIN.value,
    )
    classification_confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    classification_reason: Mapped[str | None] = mapped_column(Text)
    parser_name: Mapped[str | None] = mapped_column(String(200))
    parser_version: Mapped[str | None] = mapped_column(String(50))
    processing_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ProcessingStatus.PENDING.value
    )
    source_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=EmailSourceStatus.ACTIVE.value
    )
    source_deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    body_policy_applied: Mapped[str] = mapped_column(String(32), nullable=False)
    body_purged_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class GmailLabelOperationModel(TimestampMixin, Base):
    """Durable outbox row for one message-level Gmail Label mutation."""

    __tablename__ = "gmail_label_operations"
    __table_args__ = (
        UniqueConstraint("email_id", "label_name", name="email_gmail_label"),
        CheckConstraint(
            _enum_check("status", GmailLabelOperationStatus),
            name="gmail_label_operation_status",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="gmail_label_operation_attempt_nonnegative",
        ),
        Index(
            "ix_gmail_label_operations_processable",
            "gmail_account_id",
            "label_name",
            "status",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    gmail_account_id: Mapped[int] = mapped_column(
        ForeignKey("gmail_accounts.id", ondelete="CASCADE"), nullable=False
    )
    email_id: Mapped[int] = mapped_column(
        ForeignKey("emails.id", ondelete="CASCADE"), nullable=False
    )
    gmail_message_id: Mapped[str] = mapped_column(String(100), nullable=False)
    label_name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=GmailLabelOperationStatus.PENDING.value,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    last_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class EmailAttachmentModel(TimestampMixin, Base):
    """Attachment metadata and locally extracted content."""

    __tablename__ = "email_attachments"
    __table_args__ = (
        UniqueConstraint("email_id", "mime_part_id", name="email_mime_part"),
        CheckConstraint("size_bytes >= 0", name="attachment_size_nonnegative"),
        CheckConstraint(
            _enum_check("extraction_status", AttachmentStatus),
            name="attachment_status",
        ),
        CheckConstraint(
            "raw_encrypted_content IS NULL OR is_encrypted = 1",
            name="attachment_encrypted_bytes_only",
        ),
        CheckConstraint("is_pdf IN (0, 1)", name="attachment_is_pdf_boolean"),
        CheckConstraint(
            "is_encrypted IN (0, 1)",
            name="attachment_is_encrypted_boolean",
        ),
        Index("ix_email_attachments_email_id", "email_id"),
        Index("ix_email_attachments_sha256", "sha256"),
        Index("ix_email_attachments_extraction_status", "extraction_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email_id: Mapped[int] = mapped_column(
        ForeignKey("emails.id", ondelete="CASCADE"), nullable=False
    )
    gmail_attachment_id: Mapped[str | None] = mapped_column(String(200))
    mime_part_id: Mapped[str] = mapped_column(String(100), nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False, default="")
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    is_pdf: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_encrypted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    raw_encrypted_content: Mapped[bytes | None] = mapped_column(LargeBinary)
    extracted_text: Mapped[str | None] = mapped_column(Text)
    extraction_status: Mapped[str] = mapped_column(
        String(40), nullable=False, default=AttachmentStatus.PENDING.value
    )
    extractor_name: Mapped[str | None] = mapped_column(String(200))
    extractor_version: Mapped[str | None] = mapped_column(String(50))
    warnings_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    password_ref_used: Mapped[str | None] = mapped_column(String(500))
    extracted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    purged_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class TransactionModel(TimestampMixin, Base):
    """Parsed, effective, and manually reviewed transaction values."""

    __tablename__ = "transactions"
    __table_args__ = (
        UniqueConstraint("email_id", "source_key", name="email_source_key"),
        CheckConstraint(
            _enum_check("parsed_transaction_type", TransactionType),
            name="parsed_transaction_type",
        ),
        CheckConstraint(
            _enum_check("transaction_type", TransactionType),
            name="effective_transaction_type",
        ),
        CheckConstraint(
            "parsed_amount_minor IS NULL OR parsed_amount_minor >= 0",
            name="parsed_amount_nonnegative",
        ),
        CheckConstraint(
            "amount_minor IS NULL OR amount_minor >= 0",
            name="effective_amount_nonnegative",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="transaction_confidence_range",
        ),
        CheckConstraint(
            _enum_check("source_status", TransactionSourceStatus),
            name="transaction_source",
        ),
        CheckConstraint(
            "manually_confirmed IN (0, 1)",
            name="transaction_manually_confirmed_boolean",
        ),
        CheckConstraint(
            "manually_ignored IN (0, 1)",
            name="transaction_manually_ignored_boolean",
        ),
        CheckConstraint(
            "override_schema_version >= 1",
            name="transaction_override_schema_version_positive",
        ),
        CheckConstraint(
            "parsed_card_last_four IS NULL OR "
            "(length(parsed_card_last_four) = 4 "
            "AND parsed_card_last_four NOT GLOB '*[^A-Za-z0-9]*')",
            name="parsed_card_last_four",
        ),
        CheckConstraint(
            "card_last_four IS NULL OR "
            "(length(card_last_four) = 4 "
            "AND card_last_four NOT GLOB '*[^A-Za-z0-9]*')",
            name="effective_card_last_four",
        ),
        CheckConstraint(
            "parsed_account_last_four IS NULL OR "
            "(length(parsed_account_last_four) = 4 "
            "AND parsed_account_last_four NOT GLOB '*[^A-Za-z0-9]*')",
            name="parsed_account_last_four",
        ),
        CheckConstraint(
            "account_last_four IS NULL OR "
            "(length(account_last_four) = 4 "
            "AND account_last_four NOT GLOB '*[^A-Za-z0-9]*')",
            name="effective_account_last_four",
        ),
        Index("ix_transactions_transaction_at", "transaction_at"),
        Index("ix_transactions_merchant", "merchant"),
        Index("ix_transactions_currency", "currency"),
        Index("ix_transactions_amount_minor", "amount_minor"),
        Index("ix_transactions_email_id", "email_id"),
        Index("ix_transactions_transaction_type", "transaction_type"),
        Index(
            "ix_transactions_review_flags",
            "manually_confirmed",
            "manually_ignored",
        ),
        Index(
            "ix_transactions_source_transaction_at",
            "source_status",
            "transaction_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email_id: Mapped[int] = mapped_column(
        ForeignKey("emails.id", ondelete="CASCADE"), nullable=False
    )
    source_key: Mapped[str] = mapped_column(String(200), nullable=False)
    parsed_transaction_type: Mapped[str] = mapped_column(String(40), nullable=False)
    parsed_merchant: Mapped[str | None] = mapped_column(Text)
    parsed_original_merchant_text: Mapped[str | None] = mapped_column(Text)
    parsed_amount_minor: Mapped[int | None] = mapped_column(BigInteger)
    parsed_currency: Mapped[str | None] = mapped_column(String(3))
    parsed_card_last_four: Mapped[str | None] = mapped_column(String(4))
    parsed_account_last_four: Mapped[str | None] = mapped_column(String(4))
    parsed_transaction_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    parsed_posted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    parsed_authorization_code: Mapped[str | None] = mapped_column(String(100))
    transaction_type: Mapped[str] = mapped_column(String(40), nullable=False)
    merchant: Mapped[str | None] = mapped_column(Text)
    original_merchant_text: Mapped[str | None] = mapped_column(Text)
    amount_minor: Mapped[int | None] = mapped_column(BigInteger)
    currency: Mapped[str | None] = mapped_column(String(3))
    card_last_four: Mapped[str | None] = mapped_column(String(4))
    account_last_four: Mapped[str | None] = mapped_column(String(4))
    transaction_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    posted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    authorization_code: Mapped[str | None] = mapped_column(String(100))
    manual_overrides_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    override_schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1
    )
    overridden_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    manually_confirmed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    manually_ignored: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    user_note: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    parser_name: Mapped[str] = mapped_column(String(200), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(50), nullable=False)
    extraction_evidence_json: Mapped[list[Any]] = mapped_column(
        JSON, nullable=False, default=list
    )
    parse_warnings_json: Mapped[list[Any]] = mapped_column(
        JSON, nullable=False, default=list
    )
    source_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=TransactionSourceStatus.ACTIVE.value,
    )


class SyncStateModel(Base):
    """One mailbox cursor and resumable full-sync checkpoint per account."""

    __tablename__ = "sync_states"
    __table_args__ = (
        CheckConstraint(
            _enum_check("full_sync_status", FullSyncStatus),
            name="full_sync_status",
        ),
        CheckConstraint(
            "query_semantics_version >= 1",
            name="query_semantics_version_positive",
        ),
        CheckConstraint("version >= 1", name="sync_state_version_positive"),
        CheckConstraint(
            "reconciliation_required IN (0, 1)",
            name="sync_state_reconciliation_boolean",
        ),
    )

    gmail_account_id: Mapped[int] = mapped_column(
        ForeignKey("gmail_accounts.id", ondelete="CASCADE"), primary_key=True
    )
    committed_history_id: Mapped[str | None] = mapped_column(String(100))
    baseline_history_id: Mapped[str | None] = mapped_column(String(100))
    initial_query: Mapped[str | None] = mapped_column(Text)
    query_fingerprint: Mapped[str | None] = mapped_column(String(64))
    query_semantics_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1
    )
    full_sync_next_page_token: Mapped[str | None] = mapped_column(Text)
    full_sync_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=FullSyncStatus.IDLE.value
    )
    reconciliation_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    last_successful_sync_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class SyncRunModel(TimestampMixin, Base):
    """Auditable and resumable synchronization operation."""

    __tablename__ = "sync_runs"
    __table_args__ = (
        CheckConstraint(_enum_check("mode", SyncMode), name="sync_run_mode"),
        CheckConstraint(_enum_check("status", RunStatus), name="sync_run_status"),
        CheckConstraint(
            "pages_processed >= 0 AND discovered_count >= 0 "
            "AND new_count >= 0 AND updated_count >= 0 "
            "AND deleted_count >= 0 AND parsed_count >= 0 "
            "AND parser_failed_count >= 0 AND retried_count >= 0",
            name="sync_run_counts_nonnegative",
        ),
        CheckConstraint(
            "cancel_requested IN (0, 1)",
            name="sync_run_cancel_requested_boolean",
        ),
        Index("ix_sync_runs_status_created_at", "status", "created_at"),
        Index("ix_sync_runs_gmail_account_id", "gmail_account_id"),
        Index(
            "uq_sync_runs_one_running_per_account",
            "gmail_account_id",
            unique=True,
            sqlite_where=text("status = 'running'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    gmail_account_id: Mapped[int] = mapped_column(
        ForeignKey("gmail_accounts.id", ondelete="CASCADE"), nullable=False
    )
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=RunStatus.QUEUED.value
    )
    query: Mapped[str | None] = mapped_column(Text)
    start_history_id: Mapped[str | None] = mapped_column(String(100))
    end_history_id: Mapped[str | None] = mapped_column(String(100))
    next_page_token: Mapped[str | None] = mapped_column(Text)
    pages_processed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    discovered_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    new_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deleted_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    parsed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    parser_failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retried_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    worker_id: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    debug_trace: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class SyncRunItemModel(TimestampMixin, Base):
    """Idempotent per-message synchronization work item."""

    __tablename__ = "sync_run_items"
    __table_args__ = (
        UniqueConstraint(
            "sync_run_id",
            "gmail_message_id",
            "operation",
            name="sync_message_operation",
        ),
        CheckConstraint(
            _enum_check("operation", SyncItemOperation),
            name="sync_item_operation",
        ),
        CheckConstraint(
            _enum_check("status", SyncItemStatus),
            name="sync_item_status",
        ),
        CheckConstraint("attempt_count >= 0", name="sync_item_attempt_nonnegative"),
        CheckConstraint(
            "retryable IN (0, 1)",
            name="sync_item_retryable_boolean",
        ),
        Index("ix_sync_run_items_run_status", "sync_run_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sync_run_id: Mapped[str] = mapped_column(
        ForeignKey("sync_runs.id", ondelete="CASCADE"), nullable=False
    )
    gmail_message_id: Mapped[str] = mapped_column(String(100), nullable=False)
    operation: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=SyncItemStatus.PENDING.value
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class ParserRunModel(Base):
    """One parser attempt for one email."""

    __tablename__ = "parser_runs"
    __table_args__ = (
        CheckConstraint(
            _enum_check("trigger", ParserTrigger),
            name="parser_run_trigger",
        ),
        CheckConstraint(
            _enum_check("status", ParserRunStatus),
            name="parser_run_status",
        ),
        CheckConstraint(
            "candidate_count >= 0",
            name="parser_candidate_count_nonnegative",
        ),
        Index("ix_parser_runs_email_created_at", "email_id", "created_at"),
        Index("ix_parser_runs_sync_run_id", "sync_run_id"),
        Index("ix_parser_runs_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email_id: Mapped[int] = mapped_column(
        ForeignKey("emails.id", ondelete="CASCADE"), nullable=False
    )
    sync_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("sync_runs.id", ondelete="SET NULL")
    )
    trigger: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ParserRunStatus.RUNNING.value
    )
    parser_name: Mapped[str | None] = mapped_column(String(200))
    parser_version: Mapped[str | None] = mapped_column(String(50))
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    warnings_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    debug_trace: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.current_timestamp()
    )
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.current_timestamp()
    )


class ClassificationResultModel(Base):
    """Immutable classification pipeline stage result."""

    __tablename__ = "classification_results"
    __table_args__ = (
        CheckConstraint(
            _enum_check("verdict", ClassificationStatus),
            name="classification_verdict",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="classification_confidence_range",
        ),
        Index(
            "ix_classification_results_email_created_at",
            "email_id",
            "created_at",
        ),
        Index("ix_classification_results_verdict", "verdict"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email_id: Mapped[int] = mapped_column(
        ForeignKey("emails.id", ondelete="CASCADE"), nullable=False
    )
    parser_run_id: Mapped[int] = mapped_column(
        ForeignKey("parser_runs.id", ondelete="CASCADE"), nullable=False
    )
    stage_name: Mapped[str] = mapped_column(String(200), nullable=False)
    stage_version: Mapped[str] = mapped_column(String(50), nullable=False)
    verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(100), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    extracted_fields_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    evidence_json: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.current_timestamp()
    )


class AppSettingModel(TimestampMixin, Base):
    """Non-secret preference persisted as canonical JSON."""

    __tablename__ = "app_settings"
    __table_args__ = (
        CheckConstraint("schema_version >= 1", name="setting_schema_version_positive"),
    )

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    value_json: Mapped[Any] = mapped_column(JSON, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

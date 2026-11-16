"""Persistence models for reviewed, read-only spreadsheet cutovers."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.database.base import Base, TimestampMixin
from app.infrastructure.database.types import UTCDateTime


class ImportBatchModel(TimestampMixin, Base):
    """One normalized source snapshot previewed before it changes the ledger."""

    __tablename__ = "import_batches"
    __table_args__ = (
        UniqueConstraint("content_sha256", name="import_batch_content_sha256"),
        CheckConstraint(
            "status IN ('previewed', 'applying', 'applied', 'failed')",
            name="import_batch_status",
        ),
        CheckConstraint("row_count >= 0", name="import_batch_row_count_nonnegative"),
        CheckConstraint(
            "warning_count >= 0", name="import_batch_warning_count_nonnegative"
        ),
        Index("ix_import_batches_observed_at", "observed_at"),
        Index("ix_import_batches_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_kind: Mapped[str] = mapped_column(String(50), nullable=False)
    format_version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_locator_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    cutoff_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    warning_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(100))
    applied_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class ImportRowModel(TimestampMixin, Base):
    """One normalized source fact and the entity it created, if any."""

    __tablename__ = "import_rows"
    __table_args__ = (
        UniqueConstraint("batch_id", "source_row_key", name="import_row_source"),
        CheckConstraint(
            "status IN ('applied', 'historical', 'pending', 'skipped', 'warning')",
            name="import_row_status",
        ),
        CheckConstraint(
            "(created_entity_type IS NULL) = (created_entity_id IS NULL)",
            name="import_row_created_entity_pairing",
        ),
        Index("ix_import_rows_batch_id_entity_kind", "batch_id", "entity_kind"),
        Index("ix_import_rows_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("import_batches.id", ondelete="RESTRICT"), nullable=False
    )
    entity_kind: Mapped[str] = mapped_column(String(50), nullable=False)
    source_row_key: Mapped[str] = mapped_column(String(200), nullable=False)
    normalized_payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(100))
    created_entity_type: Mapped[str | None] = mapped_column(String(50))
    created_entity_id: Mapped[int | None] = mapped_column(Integer)


class LegacyDailyActivityModel(TimestampMixin, Base):
    """Pre-opening daily evidence that deliberately never changes the ledger."""

    __tablename__ = "legacy_daily_activities"
    __table_args__ = (
        UniqueConstraint("import_row_id", name="legacy_daily_import_row"),
        CheckConstraint("amount_minor > 0", name="legacy_daily_amount_positive"),
        CheckConstraint(
            "length(currency) = 3 AND currency GLOB '[A-Z][A-Z][A-Z]'",
            name="legacy_daily_currency",
        ),
        CheckConstraint(
            "payment_method IN ('cash', 'credit_card')",
            name="legacy_daily_payment_method",
        ),
        CheckConstraint(
            "inclusion_mode = 'pre_opening_history'",
            name="legacy_daily_inclusion_mode",
        ),
        Index(
            "ix_legacy_daily_activities_activity_date_sequence",
            "activity_date",
            "sequence",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    import_row_id: Mapped[int] = mapped_column(
        ForeignKey("import_rows.id", ondelete="RESTRICT"), nullable=False
    )
    activity_date: Mapped[date] = mapped_column(nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    payment_method: Mapped[str] = mapped_column(String(20), nullable=False)
    note: Mapped[str | None] = mapped_column(String(500))
    inclusion_mode: Mapped[str] = mapped_column(
        String(30), nullable=False, default="pre_opening_history"
    )

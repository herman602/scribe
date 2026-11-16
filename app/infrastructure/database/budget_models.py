"""SQLAlchemy mappings for the immutable ledger foundation."""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.budget_enums import (
    BalanceSnapshotSource,
    CardConfigurationStatus,
    CardHolderRole,
    CardImportAssignmentKind,
    CommitmentFulfillmentMode,
    FinancialAccountType,
    FinancialEventOrigin,
    FinancialEventStatus,
    FinancialEventType,
    LedgerAccountKind,
    PaymentMethod,
    RecognitionBasis,
    SalaryCalculationMode,
    SalaryRunItemStatus,
    SalaryRunStatus,
    ScheduledExecutionPolicy,
    ScheduledTransferPurpose,
    ScheduledTransferStatus,
    SourceLinkStatus,
    SubscriptionCadence,
    WalletPurpose,
)
from app.domain.budget_models import MAX_CADENCE_MONTHS
from app.infrastructure.database.base import Base, TimestampMixin
from app.infrastructure.database.types import UTCDateTime


def _enum_check(column: str, values: type[Any]) -> str:
    allowed = ", ".join(f"'{item.value}'" for item in values)
    return f"{column} IN ({allowed})"


_CURRENCY_CHECK = "length({0}) = 3 AND {0} GLOB '[A-Z][A-Z][A-Z]'"
#: A boundary that is not the first of a month, or is not a real date at all,
#: is rejected. ``IS`` rather than ``=`` so a malformed value, whose
#: ``strftime`` is NULL, fails instead of silently passing an unknown result.
_MONTH_START_CHECK = "{0} IS strftime('%Y-%m-01', {0})"
CADENCE_SHAPE_CHECK = (
    "(billing_cadence = 'monthly' AND cadence_months = 1) OR "
    "(billing_cadence = 'yearly' AND cadence_months = 12) OR "
    "(billing_cadence = 'custom' AND cadence_months BETWEEN 2 AND "
    f"{MAX_CADENCE_MONTHS})"
)


class FinancialAccountModel(TimestampMixin, Base):
    """One real-world balance container held at an institution."""

    __tablename__ = "financial_accounts"
    __table_args__ = (
        CheckConstraint(
            _enum_check("account_type", FinancialAccountType),
            name="financial_account_type",
        ),
        CheckConstraint(
            _CURRENCY_CHECK.format("currency"),
            name="financial_account_currency",
        ),
        CheckConstraint("is_active IN (0, 1)", name="financial_account_active_boolean"),
        CheckConstraint("version >= 1", name="financial_account_version_positive"),
        CheckConstraint(
            "masked_reference IS NULL OR "
            "masked_reference NOT GLOB '*[0-9][0-9][0-9][0-9][0-9]*'",
            name="financial_account_masked_reference",
        ),
        Index(
            "uq_financial_accounts_active_identity",
            "institution_code",
            "display_name",
            "currency",
            unique=True,
            sqlite_where=text("is_active = 1"),
        ),
        Index("ix_financial_accounts_institution_code", "institution_code"),
        Index("ix_financial_accounts_display_order", "display_order"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    institution_code: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    account_type: Mapped[str] = mapped_column(String(32), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    masked_reference: Mapped[str | None] = mapped_column(String(32))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    #: Where this account reads in the sheet. Presentation only: it carries no
    #: money and is not part of any identity.
    display_order: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )


class WalletModel(TimestampMixin, Base):
    """One local envelope inside a physical asset account."""

    __tablename__ = "wallets"
    __table_args__ = (
        UniqueConstraint("financial_account_id", "name", name="wallet_account_name"),
        CheckConstraint(_enum_check("purpose", WalletPurpose), name="wallet_purpose"),
        CheckConstraint("is_active IN (0, 1)", name="wallet_active_boolean"),
        CheckConstraint("version >= 1", name="wallet_version_positive"),
        Index(
            "uq_wallets_active_unallocated",
            "financial_account_id",
            unique=True,
            sqlite_where=text("purpose = 'unallocated' AND is_active = 1"),
        ),
        Index("ix_wallets_financial_account_id", "financial_account_id"),
        Index(
            "ix_wallets_account_display_order",
            "financial_account_id",
            "display_order",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    financial_account_id: Mapped[int] = mapped_column(
        ForeignKey("financial_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    purpose: Mapped[str] = mapped_column(
        String(32), nullable=False, default=WalletPurpose.GENERAL.value
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    #: Where this envelope reads in its account's sheet column. Presentation
    #: only: it carries no money and is not part of any identity.
    display_order: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )


class LedgerAccountModel(TimestampMixin, Base):
    """One double-entry account, optionally backed by a physical account."""

    __tablename__ = "ledger_accounts"
    __table_args__ = (
        UniqueConstraint("code", name="ledger_account_code"),
        UniqueConstraint("financial_account_id", name="ledger_account_physical"),
        CheckConstraint(
            _enum_check("kind", LedgerAccountKind),
            name="ledger_account_kind",
        ),
        CheckConstraint(
            _CURRENCY_CHECK.format("currency"),
            name="ledger_account_currency",
        ),
        CheckConstraint(
            "budget_category_id IS NULL OR kind = 'expense'",
            name="ledger_account_category_is_expense",
        ),
        CheckConstraint(
            "budget_category_id IS NULL OR financial_account_id IS NULL",
            name="ledger_account_category_not_physical",
        ),
        Index("ix_ledger_accounts_kind", "kind"),
        Index(
            "uq_ledger_accounts_category_currency",
            "budget_category_id",
            "currency",
            unique=True,
            sqlite_where=text("budget_category_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(200), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    financial_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("financial_accounts.id", ondelete="RESTRICT")
    )
    budget_category_id: Mapped[int | None] = mapped_column(
        ForeignKey("budget_categories.id", ondelete="RESTRICT")
    )


class JournalEntryModel(TimestampMixin, Base):
    """One immutable balanced accounting event.

    ``entry_type`` references the ``journal_entry_types`` registry, so a later
    milestone adds a type by inserting one row (ADR-B028).
    """

    __tablename__ = "journal_entries"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="journal_entry_idempotency"),
        UniqueConstraint("reversal_of_id", name="journal_entry_single_reversal"),
        CheckConstraint(
            "reversal_of_id IS NULL OR reversal_of_id <> id",
            name="journal_entry_reversal_not_self",
        ),
        CheckConstraint(
            "(entry_type = 'reversal' AND reversal_of_id IS NOT NULL) OR "
            "(entry_type <> 'reversal' AND reversal_of_id IS NULL)",
            name="journal_entry_reversal_pairing",
        ),
        Index("ix_journal_entries_effective_at", "effective_at"),
        Index("ix_journal_entries_entry_type", "entry_type"),
        # Not unique: an event whose allocation was redone carries the
        # superseded entry, the reversal that cancelled it, and the one now in
        # force (ADR-B103). "At most one entry still in force" cannot be
        # expressed as a partial index, because it depends on whether another
        # row reverses this one, so that invariant lives in the posting
        # service, which resolves the entry in force before staging another.
        Index("ix_journal_entries_financial_event_id", "financial_event_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entry_type: Mapped[str] = mapped_column(
        ForeignKey("journal_entry_types.code", ondelete="RESTRICT"), nullable=False
    )
    effective_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    reversal_of_id: Mapped[int | None] = mapped_column(
        ForeignKey("journal_entries.id", ondelete="RESTRICT")
    )
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    financial_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("financial_events.id", ondelete="RESTRICT")
    )


class JournalPostingModel(TimestampMixin, Base):
    """One signed line of a balanced journal entry."""

    __tablename__ = "journal_postings"
    __table_args__ = (
        CheckConstraint("amount_minor <> 0", name="journal_posting_amount_nonzero"),
        CheckConstraint(
            _CURRENCY_CHECK.format("currency"),
            name="journal_posting_currency",
        ),
        Index("ix_journal_postings_journal_entry_id", "journal_entry_id"),
        Index("ix_journal_postings_account_wallet", "ledger_account_id", "wallet_id"),
        Index("ix_journal_postings_wallet_id", "wallet_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    journal_entry_id: Mapped[int] = mapped_column(
        ForeignKey("journal_entries.id", ondelete="RESTRICT"), nullable=False
    )
    ledger_account_id: Mapped[int] = mapped_column(
        ForeignKey("ledger_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    wallet_id: Mapped[int | None] = mapped_column(
        ForeignKey("wallets.id", ondelete="RESTRICT")
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    budget_category_id: Mapped[int | None] = mapped_column(
        ForeignKey("budget_categories.id", ondelete="RESTRICT")
    )


class CardModel(TimestampMixin, Base):
    """One locally configured card identified by issuer plus last four."""

    __tablename__ = "cards"
    __table_args__ = (
        CheckConstraint(
            _enum_check("holder_role", CardHolderRole), name="card_holder_role"
        ),
        CheckConstraint(
            _enum_check("configuration_status", CardConfigurationStatus),
            name="card_configuration_status",
        ),
        CheckConstraint(
            "length(last_four) = 4 AND last_four GLOB '[0-9][0-9][0-9][0-9]'",
            name="card_last_four",
        ),
        CheckConstraint("version >= 1", name="card_version_positive"),
        CheckConstraint(
            "configuration_status <> 'ready' OR "
            "(liability_account_id IS NOT NULL AND settlement_account_id IS NOT NULL)",
            name="card_ready_requires_accounts",
        ),
        Index(
            "uq_cards_active_identity",
            "gmail_account_id",
            "issuer_code",
            "last_four",
            unique=True,
            sqlite_where=text("active_to IS NULL"),
        ),
        Index(
            "ix_cards_identity_lookup",
            "gmail_account_id",
            "issuer_code",
            "last_four",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    gmail_account_id: Mapped[int] = mapped_column(
        ForeignKey("gmail_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    issuer_code: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    last_four: Mapped[str] = mapped_column(String(4), nullable=False)
    holder_role: Mapped[str] = mapped_column(
        String(32), nullable=False, default=CardHolderRole.UNKNOWN.value
    )
    configuration_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=CardConfigurationStatus.INCOMPLETE.value
    )
    liability_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("financial_accounts.id", ondelete="RESTRICT")
    )
    settlement_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("financial_accounts.id", ondelete="RESTRICT")
    )
    default_funding_wallet_id: Mapped[int | None] = mapped_column(
        ForeignKey("wallets.id", ondelete="RESTRICT")
    )
    active_from: Mapped[datetime | None] = mapped_column(UTCDateTime())
    active_to: Mapped[datetime | None] = mapped_column(UTCDateTime())
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class CardImportAssignmentModel(TimestampMixin, Base):
    """One explicit mapping from immutable B9 operational evidence to a card."""

    __tablename__ = "card_import_assignments"
    __table_args__ = (
        UniqueConstraint("import_row_id", name="card_import_assignment_import_row"),
        CheckConstraint(
            _enum_check("assignment_kind", CardImportAssignmentKind),
            name="card_import_assignment_kind",
        ),
        CheckConstraint(
            "(assignment_kind = 'card_cycle' AND statement_day IS NOT NULL "
            "AND due_day IS NOT NULL AND autopay_day IS NOT NULL) OR "
            "(assignment_kind = 'scheduled_transfer_reference' "
            "AND statement_day IS NULL AND due_day IS NULL AND autopay_day IS NULL)",
            name="card_import_assignment_shape",
        ),
        CheckConstraint(
            "statement_day IS NULL OR statement_day BETWEEN 1 AND 31",
            name="card_import_assignment_statement_day",
        ),
        CheckConstraint(
            "due_day IS NULL OR due_day BETWEEN 1 AND 31",
            name="card_import_assignment_due_day",
        ),
        CheckConstraint(
            "autopay_day IS NULL OR autopay_day BETWEEN 1 AND 31",
            name="card_import_assignment_autopay_day",
        ),
        CheckConstraint("version >= 1", name="card_import_assignment_version"),
        Index(
            "uq_card_import_assignment_cycle_card",
            "card_id",
            unique=True,
            sqlite_where=text("assignment_kind = 'card_cycle'"),
        ),
        Index("ix_card_import_assignments_card_id", "card_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    import_row_id: Mapped[int] = mapped_column(
        ForeignKey("import_rows.id", ondelete="RESTRICT"), nullable=False
    )
    card_id: Mapped[int] = mapped_column(
        ForeignKey("cards.id", ondelete="RESTRICT"), nullable=False
    )
    assignment_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    statement_day: Mapped[int | None] = mapped_column(Integer)
    due_day: Mapped[int | None] = mapped_column(Integer)
    autopay_day: Mapped[int | None] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class FinancialEventModel(TimestampMixin, Base):
    """One canonical economic event awaiting review, allocation, or posting."""

    __tablename__ = "financial_events"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="financial_event_idempotency"),
        CheckConstraint(
            _enum_check("origin", FinancialEventOrigin), name="financial_event_origin"
        ),
        CheckConstraint(
            _enum_check("event_type", FinancialEventType), name="financial_event_type"
        ),
        CheckConstraint(
            _enum_check("payment_method", PaymentMethod),
            name="financial_event_payment_method",
        ),
        CheckConstraint(
            _enum_check("recognition_basis", RecognitionBasis),
            name="financial_event_recognition_basis",
        ),
        CheckConstraint(
            _enum_check("status", FinancialEventStatus), name="financial_event_status"
        ),
        CheckConstraint(
            "amount_minor > 0", name="financial_event_amount_positive_magnitude"
        ),
        CheckConstraint(
            _CURRENCY_CHECK.format("currency"), name="financial_event_currency"
        ),
        CheckConstraint("version >= 1", name="financial_event_version_positive"),
        Index(
            "ix_financial_events_daily_activity",
            "occurred_at",
            "payment_method",
            "status",
        ),
        # The manual-candidate lookup B7 runs before every Gmail intake
        # (ADR-B062). Leading with the columns that are always equality-tested
        # keeps the amount and time comparison to an index range rather than a
        # scan of every event ever recorded.
        Index(
            "ix_financial_events_matching_candidates",
            "origin",
            "payment_method",
            "status",
            "currency",
            "amount_minor",
            "occurred_at",
        ),
        Index("ix_financial_events_status", "status"),
        Index("ix_financial_events_card_id", "card_id"),
        Index("ix_financial_events_refund_of_event_id", "refund_of_event_id"),
        CheckConstraint(
            "refund_of_event_id IS NULL OR "
            "(event_type = 'refund' AND refund_of_event_id <> id)",
            name="financial_event_refund_shape",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    origin: Mapped[str] = mapped_column(String(32), nullable=False)
    source_import_key: Mapped[str | None] = mapped_column(String(200))
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payment_method: Mapped[str] = mapped_column(String(32), nullable=False)
    recognition_basis: Mapped[str] = mapped_column(String(32), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    merchant: Mapped[str | None] = mapped_column(Text)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    card_id: Mapped[int | None] = mapped_column(
        ForeignKey("cards.id", ondelete="RESTRICT")
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    refund_of_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("financial_events.id", ondelete="RESTRICT")
    )


class FinancialEventSourceLinkModel(TimestampMixin, Base):
    """Auditable provenance between one event and one parsed transaction."""

    __tablename__ = "financial_event_source_links"
    __table_args__ = (
        UniqueConstraint(
            "financial_event_id", "transaction_id", name="financial_event_source"
        ),
        CheckConstraint(
            _enum_check("status", SourceLinkStatus), name="source_link_status"
        ),
        CheckConstraint(
            "match_score_bps >= 0 AND match_score_bps <= 10000",
            name="source_link_score_range",
        ),
        Index(
            "uq_source_links_one_confirmed_per_transaction",
            "transaction_id",
            unique=True,
            sqlite_where=text("status = 'confirmed'"),
        ),
        Index("ix_source_links_transaction_status", "transaction_id", "status"),
        Index("ix_source_links_event_status", "financial_event_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    financial_event_id: Mapped[int] = mapped_column(
        ForeignKey("financial_events.id", ondelete="RESTRICT"), nullable=False
    )
    transaction_id: Mapped[int] = mapped_column(
        ForeignKey("transactions.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    match_score_bps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reason_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class JournalEntryTypeModel(TimestampMixin, Base):
    """Reference table of accepted journal entry types (ADR-B028).

    A later milestone adds a type by inserting one row; the financial tables
    are never rebuilt again for that reason.
    """

    __tablename__ = "journal_entry_types"

    code: Mapped[str] = mapped_column(String(40), primary_key=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")


class BudgetCategoryModel(TimestampMixin, Base):
    """One hierarchical, currency-neutral spending purpose."""

    __tablename__ = "budget_categories"
    __table_args__ = (
        CheckConstraint("is_active IN (0, 1)", name="category_boolean"),
        CheckConstraint(
            "parent_id IS NULL OR parent_id <> id", name="category_parent_not_self"
        ),
        CheckConstraint("version >= 1", name="category_version_positive"),
        Index(
            "uq_budget_categories_child_name",
            "parent_id",
            "name",
            unique=True,
            sqlite_where=text("parent_id IS NOT NULL"),
        ),
        Index(
            "uq_budget_categories_root_name",
            "name",
            unique=True,
            sqlite_where=text("parent_id IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("budget_categories.id", ondelete="RESTRICT")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class BudgetPeriodModel(TimestampMixin, Base):
    """One calendar month of budget in exactly one currency."""

    __tablename__ = "budget_periods"
    __table_args__ = (
        UniqueConstraint("year", "month", "currency", name="budget_period_identity"),
        CheckConstraint(
            _CURRENCY_CHECK.format("currency"), name="budget_period_currency"
        ),
        CheckConstraint("month >= 1 AND month <= 12", name="budget_period_month_range"),
        CheckConstraint(
            "year >= 2000 AND year <= 2200", name="budget_period_year_range"
        ),
        CheckConstraint("status IN ('open', 'closed')", name="budget_period_status"),
        CheckConstraint("version >= 1", name="budget_period_version_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    month: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class BudgetTargetModel(TimestampMixin, Base):
    """One category's monthly target, buffer, and funding wallet."""

    __tablename__ = "budget_targets"
    __table_args__ = (
        UniqueConstraint(
            "budget_period_id", "budget_category_id", name="budget_target_identity"
        ),
        CheckConstraint("target_minor >= 0", name="budget_target_nonnegative"),
        CheckConstraint("buffer_minor >= 0", name="budget_buffer_nonnegative"),
        CheckConstraint("version >= 1", name="budget_target_version_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    budget_period_id: Mapped[int] = mapped_column(
        ForeignKey("budget_periods.id", ondelete="RESTRICT"), nullable=False
    )
    budget_category_id: Mapped[int] = mapped_column(
        ForeignKey("budget_categories.id", ondelete="RESTRICT"), nullable=False
    )
    target_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    buffer_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    funding_wallet_id: Mapped[int] = mapped_column(
        ForeignKey("wallets.id", ondelete="RESTRICT"), nullable=False
    )
    month_end_destination_wallet_id: Mapped[int | None] = mapped_column(
        ForeignKey("wallets.id", ondelete="RESTRICT")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class EventAllocationModel(TimestampMixin, Base):
    """One share of a financial event assigned to a category and wallet."""

    __tablename__ = "event_allocations"
    __table_args__ = (
        CheckConstraint(
            "allocation_source IN ('rule', 'manual', 'import')",
            name="event_allocation_source",
        ),
        CheckConstraint("amount_minor > 0", name="event_allocation_amount_positive"),
        CheckConstraint(
            _CURRENCY_CHECK.format("currency"), name="event_allocation_currency"
        ),
        Index("ix_event_allocations_financial_event_id", "financial_event_id"),
        Index("ix_event_allocations_journal_entry_id", "journal_entry_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    financial_event_id: Mapped[int] = mapped_column(
        ForeignKey("financial_events.id", ondelete="RESTRICT"), nullable=False
    )
    #: The entry that carried this decision. Nullable only for the instant
    #: between writing the rows and staging the entry built from them; the
    #: allocation service links them in the same transaction (ADR-B103).
    journal_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("journal_entries.id", ondelete="RESTRICT")
    )
    budget_category_id: Mapped[int | None] = mapped_column(
        ForeignKey("budget_categories.id", ondelete="RESTRICT")
    )
    funding_wallet_id: Mapped[int] = mapped_column(
        ForeignKey("wallets.id", ondelete="RESTRICT"), nullable=False
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    allocation_source: Mapped[str] = mapped_column(String(32), nullable=False)


class DedicatedWalletAllocationModel(TimestampMixin, Base):
    """Immutable marker that routes one allocation through a clearing wallet."""

    __tablename__ = "dedicated_wallet_allocations"
    __table_args__ = (
        UniqueConstraint(
            "event_allocation_id", name="dedicated_wallet_event_allocation"
        ),
        Index(
            "ix_dedicated_wallet_allocations_clearing_wallet_id",
            "clearing_wallet_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_allocation_id: Mapped[int] = mapped_column(
        ForeignKey("event_allocations.id", ondelete="RESTRICT"), nullable=False
    )
    clearing_wallet_id: Mapped[int] = mapped_column(
        ForeignKey("wallets.id", ondelete="RESTRICT"), nullable=False
    )


class SubscriptionModel(TimestampMixin, Base):
    """One recurring service whose monthly funding need is calculated."""

    __tablename__ = "subscriptions"
    __table_args__ = (
        CheckConstraint(
            _enum_check("billing_cadence", SubscriptionCadence),
            name="subscription_cadence",
        ),
        CheckConstraint(CADENCE_SHAPE_CHECK, name="subscription_cadence_shape"),
        CheckConstraint(
            _CURRENCY_CHECK.format("currency"), name="subscription_currency"
        ),
        # A yearly cycle is anchored by its billing month and a custom cycle
        # by the month it became active. Without the anchor the cycle could
        # only be placed by guessing at the current date.
        CheckConstraint(
            "billing_cadence <> 'yearly' OR billing_month IS NOT NULL",
            name="subscription_yearly_anchor",
        ),
        CheckConstraint(
            "billing_cadence <> 'custom' OR active_from IS NOT NULL",
            name="subscription_custom_anchor",
        ),
        CheckConstraint(
            "billing_month IS NULL OR (billing_month >= 1 AND billing_month <= 12)",
            name="subscription_billing_month_range",
        ),
        CheckConstraint(
            "billing_day IS NULL OR (billing_day >= 1 AND billing_day <= 31)",
            name="subscription_billing_day_range",
        ),
        CheckConstraint(
            "active_from IS NULL OR " + _MONTH_START_CHECK.format("active_from"),
            name="subscription_active_from_month_start",
        ),
        CheckConstraint(
            "active_to IS NULL OR " + _MONTH_START_CHECK.format("active_to"),
            name="subscription_active_to_month_start",
        ),
        CheckConstraint(
            "active_to IS NULL OR active_from IS NULL OR active_to > active_from",
            name="subscription_active_range",
        ),
        CheckConstraint("is_active IN (0, 1)", name="subscription_active_boolean"),
        # Deactivating without a month would leave every historical month
        # undecidable, so the register always records when it stopped.
        CheckConstraint(
            "is_active = 1 OR active_to IS NOT NULL",
            name="subscription_inactive_needs_end",
        ),
        CheckConstraint("version >= 1", name="subscription_version_positive"),
        # Two invariants, not one. A name still has at most one running
        # interval, which is what this partial index says, and revision 0009
        # adds triggers on top so the intervals of one service never overlap.
        # Neither replaces the other: two stopped intervals can overlap
        # without breaking the index, and two adjacent intervals can both be
        # running without overlapping.
        Index(
            "uq_subscriptions_active_name",
            "name",
            unique=True,
            sqlite_where=text("is_active = 1"),
        ),
        Index("ix_subscriptions_name_activity", "name", "active_from", "active_to"),
        Index("ix_subscriptions_currency_active", "currency", "is_active"),
        Index("ix_subscriptions_funding_wallet_id", "funding_wallet_id"),
        Index("ix_subscriptions_budget_category_id", "budget_category_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    budget_category_id: Mapped[int] = mapped_column(
        ForeignKey("budget_categories.id", ondelete="RESTRICT"), nullable=False
    )
    funding_wallet_id: Mapped[int] = mapped_column(
        ForeignKey("wallets.id", ondelete="RESTRICT"), nullable=False
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    billing_cadence: Mapped[str] = mapped_column(String(32), nullable=False)
    cadence_months: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    billing_month: Mapped[int | None] = mapped_column(Integer)
    billing_day: Mapped[int | None] = mapped_column(Integer)
    active_from: Mapped[date | None] = mapped_column(Date())
    active_to: Mapped[date | None] = mapped_column(Date())
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class SubscriptionPricePeriodModel(TimestampMixin, Base):
    """One price valid over the half-open range ``[from, to)``.

    SQLite cannot express the cross-row non-overlap rule as a check
    constraint, so revision 0008 adds BEFORE INSERT and BEFORE UPDATE
    triggers alongside the service-level validation.
    """

    __tablename__ = "subscription_price_periods"
    __table_args__ = (
        CheckConstraint("amount_minor >= 0", name="subscription_price_nonnegative"),
        CheckConstraint(
            _MONTH_START_CHECK.format("effective_from"),
            name="subscription_price_from_month_start",
        ),
        CheckConstraint(
            "effective_to IS NULL OR " + _MONTH_START_CHECK.format("effective_to"),
            name="subscription_price_to_month_start",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="subscription_price_range",
        ),
        CheckConstraint("version >= 1", name="subscription_price_version_positive"),
        UniqueConstraint(
            "subscription_id", "effective_from", name="subscription_price_identity"
        ),
        Index(
            "ix_subscription_price_periods_lookup",
            "subscription_id",
            "effective_from",
            "effective_to",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="RESTRICT"), nullable=False
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    effective_from: Mapped[date] = mapped_column(Date(), nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date())
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


#: A fixed line carries the amount it always asks for; a calculated or manual
#: line must not, so a stale number can never become what a month freezes.
SALARY_ITEM_SHAPE_CHECK = (
    "(calculation_mode = 'fixed' AND fixed_amount_minor IS NOT NULL "
    "AND fixed_amount_minor >= 0) OR "
    "(calculation_mode IN ('subscription_requirement', 'manual') "
    "AND fixed_amount_minor IS NULL)"
)
#: A run's timestamps must agree with the status they describe, so a cancelled
#: draft can never look approved and an approved run can never lose its time.
SALARY_RUN_TIMESTAMP_CHECK = (
    "(status = 'draft' AND approved_at IS NULL AND cancelled_at IS NULL) OR "
    "(status = 'cancelled' AND cancelled_at IS NOT NULL AND approved_at IS NULL) OR "
    "(status IN ('approved', 'partially_funded', 'funded') "
    "AND approved_at IS NOT NULL AND cancelled_at IS NULL)"
)
#: A transferred line names both the entry that moved the money and when it
#: moved; anything else must name neither.
SALARY_ITEM_TRANSFER_CHECK = (
    "(status = 'transferred' AND transfer_entry_id IS NOT NULL "
    "AND transferred_at IS NOT NULL) OR "
    "(status <> 'transferred' AND transfer_entry_id IS NULL "
    "AND transferred_at IS NULL)"
)


class SalaryPlanTemplateModel(TimestampMixin, Base):
    """The current plan for distributing one salary account's income.

    The template describes what future runs should do. History lives in the
    immutable run snapshots, so editing this row never changes a month that has
    already been generated (ADR-B042).
    """

    __tablename__ = "salary_plan_templates"
    __table_args__ = (
        CheckConstraint(
            _CURRENCY_CHECK.format("currency"), name="salary_template_currency"
        ),
        CheckConstraint("is_active IN (0, 1)", name="salary_template_active_boolean"),
        CheckConstraint("version >= 1", name="salary_template_version_positive"),
        Index(
            "uq_salary_plan_templates_active_identity",
            "name",
            "currency",
            unique=True,
            sqlite_where=text("is_active = 1"),
        ),
        Index("ix_salary_plan_templates_source_account_id", "source_account_id"),
        Index("ix_salary_plan_templates_currency_active", "currency", "is_active"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    source_account_id: Mapped[int] = mapped_column(
        ForeignKey("financial_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class SalaryPlanTemplateItemModel(TimestampMixin, Base):
    """One destination a template sends part of the salary to.

    Two active items may not share a destination wallet, because the run would
    then have two lines competing for the same envelope and the subscription
    requirement could be claimed twice.
    """

    __tablename__ = "salary_plan_template_items"
    __table_args__ = (
        CheckConstraint(
            _enum_check("calculation_mode", SalaryCalculationMode),
            name="salary_item_calculation_mode",
        ),
        CheckConstraint(SALARY_ITEM_SHAPE_CHECK, name="salary_item_amount_shape"),
        CheckConstraint("display_order >= 0", name="salary_item_display_order"),
        CheckConstraint("is_active IN (0, 1)", name="salary_item_active_boolean"),
        CheckConstraint("version >= 1", name="salary_item_version_positive"),
        Index(
            "uq_salary_template_items_active_name",
            "template_id",
            "name",
            unique=True,
            sqlite_where=text("is_active = 1"),
        ),
        Index(
            "uq_salary_template_items_active_destination",
            "template_id",
            "destination_wallet_id",
            unique=True,
            sqlite_where=text("is_active = 1"),
        ),
        Index("ix_salary_plan_template_items_template_id", "template_id"),
        Index(
            "ix_salary_plan_template_items_destination_wallet_id",
            "destination_wallet_id",
        ),
        Index("ix_salary_plan_template_items_budget_category_id", "budget_category_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    template_id: Mapped[int] = mapped_column(
        ForeignKey("salary_plan_templates.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    destination_wallet_id: Mapped[int] = mapped_column(
        ForeignKey("wallets.id", ondelete="RESTRICT"), nullable=False
    )
    budget_category_id: Mapped[int | None] = mapped_column(
        ForeignKey("budget_categories.id", ondelete="RESTRICT")
    )
    calculation_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    fixed_amount_minor: Mapped[int | None] = mapped_column(BigInteger)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class SalaryAllocationRunModel(TimestampMixin, Base):
    """One frozen generation of a month's salary distribution.

    Every snapshot column is written once. Revision 0011 adds a BEFORE UPDATE
    trigger beside the service rule, so a direct write cannot rewrite what a
    month said either.
    """

    __tablename__ = "salary_allocation_runs"
    __table_args__ = (
        CheckConstraint(
            _enum_check("status", SalaryRunStatus), name="salary_run_status"
        ),
        CheckConstraint(_CURRENCY_CHECK.format("currency"), name="salary_run_currency"),
        CheckConstraint("salary_amount_minor > 0", name="salary_run_amount_positive"),
        CheckConstraint("planned_amount_minor >= 0", name="salary_run_planned_bounded"),
        CheckConstraint("remainder_minor >= 0", name="salary_run_remainder_bounded"),
        CheckConstraint(
            "planned_amount_minor + remainder_minor = salary_amount_minor",
            name="salary_run_plan_adds_up",
        ),
        CheckConstraint("generation >= 1", name="salary_run_generation_positive"),
        CheckConstraint("version >= 1", name="salary_run_version_positive"),
        CheckConstraint(
            "subscription_evidence_schema_version >= 1",
            name="salary_run_evidence_version_positive",
        ),
        CheckConstraint(SALARY_RUN_TIMESTAMP_CHECK, name="salary_run_timestamp_shape"),
        CheckConstraint(
            "regenerated_from_run_id IS NULL OR regenerated_from_run_id <> id",
            name="salary_run_regeneration_not_self",
        ),
        CheckConstraint(
            "(regenerated_from_run_id IS NULL AND request_fingerprint IS NULL) OR "
            "(regenerated_from_run_id IS NOT NULL AND request_fingerprint IS NOT NULL)",
            name="salary_run_regeneration_pairing",
        ),
        UniqueConstraint(
            "template_id",
            "budget_period_id",
            "generation",
            name="salary_run_generation_identity",
        ),
        Index(
            "uq_salary_runs_live_period",
            "template_id",
            "budget_period_id",
            unique=True,
            sqlite_where=text("status <> 'cancelled'"),
        ),
        Index(
            "uq_salary_runs_live_salary_event",
            "salary_event_id",
            unique=True,
            sqlite_where=text("status <> 'cancelled'"),
        ),
        Index(
            "uq_salary_runs_approval_entry",
            "approval_entry_id",
            unique=True,
            sqlite_where=text("approval_entry_id IS NOT NULL"),
        ),
        # At most one generation may claim to be the replacement of a given
        # draft, which is what makes a regenerate replay identifiable at all.
        Index(
            "uq_salary_runs_regenerated_from",
            "regenerated_from_run_id",
            unique=True,
            sqlite_where=text("regenerated_from_run_id IS NOT NULL"),
        ),
        Index("ix_salary_allocation_runs_budget_period_id", "budget_period_id"),
        Index("ix_salary_allocation_runs_source_wallet_id", "source_wallet_id"),
        Index("ix_salary_allocation_runs_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    template_id: Mapped[int] = mapped_column(
        ForeignKey("salary_plan_templates.id", ondelete="RESTRICT"), nullable=False
    )
    template_name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    template_version: Mapped[int] = mapped_column(Integer, nullable=False)
    budget_period_id: Mapped[int] = mapped_column(
        ForeignKey("budget_periods.id", ondelete="RESTRICT"), nullable=False
    )
    salary_event_id: Mapped[int] = mapped_column(
        ForeignKey("financial_events.id", ondelete="RESTRICT"), nullable=False
    )
    source_account_id: Mapped[int] = mapped_column(
        ForeignKey("financial_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    source_wallet_id: Mapped[int] = mapped_column(
        ForeignKey("wallets.id", ondelete="RESTRICT"), nullable=False
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    salary_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    planned_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    remainder_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    subscription_evidence_schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1
    )
    subscription_evidence_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    approval_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("journal_entries.id", ondelete="RESTRICT")
    )
    #: Which draft this generation replaced, and the canonical fingerprint of
    #: the request that replaced it. Together they identify a regeneration well
    #: enough to recognize a resubmission and refuse a different payload
    #: (ADR-B049). Both are NULL for a first, ordinary generation.
    regenerated_from_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("salary_allocation_runs.id", ondelete="RESTRICT")
    )
    request_fingerprint: Mapped[str | None] = mapped_column(String(200))
    generated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class SalaryAllocationRunItemModel(TimestampMixin, Base):
    """One frozen line of a run, and the reservation it may still hold.

    A ``waiting_transfer`` line is itself the salary commitment (ADR-B043), so
    no second table records the money it holds back in the source wallet.
    """

    __tablename__ = "salary_allocation_run_items"
    __table_args__ = (
        CheckConstraint(
            _enum_check("calculation_mode", SalaryCalculationMode),
            name="salary_run_item_calculation_mode",
        ),
        CheckConstraint(
            _enum_check("fulfillment_mode", CommitmentFulfillmentMode),
            name="salary_run_item_fulfillment_mode",
        ),
        CheckConstraint(
            _enum_check("status", SalaryRunItemStatus), name="salary_run_item_status"
        ),
        CheckConstraint(
            _CURRENCY_CHECK.format("currency"), name="salary_run_item_currency"
        ),
        CheckConstraint("amount_minor >= 0", name="salary_run_item_amount_bounded"),
        CheckConstraint("display_order >= 0", name="salary_run_item_display_order"),
        CheckConstraint("version >= 1", name="salary_run_item_version_positive"),
        CheckConstraint(
            SALARY_ITEM_TRANSFER_CHECK, name="salary_run_item_transfer_shape"
        ),
        UniqueConstraint("run_id", "template_item_id", name="salary_run_item_identity"),
        Index(
            "uq_salary_run_items_transfer_entry",
            "transfer_entry_id",
            unique=True,
            sqlite_where=text("transfer_entry_id IS NOT NULL"),
        ),
        Index("ix_salary_allocation_run_items_run_id", "run_id"),
        Index(
            "ix_salary_allocation_run_items_destination_wallet_id",
            "destination_wallet_id",
        ),
        Index("ix_salary_allocation_run_items_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("salary_allocation_runs.id", ondelete="RESTRICT"), nullable=False
    )
    template_item_id: Mapped[int] = mapped_column(
        ForeignKey("salary_plan_template_items.id", ondelete="RESTRICT"), nullable=False
    )
    template_item_version: Mapped[int] = mapped_column(Integer, nullable=False)
    name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    destination_wallet_id: Mapped[int] = mapped_column(
        ForeignKey("wallets.id", ondelete="RESTRICT"), nullable=False
    )
    destination_account_id: Mapped[int] = mapped_column(
        ForeignKey("financial_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    destination_wallet_name_snapshot: Mapped[str] = mapped_column(
        String(200), nullable=False
    )
    destination_account_name_snapshot: Mapped[str] = mapped_column(
        String(200), nullable=False
    )
    budget_category_id: Mapped[int | None] = mapped_column(
        ForeignKey("budget_categories.id", ondelete="RESTRICT")
    )
    budget_category_name_snapshot: Mapped[str | None] = mapped_column(String(200))
    calculation_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fulfillment_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    calculation_evidence_json: Mapped[str] = mapped_column(Text, nullable=False)
    transfer_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("journal_entries.id", ondelete="RESTRICT")
    )
    transferred_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class FundingCommitmentModel(TimestampMixin, Base):
    """Money reserved for one allocation, with an explicit fulfillment path."""

    __tablename__ = "funding_commitments"
    __table_args__ = (
        UniqueConstraint("event_allocation_id", name="funding_commitment_identity"),
        CheckConstraint("amount_minor > 0", name="funding_commitment_amount_positive"),
        CheckConstraint(
            "released_minor >= 0 AND released_minor <= amount_minor",
            name="funding_commitment_released_bounded",
        ),
        CheckConstraint(
            _CURRENCY_CHECK.format("currency"), name="funding_commitment_currency"
        ),
        CheckConstraint(
            "fulfillment_mode IN "
            "('reserve_only', 'account_transfer', 'cross_bank_transfer')",
            name="funding_commitment_fulfillment_mode",
        ),
        CheckConstraint(
            "status IN "
            "('open', 'partially_scheduled', 'scheduled', 'funded', 'cancelled')",
            name="funding_commitment_status",
        ),
        Index("ix_funding_commitments_source_status", "source_wallet_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    financial_event_id: Mapped[int] = mapped_column(
        ForeignKey("financial_events.id", ondelete="RESTRICT"), nullable=False
    )
    event_allocation_id: Mapped[int] = mapped_column(
        ForeignKey("event_allocations.id", ondelete="RESTRICT"), nullable=False
    )
    source_wallet_id: Mapped[int] = mapped_column(
        ForeignKey("wallets.id", ondelete="RESTRICT"), nullable=False
    )
    settlement_account_id: Mapped[int] = mapped_column(
        ForeignKey("financial_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    released_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    fulfillment_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")


#: Which columns each lifecycle state may and may not carry. A schedule that is
#: `assumed_executed` without an entry, or `cancelled` while still holding a
#: lease, is a contradiction the row itself refuses (ADR-B053).
SCHEDULED_TRANSFER_STATUS_SHAPE = (
    "(status IN ('scheduled', 'failed') AND executed_entry_id IS NULL "
    "AND reversal_entry_id IS NULL AND processed_at IS NULL "
    "AND cancelled_at IS NULL AND reversed_at IS NULL "
    "AND lease_owner IS NULL AND lease_expires_at IS NULL) OR "
    "(status = 'processing' AND executed_entry_id IS NULL "
    "AND reversal_entry_id IS NULL AND processed_at IS NULL "
    "AND cancelled_at IS NULL AND reversed_at IS NULL "
    "AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
    "(status = 'assumed_executed' AND executed_entry_id IS NOT NULL "
    "AND reversal_entry_id IS NULL AND processed_at IS NOT NULL "
    "AND cancelled_at IS NULL AND reversed_at IS NULL "
    "AND lease_owner IS NULL AND lease_expires_at IS NULL) OR "
    "(status = 'cancelled' AND executed_entry_id IS NULL "
    "AND reversal_entry_id IS NULL AND processed_at IS NULL "
    "AND cancelled_at IS NOT NULL AND reversed_at IS NULL "
    "AND lease_owner IS NULL AND lease_expires_at IS NULL) OR "
    "(status = 'reversed' AND executed_entry_id IS NOT NULL "
    "AND reversal_entry_id IS NOT NULL AND processed_at IS NOT NULL "
    "AND cancelled_at IS NULL AND reversed_at IS NOT NULL "
    "AND lease_owner IS NULL AND lease_expires_at IS NULL)"
)
#: The two posting shapes are mutually exclusive in the row, not only in code.
SCHEDULED_TRANSFER_PURPOSE_SHAPE = (
    "(purpose = 'clearing_funding' AND card_id IS NULL "
    "AND destination_wallet_id IS NOT NULL) OR "
    "(purpose = 'card_autopay' AND card_id IS NOT NULL "
    "AND destination_wallet_id IS NULL)"
)
SCHEDULED_TRANSFER_LEASE_PAIRING = (
    "(lease_owner IS NULL AND lease_expires_at IS NULL) OR "
    "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)"
)


class ScheduledTransferModel(TimestampMixin, Base):
    """One bank movement the user has instructed and Scribe will assume.

    Every economic column is written once; revision 0013 adds a BEFORE UPDATE
    trigger beside the service rule, so no path can rewrite the instruction a
    reservation was already calculated from (ADR-B053).
    """

    __tablename__ = "scheduled_transfers"
    __table_args__ = (
        CheckConstraint(
            _enum_check("purpose", ScheduledTransferPurpose),
            name="scheduled_transfer_purpose",
        ),
        CheckConstraint(
            _enum_check("status", ScheduledTransferStatus),
            name="scheduled_transfer_status",
        ),
        CheckConstraint(
            _enum_check("execution_policy", ScheduledExecutionPolicy),
            name="scheduled_transfer_execution_policy",
        ),
        CheckConstraint(
            SCHEDULED_TRANSFER_PURPOSE_SHAPE, name="scheduled_transfer_purpose_shape"
        ),
        CheckConstraint(
            SCHEDULED_TRANSFER_STATUS_SHAPE, name="scheduled_transfer_status_shape"
        ),
        CheckConstraint(
            SCHEDULED_TRANSFER_LEASE_PAIRING, name="scheduled_transfer_lease_pairing"
        ),
        CheckConstraint(
            "status <> 'failed' OR last_error_code IS NOT NULL",
            name="scheduled_transfer_failure_reason",
        ),
        CheckConstraint("amount_minor > 0", name="scheduled_transfer_amount_positive"),
        CheckConstraint(
            _CURRENCY_CHECK.format("currency"), name="scheduled_transfer_currency"
        ),
        CheckConstraint(
            "source_account_id <> destination_account_id",
            name="scheduled_transfer_distinct_accounts",
        ),
        CheckConstraint("attempt_count >= 0", name="scheduled_transfer_attempts"),
        CheckConstraint("version >= 1", name="scheduled_transfer_version_positive"),
        UniqueConstraint("idempotency_key", name="scheduled_transfer_idempotency"),
        Index(
            "uq_scheduled_transfers_executed_entry",
            "executed_entry_id",
            unique=True,
            sqlite_where=text("executed_entry_id IS NOT NULL"),
        ),
        Index(
            "uq_scheduled_transfers_reversal_entry",
            "reversal_entry_id",
            unique=True,
            sqlite_where=text("reversal_entry_id IS NOT NULL"),
        ),
        Index("ix_scheduled_transfers_status_due", "status", "due_at_utc"),
        Index("ix_scheduled_transfers_lease_expires_at", "lease_expires_at"),
        Index("ix_scheduled_transfers_card_id", "card_id"),
        Index("ix_scheduled_transfers_source_wallet_id", "source_wallet_id"),
        Index("ix_scheduled_transfers_source_account_id", "source_account_id"),
        Index(
            "ix_scheduled_transfers_destination_account_id",
            "destination_account_id",
        ),
        Index("ix_scheduled_transfers_destination_wallet_id", "destination_wallet_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    source_account_id: Mapped[int] = mapped_column(
        ForeignKey("financial_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    source_wallet_id: Mapped[int] = mapped_column(
        ForeignKey("wallets.id", ondelete="RESTRICT"), nullable=False
    )
    destination_account_id: Mapped[int] = mapped_column(
        ForeignKey("financial_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    destination_wallet_id: Mapped[int | None] = mapped_column(
        ForeignKey("wallets.id", ondelete="RESTRICT")
    )
    card_id: Mapped[int | None] = mapped_column(
        ForeignKey("cards.id", ondelete="RESTRICT")
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    scheduled_local_date: Mapped[date] = mapped_column(Date, nullable=False)
    scheduled_local_time: Mapped[time] = mapped_column(Time, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    due_at_utc: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    execution_policy: Mapped[str] = mapped_column(
        String(32), nullable=False, default="assume_at_due_time"
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="scheduled")
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    executed_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("journal_entries.id", ondelete="RESTRICT")
    )
    reversal_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("journal_entries.id", ondelete="RESTRICT")
    )
    lease_owner: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    reversed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error_message: Mapped[str | None] = mapped_column(String(200))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class BalanceSnapshotModel(TimestampMixin, Base):
    """One immutable observation of what a bank account actually held.

    A snapshot changes no ledger balance (ADR-B066). Revision 0014 refuses
    every UPDATE and DELETE with a trigger, so what was observed can never be
    rewritten to make a difference disappear. ``observed_balance_minor`` is
    signed and may be zero: a real account can be empty or overdrawn.
    """

    __tablename__ = "balance_snapshots"
    __table_args__ = (
        CheckConstraint(
            _enum_check("source", BalanceSnapshotSource),
            name="balance_snapshot_source",
        ),
        CheckConstraint(
            "note IS NULL OR length(note) <= 200",
            name="balance_snapshot_note_length",
        ),
        UniqueConstraint("idempotency_key", name="balance_snapshot_idempotency"),
        Index(
            "ix_balance_snapshots_account_observed_at",
            "financial_account_id",
            "observed_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    financial_account_id: Mapped[int] = mapped_column(
        ForeignKey("financial_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    observed_balance_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    note: Mapped[str | None] = mapped_column(String(200))
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)


class ScheduledTransferCommitmentModel(Base):
    """One immutable share of a commitment that a scheduled movement settles.

    There is no ``updated_at``: revision 0013 refuses every UPDATE and DELETE
    with a trigger, because a link is the evidence of what a posted movement
    paid for (ADR-B052).
    """

    __tablename__ = "scheduled_transfer_commitments"
    __table_args__ = (
        CheckConstraint(
            "amount_minor > 0", name="scheduled_transfer_commitment_amount_positive"
        ),
        Index(
            "ix_scheduled_transfer_commitments_funding_commitment_id",
            "funding_commitment_id",
        ),
    )

    scheduled_transfer_id: Mapped[int] = mapped_column(
        ForeignKey("scheduled_transfers.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    funding_commitment_id: Mapped[int] = mapped_column(
        ForeignKey("funding_commitments.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )

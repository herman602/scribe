"""add budget allocation and posting

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-09 09:00:00.000000

This is the one rebuild of ``journal_entries`` authorized by ADR-B027. It uses
the protocol in ``env.py``: foreign keys are disabled outside the transaction,
the whole migration runs under ``BEGIN IMMEDIATE``, and a complete
``PRAGMA foreign_key_check`` must pass before the transaction commits.

The rebuild converts ``entry_type`` from an ``IN (...)`` check constraint into a
foreign key onto the new ``journal_entry_types`` reference table (ADR-B028), so
a later milestone adds a type by inserting one row instead of rebuilding a
financial table again. It also replaces the B2 reversal-pairing triggers with a
real table check constraint and declares ``financial_event_id`` as a named,
reflectable foreign key, which lets ``env.py`` drop its autogenerate exception.
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CURRENCY_CHECK = "length({0}) = 3 AND {0} GLOB '[A-Z][A-Z][A-Z]'"
_REVERSAL_PAIRING = (
    "(entry_type = 'reversal' AND reversal_of_id IS NOT NULL) OR "
    "(entry_type <> 'reversal' AND reversal_of_id IS NULL)"
)
_LEGACY_ENTRY_TYPES = ("opening_balance", "internal_transfer", "adjustment", "reversal")
_B3_ENTRY_TYPES = ("expense", "refund", "budget_close")
_PAIRING_OPERATIONS = ("insert", "update")


def _journal_entries_source() -> sa.Table:
    """Restate journal_entries so the rebuild never relies on reflection.

    The B2 column-level foreign key is restated here as a named table-level
    constraint, which is what makes it reflectable afterwards.
    """
    return sa.Table(
        "journal_entries",
        sa.MetaData(),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("entry_type", sa.String(length=40), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("reversal_of_id", sa.Integer(), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("financial_event_id", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_journal_entries")),
        sa.UniqueConstraint("idempotency_key", name="journal_entry_idempotency"),
        sa.UniqueConstraint("reversal_of_id", name="journal_entry_single_reversal"),
        sa.CheckConstraint(
            "reversal_of_id IS NULL OR reversal_of_id <> id",
            name=op.f("ck_journal_entries_journal_entry_reversal_not_self"),
        ),
        sa.ForeignKeyConstraint(
            ["reversal_of_id"],
            ["journal_entries.id"],
            name=op.f("fk_journal_entries_reversal_of_id_journal_entries"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["financial_event_id"],
            ["financial_events.id"],
            name=op.f("fk_journal_entries_financial_event_id_financial_events"),
            ondelete="RESTRICT",
        ),
    )


def _financial_events_source(*, with_refund_link: bool) -> sa.Table:
    """Restate financial_events so the refund constraint can be added or removed.

    SQLite reflection does not expose a named check constraint to
    ``batch_op.drop_constraint``, and the check references the column being
    dropped, so both directions restate the table explicitly.
    """
    columns: list[Any] = [
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("origin", sa.String(length=32), nullable=False),
        sa.Column("source_import_key", sa.String(length=200), nullable=True),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("payment_method", sa.String(length=32), nullable=False),
        sa.Column("recognition_basis", sa.String(length=32), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("merchant", sa.Text(), nullable=True),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("card_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    ]
    if with_refund_link:
        columns.append(sa.Column("refund_of_event_id", sa.Integer(), nullable=True))
    constraints: list[Any] = [
        sa.PrimaryKeyConstraint("id", name=op.f("pk_financial_events")),
        sa.UniqueConstraint("idempotency_key", name="financial_event_idempotency"),
        sa.CheckConstraint(
            "amount_minor > 0",
            name=op.f("ck_financial_events_financial_event_amount_positive_magnitude"),
        ),
        sa.CheckConstraint(
            _CURRENCY_CHECK.format("currency"),
            name=op.f("ck_financial_events_financial_event_currency"),
        ),
        sa.CheckConstraint(
            "event_type IN ('purchase', 'refund', 'transfer', 'withdrawal', 'deposit', 'bill_payment', 'subscription', 'fee', 'interest_income', 'direct_debit', 'adjustment')",
            name=op.f("ck_financial_events_financial_event_type"),
        ),
        sa.CheckConstraint(
            "origin IN ('gmail_transaction', 'manual', 'import', 'system')",
            name=op.f("ck_financial_events_financial_event_origin"),
        ),
        sa.CheckConstraint(
            "payment_method IN ('cash', 'credit_card', 'bank_account', 'transfer', 'other')",
            name=op.f("ck_financial_events_financial_event_payment_method"),
        ),
        sa.CheckConstraint(
            "recognition_basis IN ('observed_manual', 'gmail_notification', 'imported', 'scheduled_assumption')",
            name=op.f("ck_financial_events_financial_event_recognition_basis"),
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'needs_allocation', 'ready', 'posted', 'ignored', 'reversed')",
            name=op.f("ck_financial_events_financial_event_status"),
        ),
        sa.CheckConstraint(
            "version >= 1",
            name=op.f("ck_financial_events_financial_event_version_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["card_id"],
            ["cards.id"],
            name=op.f("fk_financial_events_card_id_cards"),
            ondelete="RESTRICT",
        ),
    ]
    if with_refund_link:
        constraints.extend(
            (
                sa.CheckConstraint(
                    "refund_of_event_id IS NULL OR "
                    "(event_type = 'refund' AND refund_of_event_id <> id)",
                    name=op.f("ck_financial_events_financial_event_refund_shape"),
                ),
                sa.ForeignKeyConstraint(
                    ["refund_of_event_id"],
                    ["financial_events.id"],
                    name=op.f(
                        "fk_financial_events_refund_of_event_id_financial_events"
                    ),
                    ondelete="RESTRICT",
                ),
            )
        )
    return sa.Table("financial_events", sa.MetaData(), *columns, *constraints)


def _ledger_accounts_source(*, with_category: bool) -> sa.Table:
    """Restate ledger_accounts so the category shape constraints can be added."""
    columns: list[Any] = [
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=200), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("financial_account_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    ]
    if with_category:
        columns.append(sa.Column("budget_category_id", sa.Integer(), nullable=True))
    constraints: list[Any] = [
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ledger_accounts")),
        sa.UniqueConstraint("code", name="ledger_account_code"),
        sa.UniqueConstraint("financial_account_id", name="ledger_account_physical"),
        sa.CheckConstraint(
            "kind IN ('asset', 'liability', 'expense', 'income', 'equity', 'clearing')",
            name=op.f("ck_ledger_accounts_ledger_account_kind"),
        ),
        sa.CheckConstraint(
            _CURRENCY_CHECK.format("currency"),
            name=op.f("ck_ledger_accounts_ledger_account_currency"),
        ),
        sa.ForeignKeyConstraint(
            ["financial_account_id"],
            ["financial_accounts.id"],
            name=op.f("fk_ledger_accounts_financial_account_id_financial_accounts"),
            ondelete="RESTRICT",
        ),
    ]
    if with_category:
        constraints.extend(
            (
                # A category only ever describes spending, and a category
                # ledger is nominal, never a real balance container.
                sa.CheckConstraint(
                    "budget_category_id IS NULL OR kind = 'expense'",
                    name=op.f("ck_ledger_accounts_ledger_account_category_is_expense"),
                ),
                sa.CheckConstraint(
                    "budget_category_id IS NULL OR financial_account_id IS NULL",
                    name=op.f(
                        "ck_ledger_accounts_ledger_account_category_not_physical"
                    ),
                ),
                sa.ForeignKeyConstraint(
                    ["budget_category_id"],
                    ["budget_categories.id"],
                    name=op.f(
                        "fk_ledger_accounts_budget_category_id_budget_categories"
                    ),
                    ondelete="RESTRICT",
                ),
            )
        )
    return sa.Table("ledger_accounts", sa.MetaData(), *columns, *constraints)


def upgrade() -> None:
    """Add the budget vertical slice and the journal entry-type registry."""
    op.create_table(
        "journal_entry_types",
        sa.Column("code", sa.String(length=40), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_journal_entry_types")),
    )
    op.bulk_insert(
        sa.table(
            "journal_entry_types",
            sa.column("code", sa.String),
            sa.column("description", sa.Text),
        ),
        [
            {"code": code, "description": code.replace("_", " ")}
            for code in (*_LEGACY_ENTRY_TYPES, *_B3_ENTRY_TYPES)
        ],
    )

    for operation in _PAIRING_OPERATIONS:
        op.execute(f"DROP TRIGGER journal_entry_reversal_pairing_{operation}")
    op.drop_index("ix_journal_entries_financial_event_id", table_name="journal_entries")
    with op.batch_alter_table(
        "journal_entries", copy_from=_journal_entries_source()
    ) as batch_op:
        batch_op.create_check_constraint(
            "journal_entry_reversal_pairing", _REVERSAL_PAIRING
        )
        batch_op.create_foreign_key(
            op.f("fk_journal_entries_entry_type_journal_entry_types"),
            "journal_entry_types",
            ["entry_type"],
            ["code"],
            ondelete="RESTRICT",
        )
    op.create_index(
        "ix_journal_entries_effective_at",
        "journal_entries",
        ["effective_at"],
        unique=False,
    )
    op.create_index(
        "ix_journal_entries_entry_type", "journal_entries", ["entry_type"], unique=False
    )
    op.create_index(
        "uq_journal_entries_financial_event",
        "journal_entries",
        ["financial_event_id"],
        unique=True,
        sqlite_where=sa.text("financial_event_id IS NOT NULL"),
    )

    op.create_table(
        "budget_categories",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "is_active IN (0, 1)", name=op.f("ck_budget_categories_category_boolean")
        ),
        sa.CheckConstraint(
            "parent_id IS NULL OR parent_id <> id",
            name=op.f("ck_budget_categories_category_parent_not_self"),
        ),
        sa.CheckConstraint(
            "version >= 1", name=op.f("ck_budget_categories_category_version_positive")
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["budget_categories.id"],
            name=op.f("fk_budget_categories_parent_id_budget_categories"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_budget_categories")),
    )
    op.create_index(
        "uq_budget_categories_child_name",
        "budget_categories",
        ["parent_id", "name"],
        unique=True,
        sqlite_where=sa.text("parent_id IS NOT NULL"),
    )
    op.create_index(
        "uq_budget_categories_root_name",
        "budget_categories",
        ["name"],
        unique=True,
        sqlite_where=sa.text("parent_id IS NULL"),
    )

    op.create_table(
        "budget_periods",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("month", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            _CURRENCY_CHECK.format("currency"),
            name=op.f("ck_budget_periods_budget_period_currency"),
        ),
        sa.CheckConstraint(
            "month >= 1 AND month <= 12",
            name=op.f("ck_budget_periods_budget_period_month_range"),
        ),
        sa.CheckConstraint(
            "status IN ('open', 'closed')",
            name=op.f("ck_budget_periods_budget_period_status"),
        ),
        sa.CheckConstraint(
            "version >= 1",
            name=op.f("ck_budget_periods_budget_period_version_positive"),
        ),
        sa.CheckConstraint(
            "year >= 2000 AND year <= 2200",
            name=op.f("ck_budget_periods_budget_period_year_range"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_budget_periods")),
        sa.UniqueConstraint("year", "month", "currency", name="budget_period_identity"),
    )

    op.create_table(
        "budget_targets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("budget_period_id", sa.Integer(), nullable=False),
        sa.Column("budget_category_id", sa.Integer(), nullable=False),
        sa.Column("target_minor", sa.BigInteger(), nullable=False),
        sa.Column("buffer_minor", sa.BigInteger(), nullable=False),
        sa.Column("funding_wallet_id", sa.Integer(), nullable=False),
        sa.Column("month_end_destination_wallet_id", sa.Integer(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "buffer_minor >= 0",
            name=op.f("ck_budget_targets_budget_buffer_nonnegative"),
        ),
        sa.CheckConstraint(
            "target_minor >= 0",
            name=op.f("ck_budget_targets_budget_target_nonnegative"),
        ),
        sa.CheckConstraint(
            "version >= 1",
            name=op.f("ck_budget_targets_budget_target_version_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["budget_category_id"],
            ["budget_categories.id"],
            name=op.f("fk_budget_targets_budget_category_id_budget_categories"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["budget_period_id"],
            ["budget_periods.id"],
            name=op.f("fk_budget_targets_budget_period_id_budget_periods"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["funding_wallet_id"],
            ["wallets.id"],
            name=op.f("fk_budget_targets_funding_wallet_id_wallets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["month_end_destination_wallet_id"],
            ["wallets.id"],
            name=op.f("fk_budget_targets_month_end_destination_wallet_id_wallets"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_budget_targets")),
        sa.UniqueConstraint(
            "budget_period_id", "budget_category_id", name="budget_target_identity"
        ),
    )

    op.create_table(
        "event_allocations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("financial_event_id", sa.Integer(), nullable=False),
        sa.Column("budget_category_id", sa.Integer(), nullable=True),
        sa.Column("funding_wallet_id", sa.Integer(), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("allocation_source", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "allocation_source IN ('rule', 'manual', 'import')",
            name=op.f("ck_event_allocations_event_allocation_source"),
        ),
        sa.CheckConstraint(
            "amount_minor > 0",
            name=op.f("ck_event_allocations_event_allocation_amount_positive"),
        ),
        sa.CheckConstraint(
            _CURRENCY_CHECK.format("currency"),
            name=op.f("ck_event_allocations_event_allocation_currency"),
        ),
        sa.ForeignKeyConstraint(
            ["budget_category_id"],
            ["budget_categories.id"],
            name=op.f("fk_event_allocations_budget_category_id_budget_categories"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["financial_event_id"],
            ["financial_events.id"],
            name=op.f("fk_event_allocations_financial_event_id_financial_events"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["funding_wallet_id"],
            ["wallets.id"],
            name=op.f("fk_event_allocations_funding_wallet_id_wallets"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event_allocations")),
    )
    op.create_index(
        "ix_event_allocations_financial_event_id",
        "event_allocations",
        ["financial_event_id"],
        unique=False,
    )

    op.create_table(
        "funding_commitments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("financial_event_id", sa.Integer(), nullable=False),
        sa.Column("event_allocation_id", sa.Integer(), nullable=False),
        sa.Column("source_wallet_id", sa.Integer(), nullable=False),
        sa.Column("settlement_account_id", sa.Integer(), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("released_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("fulfillment_mode", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "amount_minor > 0",
            name=op.f("ck_funding_commitments_funding_commitment_amount_positive"),
        ),
        sa.CheckConstraint(
            "released_minor >= 0 AND released_minor <= amount_minor",
            name=op.f("ck_funding_commitments_funding_commitment_released_bounded"),
        ),
        sa.CheckConstraint(
            _CURRENCY_CHECK.format("currency"),
            name=op.f("ck_funding_commitments_funding_commitment_currency"),
        ),
        sa.CheckConstraint(
            "fulfillment_mode IN ('reserve_only', 'account_transfer', 'cross_bank_transfer')",
            name=op.f("ck_funding_commitments_funding_commitment_fulfillment_mode"),
        ),
        sa.CheckConstraint(
            "status IN ('open', 'partially_scheduled', 'scheduled', 'funded', 'cancelled')",
            name=op.f("ck_funding_commitments_funding_commitment_status"),
        ),
        sa.ForeignKeyConstraint(
            ["event_allocation_id"],
            ["event_allocations.id"],
            name=op.f("fk_funding_commitments_event_allocation_id_event_allocations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["financial_event_id"],
            ["financial_events.id"],
            name=op.f("fk_funding_commitments_financial_event_id_financial_events"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["settlement_account_id"],
            ["financial_accounts.id"],
            name=op.f(
                "fk_funding_commitments_settlement_account_id_financial_accounts"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_wallet_id"],
            ["wallets.id"],
            name=op.f("fk_funding_commitments_source_wallet_id_wallets"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_funding_commitments")),
        sa.UniqueConstraint("event_allocation_id", name="funding_commitment_identity"),
    )
    op.create_index(
        "ix_funding_commitments_source_status",
        "funding_commitments",
        ["source_wallet_id", "status"],
        unique=False,
    )

    # Row-level shape only: a refund must name another event, and only a refund
    # may name one. Whether that target is itself a refund is a cross-row rule
    # the B3B service enforces.
    #
    # The column is added with a plain ALTER first so the batch copy can select
    # it, and the rebuild then applies the final definition. Declaring the
    # check inside the batch instead silently does nothing.
    op.add_column(
        "financial_events",
        sa.Column("refund_of_event_id", sa.Integer(), nullable=True),
    )
    with op.batch_alter_table(
        "financial_events",
        copy_from=_financial_events_source(with_refund_link=True),
        recreate="always",
    ):
        pass
    op.create_index(
        "ix_financial_events_card_id", "financial_events", ["card_id"], unique=False
    )
    op.create_index(
        "ix_financial_events_daily_activity",
        "financial_events",
        ["occurred_at", "payment_method", "status"],
        unique=False,
    )
    op.create_index(
        "ix_financial_events_status", "financial_events", ["status"], unique=False
    )
    op.create_index(
        "ix_financial_events_refund_of_event_id",
        "financial_events",
        ["refund_of_event_id"],
        unique=False,
    )

    op.add_column(
        "ledger_accounts", sa.Column("budget_category_id", sa.Integer(), nullable=True)
    )
    with op.batch_alter_table(
        "ledger_accounts",
        copy_from=_ledger_accounts_source(with_category=True),
        recreate="always",
    ):
        pass
    op.create_index(
        "ix_ledger_accounts_kind", "ledger_accounts", ["kind"], unique=False
    )
    op.create_index(
        "uq_ledger_accounts_category_currency",
        "ledger_accounts",
        ["budget_category_id", "currency"],
        unique=True,
        sqlite_where=sa.text("budget_category_id IS NOT NULL"),
    )

    with op.batch_alter_table("journal_postings") as batch_op:
        batch_op.add_column(
            sa.Column("budget_category_id", sa.Integer(), nullable=True)
        )
        batch_op.create_foreign_key(
            op.f("fk_journal_postings_budget_category_id_budget_categories"),
            "budget_categories",
            ["budget_category_id"],
            ["id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    """Remove the budget slice and restore the B2 journal shape."""
    with op.batch_alter_table("journal_postings") as batch_op:
        batch_op.drop_constraint(
            op.f("fk_journal_postings_budget_category_id_budget_categories"),
            type_="foreignkey",
        )
        batch_op.drop_column("budget_category_id")

    op.drop_index("uq_ledger_accounts_category_currency", table_name="ledger_accounts")
    with op.batch_alter_table(
        "ledger_accounts",
        copy_from=_ledger_accounts_source(with_category=True),
        recreate="always",
    ) as batch_op:
        batch_op.drop_constraint(
            op.f("ck_ledger_accounts_ledger_account_category_is_expense"),
            type_="check",
        )
        batch_op.drop_constraint(
            op.f("ck_ledger_accounts_ledger_account_category_not_physical"),
            type_="check",
        )
        batch_op.drop_constraint(
            op.f("fk_ledger_accounts_budget_category_id_budget_categories"),
            type_="foreignkey",
        )
        batch_op.drop_column("budget_category_id")
    op.create_index(
        "ix_ledger_accounts_kind", "ledger_accounts", ["kind"], unique=False
    )

    op.drop_index(
        "ix_financial_events_refund_of_event_id", table_name="financial_events"
    )
    with op.batch_alter_table(
        "financial_events",
        copy_from=_financial_events_source(with_refund_link=True),
        recreate="always",
    ) as batch_op:
        # The check references the column, so it has to go first. copy_from is
        # what makes the named constraint findable here; SQLite reflection
        # would not expose it.
        batch_op.drop_constraint(
            op.f("ck_financial_events_financial_event_refund_shape"), type_="check"
        )
        batch_op.drop_constraint(
            op.f("fk_financial_events_refund_of_event_id_financial_events"),
            type_="foreignkey",
        )
        batch_op.drop_column("refund_of_event_id")
    op.create_index(
        "ix_financial_events_card_id", "financial_events", ["card_id"], unique=False
    )
    op.create_index(
        "ix_financial_events_daily_activity",
        "financial_events",
        ["occurred_at", "payment_method", "status"],
        unique=False,
    )
    op.create_index(
        "ix_financial_events_status", "financial_events", ["status"], unique=False
    )

    op.drop_index(
        "ix_funding_commitments_source_status", table_name="funding_commitments"
    )
    op.drop_table("funding_commitments")
    op.drop_index(
        "ix_event_allocations_financial_event_id", table_name="event_allocations"
    )
    op.drop_table("event_allocations")
    op.drop_table("budget_targets")
    op.drop_table("budget_periods")
    op.drop_index("uq_budget_categories_root_name", table_name="budget_categories")
    op.drop_index("uq_budget_categories_child_name", table_name="budget_categories")
    op.drop_table("budget_categories")

    op.drop_index("uq_journal_entries_financial_event", table_name="journal_entries")
    op.drop_index("ix_journal_entries_entry_type", table_name="journal_entries")
    op.drop_index("ix_journal_entries_effective_at", table_name="journal_entries")
    restored = _journal_entries_source()
    restored.append_constraint(
        sa.CheckConstraint(
            "entry_type IN ('opening_balance', 'internal_transfer', 'adjustment', 'reversal')",
            name=op.f("ck_journal_entries_journal_entry_type"),
        )
    )
    # An empty batch block would not rebuild anything, so the recreate is
    # explicit: this is what removes the entry_type foreign key again.
    with op.batch_alter_table("journal_entries", copy_from=restored, recreate="always"):
        pass
    op.create_index(
        "ix_journal_entries_effective_at",
        "journal_entries",
        ["effective_at"],
        unique=False,
    )
    op.create_index(
        "ix_journal_entries_entry_type", "journal_entries", ["entry_type"], unique=False
    )
    op.create_index(
        "ix_journal_entries_financial_event_id",
        "journal_entries",
        ["financial_event_id"],
        unique=False,
    )
    for operation in _PAIRING_OPERATIONS:
        op.execute(
            f"CREATE TRIGGER journal_entry_reversal_pairing_{operation} "
            f"BEFORE {operation.upper()} ON journal_entries FOR EACH ROW "
            "WHEN (NEW.entry_type = 'reversal') <> (NEW.reversal_of_id IS NOT NULL) "
            "BEGIN SELECT RAISE(ABORT, "
            "'journal entry reversal pairing violated'); END"
        )
    op.drop_table("journal_entry_types")

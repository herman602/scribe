"""add immutable ledger foundation

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-08 20:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CURRENCY_CHECK = "length({0}) = 3 AND {0} GLOB '[A-Z][A-Z][A-Z]'"


def upgrade() -> None:
    """Create accounts, wallets, ledger accounts, and the immutable journal."""
    op.create_table(
        "financial_accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("institution_code", sa.String(length=100), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("account_type", sa.String(length=32), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("masked_reference", sa.String(length=32), nullable=True),
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
            "account_type IN ('checking', 'savings', 'cash', 'card_liability', 'other')",
            name=op.f("ck_financial_accounts_financial_account_type"),
        ),
        sa.CheckConstraint(
            _CURRENCY_CHECK.format("currency"),
            name=op.f("ck_financial_accounts_financial_account_currency"),
        ),
        sa.CheckConstraint(
            "is_active IN (0, 1)",
            name=op.f("ck_financial_accounts_financial_account_active_boolean"),
        ),
        sa.CheckConstraint(
            "version >= 1",
            name=op.f("ck_financial_accounts_financial_account_version_positive"),
        ),
        sa.CheckConstraint(
            "masked_reference IS NULL OR masked_reference NOT GLOB '*[0-9][0-9][0-9][0-9][0-9]*'",
            name=op.f("ck_financial_accounts_financial_account_masked_reference"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_financial_accounts")),
    )
    op.create_index(
        "ix_financial_accounts_institution_code",
        "financial_accounts",
        ["institution_code"],
        unique=False,
    )
    op.create_index(
        "uq_financial_accounts_active_identity",
        "financial_accounts",
        ["institution_code", "display_name", "currency"],
        unique=True,
        sqlite_where=sa.text("is_active = 1"),
    )

    op.create_table(
        "wallets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("financial_account_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
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
            "purpose IN ('general', 'budget', 'clearing_hub', 'unallocated')",
            name=op.f("ck_wallets_wallet_purpose"),
        ),
        sa.CheckConstraint(
            "is_active IN (0, 1)", name=op.f("ck_wallets_wallet_active_boolean")
        ),
        sa.CheckConstraint(
            "version >= 1", name=op.f("ck_wallets_wallet_version_positive")
        ),
        sa.ForeignKeyConstraint(
            ["financial_account_id"],
            ["financial_accounts.id"],
            name=op.f("fk_wallets_financial_account_id_financial_accounts"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wallets")),
        sa.UniqueConstraint("financial_account_id", "name", name="wallet_account_name"),
    )
    op.create_index(
        "ix_wallets_financial_account_id",
        "wallets",
        ["financial_account_id"],
        unique=False,
    )
    op.create_index(
        "uq_wallets_active_unallocated",
        "wallets",
        ["financial_account_id"],
        unique=True,
        sqlite_where=sa.text("purpose = 'unallocated' AND is_active = 1"),
    )

    op.create_table(
        "ledger_accounts",
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ledger_accounts")),
        sa.UniqueConstraint("code", name="ledger_account_code"),
        sa.UniqueConstraint("financial_account_id", name="ledger_account_physical"),
    )
    op.create_index(
        "ix_ledger_accounts_kind", "ledger_accounts", ["kind"], unique=False
    )

    op.create_table(
        "journal_entries",
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
        sa.CheckConstraint(
            "entry_type IN ('opening_balance', 'internal_transfer', 'adjustment', 'reversal')",
            name=op.f("ck_journal_entries_journal_entry_type"),
        ),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_journal_entries")),
        sa.UniqueConstraint("idempotency_key", name="journal_entry_idempotency"),
        sa.UniqueConstraint("reversal_of_id", name="journal_entry_single_reversal"),
    )
    op.create_index(
        "ix_journal_entries_effective_at",
        "journal_entries",
        ["effective_at"],
        unique=False,
    )
    op.create_index(
        "ix_journal_entries_entry_type",
        "journal_entries",
        ["entry_type"],
        unique=False,
    )

    op.create_table(
        "journal_postings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("journal_entry_id", sa.Integer(), nullable=False),
        sa.Column("ledger_account_id", sa.Integer(), nullable=False),
        sa.Column("wallet_id", sa.Integer(), nullable=True),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
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
            "amount_minor <> 0",
            name=op.f("ck_journal_postings_journal_posting_amount_nonzero"),
        ),
        sa.CheckConstraint(
            _CURRENCY_CHECK.format("currency"),
            name=op.f("ck_journal_postings_journal_posting_currency"),
        ),
        sa.ForeignKeyConstraint(
            ["journal_entry_id"],
            ["journal_entries.id"],
            name=op.f("fk_journal_postings_journal_entry_id_journal_entries"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["ledger_account_id"],
            ["ledger_accounts.id"],
            name=op.f("fk_journal_postings_ledger_account_id_ledger_accounts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["wallet_id"],
            ["wallets.id"],
            name=op.f("fk_journal_postings_wallet_id_wallets"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_journal_postings")),
    )
    op.create_index(
        "ix_journal_postings_account_wallet",
        "journal_postings",
        ["ledger_account_id", "wallet_id"],
        unique=False,
    )
    op.create_index(
        "ix_journal_postings_journal_entry_id",
        "journal_postings",
        ["journal_entry_id"],
        unique=False,
    )
    op.create_index(
        "ix_journal_postings_wallet_id",
        "journal_postings",
        ["wallet_id"],
        unique=False,
    )


def downgrade() -> None:
    """Remove the ledger foundation without touching Gmail tables."""
    op.drop_index("ix_journal_postings_wallet_id", table_name="journal_postings")
    op.drop_index("ix_journal_postings_journal_entry_id", table_name="journal_postings")
    op.drop_index("ix_journal_postings_account_wallet", table_name="journal_postings")
    op.drop_table("journal_postings")
    op.drop_index("ix_journal_entries_entry_type", table_name="journal_entries")
    op.drop_index("ix_journal_entries_effective_at", table_name="journal_entries")
    op.drop_table("journal_entries")
    op.drop_index("ix_ledger_accounts_kind", table_name="ledger_accounts")
    op.drop_table("ledger_accounts")
    op.drop_index("uq_wallets_active_unallocated", table_name="wallets")
    op.drop_index("ix_wallets_financial_account_id", table_name="wallets")
    op.drop_table("wallets")
    op.drop_index(
        "uq_financial_accounts_active_identity", table_name="financial_accounts"
    )
    op.drop_index(
        "ix_financial_accounts_institution_code", table_name="financial_accounts"
    )
    op.drop_table("financial_accounts")

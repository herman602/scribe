"""add cards and financial event intake

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-08 22:00:00.000000

``financial_events`` is created before ``journal_entries`` gains its foreign
key to it. The downgrade removes that foreign key first and only then drops
``financial_events``.

``journal_entries`` is deliberately extended in place instead of through a
batch table rebuild. A rebuild has to ``DROP TABLE journal_entries``, and that
implicit row-by-row delete violates the table's own
``reversal_of_id -> journal_entries.id`` RESTRICT foreign key as soon as one
reversal row exists; SQLite cannot disable foreign key enforcement inside a
migration transaction. ``ALTER TABLE ... ADD COLUMN`` with an inline
``REFERENCES`` clause, and ``ALTER TABLE ... DROP COLUMN`` on the way back,
avoid the rebuild entirely while preserving every existing row, index, and
foreign key.

The paired reversal invariant ``entry_type = 'reversal' <=> reversal_of_id IS
NOT NULL`` is enforced by BEFORE INSERT and BEFORE UPDATE triggers, because
SQLite cannot add a CHECK constraint to an existing table without that same
rebuild. The triggers give the identical database-level guarantee for every
later write, and are dropped again on downgrade. Because a trigger cannot
validate rows that already exist, the upgrade first refuses to run when any
pre-0006 row already violates the invariant.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CURRENCY_CHECK = "length({0}) = 3 AND {0} GLOB '[A-Z][A-Z][A-Z]'"
_REVERSAL_PAIRING_VIOLATED = (
    "(NEW.entry_type = 'reversal') <> (NEW.reversal_of_id IS NOT NULL)"
)


_ADD_EVENT_LINK = (
    "ALTER TABLE journal_entries ADD COLUMN financial_event_id INTEGER "
    "CONSTRAINT fk_journal_entries_financial_event_id_financial_events "
    "REFERENCES financial_events (id) ON DELETE RESTRICT"
)


def _journal_entries_with_event_link() -> sa.Table:
    """Restate the shape this revision leaves behind, for the downgrade.

    ``ALTER TABLE ... DROP COLUMN`` cannot be used: revision 0007 rebuilds
    ``journal_entries`` with a table-level foreign key on this column, and
    SQLite refuses to drop a column named in a foreign key definition. The
    ADR-B027 protocol disables foreign keys during migrations, so a rebuild is
    safe here.
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
        sa.ForeignKeyConstraint(
            ["financial_event_id"],
            ["financial_events.id"],
            name=op.f("fk_journal_entries_financial_event_id_financial_events"),
            ondelete="RESTRICT",
        ),
    )


_PAIRING_OPERATIONS = ("insert", "update")


_EXISTING_PAIRING_VIOLATIONS = (
    "SELECT COUNT(*) FROM journal_entries WHERE"
    " (entry_type = 'reversal') <> (reversal_of_id IS NOT NULL)"
)


def _reject_existing_pairing_violations() -> None:
    """Refuse to claim an invariant the existing rows do not already satisfy."""
    violations = op.get_bind().execute(sa.text(_EXISTING_PAIRING_VIOLATIONS)).scalar()
    if violations:
        raise RuntimeError(
            f"{violations} existing journal entries violate the reversal pairing "
            "invariant; correct them with reversal entries before upgrading"
        )


def _pairing_trigger(operation: str) -> str:
    """Return the DDL enforcing the reversal pairing invariant in SQLite."""
    return (
        f"CREATE TRIGGER journal_entry_reversal_pairing_{operation} "
        f"BEFORE {operation.upper()} ON journal_entries FOR EACH ROW "
        f"WHEN ({_REVERSAL_PAIRING_VIOLATED}) "
        "BEGIN SELECT RAISE(ABORT, 'journal entry reversal pairing violated'); END"
    )


def upgrade() -> None:
    """Create cards, canonical events, source links, and the journal link."""
    _reject_existing_pairing_violations()
    op.create_table(
        "cards",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("gmail_account_id", sa.Integer(), nullable=False),
        sa.Column("issuer_code", sa.String(length=100), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("last_four", sa.String(length=4), nullable=False),
        sa.Column("holder_role", sa.String(length=32), nullable=False),
        sa.Column("configuration_status", sa.String(length=32), nullable=False),
        sa.Column("liability_account_id", sa.Integer(), nullable=True),
        sa.Column("settlement_account_id", sa.Integer(), nullable=True),
        sa.Column("default_funding_wallet_id", sa.Integer(), nullable=True),
        sa.Column("active_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("active_to", sa.DateTime(timezone=True), nullable=True),
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
            "configuration_status IN ('incomplete', 'ready')",
            name=op.f("ck_cards_card_configuration_status"),
        ),
        sa.CheckConstraint(
            "configuration_status <> 'ready' OR (liability_account_id IS NOT NULL AND settlement_account_id IS NOT NULL)",
            name=op.f("ck_cards_card_ready_requires_accounts"),
        ),
        sa.CheckConstraint(
            "holder_role IN ('primary', 'supplementary', 'unknown')",
            name=op.f("ck_cards_card_holder_role"),
        ),
        sa.CheckConstraint(
            "length(last_four) = 4 AND last_four GLOB '[0-9][0-9][0-9][0-9]'",
            name=op.f("ck_cards_card_last_four"),
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_cards_card_version_positive")),
        sa.ForeignKeyConstraint(
            ["default_funding_wallet_id"],
            ["wallets.id"],
            name=op.f("fk_cards_default_funding_wallet_id_wallets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["gmail_account_id"],
            ["gmail_accounts.id"],
            name=op.f("fk_cards_gmail_account_id_gmail_accounts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["liability_account_id"],
            ["financial_accounts.id"],
            name=op.f("fk_cards_liability_account_id_financial_accounts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["settlement_account_id"],
            ["financial_accounts.id"],
            name=op.f("fk_cards_settlement_account_id_financial_accounts"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cards")),
    )
    op.create_index(
        "ix_cards_identity_lookup",
        "cards",
        ["gmail_account_id", "issuer_code", "last_four"],
        unique=False,
    )
    op.create_index(
        "uq_cards_active_identity",
        "cards",
        ["gmail_account_id", "issuer_code", "last_four"],
        unique=True,
        sqlite_where=sa.text("active_to IS NULL"),
    )

    op.create_table(
        "financial_events",
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_financial_events")),
        sa.UniqueConstraint("idempotency_key", name="financial_event_idempotency"),
    )
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

    op.create_table(
        "financial_event_source_links",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("financial_event_id", sa.Integer(), nullable=False),
        sa.Column("transaction_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("match_score_bps", sa.Integer(), nullable=False),
        sa.Column("reason_json", sa.JSON(), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
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
            "match_score_bps >= 0 AND match_score_bps <= 10000",
            name=op.f("ck_financial_event_source_links_source_link_score_range"),
        ),
        sa.CheckConstraint(
            "status IN ('candidate', 'confirmed', 'rejected')",
            name=op.f("ck_financial_event_source_links_source_link_status"),
        ),
        sa.ForeignKeyConstraint(
            ["financial_event_id"],
            ["financial_events.id"],
            name=op.f(
                "fk_financial_event_source_links_financial_event_id_financial_events"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["transactions.id"],
            name=op.f("fk_financial_event_source_links_transaction_id_transactions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_financial_event_source_links")),
        sa.UniqueConstraint(
            "financial_event_id", "transaction_id", name="financial_event_source"
        ),
    )
    op.create_index(
        "ix_source_links_event_status",
        "financial_event_source_links",
        ["financial_event_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_source_links_transaction_status",
        "financial_event_source_links",
        ["transaction_id", "status"],
        unique=False,
    )
    op.create_index(
        "uq_source_links_one_confirmed_per_transaction",
        "financial_event_source_links",
        ["transaction_id"],
        unique=True,
        sqlite_where=sa.text("status = 'confirmed'"),
    )

    op.execute(_ADD_EVENT_LINK)
    for operation in _PAIRING_OPERATIONS:
        op.execute(_pairing_trigger(operation))
    op.create_index(
        "ix_journal_entries_financial_event_id",
        "journal_entries",
        ["financial_event_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the journal link first so financial_events can be removed safely."""
    for operation in _PAIRING_OPERATIONS:
        op.execute(f"DROP TRIGGER journal_entry_reversal_pairing_{operation}")
    op.drop_index("ix_journal_entries_financial_event_id", table_name="journal_entries")
    with op.batch_alter_table(
        "journal_entries",
        copy_from=_journal_entries_with_event_link(),
        recreate="always",
    ) as batch_op:
        batch_op.drop_column("financial_event_id")
    op.create_index(
        "ix_journal_entries_effective_at",
        "journal_entries",
        ["effective_at"],
        unique=False,
    )
    op.create_index(
        "ix_journal_entries_entry_type", "journal_entries", ["entry_type"], unique=False
    )

    op.drop_index(
        "uq_source_links_one_confirmed_per_transaction",
        table_name="financial_event_source_links",
    )
    op.drop_index(
        "ix_source_links_transaction_status",
        table_name="financial_event_source_links",
    )
    op.drop_index(
        "ix_source_links_event_status", table_name="financial_event_source_links"
    )
    op.drop_table("financial_event_source_links")

    op.drop_index("ix_financial_events_status", table_name="financial_events")
    op.drop_index("ix_financial_events_daily_activity", table_name="financial_events")
    op.drop_index("ix_financial_events_card_id", table_name="financial_events")
    op.drop_table("financial_events")

    op.drop_index("uq_cards_active_identity", table_name="cards")
    op.drop_index("ix_cards_identity_lookup", table_name="cards")
    op.drop_table("cards")

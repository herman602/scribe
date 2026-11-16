"""add the clearing hub's scheduled bank movements and their commitment links

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-09 21:30:00.000000

This revision only creates new tables, indexes, and triggers, and inserts one
row into the ``journal_entry_types`` registry. It rebuilds no existing table,
so the ADR-B027 protocol in ``env.py`` applies unchanged but has nothing to
copy: foreign keys are disabled outside the transaction, the whole migration
runs under ``BEGIN IMMEDIATE``, and a complete ``PRAGMA foreign_key_check``
must pass before it commits.

``card_payment`` is added by inserting one registry row (ADR-B028 and
ADR-B051). Clearing transfers reuse the existing ``funding_transfer`` type, no
``FinancialEventType`` is added, and ``financial_events`` is deliberately not
rebuilt: a scheduled movement is a bank instruction, not an economic event.

A schedule's economic columns freeze on insert (ADR-B053). SQLite cannot
express "these columns are writable and those are not" as a check constraint,
so BEFORE UPDATE triggers abort any statement that changes one of them,
whichever path reaches the table. The link rows are frozen even harder: UPDATE
and DELETE are both refused, and a BEFORE INSERT trigger refuses a link whose
live coverage would exceed what its commitment still requires — counting only
links whose schedule is neither cancelled nor reversed, so a cancelled
schedule gives its capacity back (ADR-B052).

The downgrade removes an accounting capability, not just a shape. It therefore
refuses, before any DDL, when schedules, links, or ``card_payment`` entries
exist, because dropping those tables would delete a recorded bank instruction
and removing the registry row would orphan a posted entry. A refusal changes
nothing: no schema, no data, and no revision.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CURRENCY_CHECK = "length({0}) = 3 AND {0} GLOB '[A-Z][A-Z][A-Z]'"
_B6_ENTRY_TYPES = ("card_payment",)
_B6_TABLES = ("scheduled_transfer_commitments", "scheduled_transfers")

_PURPOSE_SHAPE = (
    "(purpose = 'clearing_funding' AND card_id IS NULL "
    "AND destination_wallet_id IS NOT NULL) OR "
    "(purpose = 'card_autopay' AND card_id IS NOT NULL "
    "AND destination_wallet_id IS NULL)"
)
_STATUS_SHAPE = (
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
_LEASE_PAIRING = (
    "(lease_owner IS NULL AND lease_expires_at IS NULL) OR "
    "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)"
)

#: Written once when the instruction is recorded. ``IS NOT`` rather than ``<>``
#: so a nullable column compares null-safely instead of yielding an unknown
#: result that would let the change through.
_SCHEDULE_FROZEN_COLUMNS = (
    "purpose",
    "source_account_id",
    "source_wallet_id",
    "destination_account_id",
    "destination_wallet_id",
    "card_id",
    "amount_minor",
    "currency",
    "scheduled_local_date",
    "scheduled_local_time",
    "timezone",
    "due_at_utc",
    "execution_policy",
    "idempotency_key",
    "created_at",
)

_FROZEN_SCHEDULE_TRIGGER = "scheduled_transfer_instruction_is_immutable"
_LINK_NO_UPDATE_TRIGGER = "scheduled_transfer_commitment_no_update"
_LINK_NO_DELETE_TRIGGER = "scheduled_transfer_commitment_no_delete"
_LINK_COVERAGE_TRIGGER = "scheduled_transfer_commitment_within_requirement"

#: Coverage that still counts. A cancelled or reversed schedule moves nothing,
#: so it must not keep a commitment's capacity occupied (ADR-B052).
_LIVE_COVERAGE_SQL = (
    "SELECT COALESCE(SUM(link.amount_minor), 0) "
    "FROM scheduled_transfer_commitments AS link "
    "JOIN scheduled_transfers AS schedule "
    "ON schedule.id = link.scheduled_transfer_id "
    "WHERE link.funding_commitment_id = NEW.funding_commitment_id "
    "AND schedule.status NOT IN ('cancelled', 'reversed')"
)
_REQUIREMENT_SQL = (
    "SELECT commitment.amount_minor - commitment.released_minor "
    "FROM funding_commitments AS commitment "
    "WHERE commitment.id = NEW.funding_commitment_id"
)


class ScheduledTransferHistoryPresentError(RuntimeError):
    """A downgrade would have deleted a bank instruction or a posted entry."""


def _frozen_trigger(
    name: str, table: str, columns: tuple[str, ...], message: str
) -> str:
    condition = " OR ".join(f"NEW.{column} IS NOT OLD.{column}" for column in columns)
    return (
        f"CREATE TRIGGER {name} BEFORE UPDATE ON {table} FOR EACH ROW "
        f"WHEN {condition} BEGIN SELECT RAISE(ABORT, '{message}'); END"
    )


def upgrade() -> None:
    """Create the scheduled movements, their links, and the payment type."""
    op.create_table(
        "scheduled_transfers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("source_account_id", sa.Integer(), nullable=False),
        sa.Column("source_wallet_id", sa.Integer(), nullable=False),
        sa.Column("destination_account_id", sa.Integer(), nullable=False),
        sa.Column("destination_wallet_id", sa.Integer(), nullable=True),
        sa.Column("card_id", sa.Integer(), nullable=True),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("scheduled_local_date", sa.Date(), nullable=False),
        sa.Column("scheduled_local_time", sa.Time(), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("due_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("execution_policy", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("executed_entry_id", sa.Integer(), nullable=True),
        sa.Column("reversal_entry_id", sa.Integer(), nullable=True),
        sa.Column("lease_owner", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reversed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("last_error_message", sa.String(length=200), nullable=True),
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
            "purpose IN ('clearing_funding', 'card_autopay')",
            name=op.f("ck_scheduled_transfers_scheduled_transfer_purpose"),
        ),
        sa.CheckConstraint(
            "status IN ('scheduled', 'processing', 'assumed_executed', 'failed', "
            "'cancelled', 'reversed')",
            name=op.f("ck_scheduled_transfers_scheduled_transfer_status"),
        ),
        sa.CheckConstraint(
            "execution_policy IN ('assume_at_due_time')",
            name=op.f("ck_scheduled_transfers_scheduled_transfer_execution_policy"),
        ),
        sa.CheckConstraint(
            _PURPOSE_SHAPE,
            name=op.f("ck_scheduled_transfers_scheduled_transfer_purpose_shape"),
        ),
        sa.CheckConstraint(
            _STATUS_SHAPE,
            name=op.f("ck_scheduled_transfers_scheduled_transfer_status_shape"),
        ),
        sa.CheckConstraint(
            _LEASE_PAIRING,
            name=op.f("ck_scheduled_transfers_scheduled_transfer_lease_pairing"),
        ),
        sa.CheckConstraint(
            "status <> 'failed' OR last_error_code IS NOT NULL",
            name=op.f("ck_scheduled_transfers_scheduled_transfer_failure_reason"),
        ),
        sa.CheckConstraint(
            "amount_minor > 0",
            name=op.f("ck_scheduled_transfers_scheduled_transfer_amount_positive"),
        ),
        sa.CheckConstraint(
            _CURRENCY_CHECK.format("currency"),
            name=op.f("ck_scheduled_transfers_scheduled_transfer_currency"),
        ),
        sa.CheckConstraint(
            "source_account_id <> destination_account_id",
            name=op.f("ck_scheduled_transfers_scheduled_transfer_distinct_accounts"),
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name=op.f("ck_scheduled_transfers_scheduled_transfer_attempts"),
        ),
        sa.CheckConstraint(
            "version >= 1",
            name=op.f("ck_scheduled_transfers_scheduled_transfer_version_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["card_id"],
            ["cards.id"],
            name=op.f("fk_scheduled_transfers_card_id_cards"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["destination_account_id"],
            ["financial_accounts.id"],
            name=op.f(
                "fk_scheduled_transfers_destination_account_id_financial_accounts"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["destination_wallet_id"],
            ["wallets.id"],
            name=op.f("fk_scheduled_transfers_destination_wallet_id_wallets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["executed_entry_id"],
            ["journal_entries.id"],
            name=op.f("fk_scheduled_transfers_executed_entry_id_journal_entries"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reversal_entry_id"],
            ["journal_entries.id"],
            name=op.f("fk_scheduled_transfers_reversal_entry_id_journal_entries"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_account_id"],
            ["financial_accounts.id"],
            name=op.f("fk_scheduled_transfers_source_account_id_financial_accounts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_wallet_id"],
            ["wallets.id"],
            name=op.f("fk_scheduled_transfers_source_wallet_id_wallets"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scheduled_transfers")),
        sa.UniqueConstraint("idempotency_key", name="scheduled_transfer_idempotency"),
    )
    op.create_index(
        "ix_scheduled_transfers_card_id", "scheduled_transfers", ["card_id"]
    )
    op.create_index(
        "ix_scheduled_transfers_destination_account_id",
        "scheduled_transfers",
        ["destination_account_id"],
    )
    op.create_index(
        "ix_scheduled_transfers_destination_wallet_id",
        "scheduled_transfers",
        ["destination_wallet_id"],
    )
    op.create_index(
        "ix_scheduled_transfers_lease_expires_at",
        "scheduled_transfers",
        ["lease_expires_at"],
    )
    op.create_index(
        "ix_scheduled_transfers_source_account_id",
        "scheduled_transfers",
        ["source_account_id"],
    )
    op.create_index(
        "ix_scheduled_transfers_source_wallet_id",
        "scheduled_transfers",
        ["source_wallet_id"],
    )
    op.create_index(
        "ix_scheduled_transfers_status_due",
        "scheduled_transfers",
        ["status", "due_at_utc"],
    )
    op.create_index(
        "uq_scheduled_transfers_executed_entry",
        "scheduled_transfers",
        ["executed_entry_id"],
        unique=True,
        sqlite_where=sa.text("executed_entry_id IS NOT NULL"),
    )
    op.create_index(
        "uq_scheduled_transfers_reversal_entry",
        "scheduled_transfers",
        ["reversal_entry_id"],
        unique=True,
        sqlite_where=sa.text("reversal_entry_id IS NOT NULL"),
    )

    op.create_table(
        "scheduled_transfer_commitments",
        sa.Column("scheduled_transfer_id", sa.Integer(), nullable=False),
        sa.Column("funding_commitment_id", sa.Integer(), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "amount_minor > 0",
            name=op.f(
                "ck_scheduled_transfer_commitments_"
                "scheduled_transfer_commitment_amount_positive"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["funding_commitment_id"],
            ["funding_commitments.id"],
            name=op.f(
                "fk_scheduled_transfer_commitments_funding_commitment_id_"
                "funding_commitments"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["scheduled_transfer_id"],
            ["scheduled_transfers.id"],
            name=op.f(
                "fk_scheduled_transfer_commitments_scheduled_transfer_id_"
                "scheduled_transfers"
            ),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "scheduled_transfer_id",
            "funding_commitment_id",
            name=op.f("pk_scheduled_transfer_commitments"),
        ),
    )
    op.create_index(
        "ix_scheduled_transfer_commitments_funding_commitment_id",
        "scheduled_transfer_commitments",
        ["funding_commitment_id"],
    )

    op.bulk_insert(
        sa.table(
            "journal_entry_types",
            sa.column("code", sa.String),
            sa.column("description", sa.Text),
        ),
        [
            {"code": code, "description": code.replace("_", " ")}
            for code in _B6_ENTRY_TYPES
        ],
    )

    op.execute(
        _frozen_trigger(
            _FROZEN_SCHEDULE_TRIGGER,
            "scheduled_transfers",
            _SCHEDULE_FROZEN_COLUMNS,
            "a scheduled transfer instruction cannot be changed",
        )
    )
    op.execute(
        f"CREATE TRIGGER {_LINK_NO_UPDATE_TRIGGER} BEFORE UPDATE ON "
        "scheduled_transfer_commitments FOR EACH ROW BEGIN "
        "SELECT RAISE(ABORT, 'a scheduled transfer commitment link is immutable'); END"
    )
    op.execute(
        f"CREATE TRIGGER {_LINK_NO_DELETE_TRIGGER} BEFORE DELETE ON "
        "scheduled_transfer_commitments FOR EACH ROW BEGIN "
        "SELECT RAISE(ABORT, 'a scheduled transfer commitment link is immutable'); END"
    )
    op.execute(
        f"CREATE TRIGGER {_LINK_COVERAGE_TRIGGER} BEFORE INSERT ON "
        "scheduled_transfer_commitments FOR EACH ROW "
        f"WHEN (({_LIVE_COVERAGE_SQL}) + NEW.amount_minor) > COALESCE(({_REQUIREMENT_SQL}), 0) "
        "BEGIN SELECT RAISE(ABORT, 'a scheduled transfer would cover more than this "
        "commitment still requires'); END"
    )


def _reject_when_history_exists() -> None:
    """Refuse before any DDL when a downgrade would destroy real history.

    Only counts are reported. An amount, a card, an account, and a wallet name
    are all user data and never belong in a migration error message.
    """
    connection = op.get_bind()
    counts = {
        table: int(
            connection.exec_driver_sql(f"SELECT COUNT(*) FROM {table}").scalar_one()
        )
        for table in _B6_TABLES
    }
    placeholders = ", ".join("?" for _ in _B6_ENTRY_TYPES)
    counts["journal_entries"] = int(
        connection.exec_driver_sql(
            "SELECT COUNT(*) FROM journal_entries WHERE entry_type IN "
            f"({placeholders})",
            _B6_ENTRY_TYPES,
        ).scalar_one()
    )
    present = {table: count for table, count in counts.items() if count}
    if not present:
        return
    detail = ", ".join(f"{table}={count}" for table, count in sorted(present.items()))
    raise ScheduledTransferHistoryPresentError(
        "revision 0013 refuses to downgrade because scheduled transfer history "
        f"already exists ({detail}). Dropping these tables would delete a "
        "recorded bank instruction, and removing the journal entry type would "
        "orphan a posted entry. Cancel or reverse that history at 0013 first. "
        "Nothing was changed."
    )


def downgrade() -> None:
    """Remove the clearing scheduler, but only from a database that never used it."""
    _reject_when_history_exists()
    for trigger in (
        _LINK_COVERAGE_TRIGGER,
        _LINK_NO_DELETE_TRIGGER,
        _LINK_NO_UPDATE_TRIGGER,
        _FROZEN_SCHEDULE_TRIGGER,
    ):
        op.execute(f"DROP TRIGGER {trigger}")
    op.execute("DELETE FROM journal_entry_types WHERE code IN ('card_payment')")
    op.drop_index(
        "ix_scheduled_transfer_commitments_funding_commitment_id",
        table_name="scheduled_transfer_commitments",
    )
    op.drop_table("scheduled_transfer_commitments")
    op.drop_index(
        "uq_scheduled_transfers_reversal_entry", table_name="scheduled_transfers"
    )
    op.drop_index(
        "uq_scheduled_transfers_executed_entry", table_name="scheduled_transfers"
    )
    op.drop_index("ix_scheduled_transfers_status_due", table_name="scheduled_transfers")
    op.drop_index(
        "ix_scheduled_transfers_source_wallet_id", table_name="scheduled_transfers"
    )
    op.drop_index(
        "ix_scheduled_transfers_source_account_id", table_name="scheduled_transfers"
    )
    op.drop_index(
        "ix_scheduled_transfers_lease_expires_at", table_name="scheduled_transfers"
    )
    op.drop_index(
        "ix_scheduled_transfers_destination_wallet_id", table_name="scheduled_transfers"
    )
    op.drop_index(
        "ix_scheduled_transfers_destination_account_id",
        table_name="scheduled_transfers",
    )
    op.drop_index("ix_scheduled_transfers_card_id", table_name="scheduled_transfers")
    op.drop_table("scheduled_transfers")

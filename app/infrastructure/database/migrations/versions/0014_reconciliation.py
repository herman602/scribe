"""add immutable bank balance snapshots and the income/external movement types

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-09 23:30:00.000000

This revision only creates one new table, two indexes, two triggers, and two
rows in the ``journal_entry_types`` registry. It rebuilds no existing table, so
the ADR-B027 protocol in ``env.py`` applies unchanged but has nothing to copy:
foreign keys are disabled outside the transaction, the whole migration runs
under ``BEGIN IMMEDIATE``, and a complete ``PRAGMA foreign_key_check`` must pass
before it commits.

``income`` and ``external_movement`` are added by inserting registry rows
(ADR-B028 and ADR-B065): interest raises a wallet against an income account, and
a deposit or withdrawal that is neither spending nor an internal move goes
against an external clearing account. Neither is an expense, so neither may be
posted as one.

A balance snapshot is observation, not accounting (ADR-B066). It is therefore
frozen at the row level: BEFORE UPDATE and BEFORE DELETE triggers abort any
statement that would rewrite what the bank app showed, whichever path reaches
the table. ``observed_balance_minor`` is deliberately signed and may be zero: a
real account can be empty or overdrawn, and refusing to record that would make
the one observation the user has unrecordable.

``ix_financial_events_matching_candidates`` supports the manual-candidate lookup
B7 runs before every Gmail intake (ADR-B062). The existing daily-activity and
source-link indexes are left exactly as they are; this one is additional.

No ``one confirmed link per financial event`` constraint is added. The rule that
matters — one confirmed link per parsed transaction — is already revision
0007's partial unique index and stays untouched (ADR-B063).

The downgrade removes an accounting capability, not just a shape. It therefore
refuses, before any DDL, when snapshots or income/external-movement entries
exist, because dropping the table would delete recorded observations and
removing the registry rows would orphan posted entries. A refusal changes
nothing: no schema, no data, and no revision. Only counts are reported; a
balance, an account name, a merchant, and a note are user data and never belong
in a migration error message.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_B7_ENTRY_TYPES = ("income", "external_movement")
_B7_TABLES = ("balance_snapshots",)
_SNAPSHOT_SOURCES = ("manual_bank_app",)

_SNAPSHOT_NO_UPDATE_TRIGGER = "balance_snapshot_no_update"
_SNAPSHOT_NO_DELETE_TRIGGER = "balance_snapshot_no_delete"
_SNAPSHOT_IMMUTABLE_MESSAGE = "a recorded balance snapshot is immutable"

_MATCHING_INDEX = "ix_financial_events_matching_candidates"


class ReconciliationHistoryPresentError(RuntimeError):
    """A downgrade would have deleted an observation or a posted entry."""


def upgrade() -> None:
    """Create the snapshot evidence table and the two new entry types."""
    allowed_sources = ", ".join(f"'{source}'" for source in _SNAPSHOT_SOURCES)
    op.create_table(
        "balance_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("financial_account_id", sa.Integer(), nullable=False),
        sa.Column("observed_balance_minor", sa.BigInteger(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("note", sa.String(length=200), nullable=True),
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
            f"source IN ({allowed_sources})",
            name=op.f("ck_balance_snapshots_balance_snapshot_source"),
        ),
        sa.CheckConstraint(
            "note IS NULL OR length(note) <= 200",
            name=op.f("ck_balance_snapshots_balance_snapshot_note_length"),
        ),
        sa.ForeignKeyConstraint(
            ["financial_account_id"],
            ["financial_accounts.id"],
            name=op.f("fk_balance_snapshots_financial_account_id_financial_accounts"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_balance_snapshots")),
        sa.UniqueConstraint("idempotency_key", name="balance_snapshot_idempotency"),
    )
    op.create_index(
        "ix_balance_snapshots_account_observed_at",
        "balance_snapshots",
        ["financial_account_id", "observed_at"],
    )
    op.create_index(
        _MATCHING_INDEX,
        "financial_events",
        [
            "origin",
            "payment_method",
            "status",
            "currency",
            "amount_minor",
            "occurred_at",
        ],
    )

    op.bulk_insert(
        sa.table(
            "journal_entry_types",
            sa.column("code", sa.String),
            sa.column("description", sa.Text),
        ),
        [
            {"code": code, "description": code.replace("_", " ")}
            for code in _B7_ENTRY_TYPES
        ],
    )

    op.execute(
        f"CREATE TRIGGER {_SNAPSHOT_NO_UPDATE_TRIGGER} BEFORE UPDATE ON "
        "balance_snapshots FOR EACH ROW BEGIN "
        f"SELECT RAISE(ABORT, '{_SNAPSHOT_IMMUTABLE_MESSAGE}'); END"
    )
    op.execute(
        f"CREATE TRIGGER {_SNAPSHOT_NO_DELETE_TRIGGER} BEFORE DELETE ON "
        "balance_snapshots FOR EACH ROW BEGIN "
        f"SELECT RAISE(ABORT, '{_SNAPSHOT_IMMUTABLE_MESSAGE}'); END"
    )


def _reject_when_history_exists() -> None:
    """Refuse before any DDL when a downgrade would destroy real evidence."""
    connection = op.get_bind()
    counts = {
        table: int(
            connection.exec_driver_sql(f"SELECT COUNT(*) FROM {table}").scalar_one()
        )
        for table in _B7_TABLES
    }
    for code in _B7_ENTRY_TYPES:
        counts[f"journal_entries.{code}"] = int(
            connection.exec_driver_sql(
                "SELECT COUNT(*) FROM journal_entries WHERE entry_type = ?",
                (code,),
            ).scalar_one()
        )
    present = {table: count for table, count in counts.items() if count}
    if not present:
        return
    detail = ", ".join(f"{table}={count}" for table, count in sorted(present.items()))
    raise ReconciliationHistoryPresentError(
        "revision 0014 refuses to downgrade because reconciliation history "
        f"already exists ({detail}). Dropping the snapshot table would delete "
        "recorded observations, and removing a journal entry type would orphan "
        "a posted entry. Reverse that history at 0014 first. Nothing was "
        "changed."
    )


def downgrade() -> None:
    """Remove reconciliation, but only from a database that never used it."""
    _reject_when_history_exists()
    for trigger in (_SNAPSHOT_NO_DELETE_TRIGGER, _SNAPSHOT_NO_UPDATE_TRIGGER):
        op.execute(f"DROP TRIGGER {trigger}")
    placeholders = ", ".join(f"'{code}'" for code in _B7_ENTRY_TYPES)
    op.execute(f"DELETE FROM journal_entry_types WHERE code IN ({placeholders})")
    op.drop_index(_MATCHING_INDEX, table_name="financial_events")
    op.drop_index(
        "ix_balance_snapshots_account_observed_at", table_name="balance_snapshots"
    )
    op.drop_table("balance_snapshots")

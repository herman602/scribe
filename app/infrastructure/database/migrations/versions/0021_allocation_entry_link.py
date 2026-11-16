"""name the entry each allocation was posted with

Revision ID: 0021
Revises: 0020
Create Date: 2026-09-21 23:40:00.000000

Revision `0020` let an event carry more than one journal entry so a wrong
allocation could be redone (ADR-B103). It left the allocation rows themselves
unlabelled, and every reader that asked "which wallet funds this event" then
had to guess. The guesses disagreed: the event page took the newest row, the
monthly/dedicated split looked for *any* dedicated row and so kept excluding
an event whose superseded decision had been dedicated, the refund split summed
every generation and could return more than was spent, and a resubmitted
allocation form compared its lines against all rows at once and reported "this
idempotency token was already used for a different allocation" for what was in
fact its own replay.

``journal_entry_id`` ends the guessing: an allocation names the entry that
carried it. The rows in force are the ones carried by the entry in force, and
the superseded ones stay readable as the decision they were, which is what the
event history shows.

It is nullable, and deliberately so. The entry cannot exist when the rows are
written — its postings are built *from* them — so the service inserts the
allocations, stages the entry, and links them inside the one transaction. The
invariant "a posted allocation names its entry" therefore lives in the
allocation service, which ADR-B103 records; a NOT NULL column would only have
forced the same rows to be written in an order the double-entry shape forbids.

The backfill reads each allocation's own entry in the order the application
writes them: allocation rows first, then the entry whose postings were built
from them. The match is therefore the earliest non-reversal entry of the same
event written no earlier than the row. Revision `0022` repeats this derivation
for databases already stamped by the originally published, backwards version
of this backfill. The migration refuses to commit if any allocation is left
without an entry or points at another event's.

The table is rebuilt under the ADR-B027 protocol rather than extended with
``ALTER TABLE ADD COLUMN``: SQLite keeps an added column's ``REFERENCES``
clause as a column-level constraint whose ``ON DELETE`` reflection cannot
recover, and a table-level foreign key is the only shape that reads back
identically (the same reason `0012` gave).
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | Sequence[str] | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "event_allocations"
_COLUMN = "journal_entry_id"
_FK_NAME = f"fk_{_TABLE}_{_COLUMN}_journal_entries"
_EVENT_INDEX = "ix_event_allocations_financial_event_id"
_ENTRY_INDEX = "ix_event_allocations_journal_entry_id"

#: Each allocation is written before the entry whose postings are built from
#: it, so the entry that carried it is the earliest one of the same event
#: recorded no earlier than the row itself. A reversal is skipped: it cancels
#: an entry, it does not carry a decision.
_BACKFILL = f"""
UPDATE {_TABLE} SET {_COLUMN} = (
    SELECT je.id FROM journal_entries AS je
    WHERE je.financial_event_id = {_TABLE}.financial_event_id
      AND je.reversal_of_id IS NULL
      AND je.created_at >= {_TABLE}.created_at
    ORDER BY je.created_at ASC, je.id ASC
    LIMIT 1
)
"""

_UNLINKED = f"SELECT COUNT(*) FROM {_TABLE} WHERE {_COLUMN} IS NULL"
_MISLINKED = f"""
SELECT COUNT(*) FROM {_TABLE} AS ea
JOIN journal_entries AS je ON je.id = ea.{_COLUMN}
WHERE je.financial_event_id IS NOT ea.financial_event_id
"""


#: `0015` guards a dedicated-wallet route with a trigger on the *other* table
#: that reads this one. SQLite validates every trigger body when a table is
#: renamed, so the rebuild has to drop it and put it back unchanged.
_ROUTE_TRIGGER = "dedicated_wallet_allocation_valid_route"
_ROUTE_TRIGGER_SQL = (
    f"CREATE TRIGGER {_ROUTE_TRIGGER} BEFORE INSERT ON dedicated_wallet_allocations "
    "FOR EACH ROW WHEN NOT EXISTS ("
    "SELECT 1 FROM event_allocations AS allocation "
    "JOIN financial_events AS event "
    "ON event.id = allocation.financial_event_id "
    "JOIN wallets AS source ON source.id = allocation.funding_wallet_id "
    "JOIN wallets AS clearing ON clearing.id = NEW.clearing_wallet_id "
    "WHERE allocation.id = NEW.event_allocation_id "
    "AND event.payment_method = 'credit_card' "
    "AND clearing.purpose = 'clearing_hub' "
    "AND clearing.is_active = 1 "
    "AND source.financial_account_id = clearing.financial_account_id "
    "AND source.id <> clearing.id"
    ") BEGIN "
    "SELECT RAISE(ABORT, 'a dedicated-wallet allocation has an invalid route'); END"
)


class AllocationEntryLinkError(RuntimeError):
    """An allocation could not be matched to the entry that posted it."""


def _allocations_table(*, with_link: bool) -> sa.Table:
    """Describe the table exactly, so the rebuild loses no constraint.

    SQLite reflection does not recover every check, so the shape is stated
    here rather than read back, which is what `0007` established.
    """
    metadata = sa.MetaData()
    columns: list[sa.Column[Any]] = [
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
    ]
    constraints: list[sa.schema.SchemaItem] = [
        sa.PrimaryKeyConstraint("id", name=f"pk_{_TABLE}"),
        sa.CheckConstraint(
            "allocation_source IN ('rule', 'manual', 'import')",
            name=f"ck_{_TABLE}_event_allocation_source",
        ),
        sa.CheckConstraint(
            "amount_minor > 0",
            name=f"ck_{_TABLE}_event_allocation_amount_positive",
        ),
        sa.CheckConstraint(
            "length(currency) = 3 AND currency GLOB '[A-Z][A-Z][A-Z]'",
            name=f"ck_{_TABLE}_event_allocation_currency",
        ),
        sa.ForeignKeyConstraint(
            ["budget_category_id"],
            ["budget_categories.id"],
            name=f"fk_{_TABLE}_budget_category_id_budget_categories",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["financial_event_id"],
            ["financial_events.id"],
            name=f"fk_{_TABLE}_financial_event_id_financial_events",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["funding_wallet_id"],
            ["wallets.id"],
            name=f"fk_{_TABLE}_funding_wallet_id_wallets",
            ondelete="RESTRICT",
        ),
    ]
    if with_link:
        columns.append(sa.Column(_COLUMN, sa.Integer(), nullable=True))
        constraints.append(
            sa.ForeignKeyConstraint(
                [_COLUMN],
                ["journal_entries.id"],
                name=_FK_NAME,
                ondelete="RESTRICT",
            )
        )
    return sa.Table(_TABLE, metadata, *columns, *constraints)


def _verify_every_allocation_names_one_entry() -> None:
    """Refuse the whole revision if any row was left guessing.

    Only counts are reported. Which wallet funds which purchase is a fact
    about the user's money and no amount or name belongs in a migration
    error (ADR-B027).
    """
    bind = op.get_bind()
    unlinked = int(bind.exec_driver_sql(_UNLINKED).scalar_one())
    mislinked = int(bind.exec_driver_sql(_MISLINKED).scalar_one())
    if unlinked or mislinked:
        raise AllocationEntryLinkError(
            "revision 0021 refuses to commit because "
            f"{unlinked} allocation(s) could not be matched to the entry that "
            f"posted them and {mislinked} resolved to another event's entry. "
            "Guessing which decision is in force would misstate a wallet "
            "balance. Nothing was changed."
        )


def upgrade() -> None:
    """Rebuild the table with the link, fill it in, and prove it resolved."""
    op.execute(f"DROP TRIGGER {_ROUTE_TRIGGER}")
    with op.batch_alter_table(
        _TABLE, copy_from=_allocations_table(with_link=False)
    ) as batch_op:
        batch_op.add_column(sa.Column(_COLUMN, sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            _FK_NAME, "journal_entries", [_COLUMN], ["id"], ondelete="RESTRICT"
        )
    op.create_index(_EVENT_INDEX, _TABLE, ["financial_event_id"])
    op.create_index(_ENTRY_INDEX, _TABLE, [_COLUMN])
    op.execute(_ROUTE_TRIGGER_SQL)
    op.execute(_BACKFILL)
    _verify_every_allocation_names_one_entry()


def downgrade() -> None:
    """Return to the shape `0020` published.

    Dropping the column loses only a link that the timestamps can rebuild,
    which is why this downgrade does not refuse the way `0012` does. No
    allocation row and no posted entry is touched.
    """
    op.drop_index(_ENTRY_INDEX, table_name=_TABLE)
    op.execute(f"DROP TRIGGER {_ROUTE_TRIGGER}")
    with op.batch_alter_table(
        _TABLE, copy_from=_allocations_table(with_link=True)
    ) as batch_op:
        batch_op.drop_constraint(op.f(_FK_NAME), type_="foreignkey")
        batch_op.drop_column(_COLUMN)
    op.create_index(_EVENT_INDEX, _TABLE, ["financial_event_id"])
    op.execute(_ROUTE_TRIGGER_SQL)

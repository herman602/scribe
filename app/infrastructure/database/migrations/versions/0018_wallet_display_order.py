"""order the wallets of one account the way its owner arranged them

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-17 10:00:00.000000

Until now the accounts sheet had no arrangement of its own. It fell back first
to wallet name, which sorts Chinese envelopes by code point and produces an
order nobody chose, and then to creation order, which is closer but is still a
record of when a wallet was set up rather than where its owner wants to read
it. A column of a sheet is arranged deliberately, so the arrangement is stored.

``display_order`` is presentation only. It carries no money, changes no
posting, and is not part of any identity or idempotency key, so unlike the
history-bearing columns of earlier revisions it may be dropped again: a
downgrade loses an arrangement the user can retype, not a fact about their
money.

The backfill is the order the rows already read in, so the page looks the same
the moment this revision lands. The one deliberate exception is the residual
``unallocated`` envelope, which sorts to the end of its column: it is what is
left over rather than a named envelope, and the arrangement being reproduced
has no row for it at all.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | Sequence[str] | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "wallets"
_COLUMN = "display_order"
_INDEX = "ix_wallets_account_display_order"

#: Non-residual envelopes keep their existing relative order; the residual one
#: goes last. ``purpose = 'unallocated'`` evaluates to 0 or 1 in SQLite, so it
#: is a sort key rather than a filter.
_BACKFILL = f"""
WITH ordered AS (
    SELECT
        id,
        ROW_NUMBER() OVER (
            PARTITION BY financial_account_id
            ORDER BY (purpose = 'unallocated'), id
        ) AS position
    FROM {_TABLE}
)
UPDATE {_TABLE}
SET {_COLUMN} = (SELECT position FROM ordered WHERE ordered.id = {_TABLE}.id)
"""


def upgrade() -> None:
    """Add the arrangement column and seed it with the current reading order."""
    op.add_column(
        _TABLE,
        sa.Column(
            _COLUMN,
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.execute(_BACKFILL)
    op.create_index(_INDEX, _TABLE, ["financial_account_id", _COLUMN])


def downgrade() -> None:
    """Return to the schema revision 0017 was published with."""
    op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_column(_TABLE, _COLUMN)

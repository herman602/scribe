"""order the accounts of the sheet the way their owner arranged them

Revision ID: 0019
Revises: 0018
Create Date: 2026-08-17 12:00:00.000000

Revision `0018` gave each account's wallets an arrangement of their own but
left the accounts themselves sorted by institution code and display name — an
order derived from how a bank happens to be spelled, not one anybody chose. A
sheet's columns are arranged as deliberately as its rows, so the same treatment
applies to both.

``display_order`` is presentation only, exactly as in `0018`: no money, no
identity, no idempotency key, and a downgrade that simply drops it.

The backfill is creation order, which for a database populated by the B9
cutover import is the order the columns were read in when they were imported.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | Sequence[str] | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "financial_accounts"
_COLUMN = "display_order"
_INDEX = "ix_financial_accounts_display_order"

_BACKFILL = f"""
WITH ordered AS (
    SELECT id, ROW_NUMBER() OVER (ORDER BY id) AS position
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
    op.create_index(_INDEX, _TABLE, [_COLUMN])


def downgrade() -> None:
    """Return to the schema revision 0018 was published with."""
    op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_column(_TABLE, _COLUMN)

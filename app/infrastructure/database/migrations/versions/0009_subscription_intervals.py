"""keep the intervals of one subscription from overlapping

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-09 17:30:00.000000

Revision 0008 held one unique index on the name of an *active* subscription.
That was the wrong invariant. A service may hold several intervals over time —
used, stopped, used again — so the same name legitimately appears on more than
one row, and a stopped interval whose end month is still in the future released
its name immediately. Two intervals could therefore cover the same month and
each contribute to it, doubling that month's funding requirement.

The unique index is replaced by BEFORE INSERT and BEFORE UPDATE triggers
enforcing that no two intervals of one service claim a month in common.
Adjacency stays legal, because the upper bound of ``[active_from, active_to)``
is excluded. Both bounds are optional: a missing ``active_from`` means the
interval has always run and a missing ``active_to`` means it still does, and an
unbounded side can never end before the other interval starts.

The triggers are the database's own line of defence; the service checks the
same rule inside the transaction that writes the row. On INSERT ``NEW.id`` is
still NULL, so ``COALESCE(NEW.id, -1)`` keeps the self-exclusion from matching
a real row.

This revision creates no table and rebuilds none, so revision 0008 stays
exactly as it was applied.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OVERLAP_OPERATIONS = ("insert", "update")
_OVERLAP_TRIGGER = (
    "CREATE TRIGGER subscription_no_overlapping_interval_{operation} "
    "BEFORE {keyword} ON subscriptions FOR EACH ROW BEGIN "
    "SELECT RAISE(ABORT, 'subscription intervals must not overlap') WHERE EXISTS ("
    "SELECT 1 FROM subscriptions AS other "
    "WHERE other.name = NEW.name AND other.id <> COALESCE(NEW.id, -1) "
    "AND NOT (other.active_to IS NOT NULL AND NEW.active_from IS NOT NULL "
    "AND other.active_to <= NEW.active_from) "
    "AND NOT (NEW.active_to IS NOT NULL AND other.active_from IS NOT NULL "
    "AND NEW.active_to <= other.active_from)); END"
)


def upgrade() -> None:
    """Trade the active-name index for a real non-overlap invariant."""
    op.drop_index("uq_subscriptions_active_name", table_name="subscriptions")
    op.create_index(
        "ix_subscriptions_name_activity",
        "subscriptions",
        ["name", "active_from", "active_to"],
        unique=False,
    )
    for operation in _OVERLAP_OPERATIONS:
        op.execute(
            _OVERLAP_TRIGGER.format(operation=operation, keyword=operation.upper())
        )


def downgrade() -> None:
    """Restore the 0008 shape, including its unique active-name index."""
    for operation in _OVERLAP_OPERATIONS:
        op.execute(f"DROP TRIGGER subscription_no_overlapping_interval_{operation}")
    op.drop_index("ix_subscriptions_name_activity", table_name="subscriptions")
    op.create_index(
        "uq_subscriptions_active_name",
        "subscriptions",
        ["name"],
        unique=True,
        sqlite_where=sa.text("is_active = 1"),
    )

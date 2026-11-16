"""hold both subscription interval invariants at once

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-09 18:30:00.000000

Revision 0009 traded 0008's unique index on the name of an active subscription
for triggers forbidding two intervals of one service from claiming the same
month. Treating those as alternatives was wrong: they are independent rules.
Two *stopped* intervals can overlap without touching the index, and two
*adjacent* intervals can both be running without overlapping. Dropping the
index therefore left the register able to hold two running intervals of one
service, and left 0009 able to hold a state 0008 could not express, so
downgrading such a database failed on a `UNIQUE constraint` and stranded it.

This revision restores the index beside the triggers so both rules hold, and
0009 keeps exactly the content it was published with. It is written to run from
either shape a database at 0009 can have:

* the published 0009, where the index was dropped;
* a database whose index was restored out of band, where it already exists.

The index is created only when it is absent. When it is already there its
definition is verified rather than recreated, so a database that was repaired
by hand converges on the same schema instead of failing or silently accepting a
different index.

Neither invariant can be installed over rows that already break it. A unique
index cannot be created across duplicates, and a trigger never validates rows
written before it existed, so both are checked before any DDL: the migration
aborts, the transaction rolls back, the revision does not advance, and nothing
is changed. Because ``env.py`` runs every pending migration in one transaction,
a database coming from 0008 rolls all the way back to 0008. Which row of a
conflicting pair is the correct one cannot be guessed, so the check refuses
instead of editing data and reports only row identifiers — a subscription name
is user data and does not belong in a migration error.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTIVE_NAME_INDEX = "uq_subscriptions_active_name"
#: Exactly what 0008 created and what this revision restores. Compared with
#: whitespace collapsed, so a hand-repaired database is accepted only when its
#: index really is this one.
_ACTIVE_NAME_INDEX_SQL = (
    "CREATE UNIQUE INDEX uq_subscriptions_active_name "
    "ON subscriptions (name) WHERE is_active = 1"
)
_OVERLAP_PREDICATE = (
    "NOT (earlier.active_to IS NOT NULL AND later.active_from IS NOT NULL "
    "AND earlier.active_to <= later.active_from) "
    "AND NOT (later.active_to IS NOT NULL AND earlier.active_from IS NOT NULL "
    "AND later.active_to <= earlier.active_from)"
)
_OVERLAPPING_INTERVALS = (
    "SELECT earlier.id, later.id FROM subscriptions AS earlier "
    "JOIN subscriptions AS later ON earlier.name = later.name "
    "AND earlier.id < later.id WHERE " + _OVERLAP_PREDICATE + " ORDER BY earlier.id"
)
_DUPLICATE_RUNNING_NAMES = (
    "SELECT earlier.id, later.id FROM subscriptions AS earlier "
    "JOIN subscriptions AS later ON earlier.name = later.name "
    "AND earlier.id < later.id "
    "WHERE earlier.is_active = 1 AND later.is_active = 1 ORDER BY earlier.id"
)
_MAX_REPORTED_PAIRS = 10


class SubscriptionInvariantViolationError(RuntimeError):
    """Existing rows already break an invariant this revision installs."""


class UnexpectedActiveNameIndexError(RuntimeError):
    """An index of the expected name exists but is not the expected index."""


def _normalized(statement: str) -> str:
    return " ".join(statement.split())


def _reject(rule: str, pairs: list[tuple[int, int]], remedy: str) -> None:
    """Refuse to install an invariant the current rows already break.

    Only row identifiers are reported. A subscription name is user data and a
    price is financial data; neither belongs in a migration error message.
    """
    shown = ", ".join(f"{left}/{right}" for left, right in pairs[:_MAX_REPORTED_PAIRS])
    remainder = len(pairs) - min(len(pairs), _MAX_REPORTED_PAIRS)
    suffix = "" if remainder == 0 else f", and {remainder} more"
    raise SubscriptionInvariantViolationError(
        f"{len(pairs)} pair(s) of subscription rows already {rule}, so revision "
        f"0010 refuses to install its invariants. Conflicting subscription ids: "
        f"{shown}{suffix}. {remedy} Nothing was changed."
    )


def _reject_existing_violations() -> None:
    """Check both invariants before any DDL, so a failure changes nothing."""
    connection = op.get_bind()
    overlapping = [
        (int(row[0]), int(row[1]))
        for row in connection.exec_driver_sql(_OVERLAPPING_INTERVALS).all()
    ]
    if overlapping:
        _reject(
            "cover the same month twice",
            overlapping,
            "End or re-date one interval of each pair first.",
        )
    duplicated = [
        (int(row[0]), int(row[1]))
        for row in connection.exec_driver_sql(_DUPLICATE_RUNNING_NAMES).all()
    ]
    if duplicated:
        _reject(
            "are running under one name at the same time",
            duplicated,
            "Stop all but one interval of each pair from a month first.",
        )


def _existing_active_name_index() -> str | None:
    """Return the stored definition of the active-name index, if it exists."""
    row = (
        op.get_bind()
        .exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            (_ACTIVE_NAME_INDEX,),
        )
        .fetchone()
    )
    return None if row is None else str(row[0])


def upgrade() -> None:
    """Restore the active-name index beside 0009's non-overlap triggers."""
    _reject_existing_violations()
    existing = _existing_active_name_index()
    if existing is None:
        op.create_index(
            _ACTIVE_NAME_INDEX,
            "subscriptions",
            ["name"],
            unique=True,
            sqlite_where=sa.text("is_active = 1"),
        )
        return
    if _normalized(existing) != _normalized(_ACTIVE_NAME_INDEX_SQL):
        raise UnexpectedActiveNameIndexError(
            f"an index named {_ACTIVE_NAME_INDEX} already exists but is not the "
            "one revision 0010 installs. Drop it and run the upgrade again so "
            "the schema is the one the migrations describe."
        )


def downgrade() -> None:
    """Return to the schema revision 0009 was published with."""
    if _existing_active_name_index() is not None:
        op.drop_index(_ACTIVE_NAME_INDEX, table_name="subscriptions")

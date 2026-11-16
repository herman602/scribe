"""let an event carry the entry that replaced the one it had

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-21 18:00:00.000000

A financial event could hold at most one journal entry, enforced by a partial
unique index. That made an allocation permanent: the entry carrying "which
wallet funds this, which category it belongs to" could be reversed, but no
replacement could ever be staged, so a Gmail-origin expense allocated to the
wrong wallet had no correction at all (ADR-B103).

The uniqueness is dropped and the column keeps a plain index, because an event
may now carry the superseded entry, the reversal that cancelled it, and the
one in force. What replaces the constraint is not a weaker index but a
different invariant: at most one entry *still in force*, which depends on
whether some other row reverses this one and therefore cannot be written as a
partial index. `JournalRepository.get_by_financial_event` resolves that entry,
and the posting service refuses to stage a second one while it exists.

No row is read, written, or moved. Nothing about any posted entry changes.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | Sequence[str] | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "journal_entries"
_COLUMN = "financial_event_id"
_UNIQUE_INDEX = "uq_journal_entries_financial_event"
_PLAIN_INDEX = "ix_journal_entries_financial_event_id"


def upgrade() -> None:
    """Replace the unique index on the event column with a plain one."""
    op.drop_index(_UNIQUE_INDEX, table_name=_TABLE)
    op.create_index(_PLAIN_INDEX, _TABLE, [_COLUMN])


def downgrade() -> None:
    """Restore uniqueness, which fails loudly if an event now has two entries.

    That failure is correct: the old shape cannot represent a redone
    allocation, and silently discarding one of the entries would destroy
    posted evidence.
    """
    op.drop_index(_PLAIN_INDEX, table_name=_TABLE)
    op.create_index(
        _UNIQUE_INDEX,
        _TABLE,
        [_COLUMN],
        unique=True,
        sqlite_where=sa.text(f"{_COLUMN} IS NOT NULL"),
    )

"""resolve each allocation's entry in the direction it was actually written

Revision ID: 0022
Revises: 0021
Create Date: 2026-09-22 10:30:00.000000

`0021` backfilled `event_allocations.journal_entry_id` by looking for the
newest entry of the same event recorded *no later than* the allocation row.
That is backwards. An entry's postings are built from the allocation rows, so
the rows are written first and the entry a moment after: the entry that
carried an allocation is the *earliest* one written no earlier than it.

The mistake was invisible on a database whose rows and entries share a
timestamp to the second, which is the common case and the only case `0021`
was tested against. It is wrong as soon as a second boundary falls between
the two writes:

* first generation's entry at 10:00:00
* second generation's allocation at 12:00:00
* second generation's entry at 12:00:01

`0021` resolved the second generation to the *first* generation's entry, and
its own verification could not catch it, because that entry belongs to the
same financial event. The effect is a superseded split read as the one in
force: the wrong funding wallet on the page, an expense wrongly kept out of
monthly spending or wrongly put back into it, a refund returning money to a
share it never came from, and a reservation released against the wrong
commitment (ADR-B103).

The `0021` source is corrected so a database still at `0020` can reach this
revision even when its first generation crosses a second boundary. That is a
deliberate exception to ADR-B041: leaving the faulty source untouched strands
such a database at `0020`, before any later repair can run. This revision still
re-derives every link for databases already stamped by the originally
published `0021`, so both histories end up identical.

A tie this rule cannot settle is refused rather than guessed. If a generation
was posted and redone inside a single second, both generations resolve to the
same entry and another entry of the same event is left carrying no allocation
at all. That is checked, and the revision changes nothing when it happens,
because which split is in force decides a wallet balance and a coin flip is
not an answer.

No schema changes. No entry, posting, commitment, or amount is read or
written; only the link between an allocation and the entry it was posted with.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0022"
down_revision: str | Sequence[str] | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "event_allocations"
_COLUMN = "journal_entry_id"

#: The entry that carried an allocation is the earliest non-reversal entry of
#: the same event written no earlier than the row itself, because the rows are
#: written first and the entry built from them immediately after. A reversal is
#: skipped: it cancels an entry, it carries no decision.
_REBACKFILL = f"""
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
#: An entry of an event that has allocations, carrying none of them. Every
#: non-reversal entry of such an event was produced by an allocation, so one
#: left empty means two generations resolved to the same entry and this rule
#: cannot tell them apart.
_ORPHANED = f"""
SELECT COUNT(*) FROM journal_entries AS je
WHERE je.reversal_of_id IS NULL
  AND je.financial_event_id IN (SELECT financial_event_id FROM {_TABLE})
  AND NOT EXISTS (
      SELECT 1 FROM {_TABLE} AS ea WHERE ea.{_COLUMN} = je.id
  )
"""


class AllocationEntryLinkAmbiguousError(RuntimeError):
    """Two generations of one split could not be told apart."""


def _verify_every_generation_found_its_own_entry() -> None:
    """Refuse the whole revision unless every link resolved unambiguously.

    Only counts are reported. Which wallet funds which purchase is a fact
    about the user's money and no amount or name belongs in a migration
    error (ADR-B027).
    """
    bind = op.get_bind()
    unlinked = int(bind.exec_driver_sql(_UNLINKED).scalar_one())
    mislinked = int(bind.exec_driver_sql(_MISLINKED).scalar_one())
    orphaned = int(bind.exec_driver_sql(_ORPHANED).scalar_one())
    if unlinked or mislinked or orphaned:
        raise AllocationEntryLinkAmbiguousError(
            "revision 0022 refuses to commit because "
            f"{unlinked} allocation(s) could not be matched to an entry, "
            f"{mislinked} resolved to another event's entry, and {orphaned} "
            "posted entry(ies) of an event that has allocations were left "
            "carrying none — two generations of one split that a timestamp "
            "cannot separate. Guessing which one is in force would misstate a "
            "wallet balance. Nothing was changed."
        )


def upgrade() -> None:
    """Re-derive every link, and prove each generation found its own entry."""
    op.execute(_REBACKFILL)
    _verify_every_generation_found_its_own_entry()


def downgrade() -> None:
    """Keep the corrected links, which is the only honest thing to do.

    Nothing about the schema differs between `0021` and this revision, so
    there is no shape to restore. The one thing a downgrade could undo is the
    repair itself, and re-deriving the faulty links would deliberately write
    a superseded split back as the one in force.
    """

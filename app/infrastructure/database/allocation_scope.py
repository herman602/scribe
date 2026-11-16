"""SQL for "which allocation is in force", shared by every read model.

An event whose allocation was redone carries the superseded entry, the
reversal that cancelled it, and the one in force (ADR-B103). Two read models
answer questions that depend on which generation applies — the daily list and
the budget month both exclude dedicated-wallet spending from the calendar
month's allowance (ADR-B078) — and they must not each express it their own
way: one of them would eventually be corrected and the other left behind,
which is exactly how a dedicated expense stayed out of monthly spending
forever after being redone as an ordinary one.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from app.infrastructure.database.budget_models import (
    DedicatedWalletAllocationModel,
    EventAllocationModel,
    JournalEntryModel,
)


def entry_in_force_id(event_id: ColumnElement[int]) -> ColumnElement[int | None]:
    """Return the event's newest entry that nothing else reverses."""
    canceller = aliased(JournalEntryModel)
    live = aliased(JournalEntryModel)
    return (
        select(live.id)
        .where(
            live.financial_event_id == event_id,
            live.id.not_in(
                select(canceller.reversal_of_id).where(
                    canceller.reversal_of_id.is_not(None)
                )
            ),
        )
        .order_by(live.id.desc())
        .limit(1)
        # `event_id` reaches in from the enclosing query, and without this
        # SQLAlchemy adds those tables to this subquery's own FROM instead:
        # a cartesian product whose newest row belongs to another event.
        .correlate_except(live)
        .scalar_subquery()
    )


def not_dedicated(event_id: ColumnElement[int]) -> ColumnElement[bool]:
    """Return a correlated predicate for calendar-month/daily spending.

    Only the allocation in force decides this. A redone allocation leaves the
    superseded row in place, so asking whether the *event* has any dedicated
    row at all kept an expense out of monthly spending for good once it had
    been dedicated even once.
    """
    return (
        ~select(DedicatedWalletAllocationModel.id)
        .join(
            EventAllocationModel,
            EventAllocationModel.id
            == DedicatedWalletAllocationModel.event_allocation_id,
        )
        .where(
            EventAllocationModel.financial_event_id == event_id,
            EventAllocationModel.journal_entry_id == entry_in_force_id(event_id),
        )
        .exists()
    )

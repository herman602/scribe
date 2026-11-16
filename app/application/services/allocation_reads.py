"""Answer "which split funds this event now", once, for every caller.

A redone allocation leaves the decision it replaced in place: the rows are
referenced by commitments that carry their own evidence, and the ledger lines
they produced are cancelled rather than deleted (ADR-B103). So an event can
hold several generations of allocation rows, and every service that means the
current one has to say so the same way, or two of them will disagree about
which wallet is paying.

The current one is the split carried by the entry in force, which is what
`JournalRepository.get_by_financial_event` returns.
"""

from __future__ import annotations

from app.domain.budget_models import EventAllocation
from app.domain.ports.repositories import UnitOfWork


def allocations_in_force(
    unit: UnitOfWork, event_id: int
) -> tuple[EventAllocation, ...]:
    """Return the split that is posted right now, or nothing if none is."""
    entry = unit.journal.get_by_financial_event(event_id)
    if entry is None:
        return ()
    return unit.allocations.list_for_entry(entry.id)

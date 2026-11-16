"""Give back what one event's funding commitments still reserve.

Two commands need this and must do it identically: cancelling a manual
expense, and redoing an allocation. A reservation exists because money was
expected to leave later, so once the expense it was taken for is gone, what
remains outstanding is released and each commitment's status is recomputed
through the one rule every service shares (ADR-B059).
"""

from __future__ import annotations

from app.domain.budget_enums import CommitmentStatus
from app.domain.ports.repositories import UnitOfWork


def release_event_reservations(unit: UnitOfWork, event_id: int) -> None:
    """Release every outstanding share this event still holds."""
    for commitment in unit.commitments.list_for_event(event_id):
        if (
            commitment.outstanding_minor > 0
            and commitment.status is not CommitmentStatus.CANCELLED
        ):
            unit.commitments.release(commitment.id, commitment.outstanding_minor)
        unit.commitments.refresh_status(commitment.id)

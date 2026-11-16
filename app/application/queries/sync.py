"""Read-only synchronization progress query service."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from app.domain.models import SyncRun
from app.domain.ports.repositories import UnitOfWork

type UnitOfWorkFactory = Callable[[], UnitOfWork]


@dataclass(frozen=True)
class AccountSyncCursor:
    """Safe mailbox cursor fields for the local progress page."""

    profile_email: str
    committed_history_id: str | None
    reconciliation_required: bool
    last_successful_sync_at: datetime | None


@dataclass(frozen=True)
class SyncDashboardSnapshot:
    """Recent durable runs and account cursor state."""

    runs: tuple[SyncRun, ...]
    cursors: tuple[AccountSyncCursor, ...]


class SyncDashboardQueries:
    """Read sync progress through domain repository ports."""

    def __init__(self, unit_of_work_factory: UnitOfWorkFactory) -> None:
        self._unit_of_work_factory = unit_of_work_factory

    def snapshot(self, *, limit: int = 20) -> SyncDashboardSnapshot:
        """Return recent runs and persisted Gmail cursor metadata."""
        with self._unit_of_work_factory() as unit:
            runs = unit.sync_runs.list_recent(limit=limit)
            cursors = tuple(
                AccountSyncCursor(
                    profile_email=account.profile_email,
                    committed_history_id=state.committed_history_id,
                    reconciliation_required=state.reconciliation_required,
                    last_successful_sync_at=state.last_successful_sync_at,
                )
                for account in unit.accounts.list_all()
                if (state := unit.sync_states.get(account.id)) is not None
            )
        return SyncDashboardSnapshot(runs=runs, cursors=cursors)

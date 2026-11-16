"""Durable SQLite lease and cooperative cancellation guard."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.domain.errors import SyncAlreadyRunningError, SyncCancelledError
from app.domain.ports.repositories import UnitOfWork

type UnitOfWorkFactory = Callable[[], UnitOfWork]
type Clock = Callable[[], datetime]


class SyncRunLease:
    """Own and refresh one sync run lease across safe work boundaries."""

    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        run_id: str,
        *,
        lease_seconds: int = 300,
        clock: Clock = lambda: datetime.now(UTC),
        worker_id: str | None = None,
    ) -> None:
        if lease_seconds < 1:
            raise ValueError("sync lease duration must be positive")
        self._unit_of_work_factory = unit_of_work_factory
        self._run_id = run_id
        self._lease_duration = timedelta(seconds=lease_seconds)
        self._clock = clock
        self._worker_id = worker_id or f"scribe-{uuid4()}"
        self._acquired = False

    def acquire(self) -> None:
        """Atomically acquire the run or reject an active owner."""
        now = self._clock()
        with self._unit_of_work_factory() as unit:
            acquired = unit.sync_runs.acquire_lease(
                self._run_id,
                worker_id=self._worker_id,
                now=now,
                expires_at=now + self._lease_duration,
            )
            unit.commit()
        if not acquired:
            raise SyncAlreadyRunningError(
                "Another worker owns this sync. Wait for it to finish or for "
                "its lease to expire."
            )
        self._acquired = True

    def checkpoint(self) -> None:
        """Observe cancellation and renew ownership before starting more work."""
        now = self._clock()
        with self._unit_of_work_factory() as unit:
            if unit.sync_runs.cancellation_requested(self._run_id):
                raise SyncCancelledError(
                    "Sync cancellation was requested; saved progress was preserved"
                )
            renewed = unit.sync_runs.renew_lease(
                self._run_id,
                worker_id=self._worker_id,
                now=now,
                expires_at=now + self._lease_duration,
            )
            unit.commit()
        if not renewed:
            raise SyncAlreadyRunningError(
                "Sync lease was lost before the next safe work boundary"
            )

    def release(self) -> None:
        """Release ownership if this guard still owns the run."""
        if not self._acquired:
            return
        with self._unit_of_work_factory() as unit:
            unit.sync_runs.release_lease(
                self._run_id,
                worker_id=self._worker_id,
            )
            unit.commit()
        self._acquired = False

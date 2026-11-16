"""Presentation-neutral controls for one local background sync worker."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.application.services.gmail_full_sync import FullSyncRequest
from app.domain.enums import RunStatus, SyncMode
from app.domain.errors import SyncAlreadyRunningError
from app.domain.ports.repositories import UnitOfWork

type UnitOfWorkFactory = Callable[[], UnitOfWork]


@dataclass(frozen=True)
class LocalWorkerSnapshot:
    """Non-sensitive in-memory state before and around durable run creation."""

    active: bool
    mode: SyncMode | None
    status: RunStatus | None
    run_id: str | None
    message: str | None
    started_at: datetime | None
    finished_at: datetime | None


class SyncWorker(Protocol):
    """Port implemented by the local daemon-thread worker."""

    def start_incremental(self) -> None: ...

    def start_full(self, request: FullSyncRequest) -> None: ...

    def snapshot(self) -> LocalWorkerSnapshot: ...


class ControlBackgroundSync:
    """Validate and dispatch local sync mutations."""

    def __init__(
        self,
        worker: SyncWorker | None,
        unit_of_work_factory: UnitOfWorkFactory,
    ) -> None:
        #: A caller that only cancels needs no worker: cancellation is
        #: persisted for whichever process owns the run, so the CLI can stop a
        #: sync the Web process started (ADR-B096).
        self._worker = worker
        self._unit_of_work_factory = unit_of_work_factory

    def _require_worker(self) -> SyncWorker:
        if self._worker is None:
            raise ValueError("this caller cannot start or inspect a local worker")
        return self._worker

    def start_incremental(self) -> None:
        """Start incremental sync without blocking the caller."""
        if self._require_worker().snapshot().active:
            raise SyncAlreadyRunningError("A local background sync is already active")
        self._require_worker().start_incremental()

    def start_full(self, request: FullSyncRequest) -> None:
        """Start a full sync without blocking the caller."""
        if self._require_worker().snapshot().active:
            raise SyncAlreadyRunningError("A local background sync is already active")
        self._require_worker().start_full(request)

    def request_cancel(self, run_id: str) -> None:
        """Persist cancellation for whichever process currently owns the run."""
        with self._unit_of_work_factory() as unit:
            accepted = unit.sync_runs.request_cancel(run_id)
            unit.commit()
        if not accepted:
            raise ValueError("This sync is no longer running")

    def worker_snapshot(self) -> LocalWorkerSnapshot:
        """Return transient local worker state."""
        return self._require_worker().snapshot()

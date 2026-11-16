"""Single-task daemon-thread worker for local Gmail synchronization."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from app.application.services.gmail_full_sync import FullSyncRequest, FullSyncResult
from app.application.services.gmail_incremental_sync import (
    IncrementalSyncResult,
)
from app.application.services.sync_control import LocalWorkerSnapshot
from app.domain.enums import RunStatus, SyncMode
from app.domain.errors import ScribeError, SyncAlreadyRunningError
from app.infrastructure.observability.logging import LOGGER_NAME

type SyncResult = FullSyncResult | IncrementalSyncResult

_LOGGER = logging.getLogger(LOGGER_NAME)


class _SyncOperation(Protocol):
    def __call__(self) -> SyncResult: ...


class _FullSyncService(Protocol):
    def execute(self, request: FullSyncRequest) -> FullSyncResult: ...


class _IncrementalSyncService(Protocol):
    def execute(self) -> IncrementalSyncResult: ...


type FullSyncFactory = Callable[[], _FullSyncService]
type IncrementalSyncFactory = Callable[[], _IncrementalSyncService]


class LocalSyncWorker:
    """Run at most one sync in a daemon thread owned by this web process."""

    def __init__(
        self,
        incremental_factory: IncrementalSyncFactory,
        full_factory: FullSyncFactory,
    ) -> None:
        self._incremental_factory = incremental_factory
        self._full_factory = full_factory
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._snapshot = LocalWorkerSnapshot(
            active=False,
            mode=None,
            status=None,
            run_id=None,
            message=None,
            started_at=None,
            finished_at=None,
        )

    def start_incremental(self) -> None:
        """Schedule incremental sync and return immediately."""
        self._start(
            SyncMode.INCREMENTAL,
            lambda: self._incremental_factory().execute(),
        )

    def start_full(self, request: FullSyncRequest) -> None:
        """Schedule a scoped full sync and return immediately."""
        self._start(
            request.mode,
            lambda: self._full_factory().execute(request),
        )

    def snapshot(self) -> LocalWorkerSnapshot:
        """Return an immutable view of transient worker state."""
        with self._lock:
            return self._snapshot

    def _start(self, mode: SyncMode, operation: _SyncOperation) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise SyncAlreadyRunningError(
                    "A local background sync is already active"
                )
            self._snapshot = LocalWorkerSnapshot(
                active=True,
                mode=mode,
                status=RunStatus.QUEUED,
                run_id=None,
                message=None,
                started_at=None,
                finished_at=None,
            )
            self._thread = threading.Thread(
                target=self._run,
                args=(mode, operation),
                name="scribe-sync-worker",
                daemon=True,
            )
            self._thread.start()

    def _run(self, mode: SyncMode, operation: _SyncOperation) -> None:
        started = datetime.now(UTC)
        self._replace_snapshot(
            active=True,
            mode=mode,
            status=RunStatus.RUNNING,
            run_id=None,
            message=None,
            started_at=started,
            finished_at=None,
        )
        try:
            result = operation()
        except ScribeError as error:
            _LOGGER.error(
                "Local sync worker stopped error_code=%s",
                type(error).__name__,
            )
            self._replace_snapshot(
                active=False,
                mode=mode,
                status=RunStatus.FAILED,
                run_id=None,
                message=str(error),
                started_at=started,
                finished_at=datetime.now(UTC),
            )
        except Exception:
            _LOGGER.exception("Unexpected local sync worker failure")
            self._replace_snapshot(
                active=False,
                mode=mode,
                status=RunStatus.FAILED,
                run_id=None,
                message="Unexpected local worker failure; inspect the local log",
                started_at=started,
                finished_at=datetime.now(UTC),
            )
        else:
            self._replace_snapshot(
                active=False,
                mode=result.mode,
                status=result.status,
                run_id=result.run_id,
                message=None,
                started_at=started,
                finished_at=datetime.now(UTC),
            )

    def _replace_snapshot(
        self,
        *,
        active: bool,
        mode: SyncMode | None,
        status: RunStatus | None,
        run_id: str | None,
        message: str | None,
        started_at: datetime | None,
        finished_at: datetime | None,
    ) -> None:
        with self._lock:
            self._snapshot = LocalWorkerSnapshot(
                active=active,
                mode=mode,
                status=status,
                run_id=run_id,
                message=message,
                started_at=started_at,
                finished_at=finished_at,
            )

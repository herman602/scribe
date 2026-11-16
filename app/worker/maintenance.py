"""Daemon-thread coordinator that executes due local money maintenance.

This is deliberately not `LocalSyncWorker` (ADR-B056). Gmail synchronization
owns a mailbox lease, a run history, and a cancellation protocol that have
nothing to do with money; mixing them would make one failure stop the other.
The coordinator here claims and executes due scheduled transfers, and nothing
it does can stop Gmail sync or the offline Dashboard.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol

from app.application.services.scheduled_transfers import MaintenanceRunResult
from app.infrastructure.observability.logging import LOGGER_NAME

_LOGGER = logging.getLogger(LOGGER_NAME)


class _MaintenanceService(Protocol):
    """The one operation this coordinator knows how to run."""

    def run_due(self, *, limit: int | None = None) -> MaintenanceRunResult: ...


type MaintenanceFactory = Callable[[], _MaintenanceService]
type Clock = Callable[[], datetime]


@dataclass(frozen=True)
class MaintenanceSnapshot:
    """An immutable view of transient coordinator state, safe to display.

    Only counts, timestamps, and stable codes appear here. An amount, an
    account, a wallet, or a card never reaches a diagnostics page.
    """

    enabled: bool
    running: bool
    poll_seconds: int
    batch_size: int
    passes: int
    last_started_at: datetime | None = None
    last_finished_at: datetime | None = None
    last_claimed: int = 0
    last_executed: int = 0
    last_retried: int = 0
    last_failed: int = 0
    consecutive_errors: int = 0
    last_error_code: str | None = None


class LocalMaintenanceCoordinator:
    """Run bounded maintenance passes in a daemon thread owned by this process.

    Startup runs one catch-up pass immediately, so a Mac that was asleep at the
    due time executes as soon as Scribe is awake again. `start` and `stop` are
    both idempotent, and `stop` waits for the thread so a shutdown never leaves
    a half-finished pass writing to the database.
    """

    def __init__(
        self,
        maintenance_factory: MaintenanceFactory,
        *,
        enabled: bool = True,
        poll_seconds: int = 60,
        batch_size: int = 20,
        clock: Clock = lambda: datetime.now(UTC),
    ) -> None:
        self._maintenance_factory = maintenance_factory
        self._enabled = enabled
        self._poll_seconds = poll_seconds
        self._batch_size = batch_size
        self._clock = clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._snapshot = MaintenanceSnapshot(
            enabled=enabled,
            running=False,
            poll_seconds=poll_seconds,
            batch_size=batch_size,
            passes=0,
        )

    def start(self) -> None:
        """Begin polling, or do nothing when disabled or already running."""
        if not self._enabled:
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._snapshot = _with(self._snapshot, running=True)
            self._thread = threading.Thread(
                target=self._loop,
                name="scribe-maintenance-coordinator",
                daemon=True,
            )
            self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        """Ask the thread to finish its pass and wait for it to end."""
        with self._lock:
            thread = self._thread
            self._thread = None
        self._stop.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        with self._lock:
            self._snapshot = _with(self._snapshot, running=False)

    def snapshot(self) -> MaintenanceSnapshot:
        """Return the current, non-sensitive view of coordinator state."""
        with self._lock:
            return self._snapshot

    def run_once(self) -> MaintenanceRunResult:
        """Run one pass on the caller's thread, for tests and for the CLI."""
        return self._run_pass()

    def _loop(self) -> None:
        """Catch up immediately, then poll until stop is requested."""
        while True:
            self._run_pass()
            if self._stop.wait(self._poll_seconds):
                return

    def _run_pass(self) -> MaintenanceRunResult:
        """Run one bounded pass, never letting an error end the loop.

        A maintenance failure is recorded and the coordinator keeps its
        schedule: the alternative is a worker that dies silently at the first
        locked database and stops executing money for the rest of the session.
        """
        started = self._clock()
        with self._lock:
            self._snapshot = _with(self._snapshot, last_started_at=started)
        try:
            result = self._maintenance_factory().run_due(limit=self._batch_size)
        except Exception as error:
            _LOGGER.exception(
                "Local maintenance pass failed error_code=%s", type(error).__name__
            )
            with self._lock:
                self._snapshot = _with(
                    self._snapshot,
                    passes=self._snapshot.passes + 1,
                    last_finished_at=self._clock(),
                    consecutive_errors=self._snapshot.consecutive_errors + 1,
                    last_error_code=type(error).__name__,
                )
            return MaintenanceRunResult(claimed=0, executed=0, retried=0, failed=0)
        with self._lock:
            self._snapshot = _with(
                self._snapshot,
                passes=self._snapshot.passes + 1,
                last_finished_at=self._clock(),
                last_claimed=result.claimed,
                last_executed=result.executed,
                last_retried=result.retried,
                last_failed=result.failed,
                consecutive_errors=0,
                last_error_code=None,
            )
        if result.claimed:
            _LOGGER.info(
                "Local maintenance pass claimed=%s executed=%s retried=%s failed=%s",
                result.claimed,
                result.executed,
                result.retried,
                result.failed,
            )
        return result


def _with(snapshot: MaintenanceSnapshot, **values: Any) -> MaintenanceSnapshot:
    """Return a copy of one snapshot with the named fields replaced."""
    return replace(snapshot, **values)

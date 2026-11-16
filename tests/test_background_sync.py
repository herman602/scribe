"""Local worker and sync progress application service tests."""

from __future__ import annotations

import threading
from datetime import UTC, datetime

import pytest
from app.application.queries.sync import SyncDashboardQueries
from app.application.services.gmail_incremental_sync import IncrementalSyncResult
from app.application.services.sync_control import ControlBackgroundSync
from app.domain.enums import RunStatus, SyncMode
from app.domain.errors import SyncAlreadyRunningError
from app.infrastructure.database.engine import Database
from app.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork
from app.worker.local import LocalSyncWorker


class _BlockingIncremental:
    def __init__(self, entered: threading.Event, release: threading.Event) -> None:
        self._entered = entered
        self._release = release

    def execute(self) -> IncrementalSyncResult:
        self._entered.set()
        assert self._release.wait(timeout=2)
        return IncrementalSyncResult(
            run_id="run-fixture",
            status=RunStatus.SUCCEEDED,
            mode=SyncMode.INCREMENTAL,
            start_history_id="100",
            end_history_id="101",
            pages_processed=1,
            discovered_count=0,
            new_count=0,
            updated_count=0,
            deleted_count=0,
            failed_count=0,
            parser_failed_count=0,
        )


def test_local_worker_returns_immediately_and_allows_only_one_task() -> None:
    entered = threading.Event()
    release = threading.Event()
    worker = LocalSyncWorker(
        incremental_factory=lambda: _BlockingIncremental(entered, release),
        full_factory=lambda: pytest.fail("full factory must stay lazy"),
    )

    worker.start_incremental()
    assert entered.wait(timeout=1)
    assert worker.snapshot().active
    with pytest.raises(SyncAlreadyRunningError):
        worker.start_incremental()

    release.set()
    for _ in range(100):
        snapshot = worker.snapshot()
        if not snapshot.active:
            break
        threading.Event().wait(0.01)

    assert snapshot.status is RunStatus.SUCCEEDED
    assert snapshot.run_id == "run-fixture"


def test_sync_query_and_control_use_durable_repository(
    migrated_database: Database,
) -> None:
    def unit_factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(migrated_database.session_factory)

    with unit_factory() as unit:
        account = unit.accounts.get_or_create("progress@example.com")
        state = unit.sync_states.get_or_create(account.id)
        run = unit.sync_runs.create(
            account_id=account.id,
            mode=SyncMode.FULL,
            query="newer_than:1y",
            start_history_id="500",
        )
        unit.commit()

    worker = LocalSyncWorker(
        incremental_factory=lambda: pytest.fail("not started"),
        full_factory=lambda: pytest.fail("not started"),
    )
    control = ControlBackgroundSync(worker, unit_factory)
    control.request_cancel(run.id)
    snapshot = SyncDashboardQueries(unit_factory).snapshot()

    assert snapshot.cursors[0].profile_email == "progress@example.com"
    assert snapshot.cursors[0].committed_history_id == state.committed_history_id
    assert snapshot.runs[0].cancel_requested
    assert snapshot.runs[0].start_history_id == "500"
    assert snapshot.runs[0].started_at is not None
    assert snapshot.runs[0].started_at <= datetime.now(UTC)

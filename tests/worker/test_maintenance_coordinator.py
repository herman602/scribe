"""The local maintenance coordinator's lifecycle, isolation, and diagnostics."""

from __future__ import annotations

import threading
from datetime import UTC, datetime

import pytest
from app.application.services.scheduled_transfers import (
    MaintenanceRunResult,
    ScheduledTransferOutcome,
)
from app.domain.budget_enums import ScheduledTransferStatus
from app.worker.maintenance import LocalMaintenanceCoordinator

NOW = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)


class RecordingMaintenance:
    """A maintenance service that records calls and can be told to fail."""

    def __init__(
        self, *, failures: int = 0, result: MaintenanceRunResult | None = None
    ):
        self.calls = 0
        self.limits: list[int | None] = []
        self._failures = failures
        self._result = result or MaintenanceRunResult(
            claimed=0, executed=0, retried=0, failed=0
        )
        self.ran = threading.Event()

    def run_due(self, *, limit: int | None = None) -> MaintenanceRunResult:
        self.calls += 1
        self.limits.append(limit)
        self.ran.set()
        if self.calls <= self._failures:
            raise RuntimeError("測試用的維護失敗")
        return self._result


def _coordinator(
    service: RecordingMaintenance, **overrides: object
) -> LocalMaintenanceCoordinator:
    values: dict[str, object] = {
        "enabled": True,
        "poll_seconds": 60,
        "batch_size": 7,
        "clock": lambda: NOW,
    }
    values.update(overrides)
    return LocalMaintenanceCoordinator(lambda: service, **values)  # type: ignore[arg-type]


def test_starting_runs_a_catch_up_pass_immediately() -> None:
    service = RecordingMaintenance()
    coordinator = _coordinator(service)
    try:
        coordinator.start()
        assert service.ran.wait(timeout=5.0)
    finally:
        coordinator.stop()

    assert service.calls >= 1
    assert service.limits[0] == 7
    assert coordinator.snapshot().passes >= 1


def test_start_and_stop_are_both_idempotent() -> None:
    service = RecordingMaintenance()
    coordinator = _coordinator(service)
    try:
        coordinator.start()
        coordinator.start()
        assert service.ran.wait(timeout=5.0)
    finally:
        coordinator.stop()
        coordinator.stop()

    assert coordinator.snapshot().running is False


def test_a_disabled_coordinator_never_runs() -> None:
    service = RecordingMaintenance()
    coordinator = _coordinator(service, enabled=False)

    coordinator.start()
    coordinator.stop()

    assert service.calls == 0
    assert coordinator.snapshot().enabled is False


def test_one_failing_pass_never_ends_the_loop() -> None:
    service = RecordingMaintenance(failures=1)
    coordinator = _coordinator(service)

    first = coordinator.run_once()
    snapshot_after_failure = coordinator.snapshot()
    second = coordinator.run_once()

    assert first.claimed == 0
    assert snapshot_after_failure.consecutive_errors == 1
    assert snapshot_after_failure.last_error_code == "RuntimeError"
    assert second.claimed == 0
    assert coordinator.snapshot().consecutive_errors == 0
    assert coordinator.snapshot().last_error_code is None
    assert coordinator.snapshot().passes == 2


def test_a_maintenance_failure_records_no_sensitive_detail(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = RecordingMaintenance(failures=1)
    coordinator = _coordinator(service)

    with caplog.at_level("ERROR"):
        coordinator.run_once()

    assert "error_code=RuntimeError" in caplog.text
    snapshot = coordinator.snapshot()
    assert snapshot.last_error_code == "RuntimeError"
    for field in (snapshot.last_claimed, snapshot.last_executed, snapshot.last_failed):
        assert isinstance(field, int)


def test_the_snapshot_reports_the_last_pass_in_counts_only() -> None:
    service = RecordingMaintenance(
        result=MaintenanceRunResult(
            claimed=3,
            executed=2,
            retried=1,
            failed=0,
            outcomes=(
                ScheduledTransferOutcome(
                    scheduled_transfer_id=1,
                    status=ScheduledTransferStatus.ASSUMED_EXECUTED,
                    attempt=1,
                    entry_id=9,
                ),
            ),
        )
    )
    coordinator = _coordinator(service)

    coordinator.run_once()

    snapshot = coordinator.snapshot()
    assert (snapshot.last_claimed, snapshot.last_executed) == (3, 2)
    assert snapshot.last_retried == 1
    assert snapshot.last_failed == 0
    assert snapshot.poll_seconds == 60
    assert snapshot.batch_size == 7
    assert snapshot.last_finished_at == NOW


def test_stopping_waits_for_the_running_pass() -> None:
    release = threading.Event()

    class SlowMaintenance(RecordingMaintenance):
        def run_due(self, *, limit: int | None = None) -> MaintenanceRunResult:
            result = super().run_due(limit=limit)
            release.wait(timeout=5.0)
            return result

    service = SlowMaintenance()
    coordinator = _coordinator(service, poll_seconds=60)
    coordinator.start()
    assert service.ran.wait(timeout=5.0)
    release.set()

    coordinator.stop(timeout=5.0)

    assert coordinator.snapshot().running is False

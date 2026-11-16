"""Filesystem and command-boundary tests for generated LaunchAgents."""

from __future__ import annotations

import plistlib
from collections.abc import Sequence
from pathlib import Path
from subprocess import CompletedProcess

import pytest
from app.application.services.macos_services import ManageMacOSServices
from app.domain.errors import SystemServiceError
from app.domain.system_services import MacOSServiceDefinition, MacOSServiceRequest
from app.infrastructure.config.settings import AppSettings
from app.infrastructure.macos.launchd import LaunchdServiceGateway


class FakeLaunchctl:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.loaded: set[str] = set()
        self.fail_bootstrap_for: str | None = None

    def __call__(self, arguments: Sequence[str]) -> CompletedProcess[str]:
        args = tuple(arguments)
        self.calls.append(args)
        if args[:2] == ("/bin/launchctl", "print"):
            label = args[2].split("/", 2)[-1]
            return CompletedProcess(args, int(label not in self.loaded), "", "")
        if args[:2] == ("/bin/launchctl", "bootout"):
            self.loaded.discard(args[2].split("/", 2)[-1])
            return CompletedProcess(args, 0, "", "")
        if args[:2] == ("/bin/launchctl", "bootstrap"):
            label = Path(args[3]).stem
            if label == self.fail_bootstrap_for:
                return CompletedProcess(args, 5, "", "synthetic failure")
            self.loaded.add(label)
            return CompletedProcess(args, 0, "", "")
        if args[:2] == ("/usr/bin/plutil", "-lint"):
            return CompletedProcess(args, 0, "", "")
        raise AssertionError(args)


def _definition(directory: Path, label: str, marker: str) -> MacOSServiceDefinition:
    payload = plistlib.dumps(
        {"Label": label, "ProgramArguments": ["/fixture/python", marker]},
        fmt=plistlib.FMT_XML,
        sort_keys=True,
    )
    return MacOSServiceDefinition(
        label=label,
        destination=directory / f"{label}.plist",
        payload=payload,
    )


def test_install_writes_private_atomic_plists_and_bootstraps(tmp_path: Path) -> None:
    runner = FakeLaunchctl()
    gateway = LaunchdServiceGateway(uid=501, runner=runner, launch_agents_dir=tmp_path)
    definitions = (
        _definition(tmp_path, "scribe-local.web", "web"),
        _definition(tmp_path, "scribe-local.sync", "sync"),
    )

    states = gateway.install(definitions)

    assert all(state.installed and state.loaded for state in states)
    for definition in definitions:
        assert definition.destination.stat().st_mode & 0o777 == 0o600
        assert (
            plistlib.loads(definition.destination.read_bytes())["Label"]
            == definition.label
        )
    assert sum(call[:2] == ("/usr/bin/plutil", "-lint") for call in runner.calls) == 2
    assert (
        sum(call[:2] == ("/bin/launchctl", "bootstrap") for call in runner.calls) == 2
    )


def test_reinstall_boots_out_loaded_services_and_replaces_generated_files(
    tmp_path: Path,
) -> None:
    runner = FakeLaunchctl()
    gateway = LaunchdServiceGateway(uid=501, runner=runner, launch_agents_dir=tmp_path)
    original = (
        _definition(tmp_path, "scribe-local.web", "old-web"),
        _definition(tmp_path, "scribe-local.sync", "old-sync"),
    )
    gateway.install(original)
    runner.calls.clear()
    replacement = (
        _definition(tmp_path, "scribe-local.web", "new-web"),
        _definition(tmp_path, "scribe-local.sync", "new-sync"),
    )

    gateway.install(replacement)

    assert sum(call[:2] == ("/bin/launchctl", "bootout") for call in runner.calls) == 2
    assert all(b"new-" in item.destination.read_bytes() for item in replacement)


def test_second_write_failure_restores_the_pre_install_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeLaunchctl()
    gateway = LaunchdServiceGateway(uid=501, runner=runner, launch_agents_dir=tmp_path)
    definitions = (
        _definition(tmp_path, "scribe-local.web", "old-web"),
        _definition(tmp_path, "scribe-local.sync", "old-sync"),
    )
    for definition in definitions:
        definition.destination.write_bytes(definition.payload)
        runner.loaded.add(definition.label)
    before = tuple(item.destination.read_bytes() for item in definitions)
    replacement = (
        _definition(tmp_path, "scribe-local.web", "new-web"),
        _definition(tmp_path, "scribe-local.sync", "new-sync"),
    )
    original_write = gateway._write_atomic
    calls = 0

    def fail_second(path: Path, payload: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic write failure")
        original_write(path, payload)

    monkeypatch.setattr(gateway, "_write_atomic", fail_second)

    with pytest.raises(SystemServiceError, match="install"):
        gateway.install(replacement)

    assert tuple(item.destination.read_bytes() for item in definitions) == before
    assert runner.loaded == {item.label for item in definitions}


def test_uninstall_is_idempotent_and_only_removes_named_generated_files(
    tmp_path: Path,
) -> None:
    runner = FakeLaunchctl()
    gateway = LaunchdServiceGateway(uid=501, runner=runner, launch_agents_dir=tmp_path)
    definitions = (
        _definition(tmp_path, "scribe-local.web", "web"),
        _definition(tmp_path, "scribe-local.sync", "sync"),
    )
    gateway.install(definitions)
    unrelated = tmp_path / "unrelated.plist"
    unrelated.write_text("keep", encoding="utf-8")

    gateway.uninstall(tuple(item.label for item in definitions))
    gateway.uninstall(tuple(item.label for item in definitions))

    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert not any(item.destination.exists() for item in definitions)


def test_status_is_read_only_and_separates_installed_from_loaded(
    tmp_path: Path,
) -> None:
    runner = FakeLaunchctl()
    gateway = LaunchdServiceGateway(uid=501, runner=runner, launch_agents_dir=tmp_path)
    installed = _definition(tmp_path, "scribe-local.web", "web")
    missing = _definition(tmp_path, "scribe-local.sync", "sync")
    installed.destination.write_bytes(installed.payload)
    runner.loaded.add(missing.label)
    before = installed.destination.read_bytes()

    states = gateway.status((installed.label, missing.label))

    assert [(item.installed, item.loaded) for item in states] == [
        (True, False),
        (False, True),
    ]
    assert installed.destination.read_bytes() == before


def test_command_failure_has_a_safe_fixed_action_and_no_plist_content(
    tmp_path: Path,
) -> None:
    runner = FakeLaunchctl()
    runner.fail_bootstrap_for = "scribe-local.sync"
    gateway = LaunchdServiceGateway(uid=501, runner=runner, launch_agents_dir=tmp_path)
    definitions = (
        _definition(tmp_path, "scribe-local.web", "private-marker-web"),
        _definition(tmp_path, "scribe-local.sync", "private-marker-sync"),
    )

    with pytest.raises(SystemServiceError) as captured:
        gateway.install(definitions)

    assert (
        captured.value.action
        == "Run `scribe service status` and check macOS user-service permissions."
    )
    assert "private-marker" not in str(captured.value)


def test_application_service_refuses_non_macos_before_touching_gateway(
    tmp_path: Path,
) -> None:
    class UntouchedGateway:
        def status(self, labels: Sequence[str]) -> tuple[object, ...]:
            raise AssertionError("gateway must not be called")

    manager = ManageMacOSServices(
        UntouchedGateway(),  # type: ignore[arg-type]
        AppSettings(),
        launch_agents_dir=tmp_path,
        platform_name=lambda: "linux",
    )

    with pytest.raises(SystemServiceError, match="only on macOS"):
        manager.status()


def test_request_resolves_paths_once_before_rendering(tmp_path: Path) -> None:
    instance = tmp_path / "instance"
    instance.mkdir()
    python = tmp_path / "python"
    python.write_text("fixture", encoding="utf-8")

    request = MacOSServiceRequest(instance, python, 501)

    assert request.instance_dir == instance.resolve()
    assert request.python_executable == python.resolve()

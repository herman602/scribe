"""Pure contracts for portable macOS service definitions."""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest
from app.domain.system_services import MacOSServiceRequest
from app.infrastructure.config.settings import AppSettings
from app.infrastructure.macos.launchd import build_service_definitions


def _request(instance_dir: Path, python: Path) -> MacOSServiceRequest:
    return MacOSServiceRequest(
        instance_dir=instance_dir,
        python_executable=python,
        uid=501,
    )


def test_service_definitions_are_portable_and_exact(tmp_path: Path) -> None:
    instance = tmp_path / "Scribe 測試 & local"
    instance.mkdir()
    python = tmp_path / "Python Runtime" / "python"
    python.parent.mkdir()
    python.write_text("fixture", encoding="utf-8")
    launch_agents = tmp_path / "Library" / "LaunchAgents"

    definitions = build_service_definitions(
        _request(instance, python),
        AppSettings(),
        launch_agents_dir=launch_agents,
    )

    assert [item.label for item in definitions] == [
        "scribe-local.web",
        "scribe-local.sync",
    ]
    assert [item.destination for item in definitions] == [
        launch_agents / "scribe-local.web.plist",
        launch_agents / "scribe-local.sync.plist",
    ]
    web = plistlib.loads(definitions[0].payload)
    sync = plistlib.loads(definitions[1].payload)
    assert web["ProgramArguments"] == [str(python.resolve()), "-m", "app", "serve"]
    assert sync["ProgramArguments"] == [str(python.resolve()), "-m", "app", "sync"]
    assert web["WorkingDirectory"] == str(instance.resolve())
    assert sync["WorkingDirectory"] == str(instance.resolve())
    assert web["RunAtLoad"] is True
    assert web["KeepAlive"] is True
    assert "RunAtLoad" not in sync
    assert "KeepAlive" not in sync
    assert [item["Hour"] for item in sync["StartCalendarInterval"]] == [
        0,
        3,
        6,
        9,
        12,
        15,
        18,
        21,
    ]
    for payload in (web, sync):
        assert payload["Umask"] == 0o077
        assert payload["EnvironmentVariables"] == {"PYTHONUNBUFFERED": "1"}


@pytest.mark.parametrize("relative", ["instance", "python"])
def test_relative_service_paths_are_rejected(tmp_path: Path, relative: str) -> None:
    instance = tmp_path
    python = tmp_path / "python"
    python.write_text("fixture", encoding="utf-8")
    if relative == "instance":
        instance = Path("relative-instance")
    else:
        python = Path("relative-python")

    with pytest.raises(ValueError, match="absolute"):
        _request(instance, python)


def test_missing_python_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Python executable"):
        _request(tmp_path, tmp_path / "missing-python")


def test_non_directory_instance_is_rejected(tmp_path: Path) -> None:
    instance = tmp_path / "not-a-directory"
    instance.write_text("fixture", encoding="utf-8")
    python = tmp_path / "python"
    python.write_text("fixture", encoding="utf-8")

    with pytest.raises(ValueError, match="instance directory"):
        _request(instance, python)

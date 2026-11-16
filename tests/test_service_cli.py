"""CLI contracts for generated macOS services."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from app.domain.errors import SystemServiceError
from app.domain.system_services import MacOSServiceRequest
from app.presentation.cli.app import cli
from typer.testing import CliRunner

runner = CliRunner()


def test_service_install_passes_explicit_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = tmp_path / "instance"
    instance.mkdir()
    python = tmp_path / "python"
    python.write_text("fixture", encoding="utf-8")
    captured: dict[str, object] = {}

    class Service:
        def install(self, request: object) -> tuple[SimpleNamespace, ...]:
            captured["request"] = request
            return (
                SimpleNamespace(label="scribe-local.web", installed=True, loaded=True),
                SimpleNamespace(label="scribe-local.sync", installed=True, loaded=True),
            )

    monkeypatch.setattr(
        "app.presentation.cli.services.build_macos_service_manager",
        lambda settings: Service(),
    )

    result = runner.invoke(
        cli,
        [
            "service",
            "install",
            "--instance-dir",
            str(instance),
            "--python-executable",
            str(python),
        ],
    )

    assert result.exit_code == 0
    request = captured["request"]
    assert isinstance(request, MacOSServiceRequest)
    assert request.instance_dir == instance.resolve()
    assert request.python_executable == python.resolve()
    assert "scribe-local.web installed=true loaded=true" in result.stdout


def test_service_status_is_successful_when_nothing_is_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SimpleNamespace(
        status=lambda: (
            SimpleNamespace(label="scribe-local.web", installed=False, loaded=False),
            SimpleNamespace(label="scribe-local.sync", installed=False, loaded=False),
        )
    )
    monkeypatch.setattr(
        "app.presentation.cli.services.build_macos_service_manager",
        lambda settings: service,
    )

    result = runner.invoke(cli, ["service", "status"])

    assert result.exit_code == 0
    assert "installed=false loaded=false" in result.stdout


def test_service_uninstall_reports_safe_errors_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = SystemServiceError(
        "could not uninstall user services", action="Retry later."
    )
    service = SimpleNamespace(
        uninstall=lambda: (_ for _ in ()).throw(error),
    )
    monkeypatch.setattr(
        "app.presentation.cli.services.build_macos_service_manager",
        lambda settings: service,
    )

    result = runner.invoke(cli, ["service", "uninstall"])

    assert result.exit_code == 1
    assert "could not uninstall user services" in result.stderr
    assert "Retry later." in result.stderr
    assert "Traceback" not in result.stderr

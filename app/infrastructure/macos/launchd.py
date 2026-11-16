"""Safe LaunchAgent rendering and current-user launchd adapter."""

from __future__ import annotations

import os
import plistlib
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from subprocess import CompletedProcess

from app.domain.errors import SystemServiceError
from app.domain.system_services import (
    MacOSServiceDefinition,
    MacOSServiceRequest,
    MacOSServiceState,
)
from app.infrastructure.config.settings import AppSettings

CommandRunner = Callable[[Sequence[str]], CompletedProcess[str]]
_SAFE_ACTION = "Run `scribe service status` and check macOS user-service permissions."
_LABELS = ("scribe-local.web", "scribe-local.sync")


def _resolve_from_instance(instance_dir: Path, configured: Path) -> Path:
    return (
        configured.resolve()
        if configured.is_absolute()
        else (instance_dir / configured).resolve()
    )


def build_service_definitions(
    request: MacOSServiceRequest,
    settings: AppSettings,
    *,
    launch_agents_dir: Path | None = None,
) -> tuple[MacOSServiceDefinition, ...]:
    """Render deterministic plist bytes without writing or invoking commands."""
    destination_dir = (
        launch_agents_dir
        if launch_agents_dir is not None
        else Path.home() / "Library" / "LaunchAgents"
    ).resolve()
    log_dir = _resolve_from_instance(request.instance_dir, settings.log_dir)
    common: dict[str, object] = {
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
        "ProcessType": "Background",
        "Umask": 0o077,
        "WorkingDirectory": str(request.instance_dir),
    }
    web = {
        **common,
        "KeepAlive": True,
        "Label": _LABELS[0],
        "ProgramArguments": [
            str(request.python_executable),
            "-m",
            "app",
            "serve",
        ],
        "RunAtLoad": True,
        "StandardErrorPath": str(log_dir / "service-web.stderr.log"),
        "StandardOutPath": str(log_dir / "service-web.stdout.log"),
    }
    sync = {
        **common,
        "Label": _LABELS[1],
        "ProgramArguments": [
            str(request.python_executable),
            "-m",
            "app",
            "sync",
        ],
        "StandardErrorPath": str(log_dir / "service-sync.stderr.log"),
        "StandardOutPath": str(log_dir / "service-sync.stdout.log"),
        "StartCalendarInterval": [
            {"Hour": hour, "Minute": 0} for hour in range(0, 24, 3)
        ],
    }
    return tuple(
        MacOSServiceDefinition(
            label=label,
            destination=destination_dir / f"{label}.plist",
            payload=plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True),
        )
        for label, payload in ((_LABELS[0], web), (_LABELS[1], sync))
    )


def _run(arguments: Sequence[str]) -> CompletedProcess[str]:
    return subprocess.run(
        list(arguments),
        check=False,
        capture_output=True,
        text=True,
    )


class LaunchdServiceGateway:
    """Manage only the two generated Scribe LaunchAgents for one GUI user."""

    def __init__(
        self,
        *,
        uid: int,
        launch_agents_dir: Path | None = None,
        runner: CommandRunner = _run,
    ) -> None:
        self._uid = uid
        self._domain = f"gui/{uid}"
        self._launch_agents_dir = (
            launch_agents_dir
            if launch_agents_dir is not None
            else Path.home() / "Library" / "LaunchAgents"
        ).resolve()
        self._runner = runner

    def _destination(self, label: str) -> Path:
        if label not in _LABELS:
            raise SystemServiceError(
                "Refusing to manage an unknown user service.", action=_SAFE_ACTION
            )
        return self._launch_agents_dir / f"{label}.plist"

    def _loaded(self, label: str) -> bool:
        result = self._runner(("/bin/launchctl", "print", f"{self._domain}/{label}"))
        return result.returncode == 0

    def _command(self, arguments: Sequence[str], *, operation: str) -> None:
        result = self._runner(arguments)
        if result.returncode != 0:
            raise SystemServiceError(
                f"Could not {operation} Scribe's macOS user service.",
                action=_SAFE_ACTION,
            )

    def _write_atomic(self, path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            plistlib.loads(temporary.read_bytes())
            self._command(
                ("/usr/bin/plutil", "-lint", str(temporary)),
                operation="validate",
            )
            os.replace(temporary, path)
            path.chmod(0o600)
        finally:
            temporary.unlink(missing_ok=True)

    def install(
        self, definitions: Sequence[MacOSServiceDefinition]
    ) -> tuple[MacOSServiceState, ...]:
        if tuple(item.label for item in definitions) != _LABELS:
            raise SystemServiceError(
                "The generated Scribe service set is incomplete.", action=_SAFE_ACTION
            )
        for definition in definitions:
            if (
                definition.destination.resolve()
                != self._destination(definition.label).resolve()
            ):
                raise SystemServiceError(
                    "Refusing to write a generated service outside LaunchAgents.",
                    action=_SAFE_ACTION,
                )

        backups: dict[Path, bytes | None] = {
            item.destination: (
                item.destination.read_bytes() if item.destination.is_file() else None
            )
            for item in definitions
        }
        loaded_before = {item.label for item in definitions if self._loaded(item.label)}
        try:
            for label in loaded_before:
                self._command(
                    ("/bin/launchctl", "bootout", f"{self._domain}/{label}"),
                    operation="stop",
                )
            for definition in definitions:
                self._write_atomic(definition.destination, definition.payload)
            for definition in definitions:
                self._command(
                    (
                        "/bin/launchctl",
                        "bootstrap",
                        self._domain,
                        str(definition.destination),
                    ),
                    operation="install",
                )
        except (
            OSError,
            ValueError,
            plistlib.InvalidFileException,
            SystemServiceError,
        ) as error:
            for definition in definitions:
                if self._loaded(definition.label):
                    self._runner(
                        (
                            "/bin/launchctl",
                            "bootout",
                            f"{self._domain}/{definition.label}",
                        )
                    )
            for path, payload in backups.items():
                if payload is None:
                    path.unlink(missing_ok=True)
                else:
                    self._write_atomic(path, payload)
            for definition in definitions:
                if definition.label in loaded_before:
                    self._command(
                        (
                            "/bin/launchctl",
                            "bootstrap",
                            self._domain,
                            str(definition.destination),
                        ),
                        operation="restore",
                    )
            if isinstance(error, SystemServiceError):
                raise
            raise SystemServiceError(
                "Could not install Scribe's macOS user services.", action=_SAFE_ACTION
            ) from error
        return self.status(_LABELS)

    def uninstall(self, labels: Sequence[str]) -> tuple[MacOSServiceState, ...]:
        for label in labels:
            destination = self._destination(label)
            if self._loaded(label):
                self._command(
                    ("/bin/launchctl", "bootout", f"{self._domain}/{label}"),
                    operation="uninstall",
                )
            destination.unlink(missing_ok=True)
        return self.status(labels)

    def status(self, labels: Sequence[str]) -> tuple[MacOSServiceState, ...]:
        return tuple(
            MacOSServiceState(
                label=label,
                installed=self._destination(label).is_file(),
                loaded=self._loaded(label),
                destination=self._destination(label),
            )
            for label in labels
        )

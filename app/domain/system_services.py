"""Immutable local-service models independent of launchd and Typer."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MacOSServiceRequest:
    """Validated paths and user identity used to render local services."""

    instance_dir: Path
    python_executable: Path
    uid: int

    def __post_init__(self) -> None:
        if (
            not self.instance_dir.is_absolute()
            or not self.python_executable.is_absolute()
        ):
            raise ValueError("macOS service paths must be absolute")
        instance_dir = self.instance_dir.resolve()
        python_executable = self.python_executable.resolve()
        if not instance_dir.is_dir():
            raise ValueError("the Scribe instance directory does not exist")
        if not python_executable.is_file():
            raise ValueError("the Python executable does not exist")
        if self.uid < 0:
            raise ValueError("the macOS user id must not be negative")
        object.__setattr__(self, "instance_dir", instance_dir)
        object.__setattr__(self, "python_executable", python_executable)


@dataclass(frozen=True)
class MacOSServiceDefinition:
    """One generated LaunchAgent and its exact private destination."""

    label: str
    destination: Path
    payload: bytes


@dataclass(frozen=True)
class MacOSServiceState:
    """Installed and launchd-loaded state are intentionally separate."""

    label: str
    installed: bool
    loaded: bool
    destination: Path

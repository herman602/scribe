"""Typer adapter for generated current-user macOS services."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from app.bootstrap import build_macos_service_manager
from app.domain.errors import SystemServiceError
from app.domain.system_services import MacOSServiceRequest, MacOSServiceState
from app.infrastructure.config.settings import AppSettings

service_cli = typer.Typer(
    help="Install or inspect generated current-user macOS services.",
    no_args_is_help=True,
)


def _settings() -> AppSettings:
    try:
        return AppSettings()
    except ValidationError as error:
        typer.echo(f"Configuration error:\n{error}", err=True)
        raise typer.Exit(code=2) from error


def _emit(states: tuple[MacOSServiceState, ...]) -> None:
    for state in states:
        typer.echo(
            f"{state.label} installed={str(state.installed).lower()} "
            f"loaded={str(state.loaded).lower()}"
        )


def _fail(error: SystemServiceError) -> None:
    typer.echo(str(error), err=True)
    typer.echo(error.action, err=True)
    raise typer.Exit(code=1) from error


@service_cli.command("install")
def install(
    instance_dir: Annotated[
        Path | None,
        typer.Option(help="Scribe checkout containing config.toml and app/."),
    ] = None,
    python_executable: Annotated[
        Path | None, typer.Option(help="Python executable used to run both services.")
    ] = None,
) -> None:
    """Generate, validate, install, and load both Scribe LaunchAgents."""
    try:
        request = MacOSServiceRequest(
            instance_dir=(instance_dir or Path.cwd()).resolve(),
            python_executable=(python_executable or Path(sys.executable)).resolve(),
            uid=os.getuid(),
        )
        states = build_macos_service_manager(_settings()).install(request)
    except (SystemServiceError, ValueError) as error:
        if isinstance(error, SystemServiceError):
            _fail(error)
        typer.echo(f"Could not install Scribe services: {error}", err=True)
        raise typer.Exit(code=1) from error
    _emit(states)


@service_cli.command("status")
def status() -> None:
    """Show generated-file and launchd state without changing either."""
    try:
        states = build_macos_service_manager(_settings()).status()
    except SystemServiceError as error:
        _fail(error)
    _emit(states)


@service_cli.command("uninstall")
def uninstall() -> None:
    """Unload and remove only Scribe's two generated LaunchAgents."""
    try:
        states = build_macos_service_manager(_settings()).uninstall()
    except SystemServiceError as error:
        _fail(error)
    _emit(states)

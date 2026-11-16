"""Application workflow for portable current-user macOS services."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from app.domain.errors import SystemServiceError
from app.domain.ports.system_services import MacOSServiceGateway
from app.domain.system_services import MacOSServiceRequest, MacOSServiceState
from app.infrastructure.config.settings import AppSettings
from app.infrastructure.macos.launchd import build_service_definitions

SERVICE_LABELS = ("scribe-local.web", "scribe-local.sync")


class ManageMacOSServices:
    """Render and manage Scribe LaunchAgents without building app runtimes."""

    def __init__(
        self,
        gateway: MacOSServiceGateway,
        settings: AppSettings,
        *,
        launch_agents_dir: Path,
        platform_name: Callable[[], str],
    ) -> None:
        self._gateway = gateway
        self._settings = settings
        self._launch_agents_dir = launch_agents_dir
        self._platform_name = platform_name

    def _require_macos(self) -> None:
        if self._platform_name() != "darwin":
            raise SystemServiceError(
                "Scribe service management is available only on macOS.",
                action="Run Scribe manually with `uv run python -m app serve`.",
            )

    def install(self, request: MacOSServiceRequest) -> tuple[MacOSServiceState, ...]:
        self._require_macos()
        definitions = build_service_definitions(
            request,
            self._settings,
            launch_agents_dir=self._launch_agents_dir,
        )
        return self._gateway.install(definitions)

    def uninstall(self) -> tuple[MacOSServiceState, ...]:
        self._require_macos()
        return self._gateway.uninstall(SERVICE_LABELS)

    def status(self) -> tuple[MacOSServiceState, ...]:
        self._require_macos()
        return self._gateway.status(SERVICE_LABELS)

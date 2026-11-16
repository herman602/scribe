"""Port for current-user operating-system services."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from app.domain.system_services import MacOSServiceDefinition, MacOSServiceState


class MacOSServiceGateway(Protocol):
    """Install, remove, and inspect only Scribe's generated services."""

    def install(
        self, definitions: Sequence[MacOSServiceDefinition]
    ) -> tuple[MacOSServiceState, ...]: ...

    def uninstall(self, labels: Sequence[str]) -> tuple[MacOSServiceState, ...]: ...

    def status(self, labels: Sequence[str]) -> tuple[MacOSServiceState, ...]: ...

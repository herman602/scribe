"""Pure access-policy rules for local and Cloudflare-fronted requests."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from app.infrastructure.config.settings import AppSettings

_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "testserver"})
_EXTERNAL_EXACT_PATHS = frozenset({"/", "/reports"})
_EXTERNAL_PATH_PREFIXES = ("/static/",)
_READ_METHODS = frozenset({"GET", "HEAD"})
_EXTERNAL_CONFIRM_PATH = "/transactions/confirm"


def local_full_hosts(settings: AppSettings) -> frozenset[str]:
    """Return exact Host values that receive the full local application."""
    lan_hosts = settings.lan_trusted_hosts if settings.lan_access_enabled else ()
    return _LOCAL_HOSTS | {settings.host} | frozenset(lan_hosts)


class WebAccessMode(StrEnum):
    """Request exposure level selected from its validated Host header."""

    LOCAL_FULL = "local_full"
    EXTERNAL_READ_ONLY = "external_read_only"


class AccessAssertionVerifier(Protocol):
    """Verify a signed identity assertion supplied by the access proxy."""

    def verify(self, assertion: str) -> str:
        """Return the verified identity email or raise on invalid input."""
        ...


@dataclass(frozen=True)
class WebAccessPolicy:
    """Describe trusted hosts and the deliberately narrow external surface."""

    local_hosts: frozenset[str]
    external_hostname: str | None
    external_confirmation_enabled: bool

    @classmethod
    def from_settings(cls, settings: AppSettings) -> WebAccessPolicy:
        """Build a policy without broadening the loopback listener."""
        local_hosts = local_full_hosts(settings)
        external_hostname = (
            settings.external_hostname if settings.external_access_enabled else None
        )
        return cls(
            local_hosts=local_hosts,
            external_hostname=external_hostname,
            external_confirmation_enabled=(
                settings.external_confirmation_enabled and external_hostname is not None
            ),
        )

    @property
    def trusted_hosts(self) -> tuple[str, ...]:
        """Return exact hosts accepted by TrustedHostMiddleware."""
        hosts = set(self.local_hosts)
        if self.external_hostname is not None:
            hosts.add(self.external_hostname)
        return tuple(sorted(hosts))

    def mode_for(self, hostname: str | None) -> WebAccessMode:
        """Classify a trusted request as local-full or external-read-only."""
        normalized = hostname.lower().rstrip(".") if hostname else ""
        if self.external_hostname is not None and normalized == self.external_hostname:
            return WebAccessMode.EXTERNAL_READ_ONLY
        return WebAccessMode.LOCAL_FULL

    def permits(self, mode: WebAccessMode, method: str, path: str) -> bool:
        """Allow only report assets and safe reads through the external hostname."""
        if mode is WebAccessMode.LOCAL_FULL:
            return True
        normalized_method = method.upper()
        if normalized_method in _READ_METHODS:
            return path in _EXTERNAL_EXACT_PATHS or path.startswith(
                _EXTERNAL_PATH_PREFIXES
            )
        return (
            self.external_confirmation_enabled
            and normalized_method == "POST"
            and path == _EXTERNAL_CONFIRM_PATH
        )

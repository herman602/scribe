"""Validated application configuration."""

from __future__ import annotations

import ipaddress
import re
from datetime import time
from decimal import Decimal
from pathlib import Path
from typing import Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from app.domain.enums import BodyRetentionPolicy as BodyRetentionPolicy

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
_DNS_HOSTNAME_PATTERN = re.compile(
    r"(?=^.{1,253}$)"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?"
)
_PRIVATE_LAN_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


class AppSettings(BaseSettings):
    """Settings loaded from init args, environment, .env, TOML, then defaults."""

    model_config = SettingsConfigDict(
        env_prefix="SCRIBE_",
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        toml_file="config.toml",
    )

    data_dir: Path = Path("data")
    database_path: Path = Path("data/app.sqlite3")
    gmail_credentials_path: Path = Path("credentials.json")
    gmail_token_path: Path = Path("data/token.json")
    # Google does not expose OAuth publishing status in a token. Keep the
    # local policy explicit so a Testing project can warn before its seven-day
    # refresh-token expiry; set false after publishing the project.
    gmail_oauth_testing_mode: bool = True
    gmail_labeling_enabled: bool = False
    gmail_processed_label: str = "Scribe/已處理"
    log_dir: Path = Path("data/logs")
    log_file_name: str = "scribe.log"
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    lan_access_enabled: bool = False
    lan_trusted_hosts: tuple[str, ...] = ()
    external_access_enabled: bool = False
    external_hostname: str | None = None
    external_read_only: bool = True
    external_confirmation_enabled: bool = False
    cloudflare_access_team_domain: str | None = None
    cloudflare_access_audience: str | None = None
    cloudflare_access_allowed_email: str | None = None
    log_level: LogLevel = "INFO"
    body_retention_policy: BodyRetentionPolicy = BodyRetentionPolicy.FULL_TEXT
    default_gmail_query: str | None = None
    sync_batch_size: int = Field(default=100, ge=1, le=500)
    sync_worker_lease_seconds: int = Field(default=300, ge=30, le=3600)
    gmail_request_timeout_seconds: int = Field(default=30, ge=5, le=300)
    gmail_retry_max_attempts: int = Field(default=5, ge=1, le=10)
    gmail_backoff_base_seconds: float = Field(default=1.0, ge=1.0, le=30.0)
    gmail_backoff_max_seconds: float = Field(default=64.0, ge=1.0, le=300.0)
    gmail_max_body_bytes: int = Field(
        default=5 * 1024 * 1024,
        ge=1024,
        le=50 * 1024 * 1024,
    )
    gmail_max_attachment_bytes: int = Field(
        default=25 * 1024 * 1024,
        ge=1024,
        le=100 * 1024 * 1024,
    )
    pdf_max_bytes: int = Field(
        default=10 * 1024 * 1024,
        ge=1024,
        le=50 * 1024 * 1024,
    )
    pdf_max_pages: int = Field(default=30, ge=1, le=500)
    pdf_max_text_chars: int = Field(default=200_000, ge=1000, le=2_000_000)
    pdf_max_password_attempts: int = Field(default=5, ge=1, le=20)
    pdf_max_seconds: float = Field(default=10.0, ge=0.1, le=120.0)
    body_summary_chars: int = Field(default=1000, ge=100, le=10_000)
    parser_confidence_threshold: Decimal = Field(
        default=Decimal("0.75"), ge=Decimal(0), le=Decimal(1)
    )
    local_timezone: str = "Asia/Taipei"
    default_currency: str = "TWD"
    maintenance_enabled: bool = True
    maintenance_poll_seconds: int = Field(default=60, ge=5, le=3600)
    maintenance_batch_size: int = Field(default=20, ge=1, le=200)
    maintenance_lease_seconds: int = Field(default=120, ge=30, le=3600)
    maintenance_retry_base_seconds: int = Field(default=30, ge=1, le=3600)
    maintenance_retry_max_seconds: int = Field(default=1800, ge=1, le=86_400)
    scheduled_transfer_default_time: str = "06:00"
    scheduled_transfer_timezone: str = "Asia/Taipei"

    @field_validator(
        "data_dir",
        "database_path",
        "gmail_credentials_path",
        "gmail_token_path",
        "log_dir",
        mode="before",
    )
    @classmethod
    def expand_user_path(cls, value: object) -> object:
        """Expand a leading home-directory marker without changing relativity."""
        if isinstance(value, (str, Path)):
            return Path(value).expanduser()
        return value

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Add config.toml below environment and .env in source priority."""
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            TomlConfigSettingsSource(settings_cls),
            file_secret_settings,
        )

    @field_validator("host")
    @classmethod
    def require_safe_bind_host(cls, value: str) -> str:
        """Allow loopback by default and IPv4 wildcard only with LAN policy."""
        normalized = value.strip().lower()
        if normalized in {"localhost", "0.0.0.0"}:
            return normalized
        try:
            address = ipaddress.ip_address(normalized)
        except ValueError as error:
            raise ValueError(
                "host must be localhost or a loopback IP address"
            ) from error
        if not address.is_loopback:
            raise ValueError("host must be loopback or 0.0.0.0 with LAN access")
        return normalized

    @field_validator("lan_trusted_hosts")
    @classmethod
    def require_private_lan_hosts(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Accept explicit private IP addresses, never ranges or DNS wildcards."""
        normalized: list[str] = []
        for value in values:
            candidate = value.strip().lower()
            try:
                address = ipaddress.ip_address(candidate)
            except ValueError as error:
                raise ValueError(
                    "lan_trusted_hosts must contain exact private IP addresses"
                ) from error
            if not any(address in network for network in _PRIVATE_LAN_NETWORKS):
                raise ValueError(
                    "lan_trusted_hosts must contain exact private LAN addresses"
                )
            normalized.append(address.compressed)
        if len(set(normalized)) != len(normalized):
            raise ValueError("lan_trusted_hosts cannot contain duplicates")
        return tuple(normalized)

    @field_validator("external_hostname")
    @classmethod
    def require_dns_external_hostname(cls, value: str | None) -> str | None:
        """Accept one exact DNS hostname, never a URL, wildcard, or IP address."""
        if value is None:
            return None
        normalized = value.strip().lower().rstrip(".")
        if not normalized:
            return None
        if not _DNS_HOSTNAME_PATTERN.fullmatch(normalized):
            raise ValueError("external_hostname must be an exact DNS hostname")
        return normalized

    @field_validator("cloudflare_access_team_domain")
    @classmethod
    def require_cloudflare_team_domain(cls, value: str | None) -> str | None:
        """Accept an exact Cloudflare Access team hostname, never a URL."""
        if value is None:
            return None
        normalized = value.strip().lower().rstrip(".")
        if not normalized:
            return None
        if not _DNS_HOSTNAME_PATTERN.fullmatch(normalized) or not normalized.endswith(
            ".cloudflareaccess.com"
        ):
            raise ValueError(
                "cloudflare_access_team_domain must be an exact "
                "*.cloudflareaccess.com hostname"
            )
        return normalized

    @field_validator("cloudflare_access_audience")
    @classmethod
    def require_cloudflare_audience(cls, value: str | None) -> str | None:
        """Normalize the stable Access application audience tag."""
        if value is None:
            return None
        normalized = value.strip().lower()
        if not normalized:
            return None
        if not re.fullmatch(r"[a-f0-9]{64}", normalized):
            raise ValueError("cloudflare_access_audience must be a 64-character tag")
        return normalized

    @field_validator("cloudflare_access_allowed_email")
    @classmethod
    def require_cloudflare_allowed_email(cls, value: str | None) -> str | None:
        """Normalize the one identity permitted to mutate through Access."""
        if value is None:
            return None
        normalized = value.strip().casefold()
        if not normalized:
            return None
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", normalized):
            raise ValueError("cloudflare_access_allowed_email must be an email address")
        return normalized

    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, value: object) -> object:
        """Normalize text log levels before Literal validation."""
        return value.upper() if isinstance(value, str) else value

    @field_validator("gmail_processed_label")
    @classmethod
    def require_safe_gmail_label(cls, value: str) -> str:
        """Keep the application-owned Gmail Label explicit and bounded."""
        normalized = value.strip()
        if not normalized or len(normalized) > 200:
            raise ValueError("gmail_processed_label must be 1 to 200 characters")
        if any(ord(character) < 32 for character in normalized):
            raise ValueError("gmail_processed_label cannot contain control characters")
        return normalized

    @field_validator("default_currency")
    @classmethod
    def require_currency_code(cls, value: str) -> str:
        """Require the ISO-4217 code local budget pages default to."""
        normalized = value.strip().upper()
        if len(normalized) != 3 or not normalized.isalpha():
            raise ValueError("default_currency must be a three-letter code")
        return normalized

    @field_validator("local_timezone", "scheduled_transfer_timezone")
    @classmethod
    def require_valid_timezone(cls, value: str) -> str:
        """Require an installed IANA timezone for calendar-safe totals."""
        normalized = value.strip()
        try:
            ZoneInfo(normalized)
        except ZoneInfoNotFoundError as error:
            raise ValueError("timezone settings must name a valid IANA timezone") from (
                error
            )
        return normalized

    @field_validator("scheduled_transfer_default_time")
    @classmethod
    def require_wall_clock_time(cls, value: str) -> str:
        """Require a whole-minute ``HH:MM`` local policy time.

        A schedule's time is wall-clock policy, so seconds would imply a
        precision it does not have and an offset would contradict the timezone
        the schedule is read in.
        """
        normalized = value.strip()
        if not re.fullmatch(r"([01][0-9]|2[0-3]):[0-5][0-9]", normalized):
            raise ValueError("scheduled_transfer_default_time must be HH:MM")
        return normalized

    @property
    def scheduled_transfer_time(self) -> time:
        """Return the validated local time a new schedule defaults to."""
        hour, _, minute = self.scheduled_transfer_default_time.partition(":")
        return time(int(hour), int(minute))

    @model_validator(mode="after")
    def validate_cross_field_settings(self) -> Self:
        """Keep retry and external-access settings in a safe combination."""
        if self.gmail_backoff_max_seconds < self.gmail_backoff_base_seconds:
            raise ValueError("Gmail max backoff cannot be below its base")
        if self.maintenance_retry_max_seconds < self.maintenance_retry_base_seconds:
            raise ValueError("maintenance max backoff cannot be below its base")
        if self.maintenance_lease_seconds <= self.maintenance_poll_seconds:
            # A lease shorter than one poll would expire between two passes and
            # let a second worker claim a schedule the first is still posting.
            raise ValueError(
                "maintenance_lease_seconds must exceed maintenance_poll_seconds"
            )
        if self.lan_access_enabled:
            if self.host != "0.0.0.0":
                raise ValueError("LAN access requires host=0.0.0.0")
            if not self.lan_trusted_hosts:
                raise ValueError("LAN access requires at least one trusted host")
        elif self.host == "0.0.0.0" or self.lan_trusted_hosts:
            raise ValueError(
                "wildcard binding and LAN hosts require lan_access_enabled=true"
            )
        if self.external_access_enabled and self.external_hostname is None:
            raise ValueError(
                "external_hostname is required when external access is enabled"
            )
        if self.external_access_enabled and not self.external_read_only:
            raise ValueError("external access does not support unrestricted write mode")
        if self.external_confirmation_enabled:
            if not self.external_access_enabled:
                raise ValueError(
                    "external access must be enabled for external confirmation"
                )
            required = {
                "cloudflare_access_team_domain": self.cloudflare_access_team_domain,
                "cloudflare_access_audience": self.cloudflare_access_audience,
                "cloudflare_access_allowed_email": self.cloudflare_access_allowed_email,
            }
            missing = [name for name, value in required.items() if value is None]
            if missing:
                raise ValueError(
                    "external confirmation requires " + ", ".join(sorted(missing))
                )
        return self

    @property
    def log_file_path(self) -> Path:
        """Return the configured rotating log path."""
        return self.log_dir / self.log_file_name

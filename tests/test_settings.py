"""Configuration and local initialization tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from app.infrastructure.config.doctor import CheckStatus, run_foundation_checks
from app.infrastructure.config.initializer import initialize_local_environment
from app.infrastructure.config.settings import AppSettings, BodyRetentionPolicy
from pydantic import ValidationError


def test_default_settings_are_local_and_safe() -> None:
    settings = AppSettings(
        host="127.0.0.1",
        port=8000,
        external_access_enabled=False,
        external_hostname=None,
        external_read_only=True,
        external_confirmation_enabled=False,
        gmail_labeling_enabled=False,
        gmail_processed_label="Scribe/已處理",
        body_retention_policy=BodyRetentionPolicy.FULL_TEXT,
        log_dir=Path("data/logs"),
    )

    assert settings.host == "127.0.0.1"
    assert settings.port == 8000
    assert not settings.lan_access_enabled
    assert settings.lan_trusted_hosts == ()
    assert not settings.external_access_enabled
    assert settings.external_hostname is None
    assert settings.external_read_only
    assert settings.gmail_request_timeout_seconds == 30
    assert not settings.gmail_labeling_enabled
    assert settings.gmail_processed_label == "Scribe/已處理"
    assert settings.local_timezone == "Asia/Taipei"
    assert settings.body_retention_policy is BodyRetentionPolicy.FULL_TEXT
    assert settings.log_file_path == Path("data/logs/scribe.log")


@pytest.mark.parametrize("host", ["192.168.1.10", "example.com"])
def test_settings_reject_non_loopback_hosts(host: str) -> None:
    with pytest.raises(ValidationError, match="loopback"):
        AppSettings(host=host)


def test_wildcard_binding_requires_an_explicit_private_lan_allowlist() -> None:
    with pytest.raises(ValidationError, match="lan_access_enabled"):
        AppSettings(host="0.0.0.0")
    with pytest.raises(ValidationError, match="at least one trusted host"):
        AppSettings(host="0.0.0.0", lan_access_enabled=True)
    with pytest.raises(ValidationError, match=r"host=0\.0\.0\.0"):
        AppSettings(
            host="127.0.0.1",
            lan_access_enabled=True,
            lan_trusted_hosts=("192.168.0.135",),
        )

    settings = AppSettings(
        host="0.0.0.0",
        lan_access_enabled=True,
        lan_trusted_hosts=("192.168.0.135",),
    )

    assert settings.host == "0.0.0.0"
    assert settings.lan_trusted_hosts == ("192.168.0.135",)


@pytest.mark.parametrize(
    "lan_host",
    [
        "8.8.8.8",
        "127.0.0.1",
        "192.0.2.1",
        "0.0.0.0",
        "scribe.local",
        "192.168.0.0/24",
    ],
)
def test_lan_allowlist_rejects_non_private_or_non_exact_hosts(lan_host: str) -> None:
    with pytest.raises(ValidationError, match="lan_trusted_hosts"):
        AppSettings(
            host="0.0.0.0",
            lan_access_enabled=True,
            lan_trusted_hosts=(lan_host,),
        )


def test_lan_allowlist_rejects_duplicates_and_disabled_values() -> None:
    with pytest.raises(ValidationError, match="duplicates"):
        AppSettings(
            host="0.0.0.0",
            lan_access_enabled=True,
            lan_trusted_hosts=("192.168.0.135", "192.168.0.135"),
        )
    with pytest.raises(ValidationError, match="lan_access_enabled"):
        AppSettings(lan_trusted_hosts=("192.168.0.135",))


def test_settings_accept_ipv6_loopback() -> None:
    assert AppSettings(host="::1").host == "::1"


def test_settings_require_safe_external_read_only_configuration() -> None:
    with pytest.raises(ValidationError, match="external_hostname is required"):
        AppSettings(external_access_enabled=True, external_hostname=None)
    with pytest.raises(ValidationError, match="unrestricted"):
        AppSettings(
            external_access_enabled=True,
            external_hostname="scribe.example.com",
            external_read_only=False,
        )


def test_external_confirmation_requires_cloudflare_identity_settings() -> None:
    with pytest.raises(ValidationError, match="external confirmation requires"):
        AppSettings(
            external_access_enabled=True,
            external_hostname="scribe.example.com",
            external_confirmation_enabled=True,
            cloudflare_access_team_domain=None,
            cloudflare_access_audience=None,
            cloudflare_access_allowed_email=None,
        )

    settings = AppSettings(
        external_access_enabled=True,
        external_hostname="scribe.example.com",
        external_confirmation_enabled=True,
        cloudflare_access_team_domain="TEAM.cloudflareaccess.com.",
        cloudflare_access_audience="a" * 64,
        cloudflare_access_allowed_email="Herman@example.com",
    )

    assert settings.cloudflare_access_team_domain == "team.cloudflareaccess.com"
    assert settings.cloudflare_access_allowed_email == "herman@example.com"


@pytest.mark.parametrize(
    "hostname",
    [
        "https://scribe.example.com",
        "*.example.com",
        "127.0.0.1",
        "localhost",
        "scribe.example.com/path",
    ],
)
def test_settings_reject_unsafe_external_hostname(hostname: str) -> None:
    with pytest.raises(ValidationError, match="exact DNS hostname"):
        AppSettings(external_hostname=hostname)


def test_settings_normalize_external_hostname() -> None:
    settings = AppSettings(
        external_access_enabled=True,
        external_hostname="SCRIBE.Example.Com.",
    )

    assert settings.external_hostname == "scribe.example.com"


def test_settings_reject_unknown_local_timezone() -> None:
    with pytest.raises(ValidationError, match="IANA timezone"):
        AppSettings(local_timezone="Mars/Olympus")


def test_settings_reject_too_short_gmail_timeout() -> None:
    with pytest.raises(ValidationError):
        AppSettings(gmail_request_timeout_seconds=4)


def test_settings_expand_user_paths() -> None:
    settings = AppSettings(database_path=Path("~/scribe-data/app.sqlite3"))

    assert settings.database_path == Path.home() / "scribe-data/app.sqlite3"


def test_environment_overrides_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config.toml").write_text(
        'port = 8100\nlog_level = "WARNING"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SCRIBE_PORT", "8200")

    settings = AppSettings()

    assert settings.port == 8200
    assert settings.log_level == "WARNING"


def test_initializer_is_idempotent_and_does_not_overwrite_config(
    tmp_path: Path,
) -> None:
    settings = AppSettings(
        data_dir=tmp_path / "private-data",
        log_dir=tmp_path / "private-data/logs",
    )
    config_path = tmp_path / "config.toml"

    first = initialize_local_environment(settings, config_path)
    original = config_path.read_text(encoding="utf-8")
    second = initialize_local_environment(settings, config_path)

    assert settings.data_dir in first.created_paths
    assert settings.log_dir in first.created_paths
    assert config_path in first.created_paths
    assert "gmail_oauth_testing_mode = true" in original
    assert "lan_access_enabled = false" in original
    assert "lan_trusted_hosts = []" in original
    assert first.existing_paths == ()
    assert second.created_paths == ()
    assert set(second.existing_paths) == {
        settings.data_dir,
        settings.log_dir,
        config_path,
    }
    assert config_path.read_text(encoding="utf-8") == original
    if os.name == "posix":
        assert config_path.stat().st_mode & 0o777 == 0o600


def test_doctor_reports_foundation_and_optional_gmail_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = AppSettings(
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "data/logs",
        gmail_credentials_path=tmp_path / "credentials.json",
        gmail_token_path=tmp_path / "data/token.json",
    )
    initialize_local_environment(settings, tmp_path / "config.toml")

    checks = run_foundation_checks(settings)
    statuses = {check.name: check.status for check in checks}

    assert statuses["Python version"] is CheckStatus.PASS
    assert statuses["config.toml"] is CheckStatus.PASS
    assert statuses["data directory writable"] is CheckStatus.PASS
    assert statuses["Gmail credentials"] is CheckStatus.FAIL
    assert statuses["Gmail token"] is CheckStatus.FAIL
    assert statuses["Gmail API connection"] is CheckStatus.WARN

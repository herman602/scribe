"""Initialize safe local directories and non-secret configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from app.infrastructure.config.settings import AppSettings

_DEFAULT_CONFIG = """\
data_dir = "data"
database_path = "data/app.sqlite3"
gmail_credentials_path = "credentials.json"
gmail_token_path = "data/token.json"
gmail_oauth_testing_mode = true
gmail_labeling_enabled = false
gmail_processed_label = "Scribe/已處理"
log_dir = "data/logs"
host = "127.0.0.1"
port = 8000
lan_access_enabled = false
lan_trusted_hosts = []
external_access_enabled = false
external_read_only = true
external_confirmation_enabled = false
log_level = "INFO"
body_retention_policy = "full_text"
sync_batch_size = 100
gmail_request_timeout_seconds = 30
gmail_retry_max_attempts = 5
gmail_backoff_base_seconds = 1.0
gmail_backoff_max_seconds = 64.0
gmail_max_body_bytes = 5242880
gmail_max_attachment_bytes = 26214400
pdf_max_bytes = 10485760
pdf_max_pages = 30
pdf_max_text_chars = 200000
pdf_max_password_attempts = 5
pdf_max_seconds = 10.0
body_summary_chars = 1000
parser_confidence_threshold = "0.75"
local_timezone = "Asia/Taipei"
default_currency = "TWD"
maintenance_enabled = true
maintenance_poll_seconds = 60
maintenance_batch_size = 20
maintenance_lease_seconds = 120
maintenance_retry_base_seconds = 30
maintenance_retry_max_seconds = 1800
scheduled_transfer_default_time = "06:00"
scheduled_transfer_timezone = "Asia/Taipei"
"""


@dataclass(frozen=True)
class InitializationResult:
    """Filesystem changes made by initialization."""

    created_paths: tuple[Path, ...]
    existing_paths: tuple[Path, ...]


def initialize_local_environment(
    settings: AppSettings, config_path: Path = Path("config.toml")
) -> InitializationResult:
    """Create private local directories and a non-secret config if absent."""
    created: list[Path] = []
    existing: list[Path] = []

    for directory in (settings.data_dir, settings.log_dir):
        if directory.exists():
            existing.append(directory)
            continue
        directory.mkdir(mode=0o700, parents=True)
        created.append(directory)

    if config_path.exists():
        existing.append(config_path)
    else:
        descriptor = os.open(
            config_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(_DEFAULT_CONFIG)
        created.append(config_path)

    return InitializationResult(tuple(created), tuple(existing))

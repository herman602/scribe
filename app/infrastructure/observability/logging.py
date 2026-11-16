"""Structured, local, sensitive-data-aware logging."""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler

from app.infrastructure.config.settings import AppSettings

LOGGER_NAME = "scribe"
_LOG_CONTEXT: ContextVar[Mapping[str, str] | None] = ContextVar(
    "scribe_log_context", default=None
)
_SECRET_PATTERN = re.compile(
    r"(?i)\b(access_token|refresh_token|client_secret|password)\b"
    r"(\s*[=:]\s*)"
    r"(\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_LONG_NUMBER_PATTERN = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")


def redact_sensitive_text(value: str) -> str:
    """Redact common tokens, secrets, and full financial identifiers."""

    def replace_secret(match: re.Match[str]) -> str:
        return f"{match.group(1)}{match.group(2)}[REDACTED]"

    redacted = _SECRET_PATTERN.sub(replace_secret, value)
    redacted = _BEARER_PATTERN.sub("Bearer [REDACTED]", redacted)
    return _LONG_NUMBER_PATTERN.sub("[REDACTED_NUMBER]", redacted)


class SensitiveDataFilter(logging.Filter):
    """Redact a fully formatted record before handlers emit it."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_sensitive_text(record.getMessage())
        record.args = ()
        return True


class ContextFilter(logging.Filter):
    """Attach bounded structured context to every record."""

    _fields = ("sync_run_id", "email_message_id", "parser_name")

    def filter(self, record: logging.LogRecord) -> bool:
        context = _LOG_CONTEXT.get() or {}
        for field in self._fields:
            setattr(record, field, context.get(field, "-"))
        return True


@contextmanager
def log_context(**values: str | None) -> Iterator[None]:
    """Temporarily add safe structured fields to application logs."""
    current = _LOG_CONTEXT.get() or {}
    merged = {**current, **{key: value for key, value in values.items() if value}}
    token = _LOG_CONTEXT.set(merged)
    try:
        yield
    finally:
        _LOG_CONTEXT.reset(token)


def _prepare_log_file(settings: AppSettings) -> None:
    settings.log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    settings.log_file_path.touch(mode=0o600, exist_ok=True)
    with suppress(OSError):
        os.chmod(settings.log_file_path, 0o600)


def configure_logging(settings: AppSettings) -> logging.Logger:
    """Configure isolated console and rotating-file application handlers."""
    _prepare_log_file(settings)
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(settings.log_level)
    logger.propagate = False

    for handler in logger.handlers:
        handler.close()
    logger.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s "
        "sync_run_id=%(sync_run_id)s "
        "email_message_id=%(email_message_id)s "
        "parser_name=%(parser_name)s %(message)s"
    )

    console = logging.StreamHandler()
    file_handler = RotatingFileHandler(
        settings.log_file_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    for handler in (console, file_handler):
        handler.setLevel(settings.log_level)
        handler.addFilter(ContextFilter())
        handler.addFilter(SensitiveDataFilter())
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger

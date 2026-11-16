"""Logging configuration and redaction tests."""

from __future__ import annotations

import logging
from pathlib import Path

from app.infrastructure.config.settings import AppSettings
from app.infrastructure.observability.logging import (
    configure_logging,
    log_context,
    redact_sensitive_text,
)


def test_redaction_masks_tokens_passwords_and_long_numbers() -> None:
    source = (
        'access_token="abc.def" password=hunter2 '
        "Authorization: Bearer abc.def.ghi card=4111 1111 1111 1111"
    )

    redacted = redact_sensitive_text(source)

    assert "abc.def" not in redacted
    assert "hunter2" not in redacted
    assert "4111" not in redacted
    assert redacted.count("[REDACTED]") >= 3
    assert "[REDACTED_NUMBER]" in redacted


def test_configure_logging_is_idempotent_and_writes_context(
    tmp_path: Path,
) -> None:
    settings = AppSettings(
        log_dir=tmp_path / "logs",
        log_level="INFO",
    )

    first = configure_logging(settings)
    second = configure_logging(settings)
    with log_context(sync_run_id="run-123", parser_name="fixture-parser"):
        second.info("safe event")
    for handler in second.handlers:
        handler.flush()

    handlers = tuple(second.handlers)
    try:
        content = settings.log_file_path.read_text(encoding="utf-8")
        assert first is second
        assert len(second.handlers) == 2
        assert "sync_run_id=run-123" in content
        assert "parser_name=fixture-parser" in content
        assert "safe event" in content
    finally:
        for handler in handlers:
            handler.close()
        logging.getLogger("scribe").handlers.clear()

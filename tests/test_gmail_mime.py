"""Local Gmail MIME and charset decoding tests."""

from __future__ import annotations

from email.header import Header

import pytest
from app.domain.errors import EmailDecodeError
from app.domain.gmail import (
    GmailAttachmentBytes,
    GmailHeader,
    GmailMessagePart,
    GmailRawMessage,
)
from app.infrastructure.gmail.mime import GmailMimeDecoder


def _part(
    *,
    part_id: str,
    mime_type: str,
    content: bytes | None = None,
    filename: str = "",
    attachment_id: str | None = None,
    headers: tuple[GmailHeader, ...] = (),
    parts: tuple[GmailMessagePart, ...] = (),
) -> GmailMessagePart:
    return GmailMessagePart(
        part_id=part_id,
        mime_type=mime_type,
        filename=filename,
        headers=headers,
        body_size=len(content or b""),
        body_data=content,
        attachment_id=attachment_id,
        parts=parts,
    )


def _message(payload: GmailMessagePart) -> GmailRawMessage:
    return GmailRawMessage(
        message_id="message-1",
        thread_id="thread-1",
        history_id="101",
        internal_date_ms=1_721_000_000_000,
        snippet="fixture snippet",
        label_ids=("INBOX", "IMPORTANT", "INBOX"),
        payload=payload,
    )


def test_decoder_prefers_plain_body_and_fetches_pdf_attachment() -> None:
    subject = Header("消費通知", "utf-8").encode()
    root = _part(
        part_id="",
        mime_type="multipart/mixed",
        headers=(
            GmailHeader("From", '"測試銀行" <NOTICE@BANK.EXAMPLE>'),
            GmailHeader("To", "User <user@example.com>"),
            GmailHeader("Cc", "audit@example.com"),
            GmailHeader("Subject", subject),
            GmailHeader("Date", "Mon, 27 Jul 2026 10:30:00 +0800"),
        ),
        parts=(
            _part(
                part_id="0",
                mime_type="text/plain",
                content="本期消費測試".encode("cp950"),
                headers=(GmailHeader("Content-Type", "text/plain; charset=big5"),),
            ),
            _part(
                part_id="1",
                mime_type="text/html",
                content=b"<p>duplicate html</p>",
                headers=(GmailHeader("Content-Type", "text/html; charset=utf-8"),),
            ),
            _part(
                part_id="2",
                mime_type="application/pdf",
                filename="fixture.pdf",
                attachment_id="attachment-1",
            ),
        ),
    )
    decoder = GmailMimeDecoder()
    calls: list[tuple[str, str]] = []

    def load(message_id: str, attachment_id: str) -> GmailAttachmentBytes:
        calls.append((message_id, attachment_id))
        return GmailAttachmentBytes(b"%PDF-1.7 fixture")

    decoded = decoder.decode(_message(root), load)

    assert decoded.sender == "notice@bank.example"
    assert decoded.recipients == ("audit@example.com", "user@example.com")
    assert decoded.subject == "消費通知"
    assert decoded.text_body == "本期消費測試"
    assert decoded.has_html_body
    assert decoded.label_ids == ("IMPORTANT", "INBOX")
    assert decoded.attachments[0].content.startswith(b"%PDF-")
    assert calls == [("message-1", "attachment-1")]


def test_html_only_body_is_converted_without_script_content() -> None:
    root = _part(
        part_id="",
        mime_type="multipart/alternative",
        headers=(
            GmailHeader("From", "notice@bank.example"),
            GmailHeader("Date", "Mon, 27 Jul 2026 10:30:00 +0800"),
        ),
        parts=(
            _part(
                part_id="0",
                mime_type="text/html",
                content=(
                    b"<style>hidden</style><p>Merchant &amp; Co.</p>"
                    b"<script>secret()</script><div>NT$ 100</div>"
                ),
                headers=(GmailHeader("Content-Type", "text/html; charset=utf-8"),),
            ),
        ),
    )

    decoded = GmailMimeDecoder().decode(
        _message(root),
        lambda message_id, attachment_id: GmailAttachmentBytes(b""),
    )

    assert decoded.text_body == "Merchant & Co.\nNT$ 100"
    assert "hidden" not in decoded.text_body
    assert "secret" not in decoded.text_body


def test_invalid_charset_falls_back_with_non_sensitive_warning() -> None:
    root = _part(
        part_id="",
        mime_type="text/plain",
        content=b"\xff\xfe\xfd",
        headers=(
            GmailHeader("From", "notice@bank.example"),
            GmailHeader("Content-Type", "text/plain; charset=unknown-fixture"),
        ),
    )

    decoded = GmailMimeDecoder().decode(
        _message(root),
        lambda message_id, attachment_id: GmailAttachmentBytes(b""),
    )

    assert "\ufffd" in decoded.text_body
    assert {warning.code for warning in decoded.warnings} == {
        "missing_date",
        "charset_replacement",
    }


def test_decoder_enforces_body_and_attachment_byte_limits() -> None:
    root = _part(
        part_id="0",
        mime_type="text/plain",
        content=b"too large",
    )
    decoder = GmailMimeDecoder(max_body_bytes=4)

    with pytest.raises(EmailDecodeError, match="byte limit"):
        decoder.decode(
            _message(root),
            lambda message_id, attachment_id: GmailAttachmentBytes(b""),
        )

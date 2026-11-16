"""Bounded local decoding for Gmail's parsed MIME representation."""

from __future__ import annotations

import re
from datetime import datetime
from email.header import decode_header, make_header
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser

from app.domain.errors import EmailDecodeError
from app.domain.gmail import (
    AttachmentLoader,
    DecodedAttachment,
    DecodedEmail,
    DecodeWarning,
    GmailHeader,
    GmailMessagePart,
    GmailRawMessage,
)

_BLOCK_TAGS = {
    "address",
    "article",
    "br",
    "div",
    "footer",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "li",
    "p",
    "section",
    "table",
    "td",
    "th",
    "tr",
}


class _TextHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fragments: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in {"script", "style"}:
            self.ignored_depth += 1
        elif tag in _BLOCK_TAGS:
            self.fragments.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.ignored_depth:
            self.ignored_depth -= 1
        elif tag in _BLOCK_TAGS:
            self.fragments.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.fragments.append(data)


class GmailMimeDecoder:
    """Decode headers, bodies, charsets, HTML, and attachment content."""

    def __init__(
        self,
        *,
        max_body_bytes: int = 5 * 1024 * 1024,
        max_attachment_bytes: int = 25 * 1024 * 1024,
    ) -> None:
        if max_body_bytes < 1 or max_attachment_bytes < 1:
            raise ValueError("MIME byte limits must be positive")
        self._max_body_bytes = max_body_bytes
        self._max_attachment_bytes = max_attachment_bytes

    def decode(
        self, message: GmailRawMessage, attachment_loader: AttachmentLoader
    ) -> DecodedEmail:
        """Decode one message while keeping recovery warnings content-free."""
        warnings: list[DecodeWarning] = []
        headers = message.payload.headers
        sender_values = _header_values(headers, "from")
        sender = _first_address(sender_values)
        if not sender:
            warnings.append(DecodeWarning("missing_sender", "From address is absent"))
        recipients = _addresses(
            _header_values(headers, "to")
            + _header_values(headers, "cc")
            + _header_values(headers, "bcc")
        )
        subject = _decoded_header(_first_header(headers, "subject"), warnings)
        received_at = _received_at(_first_header(headers, "date"), warnings)

        plain_parts: list[str] = []
        html_parts: list[str] = []
        attachments: list[DecodedAttachment] = []
        self._walk(
            message,
            message.payload,
            path="root",
            attachment_loader=attachment_loader,
            plain_parts=plain_parts,
            html_parts=html_parts,
            attachments=attachments,
            warnings=warnings,
        )
        plain_text = _join_body_parts(plain_parts)
        has_html = bool(html_parts)
        if not plain_text and html_parts:
            plain_text = _html_to_text(_join_body_parts(html_parts))
        return DecodedEmail(
            message_id=message.message_id,
            thread_id=message.thread_id,
            history_id=message.history_id,
            internal_date_ms=message.internal_date_ms,
            sender=sender,
            recipients=recipients,
            subject=subject,
            received_at=received_at,
            snippet=message.snippet,
            text_body=plain_text,
            has_html_body=has_html,
            label_ids=tuple(sorted(set(message.label_ids))),
            attachments=tuple(attachments),
            warnings=tuple(warnings),
        )

    def _walk(
        self,
        message: GmailRawMessage,
        part: GmailMessagePart,
        *,
        path: str,
        attachment_loader: AttachmentLoader,
        plain_parts: list[str],
        html_parts: list[str],
        attachments: list[DecodedAttachment],
        warnings: list[DecodeWarning],
    ) -> None:
        if part.parts:
            for index, child in enumerate(part.parts):
                self._walk(
                    message,
                    child,
                    path=f"{path}.{index}",
                    attachment_loader=attachment_loader,
                    plain_parts=plain_parts,
                    html_parts=html_parts,
                    attachments=attachments,
                    warnings=warnings,
                )
            return

        disposition = _first_header(part.headers, "content-disposition").casefold()
        is_attachment = (
            bool(part.filename)
            or disposition.startswith("attachment")
            or part.mime_type == "application/pdf"
        )
        if not is_attachment and part.mime_type not in {"text/plain", "text/html"}:
            return
        limit = self._max_attachment_bytes if is_attachment else self._max_body_bytes
        content = _part_content(
            message.message_id,
            part,
            attachment_loader,
            limit=limit,
        )
        if is_attachment:
            attachments.append(
                DecodedAttachment(
                    mime_part_id=part.part_id or path,
                    filename=_decoded_header(part.filename, warnings),
                    mime_type=part.mime_type,
                    gmail_attachment_id=part.attachment_id,
                    content=content,
                )
            )
            return
        decoded = _decode_text(content, part.headers, warnings)
        if part.mime_type == "text/plain":
            plain_parts.append(decoded)
        else:
            html_parts.append(decoded)


def _header_values(headers: tuple[GmailHeader, ...], name: str) -> list[str]:
    folded = name.casefold()
    return [item.value for item in headers if item.name.casefold() == folded]


def _first_header(headers: tuple[GmailHeader, ...], name: str) -> str:
    values = _header_values(headers, name)
    return values[0] if values else ""


def _decoded_header(value: str, warnings: list[DecodeWarning]) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except (LookupError, UnicodeError, ValueError):
        warnings.append(
            DecodeWarning("header_decode_fallback", "A MIME header needed fallback")
        )
        return value.encode("utf-8", errors="replace").decode("utf-8")


def _addresses(values: list[str]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                address.strip().lower()
                for _, address in getaddresses(values)
                if address.strip()
            }
        )
    )


def _first_address(values: list[str]) -> str:
    addresses = _addresses(values)
    return addresses[0] if addresses else ""


def _received_at(value: str, warnings: list[DecodeWarning]) -> datetime | None:
    if not value:
        warnings.append(DecodeWarning("missing_date", "Date header is absent"))
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        warnings.append(DecodeWarning("invalid_date", "Date header is invalid"))
        return None
    if parsed.tzinfo is None:
        warnings.append(
            DecodeWarning("naive_date", "Date header has no timezone and was ignored")
        )
        return None
    return parsed


def _part_content(
    message_id: str,
    part: GmailMessagePart,
    attachment_loader: AttachmentLoader,
    *,
    limit: int,
) -> bytes:
    if part.body_data is not None:
        content = part.body_data
    elif part.attachment_id:
        content = attachment_loader(message_id, part.attachment_id).data
    else:
        content = b""
    if len(content) > limit or part.body_size > limit:
        raise EmailDecodeError(
            f"MIME part {part.part_id or 'root'} exceeds the configured byte limit"
        )
    return content


def _decode_text(
    content: bytes,
    headers: tuple[GmailHeader, ...],
    warnings: list[DecodeWarning],
) -> str:
    content_type = _first_header(headers, "content-type")
    message = Message()
    if content_type:
        message["content-type"] = content_type
    declared = message.get_content_charset()
    candidates = [declared, "utf-8", "cp950", "big5"]
    attempted: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate.casefold() in attempted:
            continue
        attempted.add(candidate.casefold())
        try:
            return content.decode(candidate)
        except (LookupError, UnicodeDecodeError):
            continue
    warnings.append(
        DecodeWarning(
            "charset_replacement",
            "Text contained bytes invalid in supported charsets",
        )
    )
    return content.decode("utf-8", errors="replace")


def _join_body_parts(parts: list[str]) -> str:
    normalized = []
    seen = set()
    for value in parts:
        stripped = value.strip()
        if stripped and stripped not in seen:
            normalized.append(stripped)
            seen.add(stripped)
    return "\n\n".join(normalized)


def _html_to_text(value: str) -> str:
    parser = _TextHTMLParser()
    try:
        parser.feed(value)
        parser.close()
    except ValueError as error:
        raise EmailDecodeError("HTML body could not be decoded") from error
    text = "".join(parser.fragments).replace("\xa0", " ")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{2,}", "\n", text).strip()

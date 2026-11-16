"""Prepare one decoded Gmail message for atomic parser persistence."""

from __future__ import annotations

import hashlib
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime

from app.domain.enums import AttachmentStatus
from app.domain.errors import ParserError, ScribeError
from app.domain.gmail import DecodedAttachment, DecodedEmail
from app.domain.models import (
    AttachmentSnapshot,
    ProcessedAttachmentSnapshot,
    SenderRule,
)
from app.domain.parsing import (
    AttachmentExtractor,
    ClassificationEngine,
    ExtractionLimits,
    ParserAttachment,
    ParserEmail,
    PipelineResult,
    RawPdfAttachment,
    SecretStore,
    SecretValue,
)


@dataclass(frozen=True)
class PreparedEmailProcessing:
    """Local extraction/parser outcome with only persistence-safe fields."""

    attachments: tuple[ProcessedAttachmentSnapshot, ...]
    pipeline_result: PipelineResult | None
    error_code: str | None = None
    error_message: str | None = None
    debug_trace: str | None = None

    @property
    def succeeded(self) -> bool:
        """Return whether a complete pipeline result is available."""
        return self.pipeline_result is not None


class PrepareEmailProcessing:
    """Resolve local PDF secrets, extract attachments, and run parsers."""

    def __init__(
        self,
        *,
        pipeline: ClassificationEngine,
        attachment_extractor: AttachmentExtractor,
        secret_store: SecretStore,
        extraction_limits: ExtractionLimits,
    ) -> None:
        self._pipeline = pipeline
        self._attachment_extractor = attachment_extractor
        self._secret_store = secret_store
        self._extraction_limits = extraction_limits

    def execute(
        self, decoded: DecodedEmail, matched_rule: SenderRule | None
    ) -> PreparedEmailProcessing:
        """Process one message without database or network access."""
        try:
            passwords = self._password_candidates(matched_rule)
            attachments = tuple(
                self._process_attachment(item, passwords)
                for item in decoded.attachments
            )
            parser_email = ParserEmail(
                email_id=None,
                gmail_message_id=decoded.message_id,
                sender=decoded.sender,
                subject=decoded.subject,
                received_at=decoded.received_at,
                internal_date=datetime.fromtimestamp(
                    decoded.internal_date_ms / 1000, tz=UTC
                ),
                text_body=decoded.text_body,
                has_html_body=decoded.has_html_body,
                labels=frozenset(decoded.label_ids),
                institution_key=(
                    matched_rule.institution_key if matched_rule else None
                ),
                parser_hint=matched_rule.parser_name if matched_rule else None,
                attachments=tuple(_parser_attachment(item) for item in attachments),
            )
            return PreparedEmailProcessing(
                attachments=attachments,
                pipeline_result=self._pipeline.evaluate(parser_email),
            )
        except ScribeError as error:
            return PreparedEmailProcessing(
                attachments=self._fallback_attachments(decoded),
                pipeline_result=None,
                error_code=type(error).__name__,
                error_message=str(error),
                debug_trace=traceback.format_exc(limit=20),
            )
        except Exception:
            safe_error = ParserError("Transaction processing failed unexpectedly")
            return PreparedEmailProcessing(
                attachments=self._fallback_attachments(decoded),
                pipeline_result=None,
                error_code=type(safe_error).__name__,
                error_message=str(safe_error),
                debug_trace=traceback.format_exc(limit=20),
            )

    def _fallback_attachments(
        self, decoded: DecodedEmail
    ) -> tuple[ProcessedAttachmentSnapshot, ...]:
        """Preserve encrypted originals even when parsing/secret lookup fails."""
        snapshots = []
        for attachment in decoded.attachments:
            try:
                snapshots.append(self._process_attachment(attachment, ()))
            except Exception:
                snapshots.append(_pending_attachment(attachment))
        return tuple(snapshots)

    def _password_candidates(
        self, matched_rule: SenderRule | None
    ) -> tuple[tuple[str, SecretValue], ...]:
        if matched_rule is None:
            return ()
        candidates = []
        for reference in matched_rule.pdf_password_refs:
            secret = self._secret_store.get(reference)
            if secret is not None:
                candidates.append((reference, secret))
        return tuple(candidates)

    def _process_attachment(
        self,
        attachment: DecodedAttachment,
        passwords: tuple[tuple[str, SecretValue], ...],
    ) -> ProcessedAttachmentSnapshot:
        pending = _pending_attachment(attachment)
        if not _is_pdf_candidate(attachment):
            return pending
        raw = RawPdfAttachment(
            mime_part_id=attachment.mime_part_id,
            filename=attachment.filename,
            mime_type=attachment.mime_type,
            sha256=pending.attachment.sha256,
            content=attachment.content,
        )
        result = self._attachment_extractor.extract(
            raw,
            passwords,
            self._extraction_limits,
        )
        return ProcessedAttachmentSnapshot(
            attachment=pending.attachment,
            is_encrypted=result.is_encrypted,
            extraction_status=result.extraction_status,
            extracted_text=result.extracted_text,
            raw_encrypted_content=result.encrypted_content,
            extractor_name=result.extractor_name,
            extractor_version=result.extractor_version,
            warnings=tuple(item.as_json() for item in result.warnings),
            error_code=result.error_code,
            error_message=result.error_message,
            password_ref_used=result.password_ref_used,
        )


def _pending_attachment(
    attachment: DecodedAttachment,
) -> ProcessedAttachmentSnapshot:
    is_pdf = _is_pdf_candidate(attachment)
    return ProcessedAttachmentSnapshot(
        attachment=AttachmentSnapshot(
            gmail_attachment_id=attachment.gmail_attachment_id,
            mime_part_id=attachment.mime_part_id,
            filename=attachment.filename,
            mime_type=attachment.mime_type,
            size_bytes=len(attachment.content),
            sha256=hashlib.sha256(attachment.content).hexdigest(),
            is_pdf=is_pdf,
        ),
        is_encrypted=False,
        extraction_status=AttachmentStatus.PENDING,
    )


def _parser_attachment(
    snapshot: ProcessedAttachmentSnapshot,
) -> ParserAttachment:
    metadata = snapshot.attachment
    return ParserAttachment(
        attachment_id=None,
        mime_part_id=metadata.mime_part_id,
        filename=metadata.filename,
        mime_type=metadata.mime_type,
        sha256=metadata.sha256,
        encrypted=snapshot.is_encrypted,
        extraction_status=snapshot.extraction_status,
        extracted_text=snapshot.extracted_text,
        extractor_name=snapshot.extractor_name,
        extractor_version=snapshot.extractor_version,
    )


def _is_pdf_candidate(attachment: DecodedAttachment) -> bool:
    return (
        attachment.mime_type.casefold() == "application/pdf"
        or attachment.filename.casefold().endswith(".pdf")
        or attachment.content.startswith(b"%PDF-")
    )

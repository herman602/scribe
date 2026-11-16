"""Shared behavior for bank notices whose details live in encrypted PDFs."""

from __future__ import annotations

from decimal import Decimal

from app.domain.enums import AttachmentStatus, ClassificationStatus
from app.domain.parsing import ParserEmail, ParseResult, TransactionCandidate
from app.infrastructure.parsers.extractors import build_candidates, extract_amounts


def extracted_pdf_text(email: ParserEmail) -> str:
    """Return text only from attachments extracted by the local PDF adapter."""
    return "\n".join(
        attachment.extracted_text or ""
        for attachment in email.attachments
        if attachment.extraction_status is AttachmentStatus.EXTRACTED
        and attachment.extracted_text
    )


def unavailable_pdf_result(
    email: ParserEmail,
    *,
    parser_name: str,
    parser_version: str,
    institution_label: str,
) -> ParseResult:
    """Explain why a configured encrypted-PDF notice produced no transaction."""
    statuses = {attachment.extraction_status for attachment in email.attachments}
    if statuses & {
        AttachmentStatus.PASSWORD_REQUIRED,
        AttachmentStatus.WRONG_PASSWORD,
    }:
        reason = (
            f"{institution_label} notice matched; configure a local PDF password "
            "to extract transaction details"
        )
    elif extracted_pdf_text(email):
        reason = (
            f"{institution_label} PDF was extracted but no supported "
            "transaction row was found"
        )
    else:
        reason = f"{institution_label} notice matched but no extracted PDF text exists"
    return ParseResult(
        matched=False,
        classification=ClassificationStatus.UNCERTAIN,
        confidence=Decimal("0.45"),
        reason=reason,
        candidates=(),
        warnings=tuple(
            warning
            for attachment in email.attachments
            for warning in attachment.warnings
        ),
        evidence=(),
        parser_name=parser_name,
        parser_version=parser_version,
    )


def confirmed_pdf_result(
    candidates: tuple[TransactionCandidate, ...],
    *,
    parser_name: str,
    parser_version: str,
    institution_label: str,
    confidence: Decimal,
) -> ParseResult:
    """Build an auditable successful result for structured local PDF rows."""
    return ParseResult(
        matched=True,
        classification=ClassificationStatus.CONFIRMED,
        confidence=confidence,
        reason=f"{institution_label} locally decrypted PDF transaction data matched",
        candidates=candidates,
        warnings=tuple(
            warning for candidate in candidates for warning in candidate.warnings
        ),
        evidence=tuple(
            evidence for candidate in candidates for evidence in candidate.evidence
        ),
        parser_name=parser_name,
        parser_version=parser_version,
    )


def parse_pdf_notice(
    email: ParserEmail,
    *,
    parser_name: str,
    parser_version: str,
    institution_label: str,
) -> ParseResult:
    """Parse only locally extracted PDF text and explain unavailable content."""
    pdf_text = extracted_pdf_text(email)
    candidates: tuple[TransactionCandidate, ...] = ()
    if extract_amounts(pdf_text):
        candidates = build_candidates(pdf_text, confidence=Decimal("0.93"))
    if candidates:
        return confirmed_pdf_result(
            candidates,
            parser_name=parser_name,
            parser_version=parser_version,
            institution_label=institution_label,
            confidence=Decimal("0.93"),
        )
    return unavailable_pdf_result(
        email,
        parser_name=parser_name,
        parser_version=parser_version,
        institution_label=institution_label,
    )

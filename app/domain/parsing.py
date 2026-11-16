"""Framework-free parser, classification, PDF, and secret-store contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from app.domain.enums import AttachmentStatus, ClassificationStatus, TransactionType
from app.domain.types import JsonValue


class SecretValue:
    """Short-lived secret whose string representations are always redacted."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not value:
            raise ValueError("secret value cannot be empty")
        self._value = value

    def reveal(self) -> str:
        """Return the secret only at the decryption boundary."""
        return self._value

    def __repr__(self) -> str:
        return "SecretValue([REDACTED])"

    def __str__(self) -> str:
        return "[REDACTED]"


class SecretStore(Protocol):
    """Opaque-reference boundary for an OS credential store."""

    def get(self, reference: str) -> SecretValue | None: ...

    def set(self, reference: str, value: SecretValue) -> None: ...

    def delete(self, reference: str) -> None: ...


@dataclass(frozen=True)
class Evidence:
    """Bounded extraction evidence without a complete message copy."""

    field: str
    source: str
    excerpt: str
    page: int | None = None

    def as_json(self) -> dict[str, JsonValue]:
        """Return JSON-safe evidence for immutable persistence."""
        return {
            "field": self.field,
            "source": self.source,
            "excerpt": self.excerpt,
            "page": self.page,
        }


@dataclass(frozen=True)
class ParseWarning:
    """A non-fatal, user-reviewable parsing limitation."""

    code: str
    message: str

    def as_json(self) -> dict[str, JsonValue]:
        """Return JSON-safe warning data."""
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class ParserAttachment:
    """Persistence-safe attachment content provided to transaction parsers."""

    attachment_id: int | None
    mime_part_id: str
    filename: str
    mime_type: str
    sha256: str
    encrypted: bool
    extraction_status: AttachmentStatus
    extracted_text: str | None
    extractor_name: str | None
    extractor_version: str | None
    warnings: tuple[ParseWarning, ...] = ()


@dataclass(frozen=True)
class ParserEmail:
    """Immutable, fully local parser input for one Gmail message."""

    email_id: int | None
    gmail_message_id: str
    sender: str
    subject: str
    received_at: datetime | None
    internal_date: datetime
    text_body: str
    has_html_body: bool
    labels: frozenset[str]
    institution_key: str | None
    parser_hint: str | None
    attachments: tuple[ParserAttachment, ...] = ()

    @property
    def searchable_text(self) -> str:
        """Combine body and extracted attachment text for local parsing."""
        attachment_text = "\n".join(
            item.extracted_text or "" for item in self.attachments
        )
        return "\n".join(
            part for part in (self.subject, self.text_body, attachment_text) if part
        )


@dataclass(frozen=True)
class StageResult:
    """One persisted classification layer result."""

    stage_name: str
    stage_version: str
    verdict: ClassificationStatus
    confidence: Decimal
    reason_code: str
    reason: str
    extracted_fields: Mapping[str, JsonValue]
    evidence: tuple[Evidence, ...] = ()


@dataclass(frozen=True)
class TransactionCandidate:
    """One parser-produced transaction before repository reconciliation."""

    source_key: str
    transaction_type: TransactionType
    confidence: Decimal
    merchant: str | None = None
    original_merchant_text: str | None = None
    amount_minor: int | None = None
    currency: str | None = None
    card_last_four: str | None = None
    account_last_four: str | None = None
    transaction_at: datetime | None = None
    posted_at: datetime | None = None
    authorization_code: str | None = None
    evidence: tuple[Evidence, ...] = ()
    warnings: tuple[ParseWarning, ...] = ()


@dataclass(frozen=True)
class ParseResult:
    """Deterministic result from one named parser."""

    matched: bool
    classification: ClassificationStatus
    confidence: Decimal
    reason: str
    candidates: tuple[TransactionCandidate, ...]
    warnings: tuple[ParseWarning, ...]
    evidence: tuple[Evidence, ...]
    parser_name: str
    parser_version: str


class TransactionParser(Protocol):
    """Independent parser with no database access."""

    name: str
    version: str
    priority: int

    def supports(self, email: ParserEmail) -> bool: ...

    def parse(self, email: ParserEmail) -> ParseResult: ...


class ParserRegistry(Protocol):
    """Deterministically order parsers for one email."""

    def candidates_for(self, email: ParserEmail) -> Sequence[TransactionParser]: ...


@dataclass(frozen=True)
class RawPdfAttachment:
    """Ephemeral PDF bytes plus safe identifying metadata."""

    mime_part_id: str
    filename: str
    mime_type: str
    sha256: str
    content: bytes


@dataclass(frozen=True)
class ExtractionLimits:
    """Resource limits enforced by a local PDF extractor."""

    max_bytes: int
    max_pages: int
    max_text_chars: int
    max_password_attempts: int
    max_seconds: float


@dataclass(frozen=True)
class AttachmentExtractionResult:
    """Local PDF extraction result suitable for persistence."""

    mime_part_id: str
    is_pdf: bool
    is_encrypted: bool
    extraction_status: AttachmentStatus
    extracted_text: str | None
    encrypted_content: bytes | None
    extractor_name: str | None
    extractor_version: str | None
    warnings: tuple[ParseWarning, ...]
    error_code: str | None = None
    error_message: str | None = None
    password_ref_used: str | None = None


class AttachmentExtractor(Protocol):
    """Bounded local attachment extractor."""

    name: str
    version: str

    def supports(self, attachment: RawPdfAttachment) -> bool: ...

    def extract(
        self,
        attachment: RawPdfAttachment,
        password_candidates: Sequence[tuple[str, SecretValue]],
        limits: ExtractionLimits,
    ) -> AttachmentExtractionResult: ...


@dataclass(frozen=True)
class PipelineResult:
    """Final classification plus all auditable stage and parser output."""

    classification: ClassificationStatus
    confidence: Decimal
    reason: str
    parser_name: str
    parser_version: str
    stages: tuple[StageResult, ...]
    candidates: tuple[TransactionCandidate, ...]
    warnings: tuple[ParseWarning, ...]


class ClassificationEngine(Protocol):
    """Application-facing boundary for an auditable classification pipeline."""

    def evaluate(self, email: ParserEmail) -> PipelineResult: ...

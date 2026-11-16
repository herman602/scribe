"""Bounded, memory-only extraction for text-based PDF attachments."""

from __future__ import annotations

import io
import time
from collections.abc import Callable, Sequence

import pdfplumber
from pdfminer.pdfexceptions import PDFException
from pypdf import PasswordType, PdfReader
from pypdf.errors import DependencyError, PyPdfError

from app.domain.enums import AttachmentStatus
from app.domain.parsing import (
    AttachmentExtractionResult,
    ExtractionLimits,
    ParseWarning,
    RawPdfAttachment,
    SecretValue,
)

type Clock = Callable[[], float]


class LocalPdfExtractor:
    """Inspect encryption with pypdf and extract page text with pdfplumber."""

    name = "local_pdf"
    version = "1.0.0"

    def __init__(self, *, clock: Clock = time.monotonic) -> None:
        self._clock = clock

    def supports(self, attachment: RawPdfAttachment) -> bool:
        """Require PDF metadata or extension and the PDF magic bytes."""
        declared_pdf = (
            attachment.mime_type.casefold() == "application/pdf"
            or attachment.filename.casefold().endswith(".pdf")
        )
        return declared_pdf and attachment.content.startswith(b"%PDF-")

    def extract(
        self,
        attachment: RawPdfAttachment,
        password_candidates: Sequence[tuple[str, SecretValue]],
        limits: ExtractionLimits,
    ) -> AttachmentExtractionResult:
        """Extract page-aware text without persisting plaintext PDF bytes."""
        if not self.supports(attachment):
            return self._failure(
                attachment,
                status=AttachmentStatus.FAILED,
                encrypted=False,
                code="not_pdf",
                message="Attachment is not a valid PDF",
            )
        if len(attachment.content) > limits.max_bytes:
            return self._failure(
                attachment,
                status=AttachmentStatus.TOO_LARGE,
                encrypted=False,
                code="pdf_bytes_limit",
                message="PDF exceeds the configured byte limit",
            )
        started = self._clock()
        try:
            reader = PdfReader(io.BytesIO(attachment.content), strict=True)
        except (PyPdfError, OSError, ValueError) as error:
            del error
            return self._failure(
                attachment,
                status=AttachmentStatus.FAILED,
                encrypted=False,
                code="malformed_pdf",
                message="PDF structure could not be read",
            )

        encrypted = reader.is_encrypted
        password: str | None = None
        password_ref: str | None = None
        if encrypted:
            candidates = tuple(password_candidates[: limits.max_password_attempts])
            if not candidates:
                return self._failure(
                    attachment,
                    status=AttachmentStatus.PASSWORD_REQUIRED,
                    encrypted=True,
                    code="pdf_password_required",
                    message="Encrypted PDF requires a configured local password",
                    retain_encrypted=True,
                )
            decrypted = False
            try:
                for reference, secret in candidates:
                    reader = PdfReader(io.BytesIO(attachment.content), strict=True)
                    decrypted_as = reader.decrypt(secret.reveal())
                    if decrypted_as != PasswordType.NOT_DECRYPTED:
                        password = secret.reveal()
                        password_ref = reference
                        decrypted = True
                        break
                    self._check_time(started, limits)
            except DependencyError:
                return self._failure(
                    attachment,
                    status=AttachmentStatus.UNSUPPORTED_ENCRYPTION,
                    encrypted=True,
                    code="unsupported_pdf_encryption",
                    message="PDF encryption is not supported locally",
                    retain_encrypted=True,
                )
            except TimeoutError:
                return self._failure(
                    attachment,
                    status=AttachmentStatus.TOO_LARGE,
                    encrypted=True,
                    code="pdf_time_limit",
                    message="PDF extraction exceeded the configured time limit",
                    retain_encrypted=True,
                )
            except (PyPdfError, OSError, ValueError):
                decrypted = False
            if not decrypted:
                return self._failure(
                    attachment,
                    status=AttachmentStatus.WRONG_PASSWORD,
                    encrypted=True,
                    code="pdf_wrong_password",
                    message="Configured PDF passwords did not decrypt the document",
                    retain_encrypted=True,
                )

        try:
            page_count = len(reader.pages)
            if page_count > limits.max_pages:
                return self._failure(
                    attachment,
                    status=AttachmentStatus.TOO_LARGE,
                    encrypted=encrypted,
                    code="pdf_page_limit",
                    message="PDF exceeds the configured page limit",
                    retain_encrypted=encrypted,
                )
            self._check_time(started, limits)
            page_text: list[str] = []
            total_chars = 0
            with pdfplumber.open(
                io.BytesIO(attachment.content),
                password=password,
                unicode_norm="NFKC",
                strict_metadata=True,
            ) as document:
                if len(document.pages) > limits.max_pages:
                    return self._failure(
                        attachment,
                        status=AttachmentStatus.TOO_LARGE,
                        encrypted=encrypted,
                        code="pdf_page_limit",
                        message="PDF exceeds the configured page limit",
                        retain_encrypted=encrypted,
                    )
                for page_number, page in enumerate(document.pages, start=1):
                    text = (page.extract_text() or "").strip()
                    if text:
                        page_text.append(f"--- PAGE {page_number} ---\n{text}")
                        total_chars += len(text)
                    if total_chars > limits.max_text_chars:
                        return self._failure(
                            attachment,
                            status=AttachmentStatus.TOO_LARGE,
                            encrypted=encrypted,
                            code="pdf_text_limit",
                            message="PDF extracted text exceeds the configured limit",
                            retain_encrypted=encrypted,
                        )
                    self._check_time(started, limits)
        except TimeoutError:
            return self._failure(
                attachment,
                status=AttachmentStatus.TOO_LARGE,
                encrypted=encrypted,
                code="pdf_time_limit",
                message="PDF extraction exceeded the configured time limit",
                retain_encrypted=encrypted,
            )
        except (PDFException, PyPdfError, OSError, ValueError, RuntimeError):
            return self._failure(
                attachment,
                status=AttachmentStatus.FAILED,
                encrypted=encrypted,
                code="pdf_extraction_failed",
                message="PDF text extraction failed locally",
                retain_encrypted=encrypted,
            )

        if not page_text:
            return self._failure(
                attachment,
                status=AttachmentStatus.IMAGE_ONLY,
                encrypted=encrypted,
                code="pdf_image_only",
                message="PDF contains no extractable text; local OCR is disabled",
                retain_encrypted=encrypted,
            )
        return AttachmentExtractionResult(
            mime_part_id=attachment.mime_part_id,
            is_pdf=self.supports(attachment),
            is_encrypted=encrypted,
            extraction_status=AttachmentStatus.EXTRACTED,
            extracted_text="\n\n".join(page_text),
            encrypted_content=attachment.content if encrypted else None,
            extractor_name=self.name,
            extractor_version=self.version,
            warnings=(),
            password_ref_used=password_ref,
        )

    def _check_time(self, started: float, limits: ExtractionLimits) -> None:
        if self._clock() - started > limits.max_seconds:
            raise TimeoutError

    def _failure(
        self,
        attachment: RawPdfAttachment,
        *,
        status: AttachmentStatus,
        encrypted: bool,
        code: str,
        message: str,
        retain_encrypted: bool = False,
    ) -> AttachmentExtractionResult:
        return AttachmentExtractionResult(
            mime_part_id=attachment.mime_part_id,
            is_pdf=self.supports(attachment),
            is_encrypted=encrypted,
            extraction_status=status,
            extracted_text=None,
            encrypted_content=attachment.content if retain_encrypted else None,
            extractor_name=self.name,
            extractor_version=self.version,
            warnings=(ParseWarning(code=code, message=message),),
            error_code=code,
            error_message=message,
        )

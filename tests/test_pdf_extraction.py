"""Encrypted PDF extraction and local keyring adapter tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from app.domain.enums import AttachmentStatus
from app.domain.errors import SecretStoreError
from app.domain.parsing import ExtractionLimits, RawPdfAttachment, SecretValue
from app.infrastructure.attachments.pdf import LocalPdfExtractor
from app.infrastructure.attachments.secrets import KeyringSecretStore
from keyring.errors import KeyringError, PasswordDeleteError

PDF_FIXTURES = Path(__file__).parent / "fixtures" / "pdf"
PASSWORD = "fixture-only-password"


def _raw(name: str) -> RawPdfAttachment:
    content = (PDF_FIXTURES / name).read_bytes()
    return RawPdfAttachment(
        mime_part_id="1",
        filename=name,
        mime_type="application/pdf",
        sha256="fixture-sha",
        content=content,
    )


def _limits(**overrides: int | float) -> ExtractionLimits:
    values: dict[str, int | float] = {
        "max_bytes": 1024 * 1024,
        "max_pages": 10,
        "max_text_chars": 100_000,
        "max_password_attempts": 3,
        "max_seconds": 5.0,
    }
    values.update(overrides)
    return ExtractionLimits(
        max_bytes=int(values["max_bytes"]),
        max_pages=int(values["max_pages"]),
        max_text_chars=int(values["max_text_chars"]),
        max_password_attempts=int(values["max_password_attempts"]),
        max_seconds=float(values["max_seconds"]),
    )


def test_unencrypted_pdf_text_is_extracted_without_retaining_pdf_bytes() -> None:
    result = LocalPdfExtractor().extract(_raw("fubon_purchase.pdf"), (), _limits())

    assert result.extraction_status is AttachmentStatus.EXTRACTED
    assert "FIXTURE BOOK STORE" in (result.extracted_text or "")
    assert result.encrypted_content is None
    assert not result.is_encrypted


def test_encrypted_pdf_requires_password_then_decrypts_in_memory() -> None:
    extractor = LocalPdfExtractor()
    raw = _raw("fubon_purchase_encrypted.pdf")

    missing = extractor.extract(raw, (), _limits())
    wrong = extractor.extract(raw, (("wrong-ref", SecretValue("wrong")),), _limits())
    correct = extractor.extract(
        raw,
        (
            ("old-ref", SecretValue("old-password")),
            ("fixture-ref", SecretValue(PASSWORD)),
        ),
        _limits(),
    )
    capped = extractor.extract(
        raw,
        (
            ("old-ref", SecretValue("old-password")),
            ("fixture-ref", SecretValue(PASSWORD)),
        ),
        _limits(max_password_attempts=1),
    )

    assert missing.extraction_status is AttachmentStatus.PASSWORD_REQUIRED
    assert wrong.extraction_status is AttachmentStatus.WRONG_PASSWORD
    assert correct.extraction_status is AttachmentStatus.EXTRACTED
    assert correct.is_encrypted
    assert correct.encrypted_content == raw.content
    assert correct.password_ref_used == "fixture-ref"
    assert "TWD 1280" in (correct.extracted_text or "")
    assert capped.extraction_status is AttachmentStatus.WRONG_PASSWORD
    assert PASSWORD not in repr(SecretValue(PASSWORD))
    assert PASSWORD not in str(SecretValue(PASSWORD))


def test_image_only_invalid_and_oversized_pdfs_have_safe_statuses() -> None:
    extractor = LocalPdfExtractor()
    image_only = extractor.extract(_raw("image_only_notice.pdf"), (), _limits())
    invalid = RawPdfAttachment(
        mime_part_id="2",
        filename="broken.pdf",
        mime_type="application/pdf",
        sha256="broken",
        content=b"%PDF-invalid",
    )
    malformed = extractor.extract(invalid, (), _limits())
    too_large = extractor.extract(_raw("fubon_purchase.pdf"), (), _limits(max_bytes=10))
    not_pdf = extractor.extract(
        RawPdfAttachment(
            mime_part_id="3",
            filename="note.txt",
            mime_type="text/plain",
            sha256="text",
            content=b"plain",
        ),
        (),
        _limits(),
    )

    assert image_only.extraction_status is AttachmentStatus.IMAGE_ONLY
    assert malformed.error_code == "malformed_pdf"
    assert too_large.extraction_status is AttachmentStatus.TOO_LARGE
    assert not not_pdf.is_pdf
    assert not_pdf.error_code == "not_pdf"


def test_pdf_soft_time_and_page_limits_are_enforced() -> None:
    times = iter((0.0, 6.0))
    timed = LocalPdfExtractor(clock=lambda: next(times)).extract(
        _raw("fubon_purchase.pdf"), (), _limits(max_seconds=5.0)
    )
    paged = LocalPdfExtractor().extract(
        _raw("fubon_purchase.pdf"), (), _limits(max_pages=0)
    )

    assert timed.error_code == "pdf_time_limit"
    assert paged.error_code == "pdf_page_limit"


def test_keyring_adapter_never_exposes_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(
        "app.infrastructure.attachments.secrets.keyring.set_password",
        lambda service, reference, value: values.__setitem__(
            (service, reference), value
        ),
    )
    monkeypatch.setattr(
        "app.infrastructure.attachments.secrets.keyring.get_password",
        lambda service, reference: values.get((service, reference)),
    )
    monkeypatch.setattr(
        "app.infrastructure.attachments.secrets.keyring.delete_password",
        lambda service, reference: values.pop((service, reference), None),
    )
    store = KeyringSecretStore()

    store.set("opaque-ref", SecretValue(PASSWORD))
    resolved = store.get("opaque-ref")
    assert resolved is not None and resolved.reveal() == PASSWORD
    store.delete("opaque-ref")
    assert store.get("opaque-ref") is None
    with pytest.raises(ValueError):
        store.get("bad reference")


def test_keyring_errors_are_translated(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object) -> None:
        del args
        raise KeyringError

    monkeypatch.setattr(
        "app.infrastructure.attachments.secrets.keyring.get_password", fail
    )
    with pytest.raises(SecretStoreError):
        KeyringSecretStore().get("opaque-ref")

    monkeypatch.setattr(
        "app.infrastructure.attachments.secrets.keyring.delete_password",
        lambda *args: (_ for _ in ()).throw(PasswordDeleteError()),
    )
    KeyringSecretStore().delete("opaque-ref")

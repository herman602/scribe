"""Regenerate anonymized PDF parser fixtures."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import A4  # type: ignore[import-untyped]
from reportlab.pdfgen.canvas import Canvas  # type: ignore[import-untyped]

FIXTURE_PASSWORD = "fixture-only-password"
OUTPUT_DIR = Path(__file__).parent / "pdf"


def _text_pdf(lines: tuple[str, ...]) -> bytes:
    stream = BytesIO()
    canvas = Canvas(stream, pagesize=A4)
    y = 800
    for line in lines:
        canvas.drawString(72, y, line)
        y -= 24
    canvas.save()
    return stream.getvalue()


def _encrypted_pdf(content: bytes) -> bytes:
    reader = PdfReader(BytesIO(content))
    writer = PdfWriter()
    writer.append_pages_from_reader(reader)
    writer.encrypt(FIXTURE_PASSWORD, algorithm="AES-256-R5")
    stream = BytesIO()
    writer.write(stream)
    return stream.getvalue()


def main() -> None:
    """Write deterministic, synthetic PDFs without real financial data."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    notice = _text_pdf(
        (
            "FUBON PURCHASE NOTIFICATION",
            "Purchase notification",
            "Transaction time: 2026/07/25 13:45",
            "Merchant: FIXTURE BOOK STORE",
            "Amount: TWD 1280",
            "Card ending: 4455",
        )
    )
    (OUTPUT_DIR / "fubon_purchase.pdf").write_bytes(notice)
    (OUTPUT_DIR / "fubon_purchase_encrypted.pdf").write_bytes(_encrypted_pdf(notice))

    image_only = BytesIO()
    canvas = Canvas(image_only, pagesize=A4)
    canvas.rect(72, 600, 300, 120, fill=1)
    canvas.save()
    (OUTPUT_DIR / "image_only_notice.pdf").write_bytes(image_only.getvalue())


if __name__ == "__main__":
    main()

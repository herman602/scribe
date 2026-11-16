"""Hua Nan Bank encrypted-PDF transaction-notification parser."""

from __future__ import annotations

import re
from decimal import Decimal

from app.domain.parsing import ParserEmail, ParseResult, TransactionCandidate
from app.infrastructure.parsers.banks.common import (
    normalized_lines,
    parse_amount,
    parse_transaction_datetime,
    structured_candidate,
    transaction_type,
)
from app.infrastructure.parsers.banks.pdf_notice import (
    confirmed_pdf_result,
    extracted_pdf_text,
    unavailable_pdf_result,
)

_DATE = re.compile(r"^20\d{2}/\d{1,2}/\d{1,2}$")
_TIME = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")
_ROW = re.compile(
    r"^(?P<card>\d{4})\s+"
    r"(?P<amount>\d[\d,]*(?:\.\d+)?)\s+"
    r"(?P<category>\d{1,2})\s+"
    r"(?P<card_name>.+?)\s+(?:正卡|附卡)$"
)
_TABLE_HEADERS = ("卡片後四碼", "消費日", "消費金額")
_HEADER_START = "卡片後四碼"


class HncbTransactionParser:
    """Parse HNCB data only after bounded local PDF decryption."""

    name = "banks.hncb"
    version = "1.1.0"
    priority = 150

    def supports(self, email: ParserEmail) -> bool:
        """Match supported stable aliases without hard-coded senders."""
        return email.parser_hint == self.name or email.institution_key in {
            "hncb",
            "hua_nan",
        }

    def parse(self, email: ParserEmail) -> ParseResult:
        """Parse the HNCB daily-summary table from locally extracted text."""
        pdf_text = extracted_pdf_text(email)
        if not all(header in pdf_text for header in _TABLE_HEADERS):
            return self._unavailable(email)
        candidates: list[TransactionCandidate] = []
        for section in _table_sections(pdf_text):
            lines = normalized_lines(section)
            dates = tuple(line for line in lines if _DATE.fullmatch(line))
            times = tuple(line for line in lines if _TIME.fullmatch(line))
            rows = tuple(match for line in lines if (match := _ROW.fullmatch(line)))
            for index, row in enumerate(rows):
                amount = parse_amount(row.group("amount"), default_currency="TWD")
                if amount is None:
                    continue
                transaction_at = (
                    parse_transaction_datetime(
                        dates[index],
                        times[index],
                        reference=email.received_at or email.internal_date,
                    )
                    if index < len(dates) and index < len(times)
                    else None
                )
                candidates.append(
                    structured_candidate(
                        parser_name=self.name,
                        row_index=len(candidates),
                        confidence=Decimal("0.97"),
                        amount_minor=amount[0],
                        currency=amount[1],
                        transaction_type=transaction_type(pdf_text),
                        merchant=None,
                        card_last_four=row.group("card"),
                        transaction_at=transaction_at,
                        source="pdf_attachment",
                    )
                )
        if not candidates:
            return self._unavailable(email)
        return confirmed_pdf_result(
            tuple(candidates),
            parser_name=self.name,
            parser_version=self.version,
            institution_label="HNCB",
            confidence=Decimal("0.97"),
        )

    def _unavailable(self, email: ParserEmail) -> ParseResult:
        return unavailable_pdf_result(
            email,
            parser_name=self.name,
            parser_version=self.version,
            institution_label="HNCB",
        )


def _table_sections(text: str) -> tuple[str, ...]:
    return tuple(
        f"{_HEADER_START}{section}" for section in text.split(_HEADER_START)[1:]
    )

"""SinoPac encrypted-PDF transaction-notification parser."""

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

_ROW = re.compile(
    r"^(?:正卡|附卡)\s+"
    r"(?P<card>\d{4})\s+"
    r"(?:(?P<month>\d{1,2})\s*/\s*(?P<day>\d{1,2})\s+"
    r"(?P<hour>\d{1,2})\s*:\s*(?P<minute>\d{2})|日期未提供)\s+"
    r"(?P<region>[A-Za-z]{2,3})\s+"
    r"(?P<amount>(?:NT\$|TWD|USD|JPY|EUR|CNY|RMB|HKD)\s*"
    r"\d[\d,]*(?:\.\d+)?)$",
    re.IGNORECASE,
)
_TABLE_HEADERS = ("卡號末四碼", "消費日期", "消費時間", "消費金額")
_HEADER_START = "卡別"


class SinopacTransactionParser:
    """Parse SinoPac data only after bounded local PDF decryption."""

    name = "banks.sinopac"
    version = "1.1.0"
    priority = 140

    def supports(self, email: ParserEmail) -> bool:
        """Match an explicit parser hint or stable institution key."""
        return email.parser_hint == self.name or email.institution_key == "sinopac"

    def parse(self, email: ParserEmail) -> ParseResult:
        """Parse the SinoPac authorization row after local decryption."""
        pdf_text = extracted_pdf_text(email)
        if not all(header in pdf_text for header in _TABLE_HEADERS):
            return self._unavailable(email)
        candidates: list[TransactionCandidate] = []
        for section in _table_sections(pdf_text):
            for line in normalized_lines(section):
                row = _ROW.fullmatch(line)
                if row is None:
                    continue
                amount = parse_amount(row.group("amount"))
                if amount is None:
                    continue
                transaction_at = None
                if row.group("month") is not None:
                    transaction_at = parse_transaction_datetime(
                        f"{row.group('month')}/{row.group('day')}",
                        f"{row.group('hour')}:{row.group('minute')}",
                        reference=email.received_at or email.internal_date,
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
            institution_label="SinoPac",
            confidence=Decimal("0.97"),
        )

    def _unavailable(self, email: ParserEmail) -> ParseResult:
        return unavailable_pdf_result(
            email,
            parser_name=self.name,
            parser_version=self.version,
            institution_label="SinoPac",
        )


def _table_sections(text: str) -> tuple[str, ...]:
    return tuple(
        f"{_HEADER_START}{section}" for section in text.split(_HEADER_START)[1:]
    )

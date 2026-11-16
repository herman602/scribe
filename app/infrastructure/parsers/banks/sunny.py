"""Sunny Bank flattened-table transaction parser."""

from __future__ import annotations

from decimal import Decimal

from app.domain.enums import ClassificationStatus
from app.domain.parsing import ParserEmail, ParseResult, TransactionCandidate
from app.infrastructure.parsers.banks.common import (
    last_four,
    normalized_lines,
    parse_amount,
    parse_transaction_datetime,
    result_warnings,
    structured_candidate,
    transaction_type,
)

_HEADERS = ("卡別", "卡號末四碼", "授權日期", "授權時間", "消費金額", "消費國別")


class SunnyTransactionParser:
    """Parse one or more Sunny Bank table blocks without sender constants."""

    name = "banks.sunny"
    version = "1.0.0"
    priority = 110

    def supports(self, email: ParserEmail) -> bool:
        """Match an explicit parser hint or stable institution key."""
        return email.parser_hint == self.name or email.institution_key in {
            "sunny",
            "yangxin",
        }

    def parse(self, email: ParserEmail) -> ParseResult:
        """Parse flattened authorization tables and yearless MM/DD dates."""
        lines = normalized_lines(email.searchable_text)
        candidates: list[TransactionCandidate] = []
        for index in range(len(lines) - len(_HEADERS)):
            if lines[index : index + len(_HEADERS)] != _HEADERS:
                continue
            values = lines[index + len(_HEADERS) : index + len(_HEADERS) * 2]
            if len(values) != len(_HEADERS):
                continue
            amount = parse_amount(values[4])
            if amount is None:
                continue
            transaction_at = parse_transaction_datetime(
                values[2],
                values[3],
                reference=email.received_at or email.internal_date,
            )
            candidates.append(
                structured_candidate(
                    parser_name=self.name,
                    row_index=len(candidates),
                    confidence=Decimal("0.95"),
                    amount_minor=amount[0],
                    currency=amount[1],
                    transaction_type=transaction_type(email.searchable_text),
                    merchant=None,
                    card_last_four=last_four(values[1]),
                    transaction_at=transaction_at,
                )
            )
        parsed = tuple(candidates)
        if not parsed:
            return ParseResult(
                matched=False,
                classification=ClassificationStatus.UNCERTAIN,
                confidence=Decimal("0.35"),
                reason="Sunny Bank template matched but no complete authorization row",
                candidates=(),
                warnings=(),
                evidence=(),
                parser_name=self.name,
                parser_version=self.version,
            )
        return ParseResult(
            matched=True,
            classification=ClassificationStatus.CONFIRMED,
            confidence=Decimal("0.95"),
            reason="Sunny Bank authorization table matched",
            candidates=parsed,
            warnings=result_warnings(parsed),
            evidence=tuple(item for row in parsed for item in row.evidence),
            parser_name=self.name,
            parser_version=self.version,
        )

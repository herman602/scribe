"""Cathay United Bank transaction-summary parser."""

from __future__ import annotations

import re
from decimal import Decimal

from app.domain.enums import ClassificationStatus
from app.domain.parsing import ParserEmail, ParseResult
from app.infrastructure.parsers.banks.common import (
    field_value,
    last_four,
    normalized_lines,
    parse_amount,
    parse_transaction_datetime,
    result_warnings,
    structured_candidate,
    transaction_type,
)

_DATE = re.compile(r"^20\d{2}[/-]\d{1,2}[/-]\d{1,2}$")
_TIME = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")


class CathayTransactionParser:
    """Parse Cathay summary rows independently from persistence."""

    name = "banks.cathay"
    version = "1.0.0"
    priority = 120

    def supports(self, email: ParserEmail) -> bool:
        """Match an explicit parser hint or stable institution key."""
        return email.parser_hint == self.name or email.institution_key == "cathay"

    def parse(self, email: ParserEmail) -> ParseResult:
        """Pair ordered authorization timestamps with ordered amount rows."""
        lines = normalized_lines(email.searchable_text)
        times = []
        for index, line in enumerate(lines[:-1]):
            if _DATE.fullmatch(line) and _TIME.fullmatch(lines[index + 1]):
                times.append((line, lines[index + 1]))
        amount_rows = []
        for index, line in enumerate(lines[:-1]):
            amount = parse_amount(line)
            if amount is None:
                continue
            merchant = lines[index + 1]
            amount_rows.append((amount, merchant))
        card = last_four(field_value(email.searchable_text, "卡號後4碼"))
        candidates = tuple(
            structured_candidate(
                parser_name=self.name,
                row_index=index,
                confidence=Decimal("0.96"),
                amount_minor=amount[0],
                currency=amount[1],
                transaction_type=transaction_type(email.searchable_text),
                merchant=merchant,
                card_last_four=card,
                transaction_at=(
                    parse_transaction_datetime(
                        times[index][0],
                        times[index][1],
                        reference=email.received_at or email.internal_date,
                    )
                    if index < len(times)
                    else None
                ),
            )
            for index, (amount, merchant) in enumerate(amount_rows)
        )
        if not candidates:
            return ParseResult(
                matched=False,
                classification=ClassificationStatus.UNCERTAIN,
                confidence=Decimal("0.35"),
                reason="Cathay summary template matched but no transaction row",
                candidates=(),
                warnings=(),
                evidence=(),
                parser_name=self.name,
                parser_version=self.version,
            )
        return ParseResult(
            matched=True,
            classification=ClassificationStatus.CONFIRMED,
            confidence=Decimal("0.96"),
            reason="Cathay transaction-summary table matched",
            candidates=candidates,
            warnings=result_warnings(candidates),
            evidence=tuple(item for row in candidates for item in row.evidence),
            parser_name=self.name,
            parser_version=self.version,
        )

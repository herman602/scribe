"""First Bank flattened transaction-summary parser."""

from __future__ import annotations

import re
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

_DATE = re.compile(r"^20\d{2}[/-]\d{1,2}[/-]\d{1,2}$")
_TIME = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")
_CARD_ROLE = {"主卡", "附卡"}


class FirstBankTransactionParser:
    """Parse First Bank rows whose numeric amounts imply TWD."""

    name = "banks.first_bank"
    version = "1.0.0"
    priority = 130

    def supports(self, email: ParserEmail) -> bool:
        """Match supported stable aliases without hard-coded senders."""
        return email.parser_hint == self.name or email.institution_key in {
            "first",
            "first_bank",
        }

    def parse(self, email: ParserEmail) -> ParseResult:
        """Locate rows around date/time anchors in the flattened HTML table."""
        lines = normalized_lines(email.searchable_text)
        candidates: list[TransactionCandidate] = []
        for index, line in enumerate(lines):
            if (
                not _DATE.fullmatch(line)
                or index < 3
                or index + 4 >= len(lines)
                or not _TIME.fullmatch(lines[index + 1])
                or lines[index - 1] not in _CARD_ROLE
            ):
                continue
            amount = parse_amount(lines[index + 3], default_currency="TWD")
            if amount is None:
                continue
            candidates.append(
                structured_candidate(
                    parser_name=self.name,
                    row_index=len(candidates),
                    confidence=Decimal("0.96"),
                    amount_minor=amount[0],
                    currency=amount[1],
                    transaction_type=transaction_type(email.searchable_text),
                    merchant=lines[index + 4],
                    card_last_four=last_four(lines[index - 2]),
                    transaction_at=parse_transaction_datetime(
                        line,
                        lines[index + 1],
                        reference=email.received_at or email.internal_date,
                    ),
                )
            )
        parsed = tuple(candidates)
        if not parsed:
            return ParseResult(
                matched=False,
                classification=ClassificationStatus.UNCERTAIN,
                confidence=Decimal("0.35"),
                reason="First Bank summary template matched but no complete row",
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
            reason="First Bank transaction-summary table matched",
            candidates=parsed,
            warnings=result_warnings(parsed),
            evidence=tuple(item for row in parsed for item in row.evidence),
            parser_name=self.name,
            parser_version=self.version,
        )

"""Standard Chartered Taiwan bilingual daily-summary parser."""

from __future__ import annotations

import re
from decimal import Decimal

from app.domain.enums import ClassificationStatus, TransactionType
from app.domain.parsing import ParserEmail, ParseResult, TransactionCandidate
from app.infrastructure.parsers.banks.common import (
    normalized_lines,
    parse_amount,
    parse_transaction_datetime,
    result_warnings,
    structured_candidate,
)

_DAILY_SUBJECT = "渣打銀行信用卡每日消費彙總通知"
_LOGIN_MARKER = "成功登入"
_DATE_TIME = re.compile(
    r"^(?P<date>20\d{2}[/-]\d{1,2}[/-]\d{1,2})\s+"
    r"(?P<time>\d{1,2}:\d{2}(?::\d{2})?)$"
)
_LAST_FOUR = re.compile(r"(?<!\d)(\d{4})(?!\d)")
_AUTHORIZATION_CODE = re.compile(r"^[A-Z0-9]{4,20}$", re.IGNORECASE)


class StandardCharteredTransactionParser:
    """Parse each Chinese row once and ignore its duplicate English rendering."""

    name = "banks.standard_chartered"
    version = "1.0.0"
    priority = 125

    def supports(self, email: ParserEmail) -> bool:
        """Match an explicit parser hint or stable Standard Chartered aliases."""
        return email.parser_hint == self.name or email.institution_key in {
            "scb",
            "standard_chartered",
        }

    def parse(self, email: ParserEmail) -> ParseResult:
        """Extract repeated card/date/amount/authorization row groups."""
        if _LOGIN_MARKER in email.subject:
            return self._not_transaction("Standard Chartered login notification")
        if email.subject.strip() != _DAILY_SUBJECT:
            return self._uncertain(
                "Standard Chartered message is not a supported daily summary"
            )

        lines = normalized_lines(email.searchable_text)
        chinese_end = next(
            (
                index
                for index, line in enumerate(lines)
                if line.casefold() == "dear customer,"
            ),
            len(lines),
        )
        candidates: list[TransactionCandidate] = []
        for index, line in enumerate(lines[:chinese_end]):
            if (
                line != "卡號"
                or index + 7 >= chinese_end
                or lines[index + 2] != "消費日"
                or lines[index + 4] != "消費金額"
                or lines[index + 6] != "授權碼"
            ):
                continue
            amount_text = lines[index + 5]
            amount = parse_amount(amount_text)
            if amount is None:
                continue
            date_time = _DATE_TIME.fullmatch(lines[index + 3])
            transaction_at = (
                parse_transaction_datetime(
                    date_time.group("date"),
                    date_time.group("time"),
                    reference=email.received_at or email.internal_date,
                )
                if date_time is not None
                else None
            )
            authorization_value = lines[index + 7].strip()
            authorization_code = (
                authorization_value
                if _AUTHORIZATION_CODE.fullmatch(authorization_value) is not None
                else None
            )
            card_matches = _LAST_FOUR.findall(lines[index + 1])
            candidates.append(
                structured_candidate(
                    parser_name=self.name,
                    row_index=len(candidates),
                    confidence=Decimal("0.98"),
                    amount_minor=amount[0],
                    currency=amount[1],
                    transaction_type=(
                        TransactionType.REFUND
                        if self._is_negative_amount(amount_text)
                        else TransactionType.PURCHASE
                    ),
                    merchant=None,
                    card_last_four=card_matches[-1] if card_matches else None,
                    transaction_at=transaction_at,
                    authorization_code=authorization_code,
                )
            )

        parsed = tuple(candidates)
        if not parsed:
            return self._uncertain(
                "Standard Chartered daily summary matched but no complete amount row"
            )
        return ParseResult(
            matched=True,
            classification=ClassificationStatus.CONFIRMED,
            confidence=Decimal("0.98"),
            reason=(
                "Standard Chartered Chinese daily-summary rows matched; "
                "duplicate English rendering ignored"
            ),
            candidates=parsed,
            warnings=result_warnings(parsed),
            evidence=tuple(item for row in parsed for item in row.evidence),
            parser_name=self.name,
            parser_version=self.version,
        )

    @staticmethod
    def _is_negative_amount(value: str) -> bool:
        compact = value.replace(" ", "").replace(",", "")
        return "-" in compact

    def _uncertain(self, reason: str) -> ParseResult:
        return ParseResult(
            matched=False,
            classification=ClassificationStatus.UNCERTAIN,
            confidence=Decimal("0.35"),
            reason=reason,
            candidates=(),
            warnings=(),
            evidence=(),
            parser_name=self.name,
            parser_version=self.version,
        )

    def _not_transaction(self, reason: str) -> ParseResult:
        return ParseResult(
            matched=False,
            classification=ClassificationStatus.NOT_TRANSACTION,
            confidence=Decimal("0.99"),
            reason=reason,
            candidates=(),
            warnings=(),
            evidence=(),
            parser_name=self.name,
            parser_version=self.version,
        )

"""Taipei Fubon transaction-notification parser."""

from __future__ import annotations

from decimal import Decimal

from app.domain.enums import ClassificationStatus
from app.domain.parsing import ParserEmail, ParseResult
from app.infrastructure.parsers.extractors import build_candidates, extract_amounts

_TRANSACTION_MARKERS = (
    "消費通知",
    "刷卡通知",
    "消費金額",
    "退款通知",
    "退款金額",
    "purchase notification",
    "refund notification",
)
_MARKETING_MARKERS = ("優惠", "活動", "抽獎", "限時", "紅利")


class FubonTransactionParser:
    """Parse configured Fubon notices without hard-coded sender addresses."""

    name = "banks.fubon"
    version = "1.1.0"
    priority = 100

    def supports(self, email: ParserEmail) -> bool:
        """Use the rule binding/institution key, never a sender constant."""
        return email.parser_hint == self.name or email.institution_key == "fubon"

    def parse(self, email: ParserEmail) -> ParseResult:
        """Parse purchase/refund blocks from body or extracted PDF text."""
        text = email.searchable_text
        folded = text.casefold()
        transaction_signal = any(marker in folded for marker in _TRANSACTION_MARKERS)
        marketing_signal = any(marker in folded for marker in _MARKETING_MARKERS)
        if marketing_signal and not transaction_signal:
            return ParseResult(
                matched=False,
                classification=ClassificationStatus.NOT_TRANSACTION,
                confidence=Decimal("0.98"),
                reason="Fubon marketing message has no transaction-notice marker",
                candidates=(),
                warnings=(),
                evidence=(),
                parser_name=self.name,
                parser_version=self.version,
            )
        if not transaction_signal or not extract_amounts(text):
            return ParseResult(
                matched=False,
                classification=ClassificationStatus.UNCERTAIN,
                confidence=Decimal("0.35"),
                reason=(
                    "Fubon template matched but required transaction amount is absent"
                ),
                candidates=(),
                warnings=(),
                evidence=(),
                parser_name=self.name,
                parser_version=self.version,
            )
        candidates = build_candidates(text, confidence=Decimal("0.95"))
        return ParseResult(
            matched=bool(candidates),
            classification=ClassificationStatus.CONFIRMED,
            confidence=Decimal("0.95"),
            reason="Fubon transaction template and currency amount matched",
            candidates=candidates,
            warnings=tuple(
                warning for candidate in candidates for warning in candidate.warnings
            ),
            evidence=tuple(
                evidence for candidate in candidates for evidence in candidate.evidence
            ),
            parser_name=self.name,
            parser_version=self.version,
        )

"""Generic currency-qualified transaction parser."""

from __future__ import annotations

from decimal import Decimal

from app.domain.enums import ClassificationStatus, TransactionType
from app.domain.parsing import ParserEmail, ParseResult
from app.infrastructure.parsers.extractors import (
    build_candidates,
    extract_amounts,
    infer_transaction_type,
)


class GenericTransactionParser:
    """Parse common transaction notices without bank-specific assumptions."""

    name = "generic"
    version = "1.1.0"
    priority = 1000

    def supports(self, email: ParserEmail) -> bool:
        """Require both a currency amount and a transaction-type signal."""
        text = email.searchable_text
        return bool(extract_amounts(text)) and (
            infer_transaction_type(text) is not TransactionType.UNKNOWN
        )

    def parse(self, email: ParserEmail) -> ParseResult:
        """Return independent candidates for every detected amount."""
        if not self.supports(email):
            return ParseResult(
                matched=False,
                classification=ClassificationStatus.UNCERTAIN,
                confidence=Decimal("0.20"),
                reason="Generic parser did not find enough transaction signals",
                candidates=(),
                warnings=(),
                evidence=(),
                parser_name=self.name,
                parser_version=self.version,
            )
        candidates = build_candidates(
            email.searchable_text,
            confidence=Decimal("0.80"),
        )
        return ParseResult(
            matched=bool(candidates),
            classification=ClassificationStatus.LIKELY,
            confidence=Decimal("0.80"),
            reason="Currency amount and transaction signals matched generic rules",
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

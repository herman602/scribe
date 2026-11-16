"""Auditable multi-signal transaction classification pipeline."""

from __future__ import annotations

from decimal import Decimal

from app.domain.enums import ClassificationStatus
from app.domain.errors import ParserError
from app.domain.parsing import (
    Evidence,
    ParserEmail,
    ParseResult,
    ParserRegistry,
    PipelineResult,
    StageResult,
)
from app.domain.types import JsonValue
from app.infrastructure.parsers.extractors import (
    extract_amounts,
    extract_datetime,
    extract_last_four,
    extract_merchant,
)

_POSITIVE_KEYWORDS = (
    "消費",
    "刷卡",
    "交易",
    "退款",
    "退刷",
    "轉帳",
    "purchase",
    "refund",
    "transaction",
)
_MARKETING_KEYWORDS = ("優惠", "活動", "抽獎", "限時", "紅利加碼", "立即申辦")


class ClassificationPipeline:
    """Evaluate explainable signals, then bank-specific/generic parsers."""

    version = "1.0.0"

    def __init__(
        self,
        registry: ParserRegistry,
        *,
        confirmed_threshold: Decimal = Decimal("0.90"),
    ) -> None:
        if not Decimal(0) <= confirmed_threshold <= Decimal(1):
            raise ValueError("classification threshold must be between zero and one")
        self._registry = registry
        self._confirmed_threshold = confirmed_threshold

    def evaluate(self, email: ParserEmail) -> PipelineResult:
        """Return every stage plus one deterministic final decision."""
        text = email.searchable_text
        amounts = extract_amounts(text)
        card = extract_last_four(text)
        transaction_time = extract_datetime(text)
        merchant = extract_merchant(text)
        folded = text.casefold()
        positive = tuple(word for word in _POSITIVE_KEYWORDS if word in folded)
        marketing = tuple(word for word in _MARKETING_KEYWORDS if word in folded)
        stages = [
            StageResult(
                stage_name="known_sender",
                stage_version=self.version,
                verdict=(
                    ClassificationStatus.LIKELY
                    if email.institution_key
                    else ClassificationStatus.UNCERTAIN
                ),
                confidence=(
                    Decimal("0.65") if email.institution_key else Decimal("0.20")
                ),
                reason_code=(
                    "sender_rule_matched"
                    if email.institution_key
                    else "sender_rule_absent"
                ),
                reason=(
                    "Email matched a configured institution sender rule"
                    if email.institution_key
                    else "Email has no configured institution sender rule"
                ),
                extracted_fields={"institution_key": email.institution_key},
            ),
            self._signal_stage(
                "keyword_signals",
                bool(positive),
                "transaction_keywords_found",
                "No transaction keywords found",
                {"keywords": list(positive), "marketing_keywords": list(marketing)},
            ),
            self._signal_stage(
                "currency_amount",
                bool(amounts),
                "currency_amount_found",
                "No currency-qualified amount found",
                {
                    "count": len(amounts),
                    "currencies": sorted({item.currency for item in amounts}),
                },
            ),
            self._signal_stage(
                "card_last_four",
                card is not None,
                "card_last_four_found",
                "Card last four not found",
                {"card_last_four": card},
            ),
            self._signal_stage(
                "transaction_time",
                transaction_time is not None,
                "transaction_time_found",
                "Transaction time not found",
                {
                    "transaction_time": (
                        transaction_time.isoformat() if transaction_time else None
                    )
                },
            ),
            self._signal_stage(
                "merchant",
                merchant is not None,
                "merchant_found",
                "Merchant not found",
                {"merchant": merchant},
            ),
        ]

        if marketing and not positive and not amounts:
            stages.append(
                StageResult(
                    stage_name="marketing_exclusion",
                    stage_version=self.version,
                    verdict=ClassificationStatus.NOT_TRANSACTION,
                    confidence=Decimal("0.98"),
                    reason_code="marketing_without_transaction",
                    reason="Marketing signals found without transaction evidence",
                    extracted_fields={"keywords": list(marketing)},
                )
            )
            return PipelineResult(
                classification=ClassificationStatus.NOT_TRANSACTION,
                confidence=Decimal("0.98"),
                reason="Marketing message has no transaction evidence",
                parser_name="classification_pipeline",
                parser_version=self.version,
                stages=tuple(stages),
                candidates=(),
                warnings=(),
            )

        parsed = self._run_parsers(email)
        stages.append(
            StageResult(
                stage_name="parser",
                stage_version=parsed.parser_version,
                verdict=parsed.classification,
                confidence=parsed.confidence,
                reason_code=(
                    "parser_matched" if parsed.matched else "parser_not_matched"
                ),
                reason=parsed.reason,
                extracted_fields={
                    "parser_name": parsed.parser_name,
                    "candidate_count": len(parsed.candidates),
                },
                evidence=parsed.evidence,
            )
        )
        if parsed.matched:
            classification = (
                ClassificationStatus.CONFIRMED
                if parsed.confidence >= self._confirmed_threshold
                else ClassificationStatus.LIKELY
            )
            return PipelineResult(
                classification=classification,
                confidence=parsed.confidence,
                reason=parsed.reason,
                parser_name=parsed.parser_name,
                parser_version=parsed.parser_version,
                stages=tuple(stages),
                candidates=parsed.candidates,
                warnings=parsed.warnings,
            )
        likely = bool(email.institution_key and positive and amounts)
        return PipelineResult(
            classification=(
                ClassificationStatus.LIKELY if likely else parsed.classification
            ),
            confidence=Decimal("0.65") if likely else parsed.confidence,
            reason=(
                "Multiple transaction signals found but no parser candidate"
                if likely
                else parsed.reason
            ),
            parser_name=parsed.parser_name,
            parser_version=parsed.parser_version,
            stages=tuple(stages),
            candidates=(),
            warnings=parsed.warnings,
        )

    def _run_parsers(self, email: ParserEmail) -> ParseResult:
        attempted = False
        last: ParseResult | None = None
        for parser in self._registry.candidates_for(email):
            attempted = True
            try:
                result = parser.parse(email)
            except Exception as error:
                raise ParserError(f"Parser {parser.name} failed") from error
            last = result
            if result.matched:
                return result
            if result.classification is ClassificationStatus.NOT_TRANSACTION:
                return result
        if last is not None:
            return last
        return ParseResult(
            matched=False,
            classification=ClassificationStatus.UNCERTAIN,
            confidence=Decimal("0.20"),
            reason=(
                "No registered parser supports this email"
                if not attempted
                else "No parser produced a candidate"
            ),
            candidates=(),
            warnings=(),
            evidence=(),
            parser_name="none",
            parser_version=self.version,
        )

    def _signal_stage(
        self,
        name: str,
        found: bool,
        found_reason: str,
        absent_reason: str,
        fields: dict[str, object],
    ) -> StageResult:
        json_fields = {key: _json_value(value) for key, value in fields.items()}
        evidence: tuple[Evidence, ...] = ()
        if found:
            evidence = (
                Evidence(
                    field=name,
                    source="derived_signal",
                    excerpt=found_reason,
                ),
            )
        return StageResult(
            stage_name=name,
            stage_version=self.version,
            verdict=(
                ClassificationStatus.LIKELY if found else ClassificationStatus.UNCERTAIN
            ),
            confidence=Decimal("0.70") if found else Decimal("0.10"),
            reason_code=found_reason if found else f"{name}_absent",
            reason=found_reason if found else absent_reason,
            extracted_fields=json_fields,
            evidence=evidence,
        )


def _json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    raise TypeError("classification field is not JSON-safe")

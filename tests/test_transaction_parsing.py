"""Deterministic parser and classification-pipeline tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from app.domain.enums import ClassificationStatus, TransactionType
from app.domain.parsing import ParserEmail
from app.infrastructure.parsers import (
    ClassificationPipeline,
    DefaultParserRegistry,
    FubonTransactionParser,
    GenericTransactionParser,
    StandardCharteredTransactionParser,
)

FIXTURES = Path(__file__).parent / "fixtures" / "emails"


def _email(
    body: str,
    *,
    subject: str = "Fixture notice",
    institution: str | None = "fubon",
) -> ParserEmail:
    return ParserEmail(
        email_id=None,
        gmail_message_id="fixture-message",
        sender="fixture@bank.invalid",
        subject=subject,
        received_at=datetime(2026, 7, 20, tzinfo=UTC),
        internal_date=datetime(2026, 7, 20, tzinfo=UTC),
        text_body=body,
        has_html_body=False,
        labels=frozenset({"INBOX"}),
        institution_key=institution,
        parser_hint="banks.fubon" if institution == "fubon" else None,
    )


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_fubon_purchase_extracts_exact_minor_units() -> None:
    result = FubonTransactionParser().parse(_email(_fixture("fubon_purchase.txt")))

    assert result.matched
    assert result.classification is ClassificationStatus.CONFIRMED
    assert len(result.candidates) == 1
    transaction = result.candidates[0]
    assert transaction.transaction_type is TransactionType.PURCHASE
    assert transaction.amount_minor == 128_000
    assert transaction.currency == "TWD"
    assert transaction.merchant == "測試書店"
    assert transaction.card_last_four == "1234"
    assert transaction.authorization_code == "AB1234"
    assert transaction.transaction_at is not None


def test_fubon_refund_and_zero_decimal_jpy() -> None:
    result = FubonTransactionParser().parse(_email(_fixture("fubon_refund.txt")))

    transaction = result.candidates[0]
    assert transaction.transaction_type is TransactionType.REFUND
    assert transaction.amount_minor == 1299
    assert transaction.currency == "USD"


def test_fubon_missing_amount_is_uncertain() -> None:
    result = FubonTransactionParser().parse(
        _email("消費通知\n交易時間\uff1a2026/07/20 08:00\n消費店家\uff1a測試商店")
    )

    assert not result.matched
    assert result.classification is ClassificationStatus.UNCERTAIN
    assert result.candidates == ()


def test_fubon_missing_date_keeps_candidate_with_warning() -> None:
    result = FubonTransactionParser().parse(
        _email("消費通知\n\n消費店家\uff1a測試商店\n消費金額\uff1aTWD 10")
    )

    assert result.matched
    assert result.candidates[0].transaction_at is None
    assert {warning.code for warning in result.warnings} == {"missing_transaction_time"}


def test_fubon_marketing_email_is_hard_negative() -> None:
    result = FubonTransactionParser().parse(
        _email(_fixture("fubon_marketing.txt"), subject="優惠活動")
    )

    assert not result.matched
    assert result.classification is ClassificationStatus.NOT_TRANSACTION
    assert result.confidence == Decimal("0.98")


def test_html_fixture_and_encoding_replacement_are_tolerated() -> None:
    html_result = FubonTransactionParser().parse(_email(_fixture("fubon_html.html")))
    replacement_result = FubonTransactionParser().parse(
        _email(
            "消費通知\n\n交易時間\uff1a2026/07/20 10:00\n"
            "消費店家\uff1a測�商店\n消費金額\uff1aUSD 1.25"
        )
    )

    assert html_result.candidates[0].amount_minor == 39_900
    assert replacement_result.candidates[0].amount_minor == 125


def test_one_email_can_produce_multiple_independent_transactions() -> None:
    result = FubonTransactionParser().parse(_email(_fixture("fubon_multiple.txt")))

    assert len(result.candidates) == 2
    first, second = result.candidates
    assert first.source_key != second.source_key
    assert first.transaction_type is TransactionType.PURCHASE
    assert first.amount_minor == 12_000
    assert first.card_last_four == "1111"
    assert second.transaction_type is TransactionType.REFUND
    assert second.amount_minor == 1280
    assert second.currency == "JPY"
    assert second.card_last_four == "2222"


def test_generic_parser_requires_amount_and_transaction_signal() -> None:
    parser = GenericTransactionParser()

    assert not parser.supports(_email("TWD 10", institution=None))
    result = parser.parse(
        _email(
            "Purchase\n\nTransaction time: 2026/07/20 10:00\n"
            "Merchant: FIXTURE STORE\nUSD 2.50",
            institution=None,
        )
    )
    assert result.matched
    assert result.candidates[0].amount_minor == 250


def test_pipeline_persists_every_signal_and_prefers_specific_parser() -> None:
    pipeline = ClassificationPipeline(
        DefaultParserRegistry((GenericTransactionParser(), FubonTransactionParser())),
        confirmed_threshold=Decimal("0.90"),
    )

    result = pipeline.evaluate(_email(_fixture("fubon_purchase.txt")))

    assert result.classification is ClassificationStatus.CONFIRMED
    assert result.parser_name == "banks.fubon"
    assert [stage.stage_name for stage in result.stages] == [
        "known_sender",
        "keyword_signals",
        "currency_amount",
        "card_last_four",
        "transaction_time",
        "merchant",
        "parser",
    ]


def test_pipeline_prefers_standard_chartered_over_generic() -> None:
    pipeline = ClassificationPipeline(
        DefaultParserRegistry(
            (
                GenericTransactionParser(),
                StandardCharteredTransactionParser(),
            )
        )
    )
    email = _email(
        _fixture("standard_chartered_daily.txt"),
        subject="渣打銀行信用卡每日消費彙總通知",
        institution="scb",
    )

    result = pipeline.evaluate(email)

    assert result.parser_name == "banks.standard_chartered"
    assert result.classification is ClassificationStatus.CONFIRMED
    assert len(result.candidates) == 1
    assert result.candidates[0].transaction_type is TransactionType.PURCHASE


def test_pipeline_excludes_marketing_without_transaction_evidence() -> None:
    pipeline = ClassificationPipeline(
        DefaultParserRegistry((GenericTransactionParser(),))
    )

    result = pipeline.evaluate(
        _email("優惠活動\uff0c限時抽獎", subject="紅利加碼", institution=None)
    )

    assert result.classification is ClassificationStatus.NOT_TRANSACTION
    assert result.parser_name == "classification_pipeline"


def test_registry_rejects_duplicate_parser_names_and_threshold_bounds() -> None:
    with pytest.raises(ValueError, match="unique"):
        DefaultParserRegistry((GenericTransactionParser(), GenericTransactionParser()))
    with pytest.raises(ValueError, match="between"):
        ClassificationPipeline(
            DefaultParserRegistry(()), confirmed_threshold=Decimal("1.01")
        )

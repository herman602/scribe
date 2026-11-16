"""Anonymized contract tests for mailbox-derived bank templates."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from app.domain.enums import AttachmentStatus, ClassificationStatus, TransactionType
from app.domain.parsing import ParserAttachment, ParserEmail, ParseWarning
from app.infrastructure.parsers import (
    CathayTransactionParser,
    FirstBankTransactionParser,
    FubonTransactionParser,
    HncbTransactionParser,
    SinopacTransactionParser,
    StandardCharteredTransactionParser,
    SunnyTransactionParser,
)

FIXTURES = Path(__file__).parent / "fixtures" / "emails"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _email(
    body: str,
    *,
    institution: str,
    parser_name: str,
    subject: str = "匿名消費通知",
    received_at: datetime | None = None,
    attachments: tuple[ParserAttachment, ...] = (),
    has_html_body: bool = False,
) -> ParserEmail:
    received = received_at or datetime(2026, 7, 20, tzinfo=UTC)
    return ParserEmail(
        email_id=None,
        gmail_message_id="anonymous-fixture-message",
        sender="notice@bank.invalid",
        subject=subject,
        received_at=received,
        internal_date=received,
        text_body=body,
        has_html_body=has_html_body,
        labels=frozenset({"INBOX"}),
        institution_key=institution,
        parser_hint=parser_name,
        attachments=attachments,
    )


def _attachment(
    *,
    status: AttachmentStatus,
    text: str | None = None,
    warnings: tuple[ParseWarning, ...] = (),
) -> ParserAttachment:
    return ParserAttachment(
        attachment_id=1,
        mime_part_id="1",
        filename="anonymous.pdf",
        mime_type="application/pdf",
        sha256="0" * 64,
        encrypted=True,
        extraction_status=status,
        extracted_text=text,
        extractor_name="local_pdf",
        extractor_version="1.0.0",
        warnings=warnings,
    )


def test_fubon_mailbox_template_parses_roc_year_and_separate_time() -> None:
    result = FubonTransactionParser().parse(
        _email(
            _fixture("fubon_actual_template.txt"),
            institution="fubon",
            parser_name="banks.fubon",
        )
    )

    transaction = result.candidates[0]
    assert transaction.transaction_at is not None
    assert transaction.amount_minor == 128_000
    assert transaction.card_last_four == "2468"
    assert transaction.merchant == "匿名文具店"
    assert transaction.transaction_at == datetime(
        2026, 7, 22, 9, 8, 7, tzinfo=transaction.transaction_at.tzinfo
    )


def test_fubon_negative_authorization_is_a_refund_magnitude() -> None:
    """A minus sign is the direction; stored money remains non-negative."""
    result = FubonTransactionParser().parse(
        _email(
            _fixture("fubon_negative_authorization.txt"),
            institution="fubon",
            parser_name="banks.fubon",
        )
    )

    assert result.classification is ClassificationStatus.CONFIRMED
    assert len(result.candidates) == 1
    transaction = result.candidates[0]
    assert transaction.transaction_type is TransactionType.REFUND
    assert transaction.amount_minor == 26_000
    assert transaction.currency == "TWD"
    assert transaction.card_last_four == "2468"
    assert transaction.merchant == "匿名行動支付"


def test_sunny_table_parses_chinese_twd_and_yearless_date() -> None:
    result = SunnyTransactionParser().parse(
        _email(
            _fixture("sunny_purchase.txt"),
            institution="sunny",
            parser_name="banks.sunny",
        )
    )

    transaction = result.candidates[0]
    assert transaction.amount_minor == 68_000
    assert transaction.currency == "TWD"
    assert transaction.card_last_four == "1357"
    assert transaction.transaction_at is not None
    assert transaction.transaction_at.year == 2026
    assert {warning.code for warning in result.warnings} == {"missing_merchant"}


def test_sunny_yearless_date_uses_previous_year_near_new_year() -> None:
    body = _fixture("sunny_purchase.txt").replace("07/19", "12/31")
    result = SunnyTransactionParser().parse(
        _email(
            body,
            institution="yangxin",
            parser_name="banks.sunny",
            received_at=datetime(2026, 1, 2, tzinfo=UTC),
        )
    )

    assert result.candidates[0].transaction_at is not None
    assert result.candidates[0].transaction_at.year == 2025


def test_sunny_missing_amount_is_uncertain() -> None:
    body = _fixture("sunny_purchase.txt").replace("新台幣680", "金額未提供")
    result = SunnyTransactionParser().parse(
        _email(body, institution="sunny", parser_name="banks.sunny")
    )

    assert not result.matched
    assert result.classification is ClassificationStatus.UNCERTAIN


def test_sunny_support_is_configuration_driven() -> None:
    parser = SunnyTransactionParser()
    configured = _email(
        _fixture("sunny_purchase.txt"),
        institution="custom_bank",
        parser_name="banks.sunny",
    )
    unrelated = _email(
        _fixture("sunny_purchase.txt"),
        institution="custom_bank",
        parser_name="banks.other",
    )

    assert parser.supports(configured)
    assert not parser.supports(unrelated)


def test_cathay_summary_extracts_structured_row() -> None:
    result = CathayTransactionParser().parse(
        _email(
            _fixture("cathay_summary.txt"),
            institution="cathay",
            parser_name="banks.cathay",
        )
    )

    transaction = result.candidates[0]
    assert transaction.amount_minor == 123_400
    assert transaction.card_last_four == "9753"
    assert transaction.merchant == "匿名雜貨店"
    assert transaction.transaction_at is not None


def test_cathay_multiple_amount_rows_produce_independent_candidates() -> None:
    body = (
        _fixture("cathay_summary.txt")
        + """
2026/07/20
10:05
國內
NT$500
匿名書店
一般消費
測試資料
"""
    )
    result = CathayTransactionParser().parse(
        _email(body, institution="cathay", parser_name="banks.cathay")
    )

    assert [item.amount_minor for item in result.candidates] == [123_400, 50_000]
    assert result.candidates[0].source_key != result.candidates[1].source_key


def test_cathay_refund_and_missing_date_are_auditable() -> None:
    body = (
        _fixture("cathay_summary.txt")
        .replace("2026/07/19\n13:25\n", "")
        .replace("一般消費", "退款")
    )
    result = CathayTransactionParser().parse(
        _email(body, institution="cathay", parser_name="banks.cathay")
    )

    assert result.candidates[0].transaction_type is TransactionType.REFUND
    assert {warning.code for warning in result.warnings} == {"missing_transaction_time"}


def test_first_bank_numeric_amount_is_implicit_twd() -> None:
    result = FirstBankTransactionParser().parse(
        _email(
            _fixture("first_bank_summary.txt"),
            institution="first_bank",
            parser_name="banks.first_bank",
        )
    )

    transaction = result.candidates[0]
    assert transaction.amount_minor == 250_000
    assert transaction.currency == "TWD"
    assert transaction.card_last_four == "8642"
    assert transaction.merchant == "匿名餐廳"


def test_first_bank_incomplete_or_marketing_message_is_uncertain() -> None:
    result = FirstBankTransactionParser().parse(
        _email(
            "第一銀行信用卡優惠活動\uff0c沒有授權交易表格。",
            institution="first",
            parser_name="banks.first_bank",
        )
    )

    assert not result.matched
    assert result.candidates == ()


def test_standard_chartered_bilingual_summary_parses_one_twd_purchase() -> None:
    result = StandardCharteredTransactionParser().parse(
        _email(
            _fixture("standard_chartered_daily.txt"),
            institution="scb",
            parser_name="banks.standard_chartered",
            subject="渣打銀行信用卡每日消費彙總通知",
            has_html_body=True,
        )
    )

    assert result.matched
    assert result.classification is ClassificationStatus.CONFIRMED
    assert len(result.candidates) == 1
    transaction = result.candidates[0]
    assert transaction.transaction_type is TransactionType.PURCHASE
    assert transaction.amount_minor == 128_000
    assert transaction.currency == "TWD"
    assert transaction.card_last_four == "2468"
    assert transaction.authorization_code == "123456"
    assert transaction.transaction_at is not None
    assert transaction.transaction_at == datetime(
        2026,
        7,
        20,
        9,
        8,
        7,
        tzinfo=transaction.transaction_at.tzinfo,
    )
    assert {warning.code for warning in result.warnings} == {"missing_merchant"}


def test_standard_chartered_usd_refund_uses_exact_minor_units() -> None:
    body = (
        _fixture("standard_chartered_daily.txt")
        .replace("TWD 1,280", "USD -12.99")
        .replace("123456", "A1B2C3")
    )
    result = StandardCharteredTransactionParser().parse(
        _email(
            body,
            institution="standard_chartered",
            parser_name="banks.standard_chartered",
            subject="渣打銀行信用卡每日消費彙總通知",
        )
    )

    transaction = result.candidates[0]
    assert transaction.transaction_type is TransactionType.REFUND
    assert transaction.amount_minor == 1299
    assert transaction.currency == "USD"
    assert transaction.authorization_code == "A1B2C3"


def test_standard_chartered_missing_amount_is_uncertain() -> None:
    body = _fixture("standard_chartered_daily.txt").replace("TWD 1,280", "金額未提供")
    result = StandardCharteredTransactionParser().parse(
        _email(
            body,
            institution="scb",
            parser_name="banks.standard_chartered",
            subject="渣打銀行信用卡每日消費彙總通知",
        )
    )

    assert not result.matched
    assert result.classification is ClassificationStatus.UNCERTAIN


def test_standard_chartered_missing_date_keeps_transaction_for_review() -> None:
    body = _fixture("standard_chartered_daily.txt").replace(
        "2026/07/20 09:08:07", "日期未提供"
    )
    result = StandardCharteredTransactionParser().parse(
        _email(
            body,
            institution="scb",
            parser_name="banks.standard_chartered",
            subject="渣打銀行信用卡每日消費彙總通知",
        )
    )

    assert result.matched
    assert result.candidates[0].transaction_at is None
    assert {warning.code for warning in result.warnings} == {
        "missing_merchant",
        "missing_transaction_time",
    }


def test_standard_chartered_login_and_marketing_messages_do_not_match() -> None:
    parser = StandardCharteredTransactionParser()
    login = parser.parse(
        _email(
            "您已成功登入渣打網路銀行。",
            institution="scb",
            parser_name=parser.name,
            subject="渣打銀行網路銀行【成功登入】通知",
        )
    )
    marketing = parser.parse(
        _email(
            "渣打信用卡優惠活動, 不含交易資料。",
            institution="scb",
            parser_name=parser.name,
            subject="渣打信用卡優惠",
        )
    )

    assert not login.matched
    assert login.classification is ClassificationStatus.NOT_TRANSACTION
    assert not marketing.matched
    assert marketing.classification is ClassificationStatus.UNCERTAIN


def test_standard_chartered_html_encoding_anomaly_and_multiple_rows() -> None:
    first = _fixture("standard_chartered_daily.txt").replace(
        "親愛的客戶您好", "親愛的客�您好"
    )
    second_row = """
卡號
9999-XXXX-XXXX-1357
消費日
2026/07/20 10:15:00
消費金額
USD 8.50
授權碼
ZX90Q1
"""
    body = first.replace("渣打國際商業銀行　敬上", second_row)
    result = StandardCharteredTransactionParser().parse(
        _email(
            body,
            institution="scb",
            parser_name="banks.standard_chartered",
            subject="渣打銀行信用卡每日消費彙總通知",
            has_html_body=True,
        )
    )

    assert len(result.candidates) == 2
    assert [item.amount_minor for item in result.candidates] == [128_000, 850]
    assert [item.card_last_four for item in result.candidates] == ["2468", "1357"]
    assert result.candidates[0].source_key != result.candidates[1].source_key


def test_standard_chartered_support_is_configuration_driven() -> None:
    parser = StandardCharteredTransactionParser()
    hinted = _email(
        _fixture("standard_chartered_daily.txt"),
        institution="custom_bank",
        parser_name=parser.name,
    )
    unrelated = _email(
        _fixture("standard_chartered_daily.txt"),
        institution="custom_bank",
        parser_name="banks.other",
    )

    assert parser.supports(hinted)
    assert not parser.supports(unrelated)


@pytest.mark.parametrize(
    ("parser", "institution", "fixture"),
    [
        (SinopacTransactionParser(), "sinopac", "sinopac_pdf_text.txt"),
        (HncbTransactionParser(), "hncb", "hncb_pdf_text.txt"),
    ],
)
def test_encrypted_pdf_parsers_use_only_locally_extracted_text(
    parser: SinopacTransactionParser | HncbTransactionParser,
    institution: str,
    fixture: str,
) -> None:
    result = parser.parse(
        _email(
            "交易資料請見加密附件。",
            institution=institution,
            parser_name=parser.name,
            attachments=(
                _attachment(
                    status=AttachmentStatus.EXTRACTED,
                    text=_fixture(fixture),
                ),
            ),
        )
    )

    assert result.matched
    assert result.classification is ClassificationStatus.CONFIRMED
    transaction = result.candidates[0]
    assert transaction.currency == "TWD"
    assert transaction.transaction_at is not None
    assert transaction.merchant is None
    assert {warning.code for warning in result.warnings} == {"missing_merchant"}


def test_hncb_real_layout_uses_implicit_twd_and_separate_date_time() -> None:
    result = HncbTransactionParser().parse(
        _email(
            "交易資料請見加密附件。",
            institution="hncb",
            parser_name="banks.hncb",
            attachments=(
                _attachment(
                    status=AttachmentStatus.EXTRACTED,
                    text=_fixture("hncb_pdf_text.txt"),
                ),
            ),
        )
    )

    transaction = result.candidates[0]
    assert transaction.amount_minor == 121_800
    assert transaction.card_last_four == "3344"
    assert transaction.transaction_at is not None
    assert transaction.transaction_at == datetime(
        2026,
        7,
        18,
        18,
        5,
        tzinfo=transaction.transaction_at.tzinfo,
    )


def test_sinopac_real_layout_uses_year_from_received_time() -> None:
    result = SinopacTransactionParser().parse(
        _email(
            "交易資料請見加密附件。",
            institution="sinopac",
            parser_name="banks.sinopac",
            attachments=(
                _attachment(
                    status=AttachmentStatus.EXTRACTED,
                    text=_fixture("sinopac_pdf_text.txt"),
                ),
            ),
        )
    )

    transaction = result.candidates[0]
    assert transaction.amount_minor == 32_000
    assert transaction.card_last_four == "1122"
    assert transaction.transaction_at is not None
    assert transaction.transaction_at.year == 2026


@pytest.mark.parametrize(
    ("parser", "institution"),
    [
        (SinopacTransactionParser(), "sinopac"),
        (HncbTransactionParser(), "hua_nan"),
    ],
)
def test_encrypted_pdf_password_requirement_is_uncertain_not_failure(
    parser: SinopacTransactionParser | HncbTransactionParser,
    institution: str,
) -> None:
    result = parser.parse(
        _email(
            "交易資料請見加密附件。",
            institution=institution,
            parser_name=parser.name,
            attachments=(
                _attachment(
                    status=AttachmentStatus.PASSWORD_REQUIRED,
                    warnings=(
                        ParseWarning(
                            "pdf_password_required",
                            "A local PDF password is required",
                        ),
                    ),
                ),
            ),
        )
    )

    assert not result.matched
    assert result.classification is ClassificationStatus.UNCERTAIN
    assert {warning.code for warning in result.warnings} == {"pdf_password_required"}


def test_sunny_refund_encoding_and_multiple_decoded_html_tables() -> None:
    first = _fixture("sunny_purchase.txt").replace("測試信用卡", "測�信用卡")
    second = _fixture("sunny_purchase.txt").replace("新台幣680", "新臺幣120")
    result = SunnyTransactionParser().parse(
        _email(
            first + "\n退款\n" + second,
            institution="sunny",
            parser_name="banks.sunny",
            has_html_body=True,
        )
    )

    assert len(result.candidates) == 2
    assert all(
        item.transaction_type is TransactionType.REFUND for item in result.candidates
    )
    assert [item.amount_minor for item in result.candidates] == [68_000, 12_000]


def test_sunny_missing_date_keeps_amount_for_review() -> None:
    body = _fixture("sunny_purchase.txt").replace("07/19", "日期未提供")
    result = SunnyTransactionParser().parse(
        _email(body, institution="sunny", parser_name="banks.sunny")
    )

    assert result.matched
    assert result.candidates[0].transaction_at is None
    assert {warning.code for warning in result.warnings} == {
        "missing_merchant",
        "missing_transaction_time",
    }


def test_cathay_missing_amount_and_marketing_do_not_match() -> None:
    parser = CathayTransactionParser()
    no_amount = _fixture("cathay_summary.txt").replace("NT$1,234", "金額未提供")

    assert not parser.parse(
        _email(no_amount, institution="cathay", parser_name=parser.name)
    ).matched
    assert not parser.parse(
        _email(
            "國泰信用卡優惠活動\uff0c不含實際交易明細。",
            institution="cathay",
            parser_name=parser.name,
        )
    ).matched


def test_cathay_decoded_html_and_encoding_replacement_are_tolerated() -> None:
    body = _fixture("cathay_summary.txt").replace("匿名雜貨店", "匿名雜�店")
    result = CathayTransactionParser().parse(
        _email(
            body,
            institution="cathay",
            parser_name="banks.cathay",
            has_html_body=True,
        )
    )

    assert result.candidates[0].merchant == "匿名雜�店"


def test_first_bank_refund_multiple_rows_and_decoded_html() -> None:
    body = (
        _fixture("first_bank_summary.txt")
        + """
測�信用卡
2468
附卡
2026/07/20
08:10
國內
300
匿名書店
退款
"""
    )
    result = FirstBankTransactionParser().parse(
        _email(
            body,
            institution="first_bank",
            parser_name="banks.first_bank",
            has_html_body=True,
        )
    )

    assert len(result.candidates) == 2
    assert all(
        item.transaction_type is TransactionType.REFUND for item in result.candidates
    )
    assert [item.amount_minor for item in result.candidates] == [250_000, 30_000]


@pytest.mark.parametrize(
    "replacement",
    [
        ("2026/07/19", "日期未提供"),
        ("2,500", "金額未提供"),
    ],
)
def test_first_bank_missing_required_row_value_is_uncertain(
    replacement: tuple[str, str],
) -> None:
    body = _fixture("first_bank_summary.txt").replace(*replacement)
    result = FirstBankTransactionParser().parse(
        _email(body, institution="first", parser_name="banks.first_bank")
    )

    assert not result.matched
    assert result.classification is ClassificationStatus.UNCERTAIN


@pytest.mark.parametrize(
    ("parser", "institution", "fixture"),
    [
        (SinopacTransactionParser(), "sinopac", "sinopac_pdf_text.txt"),
        (HncbTransactionParser(), "hncb", "hncb_pdf_text.txt"),
    ],
)
def test_pdf_parser_refund_multiple_missing_date_and_encoding(
    parser: SinopacTransactionParser | HncbTransactionParser,
    institution: str,
    fixture: str,
) -> None:
    first = "退款\n" + (
        _fixture(fixture)
        .replace("07 / 18 12:30", "日期未提供")
        .replace("2026/07/18\n", "")
        .replace("18:05\n", "")
    )
    second = "退款\n" + _fixture(fixture).replace("匿名", "匿�")
    result = parser.parse(
        _email(
            "退款通知",
            institution=institution,
            parser_name=parser.name,
            attachments=(
                _attachment(
                    status=AttachmentStatus.EXTRACTED,
                    text=first + "\n\n" + second,
                ),
            ),
            has_html_body=True,
        )
    )

    assert len(result.candidates) == 2
    assert all(
        item.transaction_type is TransactionType.REFUND for item in result.candidates
    )
    assert result.candidates[0].transaction_at is None


@pytest.mark.parametrize(
    ("parser", "institution"),
    [
        (SinopacTransactionParser(), "sinopac"),
        (HncbTransactionParser(), "hncb"),
    ],
)
def test_pdf_parser_extracted_marketing_or_missing_amount_does_not_match(
    parser: SinopacTransactionParser | HncbTransactionParser,
    institution: str,
) -> None:
    result = parser.parse(
        _email(
            "優惠活動",
            institution=institution,
            parser_name=parser.name,
            attachments=(
                _attachment(
                    status=AttachmentStatus.EXTRACTED,
                    text="本附件不含交易金額。",
                ),
            ),
        )
    )

    assert not result.matched
    assert result.classification is ClassificationStatus.UNCERTAIN

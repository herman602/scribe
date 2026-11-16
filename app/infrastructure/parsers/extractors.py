"""Shared deterministic extraction helpers for transaction parsers."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from app.domain.enums import TransactionType
from app.domain.parsing import Evidence, ParseWarning, TransactionCandidate

_AMOUNT_PATTERN = re.compile(
    r"(?P<currency>新[台臺]幣|NT\$|NTD|TWD|USD|JPY|EUR|CNY|RMB|HKD)"
    r"\s*[:\uFF1A]?\s*(?P<sign>[+-]?)\s*"
    r"(?P<amount>\d[\d,]*(?:\.\d+)?)",
    re.IGNORECASE,
)
_CARD_PATTERN = re.compile(
    r"(?:消費卡號|卡號|信用卡|card)\s*(?:後4碼|末四碼|末4碼|尾號|ending)?"
    r"\s*[:\uFF1A#]?\s*(?:[*xX\u2022-]*)(?P<last4>[A-Za-z0-9]{4})",
    re.IGNORECASE,
)
_ACCOUNT_PATTERN = re.compile(
    r"(?:帳號|account)\s*(?:末四碼|末4碼|尾號|ending)?"
    r"\s*[:\uFF1A#]?\s*(?:[*xX\u2022-]*)(?P<last4>[A-Za-z0-9]{4})",
    re.IGNORECASE,
)
_DATE_PATTERN = re.compile(
    r"(?P<year>20\d{2})[/-](?P<month>\d{1,2})[/-](?P<day>\d{1,2})"
    r"(?:\s+|T)(?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?"
)
_SPLIT_DATE_PATTERN = re.compile(
    r"(?P<year>20\d{2})[/-](?P<month>\d{1,2})[/-](?P<day>\d{1,2})"
    r"(?:[^\d\r\n]*\r?\n[^\d\r\n]*){1,2}"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?"
)
_ROC_DATE_PATTERN = re.compile(
    r"(?P<year>\d{2,3})\s*年\s*(?P<month>\d{1,2})\s*月\s*"
    r"(?P<day>\d{1,2})\s*日(?:[^\d]{0,24})"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?"
)
_MERCHANT_PATTERN = re.compile(
    r"(?:交易內容|消費(?:店家(?:名稱)?|商店(?:名稱)?|特店)|"
    r"商店名稱|商店|特店|merchant)"
    r"\s*[:\uFF1A]\s*(?P<merchant>[^\r\n]{1,120})",
    re.IGNORECASE,
)
_AUTH_PATTERN = re.compile(
    r"(?:授權碼|authorization(?:\s+code)?)"
    r"\s*[:\uFF1A#]?\s*(?P<code>[A-Za-z0-9-]{3,30})",
    re.IGNORECASE,
)
_LONG_NUMBER = re.compile(r"(?<!\d)\d{8,}(?!\d)")
_PAGE_MARKER = re.compile(r"--- PAGE (?P<page>\d+) ---")
_MINOR_EXPONENTS = {
    "TWD": 2,
    "USD": 2,
    "JPY": 0,
    "EUR": 2,
    "CNY": 2,
    "HKD": 2,
}
_CURRENCY_ALIASES = {
    "新台幣": "TWD",
    "新臺幣": "TWD",
    "NT$": "TWD",
    "NTD": "TWD",
    "RMB": "CNY",
}


@dataclass(frozen=True)
class AmountMatch:
    currency: str
    amount_minor: int
    is_negative: bool
    start: int
    end: int
    raw: str


def extract_amounts(text: str) -> tuple[AmountMatch, ...]:
    """Extract currency-qualified, exact minor-unit amounts."""
    results = []
    for match in _AMOUNT_PATTERN.finditer(text):
        raw_currency = match.group("currency").upper()
        currency = _CURRENCY_ALIASES.get(raw_currency, raw_currency)
        exponent = _MINOR_EXPONENTS[currency]
        try:
            major = Decimal(match.group("amount").replace(",", ""))
        except InvalidOperation:
            continue
        minor = major * (Decimal(10) ** exponent)
        if minor != minor.to_integral_value() or minor < 0:
            continue
        results.append(
            AmountMatch(
                currency=currency,
                amount_minor=int(minor),
                is_negative=match.group("sign") == "-",
                start=match.start(),
                end=match.end(),
                raw=match.group(0),
            )
        )
    return tuple(results)


def extract_last_four(text: str, *, account: bool = False) -> str | None:
    pattern = _ACCOUNT_PATTERN if account else _CARD_PATTERN
    match = pattern.search(text)
    return match.group("last4") if match else None


def extract_datetime(text: str) -> datetime | None:
    """Extract an explicit timestamp and interpret it in Asia/Taipei."""
    match = _DATE_PATTERN.search(text)
    year_offset = 0
    if match is None:
        match = _SPLIT_DATE_PATTERN.search(text)
    if match is None:
        match = _ROC_DATE_PATTERN.search(text)
        year_offset = 1911
    if match is None:
        return None
    try:
        return datetime(
            int(match.group("year")) + year_offset,
            int(match.group("month")),
            int(match.group("day")),
            int(match.group("hour")),
            int(match.group("minute")),
            int(match.group("second") or 0),
            tzinfo=ZoneInfo("Asia/Taipei"),
        )
    except ValueError:
        return None


def extract_merchant(text: str) -> str | None:
    match = _MERCHANT_PATTERN.search(text)
    return match.group("merchant").strip(" .\uff0c,") if match else None


def extract_authorization_code(text: str) -> str | None:
    match = _AUTH_PATTERN.search(text)
    return match.group("code") if match else None


def infer_transaction_type(text: str) -> TransactionType:
    folded = text.casefold()
    if any(word in folded for word in ("退款", "退刷", "refund")):
        return TransactionType.REFUND
    if any(word in folded for word in ("轉帳", "transfer")):
        return TransactionType.TRANSFER
    if any(word in folded for word in ("提款", "withdrawal")):
        return TransactionType.WITHDRAWAL
    if any(word in folded for word in ("入帳", "存款", "deposit")):
        return TransactionType.DEPOSIT
    if any(word in folded for word in ("繳費", "bill payment")):
        return TransactionType.BILL_PAYMENT
    if any(word in folded for word in ("訂閱", "subscription")):
        return TransactionType.SUBSCRIPTION
    if any(word in folded for word in ("手續費", "fee")):
        return TransactionType.FEE
    if any(word in folded for word in ("消費", "刷卡", "purchase", "交易")):
        return TransactionType.PURCHASE
    return TransactionType.UNKNOWN


def build_candidates(
    text: str,
    *,
    confidence: Decimal,
    require_merchant: bool = False,
) -> tuple[TransactionCandidate, ...]:
    """Build one candidate per currency-qualified amount occurrence."""
    amounts = extract_amounts(text)
    allow_global_fallback = len(amounts) == 1
    candidates = []
    duplicate_counts: dict[str, int] = {}
    for amount in amounts:
        context = _context_for(text, amount.start, amount.end)
        card_last_four = extract_last_four(context)
        account_last_four = extract_last_four(context, account=True)
        transaction_at = extract_datetime(context)
        authorization_code = extract_authorization_code(context)
        if allow_global_fallback:
            card_last_four = card_last_four or extract_last_four(text)
            account_last_four = account_last_four or extract_last_four(
                text, account=True
            )
            transaction_at = transaction_at or extract_datetime(text)
            authorization_code = authorization_code or extract_authorization_code(text)
        transaction_type = infer_transaction_type(context)
        if transaction_type is TransactionType.UNKNOWN:
            transaction_type = infer_transaction_type(text)
        if amount.is_negative:
            transaction_type = TransactionType.REFUND
        merchant = extract_merchant(context)
        if allow_global_fallback:
            merchant = merchant or extract_merchant(text)
        if require_merchant and merchant is None:
            continue
        warnings = []
        if transaction_at is None:
            warnings.append(
                ParseWarning("missing_transaction_time", "Transaction time is absent")
            )
        if merchant is None:
            warnings.append(
                ParseWarning("missing_merchant", "Merchant could not be extracted")
            )
        source_block = " ".join(context.split()).casefold()
        occurrence = duplicate_counts.get(source_block, 0)
        duplicate_counts[source_block] = occurrence + 1
        source_key = hashlib.sha256(
            f"transaction-block|{source_block}|{occurrence}".encode()
        ).hexdigest()[:32]
        page = _page_number(text, amount.start)
        evidence = (
            Evidence(
                field="amount",
                source="pdf_attachment" if page is not None else "email_body",
                excerpt=_bounded_excerpt(context),
                page=page,
            ),
        )
        candidates.append(
            TransactionCandidate(
                source_key=source_key,
                transaction_type=transaction_type,
                confidence=confidence,
                merchant=merchant,
                original_merchant_text=merchant,
                amount_minor=amount.amount_minor,
                currency=amount.currency,
                card_last_four=card_last_four,
                account_last_four=account_last_four,
                transaction_at=transaction_at,
                authorization_code=authorization_code,
                evidence=evidence,
                warnings=tuple(warnings),
            )
        )
    return tuple(candidates)


def _context_for(text: str, start: int, end: int) -> str:
    paragraph_start = text.rfind("\n\n", 0, start)
    paragraph_end = text.find("\n\n", end)
    if paragraph_start >= 0 or paragraph_end >= 0:
        context_start = 0 if paragraph_start < 0 else paragraph_start + 2
        context_end = len(text) if paragraph_end < 0 else paragraph_end
        return text[context_start:context_end]
    line_start = text.rfind("\n", 0, start)
    context_start = 0 if line_start < 0 else line_start + 1
    line_end = text.find("\n", end)
    context_end = len(text) if line_end < 0 else line_end
    return text[context_start:context_end]


def _bounded_excerpt(value: str, limit: int = 160) -> str:
    redacted = _LONG_NUMBER.sub("[REDACTED_NUMBER]", " ".join(value.split()))
    return redacted[:limit]


def _page_number(text: str, position: int) -> int | None:
    matches = tuple(_PAGE_MARKER.finditer(text, 0, position))
    return int(matches[-1].group("page")) if matches else None

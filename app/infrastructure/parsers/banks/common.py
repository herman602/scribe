"""Small deterministic helpers shared by bank-specific parsers."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from app.domain.enums import TransactionType
from app.domain.parsing import Evidence, ParseWarning, TransactionCandidate

_TAIPEI = ZoneInfo("Asia/Taipei")
_DATE = re.compile(r"^(?P<year>20\d{2})[/-](?P<month>\d{1,2})[/-](?P<day>\d{1,2})$")
_ROC_DATE = re.compile(
    r"^(?P<year>\d{2,3})\s*年\s*(?P<month>\d{1,2})\s*月\s*"
    r"(?P<day>\d{1,2})\s*日$"
)
_MONTH_DAY = re.compile(r"^(?P<month>\d{1,2})[/-](?P<day>\d{1,2})$")
_TIME = re.compile(r"^(?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?$")
_NUMBER = re.compile(r"^-?\d[\d,]*(?:\.\d+)?$")
_LAST_FOUR = re.compile(r"(?<!\d)(\d{4})(?!\d)")
_EXPONENT = {"TWD": 2, "USD": 2, "JPY": 0, "EUR": 2, "CNY": 2, "HKD": 2}
_CURRENCY_PREFIXES = (
    ("新台幣", "TWD"),
    ("新臺幣", "TWD"),
    ("NT$", "TWD"),
    ("NTD", "TWD"),
    ("TWD", "TWD"),
    ("USD", "USD"),
    ("JPY", "JPY"),
    ("EUR", "EUR"),
    ("CNY", "CNY"),
    ("RMB", "CNY"),
    ("HKD", "HKD"),
)


def normalized_lines(text: str) -> tuple[str, ...]:
    """Return visible text lines with HTML-table whitespace removed."""
    return tuple(line.strip() for line in text.splitlines() if line.strip())


def field_value(text: str, label: str) -> str | None:
    """Read one full-width/ASCII-colon field from flattened email text."""
    match = re.search(
        rf"{re.escape(label)}\s*[\uFF1A:]\s*(?P<value>[^\r\n]+)",
        text,
        re.IGNORECASE,
    )
    return match.group("value").strip() if match else None


def last_four(value: str | None) -> str | None:
    """Extract only a four-digit card/account suffix."""
    if not value:
        return None
    match = _LAST_FOUR.search(value)
    return match.group(1) if match else None


def parse_amount(
    value: str,
    *,
    default_currency: str | None = None,
) -> tuple[int, str] | None:
    """Convert a display amount into an exact integer minor unit."""
    compact = value.strip().replace(" ", "")
    currency = default_currency
    for prefix, code in _CURRENCY_PREFIXES:
        if compact.casefold().startswith(prefix.casefold()):
            currency = code
            compact = compact[len(prefix) :]
            break
    compact = compact.removesuffix("元").replace(",", "")
    if currency not in _EXPONENT or not _NUMBER.fullmatch(compact):
        return None
    try:
        major = Decimal(compact)
    except InvalidOperation:
        return None
    minor = major.copy_abs() * (Decimal(10) ** _EXPONENT[currency])
    if minor != minor.to_integral_value():
        return None
    return int(minor), currency


def parse_transaction_datetime(
    date_value: str,
    time_value: str,
    *,
    reference: datetime,
) -> datetime | None:
    """Parse Gregorian, ROC, or yearless dates in the Taipei time zone."""
    time_match = _TIME.fullmatch(time_value.strip())
    if not time_match:
        return None
    date_text = date_value.strip()
    year: int
    date_match = _DATE.fullmatch(date_text)
    if date_match:
        year = int(date_match.group("year"))
    else:
        roc_match = _ROC_DATE.fullmatch(date_text)
        if roc_match:
            date_match = roc_match
            year = int(roc_match.group("year")) + 1911
        else:
            month_day_match = _MONTH_DAY.fullmatch(date_text)
            if not month_day_match:
                return None
            date_match = month_day_match
            local_reference = reference.astimezone(_TAIPEI)
            year = local_reference.year
    try:
        candidate = datetime(
            year,
            int(date_match.group("month")),
            int(date_match.group("day")),
            int(time_match.group("hour")),
            int(time_match.group("minute")),
            int(time_match.group("second") or 0),
            tzinfo=_TAIPEI,
        )
    except ValueError:
        return None
    if _MONTH_DAY.fullmatch(date_text):
        local_reference = reference.astimezone(_TAIPEI)
        if candidate > local_reference + timedelta(days=7):
            try:
                candidate = candidate.replace(year=candidate.year - 1)
            except ValueError:
                return None
    return candidate


def structured_candidate(
    *,
    parser_name: str,
    row_index: int,
    confidence: Decimal,
    amount_minor: int,
    currency: str,
    transaction_type: TransactionType,
    merchant: str | None,
    card_last_four: str | None,
    transaction_at: datetime | None,
    authorization_code: str | None = None,
    source: str = "email_body",
) -> TransactionCandidate:
    """Build a stable candidate without retaining a complete source message."""
    identity_parts = [
        parser_name,
        str(row_index),
        str(amount_minor),
        currency,
        merchant or "",
        card_last_four or "",
        transaction_at.isoformat() if transaction_at else "",
    ]
    if authorization_code is not None:
        identity_parts.append(authorization_code)
    identity = "|".join(identity_parts)
    source_key = hashlib.sha256(identity.encode()).hexdigest()[:32]
    warnings = []
    if transaction_at is None:
        warnings.append(
            ParseWarning("missing_transaction_time", "Transaction time is absent")
        )
    if merchant is None:
        warnings.append(
            ParseWarning("missing_merchant", "Merchant could not be extracted")
        )
    evidence = (
        Evidence(
            field="transaction_row",
            source=source,
            excerpt=f"{currency} amount; structured row {row_index + 1}",
        ),
    )
    return TransactionCandidate(
        source_key=source_key,
        transaction_type=transaction_type,
        confidence=confidence,
        merchant=merchant,
        original_merchant_text=merchant,
        amount_minor=amount_minor,
        currency=currency,
        card_last_four=card_last_four,
        transaction_at=transaction_at,
        authorization_code=authorization_code,
        evidence=evidence,
        warnings=tuple(warnings),
    )


def result_warnings(
    candidates: tuple[TransactionCandidate, ...],
) -> tuple[ParseWarning, ...]:
    """Flatten candidate warnings while retaining deterministic order."""
    return tuple(warning for candidate in candidates for warning in candidate.warnings)


def transaction_type(text: str) -> TransactionType:
    """Classify purchase/refund wording used by bank notification templates."""
    folded = text.casefold()
    if any(marker in folded for marker in ("退款", "退刷", "refund")):
        return TransactionType.REFUND
    return TransactionType.PURCHASE

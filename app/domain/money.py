"""Currency precision rules shared by local presentation adapters."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class CurrencyPrecision:
    """Storage exponent and user-facing fraction digits for one currency."""

    minor_unit_exponent: int
    display_fraction_digits: int


_DEFAULT_PRECISION = CurrencyPrecision(
    minor_unit_exponent=2,
    display_fraction_digits=2,
)

_CURRENCY_PRECISION = {
    "TWD": CurrencyPrecision(2, 0),
    "USD": CurrencyPrecision(2, 2),
    "EUR": CurrencyPrecision(2, 2),
    "CNY": CurrencyPrecision(2, 2),
    "HKD": CurrencyPrecision(2, 2),
    "JPY": CurrencyPrecision(0, 0),
    "KRW": CurrencyPrecision(0, 0),
    "VND": CurrencyPrecision(0, 0),
}


def currency_precision(currency: str | None) -> CurrencyPrecision:
    """Return explicit precision metadata, defaulting unknown codes to 2/2."""
    code = (currency or "").strip().upper()
    return _CURRENCY_PRECISION.get(code, _DEFAULT_PRECISION)


#: A plain decimal number, optionally negative and optionally grouped in
#: thousands. Scientific notation is excluded on purpose: ``1e3`` is not an
#: amount a person types into a money form, and accepting it would let a typo
#: become a thousand times the intended value.
_DISPLAY_AMOUNT = re.compile(
    r"\A(?P<sign>-?)(?P<integer>\d{1,3}(?:,\d{3})*|\d+)(?:\.(?P<fraction>\d+))?\Z"
)


def parse_display_amount(
    value: str | None,
    currency: str | None,
    *,
    allow_signed: bool = False,
    allow_zero: bool = False,
) -> int:
    """Return the exact minor units a typed major-unit amount names.

    People type ``1,280`` and ``12.99``, not ``128000`` and ``1299``, so the
    web forms accept the display form and convert here. The conversion is
    exact: ``Decimal`` scaled by the currency's storage exponent, never a
    float, so ``12.99`` can never arrive as ``1298``.

    The accepted number of decimal places is the currency's *display* precision
    rather than its storage exponent. TWD is stored with two decimals but shown
    with none, so accepting ``12.50`` would store 1250 and then render it back
    as ``TWD 13`` — a value the user could never see again. Refusing it is the
    only answer that round-trips.

    ``allow_signed`` is for observed balances alone (ADR-B066): a real account
    can be empty or overdrawn. Every spending amount stays strictly positive.

    ``allow_zero`` is for the planning figures where nothing is a real answer:
    a budget buffer of zero means no cushion was set aside, and a subscription
    priced at zero is a service that currently costs nothing. Refusing those
    would force a fake number into a plan (ADR-B093).
    """
    text = (value or "").strip()
    if not text:
        raise ValueError("an amount is required")
    match = _DISPLAY_AMOUNT.fullmatch(text)
    if match is None:
        raise ValueError(
            "an amount is written in plain digits, optionally grouped by commas"
        )
    if match.group("sign") and not allow_signed:
        raise ValueError("an amount must be positive")
    precision = currency_precision(currency)
    fraction = match.group("fraction") or ""
    if len(fraction) > precision.display_fraction_digits:
        raise ValueError(
            f"{(currency or '').strip().upper() or 'this currency'} amounts use "
            f"{precision.display_fraction_digits} decimal places"
        )
    major = Decimal(
        f"{match.group('sign')}{match.group('integer').replace(',', '')}."
        f"{fraction or '0'}"
    )
    scaled = major.scaleb(precision.minor_unit_exponent)
    if scaled != scaled.to_integral_value():  # pragma: no cover - bounded above
        raise ValueError("this amount is more precise than its currency allows")
    minor = int(scaled)
    if not allow_signed and (minor < 0 or (minor == 0 and not allow_zero)):
        raise ValueError("an amount must be a positive magnitude")
    return minor

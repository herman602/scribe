"""Currency storage and display precision tests."""

from app.domain.money import CurrencyPrecision, currency_precision


def test_currency_precision_separates_twd_storage_from_display() -> None:
    assert currency_precision("TWD") == CurrencyPrecision(
        minor_unit_exponent=2,
        display_fraction_digits=0,
    )
    assert currency_precision("USD") == CurrencyPrecision(2, 2)
    assert currency_precision("JPY") == CurrencyPrecision(0, 0)


def test_currency_precision_normalizes_codes_and_has_safe_default() -> None:
    assert currency_precision(" twd ").display_fraction_digits == 0
    assert currency_precision("XYZ") == CurrencyPrecision(2, 2)
    assert currency_precision(None) == CurrencyPrecision(2, 2)

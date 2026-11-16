"""Pure tests for the typed-amount parser the money forms depend on."""

from __future__ import annotations

import pytest
from app.domain.money import parse_display_amount


@pytest.mark.parametrize(
    ("text", "currency", "expected"),
    [
        ("1,280", "TWD", 128_000),
        ("1280", "TWD", 128_000),
        ("0", "TWD", None),
        ("12.99", "USD", 1_299),
        ("12", "USD", 1_200),
        ("1,280", "JPY", 1_280),
        ("1,234,567", "TWD", 123_456_700),
        ("  45 ", "TWD", 4_500),
    ],
)
def test_a_typed_amount_becomes_exact_minor_units(
    text: str, currency: str, expected: int | None
) -> None:
    if expected is None:
        with pytest.raises(ValueError):
            parse_display_amount(text, currency)
        return
    assert parse_display_amount(text, currency) == expected


@pytest.mark.parametrize(
    "text",
    ["1e3", "1E3", "0x10", "1,28", "12.", ".5", "abc", "", "   ", "1 280", "+12"],
)
def test_an_unusable_amount_is_refused(text: str) -> None:
    with pytest.raises(ValueError):
        parse_display_amount(text, "TWD")


def test_scientific_notation_is_never_accepted_for_any_currency() -> None:
    for currency in ("TWD", "USD", "JPY"):
        with pytest.raises(ValueError):
            parse_display_amount("1e3", currency)


@pytest.mark.parametrize(
    ("text", "currency"),
    [("12.50", "TWD"), ("12.5", "JPY"), ("12.999", "USD")],
)
def test_more_decimals_than_the_currency_shows_are_refused(
    text: str, currency: str
) -> None:
    with pytest.raises(ValueError):
        parse_display_amount(text, currency)


def test_a_spending_amount_must_be_positive() -> None:
    with pytest.raises(ValueError):
        parse_display_amount("-1", "TWD")
    with pytest.raises(ValueError):
        parse_display_amount("0", "TWD")


def test_an_observed_balance_may_be_zero_or_negative() -> None:
    assert parse_display_amount("0", "TWD", allow_signed=True) == 0
    assert parse_display_amount("-1,280", "TWD", allow_signed=True) == -128_000
    assert parse_display_amount("-12.99", "USD", allow_signed=True) == -1_299


def test_the_result_is_always_an_exact_integer() -> None:
    """A float path would make 12.99 USD land on 1298 sooner or later."""
    for cents in range(1, 100):
        text = f"1.{cents:02d}"
        assert parse_display_amount(text, "USD") == 100 + cents


def test_zero_is_a_real_planning_answer_but_never_a_spending_amount() -> None:
    """A budget buffer or a free subscription is zero; an expense never is."""
    with pytest.raises(ValueError, match="positive magnitude"):
        parse_display_amount("0", "TWD")

    assert parse_display_amount("0", "TWD", allow_zero=True) == 0

    # Allowing zero must not quietly allow a negative plan.
    with pytest.raises(ValueError, match="positive"):
        parse_display_amount("-1", "TWD", allow_zero=True)

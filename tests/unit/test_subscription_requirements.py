"""Pure cadence, remainder, effective-range, and validation rules for B4."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest
from app.domain.budget_enums import (
    SubscriptionCadence,
    SubscriptionRequirementReason,
)
from app.domain.budget_models import (
    Subscription,
    SubscriptionDraft,
    SubscriptionPricePeriodDraft,
    SubscriptionRequirement,
    SubscriptionRequirementItem,
)
from app.domain.budget_rules import (
    activity_ranges_overlap,
    cycle_month_shares,
    cycle_position,
    cycle_share_minor,
    effective_ranges_overlap,
    frozen_configuration_changes,
    month_activity_reason,
    month_start,
    price_period_covers,
)
from app.domain.errors import SubscriptionCadenceError


def _item(
    *, currency: str = "TWD", contribution_minor: int = 0
) -> SubscriptionRequirementItem:
    """Build one requirement item with anonymous, invented evidence."""
    return SubscriptionRequirementItem(
        subscription_id=1,
        name="測試影音",
        currency=currency,
        year=2026,
        month=8,
        funding_wallet_id=1,
        funding_wallet_name="測試錢包",
        budget_category_id=1,
        budget_category_name="測試訂閱",
        cadence=SubscriptionCadence.MONTHLY,
        cadence_months=1,
        cycle_position=0,
        price_period_id=1,
        cycle_price_minor=contribution_minor,
        contribution_minor=contribution_minor,
        is_active=True,
        active_from=None,
        active_to=None,
        effective_from=date(2026, 1, 1),
        effective_to=None,
        reason=SubscriptionRequirementReason.MONTHLY_PRICE,
    )


def test_a_monthly_subscription_contributes_the_whole_month_price() -> None:
    assert cycle_share_minor(amount_minor=39000, cadence_months=1, position=0) == 39000


def test_a_yearly_cycle_sums_back_to_exactly_the_cycle_price() -> None:
    shares = cycle_month_shares(amount_minor=100_000, cadence_months=12)

    assert sum(shares) == 100_000
    assert len(shares) == 12
    # 100000 = 12 * 8333 + 4, so the first four months each carry one more
    # minor unit and no month is ever rounded with a float.
    assert shares == (8334, 8334, 8334, 8334) + (8333,) * 8


def test_a_custom_cycle_of_any_length_sums_back_exactly() -> None:
    for months in (2, 3, 5, 7, 11, 120):
        for amount in (0, 1, 999, 100_000, 123_457):
            shares = cycle_month_shares(amount_minor=amount, cadence_months=months)
            assert sum(shares) == amount
            assert len(shares) == months
            assert max(shares) - min(shares) <= 1


def test_the_remainder_always_lands_on_the_earliest_cycle_positions() -> None:
    shares = cycle_month_shares(amount_minor=10, cadence_months=4)

    assert shares == (3, 3, 2, 2)


def test_a_zero_price_contributes_zero_in_every_cadence() -> None:
    assert cycle_share_minor(amount_minor=0, cadence_months=1, position=0) == 0
    assert cycle_month_shares(amount_minor=0, cadence_months=12) == (0,) * 12


def test_minor_units_follow_each_currency_without_being_converted() -> None:
    # JPY has no minor unit, so 1200 JPY is 1200 minor units; TWD and USD both
    # store cents. The calculation never converts or combines them.
    assert cycle_month_shares(amount_minor=1200, cadence_months=12) == (100,) * 12
    assert sum(cycle_month_shares(amount_minor=11999, cadence_months=12)) == 11999
    assert sum(cycle_month_shares(amount_minor=9999, cadence_months=12)) == 9999


def test_a_yearly_cycle_is_anchored_by_its_billing_month_across_years() -> None:
    positions = [
        cycle_position(
            year=year,
            month=month,
            cadence=SubscriptionCadence.YEARLY,
            cadence_months=12,
            billing_month=11,
            active_from=None,
        )
        for year, month in ((2026, 11), (2026, 12), (2027, 1), (2027, 10), (2027, 11))
    ]

    assert positions == [0, 1, 2, 11, 0]


def test_a_custom_cycle_is_anchored_by_the_month_it_became_active() -> None:
    positions = [
        cycle_position(
            year=year,
            month=month,
            cadence=SubscriptionCadence.CUSTOM,
            cadence_months=5,
            billing_month=None,
            active_from=date(2026, 3, 1),
        )
        for year, month in ((2026, 3), (2026, 7), (2026, 8), (2027, 1), (2026, 1))
    ]

    # A month before the anchor still lands on a defined position rather than
    # on a negative one, so a historical preview is never undecidable.
    assert positions == [0, 4, 0, 0, 3]


def test_a_cycle_without_its_anchor_is_refused_instead_of_guessed() -> None:
    with pytest.raises(SubscriptionCadenceError):
        cycle_position(
            year=2026,
            month=8,
            cadence=SubscriptionCadence.YEARLY,
            cadence_months=12,
            billing_month=None,
            active_from=None,
        )
    with pytest.raises(SubscriptionCadenceError):
        cycle_position(
            year=2026,
            month=8,
            cadence=SubscriptionCadence.CUSTOM,
            cadence_months=3,
            billing_month=None,
            active_from=None,
        )


def test_a_cycle_position_outside_its_own_cycle_is_refused() -> None:
    with pytest.raises(SubscriptionCadenceError):
        cycle_share_minor(amount_minor=100, cadence_months=3, position=3)
    with pytest.raises(SubscriptionCadenceError):
        cycle_share_minor(amount_minor=100, cadence_months=0, position=0)


def test_a_price_range_is_half_open_so_adjacent_ranges_never_collide() -> None:
    january = month_start(2026, 1)
    april = month_start(2026, 4)

    assert price_period_covers(
        effective_from=january, effective_to=april, selected_month=january
    )
    assert not price_period_covers(
        effective_from=january, effective_to=april, selected_month=april
    )
    assert price_period_covers(
        effective_from=april, effective_to=None, selected_month=month_start(2030, 12)
    )
    assert not effective_ranges_overlap(
        first_from=january, first_to=april, second_from=april, second_to=None
    )
    assert effective_ranges_overlap(
        first_from=january,
        first_to=april,
        second_from=month_start(2026, 3),
        second_to=None,
    )
    assert effective_ranges_overlap(
        first_from=january, first_to=None, second_from=april, second_to=None
    )


def test_activity_boundaries_are_half_open_and_carry_a_stable_reason() -> None:
    active_from = month_start(2026, 3)
    active_to = month_start(2026, 9)

    assert (
        month_activity_reason(
            active_from=active_from,
            active_to=active_to,
            selected_month=month_start(2026, 2),
        )
        is SubscriptionRequirementReason.NOT_YET_ACTIVE
    )
    assert (
        month_activity_reason(
            active_from=active_from,
            active_to=active_to,
            selected_month=month_start(2026, 3),
        )
        is None
    )
    assert (
        month_activity_reason(
            active_from=active_from,
            active_to=active_to,
            selected_month=month_start(2026, 8),
        )
        is None
    )
    assert (
        month_activity_reason(
            active_from=active_from,
            active_to=active_to,
            selected_month=month_start(2026, 9),
        )
        is SubscriptionRequirementReason.NO_LONGER_ACTIVE
    )


def test_a_requirement_refuses_to_hold_two_currencies() -> None:
    with pytest.raises(ValueError, match="exactly one currency"):
        SubscriptionRequirement(
            year=2026,
            month=8,
            currency="TWD",
            items=(_item(currency="TWD"), _item(currency="JPY")),
        )


def test_a_requirement_total_is_the_sum_of_its_own_currency_only() -> None:
    requirement = SubscriptionRequirement(
        year=2026,
        month=8,
        currency="twd",
        items=(
            _item(contribution_minor=39000),
            _item(contribution_minor=1),
        ),
    )

    assert requirement.currency == "TWD"
    assert requirement.total_minor == 39001
    assert requirement.label == "2026-08"


@pytest.mark.parametrize("amount", [1200.5, True, "1200", None])
def test_a_price_rejects_float_bool_and_text_amounts(amount: Any) -> None:
    with pytest.raises(TypeError):
        SubscriptionPricePeriodDraft(
            subscription_id=1, amount_minor=amount, effective_from=date(2026, 1, 1)
        )


def test_a_price_may_be_zero_but_never_negative() -> None:
    zero = SubscriptionPricePeriodDraft(
        subscription_id=1, amount_minor=0, effective_from=date(2026, 1, 1)
    )

    assert zero.amount_minor == 0
    with pytest.raises(ValueError, match="not be negative"):
        SubscriptionPricePeriodDraft(
            subscription_id=1, amount_minor=-1, effective_from=date(2026, 1, 1)
        )


def test_a_price_range_must_name_whole_months_and_move_forward() -> None:
    with pytest.raises(ValueError, match="first day of a month"):
        SubscriptionPricePeriodDraft(
            subscription_id=1, amount_minor=1, effective_from=date(2026, 1, 15)
        )
    with pytest.raises(ValueError, match="later than"):
        SubscriptionPricePeriodDraft(
            subscription_id=1,
            amount_minor=1,
            effective_from=date(2026, 4, 1),
            effective_to=date(2026, 4, 1),
        )
    with pytest.raises(TypeError, match="calendar date"):
        SubscriptionPricePeriodDraft(
            subscription_id=1,
            amount_minor=1,
            effective_from=datetime(2026, 1, 1),
        )


def test_a_cadence_must_match_the_interval_it_claims() -> None:
    with pytest.raises(ValueError, match="one-month cycle"):
        SubscriptionDraft(
            name="測試影音",
            budget_category_id=1,
            funding_wallet_id=1,
            currency="TWD",
            billing_cadence=SubscriptionCadence.MONTHLY,
            cadence_months=2,
        )
    with pytest.raises(ValueError, match="twelve-month cycle"):
        SubscriptionDraft(
            name="測試影音",
            budget_category_id=1,
            funding_wallet_id=1,
            currency="TWD",
            billing_cadence=SubscriptionCadence.YEARLY,
            cadence_months=6,
            billing_month=3,
        )
    with pytest.raises(ValueError, match="fixed interval"):
        SubscriptionDraft(
            name="測試影音",
            budget_category_id=1,
            funding_wallet_id=1,
            currency="TWD",
            billing_cadence=SubscriptionCadence.CUSTOM,
            cadence_months=1,
            active_from=date(2026, 1, 1),
        )
    with pytest.raises(TypeError, match="integer number of months"):
        SubscriptionDraft(
            name="測試影音",
            budget_category_id=1,
            funding_wallet_id=1,
            currency="TWD",
            billing_cadence=SubscriptionCadence.MONTHLY,
            cadence_months=True,
        )


def test_a_cycle_without_a_storable_anchor_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="billing month as anchor"):
        SubscriptionDraft(
            name="測試雲端",
            budget_category_id=1,
            funding_wallet_id=1,
            currency="TWD",
            billing_cadence=SubscriptionCadence.YEARLY,
            cadence_months=12,
        )
    with pytest.raises(ValueError, match="active-from month as anchor"):
        SubscriptionDraft(
            name="測試雲端",
            budget_category_id=1,
            funding_wallet_id=1,
            currency="TWD",
            billing_cadence=SubscriptionCadence.CUSTOM,
            cadence_months=3,
        )


def test_a_subscription_normalizes_its_currency_and_bounds_its_dates() -> None:
    draft = SubscriptionDraft(
        name="  測試影音  ",
        budget_category_id=1,
        funding_wallet_id=1,
        currency="twd",
        billing_cadence=SubscriptionCadence.MONTHLY,
        cadence_months=1,
        billing_day=15,
        active_from=date(2026, 1, 1),
    )

    assert draft.name == "測試影音"
    assert draft.currency == "TWD"
    assert draft.billing_day == 15
    with pytest.raises(ValueError, match="later than active from"):
        SubscriptionDraft(
            name="測試影音",
            budget_category_id=1,
            funding_wallet_id=1,
            currency="TWD",
            billing_cadence=SubscriptionCadence.MONTHLY,
            cadence_months=1,
            active_from=date(2026, 4, 1),
            active_to=date(2026, 1, 1),
        )


@pytest.mark.parametrize(
    ("billing_month", "billing_day"),
    [(0, None), (13, None), (None, 0), (None, 32)],
)
def test_an_out_of_range_billing_month_or_day_is_refused(
    billing_month: int | None, billing_day: int | None
) -> None:
    with pytest.raises(ValueError):
        SubscriptionDraft(
            name="測試影音",
            budget_category_id=1,
            funding_wallet_id=1,
            currency="TWD",
            billing_cadence=SubscriptionCadence.MONTHLY,
            cadence_months=1,
            billing_month=billing_month,
            billing_day=billing_day,
        )


def test_month_start_refuses_a_month_outside_the_calendar() -> None:
    assert month_start(2026, 8) == date(2026, 8, 1)
    with pytest.raises(Exception, match="yyyy-mm"):
        month_start(2026, 13)


def _subscription(
    *,
    currency: str = "TWD",
    cadence: SubscriptionCadence = SubscriptionCadence.MONTHLY,
    cadence_months: int = 1,
    billing_month: int | None = None,
    active_from: date | None = None,
) -> Subscription:
    """Build one persisted subscription record with invented values."""
    stamp = datetime(2026, 8, 9, tzinfo=UTC)
    return Subscription(
        id=1,
        name="測試影音",
        budget_category_id=1,
        funding_wallet_id=1,
        currency=currency,
        billing_cadence=cadence,
        cadence_months=cadence_months,
        billing_month=billing_month,
        billing_day=None,
        active_from=active_from,
        active_to=None,
        is_active=True,
        version=1,
        created_at=stamp,
        updated_at=stamp,
    )


def _changes(current: Subscription, **overrides: object) -> tuple[str, ...]:
    values: dict[str, object] = {
        "currency": current.currency,
        "billing_cadence": current.billing_cadence,
        "cadence_months": current.cadence_months,
        "billing_month": current.billing_month,
        "budget_category_id": current.budget_category_id,
        "funding_wallet_id": current.funding_wallet_id,
        "active_from": current.active_from,
    }
    values.update(overrides)
    return frozen_configuration_changes(current, **values)  # type: ignore[arg-type]


def test_an_unchanged_edit_touches_no_frozen_field() -> None:
    current = _subscription()

    assert _changes(current) == ()
    assert _changes(current, budget_category_id=1) == ()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("currency", "USD"),
        ("billing_cadence", SubscriptionCadence.YEARLY),
        ("cadence_months", 12),
        ("billing_month", 3),
        ("budget_category_id", 7),
        ("funding_wallet_id", 9),
    ],
)
def test_every_field_a_stored_price_is_read_through_is_named(
    field: str, value: object
) -> None:
    assert _changes(_subscription(), **{field: value}) == (field,)


def test_more_than_one_changed_field_is_reported_in_a_stable_order() -> None:
    changed = _changes(
        _subscription(), currency="USD", funding_wallet_id=9, billing_month=3
    )

    assert changed == ("currency", "billing_month", "funding_wallet_id")


def test_the_anchor_is_frozen_only_where_it_places_a_cycle() -> None:
    """`active_from` is a plain boundary until a custom cadence anchors on it."""
    monthly = _subscription(active_from=date(2026, 1, 1))
    custom = _subscription(
        cadence=SubscriptionCadence.CUSTOM,
        cadence_months=3,
        active_from=date(2026, 1, 1),
    )

    assert _changes(monthly, active_from=date(2026, 2, 1)) == ()
    assert _changes(custom, active_from=date(2026, 2, 1)) == ("active_from",)
    assert _changes(custom, active_from=date(2026, 1, 1)) == ()


@pytest.mark.parametrize(
    ("first", "second", "overlaps"),
    [
        # Adjacent: the upper bound is excluded, so no month is shared.
        ((date(2026, 1, 1), date(2026, 7, 1)), (date(2026, 7, 1), None), False),
        ((date(2026, 7, 1), None), (date(2026, 1, 1), date(2026, 7, 1)), False),
        # Sharing even one month is an overlap.
        ((date(2026, 1, 1), date(2026, 8, 1)), (date(2026, 7, 1), None), True),
        ((date(2026, 1, 1), None), (date(2026, 7, 1), date(2026, 9, 1)), True),
        # An unbounded end swallows everything after it.
        ((date(2026, 1, 1), None), (date(2030, 1, 1), None), True),
        # An unbounded start swallows everything before it.
        ((None, date(2026, 7, 1)), (date(2026, 1, 1), date(2026, 4, 1)), True),
        ((None, date(2026, 7, 1)), (date(2026, 7, 1), None), False),
        # Two fully unbounded intervals always collide.
        ((None, None), (date(2030, 1, 1), date(2030, 2, 1)), True),
        ((None, None), (None, None), True),
        # Disjoint with a gap.
        ((date(2026, 1, 1), date(2026, 4, 1)), (date(2026, 9, 1), None), False),
    ],
)
def test_activity_intervals_overlap_only_when_they_share_a_month(
    first: tuple[date | None, date | None],
    second: tuple[date | None, date | None],
    overlaps: bool,
) -> None:
    assert (
        activity_ranges_overlap(
            first_from=first[0],
            first_to=first[1],
            second_from=second[0],
            second_to=second[1],
        )
        is overlaps
    )
    # The relation is symmetric, so the order of comparison cannot matter.
    assert (
        activity_ranges_overlap(
            first_from=second[0],
            first_to=second[1],
            second_from=first[0],
            second_to=first[1],
        )
        is overlaps
    )

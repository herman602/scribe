"""Subscription register persistence, concurrency, and database defences."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import date

import pytest
from app.domain.budget_enums import (
    FinancialAccountType,
    SubscriptionCadence,
    WalletPurpose,
)
from app.domain.budget_models import (
    BudgetCategoryDraft,
    FinancialAccountDraft,
    Subscription,
    SubscriptionDraft,
    SubscriptionPricePeriodDraft,
    SubscriptionPricePeriodUpdate,
    SubscriptionUpdate,
    WalletDraft,
)
from app.domain.errors import (
    ConcurrentUpdateError,
    SubscriptionError,
    SubscriptionOverlapError,
)
from app.infrastructure.database.engine import Database
from app.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

type UnitFactory = Callable[[], SqlAlchemyUnitOfWork]

JANUARY = date(2026, 1, 1)
APRIL = date(2026, 4, 1)
JULY = date(2026, 7, 1)


@pytest.fixture
def unit_factory(migrated_database: Database) -> Iterator[UnitFactory]:
    """Return a factory of units of work over one migrated test database."""

    def factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(migrated_database.session_factory)

    yield factory


@pytest.fixture
def seeded(unit_factory: UnitFactory) -> tuple[int, int]:
    """Create one invented category and one invented funding wallet."""
    with unit_factory() as unit:
        category = unit.budget_categories.add(BudgetCategoryDraft(name="測試訂閱"))
        account = unit.financial_accounts.add(
            FinancialAccountDraft(
                institution_code="test_bank",
                display_name="測試支票帳戶",
                account_type=FinancialAccountType.CHECKING,
                currency="TWD",
            )
        )
        wallet = unit.wallets.add(
            WalletDraft(
                financial_account_id=account.id,
                name="測試訂閱錢包",
                purpose=WalletPurpose.BUDGET,
            )
        )
        unit.commit()
        return category.id, wallet.id


def _draft(
    seeded: tuple[int, int],
    *,
    name: str = "測試影音",
    cadence: SubscriptionCadence = SubscriptionCadence.MONTHLY,
    cadence_months: int = 1,
    billing_month: int | None = None,
    active_from: date | None = None,
) -> SubscriptionDraft:
    category_id, wallet_id = seeded
    return SubscriptionDraft(
        name=name,
        budget_category_id=category_id,
        funding_wallet_id=wallet_id,
        currency="TWD",
        billing_cadence=cadence,
        cadence_months=cadence_months,
        billing_month=billing_month,
        active_from=active_from,
    )


def test_a_subscription_round_trips_through_the_repository(
    unit_factory: UnitFactory, seeded: tuple[int, int]
) -> None:
    with unit_factory() as unit:
        created = unit.subscriptions.add(_draft(seeded, active_from=JANUARY))
        unit.commit()

    with unit_factory() as unit:
        found = unit.subscriptions.get(created.id)
        by_name = unit.subscriptions.list_by_name("測試影音")
        listed = unit.subscriptions.list_all()
        currencies = unit.subscriptions.list_currencies()

    assert found == created
    assert by_name == (created,)
    assert listed == (created,)
    assert currencies == ("TWD",)
    assert created.version == 1
    assert created.is_active
    assert created.active_from == JANUARY


def test_a_stale_subscription_version_is_a_conflict_not_an_overwrite(
    unit_factory: UnitFactory, seeded: tuple[int, int]
) -> None:
    with unit_factory() as unit:
        created = unit.subscriptions.add(_draft(seeded))
        unit.commit()

    def change(version: int, name: str) -> SubscriptionUpdate:
        return SubscriptionUpdate(
            subscription_id=created.id,
            expected_version=version,
            name=name,
            budget_category_id=created.budget_category_id,
            funding_wallet_id=created.funding_wallet_id,
            currency=created.currency,
            billing_cadence=created.billing_cadence,
            cadence_months=created.cadence_months,
            billing_month=created.billing_month,
            billing_day=created.billing_day,
            active_from=created.active_from,
            active_to=created.active_to,
            is_active=True,
        )

    with unit_factory() as unit:
        saved = unit.subscriptions.save(change(1, "測試影音 Plus"))
        unit.commit()

    with unit_factory() as unit, pytest.raises(ConcurrentUpdateError):
        unit.subscriptions.save(change(1, "被丟棄的名稱"))

    with unit_factory() as unit:
        current = unit.subscriptions.get(created.id)

    assert saved.version == 2
    assert current is not None
    assert current.name == "測試影音 Plus"


def test_saving_a_subscription_that_no_longer_exists_is_reported_plainly(
    unit_factory: UnitFactory, seeded: tuple[int, int]
) -> None:
    category_id, wallet_id = seeded
    with unit_factory() as unit, pytest.raises(SubscriptionError):
        unit.subscriptions.save(
            SubscriptionUpdate(
                subscription_id=9999,
                expected_version=1,
                name="測試影音",
                budget_category_id=category_id,
                funding_wallet_id=wallet_id,
                currency="TWD",
                billing_cadence=SubscriptionCadence.MONTHLY,
                cadence_months=1,
                billing_month=None,
                billing_day=None,
                active_from=None,
                active_to=None,
                is_active=True,
            )
        )


def test_a_stale_price_version_is_a_conflict_not_an_overwrite(
    unit_factory: UnitFactory, seeded: tuple[int, int]
) -> None:
    with unit_factory() as unit:
        subscription = unit.subscriptions.add(_draft(seeded))
        price = unit.subscription_prices.add(
            SubscriptionPricePeriodDraft(
                subscription_id=subscription.id,
                amount_minor=39000,
                effective_from=JANUARY,
            )
        )
        unit.commit()

    with unit_factory() as unit:
        saved = unit.subscription_prices.save(
            SubscriptionPricePeriodUpdate(
                price_period_id=price.id,
                expected_version=1,
                amount_minor=42000,
                effective_from=JANUARY,
                effective_to=None,
            )
        )
        unit.commit()

    with unit_factory() as unit, pytest.raises(ConcurrentUpdateError):
        unit.subscription_prices.save(
            SubscriptionPricePeriodUpdate(
                price_period_id=price.id,
                expected_version=1,
                amount_minor=1,
                effective_from=JANUARY,
                effective_to=None,
            )
        )

    with unit_factory() as unit:
        current = unit.subscription_prices.get(price.id)

    assert saved.version == 2
    assert current is not None
    assert current.amount_minor == 42000


def test_an_overlapping_insert_is_refused_and_writes_nothing(
    unit_factory: UnitFactory, seeded: tuple[int, int]
) -> None:
    """The trigger is the database's own line of defence, not only the service."""
    with unit_factory() as unit:
        subscription = unit.subscriptions.add(_draft(seeded))
        unit.subscription_prices.add(
            SubscriptionPricePeriodDraft(
                subscription_id=subscription.id,
                amount_minor=39000,
                effective_from=JANUARY,
                effective_to=JULY,
            )
        )
        unit.commit()

    with unit_factory() as unit, pytest.raises(SubscriptionOverlapError):
        unit.subscription_prices.add(
            SubscriptionPricePeriodDraft(
                subscription_id=subscription.id,
                amount_minor=42000,
                effective_from=APRIL,
                effective_to=None,
            )
        )

    with unit_factory() as unit:
        prices = unit.subscription_prices.list_for_subscription(subscription.id)

    assert len(prices) == 1
    assert prices[0].effective_from == JANUARY


def test_an_overlapping_update_is_refused_and_writes_nothing(
    unit_factory: UnitFactory, seeded: tuple[int, int]
) -> None:
    with unit_factory() as unit:
        subscription = unit.subscriptions.add(_draft(seeded))
        first = unit.subscription_prices.add(
            SubscriptionPricePeriodDraft(
                subscription_id=subscription.id,
                amount_minor=39000,
                effective_from=JANUARY,
                effective_to=APRIL,
            )
        )
        second = unit.subscription_prices.add(
            SubscriptionPricePeriodDraft(
                subscription_id=subscription.id,
                amount_minor=42000,
                effective_from=APRIL,
                effective_to=None,
            )
        )
        unit.commit()

    with unit_factory() as unit, pytest.raises(SubscriptionOverlapError):
        unit.subscription_prices.save(
            SubscriptionPricePeriodUpdate(
                price_period_id=second.id,
                expected_version=1,
                amount_minor=42000,
                effective_from=date(2026, 2, 1),
                effective_to=None,
            )
        )

    with unit_factory() as unit:
        prices = unit.subscription_prices.list_for_subscription(subscription.id)

    assert [item.effective_from for item in prices] == [first.effective_from, APRIL]
    assert [item.version for item in prices] == [1, 1]


def test_adjacent_half_open_ranges_are_legal(
    unit_factory: UnitFactory, seeded: tuple[int, int]
) -> None:
    with unit_factory() as unit:
        subscription = unit.subscriptions.add(_draft(seeded))
        unit.subscription_prices.add(
            SubscriptionPricePeriodDraft(
                subscription_id=subscription.id,
                amount_minor=39000,
                effective_from=JANUARY,
                effective_to=APRIL,
            )
        )
        unit.subscription_prices.add(
            SubscriptionPricePeriodDraft(
                subscription_id=subscription.id,
                amount_minor=42000,
                effective_from=APRIL,
                effective_to=None,
            )
        )
        unit.commit()

    with unit_factory() as unit:
        prices = unit.subscription_prices.list_for_subscription(subscription.id)
        clashes = unit.subscription_prices.list_overlapping(
            subscription_id=subscription.id,
            effective_from=APRIL,
            effective_to=None,
            exclude_id=prices[1].id,
        )

    assert len(prices) == 2
    assert clashes == ()


def test_two_subscriptions_may_share_the_same_effective_range(
    unit_factory: UnitFactory, seeded: tuple[int, int]
) -> None:
    with unit_factory() as unit:
        first = unit.subscriptions.add(_draft(seeded, name="測試影音"))
        second = unit.subscriptions.add(_draft(seeded, name="測試雲端"))
        for subscription in (first, second):
            unit.subscription_prices.add(
                SubscriptionPricePeriodDraft(
                    subscription_id=subscription.id,
                    amount_minor=39000,
                    effective_from=JANUARY,
                    effective_to=APRIL,
                )
            )
        unit.commit()

    with unit_factory() as unit:
        assert len(unit.subscription_prices.list_for_subscription(first.id)) == 1
        assert len(unit.subscription_prices.list_for_subscription(second.id)) == 1


def _interval_update(
    subscription: object, *, active_from: date | None, active_to: date | None
) -> SubscriptionUpdate:
    """Move one interval's activity range, keeping everything else."""
    assert isinstance(subscription, Subscription)
    return SubscriptionUpdate(
        subscription_id=subscription.id,
        expected_version=subscription.version,
        name=subscription.name,
        budget_category_id=subscription.budget_category_id,
        funding_wallet_id=subscription.funding_wallet_id,
        currency=subscription.currency,
        billing_cadence=subscription.billing_cadence,
        cadence_months=subscription.cadence_months,
        billing_month=subscription.billing_month,
        billing_day=subscription.billing_day,
        active_from=active_from,
        active_to=active_to,
        # Giving an interval an end month is what stops it, and only one
        # interval of a name may be running at a time.
        is_active=active_to is None,
    )


def test_adjacent_intervals_of_one_service_are_legal(
    unit_factory: UnitFactory, seeded: tuple[int, int]
) -> None:
    """A service may be used, stopped, and used again under one name."""
    with unit_factory() as unit:
        first = unit.subscriptions.add(_draft(seeded, active_from=JANUARY))
        unit.subscriptions.save(
            _interval_update(first, active_from=JANUARY, active_to=JULY)
        )
        unit.subscriptions.add(_draft(seeded, active_from=JULY))
        unit.commit()

    with unit_factory() as unit:
        rows = unit.subscriptions.list_by_name("測試影音")

    assert len(rows) == 2
    assert [item.active_from for item in rows] == [JANUARY, JULY]
    assert [item.active_to for item in rows] == [JULY, None]


def test_the_database_refuses_a_second_overlapping_interval(
    unit_factory: UnitFactory, seeded: tuple[int, int]
) -> None:
    """The trigger is the database's own defence, not only the service."""
    with unit_factory() as unit:
        first = unit.subscriptions.add(_draft(seeded, active_from=JANUARY))
        unit.subscriptions.save(
            _interval_update(first, active_from=JANUARY, active_to=date(2026, 11, 1))
        )
        unit.commit()

    with (
        unit_factory() as unit,
        pytest.raises(SubscriptionOverlapError, match="intervals must not overlap"),
    ):
        unit.subscriptions.add(_draft(seeded, active_from=JULY))

    with unit_factory() as unit:
        assert len(unit.subscriptions.list_by_name("測試影音")) == 1


def test_the_database_refuses_extending_an_interval_into_the_next(
    unit_factory: UnitFactory, seeded: tuple[int, int]
) -> None:
    with unit_factory() as unit:
        first = unit.subscriptions.add(_draft(seeded, active_from=JANUARY))
        unit.subscriptions.save(
            _interval_update(first, active_from=JANUARY, active_to=APRIL)
        )
        second = unit.subscriptions.add(_draft(seeded, active_from=APRIL))
        unit.commit()

    with (
        unit_factory() as unit,
        pytest.raises(SubscriptionOverlapError, match="intervals must not overlap"),
    ):
        stopped = unit.subscriptions.get(first.id)
        unit.subscriptions.save(
            _interval_update(stopped, active_from=JANUARY, active_to=JULY)
        )

    with unit_factory() as unit:
        rows = unit.subscriptions.list_by_name("測試影音")

    assert [item.active_to for item in rows] == [APRIL, None]
    assert rows[1].id == second.id


def test_an_open_ended_interval_blocks_every_later_one(
    unit_factory: UnitFactory, seeded: tuple[int, int]
) -> None:
    """A missing end month means still running, so nothing may follow it."""
    with unit_factory() as unit:
        unit.subscriptions.add(_draft(seeded, active_from=JANUARY))
        unit.commit()

    with (
        unit_factory() as unit,
        pytest.raises(SubscriptionOverlapError, match="intervals must not overlap"),
    ):
        unit.subscriptions.add(_draft(seeded, active_from=date(2030, 1, 1)))

    with unit_factory() as unit:
        assert len(unit.subscriptions.list_by_name("測試影音")) == 1


def test_an_interval_without_a_start_month_blocks_every_earlier_one(
    unit_factory: UnitFactory, seeded: tuple[int, int]
) -> None:
    """A missing start month means since always, so nothing may precede it."""
    with unit_factory() as unit:
        first = unit.subscriptions.add(_draft(seeded))
        unit.subscriptions.save(
            _interval_update(first, active_from=None, active_to=JULY)
        )
        unit.commit()

    with (
        unit_factory() as unit,
        pytest.raises(SubscriptionOverlapError, match="intervals must not overlap"),
    ):
        unit.subscriptions.add(_draft(seeded, active_from=JANUARY))

    with unit_factory() as unit:
        rows = unit.subscriptions.list_by_name("測試影音")
        after_adjacent = unit.subscriptions.add(_draft(seeded, active_from=JULY))
        unit.commit()

    assert len(rows) == 1
    assert after_adjacent.active_from == JULY


def test_two_different_services_never_constrain_each_other(
    unit_factory: UnitFactory, seeded: tuple[int, int]
) -> None:
    with unit_factory() as unit:
        unit.subscriptions.add(_draft(seeded, name="測試影音", active_from=JANUARY))
        unit.subscriptions.add(_draft(seeded, name="測試雲端", active_from=JANUARY))
        unit.commit()

    with unit_factory() as unit:
        assert len(unit.subscriptions.list_all()) == 2


def test_subscription_foreign_keys_all_restrict(migrated_database: Database) -> None:
    with migrated_database.engine.connect() as connection:
        deletes = {
            (table, row[3]): row[6]
            for table in ("subscriptions", "subscription_price_periods")
            for row in connection.exec_driver_sql(
                f"PRAGMA foreign_key_list({table})"
            ).all()
        }

    assert deletes == {
        ("subscriptions", "funding_wallet_id"): "RESTRICT",
        ("subscriptions", "budget_category_id"): "RESTRICT",
        ("subscription_price_periods", "subscription_id"): "RESTRICT",
    }


def test_a_referenced_category_or_wallet_cannot_be_deleted(
    migrated_database: Database, unit_factory: UnitFactory, seeded: tuple[int, int]
) -> None:
    with unit_factory() as unit:
        unit.subscriptions.add(_draft(seeded))
        unit.commit()
    category_id, wallet_id = seeded

    for table, row_id in (
        ("budget_categories", category_id),
        ("wallets", wallet_id),
    ):
        with (
            migrated_database.engine.begin() as connection,
            pytest.raises(IntegrityError, match="FOREIGN KEY"),
        ):
            connection.execute(
                text(f"DELETE FROM {table} WHERE id = :id"), {"id": row_id}
            )


_INSERT_SUBSCRIPTION = (
    "INSERT INTO subscriptions (name, budget_category_id, funding_wallet_id,"
    " currency, billing_cadence, cadence_months, billing_month, billing_day,"
    " active_from, active_to, is_active, version, created_at, updated_at)"
    " VALUES (?, ?, ?, 'TWD', ?, ?, ?, ?, ?, ?, ?, 1, '2026-08-09', '2026-08-09')"
)


@pytest.mark.parametrize(
    (
        "label",
        "cadence",
        "months",
        "billing_month",
        "billing_day",
        "start",
        "end",
        "active",
    ),
    [
        ("monthly cannot span two months", "monthly", 2, None, None, None, None, 1),
        ("yearly must span twelve", "yearly", 6, 3, None, None, None, 1),
        ("custom cannot be one month", "custom", 1, None, None, "2026-01-01", None, 1),
        (
            "custom cannot exceed the cap",
            "custom",
            121,
            None,
            None,
            "2026-01-01",
            None,
            1,
        ),
        ("an unknown cadence", "weekly", 1, None, None, None, None, 1),
        ("yearly without its anchor", "yearly", 12, None, None, None, None, 1),
        ("custom without its anchor", "custom", 3, None, None, None, None, 1),
        ("a month outside the year", "monthly", 1, 13, None, None, None, 1),
        ("a day outside the month", "monthly", 1, None, 32, None, None, 1),
        ("a mid-month activity start", "monthly", 1, None, None, "2026-01-15", None, 1),
        ("a malformed activity start", "monthly", 1, None, None, "nonsense", None, 1),
        (
            "an end before its start",
            "monthly",
            1,
            None,
            None,
            "2026-04-01",
            "2026-01-01",
            1,
        ),
        ("deactivating with no end month", "monthly", 1, None, None, None, None, 0),
    ],
)
def test_the_database_refuses_an_invalid_subscription_shape(
    migrated_database: Database,
    seeded: tuple[int, int],
    label: str,
    cadence: str,
    months: int,
    billing_month: int | None,
    billing_day: int | None,
    start: str | None,
    end: str | None,
    active: int,
) -> None:
    category_id, wallet_id = seeded

    with (
        migrated_database.engine.begin() as connection,
        pytest.raises(IntegrityError, match="CHECK constraint"),
    ):
        connection.exec_driver_sql(
            _INSERT_SUBSCRIPTION,
            (
                label,
                category_id,
                wallet_id,
                cadence,
                months,
                billing_month,
                billing_day,
                start,
                end,
                active,
            ),
        )


def test_the_database_refuses_an_invalid_price_period(
    migrated_database: Database, unit_factory: UnitFactory, seeded: tuple[int, int]
) -> None:
    with unit_factory() as unit:
        subscription = unit.subscriptions.add(_draft(seeded))
        unit.commit()
    insert = (
        "INSERT INTO subscription_price_periods (subscription_id, amount_minor,"
        " effective_from, effective_to, version, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, 1, '2026-08-09', '2026-08-09')"
    )

    for amount, start, end in (
        (-1, "2026-01-01", None),
        (100, "2026-01-15", None),
        (100, "nonsense", None),
        (100, "2026-01-01", "2026-01-01"),
        (100, "2026-04-01", "2026-01-01"),
        (100, "2026-01-01", "2026-04-15"),
    ):
        with (
            migrated_database.engine.begin() as connection,
            pytest.raises(IntegrityError, match="CHECK constraint"),
        ):
            connection.exec_driver_sql(insert, (subscription.id, amount, start, end))


def test_the_database_refuses_two_prices_starting_in_the_same_month(
    migrated_database: Database, unit_factory: UnitFactory, seeded: tuple[int, int]
) -> None:
    with unit_factory() as unit:
        subscription = unit.subscriptions.add(_draft(seeded))
        unit.subscription_prices.add(
            SubscriptionPricePeriodDraft(
                subscription_id=subscription.id,
                amount_minor=39000,
                effective_from=JANUARY,
                effective_to=APRIL,
            )
        )
        unit.commit()

    with (
        migrated_database.engine.begin() as connection,
        pytest.raises(IntegrityError),
    ):
        connection.exec_driver_sql(
            "INSERT INTO subscription_price_periods (subscription_id, amount_minor,"
            " effective_from, effective_to, version, created_at, updated_at)"
            " VALUES (?, 1, '2026-01-01', '2026-02-01', 1, '2026-08-09','2026-08-09')",
            (subscription.id,),
        )

"""Subscription management and monthly requirement calculation end to end."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

import pytest
from app.application.queries.subscriptions import SubscriptionQueries
from app.application.services.budget_planning import (
    AddCategoryRequest,
    ManageBudgetPlan,
    UpdateCategoryRequest,
)
from app.application.services.ledger import (
    AddWalletRequest,
    LedgerSetupService,
    OpenFinancialAccountRequest,
)
from app.application.services.subscriptions import (
    AddPricePeriodRequest,
    AddSubscriptionRequest,
    CalculateSubscriptionRequirement,
    DeactivateSubscriptionRequest,
    ManageSubscriptions,
    ResumeSubscriptionRequest,
    UpdatePricePeriodRequest,
    UpdateSubscriptionRequest,
)
from app.domain.budget_enums import (
    FinancialAccountType,
    SubscriptionCadence,
    SubscriptionRequirementReason,
)
from app.domain.budget_models import Subscription
from app.domain.errors import (
    LedgerConfigurationError,
    SubscriptionError,
    SubscriptionOverlapError,
)
from app.infrastructure.database.engine import Database
from app.infrastructure.database.subscription_queries import (
    SqlAlchemySubscriptionReadRepository,
)
from app.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork

type UnitFactory = Callable[[], SqlAlchemyUnitOfWork]


class Workspace:
    """One migrated database with the services B4 exercises."""

    def __init__(self, database: Database) -> None:
        def factory() -> SqlAlchemyUnitOfWork:
            return SqlAlchemyUnitOfWork(database.session_factory)

        read_repository = SqlAlchemySubscriptionReadRepository(database.session_factory)
        self.factory = factory
        self.setup = LedgerSetupService(factory)
        self.plan = ManageBudgetPlan(factory)
        self.subscriptions = ManageSubscriptions(factory)
        self.requirement = CalculateSubscriptionRequirement(read_repository)
        self.register = SubscriptionQueries(read_repository)
        self.wallets: dict[str, int] = {}
        self.categories: dict[str, int] = {}

    def open_account(self, *, name: str, currency: str, wallet: str) -> int:
        """Open one invented asset account and add one named wallet to it."""
        account = self.setup.open_account(
            OpenFinancialAccountRequest(
                institution_code="test_bank",
                display_name=name,
                account_type=FinancialAccountType.CHECKING,
                currency=currency,
            )
        )
        added = self.setup.add_wallet(
            AddWalletRequest(financial_account_id=account.account.id, name=wallet)
        )
        self.wallets[wallet] = added.id
        return account.account.id

    def add_category(self, name: str) -> int:
        """Create one invented spending purpose."""
        category = self.plan.add_category(AddCategoryRequest(name=name))
        self.categories[name] = category.id
        return category.id


@pytest.fixture
def workspace(migrated_database: Database) -> Workspace:
    """Seed one TWD account, one wallet, and one category."""
    space = Workspace(migrated_database)
    space.open_account(name="測試支票帳戶", currency="TWD", wallet="測試訂閱錢包")
    space.add_category("測試訂閱")
    return space


def _add(
    workspace: Workspace,
    *,
    name: str = "測試影音",
    cadence: SubscriptionCadence = SubscriptionCadence.MONTHLY,
    cadence_months: int = 1,
    billing_month: int | None = None,
    active_from: date | None = None,
    active_to: date | None = None,
    currency: str = "TWD",
    wallet: str = "測試訂閱錢包",
    category: str = "測試訂閱",
) -> int:
    subscription = workspace.subscriptions.add(
        AddSubscriptionRequest(
            name=name,
            budget_category_id=workspace.categories[category],
            funding_wallet_id=workspace.wallets[wallet],
            currency=currency,
            billing_cadence=cadence,
            cadence_months=cadence_months,
            billing_month=billing_month,
            active_from=active_from,
            active_to=active_to,
        )
    )
    return subscription.id


def test_a_monthly_subscription_requires_its_price_every_month(
    workspace: Workspace,
) -> None:
    subscription_id = _add(workspace)
    workspace.subscriptions.add_price(
        AddPricePeriodRequest(
            subscription_id=subscription_id,
            amount_minor=39000,
            effective_from=date(2026, 1, 1),
        )
    )

    august = workspace.requirement.execute(year=2026, month=8, currency="TWD")

    assert august.total_minor == 39000
    assert len(august.items) == 1
    item = august.items[0]
    assert item.contribution_minor == 39000
    assert item.cycle_price_minor == 39000
    assert item.cycle_position == 0
    assert item.reason is SubscriptionRequirementReason.MONTHLY_PRICE
    assert item.funding_wallet_name == "測試訂閱錢包"
    assert item.budget_category_name == "測試訂閱"
    assert item.price_period_id is not None
    assert item.label == "2026-08"


def test_a_yearly_subscription_spreads_its_cycle_price_exactly(
    workspace: Workspace,
) -> None:
    subscription_id = _add(
        workspace,
        cadence=SubscriptionCadence.YEARLY,
        cadence_months=12,
        billing_month=3,
    )
    workspace.subscriptions.add_price(
        AddPricePeriodRequest(
            subscription_id=subscription_id,
            amount_minor=100_000,
            effective_from=date(2026, 1, 1),
        )
    )

    months = [
        workspace.requirement.execute(year=2026, month=month, currency="TWD")
        for month in range(3, 13)
    ] + [
        workspace.requirement.execute(year=2027, month=month, currency="TWD")
        for month in (1, 2)
    ]

    assert sum(item.total_minor for item in months) == 100_000
    assert [item.total_minor for item in months[:4]] == [8334, 8334, 8334, 8334]
    assert months[4].total_minor == 8333
    assert months[0].items[0].reason is SubscriptionRequirementReason.CYCLE_SHARE
    assert months[0].items[0].cycle_position == 0
    assert months[11].items[0].cycle_position == 11


def test_a_custom_cycle_spreads_from_the_month_it_became_active(
    workspace: Workspace,
) -> None:
    subscription_id = _add(
        workspace,
        name="測試雲端",
        cadence=SubscriptionCadence.CUSTOM,
        cadence_months=5,
        active_from=date(2026, 3, 1),
    )
    workspace.subscriptions.add_price(
        AddPricePeriodRequest(
            subscription_id=subscription_id,
            amount_minor=1_003,
            effective_from=date(2026, 3, 1),
        )
    )

    cycle = [
        workspace.requirement.execute(year=2026, month=month, currency="TWD")
        for month in range(3, 8)
    ]

    assert [item.total_minor for item in cycle] == [201, 201, 201, 200, 200]
    assert sum(item.total_minor for item in cycle) == 1_003


def test_a_historical_month_uses_the_price_effective_then(
    workspace: Workspace,
) -> None:
    subscription_id = _add(workspace)
    workspace.subscriptions.add_price(
        AddPricePeriodRequest(
            subscription_id=subscription_id,
            amount_minor=39000,
            effective_from=date(2026, 1, 1),
            effective_to=date(2026, 7, 1),
        )
    )
    workspace.subscriptions.add_price(
        AddPricePeriodRequest(
            subscription_id=subscription_id,
            amount_minor=49000,
            effective_from=date(2026, 7, 1),
        )
    )

    june = workspace.requirement.execute(year=2026, month=6, currency="TWD")
    july = workspace.requirement.execute(year=2026, month=7, currency="TWD")
    december = workspace.requirement.execute(year=2025, month=12, currency="TWD")

    assert june.total_minor == 39000
    assert july.total_minor == 49000
    assert december.total_minor == 0
    assert december.items[0].reason is SubscriptionRequirementReason.NO_PRICE_FOR_MONTH
    assert december.items[0].price_period_id is None


def test_changing_a_price_leaves_the_cycle_anchor_where_it_was(
    workspace: Workspace,
) -> None:
    subscription_id = _add(
        workspace,
        cadence=SubscriptionCadence.YEARLY,
        cadence_months=12,
        billing_month=3,
    )
    workspace.subscriptions.add_price(
        AddPricePeriodRequest(
            subscription_id=subscription_id,
            amount_minor=120_000,
            effective_from=date(2026, 1, 1),
            effective_to=date(2026, 8, 1),
        )
    )
    workspace.subscriptions.add_price(
        AddPricePeriodRequest(
            subscription_id=subscription_id,
            amount_minor=240_000,
            effective_from=date(2026, 8, 1),
        )
    )

    march = workspace.requirement.execute(year=2026, month=3, currency="TWD")
    august = workspace.requirement.execute(year=2026, month=8, currency="TWD")

    assert march.items[0].cycle_position == 0
    assert august.items[0].cycle_position == 5
    assert march.total_minor == 10_000
    assert august.total_minor == 20_000


def test_a_zero_price_stays_visible_and_contributes_nothing(
    workspace: Workspace,
) -> None:
    subscription_id = _add(workspace, name="測試贈閱")
    workspace.subscriptions.add_price(
        AddPricePeriodRequest(
            subscription_id=subscription_id,
            amount_minor=0,
            effective_from=date(2026, 1, 1),
        )
    )

    august = workspace.requirement.execute(year=2026, month=8, currency="TWD")

    assert august.total_minor == 0
    assert len(august.items) == 1
    assert august.items[0].reason is SubscriptionRequirementReason.ZERO_PRICE
    assert august.items[0].cycle_price_minor == 0
    assert not august.items[0].contributes


def test_deactivating_keeps_history_and_zeroes_only_later_months(
    workspace: Workspace,
) -> None:
    subscription_id = _add(workspace, active_from=date(2026, 1, 1))
    workspace.subscriptions.add_price(
        AddPricePeriodRequest(
            subscription_id=subscription_id,
            amount_minor=39000,
            effective_from=date(2026, 1, 1),
        )
    )
    current = workspace.subscriptions.get(subscription_id)
    assert current is not None

    workspace.subscriptions.deactivate(
        DeactivateSubscriptionRequest(
            subscription_id=subscription_id,
            expected_version=current.version,
            active_to=date(2026, 8, 1),
        )
    )

    july = workspace.requirement.execute(year=2026, month=7, currency="TWD")
    august = workspace.requirement.execute(year=2026, month=8, currency="TWD")

    assert july.total_minor == 39000
    assert july.items[0].reason is SubscriptionRequirementReason.MONTHLY_PRICE
    assert not july.items[0].is_active
    assert august.total_minor == 0
    assert august.items[0].reason is SubscriptionRequirementReason.NO_LONGER_ACTIVE
    assert august.items[0].name == "測試影音"


def test_a_month_before_the_activity_range_is_zero_with_its_own_reason(
    workspace: Workspace,
) -> None:
    subscription_id = _add(workspace, active_from=date(2026, 5, 1))
    workspace.subscriptions.add_price(
        AddPricePeriodRequest(
            subscription_id=subscription_id,
            amount_minor=39000,
            effective_from=date(2026, 1, 1),
        )
    )

    april = workspace.requirement.execute(year=2026, month=4, currency="TWD")

    assert april.total_minor == 0
    assert april.items[0].reason is SubscriptionRequirementReason.NOT_YET_ACTIVE


def test_deactivating_without_naming_a_month_is_refused(
    workspace: Workspace,
) -> None:
    subscription_id = _add(workspace)
    current = workspace.subscriptions.get(subscription_id)
    assert current is not None

    with pytest.raises(SubscriptionError, match="month it stopped"):
        workspace.subscriptions.deactivate(
            DeactivateSubscriptionRequest(
                subscription_id=subscription_id,
                expected_version=current.version,
                active_to=None,  # type: ignore[arg-type]
            )
        )


def test_each_currency_is_calculated_and_totalled_on_its_own(
    workspace: Workspace,
) -> None:
    workspace.open_account(name="測試外幣帳戶", currency="JPY", wallet="測試日圓錢包")
    workspace.open_account(name="測試美元帳戶", currency="USD", wallet="測試美元錢包")
    twd = _add(workspace, name="測試影音")
    jpy = _add(workspace, name="測試日本影音", currency="JPY", wallet="測試日圓錢包")
    usd = _add(workspace, name="測試雲端", currency="USD", wallet="測試美元錢包")
    for subscription_id, amount in ((twd, 39000), (jpy, 1200), (usd, 999)):
        workspace.subscriptions.add_price(
            AddPricePeriodRequest(
                subscription_id=subscription_id,
                amount_minor=amount,
                effective_from=date(2026, 1, 1),
            )
        )

    currencies = workspace.requirement.currencies()
    totals = {
        code: workspace.requirement.execute(
            year=2026, month=8, currency=code
        ).total_minor
        for code in currencies
    }

    assert currencies == ("JPY", "TWD", "USD")
    assert totals == {"JPY": 1200, "TWD": 39000, "USD": 999}
    for code in currencies:
        requirement = workspace.requirement.execute(year=2026, month=8, currency=code)
        assert {item.currency for item in requirement.items} == {code}


def test_a_subscription_must_be_funded_from_a_wallet_in_its_own_currency(
    workspace: Workspace,
) -> None:
    workspace.open_account(name="測試外幣帳戶", currency="JPY", wallet="測試日圓錢包")

    with pytest.raises(LedgerConfigurationError, match="own currency"):
        _add(workspace, currency="TWD", wallet="測試日圓錢包")


def test_a_subscription_cannot_use_a_card_liability_account(
    workspace: Workspace,
) -> None:
    card = workspace.setup.open_account(
        OpenFinancialAccountRequest(
            institution_code="test_bank",
            display_name="測試信用卡",
            account_type=FinancialAccountType.CARD_LIABILITY,
            currency="TWD",
        )
    )

    assert card.unallocated_wallet is None


def test_an_inactive_category_cannot_receive_a_new_subscription(
    workspace: Workspace,
) -> None:
    category_id = workspace.add_category("測試舊分類")
    categories = {item.id: item for item in workspace.plan.list_categories()}
    workspace.plan.update_category(
        UpdateCategoryRequest(
            category_id=category_id,
            expected_version=categories[category_id].version,
            name="測試舊分類",
            parent_id=None,
            is_active=False,
        )
    )

    with pytest.raises(LedgerConfigurationError, match="inactive budget category"):
        _add(workspace, name="測試雲端", category="測試舊分類")


def test_an_overlapping_price_is_refused_atomically_by_the_service(
    workspace: Workspace,
) -> None:
    subscription_id = _add(workspace)
    workspace.subscriptions.add_price(
        AddPricePeriodRequest(
            subscription_id=subscription_id,
            amount_minor=39000,
            effective_from=date(2026, 1, 1),
            effective_to=date(2026, 7, 1),
        )
    )

    with pytest.raises(SubscriptionOverlapError):
        workspace.subscriptions.add_price(
            AddPricePeriodRequest(
                subscription_id=subscription_id,
                amount_minor=49000,
                effective_from=date(2026, 4, 1),
            )
        )

    prices = workspace.subscriptions.list_prices(subscription_id)
    assert len(prices) == 1
    assert (
        workspace.requirement.execute(year=2026, month=4, currency="TWD").total_minor
        == 39000
    )


def test_a_price_correction_keeps_its_version_and_its_history(
    workspace: Workspace,
) -> None:
    subscription_id = _add(workspace)
    price = workspace.subscriptions.add_price(
        AddPricePeriodRequest(
            subscription_id=subscription_id,
            amount_minor=39000,
            effective_from=date(2026, 1, 1),
        )
    )

    corrected = workspace.subscriptions.update_price(
        UpdatePricePeriodRequest(
            price_period_id=price.id,
            expected_version=price.version,
            amount_minor=41000,
            effective_from=date(2026, 1, 1),
            effective_to=None,
        )
    )

    assert corrected.version == 2
    assert len(workspace.subscriptions.list_prices(subscription_id)) == 1
    assert (
        workspace.requirement.execute(year=2026, month=8, currency="TWD").total_minor
        == 41000
    )


def test_a_price_for_an_unknown_subscription_is_refused(
    workspace: Workspace,
) -> None:
    with pytest.raises(SubscriptionError, match="was not found"):
        workspace.subscriptions.add_price(
            AddPricePeriodRequest(
                subscription_id=9999,
                amount_minor=1,
                effective_from=date(2026, 1, 1),
            )
        )
    with pytest.raises(SubscriptionError, match="was not found"):
        workspace.subscriptions.update_price(
            UpdatePricePeriodRequest(
                price_period_id=9999,
                expected_version=1,
                amount_minor=1,
                effective_from=date(2026, 1, 1),
                effective_to=None,
            )
        )


def _update(
    workspace: Workspace,
    current: object,
    **changes: object,
) -> UpdateSubscriptionRequest:
    """Build one edit that keeps every field the caller did not change."""
    assert isinstance(current, Subscription)
    values: dict[str, object] = {
        "subscription_id": current.id,
        "expected_version": current.version,
        "name": current.name,
        "budget_category_id": current.budget_category_id,
        "funding_wallet_id": current.funding_wallet_id,
        "currency": current.currency,
        "billing_cadence": current.billing_cadence,
        "cadence_months": current.cadence_months,
        "billing_month": current.billing_month,
        "billing_day": current.billing_day,
        "active_from": current.active_from,
        "active_to": current.active_to,
    }
    values.update(changes)
    return UpdateSubscriptionRequest(**values)  # type: ignore[arg-type]


def test_an_unpriced_subscription_can_still_change_its_configuration(
    workspace: Workspace,
) -> None:
    subscription_id = _add(workspace)
    current = workspace.subscriptions.get(subscription_id)
    assert current is not None

    updated = workspace.subscriptions.update(
        _update(
            workspace,
            current,
            name="測試影音 Plus",
            billing_cadence=SubscriptionCadence.YEARLY,
            cadence_months=12,
            billing_month=3,
            billing_day=15,
            active_from=date(2026, 1, 1),
        )
    )

    assert updated.name == "測試影音 Plus"
    assert updated.billing_cadence is SubscriptionCadence.YEARLY
    assert updated.billing_month == 3
    assert updated.version == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("currency", "USD"),
        ("billing_cadence", SubscriptionCadence.YEARLY),
        ("billing_month", 3),
    ],
)
def test_a_priced_subscription_cannot_reinterpret_its_stored_prices(
    workspace: Workspace, field: str, value: object
) -> None:
    """A price stores an amount and a range, not the configuration behind it.

    Changing any of these would reread the same 99900 as another currency or
    another cycle, so every month already calculated would silently change.
    """
    subscription_id = _add(workspace)
    workspace.subscriptions.add_price(
        AddPricePeriodRequest(
            subscription_id=subscription_id,
            amount_minor=99900,
            effective_from=date(2026, 1, 1),
        )
    )
    before = workspace.requirement.execute(year=2026, month=2, currency="TWD")
    current = workspace.subscriptions.get(subscription_id)

    changes: dict[str, object] = {field: value}
    if field == "billing_cadence":
        changes["cadence_months"] = 12
        changes["billing_month"] = 3
    with pytest.raises(SubscriptionError, match="cannot change"):
        workspace.subscriptions.update(_update(workspace, current, **changes))

    after = workspace.requirement.execute(year=2026, month=2, currency="TWD")
    assert before.items[0].contribution_minor == 99900
    assert after.items == before.items
    assert after.total_minor == before.total_minor


def test_a_priced_custom_cycle_cannot_change_its_interval_length(
    workspace: Workspace,
) -> None:
    subscription_id = _add(
        workspace,
        name="測試雲端",
        cadence=SubscriptionCadence.CUSTOM,
        cadence_months=3,
        active_from=date(2026, 1, 1),
    )
    workspace.subscriptions.add_price(
        AddPricePeriodRequest(
            subscription_id=subscription_id,
            amount_minor=3000,
            effective_from=date(2026, 1, 1),
        )
    )
    before = workspace.requirement.execute(year=2026, month=2, currency="TWD")
    current = workspace.subscriptions.get(subscription_id)

    with pytest.raises(SubscriptionError, match="cadence_months"):
        workspace.subscriptions.update(_update(workspace, current, cadence_months=6))

    after = workspace.requirement.execute(year=2026, month=2, currency="TWD")
    assert before.items[0].contribution_minor == 1000
    assert after.items == before.items


def test_a_priced_subscription_cannot_move_its_category_or_wallet(
    workspace: Workspace,
) -> None:
    workspace.open_account(name="測試第二帳戶", currency="TWD", wallet="測試其他錢包")
    other_category = workspace.add_category("測試其他分類")
    subscription_id = _add(workspace)
    workspace.subscriptions.add_price(
        AddPricePeriodRequest(
            subscription_id=subscription_id,
            amount_minor=39000,
            effective_from=date(2026, 1, 1),
        )
    )
    before = workspace.requirement.execute(year=2026, month=2, currency="TWD")
    current = workspace.subscriptions.get(subscription_id)

    for field, value in (
        ("budget_category_id", other_category),
        ("funding_wallet_id", workspace.wallets["測試其他錢包"]),
    ):
        with pytest.raises(SubscriptionError, match="cannot change"):
            workspace.subscriptions.update(
                _update(workspace, current, **{field: value})
            )

    after = workspace.requirement.execute(year=2026, month=2, currency="TWD")
    assert after.items == before.items
    assert after.items[0].funding_wallet_name == "測試訂閱錢包"
    assert after.items[0].budget_category_name == "測試訂閱"


def test_a_priced_custom_cycle_cannot_move_its_anchor(workspace: Workspace) -> None:
    subscription_id = _add(
        workspace,
        name="測試雲端",
        cadence=SubscriptionCadence.CUSTOM,
        cadence_months=3,
        active_from=date(2026, 1, 1),
    )
    workspace.subscriptions.add_price(
        AddPricePeriodRequest(
            subscription_id=subscription_id,
            amount_minor=3000,
            effective_from=date(2026, 1, 1),
        )
    )
    before = workspace.requirement.execute(year=2026, month=3, currency="TWD")
    current = workspace.subscriptions.get(subscription_id)

    with pytest.raises(SubscriptionError, match="active_from"):
        workspace.subscriptions.update(
            _update(workspace, current, active_from=date(2026, 2, 1))
        )

    after = workspace.requirement.execute(year=2026, month=3, currency="TWD")
    assert before.items[0].cycle_position == 2
    assert after.items == before.items


def test_a_priced_subscription_can_still_be_renamed_and_re_dated(
    workspace: Workspace,
) -> None:
    """Only the fields a stored price is read through are frozen."""
    subscription_id = _add(workspace)
    workspace.subscriptions.add_price(
        AddPricePeriodRequest(
            subscription_id=subscription_id,
            amount_minor=39000,
            effective_from=date(2026, 1, 1),
        )
    )
    current = workspace.subscriptions.get(subscription_id)

    updated = workspace.subscriptions.update(
        _update(
            workspace,
            current,
            name="測試影音 Plus",
            billing_day=20,
            active_from=date(2026, 1, 1),
        )
    )

    assert updated.name == "測試影音 Plus"
    assert updated.billing_day == 20
    assert (
        workspace.requirement.execute(year=2026, month=2, currency="TWD").total_minor
        == 39000
    )


def test_an_edit_can_never_restart_a_subscription_that_stopped(
    workspace: Workspace,
) -> None:
    subscription_id = _add(workspace, active_from=date(2026, 1, 1))
    workspace.subscriptions.add_price(
        AddPricePeriodRequest(
            subscription_id=subscription_id,
            amount_minor=39000,
            effective_from=date(2026, 1, 1),
        )
    )
    current = workspace.subscriptions.get(subscription_id)
    assert current is not None
    workspace.subscriptions.deactivate(
        DeactivateSubscriptionRequest(
            subscription_id=subscription_id,
            expected_version=current.version,
            active_to=date(2026, 8, 1),
        )
    )
    stopped = workspace.subscriptions.get(subscription_id)
    assert stopped is not None

    # Clearing the end month through an ordinary edit is refused, because a
    # deactivated row must always say when it stopped.
    with pytest.raises(ValueError, match="month it stopped"):
        workspace.subscriptions.update(_update(workspace, stopped, active_to=None))

    august = workspace.requirement.execute(year=2026, month=8, currency="TWD")
    assert august.total_minor == 0
    assert august.items[0].reason is SubscriptionRequirementReason.NO_LONGER_ACTIVE


def test_resuming_starts_a_new_interval_and_keeps_the_stop(
    workspace: Workspace,
) -> None:
    """Use, stop, use again needs two intervals; one range cannot hold it."""
    subscription_id = _add(workspace, active_from=date(2026, 1, 1))
    workspace.subscriptions.add_price(
        AddPricePeriodRequest(
            subscription_id=subscription_id,
            amount_minor=39000,
            effective_from=date(2026, 1, 1),
        )
    )
    current = workspace.subscriptions.get(subscription_id)
    assert current is not None
    workspace.subscriptions.deactivate(
        DeactivateSubscriptionRequest(
            subscription_id=subscription_id,
            expected_version=current.version,
            active_to=date(2026, 8, 1),
        )
    )

    resumed = workspace.subscriptions.resume(
        ResumeSubscriptionRequest(
            subscription_id=subscription_id, active_from=date(2026, 11, 1)
        )
    )
    workspace.subscriptions.add_price(
        AddPricePeriodRequest(
            subscription_id=resumed.id,
            amount_minor=49000,
            effective_from=date(2026, 11, 1),
        )
    )

    stopped = workspace.subscriptions.get(subscription_id)
    july = workspace.requirement.execute(year=2026, month=7, currency="TWD")
    september = workspace.requirement.execute(year=2026, month=9, currency="TWD")
    november = workspace.requirement.execute(year=2026, month=11, currency="TWD")

    assert stopped is not None
    assert stopped.active_to == date(2026, 8, 1)
    assert not stopped.is_active
    assert resumed.id != subscription_id
    assert resumed.is_active
    assert resumed.active_from == date(2026, 11, 1)
    assert resumed.billing_cadence is stopped.billing_cadence
    assert resumed.currency == stopped.currency
    # The gap stays a gap: neither interval contributes in September.
    assert july.total_minor == 39000
    assert september.total_minor == 0
    assert november.total_minor == 49000
    assert {item.reason for item in september.items} == {
        SubscriptionRequirementReason.NO_LONGER_ACTIVE,
        SubscriptionRequirementReason.NOT_YET_ACTIVE,
    }


def test_resuming_is_refused_before_the_month_the_old_interval_ended(
    workspace: Workspace,
) -> None:
    subscription_id = _add(workspace, active_from=date(2026, 1, 1))
    current = workspace.subscriptions.get(subscription_id)
    assert current is not None
    workspace.subscriptions.deactivate(
        DeactivateSubscriptionRequest(
            subscription_id=subscription_id,
            expected_version=current.version,
            active_to=date(2026, 8, 1),
        )
    )

    with pytest.raises(SubscriptionError, match="no earlier than"):
        workspace.subscriptions.resume(
            ResumeSubscriptionRequest(
                subscription_id=subscription_id, active_from=date(2026, 7, 1)
            )
        )

    assert len(workspace.subscriptions.list_all()) == 1


def test_only_a_stopped_subscription_starts_a_new_interval(
    workspace: Workspace,
) -> None:
    subscription_id = _add(workspace)

    with pytest.raises(SubscriptionError, match="only a stopped subscription"):
        workspace.subscriptions.resume(
            ResumeSubscriptionRequest(
                subscription_id=subscription_id, active_from=date(2026, 11, 1)
            )
        )

    assert len(workspace.subscriptions.list_all()) == 1


def test_deactivating_twice_is_refused(workspace: Workspace) -> None:
    subscription_id = _add(workspace)
    current = workspace.subscriptions.get(subscription_id)
    assert current is not None
    stopped = workspace.subscriptions.deactivate(
        DeactivateSubscriptionRequest(
            subscription_id=subscription_id,
            expected_version=current.version,
            active_to=date(2026, 8, 1),
        )
    )

    with pytest.raises(SubscriptionError, match="already stopped"):
        workspace.subscriptions.deactivate(
            DeactivateSubscriptionRequest(
                subscription_id=subscription_id,
                expected_version=stopped.version,
                active_to=date(2026, 9, 1),
            )
        )

    unchanged = workspace.subscriptions.get(subscription_id)
    assert unchanged is not None
    assert unchanged.active_to == date(2026, 8, 1)


def test_a_new_interval_cannot_be_slipped_in_under_the_same_name(
    workspace: Workspace,
) -> None:
    """Deactivating no longer releases the months the old interval still owns."""
    subscription_id = _add(workspace, active_from=date(2026, 1, 1))
    current = workspace.subscriptions.get(subscription_id)
    assert current is not None
    workspace.subscriptions.deactivate(
        DeactivateSubscriptionRequest(
            subscription_id=subscription_id,
            expected_version=current.version,
            active_to=date(2026, 8, 1),
        )
    )

    with pytest.raises(SubscriptionOverlapError, match="must not overlap"):
        _add(workspace, active_from=date(2026, 5, 1))
    adjacent = _add(workspace, active_from=date(2026, 8, 1))

    assert adjacent != subscription_id
    assert len(workspace.subscriptions.list_all()) == 2


def test_the_default_funding_wallet_is_configuration_not_a_hard_coded_id(
    workspace: Workspace,
) -> None:
    before = workspace.subscriptions.default_funding_wallet()

    view = workspace.subscriptions.set_default_funding_wallet(
        workspace.wallets["測試訂閱錢包"]
    )
    after = workspace.subscriptions.default_funding_wallet()

    assert not before.is_configured
    assert view.is_usable
    assert after.wallet_id == workspace.wallets["測試訂閱錢包"]
    assert after.wallet_name == "測試訂閱錢包"
    assert after.financial_account_name == "測試支票帳戶"
    assert after.currency == "TWD"


def test_the_default_funding_wallet_must_be_an_active_asset_wallet(
    workspace: Workspace,
) -> None:
    card = workspace.setup.open_account(
        OpenFinancialAccountRequest(
            institution_code="test_bank",
            display_name="測試信用卡",
            account_type=FinancialAccountType.CARD_LIABILITY,
            currency="TWD",
        )
    )

    with pytest.raises(LedgerConfigurationError):
        workspace.subscriptions.set_default_funding_wallet(9999)
    assert card.unallocated_wallet is None
    assert not workspace.subscriptions.default_funding_wallet().is_configured


def test_setting_a_default_wallet_creates_no_commitment_and_no_entry(
    workspace: Workspace,
) -> None:
    workspace.subscriptions.set_default_funding_wallet(
        workspace.wallets["測試訂閱錢包"]
    )
    subscription_id = _add(workspace)
    workspace.subscriptions.add_price(
        AddPricePeriodRequest(
            subscription_id=subscription_id,
            amount_minor=39000,
            effective_from=date(2026, 1, 1),
        )
    )
    workspace.requirement.execute(year=2026, month=8, currency="TWD")

    with workspace.factory() as unit:
        commitments = unit.commitments.list_open_for_wallet(
            workspace.wallets["測試訂閱錢包"]
        )
        balance = unit.budget_ledger.wallet_balance(workspace.wallets["測試訂閱錢包"])
        reserved = unit.budget_ledger.reserved_minor(workspace.wallets["測試訂閱錢包"])

    assert commitments == ()
    assert balance == 0
    assert reserved == 0


def test_the_register_lists_every_subscription_with_its_prices(
    workspace: Workspace,
) -> None:
    subscription_id = _add(workspace)
    workspace.subscriptions.add_price(
        AddPricePeriodRequest(
            subscription_id=subscription_id,
            amount_minor=39000,
            effective_from=date(2026, 1, 1),
            effective_to=date(2026, 7, 1),
        )
    )
    workspace.subscriptions.add_price(
        AddPricePeriodRequest(
            subscription_id=subscription_id,
            amount_minor=49000,
            effective_from=date(2026, 7, 1),
        )
    )

    rows = workspace.register.register()

    assert len(rows) == 1
    assert rows[0].subscription.name == "測試影音"
    assert rows[0].funding_wallet_name == "測試訂閱錢包"
    assert rows[0].funding_account_name == "測試支票帳戶"
    assert rows[0].budget_category_name == "測試訂閱"
    assert [price.amount_minor for price in rows[0].prices] == [39000, 49000]
    assert workspace.register.currencies() == ("TWD",)


def test_a_requirement_writes_no_journal_entry_or_salary_run(
    workspace: Workspace,
) -> None:
    """B4 calculates. Freezing the evidence into a run belongs to B5."""
    subscription_id = _add(workspace)
    workspace.subscriptions.add_price(
        AddPricePeriodRequest(
            subscription_id=subscription_id,
            amount_minor=39000,
            effective_from=date(2026, 1, 1),
        )
    )

    workspace.requirement.execute(year=2026, month=8, currency="TWD")

    with workspace.factory() as unit:
        entries = unit.journal.get_by_idempotency_key(f"subscription:{subscription_id}")
        events = unit.financial_events.list_by_status(frozenset())

    assert entries is None
    assert events == ()


def test_a_second_interval_cannot_overlap_a_stopped_one(
    workspace: Workspace,
) -> None:
    """A stopped interval whose end month is still ahead still owns it.

    Releasing the name the moment a subscription is deactivated let a new
    interval cover months the old one still covered, and the month emitted two
    items and charged for the service twice.
    """
    subscription_id = _add(workspace, active_from=date(2026, 1, 1))
    workspace.subscriptions.add_price(
        AddPricePeriodRequest(
            subscription_id=subscription_id,
            amount_minor=39000,
            effective_from=date(2026, 1, 1),
        )
    )
    current = workspace.subscriptions.get(subscription_id)
    assert current is not None
    workspace.subscriptions.deactivate(
        DeactivateSubscriptionRequest(
            subscription_id=subscription_id,
            expected_version=current.version,
            active_to=date(2026, 11, 1),
        )
    )

    with pytest.raises(SubscriptionOverlapError, match="must not overlap"):
        _add(workspace, active_from=date(2026, 8, 1))

    september = workspace.requirement.execute(year=2026, month=9, currency="TWD")
    assert len(workspace.subscriptions.list_all()) == 1
    assert len(september.items) == 1
    assert september.total_minor == 39000


def test_resuming_is_compared_against_every_interval_of_the_service(
    workspace: Workspace,
) -> None:
    """Not only the interval the resume happens to start from."""
    first_id = _add(workspace, active_from=date(2026, 1, 1))
    first = workspace.subscriptions.get(first_id)
    assert first is not None
    workspace.subscriptions.deactivate(
        DeactivateSubscriptionRequest(
            subscription_id=first_id,
            expected_version=first.version,
            active_to=date(2026, 4, 1),
        )
    )
    second = workspace.subscriptions.resume(
        ResumeSubscriptionRequest(
            subscription_id=first_id, active_from=date(2026, 4, 1)
        )
    )
    workspace.subscriptions.deactivate(
        DeactivateSubscriptionRequest(
            subscription_id=second.id,
            expected_version=second.version,
            active_to=date(2026, 9, 1),
        )
    )

    # Resuming the oldest interval again would land inside the second one.
    with pytest.raises(SubscriptionOverlapError, match="must not overlap"):
        workspace.subscriptions.resume(
            ResumeSubscriptionRequest(
                subscription_id=first_id, active_from=date(2026, 6, 1)
            )
        )
    third = workspace.subscriptions.resume(
        ResumeSubscriptionRequest(
            subscription_id=first_id, active_from=date(2026, 9, 1)
        )
    )

    rows = workspace.subscriptions.list_all()
    assert len(rows) == 3
    assert third.active_from == date(2026, 9, 1)
    for month in range(1, 13):
        requirement = workspace.requirement.execute(
            year=2026, month=month, currency="TWD"
        )
        contributing = [item for item in requirement.items if item.contributes]
        assert len(contributing) <= 1, month


def test_renaming_an_interval_onto_an_overlapping_service_is_refused(
    workspace: Workspace,
) -> None:
    """Renaming moves an interval into another service's timeline."""
    _add(workspace, name="測試影音", active_from=date(2026, 1, 1))
    other_id = _add(workspace, name="測試雲端", active_from=date(2026, 1, 1))
    other = workspace.subscriptions.get(other_id)

    with pytest.raises(SubscriptionOverlapError, match="must not overlap"):
        workspace.subscriptions.update(_update(workspace, other, name="測試影音"))

    assert sorted(item.name for item in workspace.subscriptions.list_all()) == [
        "測試影音",
        "測試雲端",
    ]


def test_extending_an_interval_into_the_next_one_is_refused(
    workspace: Workspace,
) -> None:
    first_id = _add(workspace, active_from=date(2026, 1, 1))
    first = workspace.subscriptions.get(first_id)
    assert first is not None
    workspace.subscriptions.deactivate(
        DeactivateSubscriptionRequest(
            subscription_id=first_id,
            expected_version=first.version,
            active_to=date(2026, 4, 1),
        )
    )
    workspace.subscriptions.resume(
        ResumeSubscriptionRequest(
            subscription_id=first_id, active_from=date(2026, 4, 1)
        )
    )
    stopped = workspace.subscriptions.get(first_id)

    with pytest.raises(SubscriptionOverlapError, match="must not overlap"):
        workspace.subscriptions.update(
            _update(workspace, stopped, active_to=date(2026, 7, 1))
        )

    unchanged = workspace.subscriptions.get(first_id)
    assert unchanged is not None
    assert unchanged.active_to == date(2026, 4, 1)
    assert unchanged.version == 2


def test_an_earlier_interval_stays_editable_after_a_resume(
    workspace: Workspace,
) -> None:
    """The active name of the new interval must not lock the old one."""
    first_id = _add(workspace, active_from=date(2026, 1, 1))
    first = workspace.subscriptions.get(first_id)
    assert first is not None
    workspace.subscriptions.deactivate(
        DeactivateSubscriptionRequest(
            subscription_id=first_id,
            expected_version=first.version,
            active_to=date(2026, 8, 1),
        )
    )
    workspace.subscriptions.resume(
        ResumeSubscriptionRequest(
            subscription_id=first_id, active_from=date(2026, 11, 1)
        )
    )
    stopped = workspace.subscriptions.get(first_id)

    corrected = workspace.subscriptions.update(
        _update(workspace, stopped, billing_day=20)
    )

    assert corrected.billing_day == 20
    assert not corrected.is_active
    assert corrected.active_to == date(2026, 8, 1)


def test_an_unbounded_interval_leaves_no_room_for_another(
    workspace: Workspace,
) -> None:
    _add(workspace, active_from=None)

    with pytest.raises(SubscriptionOverlapError, match="must not overlap"):
        _add(workspace, active_from=date(2030, 1, 1))

    assert len(workspace.subscriptions.list_all()) == 1


def test_only_one_interval_of_a_service_runs_at_a_time(
    workspace: Workspace,
) -> None:
    """The second invariant, and the one the unique index also holds.

    Adjacent intervals do not overlap, so the overlap rule permits them; what
    forbids two of them running at once is that a service has one current
    interval. The register's own workflow is to stop the first one first.
    """
    first_id = _add(workspace, active_from=date(2026, 1, 1), active_to=date(2026, 7, 1))

    with pytest.raises(SubscriptionError, match="already uses this name"):
        _add(workspace, active_from=date(2026, 7, 1))

    first = workspace.subscriptions.get(first_id)
    assert first is not None
    workspace.subscriptions.deactivate(
        DeactivateSubscriptionRequest(
            subscription_id=first_id,
            expected_version=first.version,
            active_to=date(2026, 7, 1),
        )
    )
    second = workspace.subscriptions.resume(
        ResumeSubscriptionRequest(
            subscription_id=first_id, active_from=date(2026, 7, 1)
        )
    )

    rows = workspace.subscriptions.list_all()
    assert [item.is_active for item in rows] == [False, True]
    assert second.active_from == date(2026, 7, 1)


def test_a_stopped_interval_may_take_a_running_services_name(
    workspace: Workspace,
) -> None:
    running_id = _add(workspace, name="測試影音", active_from=date(2026, 1, 1))
    other_id = _add(
        workspace,
        name="測試雲端",
        active_from=date(2020, 1, 1),
        active_to=date(2020, 7, 1),
    )
    other = workspace.subscriptions.get(other_id)
    assert other is not None
    workspace.subscriptions.deactivate(
        DeactivateSubscriptionRequest(
            subscription_id=other_id,
            expected_version=other.version,
            active_to=date(2020, 7, 1),
        )
    )
    stopped = workspace.subscriptions.get(other_id)

    # A stopped interval may take the name of a running one, because it does
    # not overlap it and is not itself running.
    renamed = workspace.subscriptions.update(
        _update(workspace, stopped, name="測試影音")
    )

    assert renamed.name == "測試影音"
    assert not renamed.is_active
    running = workspace.subscriptions.get(running_id)
    assert running is not None
    assert running.is_active
    named = [
        item for item in workspace.subscriptions.list_all() if item.name == "測試影音"
    ]
    assert [item.is_active for item in named] == [True, False]

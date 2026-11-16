"""B3 acceptance tests for budget totals, the buffer, and month close."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from app.application.queries.budget import BudgetQueries
from app.application.services.budget_close import CloseBudgetPeriod
from app.application.services.budget_planning import (
    AddCategoryRequest,
    ManageBudgetPlan,
    SetTargetRequest,
    UpdateCategoryRequest,
)
from app.application.services.event_allocation import (
    AllocateFinancialEvent,
    AllocateFinancialEventRequest,
    AllocationLine,
)
from app.application.services.ledger import (
    AddWalletRequest,
    JournalService,
    LedgerSetupService,
    OpenFinancialAccountRequest,
    OpeningPositionRequest,
)
from app.domain.budget_enums import (
    BudgetPeriodStatus,
    CommitmentFulfillmentMode,
    FinancialAccountType,
    FinancialEventOrigin,
    FinancialEventType,
    JournalEntryType,
    PaymentMethod,
    RecognitionBasis,
)
from app.domain.budget_models import (
    EventAllocationDraft,
    FinancialEventDraft,
    FundingCommitmentDraft,
    OpeningWalletSplit,
)
from app.domain.errors import (
    BudgetPeriodError,
    ConcurrentUpdateError,
    LedgerConfigurationError,
)
from app.domain.ports.repositories import UnitOfWork
from app.infrastructure.database.budget_queries import SqlAlchemyBudgetReadRepository
from app.infrastructure.database.engine import Database
from app.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork

TIMEZONE = "Asia/Taipei"
# 2026-08-07 13:30 Taipei; the UTC instant is still inside the local month.
OCCURRED_AT = datetime(2026, 8, 7, 5, 30, tzinfo=UTC)
CLOSED_AT = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)

type UnitOfWorkFactory = Callable[[], UnitOfWork]


@pytest.fixture
def factory(migrated_database: Database) -> UnitOfWorkFactory:
    """Return a unit-of-work factory bound to the migrated test database."""

    def build() -> UnitOfWork:
        return SqlAlchemyUnitOfWork(migrated_database.session_factory)

    return build


@pytest.fixture
def plan(factory: UnitOfWorkFactory) -> ManageBudgetPlan:
    """Return the category and target management service."""
    return ManageBudgetPlan(factory)


@pytest.fixture
def close(factory: UnitOfWorkFactory) -> CloseBudgetPeriod:
    """Return the close service on a fixed clock."""
    return CloseBudgetPeriod(
        factory,
        JournalService(factory),
        local_timezone=TIMEZONE,
        clock=lambda: CLOSED_AT,
    )


@pytest.fixture
def budget(migrated_database: Database) -> BudgetQueries:
    """Return the month view over the same database."""
    return BudgetQueries(
        SqlAlchemyBudgetReadRepository(
            migrated_database.session_factory, local_timezone=TIMEZONE
        )
    )


@pytest.fixture
def world(factory: UnitOfWorkFactory) -> dict[str, int]:
    """Open one account with three wallets and 10,000.00 opening money."""
    setup = LedgerSetupService(factory)
    bank = setup.open_account(
        OpenFinancialAccountRequest(
            institution_code="test_bank",
            display_name="測試支票帳戶",
            account_type=FinancialAccountType.CHECKING,
            currency="TWD",
        )
    )
    other = setup.open_account(
        OpenFinancialAccountRequest(
            institution_code="other_bank",
            display_name="另一家銀行",
            account_type=FinancialAccountType.SAVINGS,
            currency="TWD",
        )
    )
    for name in ("飲食", "儲蓄"):
        setup.add_wallet(
            AddWalletRequest(financial_account_id=bank.account.id, name=name)
        )
    far = setup.add_wallet(
        AddWalletRequest(financial_account_id=other.account.id, name="外部儲蓄")
    )
    JournalService(factory).record_opening_position(
        OpeningPositionRequest(
            financial_account_id=bank.account.id,
            balance_minor=1_000_000,
            observed_at=OCCURRED_AT,
            splits=(
                OpeningWalletSplit(wallet_name="尚未分配", amount_minor=400_000),
                OpeningWalletSplit(wallet_name="飲食", amount_minor=500_000),
                OpeningWalletSplit(wallet_name="儲蓄", amount_minor=100_000),
            ),
        )
    )
    with factory() as unit:
        wallets = {
            wallet.name: wallet.id
            for wallet in unit.wallets.list_for_account(bank.account.id)
        }
    return {
        "bank_account_id": bank.account.id,
        "other_account_id": other.account.id,
        "food_wallet_id": wallets["飲食"],
        "savings_wallet_id": wallets["儲蓄"],
        "unallocated_id": wallets["尚未分配"],
        "far_wallet_id": far.id,
    }


def _spend(
    factory: UnitOfWorkFactory,
    *,
    key: str,
    amount_minor: int,
    wallet_id: int,
    category_id: int | None,
    event_type: FinancialEventType = FinancialEventType.PURCHASE,
    refund_of_event_id: int | None = None,
    occurred_at: datetime = OCCURRED_AT,
) -> int:
    """Post one bank-funded event so it lands in the month's totals."""
    with factory() as unit:
        event = unit.financial_events.add(
            FinancialEventDraft(
                origin=FinancialEventOrigin.MANUAL,
                event_type=event_type,
                payment_method=PaymentMethod.BANK_ACCOUNT,
                recognition_basis=RecognitionBasis.OBSERVED_MANUAL,
                occurred_at=occurred_at,
                amount_minor=amount_minor,
                currency="TWD",
                idempotency_key=key,
                merchant="測試商店",
                refund_of_event_id=refund_of_event_id,
            )
        )
        unit.commit()
        event_id = event.id
    AllocateFinancialEvent(factory, JournalService(factory)).execute(
        AllocateFinancialEventRequest(
            financial_event_id=event_id,
            expected_version=1,
            client_token=f"token-{key}",
            lines=(
                AllocationLine(
                    amount_minor=amount_minor,
                    funding_wallet_id=wallet_id,
                    budget_category_id=category_id,
                ),
            ),
        )
    )
    return event_id


def test_budget_totals_include_refunds_with_the_correct_direction(
    factory: UnitOfWorkFactory,
    plan: ManageBudgetPlan,
    budget: BudgetQueries,
    world: dict[str, int],
) -> None:
    food = plan.add_category(AddCategoryRequest(name="飲食"))
    plan.set_target(
        SetTargetRequest(
            year=2026,
            month=8,
            currency="TWD",
            budget_category_id=food.id,
            target_minor=300_000,
            buffer_minor=50_000,
            funding_wallet_id=world["food_wallet_id"],
        )
    )
    purchase = _spend(
        factory,
        key="manual:1",
        amount_minor=120_000,
        wallet_id=world["food_wallet_id"],
        category_id=food.id,
    )
    _spend(
        factory,
        key="manual:2",
        amount_minor=20_000,
        wallet_id=world["food_wallet_id"],
        category_id=food.id,
        event_type=FinancialEventType.REFUND,
        refund_of_event_id=purchase,
    )

    view = budget.month(year=2026, month=8, currency="TWD")

    assert len(view.categories) == 1
    line = view.categories[0]
    assert line.net_spend_minor == 100_000
    assert line.remaining_minor == 200_000
    assert line.overspend_minor == 0
    assert view.total_spend_minor == 100_000
    assert view.total_remaining_minor == 200_000


def test_the_buffer_is_reported_beside_the_target_not_inside_it(
    plan: ManageBudgetPlan,
    budget: BudgetQueries,
    world: dict[str, int],
) -> None:
    food = plan.add_category(AddCategoryRequest(name="飲食"))
    plan.set_target(
        SetTargetRequest(
            year=2026,
            month=8,
            currency="TWD",
            budget_category_id=food.id,
            target_minor=300_000,
            buffer_minor=50_000,
            funding_wallet_id=world["food_wallet_id"],
        )
    )

    view = budget.month(year=2026, month=8, currency="TWD")

    assert view.total_target_minor == 300_000
    assert view.total_buffer_minor == 50_000
    # Spendable never silently absorbs the cushion.
    assert view.total_remaining_minor == 300_000


def test_overspending_is_reported_and_never_a_negative_remainder(
    factory: UnitOfWorkFactory,
    plan: ManageBudgetPlan,
    budget: BudgetQueries,
    world: dict[str, int],
) -> None:
    food = plan.add_category(AddCategoryRequest(name="飲食"))
    plan.set_target(
        SetTargetRequest(
            year=2026,
            month=8,
            currency="TWD",
            budget_category_id=food.id,
            target_minor=100_000,
            buffer_minor=0,
            funding_wallet_id=world["food_wallet_id"],
        )
    )
    _spend(
        factory,
        key="manual:1",
        amount_minor=150_000,
        wallet_id=world["food_wallet_id"],
        category_id=food.id,
    )

    line = budget.month(year=2026, month=8, currency="TWD").categories[0]

    assert line.net_spend_minor == 150_000
    assert line.remaining_minor == 0
    assert line.overspend_minor == 50_000


def test_spending_belongs_to_the_local_month_it_happened_in(
    factory: UnitOfWorkFactory,
    plan: ManageBudgetPlan,
    budget: BudgetQueries,
    world: dict[str, int],
) -> None:
    """23:30 on 31 August in Taipei is still August, though it is 15:30 UTC."""
    food = plan.add_category(AddCategoryRequest(name="飲食"))
    for year, month in ((2026, 8), (2026, 9)):
        plan.set_target(
            SetTargetRequest(
                year=year,
                month=month,
                currency="TWD",
                budget_category_id=food.id,
                target_minor=100_000,
                buffer_minor=0,
                funding_wallet_id=world["food_wallet_id"],
            )
        )
    _spend(
        factory,
        key="manual:1",
        amount_minor=10_000,
        wallet_id=world["food_wallet_id"],
        category_id=food.id,
        occurred_at=datetime(2026, 8, 31, 15, 30, tzinfo=UTC),
    )

    august = budget.month(year=2026, month=8, currency="TWD")
    september = budget.month(year=2026, month=9, currency="TWD")

    assert august.total_spend_minor == 10_000
    assert september.total_spend_minor == 0


def test_month_close_is_previewable_idempotent_and_bounded(
    factory: UnitOfWorkFactory,
    plan: ManageBudgetPlan,
    close: CloseBudgetPeriod,
    budget: BudgetQueries,
    world: dict[str, int],
) -> None:
    food = plan.add_category(AddCategoryRequest(name="飲食"))
    plan.set_target(
        SetTargetRequest(
            year=2026,
            month=8,
            currency="TWD",
            budget_category_id=food.id,
            target_minor=300_000,
            buffer_minor=50_000,
            funding_wallet_id=world["food_wallet_id"],
            month_end_destination_wallet_id=world["savings_wallet_id"],
        )
    )
    _spend(
        factory,
        key="manual:1",
        amount_minor=120_000,
        wallet_id=world["food_wallet_id"],
        category_id=food.id,
    )

    preview = close.preview(year=2026, month=8, currency="TWD")

    # 300,000 planned minus 120,000 spent leaves 180,000 to sweep, and the
    # preview writes nothing at all.
    assert preview.lines[0].unspent_minor == 180_000
    assert preview.lines[0].sweep_minor == 180_000
    assert not preview.already_closed
    assert preview.period.status is BudgetPeriodStatus.OPEN
    before = {
        wallet.wallet_id: wallet.balance_minor
        for wallet in budget.month(year=2026, month=8, currency="TWD").wallets
    }
    assert before[world["savings_wallet_id"]] == 100_000

    result = close.execute(year=2026, month=8, currency="TWD")

    assert result.entry is not None
    # A month-end sweep is auditable as itself, not as an ordinary transfer.
    assert result.entry.entry_type is JournalEntryType.BUDGET_CLOSE
    assert result.period.status is BudgetPeriodStatus.CLOSED
    after = {
        wallet.wallet_id: wallet.balance_minor
        for wallet in budget.month(year=2026, month=8, currency="TWD").wallets
    }
    assert after[world["food_wallet_id"]] == 380_000 - 180_000
    assert after[world["savings_wallet_id"]] == 280_000
    # The sweep is a reclassification: the account total cannot move.
    assert sum(after.values()) == sum(before.values())

    repeated = close.execute(year=2026, month=8, currency="TWD")

    assert repeated.entry is not None
    assert repeated.entry.id == result.entry.id
    unchanged = {
        wallet.wallet_id: wallet.balance_minor
        for wallet in budget.month(year=2026, month=8, currency="TWD").wallets
    }
    assert unchanged == after


def test_month_close_never_sweeps_more_than_the_wallet_can_give(
    factory: UnitOfWorkFactory,
    plan: ManageBudgetPlan,
    close: CloseBudgetPeriod,
    budget: BudgetQueries,
    world: dict[str, int],
) -> None:
    """A target may exceed the money that is actually there."""
    food = plan.add_category(AddCategoryRequest(name="飲食"))
    plan.set_target(
        SetTargetRequest(
            year=2026,
            month=8,
            currency="TWD",
            budget_category_id=food.id,
            target_minor=900_000,
            buffer_minor=60_000,
            funding_wallet_id=world["food_wallet_id"],
            month_end_destination_wallet_id=world["savings_wallet_id"],
        )
    )

    preview = close.preview(year=2026, month=8, currency="TWD")

    # The wallet holds 500,000 and 60,000 of it is the buffer, so 440,000 is
    # all that can move even though 900,000 went unspent.
    assert preview.lines[0].unspent_minor == 900_000
    assert preview.lines[0].sweep_minor == 440_000
    assert preview.lines[0].limited_by_available

    close.execute(year=2026, month=8, currency="TWD")

    balances = {
        wallet.wallet_id: wallet.balance_minor
        for wallet in budget.month(year=2026, month=8, currency="TWD").wallets
    }
    assert balances[world["food_wallet_id"]] == 60_000
    assert balances[world["savings_wallet_id"]] == 540_000


def test_month_close_will_not_move_money_between_accounts(
    plan: ManageBudgetPlan,
    close: CloseBudgetPeriod,
    budget: BudgetQueries,
    world: dict[str, int],
) -> None:
    food = plan.add_category(AddCategoryRequest(name="飲食"))
    plan.set_target(
        SetTargetRequest(
            year=2026,
            month=8,
            currency="TWD",
            budget_category_id=food.id,
            target_minor=100_000,
            buffer_minor=0,
            funding_wallet_id=world["food_wallet_id"],
            month_end_destination_wallet_id=world["far_wallet_id"],
        )
    )

    preview = close.preview(year=2026, month=8, currency="TWD")

    assert preview.lines[0].requires_manual_transfer
    assert preview.lines[0].unspent_minor == 100_000
    assert preview.lines[0].sweep_minor == 0
    assert preview.manual_transfers == preview.lines

    result = close.execute(year=2026, month=8, currency="TWD")

    # Nothing is posted, so the user is never told money moved when it did not.
    assert result.entry is None
    balances = {
        wallet.wallet_id: wallet.balance_minor
        for wallet in budget.month(year=2026, month=8, currency="TWD").wallets
    }
    assert balances[world["food_wallet_id"]] == 500_000
    assert balances[world["far_wallet_id"]] == 0


def test_a_reserved_wallet_balance_is_not_available_to_sweep(
    factory: UnitOfWorkFactory,
    plan: ManageBudgetPlan,
    close: CloseBudgetPeriod,
    world: dict[str, int],
) -> None:
    food = plan.add_category(AddCategoryRequest(name="飲食"))
    plan.set_target(
        SetTargetRequest(
            year=2026,
            month=8,
            currency="TWD",
            budget_category_id=food.id,
            target_minor=500_000,
            buffer_minor=0,
            funding_wallet_id=world["food_wallet_id"],
            month_end_destination_wallet_id=world["savings_wallet_id"],
        )
    )
    with factory() as unit:
        commitment_amount = 200_000
        event = unit.financial_events.add(
            FinancialEventDraft(
                origin=FinancialEventOrigin.MANUAL,
                event_type=FinancialEventType.PURCHASE,
                payment_method=PaymentMethod.CREDIT_CARD,
                recognition_basis=RecognitionBasis.OBSERVED_MANUAL,
                occurred_at=OCCURRED_AT,
                amount_minor=commitment_amount,
                currency="TWD",
                idempotency_key="manual:reserved",
            )
        )
        allocation = unit.allocations.add(
            EventAllocationDraft(
                financial_event_id=event.id,
                funding_wallet_id=world["food_wallet_id"],
                amount_minor=commitment_amount,
                currency="TWD",
                budget_category_id=food.id,
            )
        )
        unit.commitments.add(
            FundingCommitmentDraft(
                financial_event_id=event.id,
                event_allocation_id=allocation.id,
                source_wallet_id=world["food_wallet_id"],
                settlement_account_id=world["bank_account_id"],
                amount_minor=commitment_amount,
                currency="TWD",
                fulfillment_mode=CommitmentFulfillmentMode.RESERVE_ONLY,
            )
        )
        unit.commit()

    preview = close.preview(year=2026, month=8, currency="TWD")

    # 500,000 is in the wallet but 200,000 of it is already spoken for.
    assert preview.lines[0].unspent_minor == 500_000
    assert preview.lines[0].sweep_minor == 300_000
    assert preview.lines[0].limited_by_available


def test_closing_a_month_that_was_never_planned_is_refused(
    close: CloseBudgetPeriod,
) -> None:
    with pytest.raises(BudgetPeriodError, match="no budget to close"):
        close.preview(year=2026, month=8, currency="TWD")
    with pytest.raises(BudgetPeriodError, match="no budget to close"):
        close.execute(year=2026, month=8, currency="TWD")


def test_a_closed_month_no_longer_accepts_a_target(
    plan: ManageBudgetPlan,
    close: CloseBudgetPeriod,
    world: dict[str, int],
) -> None:
    food = plan.add_category(AddCategoryRequest(name="飲食"))
    request = SetTargetRequest(
        year=2026,
        month=8,
        currency="TWD",
        budget_category_id=food.id,
        target_minor=100_000,
        buffer_minor=0,
        funding_wallet_id=world["food_wallet_id"],
    )
    saved = plan.set_target(request)
    close.execute(year=2026, month=8, currency="TWD")

    with pytest.raises(BudgetPeriodError, match="closed month"):
        plan.set_target(request)
    assert saved.target_minor == 100_000


def test_categories_are_validated_renamed_and_deactivated_optimistically(
    plan: ManageBudgetPlan,
    world: dict[str, int],
) -> None:
    food = plan.add_category(AddCategoryRequest(name="飲食"))
    child = plan.add_category(AddCategoryRequest(name="外食", parent_id=food.id))

    with pytest.raises(LedgerConfigurationError, match="already uses this name"):
        plan.add_category(AddCategoryRequest(name="飲食"))
    with pytest.raises(LedgerConfigurationError, match="needs a name"):
        plan.add_category(AddCategoryRequest(name="   "))
    with pytest.raises(LedgerConfigurationError, match="its own parent"):
        plan.update_category(
            UpdateCategoryRequest(
                category_id=food.id,
                expected_version=food.version,
                name="飲食",
                parent_id=food.id,
                is_active=True,
            )
        )
    with pytest.raises(LedgerConfigurationError, match="its own descendant"):
        plan.update_category(
            UpdateCategoryRequest(
                category_id=food.id,
                expected_version=food.version,
                name="飲食",
                parent_id=child.id,
                is_active=True,
            )
        )

    renamed = plan.update_category(
        UpdateCategoryRequest(
            category_id=child.id,
            expected_version=child.version,
            name="外食與飲料",
            parent_id=food.id,
            is_active=False,
        )
    )

    assert renamed.name == "外食與飲料"
    assert not renamed.is_active
    with pytest.raises(ConcurrentUpdateError):
        plan.update_category(
            UpdateCategoryRequest(
                category_id=child.id,
                expected_version=child.version,
                name="又改一次",
                parent_id=food.id,
                is_active=True,
            )
        )
    # An inactive category can no longer be given new budget.
    with pytest.raises(LedgerConfigurationError, match="inactive"):
        plan.set_target(
            SetTargetRequest(
                year=2026,
                month=8,
                currency="TWD",
                budget_category_id=child.id,
                target_minor=1_000,
                buffer_minor=0,
                funding_wallet_id=world["food_wallet_id"],
            )
        )


def test_a_target_must_be_funded_from_a_wallet_in_the_same_currency(
    factory: UnitOfWorkFactory,
    plan: ManageBudgetPlan,
    world: dict[str, int],
) -> None:
    setup = LedgerSetupService(factory)
    usd = setup.open_account(
        OpenFinancialAccountRequest(
            institution_code="test_bank",
            display_name="美金帳戶",
            account_type=FinancialAccountType.SAVINGS,
            currency="USD",
        )
    )
    usd_wallet = setup.add_wallet(
        AddWalletRequest(financial_account_id=usd.account.id, name="美金儲蓄")
    )
    food = plan.add_category(AddCategoryRequest(name="飲食"))

    with pytest.raises(LedgerConfigurationError, match="same currency"):
        plan.set_target(
            SetTargetRequest(
                year=2026,
                month=8,
                currency="TWD",
                budget_category_id=food.id,
                target_minor=1_000,
                buffer_minor=0,
                funding_wallet_id=usd_wallet.id,
            )
        )
    with pytest.raises(LedgerConfigurationError, match="same currency"):
        plan.set_target(
            SetTargetRequest(
                year=2026,
                month=8,
                currency="TWD",
                budget_category_id=food.id,
                target_minor=1_000,
                buffer_minor=0,
                funding_wallet_id=world["food_wallet_id"],
                month_end_destination_wallet_id=usd_wallet.id,
            )
        )


def test_a_target_rejects_float_and_negative_amounts(
    plan: ManageBudgetPlan,
    world: dict[str, int],
) -> None:
    food = plan.add_category(AddCategoryRequest(name="飲食"))

    def _request(target: object, buffer: object) -> SetTargetRequest:
        return SetTargetRequest(
            year=2026,
            month=8,
            currency="TWD",
            budget_category_id=food.id,
            target_minor=target,  # type: ignore[arg-type]
            buffer_minor=buffer,  # type: ignore[arg-type]
            funding_wallet_id=world["food_wallet_id"],
        )

    for target, buffer in ((1000.5, 0), (True, 0), (1000, 0.5), (1000, True)):
        with pytest.raises(TypeError):
            plan.set_target(_request(target, buffer))
    for target, buffer in ((-1, 0), (1000, -1)):
        with pytest.raises(ValueError):
            plan.set_target(_request(target, buffer))


def test_a_buffer_already_eaten_by_overspend_is_not_withheld_again(
    factory: UnitOfWorkFactory,
    plan: ManageBudgetPlan,
    close: CloseBudgetPeriod,
    budget: BudgetQueries,
    world: dict[str, int],
) -> None:
    """An overspend spends the cushion; the close must not reserve it twice."""
    food = plan.add_category(AddCategoryRequest(name="飲食"))
    travel = plan.add_category(AddCategoryRequest(name="旅遊"))
    plan.set_target(
        SetTargetRequest(
            year=2026,
            month=8,
            currency="TWD",
            budget_category_id=food.id,
            target_minor=300_000,
            buffer_minor=100_000,
            funding_wallet_id=world["food_wallet_id"],
        )
    )
    plan.set_target(
        SetTargetRequest(
            year=2026,
            month=8,
            currency="TWD",
            budget_category_id=travel.id,
            target_minor=100_000,
            buffer_minor=0,
            funding_wallet_id=world["food_wallet_id"],
            month_end_destination_wallet_id=world["savings_wallet_id"],
        )
    )
    _spend(
        factory,
        key="manual:1",
        amount_minor=350_000,
        wallet_id=world["food_wallet_id"],
        category_id=food.id,
    )

    preview = close.preview(year=2026, month=8, currency="TWD")
    lines = {line.category_name: line for line in preview.lines}

    # 100,000 buffer minus a 50,000 overspend leaves 50,000 still withheld.
    assert lines["飲食"].overspend_minor == 50_000
    assert lines["飲食"].remaining_buffer_minor == 50_000
    assert lines["飲食"].sweep_minor == 0
    # The wallet holds 150,000, 50,000 of it is the surviving cushion, so all
    # 100,000 of the untouched target can still be swept.
    assert lines["旅遊"].unspent_minor == 100_000
    assert lines["旅遊"].sweep_minor == 100_000
    assert not lines["旅遊"].limited_by_available

    close.execute(year=2026, month=8, currency="TWD")

    wallets = {
        wallet.wallet_id: wallet.balance_minor
        for wallet in budget.month(year=2026, month=8, currency="TWD").wallets
    }
    assert wallets[world["food_wallet_id"]] == 50_000
    assert wallets[world["savings_wallet_id"]] == 200_000


def test_the_page_shows_both_the_buffer_target_and_what_is_left_of_it(
    factory: UnitOfWorkFactory,
    plan: ManageBudgetPlan,
    budget: BudgetQueries,
    world: dict[str, int],
) -> None:
    food = plan.add_category(AddCategoryRequest(name="飲食"))
    plan.set_target(
        SetTargetRequest(
            year=2026,
            month=8,
            currency="TWD",
            budget_category_id=food.id,
            target_minor=100_000,
            buffer_minor=40_000,
            funding_wallet_id=world["food_wallet_id"],
        )
    )
    _spend(
        factory,
        key="manual:1",
        amount_minor=125_000,
        wallet_id=world["food_wallet_id"],
        category_id=food.id,
    )

    view = budget.month(year=2026, month=8, currency="TWD")

    assert view.total_buffer_minor == 40_000
    assert view.total_buffer_remaining_minor == 15_000
    assert view.categories[0].overspend_minor == 25_000


def test_spending_without_a_target_or_a_category_is_still_counted(
    factory: UnitOfWorkFactory,
    plan: ManageBudgetPlan,
    budget: BudgetQueries,
    world: dict[str, int],
) -> None:
    """A budget that quietly omits spending is worse than no budget."""
    food = plan.add_category(AddCategoryRequest(name="飲食"))
    unplanned = plan.add_category(AddCategoryRequest(name="雜項"))
    plan.set_target(
        SetTargetRequest(
            year=2026,
            month=8,
            currency="TWD",
            budget_category_id=food.id,
            target_minor=100_000,
            buffer_minor=0,
            funding_wallet_id=world["food_wallet_id"],
        )
    )
    _spend(
        factory,
        key="manual:1",
        amount_minor=30_000,
        wallet_id=world["food_wallet_id"],
        category_id=food.id,
    )
    _spend(
        factory,
        key="manual:2",
        amount_minor=20_000,
        wallet_id=world["food_wallet_id"],
        category_id=unplanned.id,
    )
    _spend(
        factory,
        key="manual:3",
        amount_minor=5_000,
        wallet_id=world["food_wallet_id"],
        category_id=None,
    )

    view = budget.month(year=2026, month=8, currency="TWD")
    rows = {item.name: item for item in view.categories}

    assert view.total_spend_minor == 55_000
    assert view.unplanned_spend_minor == 25_000
    assert view.total_target_minor == 100_000
    assert rows["飲食"].is_planned
    assert not rows["雜項 · 未編列"].is_planned
    assert rows["雜項 · 未編列"].net_spend_minor == 20_000
    assert rows["未分類"].net_spend_minor == 5_000
    assert rows["未分類"].budget_category_id is None


def test_a_month_with_spending_but_no_budget_period_still_reports_it(
    factory: UnitOfWorkFactory,
    plan: ManageBudgetPlan,
    budget: BudgetQueries,
    world: dict[str, int],
) -> None:
    food = plan.add_category(AddCategoryRequest(name="飲食"))
    _spend(
        factory,
        key="manual:1",
        amount_minor=40_000,
        wallet_id=world["food_wallet_id"],
        category_id=food.id,
    )

    view = budget.month(year=2026, month=8, currency="TWD")

    assert view.period is None
    assert view.total_target_minor == 0
    assert view.total_spend_minor == 40_000
    assert view.unplanned_spend_minor == 40_000


def test_an_existing_target_can_be_corrected_with_its_version(
    plan: ManageBudgetPlan,
    budget: BudgetQueries,
    world: dict[str, int],
) -> None:
    food = plan.add_category(AddCategoryRequest(name="飲食"))
    created = plan.set_target(
        SetTargetRequest(
            year=2026,
            month=8,
            currency="TWD",
            budget_category_id=food.id,
            target_minor=100_000,
            buffer_minor=0,
            funding_wallet_id=world["food_wallet_id"],
        )
    )

    row = budget.month(year=2026, month=8, currency="TWD").categories[0]

    # The page carries the identity a correction needs.
    assert row.target_id == created.id
    assert row.target_version == created.version

    corrected = plan.set_target(
        SetTargetRequest(
            year=2026,
            month=8,
            currency="TWD",
            budget_category_id=food.id,
            target_minor=250_000,
            buffer_minor=10_000,
            funding_wallet_id=world["food_wallet_id"],
            expected_version=row.target_version,
        )
    )

    assert corrected.target_minor == 250_000
    assert corrected.version == created.version + 1
    with pytest.raises(ConcurrentUpdateError):
        plan.set_target(
            SetTargetRequest(
                year=2026,
                month=8,
                currency="TWD",
                budget_category_id=food.id,
                target_minor=1,
                buffer_minor=0,
                funding_wallet_id=world["food_wallet_id"],
                expected_version=row.target_version,
            )
        )

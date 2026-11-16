"""B5 acceptance tests: salary receipt, generation, approval, and transfers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime

import pytest
from app.application.queries.money import MoneyBalanceQueries
from app.application.queries.salary import SalaryQueries
from app.application.services.budget_planning import (
    AddCategoryRequest,
    ManageBudgetPlan,
)
from app.application.services.ledger import (
    AddWalletRequest,
    JournalService,
    LedgerSetupService,
    OpenFinancialAccountRequest,
)
from app.application.services.salary_allocation import (
    AddSalaryTemplateItemRequest,
    AddSalaryTemplateRequest,
    ApproveSalaryAllocation,
    ApproveSalaryRunRequest,
    CancelSalaryDraftRequest,
    DeactivateSalaryTemplateItemRequest,
    GenerateSalaryAllocation,
    GenerateSalaryRunRequest,
    GenerateSalaryRunResult,
    ManageSalaryTemplates,
    MarkSalaryTransferred,
    MarkSalaryTransferredRequest,
    RecordSalaryReceipt,
    RegenerateSalaryRunRequest,
    SalaryReceiptRequest,
    SalaryReceiptResult,
    UpdateSalaryTemplateItemRequest,
)
from app.application.services.subscriptions import (
    AddPricePeriodRequest,
    AddSubscriptionRequest,
    ManageSubscriptions,
    UpdatePricePeriodRequest,
)
from app.domain.budget_enums import (
    CommitmentFulfillmentMode,
    FinancialAccountType,
    FinancialEventStatus,
    JournalEntryType,
    SalaryCalculationMode,
    SalaryRunItemStatus,
    SalaryRunStatus,
    SubscriptionCadence,
    SubscriptionRequirementReason,
)
from app.domain.budget_models import (
    ManualSalaryAmount,
    SalaryAllocationRunItem,
)
from app.domain.budget_rules import deserialize_subscription_evidence
from app.domain.errors import (
    BudgetPeriodError,
    ConcurrentUpdateError,
    CurrencyMismatchError,
    IdempotencyConflictError,
    InsufficientAvailableFundsError,
    SalaryAllocationError,
    SalaryEventError,
    SalaryPlanError,
)
from app.domain.ports.repositories import UnitOfWork
from app.infrastructure.database.budget_queries import (
    SqlAlchemyLedgerBalanceReadRepository,
)
from app.infrastructure.database.engine import Database
from app.infrastructure.database.salary_queries import SqlAlchemySalaryReadRepository
from app.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork

type UnitOfWorkFactory = Callable[[], UnitOfWork]


def _must[T](value: T | None) -> T:
    """Return a row this test has just written, so the type stays narrow."""
    assert value is not None
    return value


SALARY_AT = datetime(2026, 8, 5, 1, 0, tzinfo=UTC)
TRANSFER_AT = datetime(2026, 8, 6, 2, 30, tzinfo=UTC)
NOW = datetime(2026, 8, 5, 3, 0, tzinfo=UTC)
SALARY_MINOR = 100_000
LIVING_MINOR = 30_000
SUBSCRIPTION_PRICE_MINOR = 39_000


@dataclass(frozen=True)
class World:
    """One complete, entirely fictional salary setup."""

    source_account_id: int
    source_wallet_id: int
    living_wallet_id: int
    same_bank_account_id: int
    rent_wallet_id: int
    other_bank_account_id: int
    subscription_wallet_id: int
    category_id: int
    template_id: int
    fixed_item_id: int
    subscription_item_id: int
    manual_item_id: int


@pytest.fixture
def factory(migrated_database: Database) -> UnitOfWorkFactory:
    """Return a unit-of-work factory bound to the migrated test database."""

    def build() -> UnitOfWork:
        return SqlAlchemyUnitOfWork(migrated_database.session_factory)

    return build


@pytest.fixture
def templates(factory: UnitOfWorkFactory) -> ManageSalaryTemplates:
    return ManageSalaryTemplates(factory)


@pytest.fixture
def receipts(factory: UnitOfWorkFactory) -> RecordSalaryReceipt:
    return RecordSalaryReceipt(
        factory, JournalService(factory, clock=lambda: NOW), clock=lambda: NOW
    )


@pytest.fixture
def generate(factory: UnitOfWorkFactory) -> GenerateSalaryAllocation:
    return GenerateSalaryAllocation(factory, clock=lambda: NOW)


@pytest.fixture
def approve(factory: UnitOfWorkFactory) -> ApproveSalaryAllocation:
    return ApproveSalaryAllocation(
        factory, JournalService(factory, clock=lambda: NOW), clock=lambda: NOW
    )


@pytest.fixture
def transfer(factory: UnitOfWorkFactory) -> MarkSalaryTransferred:
    return MarkSalaryTransferred(factory, JournalService(factory, clock=lambda: NOW))


@pytest.fixture
def balances(migrated_database: Database) -> MoneyBalanceQueries:
    return MoneyBalanceQueries(
        SqlAlchemyLedgerBalanceReadRepository(migrated_database.session_factory)
    )


@pytest.fixture
def salary_queries(migrated_database: Database) -> SalaryQueries:
    return SalaryQueries(
        SqlAlchemySalaryReadRepository(migrated_database.session_factory)
    )


@pytest.fixture
def world(factory: UnitOfWorkFactory, templates: ManageSalaryTemplates) -> World:
    """Seed three accounts, four wallets, one subscription, and one template."""
    setup = LedgerSetupService(factory)
    plan = ManageBudgetPlan(factory)
    subscriptions = ManageSubscriptions(factory)

    source = setup.open_account(
        OpenFinancialAccountRequest(
            institution_code="test_bank_a",
            display_name="測試薪轉帳戶",
            account_type=FinancialAccountType.CHECKING,
            currency="TWD",
        )
    )
    living = setup.add_wallet(
        AddWalletRequest(financial_account_id=source.account.id, name="測試生活費錢包")
    )
    # Same institution, different account: still a real bank transfer.
    same_bank = setup.open_account(
        OpenFinancialAccountRequest(
            institution_code="test_bank_a",
            display_name="測試同行第二帳戶",
            account_type=FinancialAccountType.SAVINGS,
            currency="TWD",
        )
    )
    rent = setup.add_wallet(
        AddWalletRequest(financial_account_id=same_bank.account.id, name="測試房租錢包")
    )
    other_bank = setup.open_account(
        OpenFinancialAccountRequest(
            institution_code="test_bank_b",
            display_name="測試訂閱帳戶",
            account_type=FinancialAccountType.CHECKING,
            currency="TWD",
        )
    )
    subscription_wallet = setup.add_wallet(
        AddWalletRequest(
            financial_account_id=other_bank.account.id, name="測試訂閱錢包"
        )
    )
    category = plan.add_category(AddCategoryRequest(name="測試分類"))
    subscription = subscriptions.add(
        AddSubscriptionRequest(
            name="測試影音",
            budget_category_id=category.id,
            funding_wallet_id=subscription_wallet.id,
            currency="TWD",
            billing_cadence=SubscriptionCadence.MONTHLY,
            cadence_months=1,
        )
    )
    subscriptions.add_price(
        AddPricePeriodRequest(
            subscription_id=subscription.id,
            amount_minor=SUBSCRIPTION_PRICE_MINOR,
            effective_from=date(2026, 1, 1),
        )
    )
    template = templates.add_template(
        AddSalaryTemplateRequest(
            name="測試薪資範本", currency="TWD", source_account_id=source.account.id
        )
    )
    fixed = templates.add_item(
        AddSalaryTemplateItemRequest(
            template_id=template.id,
            name="測試生活費",
            destination_wallet_id=living.id,
            calculation_mode=SalaryCalculationMode.FIXED,
            fixed_amount_minor=LIVING_MINOR,
            budget_category_id=category.id,
            display_order=0,
        )
    )
    subscription_item = templates.add_item(
        AddSalaryTemplateItemRequest(
            template_id=template.id,
            name="測試訂閱",
            destination_wallet_id=subscription_wallet.id,
            calculation_mode=SalaryCalculationMode.SUBSCRIPTION_REQUIREMENT,
            display_order=1,
        )
    )
    manual = templates.add_item(
        AddSalaryTemplateItemRequest(
            template_id=template.id,
            name="測試房租",
            destination_wallet_id=rent.id,
            calculation_mode=SalaryCalculationMode.MANUAL,
            display_order=2,
        )
    )
    assert source.unallocated_wallet is not None
    return World(
        source_account_id=source.account.id,
        source_wallet_id=source.unallocated_wallet.id,
        living_wallet_id=living.id,
        same_bank_account_id=same_bank.account.id,
        rent_wallet_id=rent.id,
        other_bank_account_id=other_bank.account.id,
        subscription_wallet_id=subscription_wallet.id,
        category_id=category.id,
        template_id=template.id,
        fixed_item_id=fixed.id,
        subscription_item_id=subscription_item.id,
        manual_item_id=manual.id,
    )


def _wallet_balance(
    balances: MoneyBalanceQueries, account_id: int, wallet_id: int
) -> int:
    for account in balances.account_balances():
        if account.financial_account_id != account_id:
            continue
        for wallet in account.wallets:
            if wallet.wallet_id == wallet_id:
                return wallet.balance_minor
    raise AssertionError("wallet was not found")


def _account_balance(balances: MoneyBalanceQueries, account_id: int) -> int:
    for account in balances.account_balances():
        if account.financial_account_id == account_id:
            return account.balance_minor
    raise AssertionError("account was not found")


def _receive(
    receipts: RecordSalaryReceipt,
    world: World,
    *,
    token: str = "token-1",
    amount_minor: int = SALARY_MINOR,
    occurred_at: datetime = SALARY_AT,
) -> SalaryReceiptResult:
    return receipts.execute(
        SalaryReceiptRequest(
            template_id=world.template_id,
            amount_minor=amount_minor,
            occurred_at=occurred_at,
            client_token=token,
        )
    )


def _generate(
    generate: GenerateSalaryAllocation,
    world: World,
    event_id: int,
    *,
    manual_minor: int = 20_000,
    year: int = 2026,
    month: int = 8,
) -> GenerateSalaryRunResult:
    return generate.execute(
        GenerateSalaryRunRequest(
            template_id=world.template_id,
            year=year,
            month=month,
            salary_event_id=event_id,
            manual_amounts=(
                ManualSalaryAmount(
                    template_item_id=world.manual_item_id, amount_minor=manual_minor
                ),
            ),
        )
    )


def _item_by_name(
    items: tuple[SalaryAllocationRunItem, ...], name: str
) -> SalaryAllocationRunItem:
    return next(item for item in items if item.name_snapshot == name)


def _stop_the_subscription(factory: UnitOfWorkFactory) -> None:
    """End the seeded subscription from August, so it requires nothing then."""
    from app.application.services.subscriptions import DeactivateSubscriptionRequest

    subscriptions = ManageSubscriptions(factory)
    subscription = next(
        item for item in subscriptions.list_all() if item.name == "測試影音"
    )
    subscriptions.deactivate(
        DeactivateSubscriptionRequest(
            subscription_id=subscription.id,
            expected_version=subscription.version,
            active_to=date(2026, 8, 1),
        )
    )


def _reserve_from_the_source_wallet(
    factory: UnitOfWorkFactory, world: World, amount_minor: int
) -> None:
    """Reserve money in the salary wallet the way a card purchase does (B3)."""
    from app.domain.budget_enums import (
        CommitmentStatus,
        FinancialEventOrigin,
        FinancialEventType,
        PaymentMethod,
        RecognitionBasis,
    )
    from app.domain.budget_models import (
        EventAllocationDraft,
        FinancialEventDraft,
        FundingCommitmentDraft,
    )

    with factory() as unit:
        event = unit.financial_events.add(
            FinancialEventDraft(
                origin=FinancialEventOrigin.MANUAL,
                event_type=FinancialEventType.PURCHASE,
                payment_method=PaymentMethod.CREDIT_CARD,
                recognition_basis=RecognitionBasis.OBSERVED_MANUAL,
                occurred_at=SALARY_AT,
                amount_minor=amount_minor,
                currency="TWD",
                idempotency_key="card-reservation",
            )
        )
        allocation = unit.allocations.add(
            EventAllocationDraft(
                financial_event_id=event.id,
                funding_wallet_id=world.source_wallet_id,
                amount_minor=amount_minor,
                currency="TWD",
            )
        )
        unit.commitments.add(
            FundingCommitmentDraft(
                financial_event_id=event.id,
                event_allocation_id=allocation.id,
                source_wallet_id=world.source_wallet_id,
                settlement_account_id=world.other_bank_account_id,
                amount_minor=amount_minor,
                currency="TWD",
                fulfillment_mode=CommitmentFulfillmentMode.CROSS_BANK_TRANSFER,
                status=CommitmentStatus.OPEN,
            )
        )
        unit.commit()


# --- C. Salary receipt -----------------------------------------------------


def test_recording_a_salary_posts_one_balanced_asset_and_income_entry(
    receipts: RecordSalaryReceipt,
    balances: MoneyBalanceQueries,
    world: World,
    factory: UnitOfWorkFactory,
) -> None:
    result = _receive(receipts, world)

    assert result.created is True
    assert result.entry.entry_type is JournalEntryType.SALARY_RECEIPT
    assert result.entry.financial_event_id == result.event.id
    assert result.event.status is FinancialEventStatus.POSTED
    assert sum(posting.amount_minor for posting in result.entry.postings) == 0
    assert (
        _wallet_balance(balances, world.source_account_id, world.source_wallet_id)
        == SALARY_MINOR
    )
    assert _account_balance(balances, world.source_account_id) == SALARY_MINOR
    with factory() as unit:
        income = unit.ledger_accounts.get_by_code("income:salary:TWD")
        assert income is not None
        assert unit.journal.get_by_financial_event(result.event.id) is not None


def test_replaying_the_same_salary_token_returns_the_first_entry(
    receipts: RecordSalaryReceipt, balances: MoneyBalanceQueries, world: World
) -> None:
    first = _receive(receipts, world)
    second = _receive(receipts, world)

    assert second.created is False
    assert second.event.id == first.event.id
    assert second.entry.id == first.entry.id
    assert _account_balance(balances, world.source_account_id) == SALARY_MINOR


@pytest.mark.parametrize(
    "overrides",
    [
        {"amount_minor": SALARY_MINOR + 1},
        {"occurred_at": datetime(2026, 8, 5, 2, 0, tzinfo=UTC)},
    ],
)
def test_the_same_token_with_different_money_is_a_conflict(
    receipts: RecordSalaryReceipt, world: World, overrides: dict[str, object]
) -> None:
    _receive(receipts, world)
    with pytest.raises(IdempotencyConflictError):
        _receive(receipts, world, **overrides)  # type: ignore[arg-type]


def test_the_same_token_for_another_account_is_a_conflict(
    receipts: RecordSalaryReceipt,
    templates: ManageSalaryTemplates,
    world: World,
) -> None:
    _receive(receipts, world)
    other = templates.add_template(
        AddSalaryTemplateRequest(
            name="另一個範本",
            currency="TWD",
            source_account_id=world.other_bank_account_id,
        )
    )
    with pytest.raises(IdempotencyConflictError):
        receipts.execute(
            SalaryReceiptRequest(
                template_id=other.id,
                amount_minor=SALARY_MINOR,
                occurred_at=SALARY_AT,
                client_token="token-1",
            )
        )


def test_a_failed_salary_receipt_leaves_no_event_ledger_or_entry(
    receipts: RecordSalaryReceipt, world: World, factory: UnitOfWorkFactory
) -> None:
    with pytest.raises(SalaryPlanError):
        receipts.execute(
            SalaryReceiptRequest(
                template_id=world.template_id,
                source_account_id=world.other_bank_account_id,
                amount_minor=SALARY_MINOR,
                occurred_at=SALARY_AT,
                client_token="token-rollback",
            )
        )
    with factory() as unit:
        assert (
            unit.financial_events.get_by_idempotency_key(
                "salary_receipt:token-rollback"
            )
            is None
        )
        assert unit.ledger_accounts.get_by_code("income:salary:TWD") is None


@pytest.mark.parametrize("amount", [0, -1, 1.5, True, "1000"])
def test_a_salary_receipt_refuses_an_amount_that_is_not_positive_minor_units(
    receipts: RecordSalaryReceipt, world: World, amount: object
) -> None:
    with pytest.raises((TypeError, ValueError)):
        _receive(receipts, world, token="token-bad", amount_minor=amount)  # type: ignore[arg-type]


def test_a_salary_receipt_refuses_a_naive_instant_and_an_empty_token(
    receipts: RecordSalaryReceipt, world: World
) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _receive(receipts, world, token="t", occurred_at=datetime(2026, 8, 5, 1, 0))
    with pytest.raises(ValueError, match="idempotency token"):
        _receive(receipts, world, token="   ")


def test_a_salary_receipt_refuses_an_account_without_an_unallocated_wallet(
    receipts: RecordSalaryReceipt,
    factory: UnitOfWorkFactory,
    world: World,
) -> None:
    setup = LedgerSetupService(factory)
    cash = setup.open_account(
        OpenFinancialAccountRequest(
            institution_code="test_bank_c",
            display_name="測試卡片負債",
            account_type=FinancialAccountType.CARD_LIABILITY,
            currency="TWD",
        )
    )
    with pytest.raises(SalaryPlanError, match="asset account"):
        receipts.execute(
            SalaryReceiptRequest(
                source_account_id=cash.account.id,
                amount_minor=SALARY_MINOR,
                occurred_at=SALARY_AT,
                client_token="token-cash",
            )
        )


def test_the_salary_services_write_nothing_to_the_application_log(
    receipts: RecordSalaryReceipt,
    generate: GenerateSalaryAllocation,
    approve: ApproveSalaryAllocation,
    world: World,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(0):
        received = _receive(receipts, world)
        run = _generate(generate, world, received.event.id)
        approve.execute(
            ApproveSalaryRunRequest(run_id=run.run.id, expected_version=run.run.version)
        )

    assert caplog.records == []


# --- D. Generation ---------------------------------------------------------


def test_generation_freezes_every_mode_and_the_exact_remainder(
    receipts: RecordSalaryReceipt,
    generate: GenerateSalaryAllocation,
    world: World,
) -> None:
    received = _receive(receipts, world)

    result = _generate(generate, world, received.event.id, manual_minor=20_000)

    run = result.run
    assert result.created is True
    assert run.status is SalaryRunStatus.DRAFT
    assert run.generation == 1
    assert run.salary_amount_minor == SALARY_MINOR
    assert run.planned_amount_minor == LIVING_MINOR + SUBSCRIPTION_PRICE_MINOR + 20_000
    assert run.remainder_minor == SALARY_MINOR - run.planned_amount_minor
    assert run.planned_amount_minor + run.remainder_minor == run.salary_amount_minor

    living = _item_by_name(result.items, "測試生活費")
    subscription = _item_by_name(result.items, "測試訂閱")
    rent = _item_by_name(result.items, "測試房租")
    assert living.amount_minor == LIVING_MINOR
    assert living.fulfillment_mode is CommitmentFulfillmentMode.RESERVE_ONLY
    assert subscription.amount_minor == SUBSCRIPTION_PRICE_MINOR
    assert (
        subscription.fulfillment_mode is CommitmentFulfillmentMode.CROSS_BANK_TRANSFER
    )
    assert rent.amount_minor == 20_000
    # Same institution, different account: still a transfer the user must make.
    assert rent.fulfillment_mode is CommitmentFulfillmentMode.ACCOUNT_TRANSFER
    assert rent.destination_account_id == world.same_bank_account_id
    assert subscription.destination_wallet_name_snapshot == "測試訂閱錢包"
    assert subscription.destination_account_name_snapshot == "測試訂閱帳戶"
    assert living.budget_category_name_snapshot == "測試分類"


def test_a_subscription_line_takes_exactly_its_wallets_contributions(
    receipts: RecordSalaryReceipt,
    generate: GenerateSalaryAllocation,
    factory: UnitOfWorkFactory,
    world: World,
) -> None:
    """A subscription funded from another wallet must not land on this line."""
    subscriptions = ManageSubscriptions(factory)
    subscriptions.add(
        AddSubscriptionRequest(
            name="測試雲端",
            budget_category_id=world.category_id,
            funding_wallet_id=world.living_wallet_id,
            currency="TWD",
            billing_cadence=SubscriptionCadence.MONTHLY,
            cadence_months=1,
        )
    )
    received = _receive(receipts, world)

    # The living wallet has no subscription line, so its requirement is
    # uncovered and the whole generation is refused rather than silently
    # dropped.
    subscription = next(
        item for item in subscriptions.list_all() if item.name == "測試雲端"
    )
    subscriptions.add_price(
        AddPricePeriodRequest(
            subscription_id=subscription.id,
            amount_minor=1_000,
            effective_from=date(2026, 1, 1),
        )
    )
    with pytest.raises(SalaryAllocationError, match="no line for"):
        _generate(generate, world, received.event.id)


def test_a_zero_contribution_stays_in_the_snapshot_with_its_reason(
    receipts: RecordSalaryReceipt,
    generate: GenerateSalaryAllocation,
    factory: UnitOfWorkFactory,
    world: World,
) -> None:
    subscriptions = ManageSubscriptions(factory)
    subscriptions.add(
        AddSubscriptionRequest(
            name="測試無價格",
            budget_category_id=world.category_id,
            funding_wallet_id=world.subscription_wallet_id,
            currency="TWD",
            billing_cadence=SubscriptionCadence.MONTHLY,
            cadence_months=1,
        )
    )
    received = _receive(receipts, world)

    result = _generate(generate, world, received.event.id)

    evidence = deserialize_subscription_evidence(result.run.subscription_evidence_json)
    zero = next(item for item in evidence.items if item.name == "測試無價格")
    assert zero.contribution_minor == 0
    assert zero.reason is SubscriptionRequirementReason.NO_PRICE_FOR_MONTH
    assert isinstance(zero.contribution_minor, int)
    assert evidence.total_minor == SUBSCRIPTION_PRICE_MINOR
    assert (
        _item_by_name(result.items, "測試訂閱").amount_minor == SUBSCRIPTION_PRICE_MINOR
    )


def test_a_missing_or_unexpected_manual_amount_is_refused(
    receipts: RecordSalaryReceipt,
    generate: GenerateSalaryAllocation,
    world: World,
    factory: UnitOfWorkFactory,
) -> None:
    received = _receive(receipts, world)

    with pytest.raises(SalaryAllocationError, match="needs an explicit amount"):
        generate.execute(
            GenerateSalaryRunRequest(
                template_id=world.template_id,
                year=2026,
                month=8,
                salary_event_id=received.event.id,
            )
        )
    with pytest.raises(SalaryAllocationError, match="does not have"):
        generate.execute(
            GenerateSalaryRunRequest(
                template_id=world.template_id,
                year=2026,
                month=8,
                salary_event_id=received.event.id,
                manual_amounts=(
                    ManualSalaryAmount(
                        template_item_id=world.manual_item_id, amount_minor=1
                    ),
                    ManualSalaryAmount(
                        template_item_id=world.fixed_item_id, amount_minor=1
                    ),
                ),
            )
        )
    with factory() as unit:
        assert unit.salary_runs.find_live_by_salary_event(received.event.id) is None


def test_a_plan_larger_than_the_salary_writes_no_run_or_item(
    receipts: RecordSalaryReceipt,
    generate: GenerateSalaryAllocation,
    world: World,
    factory: UnitOfWorkFactory,
) -> None:
    received = _receive(receipts, world)

    with pytest.raises(SalaryAllocationError, match="larger than this salary"):
        _generate(generate, world, received.event.id, manual_minor=SALARY_MINOR)

    with factory() as unit:
        assert unit.salary_runs.find_live_by_salary_event(received.event.id) is None
        period = unit.budget_periods.find(year=2026, month=8, currency="TWD")
        assert period is None or unit.salary_runs.list_for_period(period.id) == ()


def test_changing_the_template_or_a_price_never_changes_a_frozen_run(
    receipts: RecordSalaryReceipt,
    generate: GenerateSalaryAllocation,
    templates: ManageSalaryTemplates,
    factory: UnitOfWorkFactory,
    salary_queries: SalaryQueries,
    world: World,
) -> None:
    received = _receive(receipts, world)
    result = _generate(generate, world, received.event.id)
    before = salary_queries.run(result.run.id)
    assert before is not None

    subscriptions = ManageSubscriptions(factory)
    subscription = next(
        item for item in subscriptions.list_all() if item.name == "測試影音"
    )
    price = subscriptions.list_prices(subscription.id)[0]
    subscriptions.update_price(
        UpdatePricePeriodRequest(
            price_period_id=price.id,
            expected_version=price.version,
            amount_minor=99_000,
            effective_from=price.effective_from,
            effective_to=price.effective_to,
        )
    )
    templates.update_item(
        UpdateSalaryTemplateItemRequest(
            template_item_id=world.fixed_item_id,
            expected_version=1,
            name="測試生活費",
            destination_wallet_id=world.living_wallet_id,
            calculation_mode=SalaryCalculationMode.FIXED,
            budget_category_id=world.category_id,
            fixed_amount_minor=1,
            display_order=0,
        )
    )

    after = salary_queries.run(result.run.id)
    assert after is not None
    assert after.run.planned_amount_minor == before.run.planned_amount_minor
    assert after.run.remainder_minor == before.run.remainder_minor
    assert after.run.subscription_evidence_json == before.run.subscription_evidence_json
    assert [item.item.amount_minor for item in after.items] == [
        item.item.amount_minor for item in before.items
    ]


def test_generating_again_replays_the_live_draft_without_recalculating(
    receipts: RecordSalaryReceipt,
    generate: GenerateSalaryAllocation,
    templates: ManageSalaryTemplates,
    world: World,
) -> None:
    received = _receive(receipts, world)
    first = _generate(generate, world, received.event.id)
    templates.update_item(
        UpdateSalaryTemplateItemRequest(
            template_item_id=world.fixed_item_id,
            expected_version=1,
            name="測試生活費",
            destination_wallet_id=world.living_wallet_id,
            calculation_mode=SalaryCalculationMode.FIXED,
            budget_category_id=world.category_id,
            fixed_amount_minor=1,
            display_order=0,
        )
    )

    second = _generate(generate, world, received.event.id)

    assert second.created is False
    assert second.run.id == first.run.id
    assert second.run.generation == 1
    assert second.run.planned_amount_minor == first.run.planned_amount_minor


def test_regenerating_cancels_the_old_draft_and_creates_the_next_generation(
    receipts: RecordSalaryReceipt,
    generate: GenerateSalaryAllocation,
    templates: ManageSalaryTemplates,
    factory: UnitOfWorkFactory,
    world: World,
) -> None:
    received = _receive(receipts, world)
    first = _generate(generate, world, received.event.id)
    templates.update_item(
        UpdateSalaryTemplateItemRequest(
            template_item_id=world.fixed_item_id,
            expected_version=1,
            name="測試生活費",
            destination_wallet_id=world.living_wallet_id,
            calculation_mode=SalaryCalculationMode.FIXED,
            budget_category_id=world.category_id,
            fixed_amount_minor=1_000,
            display_order=0,
        )
    )

    second = generate.regenerate(
        RegenerateSalaryRunRequest(
            run_id=first.run.id,
            expected_version=first.run.version,
            manual_amounts=(
                ManualSalaryAmount(
                    template_item_id=world.manual_item_id, amount_minor=20_000
                ),
            ),
        )
    )

    assert second.created is True
    assert second.run.generation == 2
    assert second.run.planned_amount_minor == 1_000 + SUBSCRIPTION_PRICE_MINOR + 20_000
    with factory() as unit:
        old = _must(unit.salary_runs.get(first.run.id))
        assert old.status is SalaryRunStatus.CANCELLED
        assert old.cancelled_at == NOW
        assert old.planned_amount_minor == first.run.planned_amount_minor
        assert len(unit.salary_run_items.list_for_run(old.id)) == 3

    # The identical request replays; the payload is part of what identifies it.
    replay = generate.regenerate(
        RegenerateSalaryRunRequest(
            run_id=first.run.id,
            expected_version=first.run.version,
            manual_amounts=(
                ManualSalaryAmount(
                    template_item_id=world.manual_item_id, amount_minor=20_000
                ),
            ),
        )
    )
    assert replay.created is False
    assert replay.run.id == second.run.id
    assert replay.run.regenerated_from_run_id == first.run.id


def test_a_failed_regeneration_leaves_the_old_draft_exactly_as_it_was(
    receipts: RecordSalaryReceipt,
    generate: GenerateSalaryAllocation,
    factory: UnitOfWorkFactory,
    world: World,
) -> None:
    received = _receive(receipts, world)
    first = _generate(generate, world, received.event.id)

    with pytest.raises(SalaryAllocationError, match="larger than this salary"):
        generate.regenerate(
            RegenerateSalaryRunRequest(
                run_id=first.run.id,
                expected_version=first.run.version,
                manual_amounts=(
                    ManualSalaryAmount(
                        template_item_id=world.manual_item_id,
                        amount_minor=SALARY_MINOR,
                    ),
                ),
            )
        )

    with factory() as unit:
        unchanged = _must(unit.salary_runs.get(first.run.id))
        assert unchanged.status is SalaryRunStatus.DRAFT
        assert unchanged.version == first.run.version
        assert unchanged.cancelled_at is None
        assert (
            unit.salary_runs.next_generation(
                template_id=world.template_id,
                budget_period_id=unchanged.budget_period_id,
            )
            == 2
        )


def test_a_stale_regeneration_is_refused(
    receipts: RecordSalaryReceipt,
    generate: GenerateSalaryAllocation,
    world: World,
) -> None:
    received = _receive(receipts, world)
    first = _generate(generate, world, received.event.id)
    with pytest.raises(ConcurrentUpdateError):
        generate.regenerate(
            RegenerateSalaryRunRequest(
                run_id=first.run.id,
                expected_version=first.run.version + 5,
                manual_amounts=(
                    ManualSalaryAmount(
                        template_item_id=world.manual_item_id, amount_minor=1
                    ),
                ),
            )
        )


def test_an_approved_run_is_never_regenerated(
    receipts: RecordSalaryReceipt,
    generate: GenerateSalaryAllocation,
    approve: ApproveSalaryAllocation,
    world: World,
) -> None:
    received = _receive(receipts, world)
    result = _generate(generate, world, received.event.id)
    approved = approve.execute(
        ApproveSalaryRunRequest(
            run_id=result.run.id, expected_version=result.run.version
        )
    )

    with pytest.raises(SalaryAllocationError, match="never regenerated"):
        generate.regenerate(
            RegenerateSalaryRunRequest(
                run_id=approved.run.id, expected_version=approved.run.version
            )
        )


def test_a_closed_month_refuses_a_new_salary_run(
    receipts: RecordSalaryReceipt,
    generate: GenerateSalaryAllocation,
    factory: UnitOfWorkFactory,
    world: World,
) -> None:
    received = _receive(receipts, world)
    with factory() as unit:
        period = unit.budget_periods.get_or_create(year=2026, month=8, currency="TWD")
        unit.budget_periods.close(period.id, closed_at=NOW)
        unit.commit()

    with pytest.raises(BudgetPeriodError, match="closed month"):
        _generate(generate, world, received.event.id)


def test_one_salary_cannot_feed_two_live_runs(
    receipts: RecordSalaryReceipt,
    generate: GenerateSalaryAllocation,
    templates: ManageSalaryTemplates,
    world: World,
) -> None:
    received = _receive(receipts, world)
    _generate(generate, world, received.event.id)
    second = templates.add_template(
        AddSalaryTemplateRequest(
            name="第二個範本", currency="TWD", source_account_id=world.source_account_id
        )
    )
    templates.add_item(
        AddSalaryTemplateItemRequest(
            template_id=second.id,
            name="訂閱",
            destination_wallet_id=world.subscription_wallet_id,
            calculation_mode=SalaryCalculationMode.SUBSCRIPTION_REQUIREMENT,
        )
    )

    with pytest.raises(SalaryEventError, match="already_used"):
        generate.execute(
            GenerateSalaryRunRequest(
                template_id=second.id,
                year=2026,
                month=8,
                salary_event_id=received.event.id,
            )
        )


def test_an_event_that_is_not_a_posted_salary_deposit_is_refused(
    generate: GenerateSalaryAllocation,
    factory: UnitOfWorkFactory,
    world: World,
) -> None:
    from app.domain.budget_enums import (
        FinancialEventOrigin,
        FinancialEventType,
        PaymentMethod,
        RecognitionBasis,
    )
    from app.domain.budget_models import FinancialEventDraft

    with factory() as unit:
        event = unit.financial_events.add(
            FinancialEventDraft(
                origin=FinancialEventOrigin.MANUAL,
                event_type=FinancialEventType.DEPOSIT,
                payment_method=PaymentMethod.BANK_ACCOUNT,
                recognition_basis=RecognitionBasis.OBSERVED_MANUAL,
                occurred_at=SALARY_AT,
                amount_minor=SALARY_MINOR,
                currency="TWD",
                idempotency_key="not-posted",
            )
        )
        unit.commit()

    with pytest.raises(SalaryEventError, match="not_posted"):
        _generate(generate, world, event.id)


def test_cancelling_a_draft_keeps_it_and_frees_the_month(
    receipts: RecordSalaryReceipt,
    generate: GenerateSalaryAllocation,
    factory: UnitOfWorkFactory,
    world: World,
) -> None:
    received = _receive(receipts, world)
    first = _generate(generate, world, received.event.id)

    cancelled = generate.cancel(
        CancelSalaryDraftRequest(
            run_id=first.run.id, expected_version=first.run.version
        )
    )

    assert cancelled.status is SalaryRunStatus.CANCELLED
    with factory() as unit:
        assert len(unit.salary_run_items.list_for_run(first.run.id)) == 3
        assert (
            unit.journal.get_by_idempotency_key(f"salary_allocation:run:{first.run.id}")
            is None
        )

    second = _generate(generate, world, received.event.id)
    assert second.created is True
    assert second.run.generation == 2


# --- E. Approval -----------------------------------------------------------


def test_approving_moves_same_account_wallets_without_changing_the_bank_total(
    receipts: RecordSalaryReceipt,
    generate: GenerateSalaryAllocation,
    approve: ApproveSalaryAllocation,
    balances: MoneyBalanceQueries,
    world: World,
) -> None:
    received = _receive(receipts, world)
    result = _generate(generate, world, received.event.id)

    approved = approve.execute(
        ApproveSalaryRunRequest(
            run_id=result.run.id, expected_version=result.run.version
        )
    )

    assert approved.created is True
    assert approved.entry is not None
    assert approved.entry.entry_type is JournalEntryType.INTERNAL_TRANSFER
    assert sum(posting.amount_minor for posting in approved.entry.postings) == 0
    assert approved.run.approval_entry_id == approved.entry.id
    # The salary account's total is untouched; only its wallets moved.
    assert _account_balance(balances, world.source_account_id) == SALARY_MINOR
    assert (
        _wallet_balance(balances, world.source_account_id, world.living_wallet_id)
        == LIVING_MINOR
    )
    assert (
        _wallet_balance(balances, world.source_account_id, world.source_wallet_id)
        == SALARY_MINOR - LIVING_MINOR
    )
    # Nothing left either other bank.
    assert _account_balance(balances, world.other_bank_account_id) == 0
    assert _account_balance(balances, world.same_bank_account_id) == 0


def test_a_cross_account_approval_only_reserves_and_never_credits_the_destination(
    receipts: RecordSalaryReceipt,
    generate: GenerateSalaryAllocation,
    approve: ApproveSalaryAllocation,
    balances: MoneyBalanceQueries,
    factory: UnitOfWorkFactory,
    world: World,
) -> None:
    received = _receive(receipts, world)
    result = _generate(generate, world, received.event.id)

    approved = approve.execute(
        ApproveSalaryRunRequest(
            run_id=result.run.id, expected_version=result.run.version
        )
    )

    subscription = _item_by_name(approved.items, "測試訂閱")
    rent = _item_by_name(approved.items, "測試房租")
    living = _item_by_name(approved.items, "測試生活費")
    assert subscription.status is SalaryRunItemStatus.WAITING_TRANSFER
    assert rent.status is SalaryRunItemStatus.WAITING_TRANSFER
    assert living.status is SalaryRunItemStatus.ALLOCATED
    assert approved.run.status is SalaryRunStatus.PARTIALLY_FUNDED
    assert (
        _wallet_balance(
            balances, world.other_bank_account_id, world.subscription_wallet_id
        )
        == 0
    )
    assert (
        _wallet_balance(balances, world.same_bank_account_id, world.rent_wallet_id) == 0
    )
    with factory() as unit:
        reserved = unit.budget_ledger.reserved_minor(world.source_wallet_id)
        balance = unit.budget_ledger.wallet_balance(world.source_wallet_id)
    assert reserved == SUBSCRIPTION_PRICE_MINOR + 20_000
    assert balance == SALARY_MINOR - LIVING_MINOR
    assert balance - reserved == result.run.remainder_minor


def test_a_plan_with_only_same_account_lines_is_funded_at_once(
    receipts: RecordSalaryReceipt,
    generate: GenerateSalaryAllocation,
    approve: ApproveSalaryAllocation,
    templates: ManageSalaryTemplates,
    factory: UnitOfWorkFactory,
    world: World,
) -> None:
    _stop_the_subscription(factory)
    for item_id in (world.subscription_item_id, world.manual_item_id):
        templates.deactivate_item(
            DeactivateSalaryTemplateItemRequest(
                template_item_id=item_id, expected_version=1
            )
        )
    received = _receive(receipts, world)
    result = generate.execute(
        GenerateSalaryRunRequest(
            template_id=world.template_id,
            year=2026,
            month=8,
            salary_event_id=received.event.id,
        )
    )

    approved = approve.execute(
        ApproveSalaryRunRequest(
            run_id=result.run.id, expected_version=result.run.version
        )
    )

    assert approved.run.status is SalaryRunStatus.FUNDED


def test_a_plan_with_only_cross_account_lines_is_merely_approved(
    receipts: RecordSalaryReceipt,
    generate: GenerateSalaryAllocation,
    approve: ApproveSalaryAllocation,
    templates: ManageSalaryTemplates,
    world: World,
) -> None:
    templates.deactivate_item(
        DeactivateSalaryTemplateItemRequest(
            template_item_id=world.fixed_item_id, expected_version=1
        )
    )
    received = _receive(receipts, world)
    result = _generate(generate, world, received.event.id)

    approved = approve.execute(
        ApproveSalaryRunRequest(
            run_id=result.run.id, expected_version=result.run.version
        )
    )

    assert approved.run.status is SalaryRunStatus.APPROVED
    assert approved.entry is None
    assert approved.run.approval_entry_id is None


def test_a_plan_of_only_zero_lines_is_funded_without_a_journal_entry(
    receipts: RecordSalaryReceipt,
    generate: GenerateSalaryAllocation,
    approve: ApproveSalaryAllocation,
    templates: ManageSalaryTemplates,
    factory: UnitOfWorkFactory,
    world: World,
) -> None:
    _stop_the_subscription(factory)
    templates.deactivate_item(
        DeactivateSalaryTemplateItemRequest(
            template_item_id=world.subscription_item_id, expected_version=1
        )
    )
    templates.update_item(
        UpdateSalaryTemplateItemRequest(
            template_item_id=world.fixed_item_id,
            expected_version=1,
            name="測試生活費",
            destination_wallet_id=world.living_wallet_id,
            calculation_mode=SalaryCalculationMode.FIXED,
            budget_category_id=world.category_id,
            fixed_amount_minor=0,
            display_order=0,
        )
    )
    received = _receive(receipts, world)
    result = _generate(generate, world, received.event.id, manual_minor=0)
    assert result.run.planned_amount_minor == 0
    assert result.run.remainder_minor == SALARY_MINOR

    approved = approve.execute(
        ApproveSalaryRunRequest(
            run_id=result.run.id, expected_version=result.run.version
        )
    )

    assert approved.run.status is SalaryRunStatus.FUNDED
    assert approved.entry is None
    assert all(
        item.status is SalaryRunItemStatus.NOT_REQUIRED for item in approved.items
    )
    with factory() as unit:
        assert unit.budget_ledger.reserved_minor(world.source_wallet_id) == 0
        assert (
            unit.journal.get_by_idempotency_key(
                f"salary_allocation:run:{result.run.id}"
            )
            is None
        )


def test_approving_twice_never_posts_or_reserves_twice(
    receipts: RecordSalaryReceipt,
    generate: GenerateSalaryAllocation,
    approve: ApproveSalaryAllocation,
    balances: MoneyBalanceQueries,
    factory: UnitOfWorkFactory,
    world: World,
) -> None:
    received = _receive(receipts, world)
    result = _generate(generate, world, received.event.id)
    first = approve.execute(
        ApproveSalaryRunRequest(
            run_id=result.run.id, expected_version=result.run.version
        )
    )

    second = approve.execute(
        ApproveSalaryRunRequest(
            run_id=result.run.id, expected_version=result.run.version
        )
    )

    assert second.created is False
    assert second.entry is not None
    assert first.entry is not None
    assert second.entry.id == first.entry.id
    assert second.run.version == first.run.version
    assert (
        _wallet_balance(balances, world.source_account_id, world.living_wallet_id)
        == LIVING_MINOR
    )
    with factory() as unit:
        assert unit.budget_ledger.reserved_minor(world.source_wallet_id) == (
            SUBSCRIPTION_PRICE_MINOR + 20_000
        )


def test_a_stale_approval_changes_nothing(
    receipts: RecordSalaryReceipt,
    generate: GenerateSalaryAllocation,
    approve: ApproveSalaryAllocation,
    balances: MoneyBalanceQueries,
    factory: UnitOfWorkFactory,
    world: World,
) -> None:
    received = _receive(receipts, world)
    result = _generate(generate, world, received.event.id)

    with pytest.raises(ConcurrentUpdateError):
        approve.execute(
            ApproveSalaryRunRequest(
                run_id=result.run.id, expected_version=result.run.version + 3
            )
        )

    assert (
        _wallet_balance(balances, world.source_account_id, world.living_wallet_id) == 0
    )
    with factory() as unit:
        assert (
            _must(unit.salary_runs.get(result.run.id)).status is SalaryRunStatus.DRAFT
        )
        assert unit.budget_ledger.reserved_minor(world.source_wallet_id) == 0


def test_approving_more_than_the_wallet_can_still_spend_rolls_everything_back(
    receipts: RecordSalaryReceipt,
    generate: GenerateSalaryAllocation,
    approve: ApproveSalaryAllocation,
    balances: MoneyBalanceQueries,
    factory: UnitOfWorkFactory,
    world: World,
) -> None:
    """A card purchase already reserved this wallet, so the plan no longer fits."""
    received = _receive(receipts, world)
    _reserve_from_the_source_wallet(factory, world, 50_000)
    result = _generate(generate, world, received.event.id, manual_minor=20_000)
    assert result.run.planned_amount_minor == 89_000

    with pytest.raises(InsufficientAvailableFundsError):
        approve.execute(
            ApproveSalaryRunRequest(
                run_id=result.run.id, expected_version=result.run.version
            )
        )

    with factory() as unit:
        assert (
            _must(unit.salary_runs.get(result.run.id)).status is SalaryRunStatus.DRAFT
        )
        # Only the card commitment reserves anything; approval reserved nothing.
        assert unit.budget_ledger.reserved_minor(world.source_wallet_id) == 50_000
        assert (
            unit.journal.get_by_idempotency_key(
                f"salary_allocation:run:{result.run.id}"
            )
            is None
        )
    assert (
        _wallet_balance(balances, world.source_account_id, world.living_wallet_id) == 0
    )
    assert (
        _wallet_balance(balances, world.source_account_id, world.source_wallet_id)
        == SALARY_MINOR
    )


def test_a_cancelled_run_is_never_approved(
    receipts: RecordSalaryReceipt,
    generate: GenerateSalaryAllocation,
    approve: ApproveSalaryAllocation,
    world: World,
) -> None:
    received = _receive(receipts, world)
    result = _generate(generate, world, received.event.id)
    generate.cancel(
        CancelSalaryDraftRequest(
            run_id=result.run.id, expected_version=result.run.version
        )
    )

    with pytest.raises(SalaryAllocationError, match="cancelled salary run"):
        approve.execute(
            ApproveSalaryRunRequest(
                run_id=result.run.id, expected_version=result.run.version + 1
            )
        )


def test_approval_creates_no_scheduled_transfer_table_row(
    receipts: RecordSalaryReceipt,
    generate: GenerateSalaryAllocation,
    approve: ApproveSalaryAllocation,
    migrated_database: Database,
    world: World,
) -> None:
    """B6 owns scheduling; approving a salary must not anticipate it.

    Revision 0013 created these tables, so the assertion is no longer that
    they are absent but that a salary approval writes nothing into them: a
    cross-account salary line stays a manual **已轉帳** confirmation and is
    never executed by the maintenance coordinator (ADR-B057).
    """
    received = _receive(receipts, world)
    result = _generate(generate, world, received.event.id)
    approve.execute(
        ApproveSalaryRunRequest(
            run_id=result.run.id, expected_version=result.run.version
        )
    )

    with migrated_database.engine.connect() as connection:
        counts = {
            table: connection.exec_driver_sql(
                f"SELECT COUNT(*) FROM {table}"
            ).scalar_one()
            for table in ("scheduled_transfers", "scheduled_transfer_commitments")
        }
    assert counts == {"scheduled_transfers": 0, "scheduled_transfer_commitments": 0}


# --- F. Confirming a real transfer -----------------------------------------


def test_confirming_a_transfer_moves_both_banks_exactly_once(
    receipts: RecordSalaryReceipt,
    generate: GenerateSalaryAllocation,
    approve: ApproveSalaryAllocation,
    transfer: MarkSalaryTransferred,
    balances: MoneyBalanceQueries,
    factory: UnitOfWorkFactory,
    world: World,
) -> None:
    received = _receive(receipts, world)
    result = _generate(generate, world, received.event.id)
    approved = approve.execute(
        ApproveSalaryRunRequest(
            run_id=result.run.id, expected_version=result.run.version
        )
    )
    subscription = _item_by_name(approved.items, "測試訂閱")

    moved = transfer.execute(
        MarkSalaryTransferredRequest(
            run_item_id=subscription.id,
            expected_version=subscription.version,
            effective_at=TRANSFER_AT,
        )
    )

    assert moved.created is True
    assert moved.entry.entry_type is JournalEntryType.FUNDING_TRANSFER
    assert moved.entry.effective_at == TRANSFER_AT
    assert sum(posting.amount_minor for posting in moved.entry.postings) == 0
    assert moved.item.status is SalaryRunItemStatus.TRANSFERRED
    assert moved.item.transfer_entry_id == moved.entry.id
    assert moved.item.transferred_at == TRANSFER_AT
    assert moved.run.status is SalaryRunStatus.PARTIALLY_FUNDED

    assert (
        _account_balance(balances, world.source_account_id)
        == SALARY_MINOR - SUBSCRIPTION_PRICE_MINOR
    )
    assert (
        _account_balance(balances, world.other_bank_account_id)
        == SUBSCRIPTION_PRICE_MINOR
    )
    assert (
        _wallet_balance(
            balances, world.other_bank_account_id, world.subscription_wallet_id
        )
        == SUBSCRIPTION_PRICE_MINOR
    )
    with factory() as unit:
        # The waiting reservation is gone; only the rent line still holds money.
        assert unit.budget_ledger.reserved_minor(world.source_wallet_id) == 20_000


def test_availability_is_never_counted_twice_around_a_transfer(
    receipts: RecordSalaryReceipt,
    generate: GenerateSalaryAllocation,
    approve: ApproveSalaryAllocation,
    transfer: MarkSalaryTransferred,
    factory: UnitOfWorkFactory,
    world: World,
) -> None:
    received = _receive(receipts, world)
    result = _generate(generate, world, received.event.id)
    approved = approve.execute(
        ApproveSalaryRunRequest(
            run_id=result.run.id, expected_version=result.run.version
        )
    )
    subscription = _item_by_name(approved.items, "測試訂閱")

    with factory() as unit:
        before_balance = unit.budget_ledger.wallet_balance(world.source_wallet_id)
        before_reserved = unit.budget_ledger.reserved_minor(world.source_wallet_id)

    transfer.execute(
        MarkSalaryTransferredRequest(
            run_item_id=subscription.id,
            expected_version=subscription.version,
            effective_at=TRANSFER_AT,
        )
    )

    with factory() as unit:
        after_balance = unit.budget_ledger.wallet_balance(world.source_wallet_id)
        after_reserved = unit.budget_ledger.reserved_minor(world.source_wallet_id)
    # The balance falls by exactly the amount and the reservation falls by
    # exactly the same amount, so the available figure is unchanged.
    assert before_balance - after_balance == SUBSCRIPTION_PRICE_MINOR
    assert before_reserved - after_reserved == SUBSCRIPTION_PRICE_MINOR
    assert before_balance - before_reserved == after_balance - after_reserved


def test_the_last_transfer_completes_the_run(
    receipts: RecordSalaryReceipt,
    generate: GenerateSalaryAllocation,
    approve: ApproveSalaryAllocation,
    transfer: MarkSalaryTransferred,
    world: World,
) -> None:
    received = _receive(receipts, world)
    result = _generate(generate, world, received.event.id)
    approved = approve.execute(
        ApproveSalaryRunRequest(
            run_id=result.run.id, expected_version=result.run.version
        )
    )

    last_status = None
    for name in ("測試訂閱", "測試房租"):
        item = _item_by_name(approved.items, name)
        outcome = transfer.execute(
            MarkSalaryTransferredRequest(
                run_item_id=item.id,
                expected_version=item.version,
                effective_at=TRANSFER_AT,
            )
        )
        last_status = outcome.run.status

    assert last_status is SalaryRunStatus.FUNDED


def test_confirming_a_transfer_twice_never_posts_twice(
    receipts: RecordSalaryReceipt,
    generate: GenerateSalaryAllocation,
    approve: ApproveSalaryAllocation,
    transfer: MarkSalaryTransferred,
    balances: MoneyBalanceQueries,
    world: World,
) -> None:
    received = _receive(receipts, world)
    result = _generate(generate, world, received.event.id)
    approved = approve.execute(
        ApproveSalaryRunRequest(
            run_id=result.run.id, expected_version=result.run.version
        )
    )
    subscription = _item_by_name(approved.items, "測試訂閱")
    first = transfer.execute(
        MarkSalaryTransferredRequest(
            run_item_id=subscription.id,
            expected_version=subscription.version,
            effective_at=TRANSFER_AT,
        )
    )

    # A retry with a *different* time must not rewrite the posted entry.
    second = transfer.execute(
        MarkSalaryTransferredRequest(
            run_item_id=subscription.id,
            expected_version=subscription.version,
            effective_at=datetime(2026, 8, 9, 5, 0, tzinfo=UTC),
        )
    )

    assert second.created is False
    assert second.entry.id == first.entry.id
    assert second.entry.effective_at == TRANSFER_AT
    assert second.item.transferred_at == TRANSFER_AT
    assert (
        _account_balance(balances, world.other_bank_account_id)
        == SUBSCRIPTION_PRICE_MINOR
    )


def test_a_line_that_is_not_waiting_has_no_transfer_to_confirm(
    receipts: RecordSalaryReceipt,
    generate: GenerateSalaryAllocation,
    approve: ApproveSalaryAllocation,
    transfer: MarkSalaryTransferred,
    world: World,
) -> None:
    received = _receive(receipts, world)
    result = _generate(generate, world, received.event.id)
    draft_item = _item_by_name(result.items, "測試訂閱")

    with pytest.raises(SalaryAllocationError, match="approved salary run"):
        transfer.execute(
            MarkSalaryTransferredRequest(
                run_item_id=draft_item.id,
                expected_version=draft_item.version,
                effective_at=TRANSFER_AT,
            )
        )

    approved = approve.execute(
        ApproveSalaryRunRequest(
            run_id=result.run.id, expected_version=result.run.version
        )
    )
    allocated = _item_by_name(approved.items, "測試生活費")
    with pytest.raises(SalaryAllocationError, match="no transfer to confirm"):
        transfer.execute(
            MarkSalaryTransferredRequest(
                run_item_id=allocated.id,
                expected_version=allocated.version,
                effective_at=TRANSFER_AT,
            )
        )


def test_an_inactive_destination_wallet_refuses_the_transfer_and_rolls_back(
    receipts: RecordSalaryReceipt,
    generate: GenerateSalaryAllocation,
    approve: ApproveSalaryAllocation,
    transfer: MarkSalaryTransferred,
    migrated_database: Database,
    factory: UnitOfWorkFactory,
    balances: MoneyBalanceQueries,
    world: World,
) -> None:
    received = _receive(receipts, world)
    result = _generate(generate, world, received.event.id)
    approved = approve.execute(
        ApproveSalaryRunRequest(
            run_id=result.run.id, expected_version=result.run.version
        )
    )
    subscription = _item_by_name(approved.items, "測試訂閱")
    with migrated_database.engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE wallets SET is_active = 0 WHERE id = ?",
            (world.subscription_wallet_id,),
        )

    with pytest.raises(SalaryPlanError, match="not active"):
        transfer.execute(
            MarkSalaryTransferredRequest(
                run_item_id=subscription.id,
                expected_version=subscription.version,
                effective_at=TRANSFER_AT,
            )
        )

    assert _account_balance(balances, world.other_bank_account_id) == 0
    with factory() as unit:
        assert (
            _must(unit.salary_run_items.get(subscription.id)).status
            is SalaryRunItemStatus.WAITING_TRANSFER
        )


def test_a_transfer_time_is_the_users_local_instant_and_never_an_assumed_one(
    receipts: RecordSalaryReceipt,
    generate: GenerateSalaryAllocation,
    approve: ApproveSalaryAllocation,
    transfer: MarkSalaryTransferred,
    world: World,
) -> None:
    """`Asia/Taipei` 18:30 is 10:30 UTC; nothing here rounds to 06:00."""
    from zoneinfo import ZoneInfo

    local = datetime(2026, 8, 6, 18, 30, tzinfo=ZoneInfo("Asia/Taipei"))
    received = _receive(receipts, world)
    result = _generate(generate, world, received.event.id)
    approved = approve.execute(
        ApproveSalaryRunRequest(
            run_id=result.run.id, expected_version=result.run.version
        )
    )
    subscription = _item_by_name(approved.items, "測試訂閱")

    moved = transfer.execute(
        MarkSalaryTransferredRequest(
            run_item_id=subscription.id,
            expected_version=subscription.version,
            effective_at=local,
        )
    )

    assert moved.entry.effective_at.astimezone(UTC) == datetime(
        2026, 8, 6, 10, 30, tzinfo=UTC
    )
    assert moved.entry.effective_at.astimezone(UTC).hour != 6


def test_a_naive_transfer_time_is_refused(
    receipts: RecordSalaryReceipt,
    generate: GenerateSalaryAllocation,
    approve: ApproveSalaryAllocation,
    transfer: MarkSalaryTransferred,
    world: World,
) -> None:
    received = _receive(receipts, world)
    result = _generate(generate, world, received.event.id)
    approved = approve.execute(
        ApproveSalaryRunRequest(
            run_id=result.run.id, expected_version=result.run.version
        )
    )
    subscription = _item_by_name(approved.items, "測試訂閱")
    with pytest.raises(ValueError, match="timezone-aware"):
        transfer.execute(
            MarkSalaryTransferredRequest(
                run_item_id=subscription.id,
                expected_version=subscription.version,
                effective_at=datetime(2026, 8, 6, 10, 30),
            )
        )


# --- Template management ---------------------------------------------------


def test_a_template_refuses_its_own_unallocated_wallet_as_a_destination(
    templates: ManageSalaryTemplates, world: World
) -> None:
    with pytest.raises(SalaryPlanError, match="remainder"):
        templates.add_item(
            AddSalaryTemplateItemRequest(
                template_id=world.template_id,
                name="不合法",
                destination_wallet_id=world.source_wallet_id,
                calculation_mode=SalaryCalculationMode.FIXED,
                fixed_amount_minor=1,
            )
        )


def test_a_template_refuses_a_destination_in_another_currency(
    templates: ManageSalaryTemplates, factory: UnitOfWorkFactory, world: World
) -> None:
    setup = LedgerSetupService(factory)
    yen = setup.open_account(
        OpenFinancialAccountRequest(
            institution_code="test_bank_d",
            display_name="測試日圓帳戶",
            account_type=FinancialAccountType.CHECKING,
            currency="JPY",
        )
    )
    wallet = setup.add_wallet(
        AddWalletRequest(financial_account_id=yen.account.id, name="測試日圓錢包")
    )
    with pytest.raises(CurrencyMismatchError):
        templates.add_item(
            AddSalaryTemplateItemRequest(
                template_id=world.template_id,
                name="日圓",
                destination_wallet_id=wallet.id,
                calculation_mode=SalaryCalculationMode.FIXED,
                fixed_amount_minor=1,
            )
        )


def test_adding_or_changing_an_item_moves_the_template_to_a_new_version(
    templates: ManageSalaryTemplates, salary_queries: SalaryQueries, world: World
) -> None:
    before = salary_queries.template(world.template_id)
    assert before is not None

    templates.deactivate_item(
        DeactivateSalaryTemplateItemRequest(
            template_item_id=world.manual_item_id, expected_version=1
        )
    )

    after = salary_queries.template(world.template_id)
    assert after is not None
    assert after.template.version == before.template.version + 1


def test_a_stale_template_version_refuses_an_item_change(
    templates: ManageSalaryTemplates, world: World
) -> None:
    with pytest.raises(ConcurrentUpdateError):
        templates.deactivate_item(
            DeactivateSalaryTemplateItemRequest(
                template_item_id=world.manual_item_id,
                expected_version=1,
                expected_template_version=99,
            )
        )


def test_the_query_service_shows_a_frozen_run_without_recalculating(
    receipts: RecordSalaryReceipt,
    generate: GenerateSalaryAllocation,
    salary_queries: SalaryQueries,
    world: World,
) -> None:
    received = _receive(receipts, world)
    result = _generate(generate, world, received.event.id)

    view = salary_queries.run(result.run.id)

    assert view is not None
    assert view.label == "2026-08"
    assert view.source.balance_minor == SALARY_MINOR
    assert view.source.reserved_minor == 0
    assert view.source.available_minor == SALARY_MINOR
    assert view.subscription_total_minor == SUBSCRIPTION_PRICE_MINOR
    assert view.salary_event is not None
    assert view.salary_event.id == received.event.id
    month = salary_queries.month(
        year=2026, month=8, template_id=world.template_id, currency="TWD"
    )
    assert month.live_run is not None
    assert month.live_run.run.id == result.run.id
    assert [run.generation for run in month.generations] == [1]
    assert month.eligible_events == ()


def test_eligible_events_list_only_a_structurally_correct_unused_salary(
    receipts: RecordSalaryReceipt,
    generate: GenerateSalaryAllocation,
    salary_queries: SalaryQueries,
    world: World,
) -> None:
    received = _receive(receipts, world)

    available = salary_queries.eligible_salary_events(world.template_id)
    assert [row.event.id for row in available] == [received.event.id]

    _generate(generate, world, received.event.id)
    assert salary_queries.eligible_salary_events(world.template_id) == ()


# --- Idempotency identity (P1 review fixes) --------------------------------


def _foreign_event_on_the_salary_token(
    factory: UnitOfWorkFactory, world: World, *, token: str
) -> int:
    """Occupy a salary token with an unrelated event of the same money.

    Amount, currency, time, and the credited wallet all match; only the
    identity of the event and its entry differ. Before the fix this was
    replayed as a successful salary receipt.
    """
    from app.application.services.ledger import JournalService as _Journal
    from app.domain.budget_enums import (
        FinancialEventOrigin,
        FinancialEventType,
        LedgerAccountKind,
        PaymentMethod,
        RecognitionBasis,
    )
    from app.domain.budget_models import (
        FinancialEventDraft,
        LedgerAccountDraft,
        Money,
        PostingDraft,
    )
    from app.domain.budget_rules import salary_receipt_event_key

    journal = _Journal(factory, clock=lambda: NOW)
    with factory() as unit:
        event = unit.financial_events.add(
            FinancialEventDraft(
                origin=FinancialEventOrigin.GMAIL_TRANSACTION,
                event_type=FinancialEventType.PURCHASE,
                payment_method=PaymentMethod.CREDIT_CARD,
                recognition_basis=RecognitionBasis.GMAIL_NOTIFICATION,
                occurred_at=SALARY_AT,
                amount_minor=SALARY_MINOR,
                currency="TWD",
                idempotency_key=salary_receipt_event_key(token),
            )
        )
        asset = unit.ledger_accounts.get_for_financial_account(world.source_account_id)
        assert asset is not None
        other = unit.ledger_accounts.add(
            LedgerAccountDraft(
                code="income:other:TWD",
                name="測試其他收入",
                kind=LedgerAccountKind.INCOME,
                currency="TWD",
            )
        )
        journal.stage_balanced(
            unit,
            entry_type=JournalEntryType.ADJUSTMENT,
            effective_at=SALARY_AT,
            idempotency_key=f"foreign:{token}",
            description="",
            postings=(
                PostingDraft(
                    ledger_account_id=asset.id,
                    amount=Money(SALARY_MINOR, "TWD"),
                    wallet_id=world.source_wallet_id,
                ),
                PostingDraft(
                    ledger_account_id=other.id,
                    amount=Money(-SALARY_MINOR, "TWD"),
                ),
            ),
            financial_event_id=event.id,
        )
        unit.commit()
    return event.id


def test_a_foreign_event_holding_the_salary_token_is_a_conflict(
    receipts: RecordSalaryReceipt,
    factory: UnitOfWorkFactory,
    balances: MoneyBalanceQueries,
    world: World,
) -> None:
    """A matching token is a claim, not proof that a salary was recorded."""
    event_id = _foreign_event_on_the_salary_token(factory, world, token="stolen")

    with pytest.raises(IdempotencyConflictError, match="records something else"):
        _receive(receipts, world, token="stolen")

    with factory() as unit:
        untouched = _must(unit.financial_events.get(event_id))
        assert untouched.event_type.value == "purchase"
        assert untouched.status is not FinancialEventStatus.POSTED
    assert _account_balance(balances, world.source_account_id) == SALARY_MINOR


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("origin", "import"),
        ("event_type", "transfer"),
        ("payment_method", "cash"),
        ("recognition_basis", "imported"),
        ("status", "ready"),
    ],
)
def test_a_receipt_whose_event_identity_changed_is_a_conflict(
    receipts: RecordSalaryReceipt,
    migrated_database: Database,
    world: World,
    column: str,
    value: str,
) -> None:
    received = _receive(receipts, world, token="identity")
    with migrated_database.engine.begin() as connection:
        connection.exec_driver_sql(
            f"UPDATE financial_events SET {column} = ? WHERE id = ?",
            (value, received.event.id),
        )

    with pytest.raises(IdempotencyConflictError, match="not_a_salary_event"):
        _receive(receipts, world, token="identity")


def test_a_receipt_whose_entry_type_changed_is_a_conflict(
    receipts: RecordSalaryReceipt, migrated_database: Database, world: World
) -> None:
    received = _receive(receipts, world, token="entry-type")
    with migrated_database.engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE journal_entries SET entry_type = 'adjustment' WHERE id = ?",
            (received.entry.id,),
        )

    with pytest.raises(IdempotencyConflictError, match="wrong_entry_type"):
        _receive(receipts, world, token="entry-type")


def test_a_receipt_credited_to_another_income_account_is_a_conflict(
    receipts: RecordSalaryReceipt,
    factory: UnitOfWorkFactory,
    migrated_database: Database,
    world: World,
) -> None:
    from app.domain.budget_enums import LedgerAccountKind
    from app.domain.budget_models import LedgerAccountDraft

    received = _receive(receipts, world, token="income")
    with factory() as unit:
        other = unit.ledger_accounts.add(
            LedgerAccountDraft(
                code="income:other:TWD",
                name="測試其他收入",
                kind=LedgerAccountKind.INCOME,
                currency="TWD",
            )
        )
        unit.commit()
    with migrated_database.engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE journal_postings SET ledger_account_id = ?"
            " WHERE journal_entry_id = ? AND amount_minor < 0",
            (other.id, received.entry.id),
        )

    with pytest.raises(IdempotencyConflictError, match="wrong_postings"):
        _receive(receipts, world, token="income")


def test_a_receipt_with_an_extra_posting_is_a_conflict(
    receipts: RecordSalaryReceipt, migrated_database: Database, world: World
) -> None:
    received = _receive(receipts, world, token="extra")
    with migrated_database.engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO journal_postings (journal_entry_id, ledger_account_id,"
            " wallet_id, amount_minor, currency, created_at, updated_at)"
            " SELECT journal_entry_id, ledger_account_id, wallet_id, 1, currency,"
            " created_at, updated_at FROM journal_postings WHERE journal_entry_id = ?"
            " LIMIT 1",
            (received.entry.id,),
        )

    with pytest.raises(IdempotencyConflictError, match="wrong_postings"):
        _receive(receipts, world, token="extra")


def test_a_receipt_whose_entry_effective_time_changed_is_a_conflict(
    receipts: RecordSalaryReceipt, migrated_database: Database, world: World
) -> None:
    received = _receive(receipts, world, token="effective")
    with migrated_database.engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE journal_entries SET effective_at = '2026-09-09 01:00:00'"
            " WHERE id = ?",
            (received.entry.id,),
        )

    with pytest.raises(IdempotencyConflictError, match="wrong_effective_time"):
        _receive(receipts, world, token="effective")


@pytest.mark.parametrize("amount", [True, float(SALARY_MINOR)])
def test_an_inexact_amount_never_matches_a_stored_integer(
    receipts: RecordSalaryReceipt, world: World, amount: object
) -> None:
    """`True == 1` and `100.0 == 100`, so the type is checked before comparing."""
    _receive(receipts, world, token="exact")

    with pytest.raises(TypeError, match="integer minor units"):
        _receive(receipts, world, token="exact", amount_minor=amount)  # type: ignore[arg-type]


def test_a_legitimate_retry_still_replays_the_first_receipt(
    receipts: RecordSalaryReceipt, balances: MoneyBalanceQueries, world: World
) -> None:
    first = _receive(receipts, world, token="legitimate")

    second = _receive(receipts, world, token="legitimate")

    assert second.created is False
    assert second.event.id == first.event.id
    assert second.entry.id == first.entry.id
    assert _account_balance(balances, world.source_account_id) == SALARY_MINOR


def test_a_cancelled_draft_replaced_by_an_ordinary_generate_is_not_a_replay(
    receipts: RecordSalaryReceipt,
    generate: GenerateSalaryAllocation,
    factory: UnitOfWorkFactory,
    world: World,
) -> None:
    """The reviewer's reproduction: generation 2 is not this request's result."""
    received = _receive(receipts, world)
    first = _generate(generate, world, received.event.id, manual_minor=10_000)
    generate.cancel(
        CancelSalaryDraftRequest(
            run_id=first.run.id, expected_version=first.run.version
        )
    )
    second = _generate(generate, world, received.event.id, manual_minor=11_000)
    assert second.created is True
    assert second.run.generation == 2
    assert second.run.regenerated_from_run_id is None

    with pytest.raises(SalaryAllocationError, match="nothing regenerated it"):
        generate.regenerate(
            RegenerateSalaryRunRequest(
                run_id=first.run.id,
                expected_version=first.run.version,
                manual_amounts=(
                    ManualSalaryAmount(
                        template_item_id=world.manual_item_id, amount_minor=22_000
                    ),
                ),
            )
        )

    with factory() as unit:
        live = unit.salary_runs.find_live(
            template_id=world.template_id,
            budget_period_id=second.run.budget_period_id,
        )
    assert live is not None
    assert live.id == second.run.id
    # Generation 2 still holds the amounts it was generated with.
    assert live.planned_amount_minor == second.run.planned_amount_minor


def test_a_regeneration_replay_with_a_different_payload_is_a_conflict(
    receipts: RecordSalaryReceipt,
    generate: GenerateSalaryAllocation,
    world: World,
) -> None:
    received = _receive(receipts, world)
    first = _generate(generate, world, received.event.id)
    generate.regenerate(
        RegenerateSalaryRunRequest(
            run_id=first.run.id,
            expected_version=first.run.version,
            manual_amounts=(
                ManualSalaryAmount(
                    template_item_id=world.manual_item_id, amount_minor=20_000
                ),
            ),
        )
    )

    with pytest.raises(IdempotencyConflictError, match="different request"):
        generate.regenerate(
            RegenerateSalaryRunRequest(
                run_id=first.run.id,
                expected_version=first.run.version,
                manual_amounts=(
                    ManualSalaryAmount(
                        template_item_id=world.manual_item_id, amount_minor=21_000
                    ),
                ),
            )
        )


def test_a_regeneration_for_another_month_is_never_mistaken_for_this_one(
    receipts: RecordSalaryReceipt,
    generate: GenerateSalaryAllocation,
    world: World,
) -> None:
    """A different salary and month regenerate independently."""
    july_salary = _receive(receipts, world, token="july")
    july = _generate(generate, world, july_salary.event.id, month=7)
    august_salary = _receive(receipts, world, token="august")
    august = _generate(generate, world, august_salary.event.id, month=8)

    july_second = generate.regenerate(
        RegenerateSalaryRunRequest(
            run_id=july.run.id,
            expected_version=july.run.version,
            manual_amounts=(
                ManualSalaryAmount(
                    template_item_id=world.manual_item_id, amount_minor=20_000
                ),
            ),
        )
    )

    assert july_second.run.regenerated_from_run_id == july.run.id
    assert july_second.run.budget_period_id == july.run.budget_period_id
    assert july_second.run.budget_period_id != august.run.budget_period_id
    assert july_second.run.salary_event_id == july_salary.event.id


def test_a_failed_regeneration_records_no_provenance_and_keeps_the_draft(
    receipts: RecordSalaryReceipt,
    generate: GenerateSalaryAllocation,
    factory: UnitOfWorkFactory,
    world: World,
) -> None:
    received = _receive(receipts, world)
    first = _generate(generate, world, received.event.id)

    with pytest.raises(SalaryAllocationError, match="larger than this salary"):
        generate.regenerate(
            RegenerateSalaryRunRequest(
                run_id=first.run.id,
                expected_version=first.run.version,
                manual_amounts=(
                    ManualSalaryAmount(
                        template_item_id=world.manual_item_id,
                        amount_minor=SALARY_MINOR,
                    ),
                ),
            )
        )

    with factory() as unit:
        unchanged = _must(unit.salary_runs.get(first.run.id))
        assert unchanged.status is SalaryRunStatus.DRAFT
        assert unit.salary_runs.find_regenerated_from(first.run.id) is None

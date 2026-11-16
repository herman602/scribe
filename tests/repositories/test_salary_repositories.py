"""B5 repository tests: conditional updates, live-run identity, reservations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from app.domain.budget_enums import (
    CommitmentFulfillmentMode,
    CommitmentStatus,
    FinancialAccountType,
    FinancialEventOrigin,
    FinancialEventStatus,
    FinancialEventType,
    PaymentMethod,
    RecognitionBasis,
    SalaryCalculationMode,
    SalaryRunItemStatus,
    SalaryRunStatus,
    WalletPurpose,
)
from app.domain.budget_models import (
    BudgetCategoryDraft,
    EventAllocationDraft,
    FinancialAccountDraft,
    FinancialEventDraft,
    FundingCommitmentDraft,
    SalaryAllocationRunDraft,
    SalaryAllocationRunItemDraft,
    SalaryPlanTemplateDraft,
    SalaryPlanTemplateItemDraft,
    SalaryPlanTemplateItemUpdate,
    SalaryPlanTemplateUpdate,
    WalletDraft,
)
from app.domain.errors import (
    ConcurrentUpdateError,
    SalaryAllocationError,
    SalaryPlanError,
)
from app.domain.ports.repositories import UnitOfWork
from app.infrastructure.database.engine import Database
from app.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork
from sqlalchemy.exc import IntegrityError

type UnitOfWorkFactory = Callable[[], UnitOfWork]


def _must[T](value: T | None) -> T:
    """Return a row the test has just written, so the type stays narrow."""
    assert value is not None
    return value


GENERATED_AT = datetime(2026, 8, 5, 1, 0, tzinfo=UTC)
APPROVED_AT = datetime(2026, 8, 5, 2, 0, tzinfo=UTC)
EVIDENCE = '{"schema_version":1,"currency":"TWD","items":[],"month":8,"year":2026}'


@dataclass(frozen=True)
class Ledger:
    """Identifiers of the seeded accounts, wallets, category, and period."""

    source_account_id: int
    source_wallet_id: int
    other_account_id: int
    other_wallet_id: int
    second_wallet_id: int
    category_id: int
    period_id: int


@pytest.fixture
def factory(migrated_database: Database) -> UnitOfWorkFactory:
    """Return a unit-of-work factory bound to the migrated test database."""

    def build() -> UnitOfWork:
        return SqlAlchemyUnitOfWork(migrated_database.session_factory)

    return build


@pytest.fixture
def ledger(factory: UnitOfWorkFactory) -> Ledger:
    """Seed two accounts, three wallets, one category, and one budget month."""
    with factory() as unit:
        source = unit.financial_accounts.add(
            FinancialAccountDraft(
                institution_code="test_bank_a",
                display_name="測試薪轉帳戶",
                account_type=FinancialAccountType.CHECKING,
                currency="TWD",
            )
        )
        source_wallet = unit.wallets.add(
            WalletDraft(
                financial_account_id=source.id,
                name="尚未分配",
                purpose=WalletPurpose.UNALLOCATED,
            )
        )
        second = unit.wallets.add(
            WalletDraft(financial_account_id=source.id, name="測試生活費")
        )
        other = unit.financial_accounts.add(
            FinancialAccountDraft(
                institution_code="test_bank_b",
                display_name="測試訂閱帳戶",
                account_type=FinancialAccountType.CHECKING,
                currency="TWD",
            )
        )
        other_wallet = unit.wallets.add(
            WalletDraft(financial_account_id=other.id, name="測試訂閱錢包")
        )
        category = unit.budget_categories.add(BudgetCategoryDraft(name="測試分類"))
        period = unit.budget_periods.get_or_create(year=2026, month=8, currency="TWD")
        unit.commit()
        return Ledger(
            source_account_id=source.id,
            source_wallet_id=source_wallet.id,
            other_account_id=other.id,
            other_wallet_id=other_wallet.id,
            second_wallet_id=second.id,
            category_id=category.id,
            period_id=period.id,
        )


def _template(unit: UnitOfWork, ledger: Ledger, *, name: str = "測試薪資範本") -> int:
    template = unit.salary_templates.add(
        SalaryPlanTemplateDraft(
            name=name, currency="TWD", source_account_id=ledger.source_account_id
        )
    )
    return template.id


def _item(
    unit: UnitOfWork,
    template_id: int,
    *,
    name: str = "測試項目",
    wallet_id: int,
    mode: SalaryCalculationMode = SalaryCalculationMode.FIXED,
    fixed_amount_minor: int | None = 10_000,
) -> int:
    item = unit.salary_template_items.add(
        SalaryPlanTemplateItemDraft(
            template_id=template_id,
            name=name,
            destination_wallet_id=wallet_id,
            calculation_mode=mode,
            fixed_amount_minor=fixed_amount_minor,
        )
    )
    return item.id


def _salary_event(unit: UnitOfWork, *, token: str, amount_minor: int = 100_000) -> int:
    event = unit.financial_events.add(
        FinancialEventDraft(
            origin=FinancialEventOrigin.MANUAL,
            event_type=FinancialEventType.DEPOSIT,
            payment_method=PaymentMethod.BANK_ACCOUNT,
            recognition_basis=RecognitionBasis.OBSERVED_MANUAL,
            occurred_at=GENERATED_AT,
            amount_minor=amount_minor,
            currency="TWD",
            idempotency_key=token,
            status=FinancialEventStatus.READY,
        )
    )
    return event.id


def _run(
    unit: UnitOfWork,
    ledger: Ledger,
    *,
    template_id: int,
    salary_event_id: int,
    generation: int = 1,
    planned_amount_minor: int = 10_000,
    salary_amount_minor: int = 100_000,
) -> int:
    run = unit.salary_runs.add(
        SalaryAllocationRunDraft(
            template_id=template_id,
            template_name_snapshot="測試薪資範本",
            template_version=1,
            budget_period_id=ledger.period_id,
            salary_event_id=salary_event_id,
            source_account_id=ledger.source_account_id,
            source_wallet_id=ledger.source_wallet_id,
            currency="TWD",
            generation=generation,
            salary_amount_minor=salary_amount_minor,
            planned_amount_minor=planned_amount_minor,
            remainder_minor=salary_amount_minor - planned_amount_minor,
            subscription_evidence_schema_version=1,
            subscription_evidence_json=EVIDENCE,
            generated_at=GENERATED_AT,
        )
    )
    return run.id


def _run_item(
    unit: UnitOfWork,
    run_id: int,
    ledger: Ledger,
    *,
    template_item_id: int,
    amount_minor: int = 10_000,
    mode: CommitmentFulfillmentMode = CommitmentFulfillmentMode.CROSS_BANK_TRANSFER,
) -> int:
    item = unit.salary_run_items.add(
        SalaryAllocationRunItemDraft(
            run_id=run_id,
            template_item_id=template_item_id,
            template_item_version=1,
            name_snapshot="測試項目",
            destination_wallet_id=ledger.other_wallet_id,
            destination_account_id=ledger.other_account_id,
            destination_wallet_name_snapshot="測試訂閱錢包",
            destination_account_name_snapshot="測試訂閱帳戶",
            budget_category_id=ledger.category_id,
            budget_category_name_snapshot="測試分類",
            calculation_mode=SalaryCalculationMode.FIXED,
            amount_minor=amount_minor,
            currency="TWD",
            display_order=0,
            fulfillment_mode=mode,
            calculation_evidence_json='{"kind":"fixed","schema_version":1}',
        )
    )
    return item.id


def test_a_template_and_its_items_only_update_under_the_exact_version(
    factory: UnitOfWorkFactory, ledger: Ledger
) -> None:
    with factory() as unit:
        template_id = _template(unit, ledger)
        item_id = _item(unit, template_id, wallet_id=ledger.other_wallet_id)
        unit.commit()

    with factory() as unit:
        with pytest.raises(ConcurrentUpdateError):
            unit.salary_templates.save(
                SalaryPlanTemplateUpdate(
                    template_id=template_id,
                    expected_version=99,
                    name="不應寫入",
                    is_active=True,
                )
            )
        unit.rollback()
        with pytest.raises(ConcurrentUpdateError):
            unit.salary_template_items.deactivate(item_id, 99)
        unit.rollback()

    with factory() as unit:
        assert _must(unit.salary_templates.get(template_id)).name == "測試薪資範本"
        assert _must(unit.salary_template_items.get(item_id)).is_active is True


def test_a_stale_writer_never_overwrites_a_newer_template(
    factory: UnitOfWorkFactory, ledger: Ledger
) -> None:
    with factory() as unit:
        template_id = _template(unit, ledger)
        unit.commit()
    with factory() as unit:
        stale = _must(unit.salary_templates.get(template_id))
        unit.salary_templates.save(
            SalaryPlanTemplateUpdate(
                template_id=template_id,
                expected_version=stale.version,
                name="第一個寫入",
                is_active=True,
            )
        )
        unit.commit()

    with factory() as unit:
        with pytest.raises(ConcurrentUpdateError):
            unit.salary_templates.save(
                SalaryPlanTemplateUpdate(
                    template_id=template_id,
                    expected_version=stale.version,
                    name="第二個寫入",
                    is_active=True,
                )
            )
        unit.rollback()

    with factory() as unit:
        assert _must(unit.salary_templates.get(template_id)).name == "第一個寫入"


def test_touching_a_template_version_is_conditional_too(
    factory: UnitOfWorkFactory, ledger: Ledger
) -> None:
    with factory() as unit:
        template_id = _template(unit, ledger)
        unit.commit()
    with factory() as unit:
        template = _must(unit.salary_templates.get(template_id))
        bumped = unit.salary_templates.touch_version(template_id, template.version)
        unit.commit()
    assert bumped.version == template.version + 1
    with factory() as unit, pytest.raises(ConcurrentUpdateError):
        unit.salary_templates.touch_version(template_id, template.version)


@pytest.mark.parametrize("collision", ["name", "destination"])
def test_a_duplicate_active_item_is_refused_and_rolls_back(
    factory: UnitOfWorkFactory, ledger: Ledger, collision: str
) -> None:
    with factory() as unit:
        template_id = _template(unit, ledger)
        _item(unit, template_id, name="訂閱", wallet_id=ledger.other_wallet_id)
        unit.commit()

    with factory() as unit:
        with pytest.raises(SalaryPlanError):
            _item(
                unit,
                template_id,
                name="訂閱" if collision == "name" else "另一個名稱",
                wallet_id=(
                    ledger.second_wallet_id
                    if collision == "name"
                    else ledger.other_wallet_id
                ),
            )
        unit.rollback()

    with factory() as unit:
        assert len(unit.salary_template_items.list_for_template(template_id)) == 1


def test_a_deactivated_item_releases_its_name_and_destination(
    factory: UnitOfWorkFactory, ledger: Ledger
) -> None:
    with factory() as unit:
        template_id = _template(unit, ledger)
        item_id = _item(
            unit, template_id, name="訂閱", wallet_id=ledger.other_wallet_id
        )
        unit.commit()
    with factory() as unit:
        item = _must(unit.salary_template_items.get(item_id))
        unit.salary_template_items.deactivate(item_id, item.version)
        _item(unit, template_id, name="訂閱", wallet_id=ledger.other_wallet_id)
        unit.commit()

    with factory() as unit:
        items = unit.salary_template_items.list_for_template(template_id)
        active = unit.salary_template_items.list_for_template(
            template_id, active_only=True
        )
    assert len(items) == 2
    assert len(active) == 1


def test_one_template_and_month_hold_at_most_one_live_run(
    factory: UnitOfWorkFactory, ledger: Ledger
) -> None:
    with factory() as unit:
        template_id = _template(unit, ledger)
        first_event = _salary_event(unit, token="salary_receipt:one")
        second_event = _salary_event(unit, token="salary_receipt:two")
        _run(unit, ledger, template_id=template_id, salary_event_id=first_event)
        unit.commit()

    with factory() as unit:
        with pytest.raises(SalaryAllocationError, match="already has a run"):
            _run(
                unit,
                ledger,
                template_id=template_id,
                salary_event_id=second_event,
                generation=2,
            )
        unit.rollback()


def test_one_salary_event_is_distributed_by_at_most_one_live_run(
    factory: UnitOfWorkFactory, ledger: Ledger
) -> None:
    with factory() as unit:
        first_template = _template(unit, ledger, name="範本一")
        second_template = _template(unit, ledger, name="範本二")
        event_id = _salary_event(unit, token="salary_receipt:shared")
        _run(unit, ledger, template_id=first_template, salary_event_id=event_id)
        unit.commit()

    with factory() as unit:
        with pytest.raises(SalaryAllocationError, match="already distributed"):
            _run(unit, ledger, template_id=second_template, salary_event_id=event_id)
        unit.rollback()


def test_a_cancelled_generation_is_kept_and_the_next_one_can_be_created(
    factory: UnitOfWorkFactory, ledger: Ledger
) -> None:
    with factory() as unit:
        template_id = _template(unit, ledger)
        event_id = _salary_event(unit, token="salary_receipt:regen")
        first = _run(unit, ledger, template_id=template_id, salary_event_id=event_id)
        unit.commit()

    with factory() as unit:
        run = _must(unit.salary_runs.get(first))
        unit.salary_runs.cancel_draft(
            first, expected_version=run.version, cancelled_at=APPROVED_AT
        )
        second = _run(
            unit,
            ledger,
            template_id=template_id,
            salary_event_id=event_id,
            generation=unit.salary_runs.next_generation(
                template_id=template_id, budget_period_id=ledger.period_id
            ),
        )
        unit.commit()

    with factory() as unit:
        generations = unit.salary_runs.list_generations(
            template_id=template_id, budget_period_id=ledger.period_id
        )
        live = unit.salary_runs.find_live(
            template_id=template_id, budget_period_id=ledger.period_id
        )
    assert [item.generation for item in generations] == [1, 2]
    assert generations[0].status is SalaryRunStatus.CANCELLED
    assert generations[0].cancelled_at == APPROVED_AT
    assert live is not None
    assert live.id == second
    assert live.generation == 2


def test_only_a_draft_is_cancelled_or_approved(
    factory: UnitOfWorkFactory, ledger: Ledger
) -> None:
    with factory() as unit:
        template_id = _template(unit, ledger)
        event_id = _salary_event(unit, token="salary_receipt:approve")
        run_id = _run(unit, ledger, template_id=template_id, salary_event_id=event_id)
        unit.commit()
    with factory() as unit:
        run = _must(unit.salary_runs.get(run_id))
        unit.salary_runs.approve(
            run_id,
            expected_version=run.version,
            status=SalaryRunStatus.APPROVED,
            approval_entry_id=None,
            approved_at=APPROVED_AT,
        )
        unit.commit()

    with factory() as unit:
        approved = _must(unit.salary_runs.get(run_id))
        with pytest.raises(SalaryAllocationError, match="no longer accepts"):
            unit.salary_runs.cancel_draft(
                run_id, expected_version=approved.version, cancelled_at=APPROVED_AT
            )
        unit.rollback()
        with pytest.raises(SalaryAllocationError, match="no longer accepts"):
            unit.salary_runs.approve(
                run_id,
                expected_version=approved.version,
                status=SalaryRunStatus.FUNDED,
                approval_entry_id=None,
                approved_at=APPROVED_AT,
            )
        unit.rollback()

    with factory() as unit:
        assert _must(unit.salary_runs.get(run_id)).status is SalaryRunStatus.APPROVED


def test_a_frozen_run_column_cannot_be_rewritten_through_the_database(
    factory: UnitOfWorkFactory, ledger: Ledger, migrated_database: Database
) -> None:
    """The service never writes these, and the trigger stops anything that does."""
    with factory() as unit:
        template_id = _template(unit, ledger)
        item_id = _item(unit, template_id, wallet_id=ledger.other_wallet_id)
        event_id = _salary_event(unit, token="salary_receipt:frozen")
        run_id = _run(unit, ledger, template_id=template_id, salary_event_id=event_id)
        run_item_id = _run_item(unit, run_id, ledger, template_item_id=item_id)
        unit.commit()

    statements = (
        "UPDATE salary_allocation_runs SET salary_amount_minor = 1 "
        f"WHERE id = {run_id}",
        f"UPDATE salary_allocation_runs SET template_version = 9 WHERE id = {run_id}",
        "UPDATE salary_allocation_runs SET subscription_evidence_json = '{}' "
        f"WHERE id = {run_id}",
        "UPDATE salary_allocation_run_items SET amount_minor = 1 "
        f"WHERE id = {run_item_id}",
        "UPDATE salary_allocation_run_items SET name_snapshot = '改名' "
        f"WHERE id = {run_item_id}",
        "UPDATE salary_allocation_run_items SET budget_category_id = NULL "
        f"WHERE id = {run_item_id}",
    )
    for statement in statements:
        with (
            migrated_database.engine.begin() as connection,
            pytest.raises(IntegrityError, match="snapshot cannot be changed"),
        ):
            connection.exec_driver_sql(statement)

    with factory() as unit:
        run = _must(unit.salary_runs.get(run_id))
        item = _must(unit.salary_run_items.get(run_item_id))
    assert run.salary_amount_minor == 100_000
    assert run.template_version == 1
    assert run.subscription_evidence_json == EVIDENCE
    assert item.amount_minor == 10_000
    assert item.name_snapshot == "測試項目"
    assert item.budget_category_id == ledger.category_id


def test_an_item_transition_needs_the_exact_version_and_status(
    factory: UnitOfWorkFactory, ledger: Ledger
) -> None:
    with factory() as unit:
        template_id = _template(unit, ledger)
        item_id = _item(unit, template_id, wallet_id=ledger.other_wallet_id)
        event_id = _salary_event(unit, token="salary_receipt:transition")
        run_id = _run(unit, ledger, template_id=template_id, salary_event_id=event_id)
        run_item_id = _run_item(unit, run_id, ledger, template_item_id=item_id)
        unit.commit()

    with factory() as unit:
        item = _must(unit.salary_run_items.get(run_item_id))
        with pytest.raises(ConcurrentUpdateError):
            unit.salary_run_items.transition(
                run_item_id,
                expected_version=item.version + 5,
                expected_status=SalaryRunItemStatus.DRAFT,
                status=SalaryRunItemStatus.WAITING_TRANSFER,
            )
        unit.rollback()
        with pytest.raises(SalaryAllocationError, match="no longer accepts"):
            unit.salary_run_items.transition(
                run_item_id,
                expected_version=item.version,
                expected_status=SalaryRunItemStatus.WAITING_TRANSFER,
                status=SalaryRunItemStatus.TRANSFERRED,
            )
        unit.rollback()

    with factory() as unit:
        item = _must(unit.salary_run_items.get(run_item_id))
        assert item.status is SalaryRunItemStatus.DRAFT


def test_two_lines_cannot_claim_the_same_transfer_entry(
    factory: UnitOfWorkFactory, ledger: Ledger, migrated_database: Database
) -> None:
    with factory() as unit:
        template_id = _template(unit, ledger)
        first_item = _item(
            unit, template_id, name="第一項", wallet_id=ledger.other_wallet_id
        )
        second_item = _item(
            unit, template_id, name="第二項", wallet_id=ledger.second_wallet_id
        )
        event_id = _salary_event(unit, token="salary_receipt:entry")
        run_id = _run(unit, ledger, template_id=template_id, salary_event_id=event_id)
        first_run_item = _run_item(unit, run_id, ledger, template_item_id=first_item)
        second_run_item = _run_item(unit, run_id, ledger, template_item_id=second_item)
        unit.commit()

    with migrated_database.engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO journal_entries (entry_type, effective_at, recorded_at,"
            " idempotency_key, description, created_at, updated_at) VALUES"
            " ('funding_transfer', '2026-08-06', '2026-08-06', 'k1', '',"
            " '2026-08-06', '2026-08-06')"
        )
    update = (
        "UPDATE salary_allocation_run_items SET status = 'transferred',"
        " transfer_entry_id = 1, transferred_at = '2026-08-06' WHERE id = ?"
    )
    with migrated_database.engine.begin() as connection:
        connection.exec_driver_sql(update, (first_run_item,))
    with (
        migrated_database.engine.begin() as connection,
        pytest.raises(IntegrityError, match="UNIQUE constraint"),
    ):
        connection.exec_driver_sql(update, (second_run_item,))


def test_reserved_minor_counts_card_commitments_and_waiting_salary_lines_once(
    factory: UnitOfWorkFactory, ledger: Ledger, migrated_database: Database
) -> None:
    """Both reservation kinds hold real money back and neither is double counted."""
    with factory() as unit:
        template_id = _template(unit, ledger)
        waiting_item = _item(
            unit, template_id, name="待轉帳", wallet_id=ledger.other_wallet_id
        )
        settled_item = _item(
            unit, template_id, name="同帳戶", wallet_id=ledger.second_wallet_id
        )
        event_id = _salary_event(unit, token="salary_receipt:reserved")
        run_id = _run(
            unit,
            ledger,
            template_id=template_id,
            salary_event_id=event_id,
            planned_amount_minor=30_000,
        )
        waiting_run_item = _run_item(
            unit,
            run_id,
            ledger,
            template_item_id=waiting_item,
            amount_minor=20_000,
        )
        allocated_run_item = _run_item(
            unit,
            run_id,
            ledger,
            template_item_id=settled_item,
            amount_minor=10_000,
            mode=CommitmentFulfillmentMode.RESERVE_ONLY,
        )
        card_event = unit.financial_events.add(
            FinancialEventDraft(
                origin=FinancialEventOrigin.MANUAL,
                event_type=FinancialEventType.PURCHASE,
                payment_method=PaymentMethod.CREDIT_CARD,
                recognition_basis=RecognitionBasis.OBSERVED_MANUAL,
                occurred_at=GENERATED_AT,
                amount_minor=5_000,
                currency="TWD",
                idempotency_key="card:1",
            )
        )
        allocation = unit.allocations.add(
            EventAllocationDraft(
                financial_event_id=card_event.id,
                funding_wallet_id=ledger.source_wallet_id,
                amount_minor=5_000,
                currency="TWD",
            )
        )
        unit.commitments.add(
            FundingCommitmentDraft(
                financial_event_id=card_event.id,
                event_allocation_id=allocation.id,
                source_wallet_id=ledger.source_wallet_id,
                settlement_account_id=ledger.other_account_id,
                amount_minor=5_000,
                currency="TWD",
                fulfillment_mode=CommitmentFulfillmentMode.CROSS_BANK_TRANSFER,
                status=CommitmentStatus.OPEN,
            )
        )
        unit.commit()

    # A draft run reserves nothing at all.
    with factory() as unit:
        assert unit.budget_ledger.reserved_minor(ledger.source_wallet_id) == 5_000
        assert (
            unit.salary_run_items.reserved_minor_for_wallet(ledger.source_wallet_id)
            == 0
        )

    with factory() as unit:
        run = _must(unit.salary_runs.get(run_id))
        unit.salary_runs.approve(
            run_id,
            expected_version=run.version,
            status=SalaryRunStatus.PARTIALLY_FUNDED,
            approval_entry_id=None,
            approved_at=APPROVED_AT,
        )
        for item_id, status in (
            (waiting_run_item, SalaryRunItemStatus.WAITING_TRANSFER),
            (allocated_run_item, SalaryRunItemStatus.ALLOCATED),
        ):
            item = _must(unit.salary_run_items.get(item_id))
            unit.salary_run_items.transition(
                item_id,
                expected_version=item.version,
                expected_status=item.status,
                status=status,
            )
        unit.commit()

    with factory() as unit:
        # 5,000 card commitment plus the 20,000 waiting line; the allocated
        # line has already moved and reserves nothing.
        assert unit.budget_ledger.reserved_minor(ledger.source_wallet_id) == 25_000
        assert (
            unit.salary_run_items.reserved_minor_for_wallet(ledger.source_wallet_id)
            == 20_000
        )
        waiting = unit.salary_run_items.list_waiting_for_source_wallet(
            ledger.source_wallet_id
        )
        assert [item.id for item in waiting] == [waiting_run_item]

    with migrated_database.engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO journal_entries (entry_type, effective_at, recorded_at,"
            " idempotency_key, description, created_at, updated_at) VALUES"
            " ('funding_transfer', '2026-08-06', '2026-08-06', 'reserved-1', '',"
            " '2026-08-06', '2026-08-06')"
        )
    with factory() as unit:
        entry_id = _must(unit.journal.get_by_idempotency_key("reserved-1")).id
        item = _must(unit.salary_run_items.get(waiting_run_item))
        unit.salary_run_items.transition(
            waiting_run_item,
            expected_version=item.version,
            expected_status=SalaryRunItemStatus.WAITING_TRANSFER,
            status=SalaryRunItemStatus.TRANSFERRED,
            transfer_entry_id=entry_id,
            transferred_at=APPROVED_AT,
        )
        unit.commit()

    # Once the money has really moved the line stops reserving anything; only
    # the card commitment is left.
    with factory() as unit:
        assert unit.budget_ledger.reserved_minor(ledger.source_wallet_id) == 5_000
        assert (
            unit.salary_run_items.reserved_minor_for_wallet(ledger.source_wallet_id)
            == 0
        )
        assert unit.budget_ledger.reserved_minor(ledger.other_wallet_id) == 0


def test_a_cancelled_runs_items_never_count_as_a_reservation(
    factory: UnitOfWorkFactory, ledger: Ledger
) -> None:
    with factory() as unit:
        template_id = _template(unit, ledger)
        item_id = _item(unit, template_id, wallet_id=ledger.other_wallet_id)
        event_id = _salary_event(unit, token="salary_receipt:cancelled")
        run_id = _run(unit, ledger, template_id=template_id, salary_event_id=event_id)
        run_item_id = _run_item(unit, run_id, ledger, template_item_id=item_id)
        run = _must(unit.salary_runs.get(run_id))
        unit.salary_runs.approve(
            run_id,
            expected_version=run.version,
            status=SalaryRunStatus.APPROVED,
            approval_entry_id=None,
            approved_at=APPROVED_AT,
        )
        item = _must(unit.salary_run_items.get(run_item_id))
        unit.salary_run_items.transition(
            run_item_id,
            expected_version=item.version,
            expected_status=item.status,
            status=SalaryRunItemStatus.WAITING_TRANSFER,
        )
        unit.commit()

    with factory() as unit:
        assert unit.budget_ledger.reserved_minor(ledger.source_wallet_id) == 10_000

    # A run that is neither approved nor partially funded reserves nothing,
    # which is what keeps a stale generation from holding money forever.
    with factory() as unit:
        run = _must(unit.salary_runs.get(run_id))
        unit.salary_runs.refresh_status(
            run_id,
            expected_version=run.version,
            expected_status=SalaryRunStatus.APPROVED,
            status=SalaryRunStatus.FUNDED,
        )
        unit.commit()
    with factory() as unit:
        assert unit.budget_ledger.reserved_minor(ledger.source_wallet_id) == 0


def test_an_item_can_be_looked_up_by_the_entry_that_moved_its_money(
    factory: UnitOfWorkFactory, ledger: Ledger, migrated_database: Database
) -> None:
    with factory() as unit:
        template_id = _template(unit, ledger)
        item_id = _item(unit, template_id, wallet_id=ledger.other_wallet_id)
        event_id = _salary_event(unit, token="salary_receipt:lookup")
        run_id = _run(unit, ledger, template_id=template_id, salary_event_id=event_id)
        run_item_id = _run_item(unit, run_id, ledger, template_item_id=item_id)
        unit.commit()
    with migrated_database.engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO journal_entries (entry_type, effective_at, recorded_at,"
            " idempotency_key, description, created_at, updated_at) VALUES"
            " ('funding_transfer', '2026-08-06', '2026-08-06', 'k2', '',"
            " '2026-08-06', '2026-08-06')"
        )
        connection.exec_driver_sql(
            "UPDATE salary_allocation_run_items SET status = 'transferred',"
            " transfer_entry_id = 1, transferred_at = '2026-08-06' WHERE id = ?",
            (run_item_id,),
        )

    with factory() as unit:
        found = unit.salary_run_items.get_by_transfer_entry(1)
    assert found is not None
    assert found.id == run_item_id


def test_a_template_item_update_keeps_its_shape_rule(
    factory: UnitOfWorkFactory, ledger: Ledger
) -> None:
    with factory() as unit:
        template_id = _template(unit, ledger)
        item_id = _item(unit, template_id, wallet_id=ledger.other_wallet_id)
        unit.commit()
    with factory() as unit:
        item = _must(unit.salary_template_items.get(item_id))
        with pytest.raises(ValueError, match="only a fixed salary item"):
            unit.salary_template_items.save(
                SalaryPlanTemplateItemUpdate(
                    template_item_id=item_id,
                    expected_version=item.version,
                    name=item.name,
                    destination_wallet_id=item.destination_wallet_id,
                    calculation_mode=SalaryCalculationMode.MANUAL,
                    budget_category_id=None,
                    fixed_amount_minor=10_000,
                    display_order=0,
                    is_active=True,
                )
            )
        unit.rollback()

    with factory() as unit:
        assert (
            _must(unit.salary_template_items.get(item_id)).calculation_mode
            is SalaryCalculationMode.FIXED
        )


def test_a_template_knows_when_something_already_depends_on_it(
    factory: UnitOfWorkFactory, ledger: Ledger
) -> None:
    with factory() as unit:
        template_id = _template(unit, ledger)
        assert unit.salary_templates.has_items_or_runs(template_id) is False
        item_id = _item(unit, template_id, wallet_id=ledger.other_wallet_id)
        assert unit.salary_templates.has_items_or_runs(template_id) is True
        assert unit.salary_template_items.is_referenced(item_id) is False
        event_id = _salary_event(unit, token="salary_receipt:referenced")
        run_id = _run(unit, ledger, template_id=template_id, salary_event_id=event_id)
        _run_item(unit, run_id, ledger, template_item_id=item_id)
        assert unit.salary_template_items.is_referenced(item_id) is True
        unit.commit()

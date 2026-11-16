"""Budget category, period, target, allocation, and commitment repositories."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from app.domain.budget_enums import (
    BudgetPeriodStatus,
    CommitmentFulfillmentMode,
    CommitmentStatus,
    FinancialAccountType,
    FinancialEventOrigin,
    FinancialEventType,
    JournalEntryType,
    LedgerAccountKind,
    PaymentMethod,
    RecognitionBasis,
    WalletPurpose,
)
from app.domain.budget_models import (
    BudgetCategoryDraft,
    BudgetCategoryUpdate,
    BudgetTargetDraft,
    EventAllocationDraft,
    FinancialAccountDraft,
    FinancialEventDraft,
    FundingCommitmentDraft,
    JournalEntryDraft,
    LedgerAccountDraft,
    Money,
    PostingDraft,
    WalletDraft,
)
from app.domain.errors import (
    BudgetPeriodError,
    ConcurrentUpdateError,
    DatabaseError,
)
from app.infrastructure.database.engine import Database
from app.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork
from sqlalchemy import inspect

OCCURRED_AT = datetime(2026, 8, 7, 13, 30, tzinfo=UTC)
CLOSED_AT = datetime(2026, 9, 1, 2, 0, tzinfo=UTC)


def _wallet(unit: SqlAlchemyUnitOfWork, *, name: str = "尚未分配") -> int:
    account = unit.financial_accounts.get_by_identity(
        institution_code="test_bank", display_name="測試帳戶", currency="TWD"
    )
    if account is None:
        account = unit.financial_accounts.add(
            FinancialAccountDraft(
                institution_code="test_bank",
                display_name="測試帳戶",
                account_type=FinancialAccountType.CHECKING,
                currency="TWD",
            )
        )
    existing = unit.wallets.get_by_name(account.id, name)
    if existing is not None:
        return existing.id
    return unit.wallets.add(
        WalletDraft(
            financial_account_id=account.id,
            name=name,
            purpose=(
                WalletPurpose.UNALLOCATED
                if name == "尚未分配"
                else WalletPurpose.BUDGET
            ),
        )
    ).id


def _event(unit: SqlAlchemyUnitOfWork, *, key: str = "gmail_transaction:1") -> int:
    return unit.financial_events.add(
        FinancialEventDraft(
            origin=FinancialEventOrigin.GMAIL_TRANSACTION,
            event_type=FinancialEventType.PURCHASE,
            payment_method=PaymentMethod.CREDIT_CARD,
            recognition_basis=RecognitionBasis.GMAIL_NOTIFICATION,
            occurred_at=OCCURRED_AT,
            amount_minor=128000,
            currency="TWD",
            idempotency_key=key,
        )
    ).id


def test_categories_round_trip_with_optimistic_versions(
    migrated_database: Database,
) -> None:
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        parent = unit.budget_categories.add(BudgetCategoryDraft(name=" 生活 "))
        child = unit.budget_categories.add(
            BudgetCategoryDraft(name="餐飲", parent_id=parent.id)
        )
        unit.commit()

    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        assert parent.name == "生活"
        assert unit.budget_categories.get(child.id) == child
        assert unit.budget_categories.get(9_999) is None
        assert unit.budget_categories.list_all() == (parent, child)
        renamed = unit.budget_categories.save(
            BudgetCategoryUpdate(
                category_id=child.id,
                expected_version=child.version,
                name="外食",
                parent_id=parent.id,
                is_active=False,
            )
        )
        unit.commit()
        assert renamed.name == "外食"
        assert not renamed.is_active
        assert renamed.version == 2

    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        with pytest.raises(ConcurrentUpdateError):
            unit.budget_categories.save(
                BudgetCategoryUpdate(
                    category_id=child.id,
                    expected_version=1,
                    name="又改一次",
                    parent_id=None,
                    is_active=True,
                )
            )
        with pytest.raises(BudgetPeriodError, match="was not found"):
            unit.budget_categories.save(
                BudgetCategoryUpdate(
                    category_id=9_999,
                    expected_version=1,
                    name="不存在",
                    parent_id=None,
                    is_active=True,
                )
            )


def test_a_referenced_category_is_detected(migrated_database: Database) -> None:
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        unused = unit.budget_categories.add(BudgetCategoryDraft(name="沒人用"))
        parent = unit.budget_categories.add(BudgetCategoryDraft(name="有子類"))
        unit.budget_categories.add(
            BudgetCategoryDraft(name="子類", parent_id=parent.id)
        )
        allocated = unit.budget_categories.add(BudgetCategoryDraft(name="被分配"))
        wallet_id = _wallet(unit)
        event_id = _event(unit)
        unit.allocations.add(
            EventAllocationDraft(
                financial_event_id=event_id,
                funding_wallet_id=wallet_id,
                amount_minor=128000,
                currency="TWD",
                budget_category_id=allocated.id,
            )
        )
        unit.commit()

        assert not unit.budget_categories.is_referenced(unused.id)
        assert unit.budget_categories.is_referenced(parent.id)
        assert unit.budget_categories.is_referenced(allocated.id)


def test_root_and_child_category_names_are_unique(
    migrated_database: Database,
) -> None:
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        parent = unit.budget_categories.add(BudgetCategoryDraft(name="生活"))
        unit.budget_categories.add(
            BudgetCategoryDraft(name="餐飲", parent_id=parent.id)
        )
        unit.commit()

    with (
        SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit,
        pytest.raises(DatabaseError, match="database write failed"),
    ):
        unit.budget_categories.add(BudgetCategoryDraft(name="生活"))

    with (
        SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit,
        pytest.raises(DatabaseError, match="database write failed"),
    ):
        unit.budget_categories.add(
            BudgetCategoryDraft(name="餐飲", parent_id=parent.id)
        )


def test_periods_are_created_once_and_closed_once(
    migrated_database: Database,
) -> None:
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        first = unit.budget_periods.get_or_create(year=2026, month=8, currency="twd")
        again = unit.budget_periods.get_or_create(year=2026, month=8, currency="TWD")
        other = unit.budget_periods.get_or_create(year=2026, month=8, currency="JPY")
        unit.commit()

        assert first.id == again.id
        assert first.currency == "TWD"
        assert first.label == "2026-08"
        assert first.status is BudgetPeriodStatus.OPEN
        assert other.id != first.id
        assert unit.budget_periods.find(year=2026, month=8, currency="TWD") == first
        assert unit.budget_periods.find(year=2026, month=9, currency="TWD") is None
        assert unit.budget_periods.get(9_999) is None

        closed = unit.budget_periods.close(first.id, closed_at=CLOSED_AT)
        assert closed.status is BudgetPeriodStatus.CLOSED
        assert closed.closed_at == CLOSED_AT
        assert closed.version == 2
        with pytest.raises(BudgetPeriodError, match="already closed"):
            unit.budget_periods.close(first.id, closed_at=CLOSED_AT)
        with pytest.raises(BudgetPeriodError, match="was not found"):
            unit.budget_periods.close(9_999, closed_at=CLOSED_AT)


def test_targets_upsert_with_optimistic_versions(
    migrated_database: Database,
) -> None:
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        period = unit.budget_periods.get_or_create(year=2026, month=8, currency="TWD")
        category = unit.budget_categories.add(BudgetCategoryDraft(name="生活"))
        wallet_id = _wallet(unit)
        destination_id = _wallet(unit, name="結餘")
        created = unit.budget_targets.upsert(
            BudgetTargetDraft(
                budget_period_id=period.id,
                budget_category_id=category.id,
                target_minor=3_000_000,
                buffer_minor=500_000,
                funding_wallet_id=wallet_id,
            )
        )
        updated = unit.budget_targets.upsert(
            BudgetTargetDraft(
                budget_period_id=period.id,
                budget_category_id=category.id,
                target_minor=3_500_000,
                buffer_minor=500_000,
                funding_wallet_id=wallet_id,
                month_end_destination_wallet_id=destination_id,
                expected_version=created.version,
            )
        )
        unit.commit()

        assert created.version == 1
        assert created.month_end_destination_wallet_id is None
        assert updated.id == created.id
        assert updated.target_minor == 3_500_000
        assert updated.month_end_destination_wallet_id == destination_id
        assert updated.version == 2
        assert unit.budget_targets.get(updated.id) == updated
        assert unit.budget_targets.get(9_999) is None
        assert unit.budget_targets.list_for_period(period.id) == (updated,)

        # Optimistic locking is never optional in either direction.
        with pytest.raises(ConcurrentUpdateError, match="changed after"):
            unit.budget_targets.upsert(
                BudgetTargetDraft(
                    budget_period_id=period.id,
                    budget_category_id=category.id,
                    target_minor=1,
                    buffer_minor=0,
                    funding_wallet_id=wallet_id,
                    expected_version=1,
                )
            )
        with pytest.raises(ConcurrentUpdateError, match="already exists"):
            unit.budget_targets.upsert(
                BudgetTargetDraft(
                    budget_period_id=period.id,
                    budget_category_id=category.id,
                    target_minor=1,
                    buffer_minor=0,
                    funding_wallet_id=wallet_id,
                )
            )
        other = unit.budget_categories.add(BudgetCategoryDraft(name="交通"))
        with pytest.raises(ConcurrentUpdateError, match="no longer exists"):
            unit.budget_targets.upsert(
                BudgetTargetDraft(
                    budget_period_id=period.id,
                    budget_category_id=other.id,
                    target_minor=1,
                    buffer_minor=0,
                    funding_wallet_id=wallet_id,
                    expected_version=1,
                )
            )


def test_ledger_accounts_can_be_found_by_category_and_currency(
    migrated_database: Database,
) -> None:
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        category = unit.budget_categories.add(BudgetCategoryDraft(name="生活"))
        twd = unit.ledger_accounts.add(
            LedgerAccountDraft(
                code="expense:category:1:TWD",
                name="生活",
                kind=LedgerAccountKind.EXPENSE,
                currency="TWD",
                budget_category_id=category.id,
            )
        )
        jpy = unit.ledger_accounts.add(
            LedgerAccountDraft(
                code="expense:category:1:JPY",
                name="生活",
                kind=LedgerAccountKind.EXPENSE,
                currency="JPY",
                budget_category_id=category.id,
            )
        )
        unit.commit()

        assert twd.budget_category_id == category.id
        assert unit.ledger_accounts.get_for_budget_category(category.id, "twd") == twd
        assert unit.ledger_accounts.get_for_budget_category(category.id, "JPY") == jpy
        assert unit.ledger_accounts.get_for_budget_category(9_999, "TWD") is None

    with (
        SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit,
        pytest.raises(DatabaseError, match="database write failed"),
    ):
        unit.ledger_accounts.add(
            LedgerAccountDraft(
                code="expense:category:1:TWD:duplicate",
                name="重複",
                kind=LedgerAccountKind.EXPENSE,
                currency="TWD",
                budget_category_id=category.id,
            )
        )


def test_only_a_nominal_expense_ledger_may_carry_a_category(
    migrated_database: Database,
) -> None:
    """The database refuses a category on a non-expense or physical ledger."""
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        category = unit.budget_categories.add(BudgetCategoryDraft(name="生活"))
        account = unit.financial_accounts.add(
            FinancialAccountDraft(
                institution_code="test_bank",
                display_name="測試帳戶",
                account_type=FinancialAccountType.CHECKING,
                currency="TWD",
            )
        )
        unit.commit()

    with (
        SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit,
        pytest.raises(DatabaseError, match="database write failed"),
    ):
        unit.ledger_accounts.add(
            LedgerAccountDraft(
                code="equity:with-category",
                name="不該有分類",
                kind=LedgerAccountKind.EQUITY,
                currency="TWD",
                budget_category_id=category.id,
            )
        )

    with (
        SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit,
        pytest.raises(DatabaseError, match="database write failed"),
    ):
        unit.ledger_accounts.add(
            LedgerAccountDraft(
                code="expense:physical",
                name="不該同時是實體帳戶",
                kind=LedgerAccountKind.EXPENSE,
                currency="TWD",
                financial_account_id=account.id,
                budget_category_id=category.id,
            )
        )


def test_a_category_update_is_conditional_on_its_version(
    migrated_database: Database,
) -> None:
    """An interleaved writer must not be overwritten by a stale form."""
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        category = unit.budget_categories.add(BudgetCategoryDraft(name="生活"))
        unit.commit()

    # Another writer commits between this caller's read and its write.
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as other:
        other.budget_categories.save(
            BudgetCategoryUpdate(
                category_id=category.id,
                expected_version=category.version,
                name="別人改的",
                parent_id=None,
                is_active=True,
            )
        )
        other.commit()

    with (
        SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit,
        pytest.raises(ConcurrentUpdateError, match="changed after"),
    ):
        unit.budget_categories.save(
            BudgetCategoryUpdate(
                category_id=category.id,
                expected_version=category.version,
                name="我改的",
                parent_id=None,
                is_active=True,
            )
        )

    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        stored = unit.budget_categories.get(category.id)
        assert stored is not None
        assert stored.name == "別人改的"
        assert stored.version == 2


def test_a_refund_link_survives_the_repository_round_trip(
    migrated_database: Database,
) -> None:
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        original_id = _event(unit)
        refund = unit.financial_events.add(
            FinancialEventDraft(
                origin=FinancialEventOrigin.GMAIL_TRANSACTION,
                event_type=FinancialEventType.REFUND,
                payment_method=PaymentMethod.CREDIT_CARD,
                recognition_basis=RecognitionBasis.GMAIL_NOTIFICATION,
                occurred_at=OCCURRED_AT,
                amount_minor=28000,
                currency="TWD",
                idempotency_key="gmail_transaction:2",
                refund_of_event_id=original_id,
            )
        )
        unit.commit()

        assert refund.refund_of_event_id == original_id
        stored = unit.financial_events.get(refund.id)
        assert stored is not None
        assert stored.refund_of_event_id == original_id
        original = unit.financial_events.get(original_id)
        assert original is not None
        assert original.refund_of_event_id is None

    with (
        SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit,
        pytest.raises(DatabaseError, match="database write failed"),
    ):
        # The database refuses a non-refund carrying a refund link.
        unit.financial_events.add(
            FinancialEventDraft(
                origin=FinancialEventOrigin.GMAIL_TRANSACTION,
                event_type=FinancialEventType.PURCHASE,
                payment_method=PaymentMethod.CREDIT_CARD,
                recognition_basis=RecognitionBasis.GMAIL_NOTIFICATION,
                occurred_at=OCCURRED_AT,
                amount_minor=1000,
                currency="TWD",
                idempotency_key="gmail_transaction:3",
                refund_of_event_id=original_id,
            )
        )


def test_allocations_and_commitments_round_trip(
    migrated_database: Database,
) -> None:
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        wallet_id = _wallet(unit)
        account = unit.financial_accounts.get_by_identity(
            institution_code="test_bank", display_name="測試帳戶", currency="TWD"
        )
        assert account is not None
        event_id = _event(unit)
        category = unit.budget_categories.add(BudgetCategoryDraft(name="生活"))
        allocation = unit.allocations.add(
            EventAllocationDraft(
                financial_event_id=event_id,
                funding_wallet_id=wallet_id,
                amount_minor=128000,
                currency="twd",
                budget_category_id=category.id,
            )
        )
        commitment = unit.commitments.add(
            FundingCommitmentDraft(
                financial_event_id=event_id,
                event_allocation_id=allocation.id,
                source_wallet_id=wallet_id,
                settlement_account_id=account.id,
                amount_minor=128000,
                currency="TWD",
                fulfillment_mode=CommitmentFulfillmentMode.RESERVE_ONLY,
            )
        )
        unit.commit()

        assert allocation.currency == "TWD"
        assert unit.allocations.list_for_event(event_id) == (allocation,)
        assert commitment.status is CommitmentStatus.OPEN
        assert commitment.outstanding_minor == 128000
        assert unit.commitments.get(commitment.id) == commitment
        assert unit.commitments.get(9_999) is None
        assert unit.commitments.list_for_event(event_id) == (commitment,)
        assert unit.commitments.list_open_for_wallet(wallet_id) == (commitment,)


def test_a_reservation_is_never_released_twice(
    migrated_database: Database,
) -> None:
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        wallet_id = _wallet(unit)
        account = unit.financial_accounts.get_by_identity(
            institution_code="test_bank", display_name="測試帳戶", currency="TWD"
        )
        assert account is not None
        event_id = _event(unit)
        allocation = unit.allocations.add(
            EventAllocationDraft(
                financial_event_id=event_id,
                funding_wallet_id=wallet_id,
                amount_minor=128000,
                currency="TWD",
            )
        )
        commitment = unit.commitments.add(
            FundingCommitmentDraft(
                financial_event_id=event_id,
                event_allocation_id=allocation.id,
                source_wallet_id=wallet_id,
                settlement_account_id=account.id,
                amount_minor=128000,
                currency="TWD",
                fulfillment_mode=CommitmentFulfillmentMode.CROSS_BANK_TRANSFER,
            )
        )
        unit.commit()

        partial = unit.commitments.release(commitment.id, 28000)
        assert partial.released_minor == 28000
        assert partial.outstanding_minor == 100000
        assert partial.status is CommitmentStatus.OPEN

        with pytest.raises(BudgetPeriodError, match="still reserves"):
            unit.commitments.release(commitment.id, 100001)
        with pytest.raises(ValueError, match="at least 1 minor units"):
            unit.commitments.release(commitment.id, 0)
        with pytest.raises(TypeError, match="integer minor units"):
            unit.commitments.release(commitment.id, True)

        finished = unit.commitments.release(commitment.id, 100000)
        assert finished.outstanding_minor == 0
        assert finished.status is CommitmentStatus.CANCELLED
        assert unit.commitments.list_open_for_wallet(wallet_id) == ()
        with pytest.raises(BudgetPeriodError, match="still reserves"):
            unit.commitments.release(commitment.id, 1)
        with pytest.raises(BudgetPeriodError, match="was not found"):
            unit.commitments.release(9_999, 1)


def test_one_commitment_per_allocation(migrated_database: Database) -> None:
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        wallet_id = _wallet(unit)
        account = unit.financial_accounts.get_by_identity(
            institution_code="test_bank", display_name="測試帳戶", currency="TWD"
        )
        assert account is not None
        event_id = _event(unit)
        allocation = unit.allocations.add(
            EventAllocationDraft(
                financial_event_id=event_id,
                funding_wallet_id=wallet_id,
                amount_minor=128000,
                currency="TWD",
            )
        )
        draft = FundingCommitmentDraft(
            financial_event_id=event_id,
            event_allocation_id=allocation.id,
            source_wallet_id=wallet_id,
            settlement_account_id=account.id,
            amount_minor=128000,
            currency="TWD",
            fulfillment_mode=CommitmentFulfillmentMode.ACCOUNT_TRANSFER,
        )
        unit.commitments.add(draft)
        with pytest.raises(DatabaseError, match="database write failed"):
            unit.commitments.add(draft)


def test_a_financial_event_may_carry_the_entry_that_replaced_its_last(
    migrated_database: Database,
) -> None:
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        wallet_id = _wallet(unit)
        account = unit.financial_accounts.get_by_identity(
            institution_code="test_bank", display_name="測試帳戶", currency="TWD"
        )
        assert account is not None
        asset = unit.ledger_accounts.add(
            LedgerAccountDraft(
                code="asset:test_bank:1",
                name="測試帳戶",
                kind=LedgerAccountKind.ASSET,
                currency="TWD",
                financial_account_id=account.id,
            )
        )
        expense = unit.ledger_accounts.add(
            LedgerAccountDraft(
                code="expense:category:1:TWD",
                name="生活",
                kind=LedgerAccountKind.EXPENSE,
                currency="TWD",
            )
        )
        event_id = _event(unit)
        postings = (
            PostingDraft(ledger_account_id=expense.id, amount=Money(128000, "TWD")),
            PostingDraft(
                ledger_account_id=asset.id,
                amount=Money(-128000, "TWD"),
                wallet_id=wallet_id,
            ),
        )
        stored = unit.journal.add(
            JournalEntryDraft(
                entry_type=JournalEntryType.EXPENSE,
                effective_at=OCCURRED_AT,
                recorded_at=OCCURRED_AT,
                idempotency_key="allocation:financial_event:1",
                description="消費",
                postings=postings,
                financial_event_id=event_id,
            )
        )
        unit.commit()

        assert stored.financial_event_id == event_id
        assert unit.journal.get_by_financial_event(event_id) == stored
        assert unit.journal.get_by_financial_event(9_999) is None
        assert stored.postings[0].budget_category_id is None

    # A redone allocation leaves the superseded entry in place and stages a
    # replacement, so the column is no longer unique (ADR-B103). "At most one
    # entry still in force" depends on whether another row reverses this one,
    # which no index can express, so the posting service enforces it and this
    # layer only has to resolve which entry that is.
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        replacement = unit.journal.add(
            JournalEntryDraft(
                entry_type=JournalEntryType.EXPENSE,
                effective_at=OCCURRED_AT,
                recorded_at=OCCURRED_AT,
                idempotency_key="allocation:financial_event:1:replacement",
                description="第二筆",
                postings=postings,
                financial_event_id=event_id,
            )
        )
        unit.commit()
        assert unit.journal.get_by_financial_event(event_id) == replacement


def test_budget_tables_carry_no_parser_or_email_columns(
    migrated_database: Database,
) -> None:
    """Budget rows describe intent, never parser evidence or Email content."""
    inspector = inspect(migrated_database.engine)
    forbidden = {"confidence", "merchant", "body_text", "snippet", "sender"}

    for table in (
        "budget_categories",
        "budget_periods",
        "budget_targets",
        "event_allocations",
        "funding_commitments",
    ):
        columns = {item["name"] for item in inspector.get_columns(table)}
        assert not (columns & forbidden), table

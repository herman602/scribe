"""B7 acceptance tests for the daily spending view and its totals."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from app.application.services.bank_movements import (
    AccountTransferRequest,
    BankMovementRequest,
    ManualRefundRequest,
)
from app.application.services.event_allocation import (
    AllocateFinancialEventRequest,
    AllocationLine,
    RedoAllocationRequest,
)
from app.application.services.ledger import (
    JournalService,
    LedgerSetupService,
    OpenFinancialAccountRequest,
    OpeningPositionRequest,
)
from app.application.services.manual_money import (
    ManualExpenseRequest,
    ReverseEventRequest,
)
from app.domain.budget_enums import (
    BankMovementKind,
    FinancialAccountType,
    FinancialEventOrigin,
    FinancialEventStatus,
    FinancialEventType,
    JournalEntryType,
    PaymentMethod,
    RecognitionBasis,
)
from app.domain.budget_models import FinancialEventDraft, OpeningWalletSplit
from app.domain.errors import AllocationRedoError, FinancialEventError

from tests.b7_support import B7_NOW, B7Services, B7World

#: 2026-08-09 04:30Z is 12:30 on 2026-08-10 in Taipei… no: +8 makes it 12:30
#: on the same day. The boundary cases below are chosen explicitly instead of
#: being derived, so a timezone mistake cannot make the test agree with itself.
LOCAL_DAY = date(2026, 8, 9)
#: 2026-08-08 16:00Z is 2026-08-09 00:00 in Taipei: the first instant of the day.
DAY_START_UTC = datetime(2026, 8, 8, 16, 0, tzinfo=UTC)
#: One second earlier still belongs to the previous local day.
BEFORE_DAY_UTC = DAY_START_UTC - timedelta(seconds=1)


def _cash(
    b7: B7Services,
    world: B7World,
    *,
    token: str,
    amount_minor: int = 12_000,
    occurred_at: datetime = B7_NOW,
    merchant: str = "早餐店",
    category_id: int | None = None,
) -> int:
    result = b7.expenses.execute(
        ManualExpenseRequest(
            payment_method=PaymentMethod.CASH,
            occurred_at=occurred_at,
            merchant=merchant,
            amount_minor=amount_minor,
            currency="TWD",
            funding_wallet_id=world.cash_wallet_id,
            client_token=token,
            budget_category_id=category_id or world.food_id,
        )
    )
    return result.event.id


def _card(
    b7: B7Services,
    world: B7World,
    *,
    token: str,
    amount_minor: int = 45_000,
    occurred_at: datetime = B7_NOW,
) -> int:
    result = b7.expenses.execute(
        ManualExpenseRequest(
            payment_method=PaymentMethod.CREDIT_CARD,
            occurred_at=occurred_at,
            merchant="咖啡廳",
            amount_minor=amount_minor,
            currency="TWD",
            funding_wallet_id=world.bank_unallocated_id,
            client_token=token,
            budget_category_id=world.food_id,
            card_id=world.card_id,
        )
    )
    return result.event.id


def _total(b7: B7Services, day: date, method: PaymentMethod) -> int:
    view = b7.spending.daily(day=day)
    return sum(total.amount_minor for total in view.totals_for(method))


def test_a_local_day_starts_at_local_midnight(
    b7: B7Services, b7_world: B7World
) -> None:
    _cash(b7, b7_world, token="inside", occurred_at=DAY_START_UTC)
    _cash(b7, b7_world, token="outside", occurred_at=BEFORE_DAY_UTC)

    view = b7.spending.daily(day=LOCAL_DAY)

    assert view.total_rows == 1
    assert _total(b7, LOCAL_DAY, PaymentMethod.CASH) == 12_000
    assert _total(b7, LOCAL_DAY - timedelta(days=1), PaymentMethod.CASH) == 12_000


def test_rows_are_newest_first_with_a_stable_tie_break(
    b7: B7Services, b7_world: B7World
) -> None:
    first = _cash(b7, b7_world, token="one", occurred_at=B7_NOW)
    second = _cash(b7, b7_world, token="two", occurred_at=B7_NOW)
    third = _cash(b7, b7_world, token="three", occurred_at=B7_NOW + timedelta(hours=1))

    view = b7.spending.daily(day=LOCAL_DAY)
    ordered = [row.financial_event_id for row in view.rows]

    assert ordered[0] == third
    # Same instant: the later entry id wins, every time the page is loaded.
    assert ordered[1:] == [second, first]
    reloaded = b7.spending.daily(day=LOCAL_DAY)
    assert [row.financial_event_id for row in reloaded.rows] == ordered


def test_the_cash_and_card_filters_select_one_instrument(
    b7: B7Services, b7_world: B7World
) -> None:
    _cash(b7, b7_world, token="cash")
    _card(b7, b7_world, token="card")

    everything = b7.spending.daily(day=LOCAL_DAY, payment_filter="all")
    cash_only = b7.spending.daily(day=LOCAL_DAY, payment_filter="cash")
    card_only = b7.spending.daily(day=LOCAL_DAY, payment_filter="card")

    assert everything.total_rows == 2
    assert cash_only.total_rows == 1
    assert card_only.total_rows == 1
    assert cash_only.rows[0].payment_method is PaymentMethod.CASH
    assert card_only.rows[0].payment_method is PaymentMethod.CREDIT_CARD


def test_totals_are_split_by_currency_and_never_added(
    b7: B7Services, b7_world: B7World
) -> None:
    setup = LedgerSetupService(b7.factory)
    yen = setup.open_account(
        OpenFinancialAccountRequest(
            institution_code="wallet",
            display_name="日圓現金",
            account_type=FinancialAccountType.CASH,
            currency="JPY",
        )
    )
    JournalService(b7.factory).record_opening_position(
        OpeningPositionRequest(
            financial_account_id=yen.account.id,
            balance_minor=30_000,
            observed_at=B7_NOW,
            splits=(OpeningWalletSplit(wallet_name="尚未分配", amount_minor=30_000),),
        )
    )
    with b7.factory() as unit:
        yen_wallet = unit.wallets.get_unallocated(yen.account.id)
        assert yen_wallet is not None
        yen_wallet_id = yen_wallet.id

    _cash(b7, b7_world, token="twd")
    b7.expenses.execute(
        ManualExpenseRequest(
            payment_method=PaymentMethod.CASH,
            occurred_at=B7_NOW,
            merchant="拉麵",
            amount_minor=1_200,
            currency="JPY",
            funding_wallet_id=yen_wallet_id,
            client_token="jpy",
            budget_category_id=b7_world.food_id,
        )
    )

    totals = b7.spending.daily(day=LOCAL_DAY).totals_for(PaymentMethod.CASH)

    assert {total.currency: total.amount_minor for total in totals} == {
        "TWD": 12_000,
        "JPY": 1_200,
    }


def test_a_refund_subtracts_from_the_day_it_happened(
    b7: B7Services, b7_world: B7World
) -> None:
    original = _card(b7, b7_world, token="card")
    b7.movements.refund(
        ManualRefundRequest(
            original_event_id=original,
            occurred_at=B7_NOW + timedelta(hours=1),
            amount_minor=15_000,
            client_token="refund",
        )
    )

    assert _total(b7, LOCAL_DAY, PaymentMethod.CREDIT_CARD) == 45_000 - 15_000
    rows = b7.spending.daily(day=LOCAL_DAY).rows
    assert any(row.reduces_spending for row in rows)


def test_only_a_spending_decision_may_be_redone(
    b7: B7Services, b7_world: B7World
) -> None:
    """An external movement and a refund are both refused, for its own reason.

    A deposit's entry records money entering the bank, not a choice about
    which wallet pays: reversing it would cancel a real movement and leave
    the event in a queue whose form cannot accept it. A refund gives back
    part of the original purchase's reservation as it posts, and reversing
    its entry does not take that back (ADR-B103).
    """
    deposit = b7.movements.execute(
        BankMovementRequest(
            kind=BankMovementKind.EXTERNAL_DEPOSIT,
            occurred_at=B7_NOW,
            amount_minor=50_000,
            currency="TWD",
            wallet_id=b7_world.bank_unallocated_id,
            description="薪資入帳",
            client_token="movement-redo",
        )
    )
    with b7.factory() as unit:
        event = unit.financial_events.get(deposit.event.id)
    assert event is not None
    assert event.status is FinancialEventStatus.POSTED

    with pytest.raises(AllocationRedoError, match="external movement"):
        b7.redo.execute(
            RedoAllocationRequest(
                financial_event_id=event.id, expected_version=event.version
            )
        )

    # And the page does not offer what the service would refuse.
    detail = b7.spending.event(event.id)
    assert detail is not None
    assert detail.has_allocation is False
    assert detail.is_redoable is False
    # Still posted: the refusal changed nothing.
    with b7.factory() as unit:
        after = unit.financial_events.get(event.id)
    assert after is not None
    assert after.status is FinancialEventStatus.POSTED

    # A refund carries an allocation, so it is refused for the other reason.
    original = _card(b7, b7_world, token="refund-redo-original")
    refund = b7.movements.refund(
        ManualRefundRequest(
            original_event_id=original,
            occurred_at=B7_NOW + timedelta(hours=1),
            amount_minor=15_000,
            client_token="refund-redo",
        )
    )
    with pytest.raises(AllocationRedoError, match="refund"):
        b7.redo.execute(
            RedoAllocationRequest(
                financial_event_id=refund.event.id,
                expected_version=refund.event.version,
            )
        )
    refund_detail = b7.spending.event(refund.event.id)
    assert refund_detail is not None
    assert refund_detail.is_redoable is False


def test_a_redone_allocation_reads_as_history_not_as_a_cancellation(
    b7: B7Services, b7_world: B7World
) -> None:
    """The event page says it was reposted, not that it was cancelled.

    Redoing an allocation leaves a cancellation behind, and reading "this was
    cancelled" off the mere existence of one would describe money that is
    still posted. The page resolves the entry in force instead, and lists
    every step so the superseded decision is still visible.
    """
    event_id = _cash(b7, b7_world, token="redo-history")
    with b7.factory() as unit:
        event = unit.financial_events.get(event_id)
    assert event is not None

    undone = b7.redo.execute(
        RedoAllocationRequest(
            financial_event_id=event_id, expected_version=event.version
        )
    )
    b7.allocate.execute(
        AllocateFinancialEventRequest(
            financial_event_id=event_id,
            expected_version=undone.event.version,
            client_token="redo-history-second",
            lines=(
                AllocationLine(
                    amount_minor=12_000,
                    funding_wallet_id=b7_world.cash_wallet_id,
                    budget_category_id=b7_world.food_id,
                ),
            ),
        )
    )

    detail = b7.spending.event(event_id)
    assert detail is not None
    # Posted again, so nothing about it is cancelled.
    assert detail.reversal_entry is None
    assert detail.event.status is FinancialEventStatus.POSTED
    # Posted, taken back, posted again.
    assert [row.is_cancellation for row in detail.history] == [False, True, False]
    assert [row.is_in_force for row in detail.history] == [False, False, True]
    # The cancellation is the negative of what it took back, so the three
    # amounts add up to one expense.
    assert sum(row.amount_minor for row in detail.history) == 12_000


def test_a_redone_allocation_names_its_own_entry(
    b7: B7Services, b7_world: B7World
) -> None:
    """Each generation of a split belongs to the entry that carried it.

    Without that link every reader had to guess which rows are in force, and
    they guessed differently: the replay check compared its lines against
    both generations at once and called a plain resubmission a different
    allocation (ADR-B103).
    """
    event_id = _cash(b7, b7_world, token="link")
    with b7.factory() as unit:
        event = unit.financial_events.get(event_id)
        first_entry = unit.journal.get_by_financial_event(event_id)
    assert event is not None and first_entry is not None

    undone = b7.redo.execute(
        RedoAllocationRequest(
            financial_event_id=event_id, expected_version=event.version
        )
    )
    second = b7.allocate.execute(
        AllocateFinancialEventRequest(
            financial_event_id=event_id,
            expected_version=undone.event.version,
            client_token="link-second",
            lines=(
                AllocationLine(
                    amount_minor=12_000,
                    funding_wallet_id=b7_world.cash_wallet_id,
                    budget_category_id=b7_world.transport_id,
                ),
            ),
        )
    )

    with b7.factory() as unit:
        every = unit.allocations.list_for_event(event_id)
        superseded = unit.allocations.list_for_entry(first_entry.id)
        in_force = unit.allocations.list_for_entry(second.entry.id)
    # Both generations are kept, and each names the entry it was posted with.
    assert len(every) == 2
    assert [item.journal_entry_id for item in superseded] == [first_entry.id]
    assert [item.journal_entry_id for item in in_force] == [second.entry.id]
    assert superseded[0].budget_category_id == b7_world.food_id
    assert in_force[0].budget_category_id == b7_world.transport_id

    # Resubmitting the second form is its own replay, not a conflict.
    again = b7.allocate.execute(
        AllocateFinancialEventRequest(
            financial_event_id=event_id,
            expected_version=undone.event.version,
            client_token="link-second",
            lines=(
                AllocationLine(
                    amount_minor=12_000,
                    funding_wallet_id=b7_world.cash_wallet_id,
                    budget_category_id=b7_world.transport_id,
                ),
            ),
        )
    )
    assert again.entry.id == second.entry.id
    assert [item.id for item in again.allocations] == [item.id for item in in_force]


def test_a_deposit_has_a_history_even_though_it_is_not_spending(
    b7: B7Services, b7_world: B7World
) -> None:
    """An entry with no expense side is described by the money side.

    The history joined the expense side alone, so a deposit, an interest
    credit, or a reconciliation adjustment showed nothing at all — the
    entries a reader is most likely to question when a balance disagrees.
    """
    deposit = b7.movements.execute(
        BankMovementRequest(
            kind=BankMovementKind.EXTERNAL_DEPOSIT,
            occurred_at=B7_NOW,
            amount_minor=50_000,
            currency="TWD",
            wallet_id=b7_world.bank_unallocated_id,
            description="薪資入帳",
            client_token="deposit-history",
        )
    )

    detail = b7.spending.event(deposit.event.id)
    assert detail is not None
    assert [row.journal_entry_id for row in detail.history] == [deposit.entry.id]
    # Money came in, so the one figure is positive.
    assert [row.amount_minor for row in detail.history] == [50_000]
    assert [row.is_in_force for row in detail.history] == [True]

    # Cancelled, the pair reads as what came in and what was taken back —
    # which is the case the page actually shows a history for.
    b7.reversal.execute(
        ReverseEventRequest(
            financial_event_id=deposit.event.id,
            expected_version=deposit.event.version,
        )
    )
    cancelled = b7.spending.event(deposit.event.id)
    assert cancelled is not None
    assert [row.amount_minor for row in cancelled.history] == [50_000, -50_000]
    assert [row.is_cancellation for row in cancelled.history] == [False, True]
    assert sum(row.amount_minor for row in cancelled.history) == 0


def test_a_form_whose_allocation_was_taken_back_is_not_replayed(
    b7: B7Services, b7_world: B7World
) -> None:
    """A stale form is refused rather than answered with a dead entry.

    Its token does name an entry, but that entry has been reversed and the
    event is waiting to be decided again. Replaying it would tell the page
    the expense was posted while nothing is.
    """
    event_id = _cash(b7, b7_world, token="stale")
    with b7.factory() as unit:
        event = unit.financial_events.get(event_id)
    assert event is not None

    undone = b7.redo.execute(
        RedoAllocationRequest(
            financial_event_id=event_id, expected_version=event.version
        )
    )
    assert undone.event.status is FinancialEventStatus.NEEDS_ALLOCATION

    with pytest.raises(FinancialEventError, match="taken back"):
        b7.allocate.execute(
            AllocateFinancialEventRequest(
                financial_event_id=event_id,
                expected_version=undone.event.version,
                client_token="stale",
                lines=(
                    AllocationLine(
                        amount_minor=12_000,
                        funding_wallet_id=b7_world.cash_wallet_id,
                        budget_category_id=b7_world.food_id,
                    ),
                ),
            )
        )
    with b7.factory() as unit:
        after = unit.financial_events.get(event_id)
    assert after is not None
    assert after.status is FinancialEventStatus.NEEDS_ALLOCATION


def test_a_cancelled_expense_nets_to_zero_and_leaves_the_list(
    b7: B7Services, b7_world: B7World
) -> None:
    """The day nets to zero, and shows neither half of the cancellation.

    A reversal and the entry it cancels add up to nothing, so listing both
    only asks the reader to notice that they canceled out. The total is a
    separate query and still counts both, so the list cannot disagree with
    it. The event page is where the cancellation is still visible.
    """
    event_id = _cash(b7, b7_world, token="cash")
    with b7.factory() as unit:
        event = unit.financial_events.get(event_id)
        assert event is not None
        version = event.version

    b7.reversal.execute(
        ReverseEventRequest(financial_event_id=event_id, expected_version=version)
    )

    # The reversal is an exact cancellation at the original instant, so the day
    # nets to zero rather than showing spending that never happened.
    assert _total(b7, LOCAL_DAY, PaymentMethod.CASH) == 0
    rows = b7.spending.daily(day=LOCAL_DAY).rows
    assert [row for row in rows if row.financial_event_id == event_id] == []
    assert [row for row in rows if row.entry_type is JournalEntryType.REVERSAL] == []


def test_a_multi_category_split_contributes_exactly_its_parts(
    b7: B7Services, b7_world: B7World
) -> None:
    with b7.factory() as unit:
        event = unit.financial_events.add(
            FinancialEventDraft(
                origin=FinancialEventOrigin.MANUAL,
                event_type=FinancialEventType.PURCHASE,
                payment_method=PaymentMethod.CASH,
                recognition_basis=RecognitionBasis.OBSERVED_MANUAL,
                occurred_at=B7_NOW,
                amount_minor=30_000,
                currency="TWD",
                idempotency_key="split-event",
                merchant="超市",
                status=FinancialEventStatus.NEEDS_ALLOCATION,
            )
        )
        unit.commit()
        event_id, version = event.id, event.version

    b7.allocate.execute(
        AllocateFinancialEventRequest(
            financial_event_id=event_id,
            expected_version=version,
            client_token="split",
            lines=(
                AllocationLine(
                    amount_minor=20_000,
                    funding_wallet_id=b7_world.cash_wallet_id,
                    budget_category_id=b7_world.food_id,
                ),
                AllocationLine(
                    amount_minor=10_000,
                    funding_wallet_id=b7_world.cash_wallet_id,
                    budget_category_id=b7_world.transport_id,
                ),
            ),
        )
    )

    row = next(
        item
        for item in b7.spending.daily(day=LOCAL_DAY).rows
        if item.financial_event_id == event_id
    )

    assert row.amount_minor == 30_000
    assert sorted(share.amount_minor for share in row.categories) == [10_000, 20_000]
    assert _total(b7, LOCAL_DAY, PaymentMethod.CASH) == 30_000


def test_a_transfer_and_a_deposit_are_not_spending(
    b7: B7Services, b7_world: B7World
) -> None:
    _cash(b7, b7_world, token="cash")
    b7.movements.transfer(
        AccountTransferRequest(
            source_wallet_id=b7_world.bank_unallocated_id,
            destination_wallet_id=b7_world.bank_food_wallet_id,
            occurred_at=B7_NOW,
            amount_minor=100_000,
            currency="TWD",
            description="撥款",
            client_token="transfer",
        )
    )
    b7.movements.execute(
        BankMovementRequest(
            kind=BankMovementKind.EXTERNAL_DEPOSIT,
            occurred_at=B7_NOW,
            amount_minor=200_000,
            currency="TWD",
            wallet_id=b7_world.bank_unallocated_id,
            description="匯入",
            client_token="deposit",
        )
    )

    view = b7.spending.daily(day=LOCAL_DAY)

    assert view.total_rows == 1
    assert _total(b7, LOCAL_DAY, PaymentMethod.CASH) == 12_000
    assert _total(b7, LOCAL_DAY, PaymentMethod.CREDIT_CARD) == 0


def test_a_bank_paid_expense_is_not_on_the_pocket_page(
    b7: B7Services, b7_world: B7World
) -> None:
    b7.movements.execute(
        BankMovementRequest(
            kind=BankMovementKind.DIRECT_DEBIT,
            occurred_at=B7_NOW,
            amount_minor=88_000,
            currency="TWD",
            wallet_id=b7_world.bank_unallocated_id,
            description="電費",
            client_token="debit",
            budget_category_id=b7_world.transport_id,
        )
    )

    assert b7.spending.daily(day=LOCAL_DAY).total_rows == 0


def test_paging_is_bounded_and_clamped(b7: B7Services, b7_world: B7World) -> None:
    for index in range(5):
        _cash(b7, b7_world, token=f"row-{index}")

    page = b7.spending.daily(day=LOCAL_DAY, page=1, page_size=2)
    last = b7.spending.daily(day=LOCAL_DAY, page=99, page_size=2)

    assert page.page_count == 3
    assert len(page.rows) == 2
    assert page.has_more
    assert last.page == 3
    assert not last.has_more


def test_the_date_strip_ends_on_the_selected_day(
    b7: B7Services, b7_world: B7World
) -> None:
    view = b7.spending.daily(day=LOCAL_DAY)

    assert len(view.days) == 7
    assert view.days[-1] == LOCAL_DAY
    assert view.days[0] == LOCAL_DAY - timedelta(days=6)

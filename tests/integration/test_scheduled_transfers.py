"""B6 acceptance tests: clearing schedules, card autopay, and their lifecycle.

Every amount here is invented and every institution code is a test code. No
real bank, card, account, or balance appears anywhere in this module.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

import pytest
from app.application.queries.clearing import ClearingQueries
from app.application.services.cards import (
    ConfigureCardRequest,
    ManageCards,
    RegisterCardRequest,
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
from app.application.services.scheduled_transfers import (
    ScheduleCardAutopayRequest,
    ScheduleClearingTransferRequest,
    ScheduleCommitmentLine,
    ScheduledTransferAction,
    ScheduleTransfers,
)
from app.domain.budget_enums import (
    CardHolderRole,
    CommitmentFulfillmentMode,
    CommitmentStatus,
    FinancialAccountType,
    FinancialEventOrigin,
    FinancialEventType,
    JournalEntryType,
    PaymentMethod,
    RecognitionBasis,
    ScheduledTransferStatus,
    WalletPurpose,
)
from app.domain.budget_models import (
    BudgetCategoryDraft,
    FinancialEventDraft,
    JournalEntry,
    Money,
    OpeningWalletSplit,
    PostingDraft,
)
from app.domain.budget_rules import scheduled_transfer_execute_key
from app.domain.errors import (
    ConcurrentUpdateError,
    ScheduledTransferCoverageError,
    ScheduledTransferError,
    ScheduledTransferOverdueError,
    ScheduledTransferShapeError,
    ScheduledTransferStateError,
    WalletAssignmentError,
)
from app.domain.ports.repositories import UnitOfWork
from app.infrastructure.database.budget_repositories import (
    SqlAlchemyFundingCommitmentRepository,
)
from app.infrastructure.database.clearing_queries import (
    SqlAlchemyClearingReadRepository,
)
from app.infrastructure.database.engine import Database
from app.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork
from sqlalchemy import text

OCCURRED_AT = datetime(2026, 8, 7, 13, 30, tzinfo=UTC)
DUE_DATE = date(2026, 9, 1)
#: 2026-09-01 06:00 Asia/Taipei is 2026-08-31 22:00 UTC.
DUE_AT = datetime(2026, 8, 31, 22, 0, tzinfo=UTC)
BEFORE_DUE = datetime(2026, 8, 30, 3, 0, tzinfo=UTC)
#: A late catch-up: the Mac slept through 06:00 and woke two days later.
LATE = datetime(2026, 9, 2, 1, 15, tzinfo=UTC)

type UnitOfWorkFactory = Callable[[], UnitOfWork]


class Clock:
    """A movable clock, so 'before due' and 'late catch-up' are both testable."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@dataclass(frozen=True)
class World:
    """Two test banks, one test card, and the reservations they produced."""

    main_account_id: int
    main_unallocated_id: int
    clearing_hub_id: int
    main_budget_id: int
    other_account_id: int
    other_unallocated_id: int
    other_budget_id: int
    liability_account_id: int
    card_id: int
    reserve_only_commitment_id: int
    reserve_only_amount: int
    cross_bank_commitment_id: int
    cross_bank_amount: int
    second_cross_bank_commitment_id: int
    second_cross_bank_amount: int
    cross_bank_event_id: int
    second_cross_bank_event_id: int


@pytest.fixture
def factory(migrated_database: Database) -> UnitOfWorkFactory:
    """Return a unit-of-work factory bound to the migrated test database."""

    def build() -> UnitOfWork:
        return SqlAlchemyUnitOfWork(migrated_database.session_factory)

    return build


@pytest.fixture
def clearing(migrated_database: Database) -> ClearingQueries:
    """Return the clearing read model over the same database."""
    return ClearingQueries(
        SqlAlchemyClearingReadRepository(migrated_database.session_factory)
    )


@pytest.fixture
def clock() -> Clock:
    """Return a clock parked before the first due time."""
    return Clock(BEFORE_DUE)


@pytest.fixture
def schedules(factory: UnitOfWorkFactory, clock: Clock) -> ScheduleTransfers:
    """Return the clearing scheduler over an injected, movable clock."""
    return ScheduleTransfers(
        factory,
        JournalService(factory, clock=clock),
        clock=clock,
        default_local_time=time(6, 0),
        timezone="Asia/Taipei",
        lease_seconds=120,
        batch_size=20,
        retry_base_seconds=30,
        retry_max_seconds=1800,
    )


def _balance(factory: UnitOfWorkFactory, wallet_id: int) -> int:
    with factory() as unit:
        return unit.budget_ledger.wallet_balance(wallet_id)


def _reserved(factory: UnitOfWorkFactory, wallet_id: int) -> int:
    with factory() as unit:
        return unit.budget_ledger.reserved_minor(wallet_id)


def _commitment_status(
    factory: UnitOfWorkFactory, commitment_id: int
) -> CommitmentStatus:
    with factory() as unit:
        commitment = unit.commitments.get(commitment_id)
        assert commitment is not None
        return commitment.status


def _schedule_status(
    factory: UnitOfWorkFactory, schedule_id: int
) -> ScheduledTransferStatus:
    with factory() as unit:
        schedule = unit.scheduled_transfers.get(schedule_id)
        assert schedule is not None
        return schedule.status


def _entry_count(factory: UnitOfWorkFactory, entry_type: JournalEntryType) -> int:
    """Count posted entries of one type, reading the ledger directly."""
    with factory() as unit:
        return len(
            [entry for entry in _all_entries(unit) if entry.entry_type is entry_type]
        )


def _all_entries(unit: UnitOfWork) -> tuple[JournalEntry, ...]:
    """Return every posted entry, walking IDs until the ledger runs out."""
    entries: list[JournalEntry] = []
    entry_id = 1
    while True:
        entry = unit.journal.get(entry_id)
        if entry is None:
            return tuple(entries)
        entries.append(entry)
        entry_id += 1


def _event(
    factory: UnitOfWorkFactory,
    *,
    key: str,
    amount_minor: int,
    card_id: int | None,
    event_type: FinancialEventType = FinancialEventType.PURCHASE,
    refund_of_event_id: int | None = None,
) -> int:
    with factory() as unit:
        event = unit.financial_events.add(
            FinancialEventDraft(
                origin=FinancialEventOrigin.GMAIL_TRANSACTION,
                event_type=event_type,
                payment_method=PaymentMethod.CREDIT_CARD,
                recognition_basis=RecognitionBasis.GMAIL_NOTIFICATION,
                occurred_at=OCCURRED_AT,
                amount_minor=amount_minor,
                currency="TWD",
                idempotency_key=key,
                merchant="測試商店",
                card_id=card_id,
                refund_of_event_id=refund_of_event_id,
            )
        )
        unit.commit()
        return event.id


@pytest.fixture
def world(factory: UnitOfWorkFactory) -> World:
    """Build two test banks, one test card, and three card reservations."""
    setup = LedgerSetupService(factory)
    journal = JournalService(factory)
    allocate = AllocateFinancialEvent(factory, journal)
    cards = ManageCards(factory)

    main = setup.open_account(
        OpenFinancialAccountRequest(
            institution_code="test_bank_a",
            display_name="測試主要帳戶",
            account_type=FinancialAccountType.CHECKING,
            currency="TWD",
        )
    )
    clearing_hub = setup.add_wallet(
        AddWalletRequest(
            financial_account_id=main.account.id,
            name="測試帳單暫存",
            purpose=WalletPurpose.CLEARING_HUB,
        )
    )
    main_budget = setup.add_wallet(
        AddWalletRequest(financial_account_id=main.account.id, name="測試主要預算")
    )
    other = setup.open_account(
        OpenFinancialAccountRequest(
            institution_code="test_bank_b",
            display_name="測試次要帳戶",
            account_type=FinancialAccountType.CHECKING,
            currency="TWD",
        )
    )
    other_budget = setup.add_wallet(
        AddWalletRequest(financial_account_id=other.account.id, name="測試次要預算")
    )
    liability = setup.open_account(
        OpenFinancialAccountRequest(
            institution_code="test_bank_a",
            display_name="測試卡負債",
            account_type=FinancialAccountType.CARD_LIABILITY,
            currency="TWD",
        )
    )
    journal.record_opening_position(
        OpeningPositionRequest(
            financial_account_id=main.account.id,
            balance_minor=1_000_000,
            observed_at=OCCURRED_AT,
            splits=(
                OpeningWalletSplit(wallet_name="尚未分配", amount_minor=600_000),
                OpeningWalletSplit(wallet_name="測試帳單暫存", amount_minor=100_000),
                OpeningWalletSplit(wallet_name="測試主要預算", amount_minor=300_000),
            ),
        )
    )
    journal.record_opening_position(
        OpeningPositionRequest(
            financial_account_id=other.account.id,
            balance_minor=500_000,
            observed_at=OCCURRED_AT,
            splits=(
                OpeningWalletSplit(wallet_name="尚未分配", amount_minor=200_000),
                OpeningWalletSplit(wallet_name="測試次要預算", amount_minor=300_000),
            ),
        )
    )
    with factory() as unit:
        gmail_account = unit.accounts.get_or_create("user@example.test")
        category = unit.budget_categories.add(BudgetCategoryDraft(name="測試分類"))
        main_unallocated = unit.wallets.get_unallocated(main.account.id)
        other_unallocated = unit.wallets.get_unallocated(other.account.id)
        assert main_unallocated is not None
        assert other_unallocated is not None
        unit.commit()
        gmail_account_id = gmail_account.id
        category_id = category.id
        main_unallocated_id = main_unallocated.id
        other_unallocated_id = other_unallocated.id

    card = cards.register(
        RegisterCardRequest(
            gmail_account_id=gmail_account_id,
            issuer_code="test_bank_a",
            display_name="測試卡",
            last_four="4321",
            holder_role=CardHolderRole.PRIMARY,
        )
    )
    cards.configure(
        ConfigureCardRequest(
            card_id=card.id,
            expected_version=card.version,
            display_name="測試卡",
            holder_role=CardHolderRole.PRIMARY,
            liability_account_id=liability.account.id,
            settlement_account_id=main.account.id,
            default_funding_wallet_id=main_unallocated_id,
        )
    )

    same_bank_event = _event(
        factory, key="test:same-bank", amount_minor=12_000, card_id=card.id
    )
    same_bank = allocate.execute(
        AllocateFinancialEventRequest(
            financial_event_id=same_bank_event,
            expected_version=1,
            client_token="token-same-bank",
            lines=(
                AllocationLine(
                    amount_minor=12_000,
                    funding_wallet_id=main_budget.id,
                    budget_category_id=category_id,
                ),
            ),
        )
    )
    cross_event = _event(
        factory, key="test:cross-bank", amount_minor=8_000, card_id=card.id
    )
    cross_bank = allocate.execute(
        AllocateFinancialEventRequest(
            financial_event_id=cross_event,
            expected_version=1,
            client_token="token-cross-bank",
            lines=(
                AllocationLine(
                    amount_minor=8_000,
                    funding_wallet_id=other_budget.id,
                    budget_category_id=category_id,
                ),
            ),
        )
    )
    second_cross_event = _event(
        factory, key="test:cross-bank-2", amount_minor=5_000, card_id=card.id
    )
    second_cross = allocate.execute(
        AllocateFinancialEventRequest(
            financial_event_id=second_cross_event,
            expected_version=1,
            client_token="token-cross-bank-2",
            lines=(
                AllocationLine(
                    amount_minor=5_000,
                    funding_wallet_id=other_budget.id,
                    budget_category_id=category_id,
                ),
            ),
        )
    )
    assert (
        same_bank.commitments[0].fulfillment_mode
        is CommitmentFulfillmentMode.RESERVE_ONLY
    )
    assert (
        cross_bank.commitments[0].fulfillment_mode
        is CommitmentFulfillmentMode.CROSS_BANK_TRANSFER
    )
    return World(
        main_account_id=main.account.id,
        main_unallocated_id=main_unallocated_id,
        clearing_hub_id=clearing_hub.id,
        main_budget_id=main_budget.id,
        other_account_id=other.account.id,
        other_unallocated_id=other_unallocated_id,
        other_budget_id=other_budget.id,
        liability_account_id=liability.account.id,
        card_id=card.id,
        reserve_only_commitment_id=same_bank.commitments[0].id,
        reserve_only_amount=12_000,
        cross_bank_commitment_id=cross_bank.commitments[0].id,
        cross_bank_amount=8_000,
        second_cross_bank_commitment_id=second_cross.commitments[0].id,
        second_cross_bank_amount=5_000,
        cross_bank_event_id=cross_event,
        second_cross_bank_event_id=second_cross_event,
    )


def _clearing_request(
    world: World,
    *,
    amount_minor: int,
    token: str,
    lines: tuple[ScheduleCommitmentLine, ...],
    scheduled_date: date = DUE_DATE,
) -> ScheduleClearingTransferRequest:
    return ScheduleClearingTransferRequest(
        source_account_id=world.other_account_id,
        source_wallet_id=world.other_unallocated_id,
        destination_account_id=world.main_account_id,
        destination_wallet_id=world.clearing_hub_id,
        amount_minor=amount_minor,
        scheduled_local_date=scheduled_date,
        client_token=token,
        lines=lines,
    )


def _autopay_request(
    world: World,
    *,
    amount_minor: int,
    token: str,
    lines: tuple[ScheduleCommitmentLine, ...],
) -> ScheduleCardAutopayRequest:
    return ScheduleCardAutopayRequest(
        card_id=world.card_id,
        source_wallet_id=world.main_unallocated_id,
        amount_minor=amount_minor,
        scheduled_local_date=DUE_DATE,
        client_token=token,
        lines=lines,
    )


def _schedule_clearing(
    schedules: ScheduleTransfers, world: World, *, token: str = "clearing-1"
) -> int:
    result = schedules.schedule_clearing(
        _clearing_request(
            world,
            amount_minor=world.cross_bank_amount,
            token=token,
            lines=(
                ScheduleCommitmentLine(
                    funding_commitment_id=world.cross_bank_commitment_id,
                    amount_minor=world.cross_bank_amount,
                ),
            ),
        )
    )
    return result.schedule.id


# --- A. Before the due time -------------------------------------------------


def test_scheduling_changes_reservation_but_not_the_actual_balance(
    factory: UnitOfWorkFactory,
    schedules: ScheduleTransfers,
    world: World,
) -> None:
    before_source = _balance(factory, world.other_budget_id)
    before_destination = _balance(factory, world.clearing_hub_id)

    _schedule_clearing(schedules, world)

    assert _balance(factory, world.other_budget_id) == before_source
    assert _balance(factory, world.clearing_hub_id) == before_destination
    assert _entry_count(factory, JournalEntryType.FUNDING_TRANSFER) == 0


def test_a_commitment_and_the_schedule_that_funds_it_reserve_once(
    factory: UnitOfWorkFactory,
    schedules: ScheduleTransfers,
    world: World,
) -> None:
    before = _reserved(factory, world.other_budget_id)
    assert before == world.cross_bank_amount + world.second_cross_bank_amount

    _schedule_clearing(schedules, world)

    assert _reserved(factory, world.other_budget_id) == before
    assert (
        _commitment_status(factory, world.cross_bank_commitment_id)
        is CommitmentStatus.SCHEDULED
    )


def test_partial_scheduling_leaves_the_rest_reserved_by_the_commitment(
    factory: UnitOfWorkFactory,
    schedules: ScheduleTransfers,
    world: World,
) -> None:
    schedules.schedule_clearing(
        _clearing_request(
            world,
            amount_minor=3_000,
            token="partial",
            lines=(
                ScheduleCommitmentLine(
                    funding_commitment_id=world.cross_bank_commitment_id,
                    amount_minor=3_000,
                ),
            ),
        )
    )

    assert (
        _reserved(factory, world.other_budget_id)
        == world.cross_bank_amount + world.second_cross_bank_amount
    )
    assert (
        _commitment_status(factory, world.cross_bank_commitment_id)
        is CommitmentStatus.PARTIALLY_SCHEDULED
    )


def test_one_movement_can_cover_several_commitments(
    factory: UnitOfWorkFactory,
    schedules: ScheduleTransfers,
    world: World,
) -> None:
    total = world.cross_bank_amount + world.second_cross_bank_amount
    schedules.schedule_clearing(
        _clearing_request(
            world,
            amount_minor=total,
            token="grouped",
            lines=(
                ScheduleCommitmentLine(
                    funding_commitment_id=world.cross_bank_commitment_id,
                    amount_minor=world.cross_bank_amount,
                ),
                ScheduleCommitmentLine(
                    funding_commitment_id=world.second_cross_bank_commitment_id,
                    amount_minor=world.second_cross_bank_amount,
                ),
            ),
        )
    )

    assert _reserved(factory, world.other_budget_id) == total
    assert (
        _commitment_status(factory, world.second_cross_bank_commitment_id)
        is CommitmentStatus.SCHEDULED
    )


def test_a_clearing_transfer_must_cover_exactly_what_it_moves(
    schedules: ScheduleTransfers, world: World
) -> None:
    with pytest.raises(ScheduledTransferShapeError, match="exactly"):
        schedules.schedule_clearing(
            _clearing_request(
                world,
                amount_minor=world.cross_bank_amount + 1,
                token="unbalanced",
                lines=(
                    ScheduleCommitmentLine(
                        funding_commitment_id=world.cross_bank_commitment_id,
                        amount_minor=world.cross_bank_amount,
                    ),
                ),
            )
        )


def test_a_schedule_cannot_cover_more_than_a_commitment_requires(
    schedules: ScheduleTransfers, world: World
) -> None:
    with pytest.raises(ScheduledTransferCoverageError):
        schedules.schedule_clearing(
            _clearing_request(
                world,
                amount_minor=world.cross_bank_amount + 1_000,
                token="over",
                lines=(
                    ScheduleCommitmentLine(
                        funding_commitment_id=world.cross_bank_commitment_id,
                        amount_minor=world.cross_bank_amount + 1_000,
                    ),
                ),
            )
        )


def test_a_clearing_transfer_refuses_a_commitment_from_another_account(
    schedules: ScheduleTransfers, world: World
) -> None:
    """Money reserved in the settlement account itself never needs a transfer.

    Such a commitment is always `reserve_only`, so its wallet is in the very
    account the clearing transfer would arrive in, and selecting it into a
    movement that leaves another bank would debit a wallet that reserved
    nothing for it.
    """
    with pytest.raises(ScheduledTransferShapeError, match="account it leaves"):
        schedules.schedule_clearing(
            _clearing_request(
                world,
                amount_minor=world.reserve_only_amount,
                token="wrong-account",
                lines=(
                    ScheduleCommitmentLine(
                        funding_commitment_id=world.reserve_only_commitment_id,
                        amount_minor=world.reserve_only_amount,
                    ),
                ),
            )
        )


def test_an_already_due_schedule_needs_an_explicit_confirmation(
    schedules: ScheduleTransfers, world: World, clock: Clock
) -> None:
    clock.now = LATE
    with pytest.raises(ScheduledTransferOverdueError):
        _schedule_clearing(schedules, world, token="overdue")

    confirmed = schedules.schedule_clearing(
        ScheduleClearingTransferRequest(
            source_account_id=world.other_account_id,
            source_wallet_id=world.other_unallocated_id,
            destination_account_id=world.main_account_id,
            destination_wallet_id=world.clearing_hub_id,
            amount_minor=world.cross_bank_amount,
            scheduled_local_date=DUE_DATE,
            client_token="overdue-confirmed",
            lines=(
                ScheduleCommitmentLine(
                    funding_commitment_id=world.cross_bank_commitment_id,
                    amount_minor=world.cross_bank_amount,
                ),
            ),
            allow_overdue=True,
        )
    )
    assert confirmed.created is True


def test_a_float_amount_is_refused_at_the_domain_boundary(
    schedules: ScheduleTransfers, world: World
) -> None:
    for amount in (True, 8_000.5, "8000"):
        with pytest.raises(TypeError):
            schedules.schedule_clearing(
                _clearing_request(
                    world,
                    amount_minor=amount,  # type: ignore[arg-type]
                    token=f"bad-{amount}",
                    lines=(),
                )
            )


# --- B. Execution -----------------------------------------------------------


def test_a_due_clearing_transfer_posts_exactly_one_balanced_entry(
    factory: UnitOfWorkFactory,
    schedules: ScheduleTransfers,
    world: World,
    clock: Clock,
) -> None:
    schedule_id = _schedule_clearing(schedules, world)
    source_before = _balance(factory, world.other_budget_id)
    destination_before = _balance(factory, world.clearing_hub_id)

    clock.now = DUE_AT + timedelta(minutes=1)
    first = schedules.run_due()
    second = schedules.run_due()

    assert (first.claimed, first.executed) == (1, 1)
    assert second.claimed == 0
    assert _entry_count(factory, JournalEntryType.FUNDING_TRANSFER) == 1
    assert (
        _balance(factory, world.other_budget_id)
        == source_before - world.cross_bank_amount
    )
    assert (
        _balance(factory, world.clearing_hub_id)
        == destination_before + world.cross_bank_amount
    )
    assert (
        _schedule_status(factory, schedule_id)
        is ScheduledTransferStatus.ASSUMED_EXECUTED
    )
    assert (
        _commitment_status(factory, world.cross_bank_commitment_id)
        is CommitmentStatus.FUNDED
    )
    assert _reserved(factory, world.other_budget_id) == world.second_cross_bank_amount


def test_a_clearing_transfer_never_touches_the_card_liability(
    factory: UnitOfWorkFactory,
    schedules: ScheduleTransfers,
    world: World,
    clock: Clock,
) -> None:
    _schedule_clearing(schedules, world)
    clock.now = DUE_AT + timedelta(minutes=1)
    schedules.run_due()

    with factory() as unit:
        ledger = unit.ledger_accounts.get_for_financial_account(
            world.liability_account_id
        )
        assert ledger is not None
        entry = unit.journal.get_by_idempotency_key("scheduled_transfer:execute:1")
        assert entry is not None
        assert all(posting.ledger_account_id != ledger.id for posting in entry.postings)
        assert entry.entry_type is JournalEntryType.FUNDING_TRANSFER


def test_a_card_autopay_moves_assets_into_the_card_liability(
    factory: UnitOfWorkFactory,
    schedules: ScheduleTransfers,
    world: World,
    clock: Clock,
) -> None:
    result = schedules.schedule_card_autopay(
        _autopay_request(
            world,
            amount_minor=world.reserve_only_amount,
            token="autopay-1",
            lines=(
                ScheduleCommitmentLine(
                    funding_commitment_id=world.reserve_only_commitment_id,
                    amount_minor=world.reserve_only_amount,
                ),
            ),
        )
    )
    budget_before = _balance(factory, world.main_budget_id)

    clock.now = DUE_AT + timedelta(minutes=1)
    run = schedules.run_due()

    assert run.executed == 1
    assert _entry_count(factory, JournalEntryType.CARD_PAYMENT) == 1
    assert (
        _balance(factory, world.main_budget_id)
        == budget_before - world.reserve_only_amount
    )
    with factory() as unit:
        liability = unit.ledger_accounts.get_for_financial_account(
            world.liability_account_id
        )
        assert liability is not None
        entry = unit.journal.get_by_idempotency_key(
            f"scheduled_transfer:execute:{result.schedule.id}"
        )
        assert entry is not None
        credit = [
            posting
            for posting in entry.postings
            if posting.ledger_account_id == liability.id
        ]
        assert len(credit) == 1
        assert credit[0].amount_minor == world.reserve_only_amount
        assert credit[0].wallet_id is None
    assert (
        _commitment_status(factory, world.reserve_only_commitment_id)
        is CommitmentStatus.FUNDED
    )


def test_an_autopay_debits_every_wallet_that_reserved_a_share(
    factory: UnitOfWorkFactory,
    schedules: ScheduleTransfers,
    world: World,
    clock: Clock,
    allocate_second_same_bank: tuple[int, int],
) -> None:
    """One card is paid out of two budget wallets plus an unlinked residual."""
    second_commitment_id, second_wallet_id = allocate_second_same_bank
    covered = world.reserve_only_amount + 4_000
    result = schedules.schedule_card_autopay(
        _autopay_request(
            world,
            amount_minor=covered + 1_000,
            token="autopay-multi",
            lines=(
                ScheduleCommitmentLine(
                    funding_commitment_id=world.reserve_only_commitment_id,
                    amount_minor=world.reserve_only_amount,
                ),
                ScheduleCommitmentLine(
                    funding_commitment_id=second_commitment_id,
                    amount_minor=4_000,
                ),
            ),
        )
    )
    budget_before = _balance(factory, world.main_budget_id)
    unallocated_before = _balance(factory, world.main_unallocated_id)

    clock.now = DUE_AT + timedelta(minutes=1)
    schedules.run_due()

    with factory() as unit:
        entry = unit.journal.get_by_idempotency_key(
            f"scheduled_transfer:execute:{result.schedule.id}"
        )
        assert entry is not None
        debits = {
            posting.wallet_id: posting.amount_minor
            for posting in entry.postings
            if posting.wallet_id is not None
        }
    # Each covered share leaves the wallet that reserved it; only the
    # uncovered residual comes out of the schedule's own source wallet.
    assert debits[world.main_budget_id] == -world.reserve_only_amount
    assert debits[second_wallet_id] == -4_000
    assert debits[world.main_unallocated_id] == -1_000
    assert len(debits) == 3
    assert (
        _balance(factory, world.main_budget_id)
        == budget_before - world.reserve_only_amount
    )
    assert _balance(factory, world.main_unallocated_id) == unallocated_before - 1_000


@pytest.fixture
def allocate_second_same_bank(
    factory: UnitOfWorkFactory, world: World
) -> tuple[int, int]:
    """Add a second same-bank card purchase funded from another wallet."""
    setup = LedgerSetupService(factory)
    second_wallet = setup.add_wallet(
        AddWalletRequest(
            financial_account_id=world.main_account_id, name="測試第二預算"
        )
    )
    JournalService(factory).post_balanced(
        entry_type=JournalEntryType.INTERNAL_TRANSFER,
        effective_at=OCCURRED_AT,
        idempotency_key="test:seed-second-wallet",
        description="測試錢包分配",
        postings=_reclassification(factory, world, second_wallet.id, 50_000),
    )
    with factory() as unit:
        category = unit.budget_categories.list_all()[0]
        category_id = category.id
    event_id = _event(
        factory, key="test:same-bank-2", amount_minor=4_000, card_id=world.card_id
    )
    result = AllocateFinancialEvent(factory, JournalService(factory)).execute(
        AllocateFinancialEventRequest(
            financial_event_id=event_id,
            expected_version=1,
            client_token="token-same-bank-2",
            lines=(
                AllocationLine(
                    amount_minor=4_000,
                    funding_wallet_id=second_wallet.id,
                    budget_category_id=category_id,
                ),
            ),
        )
    )
    return result.commitments[0].id, second_wallet.id


def _reclassification(
    factory: UnitOfWorkFactory, world: World, wallet_id: int, amount_minor: int
) -> tuple[PostingDraft, ...]:
    """Move money between two wallets of the same account, for a fixture."""
    with factory() as unit:
        ledger = unit.ledger_accounts.get_for_financial_account(world.main_account_id)
        assert ledger is not None
        ledger_id = ledger.id
    return (
        PostingDraft(
            ledger_account_id=ledger_id,
            amount=Money(-amount_minor, "TWD"),
            wallet_id=world.main_unallocated_id,
        ),
        PostingDraft(
            ledger_account_id=ledger_id,
            amount=Money(amount_minor, "TWD"),
            wallet_id=wallet_id,
        ),
    )


def test_an_autopay_refuses_a_commitment_from_another_card(
    factory: UnitOfWorkFactory, schedules: ScheduleTransfers, world: World
) -> None:
    cards = ManageCards(factory)
    with factory() as unit:
        gmail_account_id = unit.accounts.get_or_create("user@example.test").id
    other_card = cards.register(
        RegisterCardRequest(
            gmail_account_id=gmail_account_id,
            issuer_code="test_bank_a",
            display_name="測試第二卡",
            last_four="8765",
            holder_role=CardHolderRole.PRIMARY,
        )
    )
    cards.configure(
        ConfigureCardRequest(
            card_id=other_card.id,
            expected_version=other_card.version,
            display_name="測試第二卡",
            holder_role=CardHolderRole.PRIMARY,
            liability_account_id=world.liability_account_id,
            settlement_account_id=world.main_account_id,
            default_funding_wallet_id=world.main_unallocated_id,
        )
    )

    with pytest.raises(ScheduledTransferShapeError, match="made on that card"):
        schedules.schedule_card_autopay(
            ScheduleCardAutopayRequest(
                card_id=other_card.id,
                source_wallet_id=world.main_unallocated_id,
                amount_minor=world.reserve_only_amount,
                scheduled_local_date=DUE_DATE,
                client_token="autopay-wrong-card",
                lines=(
                    ScheduleCommitmentLine(
                        funding_commitment_id=world.reserve_only_commitment_id,
                        amount_minor=world.reserve_only_amount,
                    ),
                ),
            )
        )


def test_a_schedule_is_not_executed_before_it_is_due(
    factory: UnitOfWorkFactory,
    schedules: ScheduleTransfers,
    world: World,
    clock: Clock,
) -> None:
    schedule_id = _schedule_clearing(schedules, world)

    clock.now = DUE_AT - timedelta(minutes=1)
    result = schedules.run_due()

    assert (result.claimed, result.executed) == (0, 0)
    assert _schedule_status(factory, schedule_id) is ScheduledTransferStatus.SCHEDULED
    assert _entry_count(factory, JournalEntryType.FUNDING_TRANSFER) == 0


def test_a_late_catch_up_keeps_the_due_time_and_records_the_delay(
    factory: UnitOfWorkFactory,
    schedules: ScheduleTransfers,
    world: World,
    clock: Clock,
) -> None:
    schedule_id = _schedule_clearing(schedules, world)

    clock.now = LATE
    schedules.run_due()

    with factory() as unit:
        schedule = unit.scheduled_transfers.get(schedule_id)
        assert schedule is not None
        entry = unit.journal.get(schedule.executed_entry_id or 0)
        assert entry is not None
    assert entry.effective_at == DUE_AT
    assert schedule.processed_at == LATE
    assert schedule.due_at_utc == DUE_AT


# --- C. Claiming, crashes, and concurrency ----------------------------------


def test_two_coordinators_cannot_claim_the_same_schedule(
    factory: UnitOfWorkFactory,
    schedules: ScheduleTransfers,
    world: World,
    clock: Clock,
) -> None:
    _schedule_clearing(schedules, world)
    clock.now = DUE_AT + timedelta(minutes=1)
    lease_until = clock.now + timedelta(seconds=120)

    with factory() as unit:
        first = unit.scheduled_transfers.claim_due(
            now=clock.now, owner="owner-a", lease_until=lease_until, limit=10
        )
        unit.commit()
    with factory() as unit:
        second = unit.scheduled_transfers.claim_due(
            now=clock.now, owner="owner-b", lease_until=lease_until, limit=10
        )
        unit.commit()

    assert len(first) == 1
    assert second == ()


def test_an_expired_lease_is_recovered_by_the_next_pass(
    factory: UnitOfWorkFactory,
    schedules: ScheduleTransfers,
    world: World,
    clock: Clock,
) -> None:
    schedule_id = _schedule_clearing(schedules, world)
    clock.now = DUE_AT + timedelta(minutes=1)
    with factory() as unit:
        claimed = unit.scheduled_transfers.claim_due(
            now=clock.now,
            owner="owner-that-died",
            lease_until=clock.now + timedelta(seconds=1),
            limit=10,
        )
        unit.commit()
    assert len(claimed) == 1

    clock.now = clock.now + timedelta(seconds=30)
    result = schedules.run_due()

    assert (result.claimed, result.executed) == (1, 1)
    assert (
        _schedule_status(factory, schedule_id)
        is ScheduledTransferStatus.ASSUMED_EXECUTED
    )
    assert _entry_count(factory, JournalEntryType.FUNDING_TRANSFER) == 1


def test_a_stale_worker_cannot_overwrite_the_worker_that_took_over(
    factory: UnitOfWorkFactory,
    schedules: ScheduleTransfers,
    world: World,
    clock: Clock,
) -> None:
    schedule_id = _schedule_clearing(schedules, world)
    clock.now = DUE_AT + timedelta(minutes=1)
    with factory() as unit:
        stale = unit.scheduled_transfers.claim_due(
            now=clock.now,
            owner="owner-stale",
            lease_until=clock.now + timedelta(seconds=1),
            limit=10,
        )
        unit.commit()
    clock.now = clock.now + timedelta(seconds=30)
    schedules.run_due()

    with factory() as unit, pytest.raises(ConcurrentUpdateError):
        unit.scheduled_transfers.finish(
            schedule_id,
            expected_version=stale[0].version,
            lease_owner="owner-stale",
            status=ScheduledTransferStatus.FAILED,
            last_error_code="ScheduledTransferError",
            last_error_message="測試訊息",
        )
    assert (
        _schedule_status(factory, schedule_id)
        is ScheduledTransferStatus.ASSUMED_EXECUTED
    )


def test_a_crash_before_commit_never_posts_the_movement_twice(
    factory: UnitOfWorkFactory,
    schedules: ScheduleTransfers,
    world: World,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schedule_id = _schedule_clearing(schedules, world)
    clock.now = DUE_AT + timedelta(minutes=1)
    original = SqlAlchemyFundingCommitmentRepository.refresh_status
    calls = {"count": 0}

    def flaky(self: object, commitment_id: int):  # type: ignore[no-untyped-def]
        calls["count"] += 1
        if calls["count"] == 1:
            raise ConcurrentUpdateError("database was busy")
        return original(self, commitment_id)  # type: ignore[arg-type]

    monkeypatch.setattr(SqlAlchemyFundingCommitmentRepository, "refresh_status", flaky)
    crashed = schedules.run_due()
    assert crashed.retried == 1
    assert _entry_count(factory, JournalEntryType.FUNDING_TRANSFER) == 0

    monkeypatch.setattr(
        SqlAlchemyFundingCommitmentRepository, "refresh_status", original
    )
    clock.now = clock.now + timedelta(hours=1)
    recovered = schedules.run_due()

    assert recovered.executed == 1
    assert _entry_count(factory, JournalEntryType.FUNDING_TRANSFER) == 1
    assert (
        _schedule_status(factory, schedule_id)
        is ScheduledTransferStatus.ASSUMED_EXECUTED
    )


def test_the_execution_key_makes_a_second_posting_a_replay(
    factory: UnitOfWorkFactory,
    schedules: ScheduleTransfers,
    world: World,
    clock: Clock,
) -> None:
    """The stable key, not luck, is what stops a retry posting twice."""
    schedule_id = _schedule_clearing(schedules, world)
    clock.now = DUE_AT + timedelta(minutes=1)
    schedules.run_due()

    key = scheduled_transfer_execute_key(schedule_id)
    with factory() as unit:
        posted = unit.journal.get_by_idempotency_key(key)
        assert posted is not None
        replay = JournalService(factory, clock=clock).stage_balanced(
            unit,
            entry_type=JournalEntryType.FUNDING_TRANSFER,
            effective_at=posted.effective_at,
            idempotency_key=key,
            description="Clearing funding transfer (assumed at due time)",
            postings=tuple(
                PostingDraft(
                    ledger_account_id=item.ledger_account_id,
                    amount=Money(item.amount_minor, item.currency),
                    wallet_id=item.wallet_id,
                    budget_category_id=item.budget_category_id,
                )
                for item in posted.postings
            ),
        )
        assert replay.id == posted.id

    assert _entry_count(factory, JournalEntryType.FUNDING_TRANSFER) == 1


def test_a_clearing_transfer_cannot_target_a_card_liability_account(
    schedules: ScheduleTransfers, world: World
) -> None:
    """A card is paid by an autopay, never by a clearing funding transfer."""
    with pytest.raises(WalletAssignmentError):
        schedules.schedule_clearing(
            ScheduleClearingTransferRequest(
                source_account_id=world.other_account_id,
                source_wallet_id=world.other_unallocated_id,
                destination_account_id=world.liability_account_id,
                destination_wallet_id=world.clearing_hub_id,
                amount_minor=world.cross_bank_amount,
                scheduled_local_date=DUE_DATE,
                client_token="clearing-to-card",
                lines=(
                    ScheduleCommitmentLine(
                        funding_commitment_id=world.cross_bank_commitment_id,
                        amount_minor=world.cross_bank_amount,
                    ),
                ),
            )
        )


def test_a_failed_autopay_still_holds_back_its_unlinked_residual(
    factory: UnitOfWorkFactory,
    schedules: ScheduleTransfers,
    world: World,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The residual is reserved by the schedule alone, and failure keeps it."""
    assert _reserved(factory, world.main_unallocated_id) == 0
    schedules.schedule_card_autopay(
        _autopay_request(
            world,
            amount_minor=world.reserve_only_amount + 3_000,
            token="autopay-residual",
            lines=(
                ScheduleCommitmentLine(
                    funding_commitment_id=world.reserve_only_commitment_id,
                    amount_minor=world.reserve_only_amount,
                ),
            ),
        )
    )
    assert _reserved(factory, world.main_unallocated_id) == 3_000

    clock.now = DUE_AT + timedelta(minutes=1)

    def broken(self: object, commitment_id: int) -> None:
        raise ScheduledTransferShapeError("測試用的不一致")

    monkeypatch.setattr(SqlAlchemyFundingCommitmentRepository, "refresh_status", broken)
    result = schedules.run_due()

    assert result.failed == 1
    assert _reserved(factory, world.main_unallocated_id) == 3_000
    assert _reserved(factory, world.main_budget_id) == world.reserve_only_amount


def test_a_retryable_failure_waits_for_a_growing_backoff(
    factory: UnitOfWorkFactory,
    schedules: ScheduleTransfers,
    world: World,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schedule_id = _schedule_clearing(schedules, world)
    clock.now = DUE_AT + timedelta(minutes=1)

    def always_busy(self: object, commitment_id: int) -> None:
        raise ConcurrentUpdateError("database was busy")

    monkeypatch.setattr(
        SqlAlchemyFundingCommitmentRepository, "refresh_status", always_busy
    )
    schedules.run_due()
    with factory() as unit:
        first = unit.scheduled_transfers.get(schedule_id)
    assert first is not None
    assert first.status is ScheduledTransferStatus.SCHEDULED
    assert first.next_attempt_at == clock.now + timedelta(seconds=30)

    blocked = schedules.run_due()
    assert blocked.claimed == 0

    clock.now = first.next_attempt_at
    schedules.run_due()
    with factory() as unit:
        second = unit.scheduled_transfers.get(schedule_id)
    assert second is not None
    assert second.next_attempt_at == clock.now + timedelta(seconds=60)


def test_one_failure_does_not_stop_the_rest_of_the_batch(
    factory: UnitOfWorkFactory,
    migrated_database: Database,
    schedules: ScheduleTransfers,
    world: World,
    clock: Clock,
) -> None:
    broken = schedules.schedule_clearing(
        ScheduleClearingTransferRequest(
            source_account_id=world.other_account_id,
            source_wallet_id=world.other_unallocated_id,
            destination_account_id=world.main_account_id,
            # A wallet this test deactivates below, so only this schedule breaks.
            destination_wallet_id=world.main_budget_id,
            amount_minor=world.second_cross_bank_amount,
            scheduled_local_date=DUE_DATE,
            client_token="broken",
            lines=(
                ScheduleCommitmentLine(
                    funding_commitment_id=world.second_cross_bank_commitment_id,
                    amount_minor=world.second_cross_bank_amount,
                ),
            ),
        )
    ).schedule.id
    healthy = _schedule_clearing(schedules, world, token="healthy")
    _deactivate_wallet(migrated_database, world.main_budget_id)

    clock.now = DUE_AT + timedelta(minutes=1)
    result = schedules.run_due()

    assert result.claimed == 2
    assert result.executed == 1
    assert result.failed == 1
    assert _schedule_status(factory, healthy) is (
        ScheduledTransferStatus.ASSUMED_EXECUTED
    )
    assert _schedule_status(factory, broken) is ScheduledTransferStatus.FAILED


def _deactivate_wallet(migrated_database: Database, wallet_id: int) -> None:
    """Simulate a wallet that was retired after a movement was scheduled."""
    with migrated_database.engine.begin() as connection:
        connection.execute(
            text("UPDATE wallets SET is_active = 0 WHERE id = :id"),
            {"id": wallet_id},
        )


# --- D. Failure, cancellation, retry, and reversal --------------------------


def test_a_failed_schedule_still_holds_its_money_back(
    factory: UnitOfWorkFactory,
    schedules: ScheduleTransfers,
    world: World,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schedule_id = _schedule_clearing(schedules, world)
    before = _reserved(factory, world.other_budget_id)
    clock.now = DUE_AT + timedelta(minutes=1)

    def broken(self: object, commitment_id: int) -> None:
        raise ScheduledTransferShapeError("測試用的不一致")

    monkeypatch.setattr(SqlAlchemyFundingCommitmentRepository, "refresh_status", broken)
    result = schedules.run_due()

    assert result.failed == 1
    assert _schedule_status(factory, schedule_id) is ScheduledTransferStatus.FAILED
    assert _reserved(factory, world.other_budget_id) == before
    assert (
        _commitment_status(factory, world.cross_bank_commitment_id)
        is CommitmentStatus.SCHEDULED
    )


def test_a_failed_schedule_can_be_retried_then_executes(
    factory: UnitOfWorkFactory,
    schedules: ScheduleTransfers,
    world: World,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schedule_id = _schedule_clearing(schedules, world)
    clock.now = DUE_AT + timedelta(minutes=1)
    original = SqlAlchemyFundingCommitmentRepository.refresh_status

    def broken(self: object, commitment_id: int) -> None:
        raise ScheduledTransferShapeError("測試用的不一致")

    monkeypatch.setattr(SqlAlchemyFundingCommitmentRepository, "refresh_status", broken)
    schedules.run_due()
    monkeypatch.setattr(
        SqlAlchemyFundingCommitmentRepository, "refresh_status", original
    )

    with factory() as unit:
        failed = unit.scheduled_transfers.get(schedule_id)
    assert failed is not None
    schedules.retry(
        ScheduledTransferAction(
            scheduled_transfer_id=schedule_id, expected_version=failed.version
        )
    )
    result = schedules.run_due()

    assert result.executed == 1
    assert (
        _schedule_status(factory, schedule_id)
        is ScheduledTransferStatus.ASSUMED_EXECUTED
    )


def test_a_processing_schedule_cannot_be_cancelled(
    factory: UnitOfWorkFactory,
    schedules: ScheduleTransfers,
    world: World,
    clock: Clock,
) -> None:
    schedule_id = _schedule_clearing(schedules, world)
    clock.now = DUE_AT + timedelta(minutes=1)
    with factory() as unit:
        claimed = unit.scheduled_transfers.claim_due(
            now=clock.now,
            owner="owner-a",
            lease_until=clock.now + timedelta(seconds=120),
            limit=10,
        )
        unit.commit()

    with pytest.raises(ScheduledTransferStateError):
        schedules.cancel(
            ScheduledTransferAction(
                scheduled_transfer_id=schedule_id,
                expected_version=claimed[0].version,
            )
        )


def test_cancelling_releases_the_reservation_and_refreshes_the_commitment(
    factory: UnitOfWorkFactory,
    schedules: ScheduleTransfers,
    world: World,
) -> None:
    schedule_id = _schedule_clearing(schedules, world)
    with factory() as unit:
        schedule = unit.scheduled_transfers.get(schedule_id)
    assert schedule is not None

    schedules.cancel(
        ScheduledTransferAction(
            scheduled_transfer_id=schedule_id, expected_version=schedule.version
        )
    )

    assert _schedule_status(factory, schedule_id) is ScheduledTransferStatus.CANCELLED
    assert (
        _commitment_status(factory, world.cross_bank_commitment_id)
        is CommitmentStatus.OPEN
    )
    assert (
        _reserved(factory, world.other_budget_id)
        == world.cross_bank_amount + world.second_cross_bank_amount
    )


def test_a_cancelled_schedule_frees_its_coverage_capacity(
    factory: UnitOfWorkFactory,
    schedules: ScheduleTransfers,
    world: World,
) -> None:
    schedule_id = _schedule_clearing(schedules, world)
    with factory() as unit:
        schedule = unit.scheduled_transfers.get(schedule_id)
    assert schedule is not None
    schedules.cancel(
        ScheduledTransferAction(
            scheduled_transfer_id=schedule_id, expected_version=schedule.version
        )
    )

    replacement = _schedule_clearing(schedules, world, token="replacement")

    assert _schedule_status(factory, replacement) is ScheduledTransferStatus.SCHEDULED
    assert (
        _commitment_status(factory, world.cross_bank_commitment_id)
        is CommitmentStatus.SCHEDULED
    )


def test_reversing_an_executed_movement_restores_balance_and_commitment(
    factory: UnitOfWorkFactory,
    schedules: ScheduleTransfers,
    world: World,
    clock: Clock,
) -> None:
    schedule_id = _schedule_clearing(schedules, world)
    source_before = _balance(factory, world.other_budget_id)
    destination_before = _balance(factory, world.clearing_hub_id)
    clock.now = DUE_AT + timedelta(minutes=1)
    schedules.run_due()
    with factory() as unit:
        executed = unit.scheduled_transfers.get(schedule_id)
    assert executed is not None

    schedules.reverse(
        ScheduledTransferAction(
            scheduled_transfer_id=schedule_id, expected_version=executed.version
        )
    )

    assert _schedule_status(factory, schedule_id) is ScheduledTransferStatus.REVERSED
    assert _balance(factory, world.other_budget_id) == source_before
    assert _balance(factory, world.clearing_hub_id) == destination_before
    assert (
        _commitment_status(factory, world.cross_bank_commitment_id)
        is CommitmentStatus.OPEN
    )
    assert (
        _reserved(factory, world.other_budget_id)
        == world.cross_bank_amount + world.second_cross_bank_amount
    )


def test_a_reversed_schedule_frees_its_coverage_capacity(
    factory: UnitOfWorkFactory,
    schedules: ScheduleTransfers,
    world: World,
    clock: Clock,
) -> None:
    schedule_id = _schedule_clearing(schedules, world)
    clock.now = DUE_AT + timedelta(minutes=1)
    schedules.run_due()
    with factory() as unit:
        executed = unit.scheduled_transfers.get(schedule_id)
    assert executed is not None
    schedules.reverse(
        ScheduledTransferAction(
            scheduled_transfer_id=schedule_id, expected_version=executed.version
        )
    )

    clock.now = BEFORE_DUE
    replacement = schedules.schedule_clearing(
        _clearing_request(
            world,
            amount_minor=world.cross_bank_amount,
            token="after-reversal",
            lines=(
                ScheduleCommitmentLine(
                    funding_commitment_id=world.cross_bank_commitment_id,
                    amount_minor=world.cross_bank_amount,
                ),
            ),
            scheduled_date=date(2026, 9, 5),
        )
    )
    assert replacement.created is True


def test_resending_an_identical_reversal_replays_it(
    factory: UnitOfWorkFactory,
    schedules: ScheduleTransfers,
    world: World,
    clock: Clock,
) -> None:
    """A lost response must not turn a succeeded reversal into a failure.

    The second request carries the version the form was rendered with, which
    the first reversal has since moved on from. The stable reverse key is what
    identifies the work, so the replay is served instead of the state machine
    refusing `reversed -> reversed`.
    """
    schedule_id = _schedule_clearing(schedules, world)
    clock.now = DUE_AT + timedelta(minutes=1)
    schedules.run_due()
    with factory() as unit:
        executed = unit.scheduled_transfers.get(schedule_id)
    assert executed is not None

    first = schedules.reverse(
        ScheduledTransferAction(
            scheduled_transfer_id=schedule_id, expected_version=executed.version
        )
    )
    second = schedules.reverse(
        ScheduledTransferAction(
            scheduled_transfer_id=schedule_id, expected_version=executed.version
        )
    )

    assert first.created is True
    assert second.created is False
    assert first.entry is not None
    assert second.entry is not None
    assert second.entry.id == first.entry.id
    assert second.schedule.status is ScheduledTransferStatus.REVERSED
    assert second.schedule.version == first.schedule.version
    assert _entry_count(factory, JournalEntryType.REVERSAL) == 1


def test_a_replayed_reversal_verifies_the_entry_it_replays(
    factory: UnitOfWorkFactory,
    schedules: ScheduleTransfers,
    world: World,
    clock: Clock,
    migrated_database: Database,
) -> None:
    """A replay proves the stored reversal really is the one this key owns."""
    schedule_id = _schedule_clearing(schedules, world)
    clock.now = DUE_AT + timedelta(minutes=1)
    schedules.run_due()
    with factory() as unit:
        executed = unit.scheduled_transfers.get(schedule_id)
    assert executed is not None
    schedules.reverse(
        ScheduledTransferAction(
            scheduled_transfer_id=schedule_id, expected_version=executed.version
        )
    )
    with migrated_database.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE journal_entries SET idempotency_key = 'tampered' "
                "WHERE idempotency_key = :key"
            ),
            {"key": f"scheduled_transfer:reverse:{schedule_id}"},
        )

    with pytest.raises(ScheduledTransferError, match="does not match"):
        schedules.reverse(
            ScheduledTransferAction(
                scheduled_transfer_id=schedule_id,
                expected_version=executed.version,
            )
        )


def test_a_cancelled_schedule_is_still_refused_by_the_state_machine(
    factory: UnitOfWorkFactory, schedules: ScheduleTransfers, world: World
) -> None:
    """Only `reversed` replays; every other terminal state still refuses."""
    schedule_id = _schedule_clearing(schedules, world)
    with factory() as unit:
        schedule = unit.scheduled_transfers.get(schedule_id)
    assert schedule is not None
    schedules.cancel(
        ScheduledTransferAction(
            scheduled_transfer_id=schedule_id, expected_version=schedule.version
        )
    )

    with pytest.raises(ScheduledTransferStateError):
        schedules.reverse(
            ScheduledTransferAction(
                scheduled_transfer_id=schedule_id, expected_version=schedule.version
            )
        )


def test_an_executed_schedule_can_only_be_reversed(
    factory: UnitOfWorkFactory,
    schedules: ScheduleTransfers,
    world: World,
    clock: Clock,
) -> None:
    schedule_id = _schedule_clearing(schedules, world)
    clock.now = DUE_AT + timedelta(minutes=1)
    schedules.run_due()
    with factory() as unit:
        executed = unit.scheduled_transfers.get(schedule_id)
    assert executed is not None

    with pytest.raises(ScheduledTransferStateError):
        schedules.cancel(
            ScheduledTransferAction(
                scheduled_transfer_id=schedule_id,
                expected_version=executed.version,
            )
        )


# --- E. Idempotency ---------------------------------------------------------


def test_an_identical_submission_replays_the_first_schedule(
    factory: UnitOfWorkFactory, schedules: ScheduleTransfers, world: World
) -> None:
    first = schedules.schedule_clearing(
        _clearing_request(
            world,
            amount_minor=world.cross_bank_amount,
            token="replay",
            lines=(
                ScheduleCommitmentLine(
                    funding_commitment_id=world.cross_bank_commitment_id,
                    amount_minor=world.cross_bank_amount,
                ),
            ),
        )
    )
    second = schedules.schedule_clearing(
        _clearing_request(
            world,
            amount_minor=world.cross_bank_amount,
            token="replay",
            lines=(
                ScheduleCommitmentLine(
                    funding_commitment_id=world.cross_bank_commitment_id,
                    amount_minor=world.cross_bank_amount,
                ),
            ),
        )
    )

    assert (first.created, second.created) == (True, False)
    assert first.schedule.id == second.schedule.id
    with factory() as unit:
        assert (
            unit.scheduled_transfers.count_by_status(
                frozenset({ScheduledTransferStatus.SCHEDULED})
            )
            == 1
        )


def test_the_same_token_with_a_different_payload_is_a_conflict(
    schedules: ScheduleTransfers, world: World
) -> None:
    schedules.schedule_clearing(
        _clearing_request(
            world,
            amount_minor=world.cross_bank_amount,
            token="conflict",
            lines=(
                ScheduleCommitmentLine(
                    funding_commitment_id=world.cross_bank_commitment_id,
                    amount_minor=world.cross_bank_amount,
                ),
            ),
        )
    )

    with pytest.raises(ScheduledTransferError, match="different schedule"):
        schedules.schedule_clearing(
            _clearing_request(
                world,
                amount_minor=3_000,
                token="conflict",
                lines=(
                    ScheduleCommitmentLine(
                        funding_commitment_id=world.cross_bank_commitment_id,
                        amount_minor=3_000,
                    ),
                ),
            )
        )


# --- F. Refunds after scheduling --------------------------------------------


def test_a_refund_after_scheduling_never_rewrites_the_bank_instruction(
    factory: UnitOfWorkFactory,
    schedules: ScheduleTransfers,
    world: World,
) -> None:
    schedule_id = _schedule_clearing(schedules, world)
    allocate = AllocateFinancialEvent(factory, JournalService(factory))
    refund_id = _event(
        factory,
        key="test:cross-bank-refund",
        amount_minor=world.cross_bank_amount,
        card_id=world.card_id,
        event_type=FinancialEventType.REFUND,
        refund_of_event_id=world.cross_bank_event_id,
    )
    lines = allocate.propose_refund_lines(refund_id)
    allocate.execute(
        AllocateFinancialEventRequest(
            financial_event_id=refund_id,
            expected_version=1,
            client_token="token-refund",
            lines=tuple(
                AllocationLine(
                    amount_minor=line.amount_minor,
                    funding_wallet_id=line.funding_wallet_id,
                    budget_category_id=line.budget_category_id,
                )
                for line in lines
            ),
        )
    )
    # Deliberately no manual refresh: the refund path itself must leave the
    # commitment's status describing the coverage it actually has.
    with factory() as unit:
        schedule = unit.scheduled_transfers.get(schedule_id)
        links = unit.scheduled_transfer_commitments.list_for_schedule(schedule_id)
        commitment = unit.commitments.get(world.cross_bank_commitment_id)
    assert schedule is not None
    assert commitment is not None
    assert schedule.amount_minor == world.cross_bank_amount
    assert links[0].amount_minor == world.cross_bank_amount
    assert commitment.outstanding_minor == 0
    assert commitment.status is CommitmentStatus.CANCELLED
    # The bank was already told to move this money, so it is still reserved.
    assert (
        _reserved(factory, world.other_budget_id)
        == world.cross_bank_amount + world.second_cross_bank_amount
    )


def test_a_partial_refund_re_derives_the_commitment_status_by_itself(
    factory: UnitOfWorkFactory,
    schedules: ScheduleTransfers,
    world: World,
) -> None:
    """The refund path itself must leave the status describing the coverage.

    A commitment of 8,000 partly scheduled at 5,000 is `partially_scheduled`.
    Refunding 4,000 lowers the requirement to 4,000, which the existing 5,000
    of coverage now covers completely, so the persisted status has to become
    `scheduled`. No caller may have to remember to refresh it afterwards.
    """
    schedules.schedule_clearing(
        _clearing_request(
            world,
            amount_minor=5_000,
            token="partial-before-refund",
            lines=(
                ScheduleCommitmentLine(
                    funding_commitment_id=world.cross_bank_commitment_id,
                    amount_minor=5_000,
                ),
            ),
        )
    )
    assert (
        _commitment_status(factory, world.cross_bank_commitment_id)
        is CommitmentStatus.PARTIALLY_SCHEDULED
    )

    _refund(factory, world, amount_minor=4_000, token="partial-refund")

    with factory() as unit:
        commitment = unit.commitments.get(world.cross_bank_commitment_id)
    assert commitment is not None
    assert commitment.outstanding_minor == 4_000
    assert commitment.status is CommitmentStatus.SCHEDULED


def _refund(
    factory: UnitOfWorkFactory,
    world: World,
    *,
    amount_minor: int,
    token: str,
    event_id: int | None = None,
) -> None:
    """Post one refund of a card purchase through the ordinary path."""
    allocate = AllocateFinancialEvent(factory, JournalService(factory))
    refund_id = _event(
        factory,
        key=f"test:{token}",
        amount_minor=amount_minor,
        card_id=world.card_id,
        event_type=FinancialEventType.REFUND,
        refund_of_event_id=event_id or world.cross_bank_event_id,
    )
    lines = allocate.propose_refund_lines(refund_id)
    allocate.execute(
        AllocateFinancialEventRequest(
            financial_event_id=refund_id,
            expected_version=1,
            client_token=token,
            lines=tuple(
                AllocationLine(
                    amount_minor=line.amount_minor,
                    funding_wallet_id=line.funding_wallet_id,
                    budget_category_id=line.budget_category_id,
                )
                for line in lines
            ),
        )
    )


def test_cancelling_after_a_refund_lowers_the_reservation(
    factory: UnitOfWorkFactory,
    schedules: ScheduleTransfers,
    world: World,
) -> None:
    schedule_id = _schedule_clearing(schedules, world)
    with factory() as unit:
        unit.commitments.release(
            world.cross_bank_commitment_id, world.cross_bank_amount
        )
        unit.commitments.refresh_status(world.cross_bank_commitment_id)
        unit.commit()
    with factory() as unit:
        schedule = unit.scheduled_transfers.get(schedule_id)
    assert schedule is not None

    schedules.cancel(
        ScheduledTransferAction(
            scheduled_transfer_id=schedule_id, expected_version=schedule.version
        )
    )

    assert _reserved(factory, world.other_budget_id) == world.second_cross_bank_amount


def test_two_half_schedules_still_warn_after_a_refund(
    factory: UnitOfWorkFactory,
    clearing: ClearingQueries,
    schedules: ScheduleTransfers,
    world: World,
    clock: Clock,
) -> None:
    """Over-coverage belongs to the commitment, not to one link in isolation.

    Splitting 8,000 across two schedules of 4,000 and then refunding 4,000
    leaves 8,000 scheduled against a requirement of 4,000. Comparing each link
    with the requirement on its own sees 4,000 - 4,000 = 0 twice and warns
    about nothing, so the aggregate is what decides.
    """
    for index in (1, 2):
        schedules.schedule_clearing(
            _clearing_request(
                world,
                amount_minor=4_000,
                token=f"half-{index}",
                lines=(
                    ScheduleCommitmentLine(
                        funding_commitment_id=world.cross_bank_commitment_id,
                        amount_minor=4_000,
                    ),
                ),
            )
        )

    _refund(factory, world, amount_minor=4_000, token="split-refund")

    view = clearing.view(now=clock.now)
    assert view.has_refund_warning is True
    warned = [row for row in view.upcoming.rows if row.has_refund_warning]
    assert len(warned) == 2
    assert {row.excess_minor for row in warned} == {4_000}
    assert view.over_covered == 1


def test_one_whole_schedule_still_warns_after_a_refund(
    factory: UnitOfWorkFactory,
    clearing: ClearingQueries,
    schedules: ScheduleTransfers,
    world: World,
    clock: Clock,
) -> None:
    """The single-schedule case keeps working through the same aggregate."""
    _schedule_clearing(schedules, world)

    _refund(factory, world, amount_minor=4_000, token="whole-refund")

    view = clearing.view(now=clock.now)
    warned = [row for row in view.upcoming.rows if row.has_refund_warning]
    assert len(warned) == 1
    assert warned[0].excess_minor == 4_000
    assert view.has_refund_warning is True


def test_an_exactly_covered_commitment_never_warns(
    clearing: ClearingQueries, schedules: ScheduleTransfers, world: World, clock: Clock
) -> None:
    """Coverage equal to the requirement is the ordinary, quiet case."""
    _schedule_clearing(schedules, world)

    view = clearing.view(now=clock.now)

    assert view.has_refund_warning is False
    assert view.over_covered == 0
    assert all(row.excess_minor == 0 for row in view.upcoming.rows)


def test_an_executed_schedule_over_covered_by_a_refund_is_reported(
    factory: UnitOfWorkFactory,
    clearing: ClearingQueries,
    schedules: ScheduleTransfers,
    world: World,
    clock: Clock,
) -> None:
    """Money that already moved still counts toward the surplus.

    Once the transfer has executed there is nothing left to cancel, but the
    clearing hub really is holding 4,000 more than the card needed, and the
    page has to say so rather than fall silent.
    """
    _schedule_clearing(schedules, world)
    clock.now = DUE_AT + timedelta(minutes=1)
    schedules.run_due()

    _refund(factory, world, amount_minor=4_000, token="executed-refund")

    view = clearing.view(now=clock.now)
    assert view.over_covered == 1
    assert view.over_covered_commitments.rows[0].excess_minor == 4_000
    warned = [row for row in view.executed.rows if row.has_refund_warning]
    assert len(warned) == 1
    assert warned[0].is_cancellable is False


def test_an_over_covered_commitment_warns_even_from_a_later_page(
    factory: UnitOfWorkFactory,
    clearing: ClearingQueries,
    schedules: ScheduleTransfers,
    world: World,
    clock: Clock,
) -> None:
    """The warning must not depend on which page happens to be displayed."""
    for index in (1, 2):
        schedules.schedule_clearing(
            _clearing_request(
                world,
                amount_minor=4_000,
                token=f"paged-half-{index}",
                lines=(
                    ScheduleCommitmentLine(
                        funding_commitment_id=world.cross_bank_commitment_id,
                        amount_minor=4_000,
                    ),
                ),
            )
        )
    # A later movement for a different commitment sorts ahead of both halves.
    schedules.schedule_clearing(
        _clearing_request(
            world,
            amount_minor=world.second_cross_bank_amount,
            token="paged-newest",
            lines=(
                ScheduleCommitmentLine(
                    funding_commitment_id=world.second_cross_bank_commitment_id,
                    amount_minor=world.second_cross_bank_amount,
                ),
            ),
            scheduled_date=date(2026, 9, 20),
        )
    )
    _refund(factory, world, amount_minor=4_000, token="paged-refund")

    view = clearing.view(now=clock.now, limit=1)

    assert len(view.upcoming.rows) == 1
    assert view.upcoming.rows[0].has_refund_warning is False
    assert view.has_refund_warning is True
    assert view.over_covered == 1


def test_a_cancelled_schedule_stops_counting_toward_over_coverage(
    factory: UnitOfWorkFactory,
    clearing: ClearingQueries,
    schedules: ScheduleTransfers,
    world: World,
    clock: Clock,
) -> None:
    """Cancelling one of the two halves removes the surplus it created."""
    identifiers = []
    for index in (1, 2):
        identifiers.append(
            schedules.schedule_clearing(
                _clearing_request(
                    world,
                    amount_minor=4_000,
                    token=f"cancel-half-{index}",
                    lines=(
                        ScheduleCommitmentLine(
                            funding_commitment_id=world.cross_bank_commitment_id,
                            amount_minor=4_000,
                        ),
                    ),
                )
            ).schedule.id
        )
    _refund(factory, world, amount_minor=4_000, token="cancel-refund")
    assert clearing.view(now=clock.now).has_refund_warning is True

    with factory() as unit:
        schedule = unit.scheduled_transfers.get(identifiers[0])
    assert schedule is not None
    schedules.cancel(
        ScheduledTransferAction(
            scheduled_transfer_id=identifiers[0],
            expected_version=schedule.version,
        )
    )

    view = clearing.view(now=clock.now)
    assert view.has_refund_warning is False
    assert view.over_covered == 0


def test_a_cancelled_row_is_never_blamed_for_a_surplus(
    factory: UnitOfWorkFactory,
    clearing: ClearingQueries,
    schedules: ScheduleTransfers,
    world: World,
    clock: Clock,
) -> None:
    """A withdrawn instruction moves nothing, so it cannot be over-covering.

    Two schedules of 4,000 against a commitment refunded down to 2,000 leave a
    surplus of 6,000. Cancelling one lowers the surplus to 2,000 and the
    remaining live schedule still has to say so — but the cancelled row is no
    longer part of the problem and must stop claiming to be.
    """
    identifiers = [
        schedules.schedule_clearing(
            _clearing_request(
                world,
                amount_minor=4_000,
                token=f"blame-{index}",
                lines=(
                    ScheduleCommitmentLine(
                        funding_commitment_id=world.cross_bank_commitment_id,
                        amount_minor=4_000,
                    ),
                ),
            )
        ).schedule.id
        for index in (1, 2)
    ]
    _refund(factory, world, amount_minor=6_000, token="blame-refund")
    with factory() as unit:
        first = unit.scheduled_transfers.get(identifiers[0])
    assert first is not None
    schedules.cancel(
        ScheduledTransferAction(
            scheduled_transfer_id=identifiers[0], expected_version=first.version
        )
    )

    view = clearing.view(now=clock.now)

    live = [row for row in view.upcoming.rows if row.has_refund_warning]
    assert [row.schedule.id for row in live] == [identifiers[1]]
    assert live[0].excess_minor == 2_000
    assert live[0].warning_action == "cancel_and_replace"
    closed = {row.schedule.id: row for row in view.closed.rows}
    assert closed[identifiers[0]].has_refund_warning is False
    assert closed[identifiers[0]].excess_minor == 0
    assert closed[identifiers[0]].warning_action is None


def test_a_reversed_row_is_never_blamed_for_a_surplus(
    factory: UnitOfWorkFactory,
    clearing: ClearingQueries,
    schedules: ScheduleTransfers,
    world: World,
    clock: Clock,
) -> None:
    """A reversal put the money back, so that row is not over-covering either."""
    schedule_id = _schedule_clearing(schedules, world)
    clock.now = DUE_AT + timedelta(minutes=1)
    schedules.run_due()
    with factory() as unit:
        executed = unit.scheduled_transfers.get(schedule_id)
    assert executed is not None
    schedules.reverse(
        ScheduledTransferAction(
            scheduled_transfer_id=schedule_id, expected_version=executed.version
        )
    )

    _refund(factory, world, amount_minor=4_000, token="reversed-refund")

    view = clearing.view(now=clock.now)
    closed = {row.schedule.id: row for row in view.closed.rows}
    assert closed[schedule_id].has_refund_warning is False
    assert closed[schedule_id].excess_minor == 0
    assert view.over_covered == 0


def test_an_executed_row_is_told_to_reverse_not_to_cancel(
    factory: UnitOfWorkFactory,
    clearing: ClearingQueries,
    schedules: ScheduleTransfers,
    world: World,
    clock: Clock,
) -> None:
    """Money that already moved cannot be corrected by cancelling."""
    _schedule_clearing(schedules, world)
    clock.now = DUE_AT + timedelta(minutes=1)
    schedules.run_due()

    _refund(factory, world, amount_minor=4_000, token="executed-action")

    view = clearing.view(now=clock.now)
    warned = [row for row in view.executed.rows if row.has_refund_warning]
    assert len(warned) == 1
    assert warned[0].warning_action == "reverse"


def test_a_processing_row_is_told_to_wait_not_that_money_moved(
    factory: UnitOfWorkFactory,
    clearing: ClearingQueries,
    schedules: ScheduleTransfers,
    world: World,
    clock: Clock,
) -> None:
    """A claimed schedule has moved nothing yet and cannot be cancelled."""
    schedule_id = _schedule_clearing(schedules, world)
    clock.now = DUE_AT + timedelta(minutes=1)
    with factory() as unit:
        unit.scheduled_transfers.claim_due(
            now=clock.now,
            owner="owner-a",
            lease_until=clock.now + timedelta(seconds=120),
            limit=10,
        )
        unit.commit()
    _refund(factory, world, amount_minor=4_000, token="processing-refund")

    view = clearing.view(now=clock.now)

    warned = {row.schedule.id: row for row in view.upcoming.rows}
    assert warned[schedule_id].has_refund_warning is True
    assert warned[schedule_id].warning_action == "wait_for_run"


def test_the_over_covered_total_is_exact_beyond_the_page(
    factory: UnitOfWorkFactory,
    clearing: ClearingQueries,
    schedules: ScheduleTransfers,
    world: World,
    clock: Clock,
) -> None:
    """A truncated warning list must still report how many there really are."""
    for token, commitment_id, amount in (
        ("exact-a", world.cross_bank_commitment_id, world.cross_bank_amount),
        (
            "exact-b",
            world.second_cross_bank_commitment_id,
            world.second_cross_bank_amount,
        ),
    ):
        schedules.schedule_clearing(
            _clearing_request(
                world,
                amount_minor=amount,
                token=token,
                lines=(
                    ScheduleCommitmentLine(
                        funding_commitment_id=commitment_id, amount_minor=amount
                    ),
                ),
            )
        )
    _refund(factory, world, amount_minor=4_000, token="exact-refund-a")
    _refund(
        factory,
        world,
        amount_minor=2_000,
        token="exact-refund-b",
        event_id=world.second_cross_bank_event_id,
    )

    page = clearing.view(now=clock.now, limit=1).over_covered_commitments

    assert page.total == 2
    assert len(page.rows) == 1
    assert page.has_more is True
    assert clearing.view(now=clock.now, limit=1).over_covered == 2

    rest = clearing.view(now=clock.now, limit=1, warn_offset=1).over_covered_commitments
    assert len(rest.rows) == 1
    assert rest.has_more is False
    assert {row.funding_commitment_id for row in page.rows} != {
        row.funding_commitment_id for row in rest.rows
    }


def _claim(factory: UnitOfWorkFactory, clock: Clock) -> None:
    """Take every due schedule into `processing` without executing it."""
    with factory() as unit:
        unit.scheduled_transfers.claim_due(
            now=clock.now,
            owner="owner-a",
            lease_until=clock.now + timedelta(seconds=120),
            limit=10,
        )
        unit.commit()


def test_a_surplus_carried_only_by_a_claimed_schedule_says_wait(
    factory: UnitOfWorkFactory,
    clearing: ClearingQueries,
    schedules: ScheduleTransfers,
    world: World,
    clock: Clock,
) -> None:
    """A claimed movement cannot be cancelled, so the summary must not say so."""
    _schedule_clearing(schedules, world)
    clock.now = DUE_AT + timedelta(minutes=1)
    _claim(factory, clock)

    _refund(factory, world, amount_minor=4_000, token="processing-only")

    page = clearing.view(now=clock.now).over_covered_commitments
    assert page.total == 1
    assert page.actions == ("wait_for_run",)


def test_a_surplus_carried_only_by_an_executed_schedule_says_reverse(
    factory: UnitOfWorkFactory,
    clearing: ClearingQueries,
    schedules: ScheduleTransfers,
    world: World,
    clock: Clock,
) -> None:
    """Money that already moved can only be corrected by a reversal."""
    _schedule_clearing(schedules, world)
    clock.now = DUE_AT + timedelta(minutes=1)
    schedules.run_due()

    _refund(factory, world, amount_minor=4_000, token="executed-only")

    page = clearing.view(now=clock.now).over_covered_commitments
    assert page.total == 1
    assert page.actions == ("reverse",)


def test_a_mixed_surplus_lists_every_action_it_really_allows(
    factory: UnitOfWorkFactory,
    clearing: ClearingQueries,
    schedules: ScheduleTransfers,
    world: World,
    clock: Clock,
) -> None:
    """One half claimed and one half still scheduled allows two things."""
    for index in (1, 2):
        schedules.schedule_clearing(
            _clearing_request(
                world,
                amount_minor=4_000,
                token=f"mixed-{index}",
                lines=(
                    ScheduleCommitmentLine(
                        funding_commitment_id=world.cross_bank_commitment_id,
                        amount_minor=4_000,
                    ),
                ),
                scheduled_date=date(2026, 9, 1) if index == 1 else date(2026, 9, 20),
            )
        )
    clock.now = DUE_AT + timedelta(minutes=1)
    _claim(factory, clock)

    _refund(factory, world, amount_minor=6_000, token="mixed-refund")

    page = clearing.view(now=clock.now).over_covered_commitments
    assert page.actions == ("cancel_and_replace", "wait_for_run")


def test_a_warning_page_beyond_the_end_shows_the_last_real_page(
    factory: UnitOfWorkFactory,
    clearing: ClearingQueries,
    schedules: ScheduleTransfers,
    world: World,
    clock: Clock,
) -> None:
    """An out-of-range page must never claim to show rows 0 to 24,975."""
    for token, commitment_id, amount in (
        ("range-a", world.cross_bank_commitment_id, world.cross_bank_amount),
        (
            "range-b",
            world.second_cross_bank_commitment_id,
            world.second_cross_bank_amount,
        ),
    ):
        schedules.schedule_clearing(
            _clearing_request(
                world,
                amount_minor=amount,
                token=token,
                lines=(
                    ScheduleCommitmentLine(
                        funding_commitment_id=commitment_id, amount_minor=amount
                    ),
                ),
            )
        )
    _refund(factory, world, amount_minor=4_000, token="range-refund-a")
    _refund(
        factory,
        world,
        amount_minor=2_000,
        token="range-refund-b",
        event_id=world.second_cross_bank_event_id,
    )

    page = clearing.view(now=clock.now, limit=1, warn_offset=24_975)

    assert page.over_covered_commitments.total == 2
    assert len(page.over_covered_commitments.rows) == 1
    assert page.over_covered_commitments.offset == 1
    assert page.over_covered_commitments.shown_from == 2
    assert page.over_covered_commitments.shown_to == 2
    assert page.over_covered_commitments.page_number == 2
    assert page.over_covered_commitments.page_count == 2
    assert page.over_covered_commitments.has_more is False


def test_an_empty_clearing_hub_reports_a_single_empty_page(
    clearing: ClearingQueries, clock: Clock, world: World
) -> None:
    """No surplus at all still has to describe itself without dividing by zero.

    Both lists are checked, because they carry the same paging arithmetic and a
    defect in either one would show a page count of zero on a real screen.
    """
    del world

    view = clearing.view(now=clock.now, offset=500, warn_offset=500)

    page = view.over_covered_commitments
    assert (page.total, page.offset, page.rows) == (0, 0, ())
    assert (page.shown_from, page.shown_to) == (0, 0)
    assert (page.page_number, page.page_count) == (1, 1)
    assert page.has_more is False

    history = view.upcoming
    assert (history.total, history.offset, history.rows) == (0, 0, ())
    assert (history.page_number, history.page_count) == (1, 1)
    assert history.has_more is False


def test_a_history_page_beyond_the_end_shows_the_last_real_page(
    clearing: ClearingQueries, schedules: ScheduleTransfers, world: World, clock: Clock
) -> None:
    """The schedule history clamps the same way the warning list does."""
    _schedule_clearing(schedules, world)

    view = clearing.view(now=clock.now, limit=1, offset=24_975)

    assert view.upcoming.total == 1
    assert len(view.upcoming.rows) == 1
    assert view.upcoming.offset == 0
    assert view.upcoming.page_number == 1
    assert view.upcoming.page_count == 1


# --- G. B5 salary lines are never scheduled ---------------------------------


def test_a_waiting_salary_line_creates_no_commitment_to_schedule(
    factory: UnitOfWorkFactory, world: World
) -> None:
    """A salary line reserves through its own status, not a commitment.

    There is therefore nothing for a clearing schedule to select, which is
    what keeps ADR-B057 true by construction rather than by a filter.
    """
    with factory() as unit:
        commitments = unit.commitments.list_open_for_wallet(world.other_budget_id)
        salary_lines = unit.salary_run_items.list_waiting_for_source_wallet(
            world.other_budget_id
        )
    assert salary_lines == ()
    assert {item.id for item in commitments} == {
        world.cross_bank_commitment_id,
        world.second_cross_bank_commitment_id,
    }


@pytest.fixture(autouse=True)
def _no_stray_engines() -> Iterator[None]:
    """Keep every test honest about closing what it opened."""
    yield

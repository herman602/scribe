"""B3 posting acceptance tests: allocation, commitments, and refunds."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from app.application.queries.money import MoneyBalanceQueries
from app.application.services.cards import (
    ConfigureCardRequest,
    ManageCards,
    RegisterCardRequest,
)
from app.application.services.event_allocation import (
    AllocateFinancialEvent,
    AllocateFinancialEventRequest,
    AllocationLine,
    RedoAllocation,
    RedoAllocationRequest,
)
from app.application.services.ledger import (
    AddWalletRequest,
    JournalService,
    LedgerSetupService,
    OpenFinancialAccountRequest,
    OpeningPositionRequest,
)
from app.domain.budget_enums import (
    CardHolderRole,
    CommitmentFulfillmentMode,
    CommitmentStatus,
    FinancialAccountType,
    FinancialEventOrigin,
    FinancialEventStatus,
    FinancialEventType,
    JournalEntryType,
    PaymentMethod,
    RecognitionBasis,
)
from app.domain.budget_models import (
    BudgetCategoryDraft,
    FinancialAccountBalance,
    FinancialEventDraft,
    OpeningWalletSplit,
)
from app.domain.errors import (
    AllocationRedoError,
    CardConfigurationError,
    CurrencyMismatchError,
    FinancialEventError,
    IncompleteAllocationError,
    UnsupportedEventTypeError,
)
from app.domain.ports.repositories import UnitOfWork
from app.infrastructure.database.budget_queries import (
    SqlAlchemyLedgerBalanceReadRepository,
)
from app.infrastructure.database.engine import Database
from app.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork

OCCURRED_AT = datetime(2026, 8, 7, 13, 30, tzinfo=UTC)

type UnitOfWorkFactory = Callable[[], UnitOfWork]


@pytest.fixture
def factory(migrated_database: Database) -> UnitOfWorkFactory:
    """Return a unit-of-work factory bound to the migrated test database."""

    def build() -> UnitOfWork:
        return SqlAlchemyUnitOfWork(migrated_database.session_factory)

    return build


@pytest.fixture
def allocate(factory: UnitOfWorkFactory) -> AllocateFinancialEvent:
    """Return the allocation service over a frozen-clock journal."""
    return AllocateFinancialEvent(factory, JournalService(factory))


@pytest.fixture
def redo(factory: UnitOfWorkFactory) -> RedoAllocation:
    """Return the allocation-redo service over the same journal."""
    return RedoAllocation(factory, JournalService(factory))


@pytest.fixture
def balances(migrated_database: Database) -> MoneyBalanceQueries:
    """Return calculated balances over the same database."""
    return MoneyBalanceQueries(
        SqlAlchemyLedgerBalanceReadRepository(migrated_database.session_factory)
    )


def _balance(balances: MoneyBalanceQueries, account_id: int) -> FinancialAccountBalance:
    for item in balances.account_balances():
        if item.financial_account_id == account_id:
            return item
    raise AssertionError("account balance was not found")


def _wallet_total(balances: MoneyBalanceQueries, account_id: int, name: str) -> int:
    for wallet in _balance(balances, account_id).wallets:
        if wallet.name == name:
            return wallet.balance_minor
    raise AssertionError("wallet was not found")


@pytest.fixture
def world(factory: UnitOfWorkFactory) -> dict[str, int]:
    """Build one bank account, a card, its liability, and two categories."""
    setup = LedgerSetupService(factory)
    cards = ManageCards(factory)
    bank = setup.open_account(
        OpenFinancialAccountRequest(
            institution_code="test_bank",
            display_name="測試支票帳戶",
            account_type=FinancialAccountType.CHECKING,
            currency="TWD",
        )
    )
    liability = setup.open_account(
        OpenFinancialAccountRequest(
            institution_code="test_bank",
            display_name="測試信用卡負債",
            account_type=FinancialAccountType.CARD_LIABILITY,
            currency="TWD",
        )
    )
    rent = setup.add_wallet(
        AddWalletRequest(financial_account_id=bank.account.id, name="房租")
    )
    JournalService(factory).record_opening_position(
        OpeningPositionRequest(
            financial_account_id=bank.account.id,
            balance_minor=1_000_000,
            observed_at=OCCURRED_AT,
            splits=(
                OpeningWalletSplit(wallet_name="尚未分配", amount_minor=700_000),
                OpeningWalletSplit(wallet_name="房租", amount_minor=300_000),
            ),
        )
    )
    with factory() as unit:
        account = unit.accounts.get_or_create("user@example.test")
        unallocated = unit.wallets.get_unallocated(bank.account.id)
        assert unallocated is not None
        food = unit.budget_categories.add(BudgetCategoryDraft(name="飲食"))
        transport = unit.budget_categories.add(BudgetCategoryDraft(name="交通"))
        unit.commit()
        gmail_account_id = account.id
        food_id, transport_id = food.id, transport.id
        unallocated_id = unallocated.id

    card = cards.register(
        RegisterCardRequest(
            gmail_account_id=gmail_account_id,
            issuer_code="test_bank",
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
            settlement_account_id=bank.account.id,
            default_funding_wallet_id=unallocated_id,
        )
    )
    return {
        "bank_account_id": bank.account.id,
        "liability_account_id": liability.account.id,
        "unallocated_id": unallocated_id,
        "rent_id": rent.id,
        "food_id": food_id,
        "transport_id": transport_id,
        "card_id": card.id,
    }


def _event(
    factory: UnitOfWorkFactory,
    *,
    key: str,
    amount_minor: int,
    payment_method: PaymentMethod,
    card_id: int | None = None,
    event_type: FinancialEventType = FinancialEventType.PURCHASE,
    refund_of_event_id: int | None = None,
) -> int:
    with factory() as unit:
        event = unit.financial_events.add(
            FinancialEventDraft(
                origin=FinancialEventOrigin.GMAIL_TRANSACTION,
                event_type=event_type,
                payment_method=payment_method,
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


def test_a_purchase_posts_its_expense_exactly_once(
    factory: UnitOfWorkFactory,
    allocate: AllocateFinancialEvent,
    world: dict[str, int],
) -> None:
    event_id = _event(
        factory,
        key="gmail_transaction:1",
        amount_minor=128_000,
        payment_method=PaymentMethod.CREDIT_CARD,
        card_id=world["card_id"],
    )

    result = allocate.execute(
        AllocateFinancialEventRequest(
            financial_event_id=event_id,
            expected_version=1,
            client_token="token-1",
            lines=(
                AllocationLine(
                    amount_minor=128_000,
                    funding_wallet_id=world["unallocated_id"],
                    budget_category_id=world["food_id"],
                ),
            ),
        )
    )

    assert result.event.status is FinancialEventStatus.POSTED
    assert result.entry.entry_type is JournalEntryType.EXPENSE
    assert result.entry.financial_event_id == event_id
    assert len(result.allocations) == 1
    with factory() as unit:
        entries = unit.journal.get_by_financial_event(event_id)
        assert entries is not None
        assert sum(posting.amount_minor for posting in entries.postings) == 0
    # A resubmitted form returns the same entry instead of posting twice.
    repeated = allocate.execute(
        AllocateFinancialEventRequest(
            financial_event_id=event_id,
            expected_version=1,
            client_token="token-1",
            lines=(
                AllocationLine(
                    amount_minor=128_000,
                    funding_wallet_id=world["unallocated_id"],
                    budget_category_id=world["food_id"],
                ),
            ),
        )
    )
    assert repeated.entry.id == result.entry.id


def test_a_card_purchase_moves_liability_and_never_the_bank_balance(
    factory: UnitOfWorkFactory,
    allocate: AllocateFinancialEvent,
    balances: MoneyBalanceQueries,
    world: dict[str, int],
) -> None:
    before_bank = _balance(balances, world["bank_account_id"]).balance_minor
    event_id = _event(
        factory,
        key="gmail_transaction:1",
        amount_minor=128_000,
        payment_method=PaymentMethod.CREDIT_CARD,
        card_id=world["card_id"],
    )

    result = allocate.execute(
        AllocateFinancialEventRequest(
            financial_event_id=event_id,
            expected_version=1,
            client_token="token-1",
            lines=(
                AllocationLine(
                    amount_minor=128_000,
                    funding_wallet_id=world["unallocated_id"],
                    budget_category_id=world["food_id"],
                ),
            ),
        )
    )

    assert _balance(balances, world["bank_account_id"]).balance_minor == before_bank
    assert _balance(balances, world["liability_account_id"]).balance_minor == -128_000
    # The purchase reserves funding instead of pretending the bank paid.
    assert len(result.commitments) == 1
    commitment = result.commitments[0]
    assert commitment.amount_minor == 128_000
    assert commitment.status is CommitmentStatus.OPEN
    assert commitment.fulfillment_mode is CommitmentFulfillmentMode.RESERVE_ONLY


def test_a_gmail_allocation_can_be_redone_and_leaves_no_double_reservation(
    factory: UnitOfWorkFactory,
    allocate: AllocateFinancialEvent,
    redo: RedoAllocation,
    balances: MoneyBalanceQueries,
    world: dict[str, int],
) -> None:
    """A wallet chosen by mistake can be replaced, exactly once (ADR-B103).

    The event is Gmail-origin, so cancelling it is refused on the grounds
    that the bank's statement is not Scribe's to deny. Its allocation is the
    user's own decision, so redoing that has to work -- and has to leave the
    first reservation released, or the same money is held twice.
    """
    event_id = _event(
        factory,
        key="gmail_transaction:redo",
        amount_minor=128_000,
        payment_method=PaymentMethod.CREDIT_CARD,
        card_id=world["card_id"],
    )
    allocate.execute(
        AllocateFinancialEventRequest(
            financial_event_id=event_id,
            expected_version=1,
            client_token="token-first",
            lines=(
                AllocationLine(
                    amount_minor=128_000,
                    funding_wallet_id=world["unallocated_id"],
                    budget_category_id=world["food_id"],
                ),
            ),
        )
    )
    liability_after_first = _balance(
        balances, world["liability_account_id"]
    ).balance_minor

    with factory() as unit:
        posted = unit.financial_events.get(event_id)
    assert posted is not None
    undone = redo.execute(
        RedoAllocationRequest(
            financial_event_id=event_id, expected_version=posted.version
        )
    )

    assert undone.event.status is FinancialEventStatus.NEEDS_ALLOCATION
    assert undone.superseded_allocations == 1
    # The reversal cancels the liability the first allocation created, and
    # every reservation it took is given back.
    assert _balance(balances, world["liability_account_id"]).balance_minor == 0
    assert liability_after_first == -128_000
    assert all(
        commitment.outstanding_minor == 0 for commitment in undone.released_commitments
    )
    # The superseded rows stay, the way a reversal leaves them (ADR-B069);
    # what makes them harmless is that their ledger lines are cancelled.
    with factory() as unit:
        assert len(unit.allocations.list_for_event(event_id)) == 1

    second = allocate.execute(
        AllocateFinancialEventRequest(
            financial_event_id=event_id,
            expected_version=undone.event.version,
            client_token="token-second",
            lines=(
                AllocationLine(
                    amount_minor=128_000,
                    funding_wallet_id=world["rent_id"],
                    budget_category_id=world["transport_id"],
                ),
            ),
        )
    )

    assert second.event.status is FinancialEventStatus.POSTED
    # One liability, not two: the first one was reversed rather than left.
    assert _balance(balances, world["liability_account_id"]).balance_minor == -128_000
    assert [item.funding_wallet_id for item in second.allocations] == [world["rent_id"]]
    with factory() as unit:
        # Two rows now: the superseded decision and the one in force.
        assert len(unit.allocations.list_for_event(event_id)) == 2
    with factory() as unit:
        outstanding = sum(
            commitment.outstanding_minor
            for commitment in unit.commitments.list_for_event(event_id)
        )
    assert outstanding == 128_000


def test_a_posted_allocation_is_the_only_thing_a_redo_will_accept(
    factory: UnitOfWorkFactory,
    redo: RedoAllocation,
    world: dict[str, int],
) -> None:
    """Nothing is posted yet, so there is no decision to take back."""
    event_id = _event(
        factory,
        key="gmail_transaction:unposted",
        amount_minor=5_000,
        payment_method=PaymentMethod.CREDIT_CARD,
        card_id=world["card_id"],
    )

    with pytest.raises(AllocationRedoError):
        redo.execute(
            RedoAllocationRequest(financial_event_id=event_id, expected_version=1)
        )


def test_a_cash_purchase_reduces_only_its_funding_wallet(
    factory: UnitOfWorkFactory,
    allocate: AllocateFinancialEvent,
    balances: MoneyBalanceQueries,
    world: dict[str, int],
) -> None:
    event_id = _event(
        factory,
        key="gmail_transaction:1",
        amount_minor=50_000,
        payment_method=PaymentMethod.BANK_ACCOUNT,
    )

    result = allocate.execute(
        AllocateFinancialEventRequest(
            financial_event_id=event_id,
            expected_version=1,
            client_token="token-1",
            lines=(
                AllocationLine(
                    amount_minor=50_000,
                    funding_wallet_id=world["rent_id"],
                    budget_category_id=world["food_id"],
                ),
            ),
        )
    )

    assert result.commitments == ()
    assert _balance(balances, world["bank_account_id"]).balance_minor == 950_000
    assert _wallet_total(balances, world["bank_account_id"], "房租") == 250_000
    assert _wallet_total(balances, world["bank_account_id"], "尚未分配") == 700_000
    assert _balance(balances, world["liability_account_id"]).balance_minor == 0


def test_a_split_allocation_must_total_the_event_exactly(
    factory: UnitOfWorkFactory,
    allocate: AllocateFinancialEvent,
    world: dict[str, int],
) -> None:
    event_id = _event(
        factory,
        key="gmail_transaction:1",
        amount_minor=100_000,
        payment_method=PaymentMethod.CREDIT_CARD,
        card_id=world["card_id"],
    )

    def _request(*amounts: int) -> AllocateFinancialEventRequest:
        return AllocateFinancialEventRequest(
            financial_event_id=event_id,
            expected_version=1,
            client_token="token-1",
            lines=tuple(
                AllocationLine(
                    amount_minor=amount,
                    funding_wallet_id=world["unallocated_id"],
                    budget_category_id=category,
                )
                for amount, category in zip(
                    amounts,
                    (world["food_id"], world["transport_id"]),
                    strict=False,
                )
            ),
        )

    for amounts in ((60_000, 30_000), (60_000, 50_000)):
        with pytest.raises(IncompleteAllocationError, match="total exactly"):
            allocate.execute(_request(*amounts))

    result = allocate.execute(_request(60_000, 40_000))

    assert len(result.allocations) == 2
    assert {item.amount_minor for item in result.allocations} == {60_000, 40_000}
    assert {item.currency for item in result.allocations} == {"TWD"}
    assert len(result.commitments) == 2
    with factory() as unit:
        entry = unit.journal.get_by_financial_event(event_id)
        assert entry is not None
        assert sum(posting.amount_minor for posting in entry.postings) == 0
        categories = {
            posting.budget_category_id
            for posting in entry.postings
            if posting.budget_category_id is not None
        }
        assert categories == {world["food_id"], world["transport_id"]}


def test_a_refund_returns_to_the_original_category_and_direction(
    factory: UnitOfWorkFactory,
    allocate: AllocateFinancialEvent,
    balances: MoneyBalanceQueries,
    world: dict[str, int],
) -> None:
    purchase_id = _event(
        factory,
        key="gmail_transaction:1",
        amount_minor=128_000,
        payment_method=PaymentMethod.CREDIT_CARD,
        card_id=world["card_id"],
    )
    allocate.execute(
        AllocateFinancialEventRequest(
            financial_event_id=purchase_id,
            expected_version=1,
            client_token="token-1",
            lines=(
                AllocationLine(
                    amount_minor=128_000,
                    funding_wallet_id=world["unallocated_id"],
                    budget_category_id=world["food_id"],
                ),
            ),
        )
    )
    refund_id = _event(
        factory,
        key="gmail_transaction:2",
        amount_minor=28_000,
        payment_method=PaymentMethod.CREDIT_CARD,
        card_id=world["card_id"],
        event_type=FinancialEventType.REFUND,
        refund_of_event_id=purchase_id,
    )

    proposed = allocate.propose_refund_lines(refund_id)
    result = allocate.execute(
        AllocateFinancialEventRequest(
            financial_event_id=refund_id,
            expected_version=1,
            client_token="token-2",
            lines=tuple(
                AllocationLine(
                    amount_minor=line.amount_minor,
                    funding_wallet_id=line.funding_wallet_id,
                    budget_category_id=line.budget_category_id,
                )
                for line in proposed
            ),
        )
    )

    # The draft inherits the original purpose rather than inventing one.
    assert len(proposed) == 1
    assert proposed[0].budget_category_id == world["food_id"]
    assert proposed[0].amount_minor == 28_000
    assert result.entry.entry_type is JournalEntryType.REFUND
    assert result.commitments == ()
    # Liability falls back by the refund, and category totals net out.
    assert _balance(balances, world["liability_account_id"]).balance_minor == -100_000
    with factory() as unit:
        food_total = 0
        for event_id in (purchase_id, refund_id):
            entry = unit.journal.get_by_financial_event(event_id)
            assert entry is not None
            food_total += sum(
                posting.amount_minor
                for posting in entry.postings
                if posting.budget_category_id == world["food_id"]
            )
            assert sum(posting.amount_minor for posting in entry.postings) == 0
        assert food_total == 100_000
        # The unused reservation is released exactly once.
        commitment = unit.commitments.list_for_event(purchase_id)[0]
        assert commitment.released_minor == 28_000
        assert commitment.outstanding_minor == 100_000


def test_a_refund_cannot_refund_another_refund(
    factory: UnitOfWorkFactory,
    allocate: AllocateFinancialEvent,
    world: dict[str, int],
) -> None:
    """The cross-row rule the row-level check constraint cannot express."""
    purchase_id = _event(
        factory,
        key="gmail_transaction:1",
        amount_minor=128_000,
        payment_method=PaymentMethod.CREDIT_CARD,
        card_id=world["card_id"],
    )
    first_refund = _event(
        factory,
        key="gmail_transaction:2",
        amount_minor=28_000,
        payment_method=PaymentMethod.CREDIT_CARD,
        card_id=world["card_id"],
        event_type=FinancialEventType.REFUND,
        refund_of_event_id=purchase_id,
    )
    second_refund = _event(
        factory,
        key="gmail_transaction:3",
        amount_minor=1_000,
        payment_method=PaymentMethod.CREDIT_CARD,
        card_id=world["card_id"],
        event_type=FinancialEventType.REFUND,
        refund_of_event_id=first_refund,
    )

    with pytest.raises(FinancialEventError, match="refund another refund"):
        allocate.propose_refund_lines(second_refund)
    with pytest.raises(FinancialEventError, match="refund another refund"):
        allocate.execute(
            AllocateFinancialEventRequest(
                financial_event_id=second_refund,
                expected_version=1,
                client_token="token-3",
                lines=(
                    AllocationLine(
                        amount_minor=1_000,
                        funding_wallet_id=world["unallocated_id"],
                        budget_category_id=world["food_id"],
                    ),
                ),
            )
        )
    with factory() as unit:
        assert unit.journal.get_by_financial_event(second_refund) is None
        assert unit.allocations.list_for_event(second_refund) == ()


def test_a_refund_cannot_exceed_the_event_it_refunds(
    factory: UnitOfWorkFactory,
    allocate: AllocateFinancialEvent,
    world: dict[str, int],
) -> None:
    purchase_id = _event(
        factory,
        key="gmail_transaction:1",
        amount_minor=10_000,
        payment_method=PaymentMethod.CREDIT_CARD,
        card_id=world["card_id"],
    )
    allocate.execute(
        AllocateFinancialEventRequest(
            financial_event_id=purchase_id,
            expected_version=1,
            client_token="token-1",
            lines=(
                AllocationLine(
                    amount_minor=10_000,
                    funding_wallet_id=world["unallocated_id"],
                    budget_category_id=world["food_id"],
                ),
            ),
        )
    )
    oversized = _event(
        factory,
        key="gmail_transaction:2",
        amount_minor=20_000,
        payment_method=PaymentMethod.CREDIT_CARD,
        card_id=world["card_id"],
        event_type=FinancialEventType.REFUND,
        refund_of_event_id=purchase_id,
    )

    with pytest.raises(FinancialEventError, match="cannot exceed"):
        allocate.propose_refund_lines(oversized)


def test_a_partial_refund_splits_the_original_deterministically(
    factory: UnitOfWorkFactory,
    allocate: AllocateFinancialEvent,
    world: dict[str, int],
) -> None:
    purchase_id = _event(
        factory,
        key="gmail_transaction:1",
        amount_minor=100_000,
        payment_method=PaymentMethod.CREDIT_CARD,
        card_id=world["card_id"],
    )
    allocate.execute(
        AllocateFinancialEventRequest(
            financial_event_id=purchase_id,
            expected_version=1,
            client_token="token-1",
            lines=(
                AllocationLine(
                    amount_minor=70_000,
                    funding_wallet_id=world["unallocated_id"],
                    budget_category_id=world["food_id"],
                ),
                AllocationLine(
                    amount_minor=30_000,
                    funding_wallet_id=world["unallocated_id"],
                    budget_category_id=world["transport_id"],
                ),
            ),
        )
    )
    refund_id = _event(
        factory,
        key="gmail_transaction:2",
        amount_minor=999,
        payment_method=PaymentMethod.CREDIT_CARD,
        card_id=world["card_id"],
        event_type=FinancialEventType.REFUND,
        refund_of_event_id=purchase_id,
    )

    proposed = allocate.propose_refund_lines(refund_id)

    # 70/30 of 999 floors to 699 + 299; the spare unit goes to the larger
    # remainder (0.7 beats 0.3), never to whichever row happens to be first.
    assert [line.amount_minor for line in proposed] == [699, 300]
    assert sum(line.amount_minor for line in proposed) == 999
    assert [line.budget_category_id for line in proposed] == [
        world["food_id"],
        world["transport_id"],
    ]
    assert allocate.propose_refund_lines(refund_id) == proposed


def test_an_unconfigured_card_cannot_post(
    factory: UnitOfWorkFactory,
    allocate: AllocateFinancialEvent,
    world: dict[str, int],
) -> None:
    cards = ManageCards(factory)
    incomplete = cards.register(
        RegisterCardRequest(
            gmail_account_id=1,
            issuer_code="other_bank",
            display_name="未設定卡",
            last_four="9876",
        )
    )
    event_id = _event(
        factory,
        key="gmail_transaction:1",
        amount_minor=1_000,
        payment_method=PaymentMethod.CREDIT_CARD,
        card_id=incomplete.id,
    )

    with pytest.raises(CardConfigurationError, match="liability and settlement"):
        allocate.execute(
            AllocateFinancialEventRequest(
                financial_event_id=event_id,
                expected_version=1,
                client_token="token-1",
                lines=(
                    AllocationLine(
                        amount_minor=1_000,
                        funding_wallet_id=world["unallocated_id"],
                        budget_category_id=world["food_id"],
                    ),
                ),
            )
        )
    with factory() as unit:
        assert unit.journal.get_by_financial_event(event_id) is None
        assert unit.allocations.list_for_event(event_id) == ()
        event = unit.financial_events.get(event_id)
        assert event is not None
        assert event.status is FinancialEventStatus.NEEDS_ALLOCATION


def test_a_posted_event_cannot_be_allocated_again(
    factory: UnitOfWorkFactory,
    allocate: AllocateFinancialEvent,
    world: dict[str, int],
) -> None:
    event_id = _event(
        factory,
        key="gmail_transaction:1",
        amount_minor=1_000,
        payment_method=PaymentMethod.BANK_ACCOUNT,
    )
    line = AllocationLine(
        amount_minor=1_000,
        funding_wallet_id=world["unallocated_id"],
        budget_category_id=world["food_id"],
    )
    allocate.execute(
        AllocateFinancialEventRequest(
            financial_event_id=event_id,
            expected_version=1,
            client_token="token-1",
            lines=(line,),
        )
    )

    with pytest.raises(FinancialEventError, match="cannot be allocated"):
        allocate.execute(
            AllocateFinancialEventRequest(
                financial_event_id=event_id,
                expected_version=2,
                client_token="token-2",
                lines=(line,),
            )
        )
    # A stale page must not post against an event that moved underneath it.
    other_id = _event(
        factory,
        key="gmail_transaction:2",
        amount_minor=1_000,
        payment_method=PaymentMethod.BANK_ACCOUNT,
    )
    with pytest.raises(FinancialEventError, match="changed after"):
        allocate.execute(
            AllocateFinancialEventRequest(
                financial_event_id=other_id,
                expected_version=7,
                client_token="token-3",
                lines=(line,),
            )
        )
    with pytest.raises(FinancialEventError, match="was not found"):
        allocate.execute(
            AllocateFinancialEventRequest(
                financial_event_id=999,
                expected_version=1,
                client_token="token-4",
                lines=(line,),
            )
        )
    # Reusing one token for a different split must not replay the first answer.
    with pytest.raises(FinancialEventError, match="different allocation"):
        allocate.execute(
            AllocateFinancialEventRequest(
                financial_event_id=event_id,
                expected_version=1,
                client_token="token-1",
                lines=(
                    AllocationLine(
                        amount_minor=1_000,
                        funding_wallet_id=world["rent_id"],
                        budget_category_id=world["food_id"],
                    ),
                ),
            )
        )


def test_allocation_requires_lines_and_a_client_token(
    factory: UnitOfWorkFactory,
    allocate: AllocateFinancialEvent,
    world: dict[str, int],
) -> None:
    event_id = _event(
        factory,
        key="gmail_transaction:1",
        amount_minor=1_000,
        payment_method=PaymentMethod.BANK_ACCOUNT,
    )
    line = AllocationLine(
        amount_minor=1_000,
        funding_wallet_id=world["unallocated_id"],
        budget_category_id=world["food_id"],
    )

    with pytest.raises(IncompleteAllocationError, match="at least one line"):
        allocate.execute(
            AllocateFinancialEventRequest(
                financial_event_id=event_id,
                expected_version=1,
                client_token="token-1",
                lines=(),
            )
        )
    with pytest.raises(ValueError, match="client idempotency token"):
        allocate.execute(
            AllocateFinancialEventRequest(
                financial_event_id=event_id,
                expected_version=1,
                client_token="   ",
                lines=(line,),
            )
        )


@pytest.mark.parametrize(
    "event_type",
    [
        FinancialEventType.DEPOSIT,
        FinancialEventType.WITHDRAWAL,
        FinancialEventType.TRANSFER,
        FinancialEventType.INTEREST_INCOME,
        FinancialEventType.ADJUSTMENT,
    ],
)
def test_only_spending_events_may_be_allocated(
    factory: UnitOfWorkFactory,
    allocate: AllocateFinancialEvent,
    world: dict[str, int],
    event_type: FinancialEventType,
) -> None:
    """A deposit is not a purchase; posting it as one would invent spending."""
    event_id = _event(
        factory,
        key=f"gmail_transaction:{event_type.value}",
        amount_minor=50_000,
        payment_method=PaymentMethod.BANK_ACCOUNT,
        event_type=event_type,
    )

    with pytest.raises(UnsupportedEventTypeError, match="own posting rule"):
        allocate.execute(
            AllocateFinancialEventRequest(
                financial_event_id=event_id,
                expected_version=1,
                client_token="token-1",
                lines=(
                    AllocationLine(
                        amount_minor=50_000,
                        funding_wallet_id=world["rent_id"],
                        budget_category_id=world["food_id"],
                    ),
                ),
            )
        )
    with factory() as unit:
        assert unit.journal.get_by_financial_event(event_id) is None
        assert unit.allocations.list_for_event(event_id) == ()
        event = unit.financial_events.get(event_id)
        assert event is not None
        assert event.status is not FinancialEventStatus.POSTED


@pytest.mark.parametrize(
    "event_type",
    [
        FinancialEventType.PURCHASE,
        FinancialEventType.FEE,
        FinancialEventType.SUBSCRIPTION,
        FinancialEventType.BILL_PAYMENT,
        FinancialEventType.DIRECT_DEBIT,
    ],
)
def test_every_spending_event_type_can_be_allocated(
    factory: UnitOfWorkFactory,
    allocate: AllocateFinancialEvent,
    world: dict[str, int],
    event_type: FinancialEventType,
) -> None:
    event_id = _event(
        factory,
        key=f"gmail_transaction:{event_type.value}",
        amount_minor=1_000,
        payment_method=PaymentMethod.BANK_ACCOUNT,
        event_type=event_type,
    )

    result = allocate.execute(
        AllocateFinancialEventRequest(
            financial_event_id=event_id,
            expected_version=1,
            client_token="token-1",
            lines=(
                AllocationLine(
                    amount_minor=1_000,
                    funding_wallet_id=world["rent_id"],
                    budget_category_id=world["food_id"],
                ),
            ),
        )
    )

    assert result.entry.entry_type is JournalEntryType.EXPENSE
    assert result.event.status is FinancialEventStatus.POSTED


def test_partial_refunds_cannot_add_up_past_the_original(
    factory: UnitOfWorkFactory,
    allocate: AllocateFinancialEvent,
    balances: MoneyBalanceQueries,
    world: dict[str, int],
) -> None:
    """Two refunds that each pass on their own must not pass together."""
    purchase_id = _event(
        factory,
        key="gmail_transaction:1",
        amount_minor=100_000,
        payment_method=PaymentMethod.CREDIT_CARD,
        card_id=world["card_id"],
    )
    allocate.execute(
        AllocateFinancialEventRequest(
            financial_event_id=purchase_id,
            expected_version=1,
            client_token="token-1",
            lines=(
                AllocationLine(
                    amount_minor=100_000,
                    funding_wallet_id=world["unallocated_id"],
                    budget_category_id=world["food_id"],
                ),
            ),
        )
    )

    def _refund(key: str, amount_minor: int) -> int:
        return _event(
            factory,
            key=key,
            amount_minor=amount_minor,
            payment_method=PaymentMethod.CREDIT_CARD,
            card_id=world["card_id"],
            event_type=FinancialEventType.REFUND,
            refund_of_event_id=purchase_id,
        )

    first = _refund("gmail_transaction:2", 60_000)
    second = _refund("gmail_transaction:3", 60_000)
    allocate.execute(
        AllocateFinancialEventRequest(
            financial_event_id=first,
            expected_version=1,
            client_token="token-2",
            lines=(
                AllocationLine(
                    amount_minor=60_000,
                    funding_wallet_id=world["unallocated_id"],
                    budget_category_id=world["food_id"],
                ),
            ),
        )
    )

    with pytest.raises(FinancialEventError, match="cannot exceed the event"):
        allocate.propose_refund_lines(second)
    with pytest.raises(FinancialEventError, match="cannot exceed the event"):
        allocate.execute(
            AllocateFinancialEventRequest(
                financial_event_id=second,
                expected_version=1,
                client_token="token-3",
                lines=(
                    AllocationLine(
                        amount_minor=60_000,
                        funding_wallet_id=world["unallocated_id"],
                        budget_category_id=world["food_id"],
                    ),
                ),
            )
        )
    # Neither the liability nor the category may be over-credited.
    assert _balance(balances, world["liability_account_id"]).balance_minor == -40_000
    with factory() as unit:
        assert unit.journal.get_by_financial_event(second) is None
        commitment = unit.commitments.list_for_event(purchase_id)[0]
        assert commitment.released_minor == 60_000
        assert commitment.outstanding_minor == 40_000
    # The remainder is still refundable, exactly once.
    third = _refund("gmail_transaction:4", 40_000)
    allocate.execute(
        AllocateFinancialEventRequest(
            financial_event_id=third,
            expected_version=1,
            client_token="token-4",
            lines=(
                AllocationLine(
                    amount_minor=40_000,
                    funding_wallet_id=world["unallocated_id"],
                    budget_category_id=world["food_id"],
                ),
            ),
        )
    )
    assert _balance(balances, world["liability_account_id"]).balance_minor == 0


def test_a_second_partial_refund_is_proposed_against_what_is_left(
    factory: UnitOfWorkFactory,
    allocate: AllocateFinancialEvent,
    world: dict[str, int],
) -> None:
    purchase_id = _event(
        factory,
        key="gmail_transaction:1",
        amount_minor=100_000,
        payment_method=PaymentMethod.CREDIT_CARD,
        card_id=world["card_id"],
    )
    allocate.execute(
        AllocateFinancialEventRequest(
            financial_event_id=purchase_id,
            expected_version=1,
            client_token="token-1",
            lines=(
                AllocationLine(
                    amount_minor=70_000,
                    funding_wallet_id=world["unallocated_id"],
                    budget_category_id=world["food_id"],
                ),
                AllocationLine(
                    amount_minor=30_000,
                    funding_wallet_id=world["unallocated_id"],
                    budget_category_id=world["transport_id"],
                ),
            ),
        )
    )
    first = _event(
        factory,
        key="gmail_transaction:2",
        amount_minor=70_000,
        payment_method=PaymentMethod.CREDIT_CARD,
        card_id=world["card_id"],
        event_type=FinancialEventType.REFUND,
        refund_of_event_id=purchase_id,
    )
    allocate.execute(
        AllocateFinancialEventRequest(
            financial_event_id=first,
            expected_version=1,
            client_token="token-2",
            lines=(
                AllocationLine(
                    amount_minor=70_000,
                    funding_wallet_id=world["unallocated_id"],
                    budget_category_id=world["food_id"],
                ),
            ),
        )
    )
    second = _event(
        factory,
        key="gmail_transaction:3",
        amount_minor=30_000,
        payment_method=PaymentMethod.CREDIT_CARD,
        card_id=world["card_id"],
        event_type=FinancialEventType.REFUND,
        refund_of_event_id=purchase_id,
    )

    proposed = allocate.propose_refund_lines(second)

    # Food is fully refunded already, so only transport can still return money.
    assert [line.budget_category_id for line in proposed] == [world["transport_id"]]
    assert [line.amount_minor for line in proposed] == [30_000]


def test_a_refund_cannot_move_money_to_a_share_the_original_never_used(
    factory: UnitOfWorkFactory,
    allocate: AllocateFinancialEvent,
    world: dict[str, int],
) -> None:
    """Refunding into another category would credit the wrong budget."""
    purchase_id = _event(
        factory,
        key="gmail_transaction:1",
        amount_minor=100_000,
        payment_method=PaymentMethod.CREDIT_CARD,
        card_id=world["card_id"],
    )
    allocate.execute(
        AllocateFinancialEventRequest(
            financial_event_id=purchase_id,
            expected_version=1,
            client_token="token-1",
            lines=(
                AllocationLine(
                    amount_minor=70_000,
                    funding_wallet_id=world["unallocated_id"],
                    budget_category_id=world["food_id"],
                ),
                AllocationLine(
                    amount_minor=30_000,
                    funding_wallet_id=world["unallocated_id"],
                    budget_category_id=world["transport_id"],
                ),
            ),
        )
    )
    refund_id = _event(
        factory,
        key="gmail_transaction:2",
        amount_minor=100_000,
        payment_method=PaymentMethod.CREDIT_CARD,
        card_id=world["card_id"],
        event_type=FinancialEventType.REFUND,
        refund_of_event_id=purchase_id,
    )

    with pytest.raises(FinancialEventError, match="more than that share"):
        allocate.execute(
            AllocateFinancialEventRequest(
                financial_event_id=refund_id,
                expected_version=1,
                client_token="token-2",
                lines=(
                    AllocationLine(
                        amount_minor=100_000,
                        funding_wallet_id=world["unallocated_id"],
                        budget_category_id=world["food_id"],
                    ),
                ),
            )
        )
    with pytest.raises(FinancialEventError, match="wallet and category"):
        allocate.execute(
            AllocateFinancialEventRequest(
                financial_event_id=refund_id,
                expected_version=1,
                client_token="token-3",
                lines=(
                    AllocationLine(
                        amount_minor=100_000,
                        funding_wallet_id=world["rent_id"],
                        budget_category_id=world["food_id"],
                    ),
                ),
            )
        )
    with factory() as unit:
        assert unit.journal.get_by_financial_event(refund_id) is None


def test_a_refund_must_name_a_posted_and_allocated_original(
    factory: UnitOfWorkFactory,
    allocate: AllocateFinancialEvent,
    world: dict[str, int],
) -> None:
    purchase_id = _event(
        factory,
        key="gmail_transaction:1",
        amount_minor=10_000,
        payment_method=PaymentMethod.CREDIT_CARD,
        card_id=world["card_id"],
    )
    early = _event(
        factory,
        key="gmail_transaction:2",
        amount_minor=1_000,
        payment_method=PaymentMethod.CREDIT_CARD,
        card_id=world["card_id"],
        event_type=FinancialEventType.REFUND,
        refund_of_event_id=purchase_id,
    )
    unlinked = _event(
        factory,
        key="gmail_transaction:3",
        amount_minor=1_000,
        payment_method=PaymentMethod.CREDIT_CARD,
        card_id=world["card_id"],
        event_type=FinancialEventType.REFUND,
    )
    line = AllocationLine(
        amount_minor=1_000,
        funding_wallet_id=world["unallocated_id"],
        budget_category_id=world["food_id"],
    )

    with pytest.raises(FinancialEventError, match="must be posted"):
        allocate.propose_refund_lines(early)
    with pytest.raises(FinancialEventError, match="must name the event"):
        allocate.execute(
            AllocateFinancialEventRequest(
                financial_event_id=unlinked,
                expected_version=1,
                client_token="token-1",
                lines=(line,),
            )
        )


def test_a_refund_cannot_be_returned_to_a_different_card(
    factory: UnitOfWorkFactory,
    allocate: AllocateFinancialEvent,
    world: dict[str, int],
) -> None:
    """Crediting another card while releasing this card's reservation splits
    the two sides of the same refund, so it is refused rather than guessed."""
    cards = ManageCards(factory)
    other = cards.register(
        RegisterCardRequest(
            gmail_account_id=1,
            issuer_code="test_bank",
            display_name="另一張卡",
            last_four="8765",
        )
    )
    purchase_id = _event(
        factory,
        key="gmail_transaction:1",
        amount_minor=10_000,
        payment_method=PaymentMethod.CREDIT_CARD,
        card_id=world["card_id"],
    )
    allocate.execute(
        AllocateFinancialEventRequest(
            financial_event_id=purchase_id,
            expected_version=1,
            client_token="token-1",
            lines=(
                AllocationLine(
                    amount_minor=10_000,
                    funding_wallet_id=world["unallocated_id"],
                    budget_category_id=world["food_id"],
                ),
            ),
        )
    )
    elsewhere = _event(
        factory,
        key="gmail_transaction:2",
        amount_minor=1_000,
        payment_method=PaymentMethod.CREDIT_CARD,
        card_id=other.id,
        event_type=FinancialEventType.REFUND,
        refund_of_event_id=purchase_id,
    )

    with pytest.raises(FinancialEventError, match="same card"):
        allocate.propose_refund_lines(elsewhere)
    with factory() as unit:
        commitment = unit.commitments.list_for_event(purchase_id)[0]
        assert commitment.released_minor == 0


def test_a_funding_wallet_must_hold_the_events_currency(
    factory: UnitOfWorkFactory,
    allocate: AllocateFinancialEvent,
    world: dict[str, int],
) -> None:
    """The form filters the list, but a tampered POST must be refused too."""
    setup = LedgerSetupService(factory)
    usd = setup.open_account(
        OpenFinancialAccountRequest(
            institution_code="test_bank",
            display_name="美金帳戶",
            account_type=FinancialAccountType.SAVINGS,
            currency="USD",
        )
    )
    with factory() as unit:
        usd_wallet = unit.wallets.get_unallocated(usd.account.id)
        assert usd_wallet is not None
        usd_wallet_id = usd_wallet.id
    event_id = _event(
        factory,
        key="gmail_transaction:1",
        amount_minor=128_000,
        payment_method=PaymentMethod.CREDIT_CARD,
        card_id=world["card_id"],
    )

    with pytest.raises(CurrencyMismatchError, match="event's currency"):
        allocate.execute(
            AllocateFinancialEventRequest(
                financial_event_id=event_id,
                expected_version=1,
                client_token="token-1",
                lines=(
                    AllocationLine(
                        amount_minor=128_000,
                        funding_wallet_id=usd_wallet_id,
                        budget_category_id=world["food_id"],
                    ),
                ),
            )
        )
    with factory() as unit:
        assert unit.journal.get_by_financial_event(event_id) is None
        assert unit.commitments.list_for_event(event_id) == ()
        assert unit.allocations.list_for_event(event_id) == ()


def test_a_refund_must_use_the_payment_method_of_the_event_it_refunds(
    factory: UnitOfWorkFactory,
    allocate: AllocateFinancialEvent,
    balances: MoneyBalanceQueries,
    world: dict[str, int],
) -> None:
    """A card purchase refunded as a bank credit would never settle.

    The bank balance would rise, the card liability would stay where it was,
    and the card's reservation would still be released, so the two halves of
    the same refund would disagree permanently.
    """
    purchase_id = _event(
        factory,
        key="gmail_transaction:1",
        amount_minor=10_000,
        payment_method=PaymentMethod.CREDIT_CARD,
        card_id=world["card_id"],
    )
    allocate.execute(
        AllocateFinancialEventRequest(
            financial_event_id=purchase_id,
            expected_version=1,
            client_token="token-1",
            lines=(
                AllocationLine(
                    amount_minor=10_000,
                    funding_wallet_id=world["unallocated_id"],
                    budget_category_id=world["food_id"],
                ),
            ),
        )
    )
    bank_before = _balance(balances, world["bank_account_id"]).balance_minor
    mismatched = _event(
        factory,
        key="gmail_transaction:2",
        amount_minor=10_000,
        payment_method=PaymentMethod.BANK_ACCOUNT,
        card_id=world["card_id"],
        event_type=FinancialEventType.REFUND,
        refund_of_event_id=purchase_id,
    )

    with pytest.raises(FinancialEventError, match="same payment method"):
        allocate.propose_refund_lines(mismatched)
    with pytest.raises(FinancialEventError, match="same payment method"):
        allocate.execute(
            AllocateFinancialEventRequest(
                financial_event_id=mismatched,
                expected_version=1,
                client_token="token-2",
                lines=(
                    AllocationLine(
                        amount_minor=10_000,
                        funding_wallet_id=world["unallocated_id"],
                        budget_category_id=world["food_id"],
                    ),
                ),
            )
        )

    # Nothing at all was written: no entry, no allocation, no release, and
    # neither balance moved.
    assert _balance(balances, world["bank_account_id"]).balance_minor == bank_before
    assert _balance(balances, world["liability_account_id"]).balance_minor == -10_000
    with factory() as unit:
        assert unit.journal.get_by_financial_event(mismatched) is None
        assert unit.allocations.list_for_event(mismatched) == ()
        event = unit.financial_events.get(mismatched)
        assert event is not None
        assert event.status is not FinancialEventStatus.POSTED
        commitment = unit.commitments.list_for_event(purchase_id)[0]
        assert commitment.released_minor == 0
        assert commitment.outstanding_minor == 10_000

    # The same refund on the original's payment method still posts.
    matching = _event(
        factory,
        key="gmail_transaction:3",
        amount_minor=10_000,
        payment_method=PaymentMethod.CREDIT_CARD,
        card_id=world["card_id"],
        event_type=FinancialEventType.REFUND,
        refund_of_event_id=purchase_id,
    )
    allocate.execute(
        AllocateFinancialEventRequest(
            financial_event_id=matching,
            expected_version=1,
            client_token="token-3",
            lines=(
                AllocationLine(
                    amount_minor=10_000,
                    funding_wallet_id=world["unallocated_id"],
                    budget_category_id=world["food_id"],
                ),
            ),
        )
    )
    assert _balance(balances, world["liability_account_id"]).balance_minor == 0
    assert _balance(balances, world["bank_account_id"]).balance_minor == bank_before


def test_a_refund_releases_only_the_shares_it_actually_returned(
    factory: UnitOfWorkFactory,
    allocate: AllocateFinancialEvent,
    world: dict[str, int],
) -> None:
    """Releasing pro-rata across every reservation misstates both wallets."""
    purchase_id = _event(
        factory,
        key="gmail_transaction:1",
        amount_minor=100_000,
        payment_method=PaymentMethod.CREDIT_CARD,
        card_id=world["card_id"],
    )
    allocate.execute(
        AllocateFinancialEventRequest(
            financial_event_id=purchase_id,
            expected_version=1,
            client_token="token-1",
            lines=(
                AllocationLine(
                    amount_minor=70_000,
                    funding_wallet_id=world["unallocated_id"],
                    budget_category_id=world["food_id"],
                ),
                AllocationLine(
                    amount_minor=30_000,
                    funding_wallet_id=world["rent_id"],
                    budget_category_id=world["transport_id"],
                ),
            ),
        )
    )
    refund_id = _event(
        factory,
        key="gmail_transaction:2",
        amount_minor=70_000,
        payment_method=PaymentMethod.CREDIT_CARD,
        card_id=world["card_id"],
        event_type=FinancialEventType.REFUND,
        refund_of_event_id=purchase_id,
    )

    allocate.execute(
        AllocateFinancialEventRequest(
            financial_event_id=refund_id,
            expected_version=1,
            client_token="token-2",
            lines=(
                AllocationLine(
                    amount_minor=70_000,
                    funding_wallet_id=world["unallocated_id"],
                    budget_category_id=world["food_id"],
                ),
            ),
        )
    )

    with factory() as unit:
        allocations = {
            item.id: (item.funding_wallet_id, item.budget_category_id)
            for item in unit.allocations.list_for_event(purchase_id)
        }
        released = {
            allocations[item.event_allocation_id]: item.released_minor
            for item in unit.commitments.list_for_event(purchase_id)
        }
        outstanding = {
            allocations[item.event_allocation_id]: item.outstanding_minor
            for item in unit.commitments.list_for_event(purchase_id)
        }

    food = (world["unallocated_id"], world["food_id"])
    transport = (world["rent_id"], world["transport_id"])
    # Only the food share was refunded, so only its reservation is released.
    assert released[food] == 70_000
    assert released[transport] == 0
    assert outstanding[food] == 0
    assert outstanding[transport] == 30_000


def test_a_partial_refund_of_one_share_releases_only_part_of_it(
    factory: UnitOfWorkFactory,
    allocate: AllocateFinancialEvent,
    world: dict[str, int],
) -> None:
    purchase_id = _event(
        factory,
        key="gmail_transaction:1",
        amount_minor=100_000,
        payment_method=PaymentMethod.CREDIT_CARD,
        card_id=world["card_id"],
    )
    allocate.execute(
        AllocateFinancialEventRequest(
            financial_event_id=purchase_id,
            expected_version=1,
            client_token="token-1",
            lines=(
                AllocationLine(
                    amount_minor=70_000,
                    funding_wallet_id=world["unallocated_id"],
                    budget_category_id=world["food_id"],
                ),
                AllocationLine(
                    amount_minor=30_000,
                    funding_wallet_id=world["rent_id"],
                    budget_category_id=world["transport_id"],
                ),
            ),
        )
    )
    refund_id = _event(
        factory,
        key="gmail_transaction:2",
        amount_minor=20_000,
        payment_method=PaymentMethod.CREDIT_CARD,
        card_id=world["card_id"],
        event_type=FinancialEventType.REFUND,
        refund_of_event_id=purchase_id,
    )

    allocate.execute(
        AllocateFinancialEventRequest(
            financial_event_id=refund_id,
            expected_version=1,
            client_token="token-2",
            lines=(
                AllocationLine(
                    amount_minor=20_000,
                    funding_wallet_id=world["rent_id"],
                    budget_category_id=world["transport_id"],
                ),
            ),
        )
    )

    with factory() as unit:
        allocations = {
            item.id: (item.funding_wallet_id, item.budget_category_id)
            for item in unit.allocations.list_for_event(purchase_id)
        }
        released = {
            allocations[item.event_allocation_id]: item.released_minor
            for item in unit.commitments.list_for_event(purchase_id)
        }

    assert released[(world["unallocated_id"], world["food_id"])] == 0
    assert released[(world["rent_id"], world["transport_id"])] == 20_000

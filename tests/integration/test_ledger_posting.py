"""Ledger service acceptance tests over a file-backed database."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from inspect import signature

import pytest
from app.application.queries.money import MoneyBalanceQueries
from app.application.services.ledger import (
    AddWalletRequest,
    JournalService,
    LedgerSetupService,
    OpenFinancialAccountRequest,
    OpeningPositionRequest,
    RenameWalletRequest,
)
from app.domain.budget_enums import (
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
    FinancialAccountBalance,
    FinancialEventDraft,
    JournalEntry,
    LedgerAccountDraft,
    Money,
    OpeningWalletSplit,
    PostingDraft,
)
from app.domain.budget_rules import expense_ledger_account_code
from app.domain.errors import (
    ConcurrentUpdateError,
    CurrencyMismatchError,
    IdempotencyConflictError,
    InvalidReversalError,
    LedgerConfigurationError,
    OpeningPositionError,
    UnbalancedJournalError,
    WalletAssignmentError,
)
from app.domain.ports.repositories import UnitOfWork
from app.infrastructure.database.budget_queries import (
    SqlAlchemyLedgerBalanceReadRepository,
)
from app.infrastructure.database.engine import Database
from app.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork

OBSERVED_AT = datetime(2026, 8, 7, 13, 30, tzinfo=UTC)
RECORDED_AT = datetime(2026, 8, 8, 2, 0, tzinfo=UTC)

type UnitOfWorkFactory = Callable[[], UnitOfWork]


@pytest.fixture
def unit_of_work_factory(migrated_database: Database) -> UnitOfWorkFactory:
    """Return a unit-of-work factory bound to the migrated test database."""

    def factory() -> UnitOfWork:
        return SqlAlchemyUnitOfWork(migrated_database.session_factory)

    return factory


@pytest.fixture
def setup_service(unit_of_work_factory: UnitOfWorkFactory) -> LedgerSetupService:
    """Return the account and wallet setup service."""
    return LedgerSetupService(unit_of_work_factory)


@pytest.fixture
def journal(unit_of_work_factory: UnitOfWorkFactory) -> JournalService:
    """Return the journal service with a frozen recording clock."""
    return JournalService(unit_of_work_factory, clock=lambda: RECORDED_AT)


@pytest.fixture
def balances(migrated_database: Database) -> MoneyBalanceQueries:
    """Return calculated balance queries over the same database."""
    return MoneyBalanceQueries(
        SqlAlchemyLedgerBalanceReadRepository(migrated_database.session_factory)
    )


def _account_balance(
    balances: MoneyBalanceQueries, account_id: int
) -> FinancialAccountBalance:
    for item in balances.account_balances():
        if item.financial_account_id == account_id:
            return item
    raise AssertionError("account balance was not found")


def _ledger_account_id(factory: UnitOfWorkFactory, account_id: int) -> int:
    with factory() as unit:
        ledger_account = unit.ledger_accounts.get_for_financial_account(account_id)
        assert ledger_account is not None
        return ledger_account.id


def _wallet_id(factory: UnitOfWorkFactory, account_id: int, name: str) -> int:
    with factory() as unit:
        wallet = unit.wallets.get_by_name(account_id, name)
        assert wallet is not None
        return wallet.id


def _open_checking(
    setup_service: LedgerSetupService, name: str = "測試支票帳戶"
) -> int:
    result = setup_service.open_account(
        OpenFinancialAccountRequest(
            institution_code="test_bank",
            display_name=name,
            account_type=FinancialAccountType.CHECKING,
            currency="TWD",
            masked_reference="****4321",
        )
    )
    return result.account.id


def _open_card(setup_service: LedgerSetupService) -> int:
    result = setup_service.open_account(
        OpenFinancialAccountRequest(
            institution_code="test_bank",
            display_name="測試信用卡",
            account_type=FinancialAccountType.CARD_LIABILITY,
            currency="TWD",
        )
    )
    assert result.unallocated_wallet is None
    assert result.ledger_account.kind is LedgerAccountKind.LIABILITY
    return result.account.id


def test_opening_an_account_creates_its_ledger_identity_and_wallet(
    setup_service: LedgerSetupService,
) -> None:
    result = setup_service.open_account(
        OpenFinancialAccountRequest(
            institution_code="test_bank",
            display_name="測試支票帳戶",
            account_type=FinancialAccountType.CHECKING,
            currency="twd",
        )
    )

    assert result.account.currency == "TWD"
    assert result.ledger_account.kind is LedgerAccountKind.ASSET
    assert result.ledger_account.code == f"asset:test_bank:{result.account.id}"
    assert result.unallocated_wallet is not None
    assert result.unallocated_wallet.purpose is WalletPurpose.UNALLOCATED


def test_card_liability_accounts_have_no_wallets(
    setup_service: LedgerSetupService,
) -> None:
    card_id = _open_card(setup_service)

    with pytest.raises(LedgerConfigurationError, match="only asset accounts"):
        setup_service.add_wallet(
            AddWalletRequest(financial_account_id=card_id, name="不應存在")
        )


def test_account_and_wallet_setup_rejects_duplicates(
    setup_service: LedgerSetupService,
) -> None:
    account_id = _open_checking(setup_service)
    setup_service.add_wallet(
        AddWalletRequest(financial_account_id=account_id, name="房租")
    )

    with pytest.raises(LedgerConfigurationError, match="already exists"):
        _open_checking(setup_service)
    with pytest.raises(LedgerConfigurationError, match="already has a wallet"):
        setup_service.add_wallet(
            AddWalletRequest(financial_account_id=account_id, name="房租")
        )
    with pytest.raises(LedgerConfigurationError, match="created with its account"):
        setup_service.add_wallet(
            AddWalletRequest(
                financial_account_id=account_id,
                name="另一個未分配",
                purpose=WalletPurpose.UNALLOCATED,
            )
        )
    with pytest.raises(LedgerConfigurationError, match="was not found"):
        setup_service.add_wallet(
            AddWalletRequest(financial_account_id=999, name="房租")
        )


def test_wallet_rename_changes_only_the_name(
    setup_service: LedgerSetupService,
) -> None:
    account_id = _open_checking(setup_service)
    rent = setup_service.add_wallet(
        AddWalletRequest(financial_account_id=account_id, name="房租")
    )
    setup_service.add_wallet(
        AddWalletRequest(financial_account_id=account_id, name="水電")
    )

    renamed = setup_service.rename_wallet(
        RenameWalletRequest(
            wallet_id=rent.id, name="房租2026", expected_version=rent.version
        )
    )

    assert renamed.id == rent.id
    assert renamed.name == "房租2026"
    assert renamed.purpose is rent.purpose
    assert renamed.version == rent.version + 1


def test_wallet_rename_rejects_duplicates_stale_versions_and_missing_ids(
    setup_service: LedgerSetupService,
) -> None:
    account_id = _open_checking(setup_service)
    rent = setup_service.add_wallet(
        AddWalletRequest(financial_account_id=account_id, name="房租")
    )
    utilities = setup_service.add_wallet(
        AddWalletRequest(financial_account_id=account_id, name="水電")
    )

    with pytest.raises(LedgerConfigurationError, match="already has a wallet"):
        setup_service.rename_wallet(
            RenameWalletRequest(
                wallet_id=rent.id, name="水電", expected_version=rent.version
            )
        )
    with pytest.raises(ConcurrentUpdateError):
        setup_service.rename_wallet(
            RenameWalletRequest(
                wallet_id=utilities.id,
                name="水電費",
                expected_version=utilities.version + 1,
            )
        )
    with pytest.raises(LedgerConfigurationError, match="was not found"):
        setup_service.rename_wallet(
            RenameWalletRequest(wallet_id=999, name="不存在", expected_version=1)
        )


def test_wallet_sum_equals_account_balance_after_opening_and_internal_move(
    setup_service: LedgerSetupService,
    journal: JournalService,
    balances: MoneyBalanceQueries,
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    account_id = _open_checking(setup_service)
    rent = setup_service.add_wallet(
        AddWalletRequest(financial_account_id=account_id, name="房租")
    )

    journal.record_opening_position(
        OpeningPositionRequest(
            financial_account_id=account_id,
            balance_minor=500000,
            observed_at=OBSERVED_AT,
            splits=(
                OpeningWalletSplit(wallet_name="尚未分配", amount_minor=300000),
                OpeningWalletSplit(wallet_name="房租", amount_minor=200000),
            ),
        )
    )
    opened = _account_balance(balances, account_id)
    assert opened.balance_minor == 500000
    assert sum(wallet.balance_minor for wallet in opened.wallets) == 500000

    ledger_account_id = _ledger_account_id(unit_of_work_factory, account_id)
    unallocated_id = _wallet_id(unit_of_work_factory, account_id, "尚未分配")
    journal.post_balanced(
        entry_type=JournalEntryType.INTERNAL_TRANSFER,
        effective_at=OBSERVED_AT,
        idempotency_key="fixture",
        description="Reclassify into rent",
        postings=(
            PostingDraft(
                ledger_account_id=ledger_account_id,
                amount=Money(-50000, "TWD"),
                wallet_id=unallocated_id,
            ),
            PostingDraft(
                ledger_account_id=ledger_account_id,
                amount=Money(50000, "TWD"),
                wallet_id=rent.id,
            ),
        ),
    )

    moved = _account_balance(balances, account_id)
    wallet_totals = {wallet.name: wallet.balance_minor for wallet in moved.wallets}
    assert moved.balance_minor == 500000
    assert sum(wallet.balance_minor for wallet in moved.wallets) == 500000
    assert wallet_totals == {"尚未分配": 250000, "房租": 250000}


def test_liability_opening_credits_the_liability_without_wallets(
    setup_service: LedgerSetupService,
    journal: JournalService,
    balances: MoneyBalanceQueries,
) -> None:
    card_id = _open_card(setup_service)

    entry = journal.record_opening_position(
        OpeningPositionRequest(
            financial_account_id=card_id,
            balance_minor=120000,
            observed_at=OBSERVED_AT,
        )
    )

    balance = _account_balance(balances, card_id)
    assert entry.entry_type is JournalEntryType.OPENING_BALANCE
    assert balance.balance_minor == -120000
    assert balance.wallets == ()
    assert sorted(posting.amount_minor for posting in entry.postings) == [
        -120000,
        120000,
    ]


def test_retrying_the_same_idempotency_key_returns_the_same_entry(
    setup_service: LedgerSetupService,
    journal: JournalService,
    balances: MoneyBalanceQueries,
) -> None:
    account_id = _open_checking(setup_service)
    request = OpeningPositionRequest(
        financial_account_id=account_id,
        balance_minor=500000,
        observed_at=OBSERVED_AT,
        splits=(OpeningWalletSplit(wallet_name="尚未分配", amount_minor=500000),),
    )

    first = journal.record_opening_position(request)
    second = journal.record_opening_position(request)

    assert first.id == second.id
    assert first.recorded_at == RECORDED_AT
    assert _account_balance(balances, account_id).balance_minor == 500000


def test_reversal_creates_opposite_postings_exactly_once(
    setup_service: LedgerSetupService,
    journal: JournalService,
    balances: MoneyBalanceQueries,
) -> None:
    account_id = _open_checking(setup_service)
    original = journal.record_opening_position(
        OpeningPositionRequest(
            financial_account_id=account_id,
            balance_minor=500000,
            observed_at=OBSERVED_AT,
            splits=(OpeningWalletSplit(wallet_name="尚未分配", amount_minor=500000),),
        )
    )

    reversal = journal.reverse(original.id)
    repeated = journal.reverse(original.id)

    assert reversal.id == repeated.id
    assert reversal.reversal_of_id == original.id
    assert reversal.entry_type is JournalEntryType.REVERSAL
    assert reversal.effective_at == original.effective_at
    assert sorted(posting.amount_minor for posting in reversal.postings) == sorted(
        -posting.amount_minor for posting in original.postings
    )
    balance = _account_balance(balances, account_id)
    assert balance.balance_minor == 0
    assert sum(wallet.balance_minor for wallet in balance.wallets) == 0


def test_a_reversal_cannot_be_reversed_and_missing_entries_are_rejected(
    setup_service: LedgerSetupService,
    journal: JournalService,
) -> None:
    account_id = _open_checking(setup_service)
    original = journal.record_opening_position(
        OpeningPositionRequest(
            financial_account_id=account_id,
            balance_minor=500000,
            observed_at=OBSERVED_AT,
            splits=(OpeningWalletSplit(wallet_name="尚未分配", amount_minor=500000),),
        )
    )
    reversal = journal.reverse(original.id)

    with pytest.raises(LedgerConfigurationError, match="cannot itself be reversed"):
        journal.reverse(reversal.id)
    with pytest.raises(LedgerConfigurationError, match="was not found"):
        journal.reverse(999)


def test_two_currency_entry_is_rejected_before_anything_is_written(
    setup_service: LedgerSetupService,
    journal: JournalService,
    balances: MoneyBalanceQueries,
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    account_id = _open_checking(setup_service)
    ledger_account_id = _ledger_account_id(unit_of_work_factory, account_id)
    wallet_id = _wallet_id(unit_of_work_factory, account_id, "尚未分配")

    with pytest.raises(UnbalancedJournalError, match="JPY"):
        journal.post_balanced(
            entry_type=JournalEntryType.INTERNAL_TRANSFER,
            effective_at=OBSERVED_AT,
            idempotency_key="cross-currency",
            description="",
            postings=(
                PostingDraft(
                    ledger_account_id=ledger_account_id,
                    amount=Money(1000, "TWD"),
                    wallet_id=wallet_id,
                ),
                PostingDraft(
                    ledger_account_id=ledger_account_id,
                    amount=Money(-1000, "TWD"),
                    wallet_id=wallet_id,
                ),
                PostingDraft(
                    ledger_account_id=ledger_account_id,
                    amount=Money(500, "JPY"),
                    wallet_id=wallet_id,
                ),
            ),
        )

    assert _account_balance(balances, account_id).balance_minor == 0
    with unit_of_work_factory() as unit:
        assert unit.journal.get_by_idempotency_key("cross-currency") is None


def test_mismatched_wallet_account_or_currency_is_rejected(
    setup_service: LedgerSetupService,
    journal: JournalService,
    balances: MoneyBalanceQueries,
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    first_id = _open_checking(setup_service, name="第一個測試帳戶")
    second = setup_service.open_account(
        OpenFinancialAccountRequest(
            institution_code="other_bank",
            display_name="第二個測試帳戶",
            account_type=FinancialAccountType.SAVINGS,
            currency="TWD",
        )
    )
    assert second.unallocated_wallet is not None
    ledger_account_id = _ledger_account_id(unit_of_work_factory, first_id)
    wallet_id = _wallet_id(unit_of_work_factory, first_id, "尚未分配")

    def _post(postings: tuple[PostingDraft, ...], key: str) -> None:
        journal.post_balanced(
            entry_type=JournalEntryType.INTERNAL_TRANSFER,
            effective_at=OBSERVED_AT,
            idempotency_key=key,
            description="",
            postings=postings,
        )

    def _balanced(
        first: PostingDraft,
    ) -> tuple[PostingDraft, ...]:
        return (
            first,
            PostingDraft(
                ledger_account_id=ledger_account_id,
                amount=first.amount.negated(),
                wallet_id=wallet_id,
            ),
        )

    with pytest.raises(WalletAssignmentError, match="require a wallet"):
        _post(
            _balanced(
                PostingDraft(
                    ledger_account_id=ledger_account_id, amount=Money(1000, "TWD")
                )
            ),
            "missing-wallet",
        )
    with pytest.raises(WalletAssignmentError, match="different financial account"):
        _post(
            _balanced(
                PostingDraft(
                    ledger_account_id=ledger_account_id,
                    amount=Money(1000, "TWD"),
                    wallet_id=second.unallocated_wallet.id,
                )
            ),
            "foreign-wallet",
        )
    with pytest.raises(WalletAssignmentError, match="wallet was not found"):
        _post(
            _balanced(
                PostingDraft(
                    ledger_account_id=ledger_account_id,
                    amount=Money(1000, "TWD"),
                    wallet_id=999,
                )
            ),
            "unknown-wallet",
        )
    with pytest.raises(CurrencyMismatchError):
        _post(
            (
                PostingDraft(
                    ledger_account_id=ledger_account_id,
                    amount=Money(1000, "JPY"),
                    wallet_id=wallet_id,
                ),
                PostingDraft(
                    ledger_account_id=ledger_account_id,
                    amount=Money(-1000, "JPY"),
                    wallet_id=wallet_id,
                ),
            ),
            "wrong-currency",
        )
    with pytest.raises(LedgerConfigurationError, match="ledger account was not found"):
        _post(
            _balanced(PostingDraft(ledger_account_id=999, amount=Money(1000, "TWD"))),
            "unknown-ledger-account",
        )

    assert _account_balance(balances, first_id).balance_minor == 0


def test_a_wallet_cannot_be_attached_to_a_liability_posting(
    setup_service: LedgerSetupService,
    journal: JournalService,
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    account_id = _open_checking(setup_service)
    card_id = _open_card(setup_service)
    wallet_id = _wallet_id(unit_of_work_factory, account_id, "尚未分配")
    card_ledger_id = _ledger_account_id(unit_of_work_factory, card_id)

    with pytest.raises(WalletAssignmentError, match="only wallet-managed"):
        journal.post_balanced(
            entry_type=JournalEntryType.ADJUSTMENT,
            effective_at=OBSERVED_AT,
            idempotency_key="liability-wallet",
            description="",
            postings=(
                PostingDraft(
                    ledger_account_id=card_ledger_id,
                    amount=Money(1000, "TWD"),
                    wallet_id=wallet_id,
                ),
                PostingDraft(
                    ledger_account_id=card_ledger_id, amount=Money(-1000, "TWD")
                ),
            ),
        )


def test_an_invalid_opening_split_rolls_back_every_write(
    setup_service: LedgerSetupService,
    journal: JournalService,
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    account_id = _open_checking(setup_service)

    with pytest.raises(LedgerConfigurationError, match="was not found in this account"):
        journal.record_opening_position(
            OpeningPositionRequest(
                financial_account_id=account_id,
                balance_minor=500000,
                observed_at=OBSERVED_AT,
                splits=(
                    OpeningWalletSplit(wallet_name="不存在的錢包", amount_minor=500000),
                ),
            )
        )

    with unit_of_work_factory() as unit:
        assert unit.ledger_accounts.get_by_code("equity:opening:TWD") is None
        assert (
            unit.journal.get_by_idempotency_key(
                f"opening_balance:financial_account:{account_id}"
            )
            is None
        )


def test_opening_split_rules_are_explicit(
    setup_service: LedgerSetupService,
    journal: JournalService,
) -> None:
    account_id = _open_checking(setup_service)
    card_id = _open_card(setup_service)

    def _open(
        *,
        financial_account_id: int,
        balance_minor: int,
        splits: tuple[OpeningWalletSplit, ...] = (),
    ) -> None:
        journal.record_opening_position(
            OpeningPositionRequest(
                financial_account_id=financial_account_id,
                balance_minor=balance_minor,
                observed_at=OBSERVED_AT,
                splits=splits,
            )
        )

    with pytest.raises(OpeningPositionError, match="requires an explicit wallet split"):
        _open(financial_account_id=account_id, balance_minor=500000)
    with pytest.raises(OpeningPositionError, match="must equal the account balance"):
        _open(
            financial_account_id=account_id,
            balance_minor=500000,
            splits=(OpeningWalletSplit(wallet_name="尚未分配", amount_minor=400000),),
        )
    with pytest.raises(OpeningPositionError, match="only once"):
        _open(
            financial_account_id=account_id,
            balance_minor=500000,
            splits=(
                OpeningWalletSplit(wallet_name="尚未分配", amount_minor=400000),
                OpeningWalletSplit(wallet_name="尚未分配", amount_minor=100000),
            ),
        )
    with pytest.raises(OpeningPositionError, match="only wallet-managed"):
        _open(
            financial_account_id=card_id,
            balance_minor=120000,
            splits=(OpeningWalletSplit(wallet_name="尚未分配", amount_minor=120000),),
        )
    with pytest.raises(LedgerConfigurationError, match="account was not found"):
        _open(financial_account_id=999, balance_minor=1000)


def test_opening_position_preserves_negative_and_zero_wallets(
    setup_service: LedgerSetupService,
    journal: JournalService,
    balances: MoneyBalanceQueries,
) -> None:
    account_id = _open_checking(setup_service)
    setup_service.add_wallet(
        AddWalletRequest(financial_account_id=account_id, name="預算")
    )
    setup_service.add_wallet(
        AddWalletRequest(financial_account_id=account_id, name="已透支")
    )
    setup_service.add_wallet(
        AddWalletRequest(financial_account_id=account_id, name="空錢包")
    )

    entry = journal.record_opening_position(
        OpeningPositionRequest(
            financial_account_id=account_id,
            balance_minor=500_000,
            observed_at=OBSERVED_AT,
            splits=(
                OpeningWalletSplit(wallet_name="預算", amount_minor=700_000),
                OpeningWalletSplit(wallet_name="已透支", amount_minor=-200_000),
                OpeningWalletSplit(wallet_name="空錢包", amount_minor=0),
            ),
        )
    )

    opened = _account_balance(balances, account_id)
    wallet_totals = {wallet.name: wallet.balance_minor for wallet in opened.wallets}
    assert opened.balance_minor == 500_000
    assert sum(wallet_totals.values()) == opened.balance_minor
    assert wallet_totals["預算"] == 700_000
    assert wallet_totals["已透支"] == -200_000
    assert wallet_totals["空錢包"] == 0
    assert len(entry.postings) == 3
    assert all(posting.amount_minor != 0 for posting in entry.postings)

    replayed = journal.record_opening_position(
        OpeningPositionRequest(
            financial_account_id=account_id,
            balance_minor=500_000,
            observed_at=OBSERVED_AT,
            splits=(
                OpeningWalletSplit(wallet_name="預算", amount_minor=700_000),
                OpeningWalletSplit(wallet_name="已透支", amount_minor=-200_000),
                OpeningWalletSplit(wallet_name="空錢包", amount_minor=0),
            ),
        )
    )
    assert replayed.id == entry.id

    reversal = journal.reverse(entry.id, effective_at=OBSERVED_AT)
    reversed_balance = _account_balance(balances, account_id)
    assert reversed_balance.balance_minor == 0
    assert all(wallet.balance_minor == 0 for wallet in reversed_balance.wallets)
    assert len(reversal.postings) == 3


def test_a_zero_opening_share_still_names_an_existing_unique_wallet(
    setup_service: LedgerSetupService,
    journal: JournalService,
) -> None:
    account_id = _open_checking(setup_service)

    with pytest.raises(LedgerConfigurationError, match="was not found"):
        journal.record_opening_position(
            OpeningPositionRequest(
                financial_account_id=account_id,
                balance_minor=500_000,
                observed_at=OBSERVED_AT,
                splits=(
                    OpeningWalletSplit("尚未分配", 500_000),
                    OpeningWalletSplit("不存在", 0),
                ),
            )
        )

    with pytest.raises(OpeningPositionError, match="only once"):
        journal.record_opening_position(
            OpeningPositionRequest(
                financial_account_id=account_id,
                balance_minor=500_000,
                observed_at=OBSERVED_AT,
                splits=(
                    OpeningWalletSplit("尚未分配", 500_000),
                    OpeningWalletSplit("尚未分配", 0),
                ),
            )
        )


def test_ledger_timestamps_must_be_timezone_aware(
    setup_service: LedgerSetupService,
    journal: JournalService,
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    account_id = _open_checking(setup_service)
    ledger_account_id = _ledger_account_id(unit_of_work_factory, account_id)
    wallet_id = _wallet_id(unit_of_work_factory, account_id, "尚未分配")

    with pytest.raises(ValueError, match="timezone-aware"):
        journal.post_balanced(
            entry_type=JournalEntryType.INTERNAL_TRANSFER,
            effective_at=datetime(2026, 8, 7, 13, 30),
            idempotency_key="naive-timestamp",
            description="",
            postings=(
                PostingDraft(
                    ledger_account_id=ledger_account_id,
                    amount=Money(1000, "TWD"),
                    wallet_id=wallet_id,
                ),
                PostingDraft(
                    ledger_account_id=ledger_account_id,
                    amount=Money(-1000, "TWD"),
                    wallet_id=wallet_id,
                ),
            ),
        )


def test_balances_are_empty_before_any_account_exists(
    balances: MoneyBalanceQueries,
) -> None:
    assert balances.account_balances() == ()


def test_postings_to_an_inactive_wallet_are_rejected(
    setup_service: LedgerSetupService,
    journal: JournalService,
    migrated_database: Database,
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    account_id = _open_checking(setup_service)
    ledger_account_id = _ledger_account_id(unit_of_work_factory, account_id)
    rent = setup_service.add_wallet(
        AddWalletRequest(financial_account_id=account_id, name="房租")
    )
    unallocated_id = _wallet_id(unit_of_work_factory, account_id, "尚未分配")
    with migrated_database.engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE wallets SET is_active = 0 WHERE id = ?", (rent.id,)
        )

    with pytest.raises(WalletAssignmentError, match="wallet is inactive"):
        journal.post_balanced(
            entry_type=JournalEntryType.INTERNAL_TRANSFER,
            effective_at=OBSERVED_AT,
            idempotency_key="inactive-wallet",
            description="",
            postings=(
                PostingDraft(
                    ledger_account_id=ledger_account_id,
                    amount=Money(1000, "TWD"),
                    wallet_id=rent.id,
                ),
                PostingDraft(
                    ledger_account_id=ledger_account_id,
                    amount=Money(-1000, "TWD"),
                    wallet_id=unallocated_id,
                ),
            ),
        )


def test_a_colliding_idempotency_key_never_reports_a_false_reversal(
    setup_service: LedgerSetupService,
    journal: JournalService,
    balances: MoneyBalanceQueries,
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    account_id = _open_checking(setup_service)
    ledger_account_id = _ledger_account_id(unit_of_work_factory, account_id)
    wallet_id = _wallet_id(unit_of_work_factory, account_id, "尚未分配")
    original = journal.record_opening_position(
        OpeningPositionRequest(
            financial_account_id=account_id,
            balance_minor=500000,
            observed_at=OBSERVED_AT,
            splits=(OpeningWalletSplit(wallet_name="尚未分配", amount_minor=500000),),
        )
    )
    unrelated = journal.post_balanced(
        entry_type=JournalEntryType.ADJUSTMENT,
        effective_at=OBSERVED_AT,
        idempotency_key=f"reversal:journal_entry:{original.id}",
        description="不相關的調整",
        postings=(
            PostingDraft(
                ledger_account_id=ledger_account_id,
                amount=Money(1000, "TWD"),
                wallet_id=wallet_id,
            ),
            PostingDraft(
                ledger_account_id=ledger_account_id,
                amount=Money(-1000, "TWD"),
                wallet_id=wallet_id,
            ),
        ),
    )

    with pytest.raises(IdempotencyConflictError, match="different journal entry"):
        journal.reverse(original.id)

    with unit_of_work_factory() as unit:
        assert unit.journal.get_reversal_of(original.id) is None
        assert unrelated.reversal_of_id is None
    assert _account_balance(balances, account_id).balance_minor == 500000


def test_reusing_one_key_for_different_work_is_rejected(
    setup_service: LedgerSetupService,
    journal: JournalService,
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    account_id = _open_checking(setup_service)
    ledger_account_id = _ledger_account_id(unit_of_work_factory, account_id)
    wallet_id = _wallet_id(unit_of_work_factory, account_id, "尚未分配")
    rent = setup_service.add_wallet(
        AddWalletRequest(financial_account_id=account_id, name="房租")
    )

    def _move(amount_minor: int, wallet: int) -> tuple[PostingDraft, ...]:
        return (
            PostingDraft(
                ledger_account_id=ledger_account_id,
                amount=Money(-amount_minor, "TWD"),
                wallet_id=wallet_id,
            ),
            PostingDraft(
                ledger_account_id=ledger_account_id,
                amount=Money(amount_minor, "TWD"),
                wallet_id=wallet,
            ),
        )

    def _post(
        postings: tuple[PostingDraft, ...],
        *,
        entry_type: JournalEntryType = JournalEntryType.INTERNAL_TRANSFER,
        effective_at: datetime = OBSERVED_AT,
        description: str = "第一次",
    ) -> JournalEntry:
        return journal.post_balanced(
            entry_type=entry_type,
            effective_at=effective_at,
            idempotency_key="shared-key",
            description=description,
            postings=postings,
        )

    first = _post(_move(50000, rent.id))

    assert _post(_move(50000, rent.id)).id == first.id
    assert _post(tuple(reversed(_move(50000, rent.id)))).id == first.id
    assert _post(_move(50000, rent.id), description="不同說明").id == first.id
    with pytest.raises(IdempotencyConflictError, match="different journal entry"):
        _post(_move(60000, rent.id))
    with pytest.raises(IdempotencyConflictError, match="different journal entry"):
        _post(_move(50000, wallet_id))
    with pytest.raises(IdempotencyConflictError, match="different journal entry"):
        _post(_move(50000, rent.id), entry_type=JournalEntryType.ADJUSTMENT)
    with pytest.raises(IdempotencyConflictError, match="different journal entry"):
        _post(
            _move(50000, rent.id),
            effective_at=OBSERVED_AT + timedelta(days=1),
        )

    with unit_of_work_factory() as unit:
        stored = unit.journal.get(first.id)
        assert stored is not None
        assert stored.entry_type is JournalEntryType.INTERNAL_TRANSFER
        assert stored.description == "第一次"
        assert stored.effective_at == OBSERVED_AT


def test_only_reverse_may_create_a_reversal_entry(
    setup_service: LedgerSetupService,
    journal: JournalService,
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    account_id = _open_checking(setup_service)
    ledger_account_id = _ledger_account_id(unit_of_work_factory, account_id)
    wallet_id = _wallet_id(unit_of_work_factory, account_id, "尚未分配")

    assert "reversal_of_id" not in signature(JournalService.post_balanced).parameters

    with pytest.raises(InvalidReversalError, match="must name the entry it reverses"):
        journal.post_balanced(
            entry_type=JournalEntryType.REVERSAL,
            effective_at=OBSERVED_AT,
            idempotency_key="claims-to-be-a-reversal",
            description="",
            postings=(
                PostingDraft(
                    ledger_account_id=ledger_account_id,
                    amount=Money(1000, "TWD"),
                    wallet_id=wallet_id,
                ),
                PostingDraft(
                    ledger_account_id=ledger_account_id,
                    amount=Money(-1000, "TWD"),
                    wallet_id=wallet_id,
                ),
            ),
        )

    with unit_of_work_factory() as unit:
        assert unit.journal.get_by_idempotency_key("claims-to-be-a-reversal") is None


def _category_ledger(
    factory: UnitOfWorkFactory, *, name: str = "生活", currency: str = "TWD"
) -> tuple[int, int]:
    """Return one (category_id, expense ledger account id) pair."""
    with factory() as unit:
        category = unit.budget_categories.add(BudgetCategoryDraft(name=name))
        ledger = unit.ledger_accounts.add(
            LedgerAccountDraft(
                code=expense_ledger_account_code(category.id, currency),
                name=name,
                kind=LedgerAccountKind.EXPENSE,
                currency=currency,
                budget_category_id=category.id,
            )
        )
        unit.commit()
        return category.id, ledger.id


def _staged_event(factory: UnitOfWorkFactory, *, key: str) -> int:
    with factory() as unit:
        event = unit.financial_events.add(
            FinancialEventDraft(
                origin=FinancialEventOrigin.GMAIL_TRANSACTION,
                event_type=FinancialEventType.PURCHASE,
                payment_method=PaymentMethod.CREDIT_CARD,
                recognition_basis=RecognitionBasis.GMAIL_NOTIFICATION,
                occurred_at=OBSERVED_AT,
                amount_minor=1000,
                currency="TWD",
                idempotency_key=key,
            )
        )
        unit.commit()
        return event.id


def test_a_staged_entry_disappears_when_the_caller_does_not_commit(
    setup_service: LedgerSetupService,
    journal: JournalService,
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """The caller owns the commit, so an abandoned transaction leaves nothing."""
    account_id = _open_checking(setup_service)
    ledger_account_id = _ledger_account_id(unit_of_work_factory, account_id)
    wallet_id = _wallet_id(unit_of_work_factory, account_id, "尚未分配")
    category_id, expense_id = _category_ledger(unit_of_work_factory)
    event_id = _staged_event(unit_of_work_factory, key="gmail_transaction:1")
    postings = (
        PostingDraft(
            ledger_account_id=expense_id,
            amount=Money(1000, "TWD"),
            budget_category_id=category_id,
        ),
        PostingDraft(
            ledger_account_id=ledger_account_id,
            amount=Money(-1000, "TWD"),
            wallet_id=wallet_id,
        ),
    )

    with unit_of_work_factory() as unit:
        staged = journal.stage_balanced(
            unit,
            entry_type=JournalEntryType.EXPENSE,
            effective_at=OBSERVED_AT,
            idempotency_key="allocation:financial_event:1",
            description="消費",
            postings=postings,
            financial_event_id=event_id,
        )
        assert staged.financial_event_id == event_id
        # deliberately no commit

    with unit_of_work_factory() as unit:
        assert unit.journal.get_by_financial_event(event_id) is None
        assert (
            unit.journal.get_by_idempotency_key("allocation:financial_event:1") is None
        )


def test_one_event_can_hold_only_one_primary_entry(
    setup_service: LedgerSetupService,
    journal: JournalService,
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    account_id = _open_checking(setup_service)
    ledger_account_id = _ledger_account_id(unit_of_work_factory, account_id)
    wallet_id = _wallet_id(unit_of_work_factory, account_id, "尚未分配")
    category_id, expense_id = _category_ledger(unit_of_work_factory)
    first_event = _staged_event(unit_of_work_factory, key="gmail_transaction:1")
    second_event = _staged_event(unit_of_work_factory, key="gmail_transaction:2")

    def _postings(amount: int) -> tuple[PostingDraft, ...]:
        return (
            PostingDraft(
                ledger_account_id=expense_id,
                amount=Money(amount, "TWD"),
                budget_category_id=category_id,
            ),
            PostingDraft(
                ledger_account_id=ledger_account_id,
                amount=Money(-amount, "TWD"),
                wallet_id=wallet_id,
            ),
        )

    with unit_of_work_factory() as unit:
        journal.stage_balanced(
            unit,
            entry_type=JournalEntryType.EXPENSE,
            effective_at=OBSERVED_AT,
            idempotency_key="allocation:financial_event:1",
            description="消費",
            postings=_postings(1000),
            financial_event_id=first_event,
        )
        unit.commit()

    with unit_of_work_factory() as unit:
        # A different key for the same event must be refused by the service,
        # not only by the database index.
        with pytest.raises(LedgerConfigurationError, match="already has a journal"):
            journal.stage_balanced(
                unit,
                entry_type=JournalEntryType.EXPENSE,
                effective_at=OBSERVED_AT,
                idempotency_key="allocation:financial_event:1:again",
                description="重複",
                postings=_postings(1000),
                financial_event_id=first_event,
            )
        # The same key naming a different event is an idempotency conflict.
        with pytest.raises(IdempotencyConflictError, match="different journal entry"):
            journal.stage_balanced(
                unit,
                entry_type=JournalEntryType.EXPENSE,
                effective_at=OBSERVED_AT,
                idempotency_key="allocation:financial_event:1",
                description="消費",
                postings=_postings(1000),
                financial_event_id=second_event,
            )
        with pytest.raises(LedgerConfigurationError, match="event was not found"):
            journal.stage_balanced(
                unit,
                entry_type=JournalEntryType.EXPENSE,
                effective_at=OBSERVED_AT,
                idempotency_key="allocation:financial_event:999",
                description="消費",
                postings=_postings(1000),
                financial_event_id=999,
            )


def test_a_changed_category_is_a_different_operation(
    setup_service: LedgerSetupService,
    journal: JournalService,
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """Category belongs to journal identity, not just to reporting."""
    account_id = _open_checking(setup_service)
    ledger_account_id = _ledger_account_id(unit_of_work_factory, account_id)
    wallet_id = _wallet_id(unit_of_work_factory, account_id, "尚未分配")
    first_category, first_ledger = _category_ledger(unit_of_work_factory, name="生活")
    second_category, second_ledger = _category_ledger(unit_of_work_factory, name="交通")

    def _postings(category_id: int, ledger_id: int) -> tuple[PostingDraft, ...]:
        return (
            PostingDraft(
                ledger_account_id=ledger_id,
                amount=Money(1000, "TWD"),
                budget_category_id=category_id,
            ),
            PostingDraft(
                ledger_account_id=ledger_account_id,
                amount=Money(-1000, "TWD"),
                wallet_id=wallet_id,
            ),
        )

    journal.post_balanced(
        entry_type=JournalEntryType.ADJUSTMENT,
        effective_at=OBSERVED_AT,
        idempotency_key="shared-key",
        description="",
        postings=_postings(first_category, first_ledger),
    )

    with pytest.raises(IdempotencyConflictError, match="different journal entry"):
        journal.post_balanced(
            entry_type=JournalEntryType.ADJUSTMENT,
            effective_at=OBSERVED_AT,
            idempotency_key="shared-key",
            description="",
            postings=_postings(second_category, second_ledger),
        )


def test_a_posting_must_match_its_category_ledger(
    setup_service: LedgerSetupService,
    journal: JournalService,
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    account_id = _open_checking(setup_service)
    ledger_account_id = _ledger_account_id(unit_of_work_factory, account_id)
    wallet_id = _wallet_id(unit_of_work_factory, account_id, "尚未分配")
    category_id, expense_id = _category_ledger(unit_of_work_factory)
    other_category, _ = _category_ledger(unit_of_work_factory, name="交通")

    for wrong in (None, other_category):
        with pytest.raises(LedgerConfigurationError, match="exactly its ledger"):
            journal.post_balanced(
                entry_type=JournalEntryType.ADJUSTMENT,
                effective_at=OBSERVED_AT,
                idempotency_key=f"mismatch-{wrong}",
                description="",
                postings=(
                    PostingDraft(
                        ledger_account_id=expense_id,
                        amount=Money(1000, "TWD"),
                        budget_category_id=wrong,
                    ),
                    PostingDraft(
                        ledger_account_id=ledger_account_id,
                        amount=Money(-1000, "TWD"),
                        wallet_id=wallet_id,
                    ),
                ),
            )
    assert category_id != other_category


def test_a_category_can_never_be_attached_to_a_plain_ledger_account(
    setup_service: LedgerSetupService,
    journal: JournalService,
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """The reverse direction: a category must not land on equity or an asset.

    Only checking category ledgers would let a posting tag an equity or asset
    line with a budget category and pollute category totals.
    """
    account_id = _open_checking(setup_service)
    ledger_account_id = _ledger_account_id(unit_of_work_factory, account_id)
    wallet_id = _wallet_id(unit_of_work_factory, account_id, "尚未分配")
    category_id, _ = _category_ledger(unit_of_work_factory)
    with unit_of_work_factory() as unit:
        equity_id = unit.ledger_accounts.add(
            LedgerAccountDraft(
                code="equity:opening:TWD",
                name="Opening balance equity (TWD)",
                kind=LedgerAccountKind.EQUITY,
                currency="TWD",
            )
        ).id
        unit.commit()

    for ledger_id, wallet in ((equity_id, None), (ledger_account_id, wallet_id)):
        with pytest.raises(LedgerConfigurationError, match="exactly its ledger"):
            journal.post_balanced(
                entry_type=JournalEntryType.ADJUSTMENT,
                effective_at=OBSERVED_AT,
                idempotency_key=f"stray-category-{ledger_id}",
                description="",
                postings=(
                    PostingDraft(
                        ledger_account_id=ledger_id,
                        amount=Money(1000, "TWD"),
                        wallet_id=wallet,
                        budget_category_id=category_id,
                    ),
                    PostingDraft(
                        ledger_account_id=ledger_account_id,
                        amount=Money(-1000, "TWD"),
                        wallet_id=wallet_id,
                    ),
                ),
            )

    with unit_of_work_factory() as unit:
        assert (
            unit.journal.get_by_idempotency_key(f"stray-category-{equity_id}") is None
        )


def test_a_categorized_reversal_preserves_every_category(
    setup_service: LedgerSetupService,
    journal: JournalService,
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    """A reversal that dropped the category would leave the budget unbalanced."""
    account_id = _open_checking(setup_service)
    ledger_account_id = _ledger_account_id(unit_of_work_factory, account_id)
    wallet_id = _wallet_id(unit_of_work_factory, account_id, "尚未分配")
    category_id, expense_id = _category_ledger(unit_of_work_factory)

    original = journal.post_balanced(
        entry_type=JournalEntryType.ADJUSTMENT,
        effective_at=OBSERVED_AT,
        idempotency_key="categorized",
        description="分類支出",
        postings=(
            PostingDraft(
                ledger_account_id=expense_id,
                amount=Money(1000, "TWD"),
                budget_category_id=category_id,
            ),
            PostingDraft(
                ledger_account_id=ledger_account_id,
                amount=Money(-1000, "TWD"),
                wallet_id=wallet_id,
            ),
        ),
    )

    reversal = journal.reverse(original.id)

    by_ledger = {posting.ledger_account_id: posting for posting in reversal.postings}
    assert by_ledger[expense_id].budget_category_id == category_id
    assert by_ledger[expense_id].amount_minor == -1000
    assert by_ledger[ledger_account_id].budget_category_id is None
    # Category totals cancel exactly, which is what makes refunds net to zero.
    assert (
        sum(
            posting.amount_minor
            for entry in (original, reversal)
            for posting in entry.postings
            if posting.budget_category_id == category_id
        )
        == 0
    )

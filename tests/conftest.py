"""Shared file-backed SQLite fixtures, plus the shared B7 world."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from app.application.queries.spending import SpendingQueries
from app.application.services.bank_movements import RecordBankMovement
from app.application.services.cards import (
    ConfigureCardRequest,
    ManageCards,
    RegisterCardRequest,
)
from app.application.services.event_allocation import (
    AllocateFinancialEvent,
    RedoAllocation,
)
from app.application.services.financial_event_intake import FinancialEventIntake
from app.application.services.ledger import (
    AddWalletRequest,
    JournalService,
    LedgerSetupService,
    OpenFinancialAccountRequest,
    OpeningPositionRequest,
)
from app.application.services.manual_money import (
    CreateManualExpense,
    ReverseFinancialEvent,
)
from app.application.services.reconciliation import (
    AdjustReconciliation,
    RecordBalanceSnapshot,
)
from app.application.services.source_review import ReviewSourceLinks
from app.domain.budget_enums import CardHolderRole, FinancialAccountType, WalletPurpose
from app.domain.budget_models import BudgetCategoryDraft, OpeningWalletSplit
from app.domain.ports.repositories import UnitOfWork
from app.infrastructure.config.settings import AppSettings
from app.infrastructure.database.engine import Database, create_database
from app.infrastructure.database.migrations import upgrade_database
from app.infrastructure.database.spending_queries import (
    SqlAlchemySpendingReadRepository,
)
from app.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork

from tests.b7_support import B7_NOW, B7Services, B7World, UnitOfWorkFactory


@pytest.fixture(autouse=True)
def isolate_listener_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests independent from an operator's trusted-LAN config.toml."""
    monkeypatch.setenv("SCRIBE_HOST", "127.0.0.1")
    monkeypatch.setenv("SCRIBE_LAN_ACCESS_ENABLED", "false")
    monkeypatch.setenv("SCRIBE_LAN_TRUSTED_HOSTS", "[]")


@pytest.fixture
def database_settings(tmp_path: Path) -> AppSettings:
    """Return settings isolated to a test-owned local directory."""
    return AppSettings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data/app.sqlite3",
        log_dir=tmp_path / "data/logs",
        external_access_enabled=False,
        external_hostname=None,
        external_confirmation_enabled=False,
        cloudflare_access_team_domain=None,
        cloudflare_access_audience=None,
        cloudflare_access_allowed_email=None,
    )


@pytest.fixture
def migrated_database(database_settings: AppSettings) -> Iterator[Database]:
    """Create a file-backed database at the current migration head."""
    status = upgrade_database(database_settings)
    assert status.is_current
    database = create_database(database_settings)
    try:
        yield database
    finally:
        database.engine.dispose()


@pytest.fixture
def b7_factory(migrated_database: Database) -> UnitOfWorkFactory:
    """Return a unit-of-work factory bound to the migrated test database."""

    def build() -> UnitOfWork:
        return SqlAlchemyUnitOfWork(migrated_database.session_factory)

    return build


@pytest.fixture
def b7(migrated_database: Database, b7_factory: UnitOfWorkFactory) -> B7Services:
    """Compose every B7 service over one database, exactly as bootstrap does."""
    allocator = AllocateFinancialEvent(b7_factory, JournalService(b7_factory))
    intake = FinancialEventIntake(b7_factory)
    return B7Services(
        factory=b7_factory,
        allocate=allocator,
        redo=RedoAllocation(b7_factory, JournalService(b7_factory)),
        expenses=CreateManualExpense(b7_factory, allocator),
        reversal=ReverseFinancialEvent(b7_factory, JournalService(b7_factory)),
        movements=RecordBankMovement(b7_factory, JournalService(b7_factory), allocator),
        snapshots=RecordBalanceSnapshot(b7_factory),
        reconciliation=AdjustReconciliation(b7_factory, JournalService(b7_factory)),
        intake=intake,
        review=ReviewSourceLinks(b7_factory, intake),
        spending=SpendingQueries(
            SqlAlchemySpendingReadRepository(migrated_database.session_factory),
            local_timezone="Asia/Taipei",
        ),
    )


@pytest.fixture
def b7_world(b7_factory: UnitOfWorkFactory) -> B7World:
    """Open a bank account, a cash account, a card liability, and a card."""
    setup = LedgerSetupService(b7_factory)
    cards = ManageCards(b7_factory)
    bank = setup.open_account(
        OpenFinancialAccountRequest(
            institution_code="test_bank",
            display_name="測試支票帳戶",
            account_type=FinancialAccountType.CHECKING,
            currency="TWD",
        )
    )
    cash = setup.open_account(
        OpenFinancialAccountRequest(
            institution_code="wallet",
            display_name="皮夾",
            account_type=FinancialAccountType.CASH,
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
    food_wallet = setup.add_wallet(
        AddWalletRequest(financial_account_id=bank.account.id, name="飲食")
    )
    clearing_wallet = setup.add_wallet(
        AddWalletRequest(
            financial_account_id=bank.account.id,
            name="帳單暫存",
            purpose=WalletPurpose.CLEARING_HUB,
        )
    )
    journal = JournalService(b7_factory)
    journal.record_opening_position(
        OpeningPositionRequest(
            financial_account_id=bank.account.id,
            balance_minor=1_000_000,
            observed_at=B7_NOW,
            splits=(
                OpeningWalletSplit(wallet_name="尚未分配", amount_minor=700_000),
                OpeningWalletSplit(wallet_name="飲食", amount_minor=300_000),
                OpeningWalletSplit(wallet_name="帳單暫存", amount_minor=0),
            ),
        )
    )
    journal.record_opening_position(
        OpeningPositionRequest(
            financial_account_id=cash.account.id,
            balance_minor=50_000,
            observed_at=B7_NOW,
            splits=(OpeningWalletSplit(wallet_name="尚未分配", amount_minor=50_000),),
        )
    )
    with b7_factory() as unit:
        gmail_account = unit.accounts.get_or_create("user@example.test")
        bank_unallocated = unit.wallets.get_unallocated(bank.account.id)
        cash_unallocated = unit.wallets.get_unallocated(cash.account.id)
        assert bank_unallocated is not None
        assert cash_unallocated is not None
        food = unit.budget_categories.add(BudgetCategoryDraft(name="飲食"))
        transport = unit.budget_categories.add(BudgetCategoryDraft(name="交通"))
        unit.commit()
        gmail_account_id = gmail_account.id
        bank_unallocated_id = bank_unallocated.id
        cash_wallet_id = cash_unallocated.id
        food_id, transport_id = food.id, transport.id

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
            default_funding_wallet_id=bank_unallocated_id,
        )
    )
    return B7World(
        bank_account_id=bank.account.id,
        bank_unallocated_id=bank_unallocated_id,
        bank_food_wallet_id=food_wallet.id,
        bank_clearing_wallet_id=clearing_wallet.id,
        cash_account_id=cash.account.id,
        cash_wallet_id=cash_wallet_id,
        liability_account_id=liability.account.id,
        card_id=card.id,
        food_id=food_id,
        transport_id=transport_id,
        gmail_account_id=gmail_account_id,
    )

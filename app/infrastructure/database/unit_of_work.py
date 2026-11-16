"""SQLAlchemy transaction boundary implementation."""

from __future__ import annotations

from types import TracebackType

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.domain.errors import DatabaseError
from app.domain.ports.budget_repositories import (
    BalanceSnapshotRepository,
    BudgetCategoryRepository,
    BudgetLedgerRepository,
    BudgetPeriodRepository,
    BudgetTargetRepository,
    CardImportAssignmentRepository,
    CardRepository,
    EventAllocationRepository,
    FinancialAccountRepository,
    FinancialEventRepository,
    FinancialEventSourceLinkRepository,
    FundingCommitmentRepository,
    JournalRepository,
    LedgerAccountRepository,
    ReconciliationLedgerRepository,
    SalaryAllocationRunItemRepository,
    SalaryAllocationRunRepository,
    SalaryPlanTemplateItemRepository,
    SalaryPlanTemplateRepository,
    ScheduledTransferCommitmentRepository,
    ScheduledTransferRepository,
    SubscriptionPricePeriodRepository,
    SubscriptionRepository,
    WalletRepository,
)
from app.domain.ports.import_repositories import (
    ImportBatchRepository,
    ImportRowRepository,
    LegacyDailyActivityRepository,
)
from app.domain.ports.repositories import (
    AppSettingRepository,
    AttachmentRepository,
    ClassificationResultRepository,
    EmailRepository,
    GmailAccountRepository,
    GmailLabelOperationRepository,
    ParserRunRepository,
    SenderRuleRepository,
    SupplementaryCardSettingRepository,
    SyncRunRepository,
    SyncStateRepository,
    TransactionRepository,
)
from app.infrastructure.database.budget_repositories import (
    SqlAlchemyBalanceSnapshotRepository,
    SqlAlchemyBudgetCategoryRepository,
    SqlAlchemyBudgetLedgerRepository,
    SqlAlchemyBudgetPeriodRepository,
    SqlAlchemyBudgetTargetRepository,
    SqlAlchemyCardImportAssignmentRepository,
    SqlAlchemyCardRepository,
    SqlAlchemyEventAllocationRepository,
    SqlAlchemyFinancialAccountRepository,
    SqlAlchemyFinancialEventRepository,
    SqlAlchemyFinancialEventSourceLinkRepository,
    SqlAlchemyFundingCommitmentRepository,
    SqlAlchemyJournalRepository,
    SqlAlchemyLedgerAccountRepository,
    SqlAlchemyReconciliationLedgerRepository,
    SqlAlchemyWalletRepository,
)
from app.infrastructure.database.import_repositories import (
    SqlAlchemyImportBatchRepository,
    SqlAlchemyImportRowRepository,
    SqlAlchemyLegacyDailyActivityRepository,
)
from app.infrastructure.database.repositories import (
    SqlAlchemyAppSettingRepository,
    SqlAlchemyAttachmentRepository,
    SqlAlchemyClassificationResultRepository,
    SqlAlchemyEmailRepository,
    SqlAlchemyGmailAccountRepository,
    SqlAlchemyGmailLabelOperationRepository,
    SqlAlchemyParserRunRepository,
    SqlAlchemySenderRuleRepository,
    SqlAlchemySupplementaryCardSettingRepository,
    SqlAlchemySyncRunRepository,
    SqlAlchemySyncStateRepository,
    SqlAlchemyTransactionRepository,
)
from app.infrastructure.database.salary_repositories import (
    SqlAlchemySalaryAllocationRunItemRepository,
    SqlAlchemySalaryAllocationRunRepository,
    SqlAlchemySalaryPlanTemplateItemRepository,
    SqlAlchemySalaryPlanTemplateRepository,
)
from app.infrastructure.database.scheduled_transfer_repositories import (
    SqlAlchemyScheduledTransferCommitmentRepository,
    SqlAlchemyScheduledTransferRepository,
)
from app.infrastructure.database.subscription_repositories import (
    SqlAlchemySubscriptionPricePeriodRepository,
    SqlAlchemySubscriptionRepository,
)


class SqlAlchemyUnitOfWork:
    """Own a short session and expose repositories sharing its transaction."""

    accounts: GmailAccountRepository
    sender_rules: SenderRuleRepository
    supplementary_card_settings: SupplementaryCardSettingRepository
    emails: EmailRepository
    gmail_label_operations: GmailLabelOperationRepository
    attachments: AttachmentRepository
    transactions: TransactionRepository
    parser_runs: ParserRunRepository
    classification_results: ClassificationResultRepository
    sync_states: SyncStateRepository
    sync_runs: SyncRunRepository
    app_settings: AppSettingRepository
    financial_accounts: FinancialAccountRepository
    wallets: WalletRepository
    ledger_accounts: LedgerAccountRepository
    journal: JournalRepository
    cards: CardRepository
    card_import_assignments: CardImportAssignmentRepository
    financial_events: FinancialEventRepository
    source_links: FinancialEventSourceLinkRepository
    budget_categories: BudgetCategoryRepository
    budget_periods: BudgetPeriodRepository
    budget_targets: BudgetTargetRepository
    allocations: EventAllocationRepository
    commitments: FundingCommitmentRepository
    budget_ledger: BudgetLedgerRepository
    subscriptions: SubscriptionRepository
    subscription_prices: SubscriptionPricePeriodRepository
    salary_templates: SalaryPlanTemplateRepository
    salary_template_items: SalaryPlanTemplateItemRepository
    salary_runs: SalaryAllocationRunRepository
    salary_run_items: SalaryAllocationRunItemRepository
    scheduled_transfers: ScheduledTransferRepository
    scheduled_transfer_commitments: ScheduledTransferCommitmentRepository
    balance_snapshots: BalanceSnapshotRepository
    reconciliation_ledger: ReconciliationLedgerRepository
    import_batches: ImportBatchRepository
    import_rows: ImportRowRepository
    legacy_daily_activities: LegacyDailyActivityRepository

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session = session_factory()
        self.accounts = SqlAlchemyGmailAccountRepository(self._session)
        self.sender_rules = SqlAlchemySenderRuleRepository(self._session)
        self.supplementary_card_settings = SqlAlchemySupplementaryCardSettingRepository(
            self._session
        )
        self.emails = SqlAlchemyEmailRepository(self._session)
        self.gmail_label_operations = SqlAlchemyGmailLabelOperationRepository(
            self._session
        )
        self.attachments = SqlAlchemyAttachmentRepository(self._session)
        self.transactions = SqlAlchemyTransactionRepository(self._session)
        self.parser_runs = SqlAlchemyParserRunRepository(self._session)
        self.classification_results = SqlAlchemyClassificationResultRepository(
            self._session
        )
        self.sync_states = SqlAlchemySyncStateRepository(self._session)
        self.sync_runs = SqlAlchemySyncRunRepository(self._session)
        self.app_settings = SqlAlchemyAppSettingRepository(self._session)
        self.financial_accounts = SqlAlchemyFinancialAccountRepository(self._session)
        self.wallets = SqlAlchemyWalletRepository(self._session)
        self.ledger_accounts = SqlAlchemyLedgerAccountRepository(self._session)
        self.journal = SqlAlchemyJournalRepository(self._session)
        self.cards = SqlAlchemyCardRepository(self._session)
        self.card_import_assignments = SqlAlchemyCardImportAssignmentRepository(
            self._session
        )
        self.financial_events = SqlAlchemyFinancialEventRepository(self._session)
        self.source_links = SqlAlchemyFinancialEventSourceLinkRepository(self._session)
        self.budget_categories = SqlAlchemyBudgetCategoryRepository(self._session)
        self.budget_periods = SqlAlchemyBudgetPeriodRepository(self._session)
        self.budget_targets = SqlAlchemyBudgetTargetRepository(self._session)
        self.allocations = SqlAlchemyEventAllocationRepository(self._session)
        self.commitments = SqlAlchemyFundingCommitmentRepository(self._session)
        self.budget_ledger = SqlAlchemyBudgetLedgerRepository(self._session)
        self.subscriptions = SqlAlchemySubscriptionRepository(self._session)
        self.subscription_prices = SqlAlchemySubscriptionPricePeriodRepository(
            self._session
        )
        self.salary_templates = SqlAlchemySalaryPlanTemplateRepository(self._session)
        self.salary_template_items = SqlAlchemySalaryPlanTemplateItemRepository(
            self._session
        )
        self.salary_runs = SqlAlchemySalaryAllocationRunRepository(self._session)
        self.salary_run_items = SqlAlchemySalaryAllocationRunItemRepository(
            self._session
        )
        self.scheduled_transfers = SqlAlchemyScheduledTransferRepository(self._session)
        self.scheduled_transfer_commitments = (
            SqlAlchemyScheduledTransferCommitmentRepository(self._session)
        )
        self.balance_snapshots = SqlAlchemyBalanceSnapshotRepository(self._session)
        self.reconciliation_ledger = SqlAlchemyReconciliationLedgerRepository(
            self._session
        )
        self.import_batches = SqlAlchemyImportBatchRepository(self._session)
        self.import_rows = SqlAlchemyImportRowRepository(self._session)
        self.legacy_daily_activities = SqlAlchemyLegacyDailyActivityRepository(
            self._session
        )

    def __enter__(self) -> SqlAlchemyUnitOfWork:
        """Enter the unit-of-work context."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Rollback uncommitted work and close the session."""
        del exc_type, exc_value, traceback
        self._session.rollback()
        self._session.close()

    def commit(self) -> None:
        """Commit the current transaction or raise a stable database error."""
        try:
            self._session.commit()
        except SQLAlchemyError as error:
            self._session.rollback()
            raise DatabaseError(
                "database transaction could not be committed"
            ) from error

    def rollback(self) -> None:
        """Discard the current transaction."""
        self._session.rollback()

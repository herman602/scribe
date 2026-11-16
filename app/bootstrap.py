"""Application composition root."""

import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI

from app.application.queries.budget import BudgetQueries
from app.application.queries.clearing import ClearingQueries
from app.application.queries.consolidated import ConsolidatedMoneyQueries
from app.application.queries.dashboard import DashboardQueries
from app.application.queries.money import MoneyBalanceQueries, MoneyReviewQueries
from app.application.queries.salary import SalaryQueries
from app.application.queries.spending import SpendingQueries
from app.application.queries.statistics import StatisticsQueryService
from app.application.queries.subscriptions import SubscriptionQueries
from app.application.queries.sync import SyncDashboardQueries
from app.application.services.bank_movements import RecordBankMovement
from app.application.services.budget_close import CloseBudgetPeriod
from app.application.services.budget_planning import ManageBudgetPlan
from app.application.services.cards import ManageCards
from app.application.services.cutover_import import BudgetCutoverImport
from app.application.services.email_processing import PrepareEmailProcessing
from app.application.services.event_allocation import (
    AllocateFinancialEvent,
    RedoAllocation,
)
from app.application.services.financial_event_intake import FinancialEventIntake
from app.application.services.gmail_auth_health import GmailAuthHealthService
from app.application.services.gmail_authentication import AuthenticateGmail
from app.application.services.gmail_full_sync import FullGmailSync
from app.application.services.gmail_incremental_sync import IncrementalGmailSync
from app.application.services.gmail_labeling import ManageProcessedGmailLabels
from app.application.services.gmail_message_persistence import PersistGmailMessage
from app.application.services.ledger import JournalService, LedgerSetupService
from app.application.services.macos_services import ManageMacOSServices
from app.application.services.manual_money import (
    CreateManualExpense,
    ReverseFinancialEvent,
    UpdateFinancialEventNote,
)
from app.application.services.reconciliation import (
    AdjustReconciliation,
    RecordBalanceSnapshot,
)
from app.application.services.salary_allocation import (
    ApproveSalaryAllocation,
    GenerateSalaryAllocation,
    ManageSalaryTemplates,
    MarkSalaryTransferred,
    RecordSalaryReceipt,
)
from app.application.services.scheduled_transfers import ScheduleTransfers
from app.application.services.sender_rule_management import ManageSenderRules
from app.application.services.source_review import ReviewSourceLinks
from app.application.services.stored_email_reparse import ReparseStoredEmail
from app.application.services.subscriptions import (
    CalculateSubscriptionRequirement,
    ManageSubscriptions,
)
from app.application.services.supplementary_card_settings import SetSupplementaryCard
from app.application.services.sync_control import ControlBackgroundSync
from app.application.services.transaction_confirmation import ConfirmTransactions
from app.application.services.transaction_review import ReviewTransaction
from app.domain.errors import GmailAuthenticationError
from app.domain.gmail import gmail_oauth_scopes
from app.domain.parsing import ClassificationEngine, ExtractionLimits
from app.domain.ports.repositories import UnitOfWork
from app.infrastructure.attachments.pdf import LocalPdfExtractor
from app.infrastructure.attachments.secrets import KeyringSecretStore
from app.infrastructure.cloudflare.access import CloudflareAccessJwtVerifier
from app.infrastructure.config.settings import AppSettings
from app.infrastructure.database.budget_queries import (
    SqlAlchemyBudgetReadRepository,
    SqlAlchemyLedgerBalanceReadRepository,
    SqlAlchemyMoneyReviewReadRepository,
)
from app.infrastructure.database.clearing_queries import (
    SqlAlchemyClearingReadRepository,
)
from app.infrastructure.database.dashboard_queries import (
    SqlAlchemyDashboardReadRepository,
)
from app.infrastructure.database.engine import Database, create_database
from app.infrastructure.database.salary_queries import SqlAlchemySalaryReadRepository
from app.infrastructure.database.spending_queries import (
    SqlAlchemySpendingReadRepository,
)
from app.infrastructure.database.statistics_queries import (
    SqlAlchemyStatisticsReadRepository,
)
from app.infrastructure.database.subscription_queries import (
    SqlAlchemySubscriptionReadRepository,
)
from app.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork
from app.infrastructure.gmail.gateway import GoogleGmailGateway
from app.infrastructure.gmail.mime import GmailMimeDecoder
from app.infrastructure.gmail.oauth import GoogleOAuthAuthenticator
from app.infrastructure.gmail.token_store import GoogleTokenStore
from app.infrastructure.macos.launchd import LaunchdServiceGateway
from app.infrastructure.observability.logging import configure_logging
from app.infrastructure.parsers import (
    CathayTransactionParser,
    ClassificationPipeline,
    DefaultParserRegistry,
    FirstBankTransactionParser,
    FubonTransactionParser,
    GenericTransactionParser,
    HncbTransactionParser,
    SinopacTransactionParser,
    StandardCharteredTransactionParser,
    SunnyTransactionParser,
)
from app.presentation.web.app import WebDependencies, create_web_app
from app.worker.local import LocalSyncWorker
from app.worker.maintenance import LocalMaintenanceCoordinator


@dataclass(frozen=True)
class MoneyRuntime:
    """Ledger services sharing one short-lived local database engine."""

    database: Database
    setup: LedgerSetupService
    journal: JournalService
    balances: MoneyBalanceQueries
    intake: FinancialEventIntake
    cards: ManageCards
    plan: ManageBudgetPlan
    allocate: AllocateFinancialEvent
    redo_allocation: RedoAllocation
    close_period: CloseBudgetPeriod
    budget: BudgetQueries
    subscriptions: ManageSubscriptions
    subscription_register: SubscriptionQueries
    subscription_requirement: CalculateSubscriptionRequirement
    salary_templates: ManageSalaryTemplates
    salary_receipt: RecordSalaryReceipt
    salary_allocation: GenerateSalaryAllocation
    salary_approval: ApproveSalaryAllocation
    salary_transfer: MarkSalaryTransferred
    salary: SalaryQueries
    schedule_transfers: ScheduleTransfers
    clearing: ClearingQueries
    manual_expense: CreateManualExpense
    update_event_note: UpdateFinancialEventNote
    reverse_event: ReverseFinancialEvent
    bank_movements: RecordBankMovement
    balance_snapshots: RecordBalanceSnapshot
    reconciliation: AdjustReconciliation
    source_review: ReviewSourceLinks
    spending: SpendingQueries
    cutover: BudgetCutoverImport
    confirm_transactions: ConfirmTransactions
    review_transaction: ReviewTransaction
    set_supplementary_card: SetSupplementaryCard
    reparse_email: ReparseStoredEmail
    sync_control: ControlBackgroundSync
    queries: DashboardQueries

    def close(self) -> None:
        """Release the SQLite connection pool owned by this runtime."""
        self.database.engine.dispose()


@dataclass(frozen=True)
class _SyncRuntime:
    gateway: GoogleGmailGateway
    decoder: GmailMimeDecoder
    processing: PrepareEmailProcessing
    unit_of_work_factory: Callable[[], UnitOfWork]
    label_manager: ManageProcessedGmailLabels | None


def build_macos_service_manager(
    settings: AppSettings,
    *,
    uid: int | None = None,
    launch_agents_dir: Path | None = None,
    platform_name: Callable[[], str] | None = None,
) -> ManageMacOSServices:
    """Compose service management without opening Gmail or SQLite."""
    service_uid = os.getuid() if uid is None else uid
    destination = (
        launch_agents_dir
        if launch_agents_dir is not None
        else Path.home() / "Library" / "LaunchAgents"
    )
    gateway = LaunchdServiceGateway(
        uid=service_uid,
        launch_agents_dir=destination,
    )
    return ManageMacOSServices(
        gateway,
        settings,
        launch_agents_dir=destination,
        platform_name=platform_name or (lambda: sys.platform),
    )


def build_web_application(settings: AppSettings) -> FastAPI:
    """Build a configured FastAPI application."""
    configure_logging(settings)
    database = create_database(settings)

    def unit_of_work_factory() -> UnitOfWork:
        return SqlAlchemyUnitOfWork(database.session_factory)

    intake = FinancialEventIntake(unit_of_work_factory)
    worker = LocalSyncWorker(
        incremental_factory=lambda: build_incremental_sync_service(settings),
        full_factory=lambda: build_full_sync_service(settings),
    )
    schedule_transfers = build_schedule_transfers(settings, unit_of_work_factory)
    dashboard_queries = DashboardQueries(
        SqlAlchemyDashboardReadRepository(
            database.session_factory,
            local_timezone=settings.local_timezone,
        )
    )
    budget_queries = BudgetQueries(
        SqlAlchemyBudgetReadRepository(
            database.session_factory,
            local_timezone=settings.local_timezone,
        )
    )
    spending_queries = SpendingQueries(
        SqlAlchemySpendingReadRepository(database.session_factory),
        local_timezone=settings.local_timezone,
    )
    consolidated_queries = ConsolidatedMoneyQueries(
        spending=spending_queries,
        budget=budget_queries,
        parsed_transactions=dashboard_queries,
        default_currency=settings.default_currency,
        local_timezone=settings.local_timezone,
    )
    maintenance = LocalMaintenanceCoordinator(
        lambda: schedule_transfers,
        enabled=settings.maintenance_enabled,
        poll_seconds=settings.maintenance_poll_seconds,
        batch_size=settings.maintenance_batch_size,
    )
    access_verifier = None
    if settings.external_confirmation_enabled:
        assert settings.cloudflare_access_team_domain is not None
        assert settings.cloudflare_access_audience is not None
        assert settings.cloudflare_access_allowed_email is not None
        access_verifier = CloudflareAccessJwtVerifier(
            team_domain=settings.cloudflare_access_team_domain,
            audience=settings.cloudflare_access_audience,
            allowed_email=settings.cloudflare_access_allowed_email,
        )
    return create_web_app(
        settings,
        WebDependencies(
            queries=dashboard_queries,
            consolidated=consolidated_queries,
            confirm_transactions=ConfirmTransactions(
                unit_of_work_factory, intake=intake
            ),
            review_transaction=ReviewTransaction(unit_of_work_factory, intake=intake),
            set_supplementary_card=SetSupplementaryCard(unit_of_work_factory),
            reparse_email=ReparseStoredEmail(
                _build_parser_pipeline(settings),
                unit_of_work_factory,
                processed_label_name=(
                    settings.gmail_processed_label
                    if settings.gmail_labeling_enabled
                    else None
                ),
                intake=intake,
            ),
            statistics=StatisticsQueryService(
                SqlAlchemyStatisticsReadRepository(
                    database.session_factory,
                    local_timezone=settings.local_timezone,
                )
            ),
            sync_queries=SyncDashboardQueries(unit_of_work_factory),
            sync_control=ControlBackgroundSync(worker, unit_of_work_factory),
            money_review=MoneyReviewQueries(
                SqlAlchemyMoneyReviewReadRepository(database.session_factory)
            ),
            money_balances=MoneyBalanceQueries(
                SqlAlchemyLedgerBalanceReadRepository(database.session_factory)
            ),
            manage_cards=ManageCards(unit_of_work_factory),
            ledger_setup=LedgerSetupService(unit_of_work_factory),
            budget=budget_queries,
            manage_budget_plan=ManageBudgetPlan(unit_of_work_factory),
            allocate_event=AllocateFinancialEvent(
                unit_of_work_factory, JournalService(unit_of_work_factory)
            ),
            redo_allocation=RedoAllocation(
                unit_of_work_factory, JournalService(unit_of_work_factory)
            ),
            close_budget_period=CloseBudgetPeriod(
                unit_of_work_factory,
                JournalService(unit_of_work_factory),
                local_timezone=settings.local_timezone,
            ),
            manage_subscriptions=ManageSubscriptions(unit_of_work_factory),
            subscription_register=SubscriptionQueries(
                SqlAlchemySubscriptionReadRepository(database.session_factory)
            ),
            subscription_requirement=CalculateSubscriptionRequirement(
                SqlAlchemySubscriptionReadRepository(database.session_factory)
            ),
            manage_salary_templates=ManageSalaryTemplates(unit_of_work_factory),
            record_salary_receipt=RecordSalaryReceipt(
                unit_of_work_factory, JournalService(unit_of_work_factory)
            ),
            generate_salary_allocation=GenerateSalaryAllocation(unit_of_work_factory),
            approve_salary_allocation=ApproveSalaryAllocation(
                unit_of_work_factory, JournalService(unit_of_work_factory)
            ),
            mark_salary_transferred=MarkSalaryTransferred(
                unit_of_work_factory, JournalService(unit_of_work_factory)
            ),
            salary=SalaryQueries(
                SqlAlchemySalaryReadRepository(database.session_factory)
            ),
            schedule_transfers=schedule_transfers,
            clearing=ClearingQueries(
                SqlAlchemyClearingReadRepository(database.session_factory)
            ),
            manual_expense=CreateManualExpense(
                unit_of_work_factory, _allocator(unit_of_work_factory)
            ),
            update_event_note=UpdateFinancialEventNote(unit_of_work_factory),
            reverse_event=ReverseFinancialEvent(
                unit_of_work_factory, JournalService(unit_of_work_factory)
            ),
            bank_movements=RecordBankMovement(
                unit_of_work_factory,
                JournalService(unit_of_work_factory),
                _allocator(unit_of_work_factory),
            ),
            balance_snapshots=RecordBalanceSnapshot(unit_of_work_factory),
            reconciliation=AdjustReconciliation(
                unit_of_work_factory, JournalService(unit_of_work_factory)
            ),
            source_review=ReviewSourceLinks(unit_of_work_factory, intake),
            spending=spending_queries,
            gmail_auth_health=GmailAuthHealthService(unit_of_work_factory),
            maintenance=maintenance,
            external_access_verifier=access_verifier,
            close=database.engine.dispose,
        ),
    )


def _allocator(
    unit_of_work_factory: Callable[[], UnitOfWork],
) -> AllocateFinancialEvent:
    """Compose the one allocator every posting path shares."""
    return AllocateFinancialEvent(
        unit_of_work_factory, JournalService(unit_of_work_factory)
    )


def build_schedule_transfers(
    settings: AppSettings, unit_of_work_factory: Callable[[], UnitOfWork]
) -> ScheduleTransfers:
    """Compose the clearing scheduler from bounded maintenance settings."""
    return ScheduleTransfers(
        unit_of_work_factory,
        JournalService(unit_of_work_factory),
        default_local_time=settings.scheduled_transfer_time,
        timezone=settings.scheduled_transfer_timezone,
        lease_seconds=settings.maintenance_lease_seconds,
        batch_size=settings.maintenance_batch_size,
        retry_base_seconds=settings.maintenance_retry_base_seconds,
        retry_max_seconds=settings.maintenance_retry_max_seconds,
    )


def build_gmail_authentication_service(settings: AppSettings) -> AuthenticateGmail:
    """Compose Gmail OAuth with the account repository."""
    configure_logging(settings)
    database = create_database(settings)
    authenticator = GoogleOAuthAuthenticator(
        settings.gmail_credentials_path,
        GoogleTokenStore(
            settings.gmail_token_path,
            gmail_oauth_scopes(labeling_enabled=settings.gmail_labeling_enabled),
        ),
        gateway_factory=lambda credentials: GoogleGmailGateway(
            credentials,
            request_timeout_seconds=settings.gmail_request_timeout_seconds,
            max_attempts=settings.gmail_retry_max_attempts,
            backoff_base_seconds=settings.gmail_backoff_base_seconds,
            backoff_max_seconds=settings.gmail_backoff_max_seconds,
        ),
    )

    def unit_of_work_factory() -> UnitOfWork:
        return SqlAlchemyUnitOfWork(database.session_factory)

    return AuthenticateGmail(
        authenticator,
        unit_of_work_factory,
        GmailAuthHealthService(unit_of_work_factory),
    )


def build_sender_rule_service(settings: AppSettings) -> ManageSenderRules:
    """Compose offline sender-rule management over SQLite."""
    configure_logging(settings)
    database = create_database(settings)

    def unit_of_work_factory() -> UnitOfWork:
        return SqlAlchemyUnitOfWork(database.session_factory)

    return ManageSenderRules(unit_of_work_factory, KeyringSecretStore())


def build_money_runtime(settings: AppSettings) -> MoneyRuntime:
    """Compose offline ledger setup, posting, and balance reads over SQLite."""
    configure_logging(settings)
    database = create_database(settings)

    def unit_of_work_factory() -> UnitOfWork:
        return SqlAlchemyUnitOfWork(database.session_factory)

    journal = JournalService(unit_of_work_factory)

    return MoneyRuntime(
        database=database,
        setup=LedgerSetupService(unit_of_work_factory),
        journal=journal,
        balances=MoneyBalanceQueries(
            SqlAlchemyLedgerBalanceReadRepository(database.session_factory)
        ),
        intake=FinancialEventIntake(unit_of_work_factory),
        cards=ManageCards(unit_of_work_factory),
        plan=ManageBudgetPlan(unit_of_work_factory),
        allocate=AllocateFinancialEvent(
            unit_of_work_factory, JournalService(unit_of_work_factory)
        ),
        redo_allocation=RedoAllocation(
            unit_of_work_factory, JournalService(unit_of_work_factory)
        ),
        close_period=CloseBudgetPeriod(
            unit_of_work_factory,
            JournalService(unit_of_work_factory),
            local_timezone=settings.local_timezone,
        ),
        budget=BudgetQueries(
            SqlAlchemyBudgetReadRepository(
                database.session_factory,
                local_timezone=settings.local_timezone,
            )
        ),
        subscriptions=ManageSubscriptions(unit_of_work_factory),
        subscription_register=SubscriptionQueries(
            SqlAlchemySubscriptionReadRepository(database.session_factory)
        ),
        subscription_requirement=CalculateSubscriptionRequirement(
            SqlAlchemySubscriptionReadRepository(database.session_factory)
        ),
        salary_templates=ManageSalaryTemplates(unit_of_work_factory),
        salary_receipt=RecordSalaryReceipt(
            unit_of_work_factory, JournalService(unit_of_work_factory)
        ),
        salary_allocation=GenerateSalaryAllocation(unit_of_work_factory),
        salary_approval=ApproveSalaryAllocation(
            unit_of_work_factory, JournalService(unit_of_work_factory)
        ),
        salary_transfer=MarkSalaryTransferred(
            unit_of_work_factory, JournalService(unit_of_work_factory)
        ),
        salary=SalaryQueries(SqlAlchemySalaryReadRepository(database.session_factory)),
        schedule_transfers=build_schedule_transfers(settings, unit_of_work_factory),
        clearing=ClearingQueries(
            SqlAlchemyClearingReadRepository(database.session_factory)
        ),
        manual_expense=CreateManualExpense(
            unit_of_work_factory, _allocator(unit_of_work_factory)
        ),
        update_event_note=UpdateFinancialEventNote(unit_of_work_factory),
        reverse_event=ReverseFinancialEvent(
            unit_of_work_factory, JournalService(unit_of_work_factory)
        ),
        bank_movements=RecordBankMovement(
            unit_of_work_factory,
            JournalService(unit_of_work_factory),
            _allocator(unit_of_work_factory),
        ),
        balance_snapshots=RecordBalanceSnapshot(unit_of_work_factory),
        reconciliation=AdjustReconciliation(
            unit_of_work_factory, JournalService(unit_of_work_factory)
        ),
        source_review=ReviewSourceLinks(
            unit_of_work_factory, FinancialEventIntake(unit_of_work_factory)
        ),
        spending=SpendingQueries(
            SqlAlchemySpendingReadRepository(database.session_factory),
            local_timezone=settings.local_timezone,
        ),
        cutover=BudgetCutoverImport(unit_of_work_factory, journal),
        confirm_transactions=ConfirmTransactions(
            unit_of_work_factory, intake=FinancialEventIntake(unit_of_work_factory)
        ),
        review_transaction=ReviewTransaction(
            unit_of_work_factory, intake=FinancialEventIntake(unit_of_work_factory)
        ),
        set_supplementary_card=SetSupplementaryCard(unit_of_work_factory),
        reparse_email=ReparseStoredEmail(
            _build_parser_pipeline(settings),
            unit_of_work_factory,
            processed_label_name=(
                settings.gmail_processed_label
                if settings.gmail_labeling_enabled
                else None
            ),
            intake=FinancialEventIntake(unit_of_work_factory),
        ),
        sync_control=ControlBackgroundSync(None, unit_of_work_factory),
        queries=DashboardQueries(
            SqlAlchemyDashboardReadRepository(
                database.session_factory, local_timezone=settings.local_timezone
            )
        ),
    )


def build_full_sync_service(settings: AppSettings) -> FullGmailSync:
    """Compose authenticated Gmail sync, local MIME decoding, and SQLite."""
    runtime = _build_sync_runtime(settings)
    return FullGmailSync(
        runtime.gateway,
        runtime.decoder,
        runtime.processing,
        runtime.unit_of_work_factory,
        max_results=settings.sync_batch_size,
        work_batch_size=settings.sync_batch_size,
        body_retention_policy=settings.body_retention_policy,
        body_summary_chars=settings.body_summary_chars,
        lease_seconds=settings.sync_worker_lease_seconds,
        processed_label_name=(
            settings.gmail_processed_label if settings.gmail_labeling_enabled else None
        ),
        label_manager=runtime.label_manager,
        intake=FinancialEventIntake(runtime.unit_of_work_factory),
    )


def build_incremental_sync_service(settings: AppSettings) -> IncrementalGmailSync:
    """Compose history sync and its safe reconciliation fallback."""
    runtime = _build_sync_runtime(settings)
    persistence = PersistGmailMessage(
        runtime.processing,
        runtime.unit_of_work_factory,
        body_retention_policy=settings.body_retention_policy,
        body_summary_chars=settings.body_summary_chars,
        processed_label_name=(
            settings.gmail_processed_label if settings.gmail_labeling_enabled else None
        ),
        intake=FinancialEventIntake(runtime.unit_of_work_factory),
    )
    reconciliation = FullGmailSync(
        runtime.gateway,
        runtime.decoder,
        runtime.processing,
        runtime.unit_of_work_factory,
        max_results=settings.sync_batch_size,
        work_batch_size=settings.sync_batch_size,
        lease_seconds=settings.sync_worker_lease_seconds,
        body_retention_policy=settings.body_retention_policy,
        body_summary_chars=settings.body_summary_chars,
        processed_label_name=(
            settings.gmail_processed_label if settings.gmail_labeling_enabled else None
        ),
        label_manager=runtime.label_manager,
        intake=FinancialEventIntake(runtime.unit_of_work_factory),
    )
    return IncrementalGmailSync(
        runtime.gateway,
        runtime.decoder,
        persistence,
        reconciliation,
        runtime.unit_of_work_factory,
        max_results=settings.sync_batch_size,
        work_batch_size=settings.sync_batch_size,
        lease_seconds=settings.sync_worker_lease_seconds,
        label_manager=runtime.label_manager,
    )


def _build_sync_runtime(settings: AppSettings) -> _SyncRuntime:
    """Build shared Gmail/database/parser adapters for one foreground command."""
    configure_logging(settings)
    database = create_database(settings)

    def unit_of_work_factory() -> UnitOfWork:
        return SqlAlchemyUnitOfWork(database.session_factory)

    token_store = GoogleTokenStore(
        settings.gmail_token_path,
        gmail_oauth_scopes(labeling_enabled=settings.gmail_labeling_enabled),
    )
    authenticator = GoogleOAuthAuthenticator(
        settings.gmail_credentials_path,
        token_store,
    )
    try:
        credentials = authenticator.credentials_for_api()
    except GmailAuthenticationError:
        try:
            GmailAuthHealthService(unit_of_work_factory).mark_reauthorization_required(
                reason_code="credentials_unavailable"
            )
        finally:
            database.engine.dispose()
        raise
    gateway = GoogleGmailGateway(
        credentials,
        request_timeout_seconds=settings.gmail_request_timeout_seconds,
        max_attempts=settings.gmail_retry_max_attempts,
        backoff_base_seconds=settings.gmail_backoff_base_seconds,
        backoff_max_seconds=settings.gmail_backoff_max_seconds,
    )
    decoder = GmailMimeDecoder(
        max_body_bytes=settings.gmail_max_body_bytes,
        max_attachment_bytes=settings.gmail_max_attachment_bytes,
    )
    processing = PrepareEmailProcessing(
        pipeline=_build_parser_pipeline(settings),
        attachment_extractor=LocalPdfExtractor(),
        secret_store=KeyringSecretStore(),
        extraction_limits=ExtractionLimits(
            max_bytes=settings.pdf_max_bytes,
            max_pages=settings.pdf_max_pages,
            max_text_chars=settings.pdf_max_text_chars,
            max_password_attempts=settings.pdf_max_password_attempts,
            max_seconds=settings.pdf_max_seconds,
        ),
    )

    label_manager = (
        ManageProcessedGmailLabels(
            gateway,
            unit_of_work_factory,
            label_name=settings.gmail_processed_label,
        )
        if settings.gmail_labeling_enabled
        else None
    )

    return _SyncRuntime(
        gateway=gateway,
        decoder=decoder,
        processing=processing,
        unit_of_work_factory=unit_of_work_factory,
        label_manager=label_manager,
    )


def build_processed_label_service(
    settings: AppSettings,
) -> ManageProcessedGmailLabels:
    """Compose the explicit processed-message Label backfill workflow."""
    if not settings.gmail_labeling_enabled:
        raise ValueError("Gmail processed Label support is disabled in config.toml")
    runtime = _build_sync_runtime(settings)
    assert runtime.label_manager is not None
    return runtime.label_manager


def _build_parser_pipeline(settings: AppSettings) -> ClassificationEngine:
    """Build the deterministic parser chain shared by sync and offline reparse."""
    return ClassificationPipeline(
        DefaultParserRegistry(
            (
                FubonTransactionParser(),
                SunnyTransactionParser(),
                CathayTransactionParser(),
                FirstBankTransactionParser(),
                StandardCharteredTransactionParser(),
                SinopacTransactionParser(),
                HncbTransactionParser(),
                GenericTransactionParser(),
            )
        ),
        confirmed_threshold=settings.parser_confidence_threshold,
    )

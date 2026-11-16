"""Persistence ports expressed only in domain types."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from types import TracebackType
from typing import Protocol, Self

from app.domain.enums import (
    ClassificationStatus,
    ParserTrigger,
    ProcessingStatus,
    RunStatus,
    SyncItemOperation,
    SyncMode,
)
from app.domain.models import (
    AppSetting,
    AttachmentSnapshot,
    EmailAttachment,
    EmailMessage,
    EmailSnapshot,
    EmailUpsertOutcome,
    GmailAccount,
    GmailLabelOperation,
    ProcessedAttachmentSnapshot,
    SenderRule,
    SenderRuleDraft,
    SupplementaryCardSetting,
    SyncRun,
    SyncRunItem,
    SyncState,
    Transaction,
    TransactionDraft,
    TransactionReviewDraft,
)
from app.domain.parsing import PipelineResult, StageResult
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
from app.domain.types import JsonValue


class GmailAccountRepository(Protocol):
    """Persist Gmail profile identity without OAuth secrets."""

    def get_or_create(self, profile_email: str) -> GmailAccount: ...

    def get(self, account_id: int) -> GmailAccount | None: ...

    def list_all(self) -> tuple[GmailAccount, ...]: ...

    def get_by_profile_email(self, profile_email: str) -> GmailAccount | None: ...


class SenderRuleRepository(Protocol):
    """Persist configurable, versioned sender filters."""

    def list_all(self, account_id: int) -> tuple[SenderRule, ...]: ...

    def list_enabled(self, account_id: int) -> tuple[SenderRule, ...]: ...

    def get(self, rule_id: int) -> SenderRule | None: ...

    def upsert(self, draft: SenderRuleDraft) -> SenderRule: ...

    def disable(self, rule_id: int) -> SenderRule | None: ...

    def set_pdf_password_refs(
        self, rule_id: int, references: tuple[str, ...]
    ) -> SenderRule | None: ...


class SupplementaryCardSettingRepository(Protocol):
    """Persist explicit account-scoped bank and last-four mappings."""

    def mark(
        self,
        *,
        account_id: int,
        institution_key: str,
        card_last_four: str,
    ) -> SupplementaryCardSetting: ...

    def unmark(
        self,
        *,
        account_id: int,
        institution_key: str,
        card_last_four: str,
    ) -> bool: ...

    def is_marked(
        self,
        *,
        account_id: int,
        institution_key: str,
        card_last_four: str,
    ) -> bool: ...

    def list_all(self) -> tuple[SupplementaryCardSetting, ...]: ...


class EmailRepository(Protocol):
    """Persist decoded Gmail messages idempotently."""

    def get(self, email_id: int) -> EmailMessage | None: ...

    def get_by_gmail_id(
        self, account_id: int, gmail_message_id: str
    ) -> EmailMessage | None: ...

    def upsert_from_gmail(self, snapshot: EmailSnapshot) -> EmailUpsertOutcome: ...

    def mark_source_deleted(
        self, account_id: int, gmail_message_id: str, deleted_at: datetime
    ) -> bool: ...

    def list_active_gmail_ids(
        self,
        account_id: int,
        *,
        sender_rule_ids: tuple[int, ...] | None,
    ) -> tuple[str, ...]: ...

    def apply_processing_result(
        self,
        email_id: int,
        *,
        status: ProcessingStatus,
        classification: ClassificationStatus,
        confidence: Decimal | None,
        reason: str | None,
        parser_name: str | None,
        parser_version: str | None,
        purge_body: bool,
    ) -> EmailMessage: ...


class AttachmentRepository(Protocol):
    """Persist attachment metadata without retaining plaintext bytes."""

    def list_for_email(self, email_id: int) -> tuple[EmailAttachment, ...]: ...

    def replace_for_email(
        self, email_id: int, snapshots: tuple[AttachmentSnapshot, ...]
    ) -> tuple[EmailAttachment, ...]: ...

    def replace_processed_for_email(
        self,
        email_id: int,
        snapshots: tuple[ProcessedAttachmentSnapshot, ...],
    ) -> tuple[EmailAttachment, ...]: ...


class TransactionRepository(Protocol):
    """Persist transactions separately from parser logic."""

    def add(self, draft: TransactionDraft) -> Transaction: ...

    def get(self, transaction_id: int) -> Transaction | None: ...

    def list_for_email(self, email_id: int) -> tuple[Transaction, ...]: ...

    def reconcile(
        self, email_id: int, drafts: tuple[TransactionDraft, ...]
    ) -> tuple[Transaction, ...]: ...

    def confirm_pending(self, transaction_ids: frozenset[int]) -> int: ...

    def list_confirmed_ids(self) -> tuple[int, ...]: ...

    def apply_review(self, draft: TransactionReviewDraft) -> Transaction: ...


class ParserRunRepository(Protocol):
    """Persist parser attempts and their safe diagnostics."""

    def start(
        self,
        *,
        email_id: int,
        sync_run_id: str | None,
        trigger: ParserTrigger,
    ) -> int: ...

    def succeed(self, parser_run_id: int, result: PipelineResult) -> None: ...

    def fail(
        self,
        parser_run_id: int,
        *,
        error_code: str,
        error_message: str,
        debug_trace: str,
    ) -> None: ...


class ClassificationResultRepository(Protocol):
    """Append immutable classification-stage evidence."""

    def add_many(
        self,
        *,
        email_id: int,
        parser_run_id: int,
        stages: tuple[StageResult, ...],
    ) -> None: ...


class SyncStateRepository(Protocol):
    """Persist Gmail history and full-sync checkpoints."""

    def get(self, account_id: int) -> SyncState | None: ...

    def get_or_create(self, account_id: int) -> SyncState: ...

    def save(self, state: SyncState, *, expected_version: int) -> SyncState: ...


class SyncRunRepository(Protocol):
    """Persist resumable pages and message-level work items."""

    def create(
        self,
        *,
        account_id: int,
        mode: SyncMode,
        query: str | None,
        start_history_id: str,
    ) -> SyncRun: ...

    def find_any_running(self, account_id: int) -> SyncRun | None: ...

    def get(self, run_id: str) -> SyncRun | None: ...

    def list_recent(self, *, limit: int) -> tuple[SyncRun, ...]: ...

    def acquire_lease(
        self,
        run_id: str,
        *,
        worker_id: str,
        now: datetime,
        expires_at: datetime,
    ) -> bool: ...

    def renew_lease(
        self,
        run_id: str,
        *,
        worker_id: str,
        now: datetime,
        expires_at: datetime,
    ) -> bool: ...

    def release_lease(self, run_id: str, *, worker_id: str) -> None: ...

    def request_cancel(self, run_id: str) -> bool: ...

    def cancellation_requested(self, run_id: str) -> bool: ...

    def enqueue_message_ids(self, run_id: str, message_ids: tuple[str, ...]) -> int: ...

    def enqueue_changes(
        self,
        run_id: str,
        changes: tuple[tuple[str, SyncItemOperation], ...],
    ) -> int: ...

    def checkpoint_page(
        self,
        run_id: str,
        *,
        next_page_token: str | None,
        end_history_id: str | None = None,
    ) -> SyncRun: ...

    def restart_listing(self, run_id: str) -> SyncRun: ...

    def list_processable(
        self, run_id: str, *, limit: int
    ) -> tuple[SyncRunItem, ...]: ...

    def list_message_ids(self, run_id: str) -> tuple[str, ...]: ...

    def mark_succeeded(
        self,
        item_id: int,
        *,
        created: bool,
        parsed: bool = False,
        parser_failed: bool = False,
        deleted: bool = False,
    ) -> None: ...

    def mark_skipped(self, item_id: int) -> None: ...

    def mark_failed(
        self,
        item_id: int,
        *,
        error_code: str,
        error_message: str,
        retryable: bool,
    ) -> None: ...

    def failed_count(self, run_id: str) -> int: ...

    def record_error(
        self,
        run_id: str,
        *,
        error_code: str,
        error_message: str,
        debug_trace: str | None,
    ) -> None: ...

    def finish(
        self,
        run_id: str,
        *,
        status: RunStatus,
        end_history_id: str | None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> SyncRun: ...


class AppSettingRepository(Protocol):
    """Persist non-secret JSON preferences."""

    def get(self, key: str) -> AppSetting | None: ...

    def set(
        self, key: str, value: JsonValue, *, schema_version: int = 1
    ) -> AppSetting: ...


class GmailLabelOperationRepository(Protocol):
    """Persist idempotent message-level Gmail Label mutations."""

    def enqueue(self, *, email_id: int, label_name: str) -> GmailLabelOperation: ...

    def enqueue_processed(self, *, account_id: int, label_name: str) -> int: ...

    def list_processable(
        self, *, account_id: int, label_name: str, limit: int
    ) -> tuple[GmailLabelOperation, ...]: ...

    def mark_succeeded(self, operation_ids: tuple[int, ...]) -> None: ...

    def mark_failed(
        self,
        operation_ids: tuple[int, ...],
        *,
        error_code: str,
        error_message: str,
        retryable: bool,
    ) -> None: ...

    def counts(self, *, account_id: int, label_name: str) -> dict[str, int]: ...


class UnitOfWork(Protocol):
    """Own one short database transaction."""

    accounts: GmailAccountRepository
    sender_rules: SenderRuleRepository
    supplementary_card_settings: SupplementaryCardSettingRepository
    emails: EmailRepository
    attachments: AttachmentRepository
    transactions: TransactionRepository
    parser_runs: ParserRunRepository
    classification_results: ClassificationResultRepository
    sync_states: SyncStateRepository
    sync_runs: SyncRunRepository
    app_settings: AppSettingRepository
    gmail_label_operations: GmailLabelOperationRepository
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

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

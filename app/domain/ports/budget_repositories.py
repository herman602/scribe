"""Ledger persistence ports expressed only in domain types."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from typing import Protocol

from app.domain.budget_enums import (
    FinancialEventOrigin,
    FinancialEventStatus,
    PaymentMethod,
    SalaryRunItemStatus,
    SalaryRunStatus,
    ScheduledTransferStatus,
    SourceDiscrepancy,
    SourceLinkReason,
    SourceLinkStatus,
)
from app.domain.budget_models import (
    BalanceSnapshot,
    BalanceSnapshotDraft,
    BudgetCategory,
    BudgetCategoryDraft,
    BudgetCategoryUpdate,
    BudgetPeriod,
    BudgetTarget,
    BudgetTargetDraft,
    Card,
    CardConfigurationDraft,
    CardDraft,
    CardIdentity,
    CardImportAssignment,
    CardImportAssignmentDraft,
    DedicatedWalletAllocation,
    DedicatedWalletAllocationDraft,
    EventAllocation,
    EventAllocationDraft,
    FinancialAccount,
    FinancialAccountDraft,
    FinancialEvent,
    FinancialEventDraft,
    FinancialEventNoteUpdate,
    FinancialEventSourceLink,
    FinancialEventUpdate,
    FundingCommitment,
    FundingCommitmentDraft,
    JournalEntry,
    JournalEntryDraft,
    LedgerAccount,
    LedgerAccountDraft,
    SalaryAllocationRun,
    SalaryAllocationRunDraft,
    SalaryAllocationRunItem,
    SalaryAllocationRunItemDraft,
    SalaryPlanTemplate,
    SalaryPlanTemplateDraft,
    SalaryPlanTemplateItem,
    SalaryPlanTemplateItemDraft,
    SalaryPlanTemplateItemUpdate,
    SalaryPlanTemplateUpdate,
    ScheduledTransfer,
    ScheduledTransferCommitment,
    ScheduledTransferCommitmentDraft,
    ScheduledTransferDraft,
    SourceLinkDraft,
    Subscription,
    SubscriptionDraft,
    SubscriptionPricePeriod,
    SubscriptionPricePeriodDraft,
    SubscriptionPricePeriodUpdate,
    SubscriptionUpdate,
    Wallet,
    WalletDraft,
)


class FinancialAccountRepository(Protocol):
    """Persist physical balance containers without full account numbers."""

    def add(self, draft: FinancialAccountDraft) -> FinancialAccount: ...

    def get(self, account_id: int) -> FinancialAccount | None: ...

    def get_by_identity(
        self, *, institution_code: str, display_name: str, currency: str
    ) -> FinancialAccount | None: ...

    def list_active(self) -> tuple[FinancialAccount, ...]: ...

    def set_display_order(self, account_id: int, position: int) -> None: ...


class WalletRepository(Protocol):
    """Persist virtual envelopes belonging to one physical asset account."""

    def add(self, draft: WalletDraft) -> Wallet: ...

    def get(self, wallet_id: int) -> Wallet | None: ...

    def get_by_name(self, account_id: int, name: str) -> Wallet | None: ...

    def get_unallocated(self, account_id: int) -> Wallet | None: ...

    def list_for_account(self, account_id: int) -> tuple[Wallet, ...]: ...

    def set_display_order(self, wallet_id: int, position: int) -> None: ...

    def rename(self, *, wallet_id: int, name: str, expected_version: int) -> Wallet: ...


class LedgerAccountRepository(Protocol):
    """Persist the double-entry chart of accounts."""

    def add(self, draft: LedgerAccountDraft) -> LedgerAccount: ...

    def get(self, ledger_account_id: int) -> LedgerAccount | None: ...

    def get_by_code(self, code: str) -> LedgerAccount | None: ...

    def get_for_financial_account(self, account_id: int) -> LedgerAccount | None: ...

    def get_for_budget_category(
        self, category_id: int, currency: str
    ) -> LedgerAccount | None: ...

    def list_all(self) -> tuple[LedgerAccount, ...]: ...


class JournalRepository(Protocol):
    """Persist immutable balanced entries; posted rows are never mutated."""

    def add(self, draft: JournalEntryDraft) -> JournalEntry: ...

    def get(self, entry_id: int) -> JournalEntry | None: ...

    def get_by_idempotency_key(self, key: str) -> JournalEntry | None: ...

    def get_reversal_of(self, entry_id: int) -> JournalEntry | None: ...

    def get_by_financial_event(self, event_id: int) -> JournalEntry | None: ...

    def latest_by_financial_event(self, event_id: int) -> JournalEntry | None:
        """Return one event's newest primary entry, cancelled or not.

        Needed where the question is "was anything ever posted for this",
        rather than "what is posted now": a resubmitted reversal has to find
        the entry it already cancelled in order to replay its own result.
        """
        ...


class CardRepository(Protocol):
    """Persist local card configuration keyed by institution and last four."""

    def add(self, draft: CardDraft) -> Card: ...

    def get(self, card_id: int) -> Card | None: ...

    def get_by_identity(self, identity: CardIdentity) -> Card | None: ...

    def list_all(self) -> tuple[Card, ...]: ...

    def list_active_by_last_four(
        self, *, gmail_account_id: int, last_four: str
    ) -> tuple[Card, ...]: ...

    def save_configuration(self, draft: CardConfigurationDraft) -> Card: ...


class CardImportAssignmentRepository(Protocol):
    """Persist explicit mappings from immutable cutover hints to cards."""

    def add(self, draft: CardImportAssignmentDraft) -> CardImportAssignment: ...

    def get_for_import_row(self, import_row_id: int) -> CardImportAssignment | None: ...

    def list_all(self) -> tuple[CardImportAssignment, ...]: ...


class FinancialEventRepository(Protocol):
    """Persist canonical economic events without posting anything."""

    def add(self, draft: FinancialEventDraft) -> FinancialEvent: ...

    def get(self, event_id: int) -> FinancialEvent | None: ...

    def get_by_idempotency_key(self, key: str) -> FinancialEvent | None: ...

    def list_by_status(
        self, statuses: frozenset[FinancialEventStatus]
    ) -> tuple[FinancialEvent, ...]: ...

    def mark_posted(self, event_id: int, expected_version: int) -> FinancialEvent: ...

    def mark_reversed(self, event_id: int, expected_version: int) -> FinancialEvent: ...

    def mark_needs_allocation(
        self, event_id: int, expected_version: int
    ) -> FinancialEvent:
        """Send one posted event back to the allocation queue (ADR-B103)."""
        ...

    def save_note(self, change: FinancialEventNoteUpdate) -> FinancialEvent: ...

    def list_posted_refunds(self, event_id: int) -> tuple[FinancialEvent, ...]: ...

    def list_match_candidates(
        self,
        *,
        origin: FinancialEventOrigin,
        payment_method: PaymentMethod,
        status: FinancialEventStatus,
        currency: str,
        amount_minor: int,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple[FinancialEvent, ...]: ...

    def refresh_source_values(
        self, event_id: int, update: FinancialEventUpdate
    ) -> FinancialEvent: ...


class FinancialEventSourceLinkRepository(Protocol):
    """Persist auditable provenance between events and parsed transactions."""

    def add(self, draft: SourceLinkDraft) -> FinancialEventSourceLink: ...

    def get(self, link_id: int) -> FinancialEventSourceLink | None: ...

    def get_confirmed_for_transaction(
        self, transaction_id: int
    ) -> FinancialEventSourceLink | None: ...

    def list_for_event(self, event_id: int) -> tuple[FinancialEventSourceLink, ...]: ...

    def list_for_transaction(
        self, transaction_id: int
    ) -> tuple[FinancialEventSourceLink, ...]: ...

    def list_for_email(self, email_id: int) -> tuple[FinancialEventSourceLink, ...]: ...

    def list_by_status(
        self, statuses: frozenset[SourceLinkStatus], *, limit: int, offset: int = 0
    ) -> tuple[FinancialEventSourceLink, ...]: ...

    def count_by_status(self, statuses: frozenset[SourceLinkStatus]) -> int: ...

    def upsert_candidate(self, draft: SourceLinkDraft) -> FinancialEventSourceLink: ...

    def confirm(
        self,
        link_id: int,
        *,
        reviewed_at: datetime,
        reasons: tuple[SourceLinkReason, ...],
    ) -> FinancialEventSourceLink: ...

    def reject(
        self,
        link_id: int,
        *,
        reviewed_at: datetime,
        reasons: tuple[SourceLinkReason, ...],
    ) -> FinancialEventSourceLink: ...

    def replace_reasons(
        self, link_id: int, reasons: tuple[SourceLinkReason, ...]
    ) -> FinancialEventSourceLink: ...

    def record_discrepancies(
        self, link_id: int, discrepancies: tuple[SourceDiscrepancy, ...]
    ) -> FinancialEventSourceLink: ...


class BalanceSnapshotRepository(Protocol):
    """Persist immutable observations of real bank balances.

    There is deliberately no update and no delete: a snapshot is evidence of
    what was seen, and a snapshot that could be edited would let a
    reconciliation difference disappear without any accounting (ADR-B066).
    """

    def add(self, draft: BalanceSnapshotDraft) -> BalanceSnapshot: ...

    def get(self, snapshot_id: int) -> BalanceSnapshot | None: ...

    def get_by_idempotency_key(self, key: str) -> BalanceSnapshot | None: ...

    def list_for_account(
        self, account_id: int, *, limit: int
    ) -> tuple[BalanceSnapshot, ...]: ...

    def latest_for_account(self, account_id: int) -> BalanceSnapshot | None: ...


class ReconciliationLedgerRepository(Protocol):
    """Read ledger balances as of an instant, inside the caller's transaction."""

    def physical_balance_as_of(
        self, *, financial_account_id: int, as_of: datetime
    ) -> int: ...


class BudgetCategoryRepository(Protocol):
    """Persist hierarchical, currency-neutral spending purposes."""

    def add(self, draft: BudgetCategoryDraft) -> BudgetCategory: ...

    def get(self, category_id: int) -> BudgetCategory | None: ...

    def list_all(self) -> tuple[BudgetCategory, ...]: ...

    def save(self, update: BudgetCategoryUpdate) -> BudgetCategory: ...

    def is_referenced(self, category_id: int) -> bool: ...


class BudgetPeriodRepository(Protocol):
    """Persist calendar-month budget periods."""

    def get_or_create(
        self, *, year: int, month: int, currency: str
    ) -> BudgetPeriod: ...

    def get(self, period_id: int) -> BudgetPeriod | None: ...

    def find(self, *, year: int, month: int, currency: str) -> BudgetPeriod | None: ...

    def close(self, period_id: int, *, closed_at: datetime) -> BudgetPeriod: ...


class BudgetTargetRepository(Protocol):
    """Persist monthly targets, buffers, and their funding wallets."""

    def upsert(self, draft: BudgetTargetDraft) -> BudgetTarget: ...

    def get(self, target_id: int) -> BudgetTarget | None: ...

    def list_for_period(self, period_id: int) -> tuple[BudgetTarget, ...]: ...


class EventAllocationRepository(Protocol):
    """Persist the shares one financial event is split into."""

    def add(self, draft: EventAllocationDraft) -> EventAllocation: ...

    def list_for_event(self, event_id: int) -> tuple[EventAllocation, ...]: ...

    def list_for_entry(self, entry_id: int) -> tuple[EventAllocation, ...]: ...

    def attach_entry(
        self, allocation_ids: tuple[int, ...], entry_id: int
    ) -> tuple[EventAllocation, ...]: ...

    def add_dedicated(
        self, draft: DedicatedWalletAllocationDraft
    ) -> DedicatedWalletAllocation: ...

    def get_dedicated(
        self, event_allocation_id: int
    ) -> DedicatedWalletAllocation | None: ...

    def list_dedicated_for_event(
        self, event_id: int
    ) -> tuple[DedicatedWalletAllocation, ...]: ...


class BudgetLedgerRepository(Protocol):
    """Aggregate posted ledger facts inside the caller's transaction."""

    def net_spend_by_category(
        self, *, start: datetime, end: datetime, currency: str
    ) -> Mapping[int, int]: ...

    def wallet_balance(self, wallet_id: int) -> int: ...

    def reserved_minor(self, wallet_id: int) -> int: ...


class SubscriptionRepository(Protocol):
    """Persist the canonical register of recurring services."""

    def add(self, draft: SubscriptionDraft) -> Subscription: ...

    def get(self, subscription_id: int) -> Subscription | None: ...

    def list_by_name(self, name: str) -> tuple[Subscription, ...]: ...

    def list_all(self) -> tuple[Subscription, ...]: ...

    def list_by_currency(self, currency: str) -> tuple[Subscription, ...]: ...

    def list_currencies(self) -> tuple[str, ...]: ...

    def save(self, change: SubscriptionUpdate) -> Subscription: ...


class SubscriptionPricePeriodRepository(Protocol):
    """Persist effective-dated prices whose ranges never overlap."""

    def add(self, draft: SubscriptionPricePeriodDraft) -> SubscriptionPricePeriod: ...

    def get(self, price_period_id: int) -> SubscriptionPricePeriod | None: ...

    def list_for_subscription(
        self, subscription_id: int
    ) -> tuple[SubscriptionPricePeriod, ...]: ...

    def list_overlapping(
        self,
        *,
        subscription_id: int,
        effective_from: date,
        effective_to: date | None,
        exclude_id: int | None = None,
    ) -> tuple[SubscriptionPricePeriod, ...]: ...

    def save(
        self, change: SubscriptionPricePeriodUpdate
    ) -> SubscriptionPricePeriod: ...


class SalaryPlanTemplateRepository(Protocol):
    """Persist the current salary distribution templates."""

    def add(self, draft: SalaryPlanTemplateDraft) -> SalaryPlanTemplate: ...

    def get(self, template_id: int) -> SalaryPlanTemplate | None: ...

    def list_all(self) -> tuple[SalaryPlanTemplate, ...]: ...

    def save(self, change: SalaryPlanTemplateUpdate) -> SalaryPlanTemplate: ...

    def touch_version(
        self, template_id: int, expected_version: int
    ) -> SalaryPlanTemplate: ...

    def has_items_or_runs(self, template_id: int) -> bool: ...


class SalaryPlanTemplateItemRepository(Protocol):
    """Persist the destinations one template distributes a salary into."""

    def add(self, draft: SalaryPlanTemplateItemDraft) -> SalaryPlanTemplateItem: ...

    def get(self, template_item_id: int) -> SalaryPlanTemplateItem | None: ...

    def list_for_template(
        self, template_id: int, *, active_only: bool = False
    ) -> tuple[SalaryPlanTemplateItem, ...]: ...

    def save(self, change: SalaryPlanTemplateItemUpdate) -> SalaryPlanTemplateItem: ...

    def deactivate(
        self, template_item_id: int, expected_version: int
    ) -> SalaryPlanTemplateItem: ...

    def find_active_conflict(
        self,
        *,
        template_id: int,
        name: str,
        destination_wallet_id: int,
        exclude_id: int | None = None,
    ) -> SalaryPlanTemplateItem | None: ...

    def is_referenced(self, template_item_id: int) -> bool: ...


class SalaryAllocationRunRepository(Protocol):
    """Persist frozen monthly salary distributions; snapshots never change."""

    def add(self, draft: SalaryAllocationRunDraft) -> SalaryAllocationRun: ...

    def get(self, run_id: int) -> SalaryAllocationRun | None: ...

    def find_live(
        self, *, template_id: int, budget_period_id: int
    ) -> SalaryAllocationRun | None: ...

    def find_live_by_salary_event(
        self, salary_event_id: int
    ) -> SalaryAllocationRun | None: ...

    def find_regenerated_from(self, run_id: int) -> SalaryAllocationRun | None: ...

    def list_for_period(
        self, budget_period_id: int
    ) -> tuple[SalaryAllocationRun, ...]: ...

    def list_generations(
        self, *, template_id: int, budget_period_id: int
    ) -> tuple[SalaryAllocationRun, ...]: ...

    def next_generation(self, *, template_id: int, budget_period_id: int) -> int: ...

    def cancel_draft(
        self, run_id: int, *, expected_version: int, cancelled_at: datetime
    ) -> SalaryAllocationRun: ...

    def approve(
        self,
        run_id: int,
        *,
        expected_version: int,
        status: SalaryRunStatus,
        approval_entry_id: int | None,
        approved_at: datetime,
    ) -> SalaryAllocationRun: ...

    def refresh_status(
        self,
        run_id: int,
        *,
        expected_version: int,
        expected_status: SalaryRunStatus,
        status: SalaryRunStatus,
    ) -> SalaryAllocationRun: ...


class SalaryAllocationRunItemRepository(Protocol):
    """Persist the frozen lines of one run and their fulfillment state."""

    def add(self, draft: SalaryAllocationRunItemDraft) -> SalaryAllocationRunItem: ...

    def get(self, run_item_id: int) -> SalaryAllocationRunItem | None: ...

    def list_for_run(self, run_id: int) -> tuple[SalaryAllocationRunItem, ...]: ...

    def transition(
        self,
        run_item_id: int,
        *,
        expected_version: int,
        expected_status: SalaryRunItemStatus,
        status: SalaryRunItemStatus,
        transfer_entry_id: int | None = None,
        transferred_at: datetime | None = None,
    ) -> SalaryAllocationRunItem: ...

    def get_by_transfer_entry(
        self, entry_id: int
    ) -> SalaryAllocationRunItem | None: ...

    def list_waiting_for_source_wallet(
        self, wallet_id: int
    ) -> tuple[SalaryAllocationRunItem, ...]: ...

    def reserved_minor_for_wallet(self, wallet_id: int) -> int: ...


class FundingCommitmentRepository(Protocol):
    """Persist reservations; a commitment never moves money by itself."""

    def add(self, draft: FundingCommitmentDraft) -> FundingCommitment: ...

    def get(self, commitment_id: int) -> FundingCommitment | None: ...

    def list_for_event(self, event_id: int) -> tuple[FundingCommitment, ...]: ...

    def list_open_for_wallet(self, wallet_id: int) -> tuple[FundingCommitment, ...]: ...

    def list_open_for_settlement_account(
        self, settlement_account_id: int
    ) -> tuple[FundingCommitment, ...]: ...

    def release(self, commitment_id: int, amount_minor: int) -> FundingCommitment: ...

    def refresh_status(self, commitment_id: int) -> FundingCommitment: ...


class ScheduledTransferRepository(Protocol):
    """Persist assumed bank movements; economic columns freeze on insert."""

    def add(self, draft: ScheduledTransferDraft) -> ScheduledTransfer: ...

    def get(self, scheduled_transfer_id: int) -> ScheduledTransfer | None: ...

    def get_by_idempotency_key(self, key: str) -> ScheduledTransfer | None: ...

    def list_by_status(
        self,
        statuses: frozenset[ScheduledTransferStatus],
        *,
        limit: int,
        offset: int = 0,
    ) -> tuple[ScheduledTransfer, ...]: ...

    def count_by_status(self, statuses: frozenset[ScheduledTransferStatus]) -> int: ...

    def count_due(self, now: datetime) -> int: ...

    def list_due(
        self, now: datetime, *, limit: int
    ) -> tuple[ScheduledTransfer, ...]: ...

    def claim_due(
        self,
        *,
        now: datetime,
        owner: str,
        lease_until: datetime,
        limit: int,
    ) -> tuple[ScheduledTransfer, ...]: ...

    def finish(
        self,
        scheduled_transfer_id: int,
        *,
        expected_version: int,
        lease_owner: str,
        status: ScheduledTransferStatus,
        executed_entry_id: int | None = None,
        processed_at: datetime | None = None,
        next_attempt_at: datetime | None = None,
        last_error_code: str | None = None,
        last_error_message: str | None = None,
    ) -> ScheduledTransfer: ...

    def transition(
        self,
        scheduled_transfer_id: int,
        *,
        expected_version: int,
        expected_status: ScheduledTransferStatus,
        status: ScheduledTransferStatus,
        cancelled_at: datetime | None = None,
        reversal_entry_id: int | None = None,
        reversed_at: datetime | None = None,
        clear_error: bool = False,
    ) -> ScheduledTransfer: ...


class ScheduledTransferCommitmentRepository(Protocol):
    """Persist immutable coverage links between schedules and commitments."""

    def add(
        self, draft: ScheduledTransferCommitmentDraft
    ) -> ScheduledTransferCommitment: ...

    def list_for_schedule(
        self, scheduled_transfer_id: int
    ) -> tuple[ScheduledTransferCommitment, ...]: ...

    def list_for_commitment(
        self, funding_commitment_id: int
    ) -> tuple[ScheduledTransferCommitment, ...]: ...

    def covered_commitment_ids(self, scheduled_transfer_id: int) -> tuple[int, ...]: ...

    def coverage(self, funding_commitment_id: int) -> tuple[int, int]: ...

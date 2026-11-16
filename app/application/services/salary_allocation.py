"""Distribute one month's salary from a versioned plan into frozen run items.

Every use case here commits in a single Unit of Work: managing the plan,
recording the salary that arrived, generating the month, approving it, and
confirming a real bank transfer. Generation reads the subscription register
inside the same transaction that freezes it, so a run can never store an
amount that a second, later snapshot produced (ADR-B045).

Approval is where the two kinds of destination separate. A wallet inside the
salary account is reclassified immediately, because no money leaves the bank. A
wallet in any other account — even at the same institution — becomes a
``waiting_transfer`` line, which is itself the reservation (ADR-B043). Only the
user pressing "已轉帳" records the real movement (ADR-B047); nothing here ever
assumes a bank did anything.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime

from app.application.queries.subscriptions import SubscriptionRequirementInput
from app.application.services.ledger import JournalService
from app.application.services.subscriptions import calculate_requirement
from app.domain.budget_enums import (
    BudgetPeriodStatus,
    CommitmentFulfillmentMode,
    FinancialEventOrigin,
    FinancialEventStatus,
    FinancialEventType,
    JournalEntryType,
    LedgerAccountKind,
    PaymentMethod,
    RecognitionBasis,
    SalaryCalculationMode,
    SalaryRunItemStatus,
    SalaryRunStatus,
)
from app.domain.budget_models import (
    SALARY_EVIDENCE_SCHEMA_VERSION,
    BudgetPeriod,
    FinancialAccount,
    FinancialEvent,
    FinancialEventDraft,
    FixedSalaryItemEvidence,
    JournalEntry,
    LedgerAccount,
    LedgerAccountDraft,
    ManualSalaryAmount,
    ManualSalaryItemEvidence,
    Money,
    PostingDraft,
    SalaryAllocationRun,
    SalaryAllocationRunDraft,
    SalaryAllocationRunItem,
    SalaryAllocationRunItemDraft,
    SalaryItemEvidence,
    SalaryPlanTemplate,
    SalaryPlanTemplateDraft,
    SalaryPlanTemplateItem,
    SalaryPlanTemplateItemDraft,
    SalaryPlanTemplateItemUpdate,
    SalaryPlanTemplateUpdate,
    SalarySubscriptionEvidence,
    SubscriptionSalaryItemEvidence,
    Wallet,
    normalize_currency,
    validate_event_magnitude,
)
from app.domain.budget_rules import (
    SalaryPostingView,
    approved_item_status,
    derive_run_status,
    ensure_run_item_transition,
    ensure_run_transition,
    holds_wallets,
    month_start,
    price_period_covers,
    salary_approval_journal_key,
    salary_event_problem,
    salary_fulfillment_mode_for,
    salary_income_ledger_code,
    salary_planned_total,
    salary_receipt_event_key,
    salary_receipt_journal_key,
    salary_receipt_replay_problem,
    salary_regeneration_fingerprint,
    salary_remainder_minor,
    salary_transfer_journal_key,
    serialize_item_evidence,
    serialize_subscription_evidence,
)
from app.domain.errors import (
    BudgetPeriodError,
    ConcurrentUpdateError,
    CurrencyMismatchError,
    IdempotencyConflictError,
    InsufficientAvailableFundsError,
    LedgerConfigurationError,
    SalaryAllocationError,
    SalaryEventError,
    SalaryPlanError,
)
from app.domain.ports.repositories import UnitOfWork

type UnitOfWorkFactory = Callable[[], UnitOfWork]
type Clock = Callable[[], datetime]

SALARY_INCOME_NAME = "Salary income"
_APPROVAL_DESCRIPTION = "Salary allocation"
_TRANSFER_DESCRIPTION = "Salary allocation transfer"
_RECEIPT_DESCRIPTION = "Salary receipt"
#: Statuses an approval has already reached. Retrying then replays the first
#: outcome instead of posting a second entry or reserving twice.
_SETTLED_RUN_STATUSES = frozenset(
    {
        SalaryRunStatus.APPROVED,
        SalaryRunStatus.PARTIALLY_FUNDED,
        SalaryRunStatus.FUNDED,
    }
)


@dataclass(frozen=True)
class AddSalaryTemplateRequest:
    """One new plan for distributing a salary account's income."""

    name: str
    currency: str
    source_account_id: int


@dataclass(frozen=True)
class UpdateSalaryTemplateRequest:
    """One optimistic rename or activation change of a template."""

    template_id: int
    expected_version: int
    name: str
    is_active: bool


@dataclass(frozen=True)
class AddSalaryTemplateItemRequest:
    """One new destination inside an existing template.

    ``expected_template_version`` is optional so a command line does not have
    to quote it. When it is given — as the web form always does — a template
    that moved since the page was rendered is refused instead of silently
    accepting an item into a plan the user was not looking at.
    """

    template_id: int
    name: str
    destination_wallet_id: int
    calculation_mode: SalaryCalculationMode
    budget_category_id: int | None = None
    fixed_amount_minor: int | None = None
    display_order: int = 0
    expected_template_version: int | None = None


@dataclass(frozen=True)
class UpdateSalaryTemplateItemRequest:
    """One optimistic correction of an existing destination."""

    template_item_id: int
    expected_version: int
    name: str
    destination_wallet_id: int
    calculation_mode: SalaryCalculationMode
    budget_category_id: int | None
    fixed_amount_minor: int | None
    display_order: int
    is_active: bool = True
    expected_template_version: int | None = None


@dataclass(frozen=True)
class SalaryAmountEdit:
    """One amount as it was typed into the plan sheet."""

    template_item_id: int
    expected_version: int
    fixed_amount_minor: int


@dataclass(frozen=True)
class SetSalaryAmountsRequest:
    """Every amount one submission of the plan sheet carried.

    The sheet posts the whole column, so most edits arrive unchanged. Only the
    rows whose amount actually moved are written, which keeps one typed
    correction from bumping the version of fifteen untouched destinations.
    """

    template_id: int
    expected_template_version: int
    edits: tuple[SalaryAmountEdit, ...]


@dataclass(frozen=True)
class DeactivateSalaryTemplateItemRequest:
    """Stop using one destination without deleting the months that used it."""

    template_item_id: int
    expected_version: int
    expected_template_version: int | None = None


@dataclass(frozen=True)
class SalaryReceiptRequest:
    """The salary that actually arrived in the configured salary account.

    This is the one narrow bank-income entry point B5 owns. The general
    deposit and withdrawal surface is B7's, and is deliberately absent here.
    """

    amount_minor: int
    occurred_at: datetime
    client_token: str
    template_id: int | None = None
    source_account_id: int | None = None
    note: str | None = None


@dataclass(frozen=True)
class SalaryReceiptResult:
    """What recording one salary committed, replayed identically on a retry."""

    event: FinancialEvent
    entry: JournalEntry
    account: FinancialAccount
    wallet: Wallet
    created: bool


@dataclass(frozen=True)
class GenerateSalaryRunRequest:
    """Generate one month from the template as it is configured today."""

    template_id: int
    year: int
    month: int
    salary_event_id: int
    manual_amounts: tuple[ManualSalaryAmount, ...] = ()


@dataclass(frozen=True)
class RegenerateSalaryRunRequest:
    """Replace one draft with a new generation of the same month.

    The draft is named by ID, so resubmitting the request after it has already
    been replaced replays the new generation instead of producing a third one
    (ADR-B048).
    """

    run_id: int
    expected_version: int
    manual_amounts: tuple[ManualSalaryAmount, ...] = ()


@dataclass(frozen=True)
class GenerateSalaryRunResult:
    """One frozen generation and the lines it froze."""

    run: SalaryAllocationRun
    items: tuple[SalaryAllocationRunItem, ...]
    created: bool


@dataclass(frozen=True)
class CancelSalaryDraftRequest:
    """Cancel one draft; its snapshot is kept, never deleted."""

    run_id: int
    expected_version: int


@dataclass(frozen=True)
class ApproveSalaryRunRequest:
    """Approve one draft under the exact version the page displayed."""

    run_id: int
    expected_version: int


@dataclass(frozen=True)
class ApproveSalaryRunResult:
    """What approving one run committed; ``entry`` is the wallet move only."""

    run: SalaryAllocationRun
    items: tuple[SalaryAllocationRunItem, ...]
    entry: JournalEntry | None
    created: bool


@dataclass(frozen=True)
class MarkSalaryTransferredRequest:
    """The user states that one cross-account transfer really happened."""

    run_item_id: int
    expected_version: int
    effective_at: datetime


@dataclass(frozen=True)
class MarkSalaryTransferredResult:
    """What confirming one transfer committed, replayed on a retry."""

    run: SalaryAllocationRun
    item: SalaryAllocationRunItem
    entry: JournalEntry
    created: bool


@dataclass(frozen=True)
class SalaryDestination:
    """One resolved destination, validated against the plan's currency."""

    wallet: Wallet
    account: FinancialAccount
    ledger_account: LedgerAccount


def _require_aware(value: datetime, *, label: str) -> datetime:
    """Reject a naive instant; a local time must be resolved before it lands."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value


def _require_template(unit: UnitOfWork, template_id: int) -> SalaryPlanTemplate:
    template = unit.salary_templates.get(template_id)
    if template is None:
        raise SalaryPlanError("salary template was not found")
    return template


def _require_source(
    unit: UnitOfWork, account_id: int, *, currency: str
) -> SalaryDestination:
    """Resolve the salary account's Unallocated wallet and ledger identity.

    Salary lands in Unallocated and is distributed from there, so a template
    whose account has no usable Unallocated wallet cannot plan anything.
    """
    account = unit.financial_accounts.get(account_id)
    if account is None:
        raise SalaryPlanError("the salary account was not found")
    if not account.is_active:
        raise SalaryPlanError("the salary account is not active")
    if not holds_wallets(account.account_type):
        raise SalaryPlanError("a salary is received into an asset account")
    if account.currency != currency:
        raise CurrencyMismatchError("the salary account holds another currency")
    wallet = unit.wallets.get_unallocated(account.id)
    if wallet is None:
        raise SalaryPlanError("the salary account has no active Unallocated wallet")
    ledger_account = unit.ledger_accounts.get_for_financial_account(account.id)
    if ledger_account is None:
        raise LedgerConfigurationError(
            "the salary account has no physical ledger account"
        )
    return SalaryDestination(
        wallet=wallet, account=account, ledger_account=ledger_account
    )


def _require_destination(
    unit: UnitOfWork, wallet_id: int, *, currency: str
) -> SalaryDestination:
    """Resolve one destination wallet and refuse an unusable funding path."""
    wallet = unit.wallets.get(wallet_id)
    if wallet is None:
        raise SalaryPlanError("the destination wallet was not found")
    if not wallet.is_active:
        raise SalaryPlanError("the destination wallet is not active")
    account = unit.financial_accounts.get(wallet.financial_account_id)
    if account is None:  # pragma: no cover - a wallet always has an account
        raise LedgerConfigurationError("the destination account is missing")
    if not account.is_active:
        raise SalaryPlanError("the destination account is not active")
    if not holds_wallets(account.account_type):
        raise SalaryPlanError("a salary line pays into an asset account")
    if account.currency != currency:
        raise CurrencyMismatchError("the destination wallet holds another currency")
    ledger_account = unit.ledger_accounts.get_for_financial_account(account.id)
    if ledger_account is None:
        raise LedgerConfigurationError(
            "the destination account has no physical ledger account"
        )
    return SalaryDestination(
        wallet=wallet, account=account, ledger_account=ledger_account
    )


def _require_category_name(unit: UnitOfWork, category_id: int | None) -> str | None:
    """Return one category's name, refusing a deactivated choice."""
    if category_id is None:
        return None
    category = unit.budget_categories.get(category_id)
    if category is None:
        raise LedgerConfigurationError("budget category was not found")
    if not category.is_active:
        raise LedgerConfigurationError(
            "an inactive budget category cannot receive a salary line"
        )
    return category.name


def _posting_views(
    unit: UnitOfWork, entry: JournalEntry
) -> tuple[SalaryPostingView, ...]:
    """Reduce one posted entry to what the salary eligibility rule reads."""
    views: list[SalaryPostingView] = []
    for posting in entry.postings:
        ledger_account = unit.ledger_accounts.get(posting.ledger_account_id)
        if ledger_account is None:  # pragma: no cover - guarded by foreign key
            raise LedgerConfigurationError("ledger account was not found")
        views.append(
            SalaryPostingView(
                ledger_account_id=ledger_account.id,
                ledger_kind=ledger_account.kind,
                financial_account_id=ledger_account.financial_account_id,
                wallet_id=posting.wallet_id,
                amount_minor=posting.amount_minor,
                currency=posting.currency,
                budget_category_id=posting.budget_category_id,
            )
        )
    return tuple(views)


def _salary_event_problem(
    unit: UnitOfWork,
    event: FinancialEvent,
    *,
    source: SalaryDestination,
    currency: str,
    allow_run_id: int | None = None,
) -> str | None:
    """Return why this event cannot fund a run in this account, or None."""
    entry = unit.journal.get_by_financial_event(event.id)
    live = unit.salary_runs.find_live_by_salary_event(event.id)
    return salary_event_problem(
        event_type=event.event_type,
        status=event.status,
        currency=event.currency,
        amount_minor=event.amount_minor,
        expected_currency=currency,
        source_account_id=source.account.id,
        source_wallet_id=source.wallet.id,
        entry_type=None if entry is None else entry.entry_type,
        postings=() if entry is None else _posting_views(unit, entry),
        already_used=live is not None and live.id != allow_run_id,
    )


def subscription_requirement_inputs(
    unit: UnitOfWork, *, currency: str, selected_month: date
) -> tuple[SubscriptionRequirementInput, ...]:
    """Read the register through the caller's transaction, not a new session.

    B4's read repository opens its own short session, which would make the
    amounts a run freezes come from a different snapshot than the one the run
    is written into. Reading here keeps generation atomic (ADR-B045).
    """
    entries: list[SubscriptionRequirementInput] = []
    for subscription in unit.subscriptions.list_by_currency(currency):
        wallet = unit.wallets.get(subscription.funding_wallet_id)
        category = unit.budget_categories.get(subscription.budget_category_id)
        if wallet is None or category is None:  # pragma: no cover - foreign keys
            raise LedgerConfigurationError(
                "a subscription points at a wallet or category that is gone"
            )
        price = next(
            (
                period
                for period in unit.subscription_prices.list_for_subscription(
                    subscription.id
                )
                if price_period_covers(
                    effective_from=period.effective_from,
                    effective_to=period.effective_to,
                    selected_month=selected_month,
                )
            ),
            None,
        )
        entries.append(
            SubscriptionRequirementInput(
                subscription=subscription,
                budget_category_name=category.name,
                funding_wallet_name=wallet.name,
                price=price,
            )
        )
    return tuple(
        sorted(
            entries,
            key=lambda entry: (entry.subscription.name, entry.subscription.id),
        )
    )


class ManageSalaryTemplates:
    """Create and correct the plan future months are generated from."""

    def __init__(self, unit_of_work_factory: UnitOfWorkFactory) -> None:
        self._unit_of_work_factory = unit_of_work_factory

    def add_template(self, request: AddSalaryTemplateRequest) -> SalaryPlanTemplate:
        """Register one template after validating its salary account."""
        draft = SalaryPlanTemplateDraft(
            name=request.name,
            currency=request.currency,
            source_account_id=request.source_account_id,
        )
        with self._unit_of_work_factory() as unit:
            _require_source(unit, draft.source_account_id, currency=draft.currency)
            template = unit.salary_templates.add(draft)
            unit.commit()
        return template

    def update_template(
        self, request: UpdateSalaryTemplateRequest
    ) -> SalaryPlanTemplate:
        """Rename or deactivate one template under its exact version.

        The currency and salary account are not editable. A template that has
        already produced a line describes how money was moved, and pointing it
        at another bank would reinterpret every one of those lines; use a new
        template instead (ADR-B042).
        """
        with self._unit_of_work_factory() as unit:
            template = unit.salary_templates.save(
                SalaryPlanTemplateUpdate(
                    template_id=request.template_id,
                    expected_version=request.expected_version,
                    name=request.name,
                    is_active=request.is_active,
                )
            )
            unit.commit()
        return template

    def deactivate_template(
        self, template_id: int, expected_version: int
    ) -> SalaryPlanTemplate:
        """Stop offering one template without touching what it produced."""
        with self._unit_of_work_factory() as unit:
            current = _require_template(unit, template_id)
            template = unit.salary_templates.save(
                SalaryPlanTemplateUpdate(
                    template_id=template_id,
                    expected_version=expected_version,
                    name=current.name,
                    is_active=False,
                )
            )
            unit.commit()
        return template

    def add_item(self, request: AddSalaryTemplateItemRequest) -> SalaryPlanTemplateItem:
        """Add one destination and move the whole template to a new version."""
        draft = SalaryPlanTemplateItemDraft(
            template_id=request.template_id,
            name=request.name,
            destination_wallet_id=request.destination_wallet_id,
            calculation_mode=request.calculation_mode,
            budget_category_id=request.budget_category_id,
            fixed_amount_minor=request.fixed_amount_minor,
            display_order=request.display_order,
        )
        with self._unit_of_work_factory() as unit:
            template = _require_template(unit, draft.template_id)
            self._validate_item(
                unit,
                template,
                destination_wallet_id=draft.destination_wallet_id,
                budget_category_id=draft.budget_category_id,
                name=draft.name,
                exclude_id=None,
            )
            item = unit.salary_template_items.add(draft)
            self._bump_template(unit, template, request.expected_template_version)
            unit.commit()
        return item

    def update_item(
        self, request: UpdateSalaryTemplateItemRequest
    ) -> SalaryPlanTemplateItem:
        """Correct one destination under its exact version."""
        change = SalaryPlanTemplateItemUpdate(
            template_item_id=request.template_item_id,
            expected_version=request.expected_version,
            name=request.name,
            destination_wallet_id=request.destination_wallet_id,
            calculation_mode=request.calculation_mode,
            budget_category_id=request.budget_category_id,
            fixed_amount_minor=request.fixed_amount_minor,
            display_order=request.display_order,
            is_active=request.is_active,
        )
        with self._unit_of_work_factory() as unit:
            current = unit.salary_template_items.get(change.template_item_id)
            if current is None:
                raise SalaryPlanError("salary template item was not found")
            template = _require_template(unit, current.template_id)
            if change.is_active:
                self._validate_item(
                    unit,
                    template,
                    destination_wallet_id=change.destination_wallet_id,
                    budget_category_id=change.budget_category_id,
                    name=change.name,
                    exclude_id=current.id,
                )
            item = unit.salary_template_items.save(change)
            self._bump_template(unit, template, request.expected_template_version)
            unit.commit()
        return item

    def set_amounts(
        self, request: SetSalaryAmountsRequest
    ) -> tuple[SalaryPlanTemplateItem, ...]:
        """Apply the amounts one plan sheet submission changed.

        Only the amount moves here. A destination's wallet, calculation mode,
        and category are structural decisions that keep their own form, so a
        sheet edit can never silently redirect money to another envelope.

        Nothing is recalculated for a month that already exists: a generated
        run froze the plan it used, so this changes what the *next* generation
        will do and leaves every frozen month alone (ADR-B045).
        """
        with self._unit_of_work_factory() as unit:
            template = _require_template(unit, request.template_id)
            changed: list[SalaryPlanTemplateItem] = []
            for edit in request.edits:
                current = unit.salary_template_items.get(edit.template_item_id)
                if current is None or current.template_id != template.id:
                    raise SalaryPlanError(
                        "an amount was submitted for an item of another plan"
                    )
                if current.calculation_mode is not SalaryCalculationMode.FIXED:
                    raise SalaryPlanError(
                        "only a fixed destination carries a typed amount; "
                        f"'{current.name}' is calculated"
                    )
                if not current.is_active:
                    raise SalaryPlanError(
                        f"'{current.name}' is no longer part of this plan"
                    )
                if current.fixed_amount_minor == edit.fixed_amount_minor:
                    continue
                changed.append(
                    unit.salary_template_items.save(
                        SalaryPlanTemplateItemUpdate(
                            template_item_id=current.id,
                            expected_version=edit.expected_version,
                            name=current.name,
                            destination_wallet_id=current.destination_wallet_id,
                            calculation_mode=current.calculation_mode,
                            budget_category_id=current.budget_category_id,
                            fixed_amount_minor=edit.fixed_amount_minor,
                            display_order=current.display_order,
                            is_active=current.is_active,
                        )
                    )
                )
            if changed:
                self._bump_template(unit, template, request.expected_template_version)
            unit.commit()
        return tuple(changed)

    def deactivate_item(
        self, request: DeactivateSalaryTemplateItemRequest
    ) -> SalaryPlanTemplateItem:
        """Stop using one destination; the months that used it stay readable."""
        with self._unit_of_work_factory() as unit:
            current = unit.salary_template_items.get(request.template_item_id)
            if current is None:
                raise SalaryPlanError("salary template item was not found")
            template = _require_template(unit, current.template_id)
            item = unit.salary_template_items.deactivate(
                request.template_item_id, request.expected_version
            )
            self._bump_template(unit, template, request.expected_template_version)
            unit.commit()
        return item

    @staticmethod
    def _bump_template(
        unit: UnitOfWork, template: SalaryPlanTemplate, expected: int | None
    ) -> None:
        """Move the template to a new version because its plan changed.

        The statement is conditional either way, so an interleaved writer
        cannot slip between the read and the write; supplying ``expected``
        additionally refuses a form rendered before someone else edited it.
        """
        unit.salary_templates.touch_version(
            template.id, template.version if expected is None else expected
        )

    @staticmethod
    def _validate_item(
        unit: UnitOfWork,
        template: SalaryPlanTemplate,
        *,
        destination_wallet_id: int,
        budget_category_id: int | None,
        name: str,
        exclude_id: int | None,
    ) -> None:
        """Refuse a destination the plan cannot actually pay into."""
        source = _require_source(
            unit, template.source_account_id, currency=template.currency
        )
        if destination_wallet_id == source.wallet.id:
            raise SalaryPlanError(
                "the salary account's Unallocated wallet is where the remainder "
                "already stays; it is never a destination of its own plan"
            )
        _require_destination(unit, destination_wallet_id, currency=template.currency)
        _require_category_name(unit, budget_category_id)
        conflict = unit.salary_template_items.find_active_conflict(
            template_id=template.id,
            name=name,
            destination_wallet_id=destination_wallet_id,
            exclude_id=exclude_id,
        )
        if conflict is not None:
            if conflict.destination_wallet_id == destination_wallet_id:
                raise SalaryPlanError(
                    "this template already sends money to that wallet"
                )
            raise SalaryPlanError("this template already has an item with that name")


class RecordSalaryReceipt:
    """Record the salary that actually arrived, exactly once.

    This is not a general deposit form. It posts one balanced entry crediting
    a salary income account and debiting the configured salary account's
    Unallocated wallet, so the money exists in the ledger before any plan
    distributes it.
    """

    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        journal: JournalService,
        *,
        clock: Clock = lambda: datetime.now(UTC),
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._journal = journal
        self._clock = clock

    def execute(self, request: SalaryReceiptRequest) -> SalaryReceiptResult:
        """Create the event and its entry, or replay the first attempt."""
        token = request.client_token.strip()
        if not token:
            raise ValueError("a client idempotency token is required")
        # Validated before anything is compared, so a replay can never match a
        # stored integer against `True` or a float that happens to be equal.
        amount_minor = validate_event_magnitude(request.amount_minor)
        occurred_at = _require_aware(request.occurred_at, label="salary receipt time")
        key = salary_receipt_event_key(token)
        with self._unit_of_work_factory() as unit:
            source, currency = self._resolve_account(unit, request)
            existing = unit.financial_events.get_by_idempotency_key(key)
            if existing is not None:
                return self._replay(
                    unit,
                    existing,
                    source=source,
                    currency=currency,
                    amount_minor=amount_minor,
                    occurred_at=occurred_at,
                )
            event = unit.financial_events.add(
                FinancialEventDraft(
                    origin=FinancialEventOrigin.MANUAL,
                    event_type=FinancialEventType.DEPOSIT,
                    payment_method=PaymentMethod.BANK_ACCOUNT,
                    recognition_basis=RecognitionBasis.OBSERVED_MANUAL,
                    occurred_at=occurred_at,
                    amount_minor=amount_minor,
                    currency=currency,
                    idempotency_key=key,
                    status=FinancialEventStatus.READY,
                    note=request.note,
                )
            )
            income = self._salary_income_account(unit, currency)
            entry = self._journal.stage_balanced(
                unit,
                entry_type=JournalEntryType.SALARY_RECEIPT,
                effective_at=occurred_at,
                idempotency_key=salary_receipt_journal_key(event.id),
                description=_RECEIPT_DESCRIPTION,
                postings=(
                    PostingDraft(
                        ledger_account_id=source.ledger_account.id,
                        amount=Money(event.amount_minor, currency),
                        wallet_id=source.wallet.id,
                    ),
                    PostingDraft(
                        ledger_account_id=income.id,
                        amount=Money(-event.amount_minor, currency),
                    ),
                ),
                financial_event_id=event.id,
            )
            posted = unit.financial_events.mark_posted(event.id, event.version)
            unit.commit()
        return SalaryReceiptResult(
            event=posted,
            entry=entry,
            account=source.account,
            wallet=source.wallet,
            created=True,
        )

    @staticmethod
    def _resolve_account(
        unit: UnitOfWork, request: SalaryReceiptRequest
    ) -> tuple[SalaryDestination, str]:
        """Name the account explicitly or take it from a template."""
        if request.template_id is not None:
            template = _require_template(unit, request.template_id)
            if (
                request.source_account_id is not None
                and request.source_account_id != template.source_account_id
            ):
                raise SalaryPlanError(
                    "this account is not the one the template receives into"
                )
            return (
                _require_source(
                    unit, template.source_account_id, currency=template.currency
                ),
                template.currency,
            )
        if request.source_account_id is None:
            raise SalaryPlanError("a salary receipt names its template or its account")
        account = unit.financial_accounts.get(request.source_account_id)
        if account is None:
            raise SalaryPlanError("the salary account was not found")
        return (
            _require_source(unit, account.id, currency=account.currency),
            account.currency,
        )

    def _replay(
        self,
        unit: UnitOfWork,
        existing: FinancialEvent,
        *,
        source: SalaryDestination,
        currency: str,
        amount_minor: int,
        occurred_at: datetime,
    ) -> SalaryReceiptResult:
        """Return the first outcome, or refuse a token that records anything else.

        A matching idempotency key proves only that *something* once used this
        token. Replaying on that alone let an unrelated event — a card purchase
        with an adjustment entry, say — occupy a salary token and make the
        caller believe a salary had been recorded. The stored event and its
        whole entry are therefore re-verified against the shape this service
        writes before any replay is served.

        The note is deliberately not compared: it is display text and does not
        change the accounting effect, exactly as the journal's own idempotency
        check already treats a description.
        """
        entry = unit.journal.get_by_financial_event(existing.id)
        income = unit.ledger_accounts.get_by_code(
            salary_income_ledger_code(normalize_currency(currency))
        )
        problem = salary_receipt_replay_problem(
            origin=existing.origin,
            event_type=existing.event_type,
            payment_method=existing.payment_method,
            recognition_basis=existing.recognition_basis,
            status=existing.status,
            currency=existing.currency,
            amount_minor=existing.amount_minor,
            occurred_at=existing.occurred_at,
            event_id=existing.id,
            expected_currency=normalize_currency(currency),
            expected_amount_minor=amount_minor,
            expected_occurred_at=occurred_at,
            source_ledger_account_id=source.ledger_account.id,
            source_wallet_id=source.wallet.id,
            income_ledger_account_id=None if income is None else income.id,
            entry_type=None if entry is None else entry.entry_type,
            entry_effective_at=None if entry is None else entry.effective_at,
            entry_financial_event_id=(
                None if entry is None else entry.financial_event_id
            ),
            postings=() if entry is None else _posting_views(unit, entry),
        )
        if problem is not None:
            raise IdempotencyConflictError(
                f"this salary token already records something else ({problem})"
            )
        if entry is None:  # pragma: no cover - the rule already refused this
            raise SalaryEventError("this salary receipt has no journal entry")
        return SalaryReceiptResult(
            event=existing,
            entry=entry,
            account=source.account,
            wallet=source.wallet,
            created=False,
        )

    @staticmethod
    def _salary_income_account(unit: UnitOfWork, currency: str) -> LedgerAccount:
        """Return the single salary income identity of one currency."""
        code = salary_income_ledger_code(currency)
        existing = unit.ledger_accounts.get_by_code(code)
        if existing is not None:
            return existing
        return unit.ledger_accounts.add(
            LedgerAccountDraft(
                code=code,
                name=f"{SALARY_INCOME_NAME} ({currency})",
                kind=LedgerAccountKind.INCOME,
                currency=currency,
            )
        )


class GenerateSalaryAllocation:
    """Freeze one month's distribution of one salary, in one transaction."""

    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        *,
        clock: Clock = lambda: datetime.now(UTC),
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._clock = clock

    def execute(self, request: GenerateSalaryRunRequest) -> GenerateSalaryRunResult:
        """Generate one month, or replay the generation that already owns it.

        A second submission never recalculates. Returning the existing draft
        is the point: a template or a subscription price that changed in the
        meantime must not quietly alter a plan the user is already looking at.
        """
        with self._unit_of_work_factory() as unit:
            template = _require_template(unit, request.template_id)
            if not template.is_active:
                raise SalaryPlanError("this template is no longer in use")
            period = self._require_open_period(
                unit, year=request.year, month=request.month, currency=template.currency
            )
            live = unit.salary_runs.find_live(
                template_id=template.id, budget_period_id=period.id
            )
            if live is not None:
                if live.salary_event_id != request.salary_event_id:
                    raise SalaryAllocationError(
                        "this month already has a run for another salary; cancel "
                        "or regenerate it first"
                    )
                return GenerateSalaryRunResult(
                    run=live,
                    items=unit.salary_run_items.list_for_run(live.id),
                    created=False,
                )
            result = self._build(
                unit,
                template=template,
                period_id=period.id,
                year=request.year,
                month=request.month,
                salary_event_id=request.salary_event_id,
                manual_amounts=request.manual_amounts,
                generation=unit.salary_runs.next_generation(
                    template_id=template.id, budget_period_id=period.id
                ),
            )
            unit.commit()
        return result

    def regenerate(
        self, request: RegenerateSalaryRunRequest
    ) -> GenerateSalaryRunResult:
        """Replace one draft with the next generation of the same month.

        The old draft is cancelled and kept. If the new generation fails any
        validation, the whole transaction rolls back and the old draft is
        exactly as it was.
        """
        fingerprint = salary_regeneration_fingerprint(
            previous_run_id=request.run_id,
            expected_version=request.expected_version,
            manual_amounts=(
                (entry.template_item_id, entry.amount_minor)
                for entry in request.manual_amounts
            ),
        )
        with self._unit_of_work_factory() as unit:
            current = unit.salary_runs.get(request.run_id)
            if current is None:
                raise SalaryAllocationError("salary allocation run was not found")
            if current.status is SalaryRunStatus.CANCELLED:
                return self._replayed_regeneration(unit, current, fingerprint)
            if current.status is not SalaryRunStatus.DRAFT:
                raise SalaryAllocationError(
                    "an approved salary run is never regenerated; its lines "
                    "already describe money that was moved or reserved"
                )
            if current.version != request.expected_version:
                raise ConcurrentUpdateError("this run changed after the page loaded")
            template = _require_template(unit, current.template_id)
            if not template.is_active:
                raise SalaryPlanError("this template is no longer in use")
            period = unit.budget_periods.get(current.budget_period_id)
            if period is None:  # pragma: no cover - guarded by foreign key
                raise BudgetPeriodError("budget period was not found")
            if period.status is BudgetPeriodStatus.CLOSED:
                raise BudgetPeriodError("a closed month no longer accepts a salary run")
            unit.salary_runs.cancel_draft(
                current.id,
                expected_version=current.version,
                cancelled_at=self._clock(),
            )
            result = self._build(
                unit,
                template=template,
                period_id=period.id,
                year=period.year,
                month=period.month,
                salary_event_id=current.salary_event_id,
                manual_amounts=request.manual_amounts,
                generation=unit.salary_runs.next_generation(
                    template_id=template.id, budget_period_id=period.id
                ),
                regenerated_from_run_id=current.id,
                request_fingerprint=fingerprint,
            )
            unit.commit()
        return result

    def cancel(self, request: CancelSalaryDraftRequest) -> SalaryAllocationRun:
        """Cancel one draft, keeping its snapshot and every frozen line.

        A draft has neither posted nor reserved anything, so cancelling
        releases nothing and writes no journal entry.
        """
        with self._unit_of_work_factory() as unit:
            run = unit.salary_runs.cancel_draft(
                request.run_id,
                expected_version=request.expected_version,
                cancelled_at=self._clock(),
            )
            unit.commit()
        return run

    @staticmethod
    def _replayed_regeneration(
        unit: UnitOfWork, cancelled: SalaryAllocationRun, fingerprint: str
    ) -> GenerateSalaryRunResult:
        """Return the generation this exact request already produced.

        Provenance and fingerprint are both required. Looking only for a later
        live generation of the same month was wrong: a draft cancelled by hand
        and then replaced through the ordinary generate path would be reported
        as this request's own result, and the plan the caller asked for would
        never have been written (ADR-B049).
        """
        replacement = unit.salary_runs.find_regenerated_from(cancelled.id)
        if replacement is None:
            raise SalaryAllocationError(
                "this draft was already cancelled and nothing regenerated it; "
                "generate the month again"
            )
        if replacement.request_fingerprint != fingerprint:
            raise IdempotencyConflictError(
                "this draft was already regenerated from a different request"
            )
        if replacement.status is SalaryRunStatus.CANCELLED:
            raise SalaryAllocationError(
                "the generation that replaced this draft was itself replaced; "
                "reload the month before regenerating again"
            )
        return GenerateSalaryRunResult(
            run=replacement,
            items=unit.salary_run_items.list_for_run(replacement.id),
            created=False,
        )

    @staticmethod
    def _require_open_period(
        unit: UnitOfWork, *, year: int, month: int, currency: str
    ) -> BudgetPeriod:
        period = unit.budget_periods.get_or_create(
            year=year, month=month, currency=currency
        )
        if period.status is BudgetPeriodStatus.CLOSED:
            raise BudgetPeriodError("a closed month no longer accepts a salary run")
        return period

    def _build(
        self,
        unit: UnitOfWork,
        *,
        template: SalaryPlanTemplate,
        period_id: int,
        year: int,
        month: int,
        salary_event_id: int,
        manual_amounts: tuple[ManualSalaryAmount, ...],
        generation: int,
        regenerated_from_run_id: int | None = None,
        request_fingerprint: str | None = None,
    ) -> GenerateSalaryRunResult:
        """Calculate and freeze one whole generation inside the caller's work."""
        source = _require_source(
            unit, template.source_account_id, currency=template.currency
        )
        event = unit.financial_events.get(salary_event_id)
        if event is None:
            raise SalaryEventError("the salary event was not found")
        problem = _salary_event_problem(
            unit, event, source=source, currency=template.currency
        )
        if problem is not None:
            raise SalaryEventError(f"this event cannot fund a salary run ({problem})")
        items = unit.salary_template_items.list_for_template(
            template.id, active_only=True
        )
        evidence = SalarySubscriptionEvidence(
            schema_version=SALARY_EVIDENCE_SCHEMA_VERSION,
            year=year,
            month=month,
            currency=template.currency,
            items=calculate_requirement(
                subscription_requirement_inputs(
                    unit,
                    currency=template.currency,
                    selected_month=month_start(year, month),
                ),
                year=year,
                month=month,
                currency=template.currency,
            ).items,
        )
        manual = self._manual_by_item(manual_amounts, items)
        drafts = self._line_drafts(
            unit,
            template=template,
            source=source,
            items=items,
            evidence=evidence,
            manual=manual,
        )
        planned = salary_planned_total(draft.amount_minor for draft in drafts)
        remainder = salary_remainder_minor(
            salary_amount_minor=event.amount_minor, planned_amount_minor=planned
        )
        run = unit.salary_runs.add(
            SalaryAllocationRunDraft(
                template_id=template.id,
                template_name_snapshot=template.name,
                template_version=template.version,
                budget_period_id=period_id,
                salary_event_id=event.id,
                source_account_id=source.account.id,
                source_wallet_id=source.wallet.id,
                currency=template.currency,
                generation=generation,
                salary_amount_minor=event.amount_minor,
                planned_amount_minor=planned,
                remainder_minor=remainder,
                subscription_evidence_schema_version=SALARY_EVIDENCE_SCHEMA_VERSION,
                subscription_evidence_json=serialize_subscription_evidence(evidence),
                generated_at=self._clock(),
                regenerated_from_run_id=regenerated_from_run_id,
                request_fingerprint=request_fingerprint,
            )
        )
        frozen = tuple(
            unit.salary_run_items.add(
                SalaryAllocationRunItemDraft(
                    run_id=run.id,
                    template_item_id=draft.template_item_id,
                    template_item_version=draft.template_item_version,
                    name_snapshot=draft.name_snapshot,
                    destination_wallet_id=draft.destination_wallet_id,
                    destination_account_id=draft.destination_account_id,
                    destination_wallet_name_snapshot=(
                        draft.destination_wallet_name_snapshot
                    ),
                    destination_account_name_snapshot=(
                        draft.destination_account_name_snapshot
                    ),
                    budget_category_id=draft.budget_category_id,
                    budget_category_name_snapshot=draft.budget_category_name_snapshot,
                    calculation_mode=draft.calculation_mode,
                    amount_minor=draft.amount_minor,
                    currency=draft.currency,
                    display_order=draft.display_order,
                    fulfillment_mode=draft.fulfillment_mode,
                    calculation_evidence_json=draft.calculation_evidence_json,
                )
            )
            for draft in drafts
        )
        return GenerateSalaryRunResult(run=run, items=frozen, created=True)

    @staticmethod
    def _manual_by_item(
        manual_amounts: tuple[ManualSalaryAmount, ...],
        items: tuple[SalaryPlanTemplateItem, ...],
    ) -> dict[int, int]:
        """Require exactly one value per active manual line, no more, no less.

        Defaulting a missing value to zero would silently underfund a
        destination and still look like a complete, approved plan.
        """
        provided: dict[int, int] = {}
        for entry in manual_amounts:
            if entry.template_item_id in provided:
                raise SalaryAllocationError(
                    "one manual line was given two different amounts"
                )
            provided[entry.template_item_id] = entry.amount_minor
        expected = {
            item.id
            for item in items
            if item.calculation_mode is SalaryCalculationMode.MANUAL
        }
        missing = sorted(expected - provided.keys())
        if missing:
            raise SalaryAllocationError(
                "every manual line needs an explicit amount; missing item(s): "
                + ", ".join(str(item_id) for item_id in missing)
            )
        extra = sorted(provided.keys() - expected)
        if extra:
            raise SalaryAllocationError(
                "a manual amount was given for a line this plan does not have: "
                + ", ".join(str(item_id) for item_id in extra)
            )
        return provided

    def _line_drafts(
        self,
        unit: UnitOfWork,
        *,
        template: SalaryPlanTemplate,
        source: SalaryDestination,
        items: tuple[SalaryPlanTemplateItem, ...],
        evidence: SalarySubscriptionEvidence,
        manual: dict[int, int],
    ) -> tuple[SalaryAllocationRunItemDraft, ...]:
        """Freeze one line per active destination, with the evidence for it."""
        claimed: dict[int, int] = {}
        drafts: list[SalaryAllocationRunItemDraft] = []
        for item in items:
            destination = _require_destination(
                unit, item.destination_wallet_id, currency=template.currency
            )
            category_name = _require_category_name(unit, item.budget_category_id)
            amount, item_evidence = self._resolve_amount(
                item, evidence=evidence, manual=manual, currency=template.currency
            )
            if item.calculation_mode is SalaryCalculationMode.SUBSCRIPTION_REQUIREMENT:
                claimed[item.destination_wallet_id] = (
                    claimed.get(item.destination_wallet_id, 0) + 1
                )
            drafts.append(
                SalaryAllocationRunItemDraft(
                    run_id=0,
                    template_item_id=item.id,
                    template_item_version=item.version,
                    name_snapshot=item.name,
                    destination_wallet_id=destination.wallet.id,
                    destination_account_id=destination.account.id,
                    destination_wallet_name_snapshot=destination.wallet.name,
                    destination_account_name_snapshot=destination.account.display_name,
                    budget_category_id=item.budget_category_id,
                    budget_category_name_snapshot=category_name,
                    calculation_mode=item.calculation_mode,
                    amount_minor=amount,
                    currency=template.currency,
                    display_order=item.display_order,
                    fulfillment_mode=salary_fulfillment_mode_for(
                        source_account_id=source.account.id,
                        destination_account_id=destination.account.id,
                        source_institution_code=source.account.institution_code,
                        destination_institution_code=(
                            destination.account.institution_code
                        ),
                    ),
                    calculation_evidence_json=serialize_item_evidence(item_evidence),
                )
            )
        self._require_full_subscription_coverage(evidence, claimed)
        return tuple(drafts)

    @staticmethod
    def _require_full_subscription_coverage(
        evidence: SalarySubscriptionEvidence, claimed: dict[int, int]
    ) -> None:
        """Every contributing subscription must be funded by exactly one line.

        A requirement no line claims would leave a real bill unfunded while
        the month still looked complete, so the generation fails instead.
        """
        needed = sorted(
            {
                item.funding_wallet_id
                for item in evidence.items
                if item.contribution_minor > 0
            }
        )
        uncovered = [
            wallet_id for wallet_id in needed if claimed.get(wallet_id, 0) == 0
        ]
        if uncovered:
            raise SalaryAllocationError(
                "the subscription requirement asks for money in a wallet this "
                "plan has no line for; add a subscription line for wallet(s): "
                + ", ".join(str(wallet_id) for wallet_id in uncovered)
            )
        duplicated = sorted(
            wallet_id for wallet_id, count in claimed.items() if count > 1
        )
        if duplicated:  # pragma: no cover - the active index already refuses it
            raise SalaryAllocationError(
                "two lines claim the same subscription wallet: "
                + ", ".join(str(wallet_id) for wallet_id in duplicated)
            )

    @staticmethod
    def _resolve_amount(
        item: SalaryPlanTemplateItem,
        *,
        evidence: SalarySubscriptionEvidence,
        manual: dict[int, int],
        currency: str,
    ) -> tuple[int, SalaryItemEvidence]:
        """Decide one line's amount and record why it is that amount."""
        if item.calculation_mode is SalaryCalculationMode.FIXED:
            # The mode/amount shape is validated in the domain and by a check
            # constraint, so a fixed line always carries its amount.
            assert item.fixed_amount_minor is not None
            return item.fixed_amount_minor, FixedSalaryItemEvidence(
                schema_version=SALARY_EVIDENCE_SCHEMA_VERSION,
                template_item_id=item.id,
                template_item_version=item.version,
                fixed_amount_minor=item.fixed_amount_minor,
            )
        if item.calculation_mode is SalaryCalculationMode.MANUAL:
            entered = manual[item.id]
            return entered, ManualSalaryItemEvidence(
                schema_version=SALARY_EVIDENCE_SCHEMA_VERSION,
                template_item_id=item.id,
                template_item_version=item.version,
                entered_amount_minor=entered,
            )
        covered = tuple(
            entry
            for entry in evidence.items
            if entry.funding_wallet_id == item.destination_wallet_id
            and entry.contribution_minor > 0
        )
        total = sum(entry.contribution_minor for entry in covered)
        return total, SubscriptionSalaryItemEvidence(
            schema_version=SALARY_EVIDENCE_SCHEMA_VERSION,
            template_item_id=item.id,
            template_item_version=item.version,
            year=evidence.year,
            month=evidence.month,
            currency=normalize_currency(currency),
            destination_wallet_id=item.destination_wallet_id,
            subscription_ids=tuple(entry.subscription_id for entry in covered),
            contribution_total_minor=total,
        )


class ApproveSalaryAllocation:
    """Turn one frozen draft into wallet moves and waiting transfers."""

    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        journal: JournalService,
        *,
        clock: Clock = lambda: datetime.now(UTC),
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._journal = journal
        self._clock = clock

    def execute(self, request: ApproveSalaryRunRequest) -> ApproveSalaryRunResult:
        """Approve one draft, or replay the approval that already happened."""
        with self._unit_of_work_factory() as unit:
            run = unit.salary_runs.get(request.run_id)
            if run is None:
                raise SalaryAllocationError("salary allocation run was not found")
            if run.status in _SETTLED_RUN_STATUSES:
                return ApproveSalaryRunResult(
                    run=run,
                    items=unit.salary_run_items.list_for_run(run.id),
                    entry=(
                        None
                        if run.approval_entry_id is None
                        else unit.journal.get(run.approval_entry_id)
                    ),
                    created=False,
                )
            if run.status is SalaryRunStatus.CANCELLED:
                raise SalaryAllocationError("a cancelled salary run is not approved")
            if run.version != request.expected_version:
                raise ConcurrentUpdateError("this run changed after the page loaded")

            source = _require_source(unit, run.source_account_id, currency=run.currency)
            if source.wallet.id != run.source_wallet_id:
                raise SalaryPlanError(
                    "the salary account's Unallocated wallet has changed since "
                    "this month was generated; regenerate it first"
                )
            items = unit.salary_run_items.list_for_run(run.id)
            destinations = {
                item.id: _require_destination(
                    unit, item.destination_wallet_id, currency=run.currency
                )
                for item in items
            }
            self._require_available_source(unit, run)

            entry = self._reclassify(unit, run, items, source)
            targets = {
                item.id: approved_item_status(
                    amount_minor=item.amount_minor, mode=item.fulfillment_mode
                )
                for item in items
            }
            for item in items:
                ensure_run_item_transition(item.status, targets[item.id])
                unit.salary_run_items.transition(
                    item.id,
                    expected_version=item.version,
                    expected_status=item.status,
                    status=targets[item.id],
                )
            status = derive_run_status(targets.values())
            ensure_run_transition(run.status, status)
            approved = unit.salary_runs.approve(
                run.id,
                expected_version=run.version,
                status=status,
                approval_entry_id=None if entry is None else entry.id,
                approved_at=self._clock(),
            )
            settled = unit.salary_run_items.list_for_run(run.id)
            unit.commit()
        del destinations
        return ApproveSalaryRunResult(
            run=approved, items=settled, entry=entry, created=True
        )

    @staticmethod
    def _require_available_source(unit: UnitOfWork, run: SalaryAllocationRun) -> None:
        """The whole plan must fit in what the source wallet can still spend.

        Same-account lines leave Unallocated immediately and cross-account
        lines reserve it until the transfer happens, so both consume the same
        availability and both are checked here.
        """
        balance = unit.budget_ledger.wallet_balance(run.source_wallet_id)
        reserved = unit.budget_ledger.reserved_minor(run.source_wallet_id)
        if balance - reserved < run.planned_amount_minor:
            raise InsufficientAvailableFundsError(
                "the salary wallet does not have enough available money for "
                "this plan once existing reservations are counted"
            )

    def _reclassify(
        self,
        unit: UnitOfWork,
        run: SalaryAllocationRun,
        items: tuple[SalaryAllocationRunItem, ...],
        source: SalaryDestination,
    ) -> JournalEntry | None:
        """Move money between wallets of one account, never between banks.

        Every posting names the same physical ledger account, so the bank's
        total is unchanged by construction; only the wallet composition moves.
        """
        internal = tuple(
            item
            for item in items
            if item.amount_minor > 0
            and item.fulfillment_mode is CommitmentFulfillmentMode.RESERVE_ONLY
        )
        if not internal:
            return None
        total = sum(item.amount_minor for item in internal)
        postings = (
            PostingDraft(
                ledger_account_id=source.ledger_account.id,
                amount=Money(-total, run.currency),
                wallet_id=run.source_wallet_id,
            ),
            *(
                PostingDraft(
                    ledger_account_id=source.ledger_account.id,
                    amount=Money(item.amount_minor, run.currency),
                    wallet_id=item.destination_wallet_id,
                )
                for item in internal
            ),
        )
        return self._journal.stage_balanced(
            unit,
            entry_type=JournalEntryType.INTERNAL_TRANSFER,
            effective_at=self._clock(),
            idempotency_key=salary_approval_journal_key(run.id),
            description=_APPROVAL_DESCRIPTION,
            postings=postings,
        )


class MarkSalaryTransferred:
    """Record that one cross-account salary transfer really happened."""

    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        journal: JournalService,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._journal = journal

    def execute(
        self, request: MarkSalaryTransferredRequest
    ) -> MarkSalaryTransferredResult:
        """Post the real movement once, or replay the entry already posted."""
        effective_at = _require_aware(request.effective_at, label="transfer time")
        with self._unit_of_work_factory() as unit:
            item = unit.salary_run_items.get(request.run_item_id)
            if item is None:
                raise SalaryAllocationError("salary run item was not found")
            run = unit.salary_runs.get(item.run_id)
            if run is None:  # pragma: no cover - guarded by foreign key
                raise SalaryAllocationError("salary allocation run was not found")
            if item.status is SalaryRunItemStatus.TRANSFERRED:
                return self._replay(unit, run, item)
            if item.version != request.expected_version:
                raise ConcurrentUpdateError("this item changed after the page loaded")
            if run.status not in {
                SalaryRunStatus.APPROVED,
                SalaryRunStatus.PARTIALLY_FUNDED,
            }:
                raise SalaryAllocationError(
                    "only an approved salary run has transfers to confirm"
                )
            if item.status is not SalaryRunItemStatus.WAITING_TRANSFER:
                raise SalaryAllocationError(
                    f"a {item.status.value} salary line has no transfer to confirm"
                )
            if item.amount_minor <= 0:
                raise SalaryAllocationError("a zero line moves no money")

            source = _require_source(unit, run.source_account_id, currency=run.currency)
            destination = _require_destination(
                unit, item.destination_wallet_id, currency=run.currency
            )
            if destination.account.id == run.source_account_id:
                raise SalaryAllocationError(
                    "this line stays inside the salary account and needs no transfer"
                )
            self._require_transferable(unit, run, item)

            entry = self._journal.stage_balanced(
                unit,
                entry_type=JournalEntryType.FUNDING_TRANSFER,
                effective_at=effective_at,
                idempotency_key=salary_transfer_journal_key(item.id),
                description=_TRANSFER_DESCRIPTION,
                postings=(
                    PostingDraft(
                        ledger_account_id=source.ledger_account.id,
                        amount=Money(-item.amount_minor, run.currency),
                        wallet_id=run.source_wallet_id,
                    ),
                    PostingDraft(
                        ledger_account_id=destination.ledger_account.id,
                        amount=Money(item.amount_minor, run.currency),
                        wallet_id=destination.wallet.id,
                    ),
                ),
            )
            ensure_run_item_transition(item.status, SalaryRunItemStatus.TRANSFERRED)
            transferred = unit.salary_run_items.transition(
                item.id,
                expected_version=item.version,
                expected_status=item.status,
                status=SalaryRunItemStatus.TRANSFERRED,
                transfer_entry_id=entry.id,
                transferred_at=effective_at,
            )
            refreshed = self._refresh_run(unit, run)
            unit.commit()
        return MarkSalaryTransferredResult(
            run=refreshed, item=transferred, entry=entry, created=True
        )

    @staticmethod
    def _require_transferable(
        unit: UnitOfWork, run: SalaryAllocationRun, item: SalaryAllocationRunItem
    ) -> None:
        """The wallet must really hold the money this transfer sends away.

        This line's own reservation is already inside ``reserved``, so it is
        removed before comparing: otherwise the amount would be counted twice
        and a wallet holding exactly what it promised could never pay it out.
        """
        balance = unit.budget_ledger.wallet_balance(run.source_wallet_id)
        other_reservations = (
            unit.budget_ledger.reserved_minor(run.source_wallet_id) - item.amount_minor
        )
        if balance - other_reservations < item.amount_minor:
            raise InsufficientAvailableFundsError(
                "the salary wallet no longer holds enough money for this transfer"
            )

    @staticmethod
    def _refresh_run(unit: UnitOfWork, run: SalaryAllocationRun) -> SalaryAllocationRun:
        """Re-derive the run's status from what its lines have now reached."""
        statuses = tuple(
            entry.status for entry in unit.salary_run_items.list_for_run(run.id)
        )
        status = derive_run_status(statuses)
        if status is run.status:
            return run
        ensure_run_transition(run.status, status)
        return unit.salary_runs.refresh_status(
            run.id,
            expected_version=run.version,
            expected_status=run.status,
            status=status,
        )

    @staticmethod
    def _replay(
        unit: UnitOfWork, run: SalaryAllocationRun, item: SalaryAllocationRunItem
    ) -> MarkSalaryTransferredResult:
        """Return the entry this line already posted, never a second one.

        A retry that names another effective time does not rewrite the posted
        entry either. A posted entry is corrected by reversing it, which is
        the existing mechanism and deliberately not something this action can
        do silently.
        """
        if item.transfer_entry_id is None:  # pragma: no cover - checked by the table
            raise SalaryAllocationError("a transferred line must name its entry")
        entry = unit.journal.get(item.transfer_entry_id)
        if entry is None:  # pragma: no cover - guarded by foreign key
            raise SalaryAllocationError("the transfer entry was not found")
        return MarkSalaryTransferredResult(
            run=run, item=item, entry=entry, created=False
        )

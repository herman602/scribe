"""Hand-entered spending, user-owned event notes, and manual reversal.

Both accounting commands are atomic by construction (ADR-B061): the canonical event, its
allocation, any funding commitment, the balanced journal entry, and the event's
new status commit together or not at all. Neither service reaches the journal
repository; every entry still goes through `JournalService`.

The note command is deliberately smaller (ADR-B074): it conditionally updates
only display metadata and never touches accounting evidence.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from app.application.services.commitment_release import release_event_reservations
from app.application.services.event_allocation import (
    AllocateFinancialEvent,
    AllocateFinancialEventRequest,
    AllocationLine,
    AllocationResult,
)
from app.application.services.ledger import JournalService
from app.domain.budget_enums import (
    FinancialAccountType,
    FinancialEventOrigin,
    FinancialEventStatus,
    FinancialEventType,
    PaymentMethod,
    RecognitionBasis,
)
from app.domain.budget_models import (
    Card,
    FinancialEvent,
    FinancialEventDraft,
    FinancialEventNoteUpdate,
    FundingCommitment,
    JournalEntry,
    Wallet,
    normalize_currency,
    normalize_name,
    normalize_note,
    validate_aware,
    validate_event_magnitude,
)
from app.domain.budget_rules import (
    ensure_manual_expense_payment_method,
    ensure_reversible_event,
    event_reversal_journal_key,
    manual_expense_event_key,
)
from app.domain.errors import (
    CardConfigurationError,
    ConcurrentUpdateError,
    CurrencyMismatchError,
    EventReversalError,
    IdempotencyConflictError,
    LedgerConfigurationError,
    ManualExpenseError,
)
from app.domain.ports.repositories import UnitOfWork

type UnitOfWorkFactory = Callable[[], UnitOfWork]

MAX_MERCHANT_LENGTH = 200
MAX_NOTE_LENGTH = 500


@dataclass(frozen=True)
class ManualExpenseRequest:
    """One expense a person typed in, in the currency they spent."""

    payment_method: PaymentMethod
    occurred_at: datetime
    merchant: str
    amount_minor: int
    currency: str
    funding_wallet_id: int
    #: Required, unlike the Gmail review flow. A hand-entered expense is typed
    #: by the one person who knows what it was for, so leaving it uncategorized
    #: would put a number in the day's total that no budget can ever explain.
    budget_category_id: int
    client_token: str
    card_id: int | None = None
    note: str | None = None
    clearing_wallet_id: int | None = None


@dataclass(frozen=True)
class ReverseEventRequest:
    """User intent to cancel one hand-entered expense that never happened."""

    financial_event_id: int
    expected_version: int


@dataclass(frozen=True)
class ReversalResult:
    """Everything one reversal committed, for display and tests."""

    event: FinancialEvent
    original_entry: JournalEntry
    reversal_entry: JournalEntry
    commitments: tuple[FundingCommitment, ...]


@dataclass(frozen=True)
class UpdateFinancialEventNoteRequest:
    """User intent to replace or clear one canonical event's note."""

    financial_event_id: int
    expected_version: int
    note: str | None


class UpdateFinancialEventNote:
    """Edit display metadata without rewriting an immutable journal entry."""

    def __init__(self, unit_of_work_factory: UnitOfWorkFactory) -> None:
        self._unit_of_work_factory = unit_of_work_factory

    def execute(self, request: UpdateFinancialEventNoteRequest) -> FinancialEvent:
        """Persist one bounded note under optimistic concurrency control."""
        change = FinancialEventNoteUpdate(
            event_id=request.financial_event_id,
            expected_version=request.expected_version,
            note=request.note,
        )
        with self._unit_of_work_factory() as unit:
            event = unit.financial_events.save_note(change)
            unit.commit()
        return event


class CreateManualExpense:
    """Record one cash or credit-card expense as a posted ledger fact."""

    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        allocator: AllocateFinancialEvent,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._allocator = allocator

    def execute(self, request: ManualExpenseRequest) -> AllocationResult:
        """Create, allocate, reserve, post, and mark posted in one commit."""
        ensure_manual_expense_payment_method(request.payment_method)
        occurred_at = validate_aware(request.occurred_at, label="expense time")
        merchant = normalize_name(
            request.merchant,
            label="merchant or purpose",
            max_length=MAX_MERCHANT_LENGTH,
        )
        amount_minor = validate_event_magnitude(request.amount_minor)
        currency = normalize_currency(request.currency)
        note = normalize_note(
            request.note, label="expense note", max_length=MAX_NOTE_LENGTH
        )
        key = manual_expense_event_key(request.client_token)
        self._require_instrument_shape(request)

        with self._unit_of_work_factory() as unit:
            existing = unit.financial_events.get_by_idempotency_key(key)
            if existing is not None:
                replay = self._replay(
                    unit,
                    existing,
                    request,
                    occurred_at=occurred_at,
                    merchant=merchant,
                    amount_minor=amount_minor,
                    currency=currency,
                    note=note,
                )
                unit.commit()
                return replay
            wallet = self._require_funding_wallet(unit, request, currency=currency)
            if request.payment_method is PaymentMethod.CASH:
                self._require_cash_account(unit, wallet)
            else:
                self._require_ready_card(unit, request, currency=currency)
            event = unit.financial_events.add(
                FinancialEventDraft(
                    origin=FinancialEventOrigin.MANUAL,
                    event_type=FinancialEventType.PURCHASE,
                    payment_method=request.payment_method,
                    recognition_basis=RecognitionBasis.OBSERVED_MANUAL,
                    occurred_at=occurred_at,
                    amount_minor=amount_minor,
                    currency=currency,
                    idempotency_key=key,
                    merchant=merchant,
                    card_id=request.card_id,
                    status=FinancialEventStatus.NEEDS_ALLOCATION,
                    note=note,
                )
            )
            result = self._allocator.stage(
                unit,
                AllocateFinancialEventRequest(
                    financial_event_id=event.id,
                    expected_version=event.version,
                    client_token=request.client_token,
                    lines=(
                        AllocationLine(
                            amount_minor=amount_minor,
                            funding_wallet_id=request.funding_wallet_id,
                            budget_category_id=request.budget_category_id,
                            clearing_wallet_id=request.clearing_wallet_id,
                        ),
                    ),
                ),
            )
            unit.commit()
        return result

    @staticmethod
    def _require_instrument_shape(request: ManualExpenseRequest) -> None:
        """A card expense names its card; a cash expense never does."""
        if request.payment_method is PaymentMethod.CREDIT_CARD:
            if request.card_id is None:
                raise ManualExpenseError("a card expense names the card that paid")
        elif request.card_id is not None:
            raise ManualExpenseError("a cash expense does not name a card")
        if (
            request.clearing_wallet_id is not None
            and request.payment_method is not PaymentMethod.CREDIT_CARD
        ):
            raise ManualExpenseError(
                "dedicated-wallet treatment is only available for card spending"
            )

    @staticmethod
    def _require_funding_wallet(
        unit: UnitOfWork, request: ManualExpenseRequest, *, currency: str
    ) -> Wallet:
        """Return the explicit wallet the money came out of, or refuse."""
        wallet = unit.wallets.get(request.funding_wallet_id)
        if wallet is None or not wallet.is_active:
            raise LedgerConfigurationError("funding wallet was not found")
        account = unit.financial_accounts.get(wallet.financial_account_id)
        if account is None:  # pragma: no cover - wallets always have one
            raise LedgerConfigurationError("the funding account is missing")
        if not account.is_active:
            raise LedgerConfigurationError("the funding account is closed")
        if account.currency != currency:
            raise CurrencyMismatchError("a funding wallet must hold the spent currency")
        return wallet

    @staticmethod
    def _require_cash_account(unit: UnitOfWork, wallet: Wallet) -> None:
        """Cash leaves a cash account; a bank debit is a bank movement."""
        account = unit.financial_accounts.get(wallet.financial_account_id)
        if account is None:  # pragma: no cover - checked by the caller
            raise LedgerConfigurationError("the funding account is missing")
        if account.account_type is not FinancialAccountType.CASH:
            raise ManualExpenseError(
                "cash is spent from a cash account; record a bank movement instead"
            )

    @staticmethod
    def _require_ready_card(
        unit: UnitOfWork, request: ManualExpenseRequest, *, currency: str
    ) -> Card:
        """Refuse a card that cannot settle, so no half-posted purchase exists."""
        assert request.card_id is not None
        card = unit.cards.get(request.card_id)
        if card is None:
            raise CardConfigurationError("card was not found")
        if card.active_to is not None:
            raise CardConfigurationError("this card is no longer active")
        if not card.is_ready:
            raise CardConfigurationError(
                "configure the card's liability and settlement accounts first"
            )
        assert card.liability_account_id is not None
        assert card.settlement_account_id is not None
        for account_id in (card.liability_account_id, card.settlement_account_id):
            account = unit.financial_accounts.get(account_id)
            if account is None or not account.is_active:
                raise CardConfigurationError("this card's accounts are unusable")
            if account.currency != currency:
                raise CurrencyMismatchError(
                    "a card settles in its own currency; record the expense there"
                )
        return card

    @staticmethod
    def _replay(
        unit: UnitOfWork,
        existing: FinancialEvent,
        request: ManualExpenseRequest,
        *,
        occurred_at: datetime,
        merchant: str,
        amount_minor: int,
        currency: str,
        note: str | None,
    ) -> AllocationResult:
        """Return the first submission's outcome, or refuse a different one.

        A matching token proves only that something once used it. Every value
        the user chose is compared, so a second form with the same token but a
        different amount, merchant, wallet, or card is a conflict rather than a
        silent success that records nothing.
        """
        same = (
            existing.origin is FinancialEventOrigin.MANUAL
            and existing.recognition_basis is RecognitionBasis.OBSERVED_MANUAL
            and existing.event_type is FinancialEventType.PURCHASE
            and existing.payment_method is request.payment_method
            and existing.occurred_at == occurred_at
            and existing.merchant == merchant
            and existing.amount_minor == amount_minor
            and existing.currency == currency
            and existing.card_id == request.card_id
            and existing.note == note
        )
        if not same:
            raise IdempotencyConflictError(
                "this token already records a different manual expense"
            )
        entry = unit.journal.get_by_financial_event(existing.id)
        if entry is None:
            raise IdempotencyConflictError(
                "this token records an expense that was never posted"
            )
        # The split this entry carried, not every split the event ever had: a
        # redone allocation leaves the superseded rows in place (ADR-B103), and
        # comparing against all of them would report a plain replay as a
        # different submission.
        allocations = unit.allocations.list_for_entry(entry.id)
        routes = {
            item.event_allocation_id: item.clearing_wallet_id
            for item in unit.allocations.list_dedicated_for_event(existing.id)
        }
        recorded = sorted(
            (
                item.amount_minor,
                item.funding_wallet_id,
                item.budget_category_id or 0,
                routes.get(item.id) or 0,
            )
            for item in allocations
        )
        requested = [
            (
                amount_minor,
                request.funding_wallet_id,
                request.budget_category_id,
                request.clearing_wallet_id or 0,
            )
        ]
        if recorded != requested:
            raise IdempotencyConflictError(
                "this token already records a different split"
            )
        return AllocationResult(
            event=existing,
            allocations=allocations,
            commitments=unit.commitments.list_for_event(existing.id),
            entry=entry,
        )


class ReverseFinancialEvent:
    """Cancel one posted manual event without rewriting any evidence."""

    def __init__(
        self, unit_of_work_factory: UnitOfWorkFactory, journal: JournalService
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._journal = journal

    def execute(self, request: ReverseEventRequest) -> ReversalResult:
        """Stage the exact opposite entry, free reservations, and mark reversed.

        The original event, its allocations, its entry, and every B6 schedule
        and coverage link are left exactly as they are (ADR-B069): the user has
        already instructed their bank, and Scribe cannot retract that.
        """
        with self._unit_of_work_factory() as unit:
            event = unit.financial_events.get(request.financial_event_id)
            if event is None:
                raise EventReversalError("financial event was not found")
            entry = unit.journal.get_by_financial_event(event.id)
            if entry is None:
                # Nothing is in force, which is also what a completed
                # reversal looks like, so the entry it cancelled is what
                # tells the two apart (ADR-B103).
                entry = unit.journal.latest_by_financial_event(event.id)
            if entry is None:
                raise EventReversalError("this event has no posted entry to reverse")
            existing = unit.journal.get_reversal_of(entry.id)
            if existing is not None and event.status is FinancialEventStatus.REVERSED:
                # A resubmitted form must replay even though the first attempt
                # already moved the version the page was rendered with.
                return ReversalResult(
                    event=event,
                    original_entry=entry,
                    reversal_entry=existing,
                    commitments=unit.commitments.list_for_event(event.id),
                )
            ensure_reversible_event(origin=event.origin, status=event.status)
            self._require_no_live_salary_run(unit, event.id)
            if event.version != request.expected_version:
                raise ConcurrentUpdateError(
                    "this event changed after the page was loaded"
                )
            reversal = self._journal.stage_reversal(
                unit,
                entry.id,
                description="Reversal",
                idempotency_key=event_reversal_journal_key(entry.id),
            )
            release_event_reservations(unit, event.id)
            reversed_event = unit.financial_events.mark_reversed(
                event.id, event.version
            )
            commitments = unit.commitments.list_for_event(event.id)
            unit.commit()
        return ReversalResult(
            event=reversed_event,
            original_entry=entry,
            reversal_entry=reversal,
            commitments=commitments,
        )

    @staticmethod
    def _require_no_live_salary_run(unit: UnitOfWork, event_id: int) -> None:
        """Refuse to cancel the salary a live B5 run was generated from.

        A recorded salary receipt is a posted manual event like any other, so
        without this guard B7 would happily reverse the deposit a whole month's
        frozen distribution was calculated from, leaving every line of that run
        pointing at money the ledger says never arrived. The run has to be
        cancelled through B5 first.
        """
        if unit.salary_runs.find_live_by_salary_event(event_id) is not None:
            raise EventReversalError(
                "a live salary allocation was generated from this deposit; "
                "cancel that allocation before reversing it"
            )

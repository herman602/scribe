"""Turn a reviewed financial event into a categorized, funded ledger entry.

Allocation, funding commitments, the balanced journal entry, and the event's
new status all commit in one Unit of Work. Every journal invariant is still
checked by `JournalService`; this service never reaches the journal repository.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from app.application.services.allocation_reads import allocations_in_force
from app.application.services.commitment_release import release_event_reservations
from app.application.services.ledger import (
    JournalService,
    ensure_event_after_opening_cutoff,
)
from app.domain.budget_enums import (
    AllocationSource,
    CommitmentStatus,
    FinancialEventStatus,
    FinancialEventType,
    JournalEntryType,
    LedgerAccountKind,
    PaymentMethod,
    WalletPurpose,
)
from app.domain.budget_models import (
    Card,
    DedicatedWalletAllocationDraft,
    EventAllocation,
    EventAllocationDraft,
    FinancialEvent,
    FundingCommitment,
    FundingCommitmentDraft,
    JournalEntry,
    LedgerAccount,
    LedgerAccountDraft,
    Money,
    PostingDraft,
)
from app.domain.budget_rules import (
    allocation_redo_journal_key,
    distribute_proportionally,
    ensure_allocatable_event_type,
    ensure_allocation_total,
    ensure_redoable_allocation,
    ensure_refund_within_original,
    ensure_single_allocation_currency,
    expense_ledger_account_code,
    fulfillment_mode_for,
)
from app.domain.errors import (
    AllocationRedoError,
    BudgetPeriodError,
    CardConfigurationError,
    ConcurrentUpdateError,
    CurrencyMismatchError,
    FinancialEventError,
    IncompleteAllocationError,
    LedgerConfigurationError,
)
from app.domain.ports.repositories import UnitOfWork

type UnitOfWorkFactory = Callable[[], UnitOfWork]
type Clock = Callable[[], datetime]

UNCATEGORIZED_LEDGER_NAME = "Uncategorized spending"

_REFUND_TYPES = frozenset({FinancialEventType.REFUND})
_ALLOCATABLE_STATUSES = frozenset(
    {
        FinancialEventStatus.DRAFT,
        FinancialEventStatus.NEEDS_ALLOCATION,
        FinancialEventStatus.READY,
    }
)


def allocation_idempotency_key(financial_event_id: int, client_token: str) -> str:
    """Return the stable key for one allocation attempt.

    The client token makes a resubmitted form return the same entry. A genuinely
    different attempt on an already posted event is refused by the one-entry-per
    -event guard in `JournalService`, not silently accepted.
    """
    return f"allocation:financial_event:{financial_event_id}:{client_token}"


@dataclass(frozen=True)
class AllocationLine:
    """One requested share of the event being allocated."""

    amount_minor: int
    funding_wallet_id: int
    budget_category_id: int | None = None
    clearing_wallet_id: int | None = None


@dataclass(frozen=True)
class AllocateFinancialEventRequest:
    """User intent to categorize, fund, and post one reviewed event."""

    financial_event_id: int
    expected_version: int
    client_token: str
    lines: tuple[AllocationLine, ...]


@dataclass(frozen=True)
class AllocationResult:
    """Everything one allocation committed, for display and tests."""

    event: FinancialEvent
    allocations: tuple[EventAllocation, ...]
    commitments: tuple[FundingCommitment, ...]
    entry: JournalEntry


@dataclass(frozen=True)
class RefundDraftLine:
    """One proposed refund share inherited from the original expense."""

    amount_minor: int
    funding_wallet_id: int
    budget_category_id: int | None
    clearing_wallet_id: int | None = None


class AllocateFinancialEvent:
    """Allocate, reserve, and post one financial event atomically."""

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

    def propose_refund_lines(
        self, financial_event_id: int
    ) -> tuple[RefundDraftLine, ...]:
        """Inherit the original expense's split, pro-rata for a partial refund.

        A refund is a direction, not a purpose, so it returns to the category
        that was originally spent. An earlier partial refund has already used
        part of some shares, so the draft is spread across what each share can
        still return rather than across the original amounts.
        """
        with self._unit_of_work_factory() as unit:
            return self.propose_refund_lines_in(unit, financial_event_id)

    def propose_refund_lines_in(
        self, unit: UnitOfWork, financial_event_id: int
    ) -> tuple[RefundDraftLine, ...]:
        """Propose the same split inside a caller's open transaction.

        A refund recorded as one command has to decide its shares against the
        same snapshot it will be written into, so the proposal and the posting
        cannot disagree about what an earlier partial refund already used.
        """
        event = self._require_event(unit, financial_event_id)
        ensure_allocatable_event_type(event.event_type, event.payment_method)
        original = self._require_refund_target(unit, event)
        if original is None:
            return ()
        remaining = [
            (key, value)
            for key, value in self._remaining_refundable(unit, original).items()
            if value > 0
        ]
        if not remaining:
            return ()
        shares = distribute_proportionally(
            event.amount_minor, tuple(value for _, value in remaining)
        )
        return tuple(
            RefundDraftLine(
                amount_minor=share,
                funding_wallet_id=key[0],
                budget_category_id=key[1],
                clearing_wallet_id=key[2],
            )
            for (key, _), share in zip(remaining, shares, strict=True)
            if share > 0
        )

    def execute(self, request: AllocateFinancialEventRequest) -> AllocationResult:
        """Commit allocations, commitments, the entry, and the new status."""
        with self._unit_of_work_factory() as unit:
            result = self.stage(unit, request)
            unit.commit()
        return result

    def stage(
        self, unit: UnitOfWork, request: AllocateFinancialEventRequest
    ) -> AllocationResult:
        """Stage the whole allocation inside a caller's transaction.

        Every invariant `execute` enforces is enforced here — this *is* the
        implementation — but the caller owns the commit. That is what lets a
        hand-entered expense create its event and allocate it in one atomic
        change instead of leaving an event behind when allocation fails
        (ADR-B061). Nothing here reaches the journal repository: the entry
        still goes through `JournalService.stage_balanced`.
        """
        if not request.lines:
            raise IncompleteAllocationError("an allocation requires at least one line")
        if not request.client_token.strip():
            raise ValueError("a client idempotency token is required")

        key = allocation_idempotency_key(
            request.financial_event_id, request.client_token
        )
        event = self._require_event(unit, request.financial_event_id)
        replay = self._replay(unit, event, request, key)
        if replay is not None:
            return replay
        ensure_event_after_opening_cutoff(
            unit,
            event,
            effective_at=event.occurred_at,
        )
        if event.status not in _ALLOCATABLE_STATUSES:
            raise FinancialEventError(
                f"an event with status {event.status.value} cannot be allocated"
            )
        if event.version != request.expected_version:
            raise FinancialEventError("this event changed after the page was loaded")
        ensure_allocatable_event_type(event.event_type, event.payment_method)
        refund_target = self._require_refund_target(unit, event)
        self._require_funding_currency(unit, event, request.lines)
        self._require_treatment_shape(unit, event, request.lines)

        ensure_allocation_total(
            event_amount_minor=event.amount_minor,
            allocated_minor=sum(line.amount_minor for line in request.lines),
        )
        allocations = tuple(
            unit.allocations.add(
                EventAllocationDraft(
                    financial_event_id=event.id,
                    funding_wallet_id=line.funding_wallet_id,
                    amount_minor=line.amount_minor,
                    currency=event.currency,
                    budget_category_id=line.budget_category_id,
                    allocation_source=AllocationSource.MANUAL,
                )
            )
            for line in request.lines
        )
        dedicated_routes = {
            allocation.id: line.clearing_wallet_id
            for allocation, line in zip(allocations, request.lines, strict=True)
            if line.clearing_wallet_id is not None
        }
        for allocation_id, clearing_wallet_id in dedicated_routes.items():
            unit.allocations.add_dedicated(
                DedicatedWalletAllocationDraft(
                    event_allocation_id=allocation_id,
                    clearing_wallet_id=clearing_wallet_id,
                )
            )
        ensure_single_allocation_currency(
            event_currency=event.currency,
            currencies=tuple(item.currency for item in allocations),
        )
        if refund_target is not None:
            self._require_refund_lines_match_original(
                unit, refund_target, allocations, dedicated_routes
            )

        postings = self._expense_postings(unit, event, allocations)
        commitments: tuple[FundingCommitment, ...] = ()
        if event.payment_method is PaymentMethod.CREDIT_CARD:
            postings += self._card_posting(unit, event)
            postings += self._dedicated_wallet_postings(
                unit, event, allocations, dedicated_routes
            )
            commitments = self._reserve_funding(
                unit, event, allocations, dedicated_routes
            )
        else:
            postings += self._asset_postings(unit, event, allocations)

        entry = self._journal.stage_balanced(
            unit,
            entry_type=(
                JournalEntryType.REFUND
                if event.event_type in _REFUND_TYPES
                else JournalEntryType.EXPENSE
            ),
            effective_at=event.occurred_at,
            idempotency_key=key,
            description=event.merchant or "",
            postings=postings,
            financial_event_id=event.id,
        )
        # The entry could not exist until now, because its postings were built
        # from these rows. Naming it here is what lets a later reader tell the
        # split in force from one a redo superseded (ADR-B103).
        allocations = unit.allocations.attach_entry(
            tuple(item.id for item in allocations), entry.id
        )
        if refund_target is not None:
            released = self._release_original_reservations(
                unit, refund_target, allocations
            )
            # Releasing lowers what a commitment still requires, which can
            # make coverage that was only partial complete. The status is
            # re-derived here, in the same transaction, so no caller has to
            # remember to refresh it afterwards (ADR-B059).
            for commitment_id in released:
                unit.commitments.refresh_status(commitment_id)
        posted = unit.financial_events.mark_posted(event.id, event.version)

        return AllocationResult(
            event=posted,
            allocations=allocations,
            commitments=commitments,
            entry=entry,
        )

    @staticmethod
    def _replay(
        unit: UnitOfWork,
        event: FinancialEvent,
        request: AllocateFinancialEventRequest,
        key: str,
    ) -> AllocationResult | None:
        """Return the first outcome when the same submission arrives twice.

        A resubmitted form must not post a second entry, but it must also not
        quietly answer for a *different* allocation that happens to reuse the
        token, so the recorded split is compared before the replay is served.

        The comparison is against the rows *this* entry carried, not every row
        the event ever had. An event whose allocation was redone holds both
        generations, and comparing against all of them reported the submission
        as a different allocation when it was its own replay (ADR-B103).
        """
        entry = unit.journal.get_by_idempotency_key(key)
        if entry is None:
            return None
        if entry.financial_event_id != event.id:
            raise FinancialEventError(
                "this idempotency token already belongs to another event"
            )
        if unit.journal.get_reversal_of(entry.id) is not None:
            # The decision this token recorded has since been taken back
            # (ADR-B103). Answering "already recorded" would report a result
            # that no longer exists, and if the event is waiting in the
            # allocation queue the page would say it was posted while nothing
            # was.
            raise FinancialEventError(
                "the allocation this token recorded was taken back; decide it "
                "again from the current page"
            )
        allocations = unit.allocations.list_for_entry(entry.id)
        routes = AllocateFinancialEvent._clearing_by_allocation(unit, event.id)
        recorded = sorted(
            (
                item.amount_minor,
                item.funding_wallet_id,
                item.budget_category_id or 0,
                (routes.get(item.id) or 0),
            )
            for item in allocations
        )
        requested = sorted(
            (
                line.amount_minor,
                line.funding_wallet_id,
                line.budget_category_id or 0,
                line.clearing_wallet_id or 0,
            )
            for line in request.lines
        )
        if recorded != requested:
            raise FinancialEventError(
                "this idempotency token was already used for a different allocation"
            )
        return AllocationResult(
            event=event,
            allocations=allocations,
            commitments=unit.commitments.list_for_event(event.id),
            entry=entry,
        )

    @staticmethod
    def _require_event(unit: UnitOfWork, event_id: int) -> FinancialEvent:
        event = unit.financial_events.get(event_id)
        if event is None:
            raise FinancialEventError("financial event was not found")
        return event

    @staticmethod
    def _require_refund_target(
        unit: UnitOfWork, event: FinancialEvent
    ) -> FinancialEvent | None:
        """Enforce every refund rule the database cannot express in one row.

        The cumulative cap is read and checked inside the caller's
        transaction, so two partial refunds cannot each pass a per-refund
        check and together return more than was ever spent.
        """
        if event.refund_of_event_id is None:
            if event.event_type in _REFUND_TYPES:
                raise FinancialEventError(
                    "a refund must name the event it refunds before it can post"
                )
            return None
        original = unit.financial_events.get(event.refund_of_event_id)
        if original is None:
            raise FinancialEventError("the refunded event was not found")
        if original.event_type in _REFUND_TYPES:
            raise FinancialEventError("a refund cannot refund another refund")
        if original.currency != event.currency:
            raise FinancialEventError(
                "a refund must use the currency of the event it refunds"
            )
        if original.status is not FinancialEventStatus.POSTED:
            raise FinancialEventError(
                "the refunded event must be posted before it can be refunded"
            )
        if not allocations_in_force(unit, original.id):
            raise FinancialEventError(
                "the refunded event has no allocation to return money to"
            )
        ensure_refund_within_original(
            refund_minor=event.amount_minor,
            already_refunded_minor=sum(
                item.amount_minor
                for item in unit.financial_events.list_posted_refunds(original.id)
            ),
            original_minor=original.amount_minor,
        )
        if event.card_id != original.card_id:
            # Refunding onto a different instrument would credit one card while
            # releasing another card's reservation. That is a real workflow,
            # but it is a transfer plus a refund and needs its own model.
            raise FinancialEventError(
                "a refund must return to the same card as the event it refunds"
            )
        if event.payment_method is not original.payment_method:
            # The payment method decides which side of the entry moves. A card
            # purchase refunded as a bank credit would raise a bank balance,
            # leave the card liability untouched, and still release the card's
            # reservation, so the two halves would disagree permanently.
            raise FinancialEventError(
                "a refund must use the same payment method as the event it refunds"
            )
        return original

    @staticmethod
    def _require_funding_currency(
        unit: UnitOfWork, event: FinancialEvent, lines: tuple[AllocationLine, ...]
    ) -> None:
        """A wallet may only fund an event in its own account's currency.

        The web form already filters the list, but a tampered POST or a direct
        service call would otherwise reserve a TWD amount out of a USD wallet
        and quietly reduce that wallet's available balance.
        """
        for line in lines:
            wallet = unit.wallets.get(line.funding_wallet_id)
            if wallet is None or not wallet.is_active:
                raise LedgerConfigurationError("funding wallet was not found")
            account = unit.financial_accounts.get(wallet.financial_account_id)
            if account is None:  # pragma: no cover - wallets always have one
                raise LedgerConfigurationError("the funding account is missing")
            if account.currency != event.currency:
                raise CurrencyMismatchError(
                    "a funding wallet must hold the event's currency"
                )

    @staticmethod
    def _require_treatment_shape(
        unit: UnitOfWork, event: FinancialEvent, lines: tuple[AllocationLine, ...]
    ) -> None:
        """Validate a monthly-budget or dedicated-wallet allocation as a whole.

        A dedicated purchase is a card-only workflow. Every share moves inside
        one physical account into the same active clearing-hub wallet. A
        cross-account move is real banking and must go through the transfer
        workflow instead of being disguised as a virtual-wallet change.
        """
        dedicated = tuple(line for line in lines if line.clearing_wallet_id is not None)
        if not dedicated:
            return
        if len(dedicated) != len(lines):
            raise FinancialEventError(
                "one expense cannot mix monthly-budget and dedicated-wallet shares"
            )
        if event.payment_method is not PaymentMethod.CREDIT_CARD:
            raise FinancialEventError(
                "dedicated-wallet treatment is only available for card spending"
            )
        clearing_ids = {line.clearing_wallet_id for line in dedicated}
        if len(clearing_ids) != 1:
            raise FinancialEventError(
                "one dedicated expense must use one clearing-hub wallet"
            )
        clearing_id = next(iter(clearing_ids))
        assert clearing_id is not None
        clearing = unit.wallets.get(clearing_id)
        if (
            clearing is None
            or not clearing.is_active
            or clearing.purpose is not WalletPurpose.CLEARING_HUB
        ):
            raise LedgerConfigurationError(
                "the dedicated destination must be an active clearing-hub wallet"
            )
        clearing_account = unit.financial_accounts.get(clearing.financial_account_id)
        if clearing_account is None or not clearing_account.is_active:
            raise LedgerConfigurationError("the clearing account is unusable")
        if clearing_account.currency != event.currency:
            raise CurrencyMismatchError(
                "the clearing wallet must hold the event's currency"
            )
        for line in dedicated:
            if line.funding_wallet_id == clearing.id:
                raise FinancialEventError(
                    "a dedicated source wallet cannot also be the clearing hub"
                )
            source = unit.wallets.get(line.funding_wallet_id)
            if source is None:  # pragma: no cover - checked by funding validation
                raise LedgerConfigurationError("funding wallet was not found")
            if source.financial_account_id != clearing.financial_account_id:
                raise FinancialEventError(
                    "moving dedicated money across bank accounts requires a "
                    "real transfer"
                )

    @staticmethod
    def _clearing_by_allocation(unit: UnitOfWork, event_id: int) -> dict[int, int]:
        """Return the frozen clearing destination of each dedicated share."""
        return {
            item.event_allocation_id: item.clearing_wallet_id
            for item in unit.allocations.list_dedicated_for_event(event_id)
        }

    @staticmethod
    def _remaining_refundable(
        unit: UnitOfWork, original: FinancialEvent
    ) -> dict[tuple[int, int | None, int | None], int]:
        """Return what each original wallet-and-category share can still return.

        A refund returns to where the money was spent (ADR-B030). Keeping the
        remainder per share is what stops a refund from crediting one category
        for another category's spending while the aggregate still looks right.
        """
        remaining: dict[tuple[int, int | None, int | None], int] = {}
        original_routes = AllocateFinancialEvent._clearing_by_allocation(
            unit, original.id
        )
        for item in allocations_in_force(unit, original.id):
            key = (
                item.funding_wallet_id,
                item.budget_category_id,
                original_routes.get(item.id),
            )
            remaining[key] = remaining.get(key, 0) + item.amount_minor
        for refund in unit.financial_events.list_posted_refunds(original.id):
            refund_routes = AllocateFinancialEvent._clearing_by_allocation(
                unit, refund.id
            )
            for item in allocations_in_force(unit, refund.id):
                key = (
                    item.funding_wallet_id,
                    item.budget_category_id,
                    refund_routes.get(item.id),
                )
                remaining[key] = remaining.get(key, 0) - item.amount_minor
        return remaining

    def _require_refund_lines_match_original(
        self,
        unit: UnitOfWork,
        original: FinancialEvent,
        allocations: tuple[EventAllocation, ...],
        dedicated_routes: dict[int, int],
    ) -> None:
        """Refuse a refund that moves money to a share it never came from."""
        remaining = self._remaining_refundable(unit, original)
        for item in allocations:
            key = (
                item.funding_wallet_id,
                item.budget_category_id,
                dedicated_routes.get(item.id),
            )
            available = remaining.get(key)
            if available is None:
                raise FinancialEventError(
                    "a refund must return to a wallet and category the original "
                    "expense used"
                )
            if item.amount_minor > available:
                raise FinancialEventError(
                    "a refund cannot return more than that share of the "
                    "original expense"
                )
            remaining[key] = available - item.amount_minor

    def _expense_postings(
        self,
        unit: UnitOfWork,
        event: FinancialEvent,
        allocations: tuple[EventAllocation, ...],
    ) -> tuple[PostingDraft, ...]:
        """Debit the category for spending; credit it back for a refund."""
        sign = -1 if event.event_type in _REFUND_TYPES else 1
        return tuple(
            PostingDraft(
                ledger_account_id=self._expense_ledger(
                    unit, item.budget_category_id, event.currency
                ).id,
                amount=Money(sign * item.amount_minor, event.currency),
                budget_category_id=item.budget_category_id,
            )
            for item in allocations
        )

    def _card_posting(
        self, unit: UnitOfWork, event: FinancialEvent
    ) -> tuple[PostingDraft, ...]:
        """Move the card liability without touching any bank balance."""
        card = self._require_ready_card(unit, event)
        assert card.liability_account_id is not None
        liability = unit.ledger_accounts.get_for_financial_account(
            card.liability_account_id
        )
        if liability is None:
            raise CardConfigurationError(
                "the card's liability account has no ledger account"
            )
        sign = 1 if event.event_type in _REFUND_TYPES else -1
        return (
            PostingDraft(
                ledger_account_id=liability.id,
                amount=Money(sign * event.amount_minor, event.currency),
            ),
        )

    def _asset_postings(
        self,
        unit: UnitOfWork,
        event: FinancialEvent,
        allocations: tuple[EventAllocation, ...],
    ) -> tuple[PostingDraft, ...]:
        """Reduce exactly the funding wallets a cash or bank movement used."""
        sign = 1 if event.event_type in _REFUND_TYPES else -1
        postings: list[PostingDraft] = []
        for item in allocations:
            wallet = unit.wallets.get(item.funding_wallet_id)
            if wallet is None or not wallet.is_active:
                raise LedgerConfigurationError("funding wallet was not found")
            asset = unit.ledger_accounts.get_for_financial_account(
                wallet.financial_account_id
            )
            if asset is None:  # pragma: no cover - accounts always have one
                raise LedgerConfigurationError(
                    "the funding wallet's account has no ledger account"
                )
            postings.append(
                PostingDraft(
                    ledger_account_id=asset.id,
                    amount=Money(sign * item.amount_minor, event.currency),
                    wallet_id=wallet.id,
                )
            )
        return tuple(postings)

    def _dedicated_wallet_postings(
        self,
        unit: UnitOfWork,
        event: FinancialEvent,
        allocations: tuple[EventAllocation, ...],
        dedicated_routes: dict[int, int],
    ) -> tuple[PostingDraft, ...]:
        """Reclassify dedicated money into the clearing hub in the same bank.

        The physical asset account nets to zero. A purchase reduces its source
        envelope and raises the hub; a refund performs the exact economic
        opposite. The expense and card-liability sides remain separate lines
        in the same immutable journal entry.
        """
        if not dedicated_routes:
            return ()
        source_sign = 1 if event.event_type in _REFUND_TYPES else -1
        postings: list[PostingDraft] = []
        for item in allocations:
            clearing_wallet_id = dedicated_routes.get(item.id)
            if clearing_wallet_id is None:
                continue
            source = unit.wallets.get(item.funding_wallet_id)
            clearing = unit.wallets.get(clearing_wallet_id)
            if source is None or clearing is None:  # pragma: no cover - validated
                raise LedgerConfigurationError("dedicated wallet routing is missing")
            asset = unit.ledger_accounts.get_for_financial_account(
                source.financial_account_id
            )
            if asset is None:  # pragma: no cover - accounts always have one
                raise LedgerConfigurationError(
                    "the dedicated wallet's account has no ledger account"
                )
            postings.extend(
                (
                    PostingDraft(
                        ledger_account_id=asset.id,
                        amount=Money(source_sign * item.amount_minor, event.currency),
                        wallet_id=source.id,
                    ),
                    PostingDraft(
                        ledger_account_id=asset.id,
                        amount=Money(-source_sign * item.amount_minor, event.currency),
                        wallet_id=clearing.id,
                    ),
                )
            )
        return tuple(postings)

    def _reserve_funding(
        self,
        unit: UnitOfWork,
        event: FinancialEvent,
        allocations: tuple[EventAllocation, ...],
        dedicated_routes: dict[int, int],
    ) -> tuple[FundingCommitment, ...]:
        """Reserve every card purchase, same bank or not (ADR-B029)."""
        if event.event_type in _REFUND_TYPES:
            return ()
        card = self._require_ready_card(unit, event)
        assert card.settlement_account_id is not None
        settlement = unit.financial_accounts.get(card.settlement_account_id)
        if settlement is None:  # pragma: no cover - guarded when configured
            raise CardConfigurationError("the card's settlement account is missing")
        commitments: list[FundingCommitment] = []
        for item in allocations:
            wallet = unit.wallets.get(
                dedicated_routes.get(item.id, item.funding_wallet_id)
            )
            if wallet is None or not wallet.is_active:
                raise LedgerConfigurationError("funding wallet was not found")
            funding_account = unit.financial_accounts.get(wallet.financial_account_id)
            if funding_account is None:  # pragma: no cover - wallets need one
                raise LedgerConfigurationError("the funding account is missing")
            commitments.append(
                unit.commitments.add(
                    FundingCommitmentDraft(
                        financial_event_id=event.id,
                        event_allocation_id=item.id,
                        source_wallet_id=wallet.id,
                        settlement_account_id=settlement.id,
                        amount_minor=item.amount_minor,
                        currency=event.currency,
                        fulfillment_mode=fulfillment_mode_for(
                            funding_account_id=funding_account.id,
                            settlement_account_id=settlement.id,
                            funding_institution_code=funding_account.institution_code,
                            settlement_institution_code=settlement.institution_code,
                        ),
                        status=CommitmentStatus.OPEN,
                    )
                )
            )
        return tuple(commitments)

    def _release_original_reservations(
        self,
        unit: UnitOfWork,
        original: FinancialEvent,
        allocations: tuple[EventAllocation, ...],
    ) -> tuple[int, ...]:
        """Release exactly the shares this refund returned money to.

        Spreading the release across every outstanding commitment in
        proportion to its balance is wrong whenever a refund is not spread the
        same way: refunding only the food share of a food-and-transport
        purchase would release part of the transport reservation too, and when
        the two shares are funded from different wallets that misstates both
        wallets' reserved and available balances. Each refunded
        ``(wallet, category)`` share is therefore matched back to the
        commitments created for the original allocations of that same share.

        The repository still bounds every release by the outstanding amount,
        so a repeated refund can never release the same money twice.

        The commitments this call actually released are returned, so the caller
        can re-derive each one's status before committing.
        """
        released: list[int] = []
        original_routes = self._clearing_by_allocation(unit, original.id)
        by_allocation = {
            item.id: (
                item.funding_wallet_id,
                item.budget_category_id,
                original_routes.get(item.id),
            )
            for item in allocations_in_force(unit, original.id)
        }
        outstanding: dict[
            tuple[int, int | None, int | None], list[FundingCommitment]
        ] = {}
        for commitment in unit.commitments.list_for_event(original.id):
            if (
                commitment.outstanding_minor <= 0
                or commitment.status is CommitmentStatus.CANCELLED
            ):
                continue
            share = by_allocation.get(commitment.event_allocation_id)
            if share is None:  # pragma: no cover - a commitment always has one
                continue
            outstanding.setdefault(share, []).append(commitment)

        refund_routes = (
            self._clearing_by_allocation(
                unit, event_id=allocations[0].financial_event_id
            )
            if allocations
            else {}
        )
        refunded: dict[tuple[int, int | None, int | None], int] = {}
        for item in allocations:
            key = (
                item.funding_wallet_id,
                item.budget_category_id,
                refund_routes.get(item.id),
            )
            refunded[key] = refunded.get(key, 0) + item.amount_minor

        for key, amount_minor in refunded.items():
            commitments = outstanding.get(key)
            if not commitments:
                continue
            releasable = sum(item.outstanding_minor for item in commitments)
            refundable = min(amount_minor, releasable)
            if refundable <= 0:  # pragma: no cover - amounts are positive
                continue
            shares = distribute_proportionally(
                refundable, tuple(item.outstanding_minor for item in commitments)
            )
            for commitment, share_minor in zip(commitments, shares, strict=True):
                if share_minor > 0:
                    unit.commitments.release(commitment.id, share_minor)
                    released.append(commitment.id)
        return tuple(released)

    @staticmethod
    def _require_ready_card(unit: UnitOfWork, event: FinancialEvent) -> Card:
        if event.card_id is None:
            raise CardConfigurationError(
                "a credit-card event needs a configured card before it can post"
            )
        card = unit.cards.get(event.card_id)
        if card is None:  # pragma: no cover - guarded by the foreign key
            raise CardConfigurationError("card was not found")
        if not card.is_ready:
            raise CardConfigurationError(
                "configure the card's liability and settlement accounts first"
            )
        return card

    def _expense_ledger(
        self, unit: UnitOfWork, category_id: int | None, currency: str
    ) -> LedgerAccount:
        """Return the single expense identity of one category and currency."""
        if category_id is None:
            code = f"expense:uncategorized:{currency}"
            existing = unit.ledger_accounts.get_by_code(code)
            if existing is not None:
                return existing
            return unit.ledger_accounts.add(
                LedgerAccountDraft(
                    code=code,
                    name=f"{UNCATEGORIZED_LEDGER_NAME} ({currency})",
                    kind=LedgerAccountKind.EXPENSE,
                    currency=currency,
                )
            )
        category = unit.budget_categories.get(category_id)
        if category is None:
            raise BudgetPeriodError("budget category was not found")
        if not category.is_active:
            raise BudgetPeriodError("a deactivated category cannot be allocated")
        existing = unit.ledger_accounts.get_for_budget_category(category_id, currency)
        if existing is not None:
            return existing
        return unit.ledger_accounts.add(
            LedgerAccountDraft(
                code=expense_ledger_account_code(category_id, currency),
                name=category.name,
                kind=LedgerAccountKind.EXPENSE,
                currency=currency,
                budget_category_id=category_id,
            )
        )


@dataclass(frozen=True)
class RedoAllocationRequest:
    """User intent to decide one posted event's allocation again."""

    financial_event_id: int
    expected_version: int


@dataclass(frozen=True)
class RedoAllocationResult:
    """Everything undoing one allocation committed, for display and tests."""

    event: FinancialEvent
    reversed_entry: JournalEntry
    reversal_entry: JournalEntry
    released_commitments: tuple[FundingCommitment, ...]
    superseded_allocations: int


class RedoAllocation:
    """Return one posted event to the allocation queue (ADR-B103).

    An allocation records two decisions the user made -- which wallet funds
    the expense, and which category it belongs to -- and a decision can be
    wrong. Nothing about the event itself is touched: its amount, merchant,
    time, and card came from the bank and stay exactly as they are, which is
    why this is allowed for a Gmail-origin event where reversing the event
    is not.
    """

    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        journal: JournalService,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._journal = journal

    def execute(self, request: RedoAllocationRequest) -> RedoAllocationResult:
        """Reverse the posted entry, release its reservations, and requeue."""
        with self._unit_of_work_factory() as unit:
            event = unit.financial_events.get(request.financial_event_id)
            if event is None:
                raise AllocationRedoError("financial event was not found")
            ensure_redoable_allocation(
                status=event.status,
                event_type=event.event_type,
                payment_method=event.payment_method,
                has_allocation=bool(allocations_in_force(unit, event.id)),
            )
            if event.version != request.expected_version:
                raise ConcurrentUpdateError(
                    "this event changed after the page was loaded"
                )
            entry = unit.journal.get_by_financial_event(event.id)
            if entry is None:
                raise AllocationRedoError(
                    "this event has no posted entry whose allocation could be redone"
                )
            reversal = self._journal.stage_reversal(
                unit,
                entry.id,
                description="Allocation redone",
                idempotency_key=allocation_redo_journal_key(entry.id),
            )
            release_event_reservations(unit, event.id)
            # The allocation rows stay exactly as they are, the way a
            # reversal leaves them (ADR-B069). Every total is derived from
            # posted ledger lines (ADR-B064), and this reversal cancels the
            # ones they produced, so a superseded row adds up to nothing.
            superseded = len(unit.allocations.list_for_entry(entry.id))
            requeued = unit.financial_events.mark_needs_allocation(
                event.id, event.version
            )
            commitments = unit.commitments.list_for_event(event.id)
            unit.commit()
        return RedoAllocationResult(
            event=requeued,
            reversed_entry=entry,
            reversal_entry=reversal,
            released_commitments=commitments,
            superseded_allocations=superseded,
        )

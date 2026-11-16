"""Record bank movements that already happened, with their real shape.

Every command here writes a canonical manual financial event and commits it
with its journal entry and its posted status in one transaction (ADR-B065).
`adjustment` is deliberately unreachable from this module: an adjustment says
the cause is unknown, and these commands all name their cause.

No availability check is applied. The money has already moved at the bank, and
refusing to record reality because a wallet would go negative would leave
Scribe permanently disagreeing with the account it is supposed to track. The
wallet is nevertheless always explicit: `Unallocated` is a choice a person
makes, never a null this module fills in.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from app.application.services.allocation_reads import allocations_in_force
from app.application.services.event_allocation import (
    AllocateFinancialEvent,
    AllocateFinancialEventRequest,
    AllocationLine,
    AllocationResult,
    allocation_idempotency_key,
)
from app.application.services.ledger import JournalService
from app.domain.budget_enums import (
    BankMovementKind,
    FinancialAccountType,
    FinancialEventOrigin,
    FinancialEventStatus,
    FinancialEventType,
    JournalEntryType,
    LedgerAccountKind,
    PaymentMethod,
    RecognitionBasis,
)
from app.domain.budget_models import (
    EventAllocation,
    FinancialAccount,
    FinancialEvent,
    FinancialEventDraft,
    JournalEntry,
    LedgerAccount,
    LedgerAccountDraft,
    Money,
    PostingDraft,
    Wallet,
    normalize_currency,
    normalize_name,
    normalize_note,
    validate_aware,
    validate_event_magnitude,
)
from app.domain.budget_rules import (
    SPENDING_BANK_MOVEMENTS,
    account_transfer_event_key,
    account_transfer_journal_key,
    bank_movement_entry_type,
    bank_movement_event_key,
    bank_movement_event_type,
    bank_movement_journal_key,
    bank_movement_payment_method,
    distribute_proportionally,
    draft_signatures,
    ensure_distinct_transfer_wallets,
    ensure_expected_wallet_balance,
    ensure_same_account_reallocation,
    external_clearing_ledger_code,
    interest_income_ledger_code,
    manual_refund_event_key,
    posted_signatures,
    wallet_reallocation_delta_minor,
    wallet_reallocation_description,
    wallet_reallocation_event_key,
)
from app.domain.errors import (
    BankMovementError,
    CurrencyMismatchError,
    FinancialEventError,
    IdempotencyConflictError,
    LedgerConfigurationError,
)
from app.domain.ports.repositories import UnitOfWork

type UnitOfWorkFactory = Callable[[], UnitOfWork]

MAX_DESCRIPTION_LENGTH = 200
MAX_NOTE_LENGTH = 500

INTEREST_INCOME_NAME = "Interest income"
EXTERNAL_CLEARING_NAME = "External money movement"


@dataclass(frozen=True)
class BankMovementRequest:
    """One movement the bank already made, described by what it was."""

    kind: BankMovementKind
    occurred_at: datetime
    amount_minor: int
    currency: str
    wallet_id: int
    description: str
    client_token: str
    budget_category_id: int | None = None
    note: str | None = None


@dataclass(frozen=True)
class AccountTransferRequest:
    """One tracked movement between two wallets Scribe already knows."""

    source_wallet_id: int
    destination_wallet_id: int
    occurred_at: datetime
    amount_minor: int
    currency: str
    description: str
    client_token: str
    note: str | None = None


@dataclass(frozen=True)
class ReallocateWalletRequest:
    """One edited sheet cell: what this wallet should now hold, and from where.

    The amount is the intended result rather than a delta, because that is
    what the person typed over. `expected_balance_minor` is the number the
    page showed them, so a stale column is refused instead of silently posting
    against a total nobody read.
    """

    wallet_id: int
    counterpart_wallet_id: int
    expected_balance_minor: int
    target_balance_minor: int
    currency: str
    occurred_at: datetime
    client_token: str
    note: str | None = None


@dataclass(frozen=True)
class ManualRefundRequest:
    """One refund of an expense Scribe already posted."""

    original_event_id: int
    occurred_at: datetime
    amount_minor: int
    client_token: str
    note: str | None = None


@dataclass(frozen=True)
class BankMovementResult:
    """The event and entry one recorded movement committed."""

    event: FinancialEvent
    entry: JournalEntry


class RecordBankMovement:
    """Write already-observed bank movements as posted ledger facts."""

    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        journal: JournalService,
        allocator: AllocateFinancialEvent,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._journal = journal
        self._allocator = allocator

    def execute(self, request: BankMovementRequest) -> BankMovementResult:
        """Record interest, an external movement, or a bank-paid expense."""
        occurred_at = validate_aware(request.occurred_at, label="movement time")
        amount_minor = validate_event_magnitude(request.amount_minor)
        currency = normalize_currency(request.currency)
        description = normalize_name(
            request.description,
            label="movement description",
            max_length=MAX_DESCRIPTION_LENGTH,
        )
        note = normalize_note(
            request.note, label="movement note", max_length=MAX_NOTE_LENGTH
        )
        event_type = bank_movement_event_type(request.kind)
        payment_method = bank_movement_payment_method(request.kind)
        if request.kind in SPENDING_BANK_MOVEMENTS:
            if request.budget_category_id is None:
                raise BankMovementError(
                    "spending from a bank account or consumed-cash withdrawal "
                    "must name its budget category"
                )
        elif request.budget_category_id is not None:
            raise BankMovementError("only spending is assigned to a category")
        key = bank_movement_event_key(request.client_token)

        with self._unit_of_work_factory() as unit:
            existing = unit.financial_events.get_by_idempotency_key(key)
            if existing is not None:
                wallet, account = self._historical_asset_wallet(
                    unit, request.wallet_id, currency=currency
                )
                self._require_cash_withdrawal_source(request.kind, account)
                replay = self._replay(
                    unit,
                    existing,
                    event_type=event_type,
                    payment_method=payment_method,
                    occurred_at=occurred_at,
                    amount_minor=amount_minor,
                    currency=currency,
                    description=description,
                    note=note,
                    entry_type=bank_movement_entry_type(request.kind),
                    journal_key=(
                        allocation_idempotency_key(existing.id, request.client_token)
                        if request.kind in SPENDING_BANK_MOVEMENTS
                        else bank_movement_journal_key(existing.id)
                    ),
                    postings=(
                        ()
                        if request.kind in SPENDING_BANK_MOVEMENTS
                        else self._two_sided_postings(
                            unit,
                            kind=request.kind,
                            wallet=wallet,
                            account=account,
                            amount_minor=amount_minor,
                            currency=currency,
                        )
                    ),
                    lines=(
                        ((amount_minor, wallet.id, request.budget_category_id),)
                        if request.kind in SPENDING_BANK_MOVEMENTS
                        else ()
                    ),
                )
                unit.commit()
                return replay
            wallet, account = self._require_asset_wallet(
                unit, request.wallet_id, currency=currency
            )
            self._require_cash_withdrawal_source(request.kind, account)
            event = unit.financial_events.add(
                FinancialEventDraft(
                    origin=FinancialEventOrigin.MANUAL,
                    event_type=event_type,
                    payment_method=payment_method,
                    recognition_basis=RecognitionBasis.OBSERVED_MANUAL,
                    occurred_at=occurred_at,
                    amount_minor=amount_minor,
                    currency=currency,
                    idempotency_key=key,
                    merchant=description,
                    status=FinancialEventStatus.NEEDS_ALLOCATION,
                    note=note,
                )
            )
            if request.kind in SPENDING_BANK_MOVEMENTS:
                allocation = self._allocator.stage(
                    unit,
                    AllocateFinancialEventRequest(
                        financial_event_id=event.id,
                        expected_version=event.version,
                        client_token=request.client_token,
                        lines=(
                            AllocationLine(
                                amount_minor=amount_minor,
                                funding_wallet_id=wallet.id,
                                budget_category_id=request.budget_category_id,
                            ),
                        ),
                    ),
                )
                unit.commit()
                return BankMovementResult(
                    event=allocation.event, entry=allocation.entry
                )
            postings = self._two_sided_postings(
                unit,
                kind=request.kind,
                wallet=wallet,
                account=account,
                amount_minor=amount_minor,
                currency=currency,
            )
            entry = self._journal.stage_balanced(
                unit,
                entry_type=bank_movement_entry_type(request.kind),
                effective_at=occurred_at,
                idempotency_key=bank_movement_journal_key(event.id),
                description=description,
                postings=postings,
                financial_event_id=event.id,
            )
            posted = unit.financial_events.mark_posted(event.id, event.version)
            unit.commit()
        return BankMovementResult(event=posted, entry=entry)

    @staticmethod
    def _require_cash_withdrawal_source(
        kind: BankMovementKind, account: FinancialAccount
    ) -> None:
        """Keep consumed-cash withdrawals attached to an actual bank account."""
        if (
            kind is BankMovementKind.CASH_WITHDRAWAL_SPENDING
            and account.account_type
            not in {FinancialAccountType.CHECKING, FinancialAccountType.SAVINGS}
        ):
            raise BankMovementError(
                "a cash withdrawal counted as spending must come from a checking "
                "or savings account"
            )

    def transfer(self, request: AccountTransferRequest) -> BankMovementResult:
        """Move money between two tracked wallets without spending anything.

        A transfer is not an expense, so it creates no allocation, no
        commitment, and no expense posting. Recording one as spending would
        double-count every internal move as consumption.
        """
        occurred_at = validate_aware(request.occurred_at, label="transfer time")
        amount_minor = validate_event_magnitude(request.amount_minor)
        currency = normalize_currency(request.currency)
        description = normalize_name(
            request.description,
            label="transfer description",
            max_length=MAX_DESCRIPTION_LENGTH,
        )
        note = normalize_note(
            request.note, label="transfer note", max_length=MAX_NOTE_LENGTH
        )
        ensure_distinct_transfer_wallets(
            source_wallet_id=request.source_wallet_id,
            destination_wallet_id=request.destination_wallet_id,
        )
        key = account_transfer_event_key(request.client_token)

        with self._unit_of_work_factory() as unit:
            result = self._stage_wallet_move(
                unit,
                key=key,
                source_wallet_id=request.source_wallet_id,
                destination_wallet_id=request.destination_wallet_id,
                occurred_at=occurred_at,
                amount_minor=amount_minor,
                currency=currency,
                description=description,
                note=note,
            )
            unit.commit()
        return result

    def reallocate(self, request: ReallocateWalletRequest) -> BankMovementResult | None:
        """Apply one edited wallet cell as a move inside its own account.

        The account total never changes here, so this posts the same internal
        transfer a person could have entered by hand rather than an
        adjustment: the cause is known and named, and an adjustment claims the
        opposite (ADR-B065, ADR-B086). Typing back the number already shown is
        not an edit, and returns `None` instead of posting an empty entry.
        """
        occurred_at = validate_aware(request.occurred_at, label="reallocation time")
        currency = normalize_currency(request.currency)
        note = normalize_note(
            request.note, label="reallocation note", max_length=MAX_NOTE_LENGTH
        )
        ensure_distinct_transfer_wallets(
            source_wallet_id=request.wallet_id,
            destination_wallet_id=request.counterpart_wallet_id,
        )
        delta_minor = wallet_reallocation_delta_minor(
            expected_minor=request.expected_balance_minor,
            target_minor=request.target_balance_minor,
        )
        key = wallet_reallocation_event_key(request.client_token)

        with self._unit_of_work_factory() as unit:
            existing = unit.financial_events.get_by_idempotency_key(key)
            wallet_lookup = (
                self._historical_asset_wallet
                if existing is not None
                else self._require_asset_wallet
            )
            edited, edited_account = wallet_lookup(
                unit, request.wallet_id, currency=currency
            )
            counterpart, counterpart_account = wallet_lookup(
                unit, request.counterpart_wallet_id, currency=currency
            )
            ensure_same_account_reallocation(
                source_account_id=edited_account.id,
                destination_account_id=counterpart_account.id,
            )
            if existing is None:
                # A replay re-proves the entry it already wrote, so its
                # starting balance is deliberately the one that edit moved
                # away from and can no longer match.
                ensure_expected_wallet_balance(
                    expected_minor=request.expected_balance_minor,
                    actual_minor=unit.budget_ledger.wallet_balance(edited.id),
                )
            else:
                # An edited cell carries no time of its own, so a first
                # submission is stamped on arrival. A retry describes that
                # same historical move, not a second one happening now.
                occurred_at = existing.occurred_at
            if delta_minor == 0:
                return None
            description = wallet_reallocation_description(
                edited_wallet_name=edited.name,
                counterpart_wallet_name=counterpart.name,
                delta_minor=delta_minor,
            )
            source_id, destination_id = (
                (counterpart.id, edited.id)
                if delta_minor > 0
                else (edited.id, counterpart.id)
            )
            result = self._stage_wallet_move(
                unit,
                key=key,
                source_wallet_id=source_id,
                destination_wallet_id=destination_id,
                occurred_at=occurred_at,
                amount_minor=abs(delta_minor),
                currency=currency,
                description=description,
                note=note,
            )
            unit.commit()
        return result

    def _stage_wallet_move(
        self,
        unit: UnitOfWork,
        *,
        key: str,
        source_wallet_id: int,
        destination_wallet_id: int,
        occurred_at: datetime,
        amount_minor: int,
        currency: str,
        description: str,
        note: str | None,
    ) -> BankMovementResult:
        """Stage one internal transfer, replaying it if its token already ran.

        The caller commits. Both the hand-entered transfer form and an edited
        sheet cell end here so that one idempotency and replay path covers
        every wallet-to-wallet move.
        """
        existing = unit.financial_events.get_by_idempotency_key(key)
        wallet_lookup = (
            self._historical_asset_wallet
            if existing is not None
            else self._require_asset_wallet
        )
        source, source_account = wallet_lookup(
            unit, source_wallet_id, currency=currency
        )
        destination, destination_account = wallet_lookup(
            unit, destination_wallet_id, currency=currency
        )
        transfer_postings = (
            PostingDraft(
                ledger_account_id=self._physical_ledger(unit, destination_account).id,
                amount=Money(amount_minor, currency),
                wallet_id=destination.id,
            ),
            PostingDraft(
                ledger_account_id=self._physical_ledger(unit, source_account).id,
                amount=Money(-amount_minor, currency),
                wallet_id=source.id,
            ),
        )
        if existing is not None:
            return self._replay(
                unit,
                existing,
                event_type=FinancialEventType.TRANSFER,
                payment_method=PaymentMethod.TRANSFER,
                occurred_at=occurred_at,
                amount_minor=amount_minor,
                currency=currency,
                description=description,
                note=note,
                entry_type=JournalEntryType.INTERNAL_TRANSFER,
                journal_key=account_transfer_journal_key(existing.id),
                postings=transfer_postings,
                lines=(),
            )
        event = unit.financial_events.add(
            FinancialEventDraft(
                origin=FinancialEventOrigin.MANUAL,
                event_type=FinancialEventType.TRANSFER,
                payment_method=PaymentMethod.TRANSFER,
                recognition_basis=RecognitionBasis.OBSERVED_MANUAL,
                occurred_at=occurred_at,
                amount_minor=amount_minor,
                currency=currency,
                idempotency_key=key,
                merchant=description,
                status=FinancialEventStatus.NEEDS_ALLOCATION,
                note=note,
            )
        )
        entry = self._journal.stage_balanced(
            unit,
            entry_type=JournalEntryType.INTERNAL_TRANSFER,
            effective_at=occurred_at,
            idempotency_key=account_transfer_journal_key(event.id),
            description=description,
            postings=transfer_postings,
            financial_event_id=event.id,
        )
        posted = unit.financial_events.mark_posted(event.id, event.version)
        return BankMovementResult(event=posted, entry=entry)

    def refund(self, request: ManualRefundRequest) -> AllocationResult:
        """Record one refund of a posted expense, keeping every B3 rule.

        The refund is allocated through `AllocateFinancialEvent`, so the
        cumulative cap, the per-share cap, the same-card and same-method rules,
        the refusal to refund a refund, and the single release of each reserved
        share are exactly the ones B3 already enforces.
        """
        occurred_at = validate_aware(request.occurred_at, label="refund time")
        amount_minor = validate_event_magnitude(request.amount_minor)
        note = normalize_note(
            request.note, label="refund note", max_length=MAX_NOTE_LENGTH
        )
        key = manual_refund_event_key(request.client_token)

        with self._unit_of_work_factory() as unit:
            original = unit.financial_events.get(request.original_event_id)
            if original is None:
                raise FinancialEventError("the refunded event was not found")
            existing = unit.financial_events.get_by_idempotency_key(key)
            if existing is not None:
                replay = self._replay_refund(
                    unit,
                    existing,
                    original=original,
                    occurred_at=occurred_at,
                    amount_minor=amount_minor,
                    note=note,
                    client_token=request.client_token,
                )
                unit.commit()
                return replay
            event = unit.financial_events.add(
                FinancialEventDraft(
                    origin=FinancialEventOrigin.MANUAL,
                    event_type=FinancialEventType.REFUND,
                    payment_method=original.payment_method,
                    recognition_basis=RecognitionBasis.OBSERVED_MANUAL,
                    occurred_at=occurred_at,
                    amount_minor=amount_minor,
                    currency=original.currency,
                    idempotency_key=key,
                    merchant=original.merchant,
                    card_id=original.card_id,
                    status=FinancialEventStatus.NEEDS_ALLOCATION,
                    note=note,
                    refund_of_event_id=original.id,
                )
            )
            lines = tuple(
                AllocationLine(
                    amount_minor=line.amount_minor,
                    funding_wallet_id=line.funding_wallet_id,
                    budget_category_id=line.budget_category_id,
                    clearing_wallet_id=line.clearing_wallet_id,
                )
                for line in self._allocator.propose_refund_lines_in(unit, event.id)
            )
            if not lines:
                raise FinancialEventError(
                    "the refunded event has nothing left to return"
                )
            result = self._allocator.stage(
                unit,
                AllocateFinancialEventRequest(
                    financial_event_id=event.id,
                    expected_version=event.version,
                    client_token=request.client_token,
                    lines=lines,
                ),
            )
            unit.commit()
        return result

    def _two_sided_postings(
        self,
        unit: UnitOfWork,
        *,
        kind: BankMovementKind,
        wallet: Wallet,
        account: FinancialAccount,
        amount_minor: int,
        currency: str,
    ) -> tuple[PostingDraft, ...]:
        """Return the two lines one non-spending bank movement posts.

        Interest raises the wallet against income; an external deposit or
        withdrawal moves it against a clearing account that represents the
        world outside every tracked account. Calling an external deposit
        income would make an incoming transfer look like earnings.
        """
        physical = self._physical_ledger(unit, account)
        if kind is BankMovementKind.INTEREST_INCOME:
            counter = self._named_ledger(
                unit,
                code=interest_income_ledger_code(currency),
                name=f"{INTEREST_INCOME_NAME} ({currency})",
                kind=LedgerAccountKind.INCOME,
                currency=currency,
            )
            sign = 1
        else:
            counter = self._named_ledger(
                unit,
                code=external_clearing_ledger_code(currency),
                name=f"{EXTERNAL_CLEARING_NAME} ({currency})",
                kind=LedgerAccountKind.CLEARING,
                currency=currency,
            )
            sign = 1 if kind is BankMovementKind.EXTERNAL_DEPOSIT else -1
        return (
            PostingDraft(
                ledger_account_id=physical.id,
                amount=Money(sign * amount_minor, currency),
                wallet_id=wallet.id,
            ),
            PostingDraft(
                ledger_account_id=counter.id,
                amount=Money(-sign * amount_minor, currency),
            ),
        )

    @staticmethod
    def _physical_ledger(unit: UnitOfWork, account: FinancialAccount) -> LedgerAccount:
        ledger_account = unit.ledger_accounts.get_for_financial_account(account.id)
        if ledger_account is None:  # pragma: no cover - accounts always have one
            raise LedgerConfigurationError(
                "this account has no physical ledger account"
            )
        return ledger_account

    @staticmethod
    def _named_ledger(
        unit: UnitOfWork,
        *,
        code: str,
        name: str,
        kind: LedgerAccountKind,
        currency: str,
    ) -> LedgerAccount:
        existing = unit.ledger_accounts.get_by_code(code)
        if existing is not None:
            return existing
        return unit.ledger_accounts.add(
            LedgerAccountDraft(code=code, name=name, kind=kind, currency=currency)
        )

    @staticmethod
    def _require_asset_wallet(
        unit: UnitOfWork, wallet_id: int, *, currency: str
    ) -> tuple[Wallet, FinancialAccount]:
        """Return the explicit wallet a movement touched, or refuse to guess."""
        wallet, account = RecordBankMovement._historical_asset_wallet(
            unit, wallet_id, currency=currency
        )
        if not wallet.is_active:
            raise LedgerConfigurationError("wallet was not found")
        if not account.is_active:
            raise LedgerConfigurationError("the wallet's account is closed")
        return wallet, account

    @staticmethod
    def _historical_asset_wallet(
        unit: UnitOfWork, wallet_id: int, *, currency: str
    ) -> tuple[Wallet, FinancialAccount]:
        """Load the asset identity a committed entry already names.

        New movements require active configuration, but a replay describes a
        historical fact. Later deactivation must not turn an identical retry
        into a failure. RESTRICT foreign keys keep both identities present
        while allocations or postings still point at them.
        """
        wallet = unit.wallets.get(wallet_id)
        if wallet is None:
            raise LedgerConfigurationError("wallet was not found")
        account = unit.financial_accounts.get(wallet.financial_account_id)
        if account is None:  # pragma: no cover - wallets always have one
            raise LedgerConfigurationError("the wallet's account is missing")
        if account.currency != currency:
            raise CurrencyMismatchError(
                "a wallet only moves money in its own account's currency"
            )
        return wallet, account

    def _replay(
        self,
        unit: UnitOfWork,
        existing: FinancialEvent,
        *,
        event_type: FinancialEventType,
        payment_method: PaymentMethod,
        occurred_at: datetime,
        amount_minor: int,
        currency: str,
        description: str,
        note: str | None,
        entry_type: JournalEntryType,
        journal_key: str,
        postings: tuple[PostingDraft, ...],
        lines: tuple[tuple[int, int, int | None], ...],
    ) -> BankMovementResult:
        """Return the first submission, or refuse a token used differently.

        Comparing the event's own columns is not enough. A movement's *money
        position* lives in its postings and, for spending, in its allocation:
        the same token, amount, and description can name a completely different
        wallet, a different category, or a transfer running the other way. Each
        of those is a different fact about where the money is, so the whole
        canonical shape is re-derived from this request and compared with what
        the first submission actually wrote.
        """
        same_event = (
            existing.origin is FinancialEventOrigin.MANUAL
            and existing.recognition_basis is RecognitionBasis.OBSERVED_MANUAL
            and existing.event_type is event_type
            and existing.payment_method is payment_method
            and existing.status is FinancialEventStatus.POSTED
            and existing.occurred_at == occurred_at
            and existing.amount_minor == amount_minor
            and existing.currency == currency
            and existing.merchant == description
            and existing.note == note
            and existing.refund_of_event_id is None
            and existing.card_id is None
        )
        if not same_event:
            raise IdempotencyConflictError(
                "this token already records a different bank movement"
            )
        entry = self._require_entry(unit, existing.id)
        if (
            entry.entry_type is not entry_type
            or entry.effective_at != occurred_at
            or entry.financial_event_id != existing.id
            or entry.reversal_of_id is not None
        ):
            raise IdempotencyConflictError(
                "this token already records a differently posted movement"
            )
        if entry.idempotency_key != journal_key:
            raise IdempotencyConflictError(
                "this token already records a differently posted movement"
            )
        if lines:
            allocations = _ensure_same_allocation(unit, existing, lines)
            postings = self._allocation_postings(unit, existing, allocations)
        if posted_signatures(entry.postings) != draft_signatures(postings):
            raise IdempotencyConflictError(
                "this token already records money in another place"
            )
        return BankMovementResult(event=existing, entry=entry)

    def _replay_refund(
        self,
        unit: UnitOfWork,
        existing: FinancialEvent,
        *,
        original: FinancialEvent,
        occurred_at: datetime,
        amount_minor: int,
        note: str | None,
        client_token: str,
    ) -> AllocationResult:
        """Return the first refund, or refuse a token used for another one."""
        same = (
            existing.origin is FinancialEventOrigin.MANUAL
            and existing.recognition_basis is RecognitionBasis.OBSERVED_MANUAL
            and existing.event_type is FinancialEventType.REFUND
            and existing.payment_method is original.payment_method
            and existing.status is FinancialEventStatus.POSTED
            and existing.refund_of_event_id == original.id
            and existing.occurred_at == occurred_at
            and existing.amount_minor == amount_minor
            and existing.currency == original.currency
            and existing.merchant == original.merchant
            and existing.card_id == original.card_id
            and existing.note == note
        )
        if not same:
            raise IdempotencyConflictError(
                "this token already records a different refund"
            )
        entry = self._require_entry(unit, existing.id)
        if (
            entry.entry_type is not JournalEntryType.REFUND
            or entry.financial_event_id != existing.id
            or entry.effective_at != occurred_at
            or entry.reversal_of_id is not None
            or entry.idempotency_key
            != allocation_idempotency_key(existing.id, client_token)
        ):
            raise IdempotencyConflictError(
                "this token already records a differently posted refund"
            )
        allocations = unit.allocations.list_for_entry(entry.id)
        expected_lines = self._expected_refund_lines(unit, original, existing.id)
        try:
            _ensure_same_allocation(unit, existing, expected_lines)
        except IdempotencyConflictError as error:
            raise IdempotencyConflictError(
                "this token already records a refund in another place"
            ) from error
        expected_postings = self._allocation_postings(unit, existing, allocations)
        if posted_signatures(entry.postings) != draft_signatures(expected_postings):
            raise IdempotencyConflictError(
                "this token already records a differently posted refund"
            )
        return AllocationResult(
            event=existing,
            allocations=allocations,
            commitments=unit.commitments.list_for_event(existing.id),
            entry=entry,
        )

    def _allocation_postings(
        self,
        unit: UnitOfWork,
        event: FinancialEvent,
        allocations: tuple[EventAllocation, ...],
    ) -> tuple[PostingDraft, ...]:
        """Reconstruct an allocated event's immutable journal shape."""
        refund = event.event_type is FinancialEventType.REFUND
        expense_sign = -1 if refund else 1
        postings: list[PostingDraft] = []
        for allocation in allocations:
            expense = self._expense_ledger_for_replay(
                unit, allocation.budget_category_id, event.currency
            )
            postings.append(
                PostingDraft(
                    ledger_account_id=expense.id,
                    amount=Money(
                        expense_sign * allocation.amount_minor, event.currency
                    ),
                    budget_category_id=allocation.budget_category_id,
                )
            )

        if event.payment_method is PaymentMethod.CREDIT_CARD:
            if not refund or event.refund_of_event_id is None:
                raise IdempotencyConflictError(
                    "this token records an unsupported card movement"
                )
            original_entry = self._require_entry(unit, event.refund_of_event_id)
            liability_ids = tuple(
                posting.ledger_account_id
                for posting in original_entry.postings
                if self._ledger_kind(unit, posting.ledger_account_id)
                is LedgerAccountKind.LIABILITY
            )
            if len(liability_ids) != 1:
                raise IdempotencyConflictError(
                    "the original expense has no single card liability"
                )
            postings.append(
                PostingDraft(
                    ledger_account_id=liability_ids[0],
                    amount=Money(event.amount_minor, event.currency),
                )
            )
        elif event.payment_method in {PaymentMethod.CASH, PaymentMethod.BANK_ACCOUNT}:
            asset_sign = 1 if refund else -1
            for allocation in allocations:
                wallet, account = self._historical_asset_wallet(
                    unit, allocation.funding_wallet_id, currency=event.currency
                )
                postings.append(
                    PostingDraft(
                        ledger_account_id=self._physical_ledger(unit, account).id,
                        amount=Money(
                            asset_sign * allocation.amount_minor, event.currency
                        ),
                        wallet_id=wallet.id,
                    )
                )
        else:
            raise IdempotencyConflictError(
                "this token records an unsupported payment method"
            )
        return tuple(postings)

    @staticmethod
    def _expense_ledger_for_replay(
        unit: UnitOfWork, category_id: int | None, currency: str
    ) -> LedgerAccount:
        ledger = (
            unit.ledger_accounts.get_by_code(f"expense:uncategorized:{currency}")
            if category_id is None
            else unit.ledger_accounts.get_for_budget_category(category_id, currency)
        )
        if ledger is None or ledger.kind is not LedgerAccountKind.EXPENSE:
            raise IdempotencyConflictError(
                "this token records an allocation without its expense account"
            )
        return ledger

    @staticmethod
    def _ledger_kind(unit: UnitOfWork, ledger_account_id: int) -> LedgerAccountKind:
        ledger = unit.ledger_accounts.get(ledger_account_id)
        if ledger is None:
            raise IdempotencyConflictError("a journal posting lost its ledger account")
        return ledger.kind

    @staticmethod
    def _expected_refund_lines(
        unit: UnitOfWork, original: FinancialEvent, refund_id: int
    ) -> tuple[tuple[int, int, int | None], ...]:
        """Replay every deterministic refund split through the requested row."""
        remaining: dict[tuple[int, int | None], int] = {}
        for allocation in allocations_in_force(unit, original.id):
            key = (allocation.funding_wallet_id, allocation.budget_category_id)
            remaining[key] = remaining.get(key, 0) + allocation.amount_minor

        for refund in unit.financial_events.list_posted_refunds(original.id):
            available = [(key, value) for key, value in remaining.items() if value > 0]
            if not available:
                raise IdempotencyConflictError(
                    "a posted refund exceeds the original allocation"
                )
            shares = distribute_proportionally(
                refund.amount_minor, tuple(value for _, value in available)
            )
            expected = tuple(
                (share, key[0], key[1])
                for (key, _), share in zip(available, shares, strict=True)
                if share > 0
            )
            actual = tuple(
                (
                    allocation.amount_minor,
                    allocation.funding_wallet_id,
                    allocation.budget_category_id,
                )
                for allocation in allocations_in_force(unit, refund.id)
            )
            if sorted(actual) != sorted(expected):
                raise IdempotencyConflictError(
                    "a posted refund no longer matches its deterministic split"
                )
            if refund.id == refund_id:
                return expected
            for amount, wallet_id, category_id in expected:
                remaining[(wallet_id, category_id)] -= amount
        raise IdempotencyConflictError("the refund is not part of its original event")

    @staticmethod
    def _require_entry(unit: UnitOfWork, event_id: int) -> JournalEntry:
        entry = unit.journal.get_by_financial_event(event_id)
        if entry is None:
            raise IdempotencyConflictError(
                "this token records a movement that was never posted"
            )
        return entry


def _ensure_same_allocation(
    unit: UnitOfWork,
    event: FinancialEvent,
    lines: tuple[tuple[int, int | None, int | None], ...],
) -> tuple[EventAllocation, ...]:
    """Refuse a replay whose split names another wallet or another category.

    The wallet is where the money actually is and the category is what it was
    for. A token that answers "already recorded" while the stored rows put the
    money somewhere else is worse than an error: the page says the correction
    was applied and the ledger disagrees for good.
    """
    allocations = allocations_in_force(unit, event.id)
    recorded = sorted(
        (item.amount_minor, item.funding_wallet_id, item.budget_category_id or 0)
        for item in allocations
    )
    requested = sorted(
        (amount, wallet_id or 0, category_id or 0)
        for amount, wallet_id, category_id in lines
    )
    if recorded != requested:
        raise IdempotencyConflictError(
            "this token already records money in another place"
        )
    return allocations

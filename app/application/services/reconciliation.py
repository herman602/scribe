"""Compare what a bank really held with what the ledger believes.

A snapshot is an observation and posts nothing (ADR-B066). Only an explicit,
reasoned adjustment writes an entry, and it recomputes the difference as of the
observation inside its own transaction (ADR-B067), so a backdated movement
entered a second earlier can still make the gap disappear instead of being
papered over.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from app.application.services.ledger import JournalService
from app.domain.budget_enums import (
    BalanceSnapshotSource,
    FinancialEventOrigin,
    FinancialEventStatus,
    FinancialEventType,
    JournalEntryType,
    LedgerAccountKind,
    PaymentMethod,
    RecognitionBasis,
)
from app.domain.budget_models import (
    MAX_SNAPSHOT_NOTE_LENGTH,
    BalanceSnapshot,
    BalanceSnapshotDraft,
    FinancialAccount,
    FinancialEvent,
    FinancialEventDraft,
    JournalEntry,
    LedgerAccount,
    LedgerAccountDraft,
    Money,
    PostingDraft,
    normalize_name,
    normalize_note,
    validate_aware,
    validate_signed_minor_units,
)
from app.domain.budget_rules import (
    balance_snapshot_key,
    draft_signatures,
    holds_wallets,
    posted_signatures,
    reconciliation_adjustment_event_key,
    reconciliation_adjustment_journal_key,
    reconciliation_difference_minor,
    reconciliation_equity_ledger_code,
)
from app.domain.errors import (
    BalanceSnapshotError,
    IdempotencyConflictError,
    LedgerConfigurationError,
    ReconciliationError,
    StaleReconciliationError,
)
from app.domain.ports.repositories import UnitOfWork

type UnitOfWorkFactory = Callable[[], UnitOfWork]

MAX_REASON_LENGTH = 200
RECONCILIATION_EQUITY_NAME = "Reconciliation difference"


@dataclass(frozen=True)
class BalanceSnapshotRequest:
    """One reading of a bank app, written down exactly as it was seen."""

    financial_account_id: int
    observed_balance_minor: int
    observed_at: datetime
    client_token: str
    note: str | None = None
    source: BalanceSnapshotSource = BalanceSnapshotSource.MANUAL_BANK_APP


@dataclass(frozen=True)
class ReconciliationAdjustmentRequest:
    """User intent to accept one difference and say why it exists."""

    snapshot_id: int
    expected_difference_minor: int
    reason: str
    client_token: str


@dataclass(frozen=True)
class ReconciliationView:
    """What one observation says next to what the ledger says."""

    snapshot: BalanceSnapshot
    account: FinancialAccount
    ledger_balance_minor: int

    @property
    def difference_minor(self) -> int:
        """Return observed minus ledger, signed, in exact minor units."""
        return reconciliation_difference_minor(
            observed_balance_minor=self.snapshot.observed_balance_minor,
            ledger_balance_minor=self.ledger_balance_minor,
        )

    @property
    def is_reconciled(self) -> bool:
        """Return whether the bank and the ledger already agree."""
        return self.difference_minor == 0


@dataclass(frozen=True)
class ReconciliationAdjustmentResult:
    """The event and entry one accepted difference committed."""

    snapshot: BalanceSnapshot
    event: FinancialEvent
    entry: JournalEntry
    difference_minor: int


class RecordBalanceSnapshot:
    """Write down what a bank account really held, and nothing else."""

    def __init__(self, unit_of_work_factory: UnitOfWorkFactory) -> None:
        self._unit_of_work_factory = unit_of_work_factory

    def execute(self, request: BalanceSnapshotRequest) -> BalanceSnapshot:
        """Record one immutable observation without touching the ledger."""
        observed_at = validate_aware(request.observed_at, label="observed time")
        observed_balance_minor = validate_signed_minor_units(
            request.observed_balance_minor, label="observed balance"
        )
        note = normalize_note(
            request.note,
            label="snapshot note",
            max_length=MAX_SNAPSHOT_NOTE_LENGTH,
        )
        key = balance_snapshot_key(request.client_token)

        with self._unit_of_work_factory() as unit:
            existing = unit.balance_snapshots.get_by_idempotency_key(key)
            if existing is not None:
                same = (
                    existing.financial_account_id == request.financial_account_id
                    and existing.observed_balance_minor == observed_balance_minor
                    and existing.observed_at == observed_at
                    and existing.source is request.source
                    and existing.note == note
                )
                if not same:
                    raise IdempotencyConflictError(
                        "this token already records a different observation"
                    )
                return existing
            account = unit.financial_accounts.get(request.financial_account_id)
            if account is None or not account.is_active:
                raise BalanceSnapshotError("financial account was not found")
            _require_reconcilable(unit, account)
            snapshot = unit.balance_snapshots.add(
                BalanceSnapshotDraft(
                    financial_account_id=account.id,
                    observed_balance_minor=observed_balance_minor,
                    observed_at=observed_at,
                    idempotency_key=key,
                    source=request.source,
                    note=note,
                )
            )
            unit.commit()
        return snapshot

    def view(self, snapshot_id: int) -> ReconciliationView:
        """Return one observation beside the ledger balance as of that instant."""
        with self._unit_of_work_factory() as unit:
            return _view(unit, snapshot_id)


class AdjustReconciliation:
    """Accept one reasoned difference between the bank and the ledger."""

    def __init__(
        self, unit_of_work_factory: UnitOfWorkFactory, journal: JournalService
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._journal = journal

    def execute(
        self, request: ReconciliationAdjustmentRequest
    ) -> ReconciliationAdjustmentResult:
        """Recompute the difference and post exactly it, or refuse.

        The difference is recomputed here rather than trusted from the form: a
        backdated movement entered in between legitimately makes an old gap
        zero, and a zero gap must post nothing at all.
        """
        reason = normalize_name(
            request.reason, label="adjustment reason", max_length=MAX_REASON_LENGTH
        )
        key = reconciliation_adjustment_event_key(
            request.snapshot_id, request.client_token
        )

        with self._unit_of_work_factory() as unit:
            view = _view(unit, request.snapshot_id)
            existing = unit.financial_events.get_by_idempotency_key(key)
            if existing is not None:
                replay = self._replay(
                    unit,
                    existing,
                    view=view,
                    expected_difference_minor=request.expected_difference_minor,
                    reason=reason,
                )
                unit.commit()
                return replay
            difference = view.difference_minor
            if difference == 0:
                raise ReconciliationError(
                    "the bank and the ledger already agree at that moment; "
                    "there is nothing to adjust"
                )
            if difference != request.expected_difference_minor:
                raise StaleReconciliationError(
                    "this difference changed after the page was loaded; "
                    "reload before adjusting"
                )
            event = unit.financial_events.add(
                FinancialEventDraft(
                    origin=FinancialEventOrigin.MANUAL,
                    event_type=FinancialEventType.ADJUSTMENT,
                    payment_method=PaymentMethod.OTHER,
                    recognition_basis=RecognitionBasis.OBSERVED_MANUAL,
                    occurred_at=view.snapshot.observed_at,
                    amount_minor=abs(difference),
                    currency=view.account.currency,
                    idempotency_key=key,
                    status=FinancialEventStatus.NEEDS_ALLOCATION,
                    note=reason,
                )
            )
            entry = self._journal.stage_balanced(
                unit,
                entry_type=JournalEntryType.ADJUSTMENT,
                effective_at=view.snapshot.observed_at,
                idempotency_key=reconciliation_adjustment_journal_key(event.id),
                description=reason,
                postings=self._postings(unit, view=view, difference=difference),
                financial_event_id=event.id,
            )
            posted = unit.financial_events.mark_posted(event.id, event.version)
            unit.commit()
        return ReconciliationAdjustmentResult(
            snapshot=view.snapshot,
            event=posted,
            entry=entry,
            difference_minor=difference,
        )

    def _postings(
        self, unit: UnitOfWork, *, view: ReconciliationView, difference: int
    ) -> tuple[PostingDraft, ...]:
        """Move the account by the difference against reconciliation equity.

        The asset side carries the signed difference, so a bank holding more
        than the ledger raises the account and a bank holding less lowers it.
        The counter-side is equity rather than an expense: an unexplained gap
        is not spending, and filing it as spending would corrupt a category
        that never bought anything.

        The wallet is the account's `Unallocated` envelope, because that is
        exactly what an unexplained difference is: money whose purpose is not
        known yet. Assigning it to a budget wallet would silently give a
        category a balance nobody planned.
        """
        physical = unit.ledger_accounts.get_for_financial_account(view.account.id)
        if physical is None:  # pragma: no cover - checked when the snapshot is made
            raise LedgerConfigurationError(
                "this account has no physical ledger account"
            )
        wallet = unit.wallets.get_unallocated(view.account.id)
        if wallet is None:
            raise LedgerConfigurationError(
                "this account has no Unallocated wallet to hold a difference"
            )
        equity = self._equity_ledger(unit, view.account.currency)
        return (
            PostingDraft(
                ledger_account_id=physical.id,
                amount=Money(difference, view.account.currency),
                wallet_id=wallet.id,
            ),
            PostingDraft(
                ledger_account_id=equity.id,
                amount=Money(-difference, view.account.currency),
            ),
        )

    @staticmethod
    def _equity_ledger(unit: UnitOfWork, currency: str) -> LedgerAccount:
        code = reconciliation_equity_ledger_code(currency)
        existing = unit.ledger_accounts.get_by_code(code)
        if existing is not None:
            return existing
        return unit.ledger_accounts.add(
            LedgerAccountDraft(
                code=code,
                name=f"{RECONCILIATION_EQUITY_NAME} ({currency})",
                kind=LedgerAccountKind.EQUITY,
                currency=currency,
            )
        )

    def _replay(
        self,
        unit: UnitOfWork,
        existing: FinancialEvent,
        *,
        view: ReconciliationView,
        expected_difference_minor: int,
        reason: str,
    ) -> ReconciliationAdjustmentResult:
        """Return the first adjustment, or refuse a token used differently.

        The difference is deliberately not recomputed here: this adjustment is
        itself part of the ledger now, so the gap it closed reads as zero. What
        is verified instead is the whole canonical shape — the event, the entry,
        and both postings — because a token that merely *looks* used is not
        proof that it was used for this snapshot, this account, this direction,
        and this wallet. The snapshot itself is already part of the key, so a
        resubmission aimed at a different observation never reaches this
        method at all.
        """
        same_event = (
            existing.origin is FinancialEventOrigin.MANUAL
            and existing.recognition_basis is RecognitionBasis.OBSERVED_MANUAL
            and existing.event_type is FinancialEventType.ADJUSTMENT
            and existing.payment_method is PaymentMethod.OTHER
            and existing.status is FinancialEventStatus.POSTED
            and existing.occurred_at == view.snapshot.observed_at
            and existing.currency == view.account.currency
            and existing.amount_minor == abs(expected_difference_minor)
            and existing.note == reason
            and existing.refund_of_event_id is None
        )
        if not same_event or expected_difference_minor == 0:
            raise IdempotencyConflictError(
                "this token already records a different adjustment"
            )
        entry = unit.journal.get_by_financial_event(existing.id)
        if (
            entry is None
            or entry.entry_type is not JournalEntryType.ADJUSTMENT
            or entry.effective_at != view.snapshot.observed_at
            or entry.financial_event_id != existing.id
            or entry.reversal_of_id is not None
            or entry.idempotency_key
            != reconciliation_adjustment_journal_key(existing.id)
        ):
            raise IdempotencyConflictError(
                "this token records an adjustment that was never posted"
            )
        expected = self._postings(unit, view=view, difference=expected_difference_minor)
        if posted_signatures(entry.postings) != draft_signatures(expected):
            raise IdempotencyConflictError(
                "this token records a different correction of this account"
            )
        return ReconciliationAdjustmentResult(
            snapshot=view.snapshot,
            event=existing,
            entry=entry,
            difference_minor=expected_difference_minor,
        )


def _require_reconcilable(unit: UnitOfWork, account: FinancialAccount) -> None:
    """Refuse an observation of an account no adjustment could ever settle.

    A card liability is not a balance you read off a bank app and reconcile
    against a wallet: it holds no wallets at all, so the `Unallocated` envelope
    an adjustment posts into does not exist. Because a snapshot is immutable by
    design (ADR-B066), accepting one here would create evidence that can never
    be adjusted, never corrected, and never deleted. The check therefore
    happens before the row is written, not when someone later tries to use it.
    """
    if not holds_wallets(account.account_type):
        raise BalanceSnapshotError(
            "only an asset account is reconciled against a bank balance"
        )
    ledger_account = unit.ledger_accounts.get_for_financial_account(account.id)
    if ledger_account is None:
        raise LedgerConfigurationError("this account has no physical ledger account")
    if ledger_account.kind is not LedgerAccountKind.ASSET:
        raise BalanceSnapshotError(
            "only an asset account is reconciled against a bank balance"
        )
    if unit.wallets.get_unallocated(account.id) is None:
        raise LedgerConfigurationError(
            "this account has no Unallocated wallet to hold a difference"
        )


def _view(unit: UnitOfWork, snapshot_id: int) -> ReconciliationView:
    """Return one observation beside the ledger balance as of that instant."""
    snapshot = unit.balance_snapshots.get(snapshot_id)
    if snapshot is None:
        raise BalanceSnapshotError("balance snapshot was not found")
    account = unit.financial_accounts.get(snapshot.financial_account_id)
    if account is None:  # pragma: no cover - snapshots always have one
        raise BalanceSnapshotError("the observed account was not found")
    return ReconciliationView(
        snapshot=snapshot,
        account=account,
        ledger_balance_minor=unit.reconciliation_ledger.physical_balance_as_of(
            financial_account_id=account.id, as_of=snapshot.observed_at
        ),
    )

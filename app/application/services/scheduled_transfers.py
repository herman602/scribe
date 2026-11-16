"""Schedule, claim, assume, cancel, and reverse local bank movements.

A schedule is a record of an instruction the user gave their bank, plus the
local policy that Scribe will assume the movement happened at the stored due
instant (ADR-B050). Nothing here observes a bank, and no message this module
produces may claim a bank confirmed anything.

Creating a schedule changes only what a wallet has reserved. The balanced
entry, the schedule's new status, and every commitment it covers all commit in
one Unit of Work, so a crash can never leave a posted movement whose schedule
denies it. Every journal invariant is still checked by `JournalService`; this
service never reaches the journal repository.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta

from app.application.services.ledger import JournalService
from app.domain.budget_enums import (
    CommitmentFulfillmentMode,
    JournalEntryType,
    LedgerAccountKind,
    ScheduledExecutionPolicy,
    ScheduledTransferPurpose,
    ScheduledTransferStatus,
)
from app.domain.budget_models import (
    Card,
    FinancialAccount,
    FundingCommitment,
    JournalEntry,
    LedgerAccount,
    Money,
    PostingDraft,
    ScheduledTransfer,
    ScheduledTransferCommitmentDraft,
    ScheduledTransferDraft,
    Wallet,
    validate_minor_units,
)
from app.domain.budget_rules import (
    ScheduleCoverage,
    ensure_clearing_coverage_exact,
    ensure_schedule_transition,
    resolve_due_at,
    retry_delay_seconds,
    schedule_wallet_debits,
    scheduled_transfer_create_key,
    scheduled_transfer_execute_key,
    scheduled_transfer_fingerprint,
    scheduled_transfer_reverse_key,
)
from app.domain.errors import (
    CardConfigurationError,
    ConcurrentUpdateError,
    CurrencyMismatchError,
    DatabaseError,
    LedgerConfigurationError,
    ScheduledTransferError,
    ScheduledTransferOverdueError,
    ScheduledTransferShapeError,
    ScheduledTransferStateError,
    ScribeError,
    WalletAssignmentError,
)
from app.domain.ports.repositories import UnitOfWork
from app.infrastructure.observability.logging import LOGGER_NAME

type UnitOfWorkFactory = Callable[[], UnitOfWork]
type Clock = Callable[[], datetime]

_LOGGER = logging.getLogger(LOGGER_NAME)

#: What a persisted failure is allowed to say. Stable, non-sensitive, and
#: chosen from the error class alone: an amount, a card, an account, or a
#: wallet name never reaches this column or the log.
_FAILURE_MESSAGES = {
    "ScheduledTransferShapeError": "排程的帳戶、錢包或涵蓋範圍已不一致。",
    "ScheduledTransferStateError": "排程目前的狀態不允許執行。",
    "CurrencyMismatchError": "排程與帳戶的幣別不一致。",
    "WalletAssignmentError": "排程指定的錢包已不可用。",
    "LedgerConfigurationError": "排程指定的帳戶設定不完整。",
    "CardConfigurationError": "這張卡的負債或結算帳戶尚未設定完成。",
    "IdempotencyConflictError": "這個排程的記帳識別碼已被其他項目使用。",
    "UnbalancedJournalError": "排程產生的分錄不平衡。",
}
_DEFAULT_FAILURE_MESSAGE = "排程無法執行。請檢查設定後重試。"

_TRANSFERABLE_MODES = frozenset(
    {
        CommitmentFulfillmentMode.ACCOUNT_TRANSFER,
        CommitmentFulfillmentMode.CROSS_BANK_TRANSFER,
    }
)


@dataclass(frozen=True)
class ScheduleCommitmentLine:
    """One commitment share the user asked this movement to settle."""

    funding_commitment_id: int
    amount_minor: int


@dataclass(frozen=True)
class ScheduleClearingTransferRequest:
    """User intent to move money into a clearing hub or another bank account."""

    source_account_id: int
    source_wallet_id: int
    destination_account_id: int
    destination_wallet_id: int
    amount_minor: int
    scheduled_local_date: date
    client_token: str
    lines: tuple[ScheduleCommitmentLine, ...] = ()
    scheduled_local_time: time | None = None
    timezone: str | None = None
    allow_overdue: bool = False


@dataclass(frozen=True)
class ScheduleCardAutopayRequest:
    """User intent to let one card be paid from its settlement account."""

    card_id: int
    source_wallet_id: int
    amount_minor: int
    scheduled_local_date: date
    client_token: str
    lines: tuple[ScheduleCommitmentLine, ...] = ()
    scheduled_local_time: time | None = None
    timezone: str | None = None
    allow_overdue: bool = False


@dataclass(frozen=True)
class ScheduledTransferAction:
    """One user action against an existing schedule."""

    scheduled_transfer_id: int
    expected_version: int


@dataclass(frozen=True)
class ScheduledTransferResult:
    """A schedule and whether this call is the one that created it."""

    schedule: ScheduledTransfer
    created: bool
    entry: JournalEntry | None = None


@dataclass(frozen=True)
class ScheduledTransferOutcome:
    """What one execution attempt did, in terms safe to log."""

    scheduled_transfer_id: int
    status: ScheduledTransferStatus
    attempt: int
    entry_id: int | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class MaintenanceRunResult:
    """What one maintenance pass claimed and what became of it."""

    claimed: int
    executed: int
    retried: int
    failed: int
    dry_run: bool = False
    outcomes: tuple[ScheduledTransferOutcome, ...] = field(default=())

    @property
    def had_failures(self) -> bool:
        """Return whether an operator should look at the clearing page."""
        return self.failed > 0


class ScheduleTransfers:
    """Create and move the local clearing scheduler's assumed movements."""

    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        journal: JournalService,
        *,
        clock: Clock = lambda: datetime.now(UTC),
        default_local_time: time = time(6, 0),
        timezone: str = "Asia/Taipei",
        lease_seconds: int = 120,
        batch_size: int = 20,
        retry_base_seconds: int = 30,
        retry_max_seconds: int = 1800,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._journal = journal
        self._clock = clock
        self._default_local_time = default_local_time
        self._timezone = timezone
        self._lease_seconds = lease_seconds
        self._batch_size = batch_size
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds

    # ---------------------------------------------------------------- create

    def schedule_clearing(
        self, request: ScheduleClearingTransferRequest
    ) -> ScheduledTransferResult:
        """Record one funding transfer into a clearing hub or another bank."""
        with self._unit_of_work_factory() as unit:
            source_account = _require_account(unit, request.source_account_id)
            destination_account = _require_account(unit, request.destination_account_id)
            if destination_account.currency != source_account.currency:
                raise CurrencyMismatchError(
                    "a scheduled transfer moves one currency between two accounts"
                )
            source_wallet = _require_wallet(
                unit, request.source_wallet_id, source_account.id
            )
            destination_wallet = _require_wallet(
                unit, request.destination_wallet_id, destination_account.id
            )
            draft = self._draft(
                purpose=ScheduledTransferPurpose.CLEARING_FUNDING,
                source_account_id=source_account.id,
                source_wallet_id=source_wallet.id,
                destination_account_id=destination_account.id,
                destination_wallet_id=destination_wallet.id,
                card_id=None,
                amount_minor=request.amount_minor,
                currency=source_account.currency,
                local_date=request.scheduled_local_date,
                local_time=request.scheduled_local_time,
                timezone=request.timezone,
                client_token=request.client_token,
                allow_overdue=request.allow_overdue,
            )
            covered = self._validated_coverage(unit, draft, request.lines)
            ensure_clearing_coverage_exact(
                amount_minor=draft.amount_minor,
                covered_minor=sum(item.amount_minor for item in covered),
            )
            return self._insert(unit, draft, request.lines)

    def schedule_card_autopay(
        self, request: ScheduleCardAutopayRequest
    ) -> ScheduledTransferResult:
        """Record one card payment out of that card's settlement account."""
        with self._unit_of_work_factory() as unit:
            card = _require_ready_card(unit, request.card_id)
            assert card.settlement_account_id is not None
            assert card.liability_account_id is not None
            source_account = _require_account(unit, card.settlement_account_id)
            liability_account = _require_account(unit, card.liability_account_id)
            if liability_account.currency != source_account.currency:
                raise CurrencyMismatchError(
                    "a card and its settlement account must share one currency"
                )
            source_wallet = _require_wallet(
                unit, request.source_wallet_id, source_account.id
            )
            draft = self._draft(
                purpose=ScheduledTransferPurpose.CARD_AUTOPAY,
                source_account_id=source_account.id,
                source_wallet_id=source_wallet.id,
                destination_account_id=liability_account.id,
                destination_wallet_id=None,
                card_id=card.id,
                amount_minor=request.amount_minor,
                currency=source_account.currency,
                local_date=request.scheduled_local_date,
                local_time=request.scheduled_local_time,
                timezone=request.timezone,
                client_token=request.client_token,
                allow_overdue=request.allow_overdue,
            )
            covered = self._validated_coverage(unit, draft, request.lines)
            if sum(item.amount_minor for item in covered) > draft.amount_minor:
                raise ScheduledTransferShapeError(
                    "a card payment cannot settle more than it pays"
                )
            return self._insert(unit, draft, request.lines)

    def _draft(
        self,
        *,
        purpose: ScheduledTransferPurpose,
        source_account_id: int,
        source_wallet_id: int,
        destination_account_id: int,
        destination_wallet_id: int | None,
        card_id: int | None,
        amount_minor: int,
        currency: str,
        local_date: date,
        local_time: time | None,
        timezone: str | None,
        client_token: str,
        allow_overdue: bool,
    ) -> ScheduledTransferDraft:
        """Freeze one instruction, refusing a due time that is not decidable."""
        zone = timezone or self._timezone
        wall_clock = local_time or self._default_local_time
        due_at = resolve_due_at(
            local_date=local_date, local_time=wall_clock, timezone=zone
        )
        if due_at <= self._clock() and not allow_overdue:
            raise ScheduledTransferOverdueError(
                "這個排程時間已經過了。儲存後會在下一次維護時立刻視為轉出。"
                "請確認後再送出。"
            )
        return ScheduledTransferDraft(
            purpose=purpose,
            source_account_id=source_account_id,
            source_wallet_id=source_wallet_id,
            destination_account_id=destination_account_id,
            destination_wallet_id=destination_wallet_id,
            card_id=card_id,
            amount_minor=amount_minor,
            currency=currency,
            scheduled_local_date=local_date,
            scheduled_local_time=wall_clock,
            timezone=zone,
            due_at_utc=due_at,
            idempotency_key=scheduled_transfer_create_key(client_token),
            execution_policy=ScheduledExecutionPolicy.ASSUME_AT_DUE_TIME,
        )

    def _insert(
        self,
        unit: UnitOfWork,
        draft: ScheduledTransferDraft,
        lines: tuple[ScheduleCommitmentLine, ...],
    ) -> ScheduledTransferResult:
        """Write one schedule and its coverage, or replay an identical one."""
        replay = self._replay(unit, draft, lines)
        if replay is not None:
            return replay
        schedule = unit.scheduled_transfers.add(draft)
        for line in lines:
            unit.scheduled_transfer_commitments.add(
                ScheduledTransferCommitmentDraft(
                    scheduled_transfer_id=schedule.id,
                    funding_commitment_id=line.funding_commitment_id,
                    amount_minor=line.amount_minor,
                )
            )
        for line in lines:
            unit.commitments.refresh_status(line.funding_commitment_id)
        unit.commit()
        return ScheduledTransferResult(schedule=schedule, created=True)

    def _replay(
        self,
        unit: UnitOfWork,
        draft: ScheduledTransferDraft,
        lines: tuple[ScheduleCommitmentLine, ...],
    ) -> ScheduledTransferResult | None:
        """Return the first outcome when the same submission arrives twice.

        A matching token proves only that something once used it, so the whole
        payload — accounts, wallets, card, money, due time, policy, and the
        sorted coverage — is compared before the replay is served.
        """
        existing = unit.scheduled_transfers.get_by_idempotency_key(
            draft.idempotency_key
        )
        if existing is None:
            return None
        recorded = unit.scheduled_transfer_commitments.list_for_schedule(existing.id)
        wanted = scheduled_transfer_fingerprint(
            purpose=draft.purpose,
            source_account_id=draft.source_account_id,
            source_wallet_id=draft.source_wallet_id,
            destination_account_id=draft.destination_account_id,
            destination_wallet_id=draft.destination_wallet_id,
            card_id=draft.card_id,
            amount_minor=draft.amount_minor,
            currency=draft.currency,
            scheduled_local_date=draft.scheduled_local_date,
            scheduled_local_time=draft.scheduled_local_time,
            timezone=draft.timezone,
            due_at_utc=draft.due_at_utc,
            execution_policy=draft.execution_policy,
            links=tuple(
                (line.funding_commitment_id, line.amount_minor) for line in lines
            ),
        )
        found = scheduled_transfer_fingerprint(
            purpose=existing.purpose,
            source_account_id=existing.source_account_id,
            source_wallet_id=existing.source_wallet_id,
            destination_account_id=existing.destination_account_id,
            destination_wallet_id=existing.destination_wallet_id,
            card_id=existing.card_id,
            amount_minor=existing.amount_minor,
            currency=existing.currency,
            scheduled_local_date=existing.scheduled_local_date,
            scheduled_local_time=existing.scheduled_local_time,
            timezone=existing.timezone,
            due_at_utc=existing.due_at_utc,
            execution_policy=existing.execution_policy,
            links=tuple(
                (item.funding_commitment_id, item.amount_minor) for item in recorded
            ),
        )
        if wanted != found:
            raise ScheduledTransferError(
                "this idempotency token was already used for a different schedule"
            )
        return ScheduledTransferResult(schedule=existing, created=False)

    # ---------------------------------------------------------- user actions

    def cancel(self, request: ScheduledTransferAction) -> ScheduledTransfer:
        """Withdraw one schedule before it is executed, freeing its coverage."""
        with self._unit_of_work_factory() as unit:
            schedule = _require_schedule(unit, request.scheduled_transfer_id)
            ensure_schedule_transition(
                schedule.status, ScheduledTransferStatus.CANCELLED
            )
            covered = unit.scheduled_transfer_commitments.covered_commitment_ids(
                schedule.id
            )
            updated = unit.scheduled_transfers.transition(
                schedule.id,
                expected_version=request.expected_version,
                expected_status=schedule.status,
                status=ScheduledTransferStatus.CANCELLED,
                cancelled_at=self._clock(),
            )
            for commitment_id in covered:
                unit.commitments.refresh_status(commitment_id)
            unit.commit()
        return updated

    def retry(self, request: ScheduledTransferAction) -> ScheduledTransfer:
        """Put one failed schedule back in the queue, without a backoff wait."""
        with self._unit_of_work_factory() as unit:
            schedule = _require_schedule(unit, request.scheduled_transfer_id)
            if schedule.status is not ScheduledTransferStatus.FAILED:
                raise ScheduledTransferStateError(
                    "only a failed scheduled transfer is retried by hand"
                )
            ensure_schedule_transition(
                schedule.status, ScheduledTransferStatus.SCHEDULED
            )
            updated = unit.scheduled_transfers.transition(
                schedule.id,
                expected_version=request.expected_version,
                expected_status=ScheduledTransferStatus.FAILED,
                status=ScheduledTransferStatus.SCHEDULED,
                clear_error=True,
            )
            unit.commit()
        return updated

    def reverse(self, request: ScheduledTransferAction) -> ScheduledTransferResult:
        """Cancel an executed movement with its exact opposite entry.

        The reversal entry, the schedule's new status, and every commitment it
        covered are one commit, so the ledger and the schedule can never
        disagree about whether the money came back (ADR-B054).
        """
        with self._unit_of_work_factory() as unit:
            schedule = _require_schedule(unit, request.scheduled_transfer_id)
            # The state machine is consulted only after the stable reverse key
            # has had its chance. A resubmitted form carries the version the
            # page was rendered with, which the first reversal has already
            # moved past, so checking the transition first would turn a
            # succeeded reversal into a failure for the one user who most
            # needs to know it worked (ADR-B054).
            replay = self._reversal_replay(unit, schedule)
            if replay is not None:
                return replay
            ensure_schedule_transition(
                schedule.status, ScheduledTransferStatus.REVERSED
            )
            if schedule.executed_entry_id is None:  # pragma: no cover - shape check
                raise ScheduledTransferStateError(
                    "an executed schedule always owns the entry it reverses"
                )
            now = self._clock()
            entry = self._journal.stage_reversal(
                unit,
                schedule.executed_entry_id,
                description="Scheduled transfer reversal",
                effective_at=now,
                idempotency_key=scheduled_transfer_reverse_key(schedule.id),
            )
            covered = unit.scheduled_transfer_commitments.covered_commitment_ids(
                schedule.id
            )
            updated = unit.scheduled_transfers.transition(
                schedule.id,
                expected_version=request.expected_version,
                expected_status=ScheduledTransferStatus.ASSUMED_EXECUTED,
                status=ScheduledTransferStatus.REVERSED,
                reversal_entry_id=entry.id,
                reversed_at=now,
            )
            for commitment_id in covered:
                unit.commitments.refresh_status(commitment_id)
            unit.commit()
        return ScheduledTransferResult(schedule=updated, created=True, entry=entry)

    @staticmethod
    def _reversal_replay(
        unit: UnitOfWork, schedule: ScheduledTransfer
    ) -> ScheduledTransferResult | None:
        """Return the first reversal when the same request arrives twice.

        Being `reversed` is not on its own proof that *this* request produced
        the reversal, so the stored entry is re-verified in full: it must be
        the entry the schedule points at, it must own this schedule's reverse
        key, and it must reverse exactly the entry the execution posted.
        Anything else is a conflict rather than a comforting replay.
        """
        if schedule.status is not ScheduledTransferStatus.REVERSED:
            return None
        key = scheduled_transfer_reverse_key(schedule.id)
        if schedule.reversal_entry_id is None:  # pragma: no cover - shape check
            raise ScheduledTransferStateError(
                "a reversed schedule always owns the entry that reversed it"
            )
        entry = unit.journal.get(schedule.reversal_entry_id)
        if entry is None:  # pragma: no cover - guarded by the foreign key
            raise ScheduledTransferError("the reversal entry was not found")
        if (
            entry.idempotency_key != key
            or entry.entry_type is not JournalEntryType.REVERSAL
            or entry.reversal_of_id != schedule.executed_entry_id
        ):
            raise ScheduledTransferError(
                "this schedule's recorded reversal does not match the reversal "
                "this request describes"
            )
        return ScheduledTransferResult(schedule=schedule, created=False, entry=entry)

    # ------------------------------------------------------------- execution

    def preview_due(self, *, limit: int | None = None) -> MaintenanceRunResult:
        """Return what a run right now would claim, writing nothing at all."""
        now = self._clock()
        with self._unit_of_work_factory() as unit:
            due = unit.scheduled_transfers.list_due(
                now, limit=limit or self._batch_size
            )
        return MaintenanceRunResult(
            claimed=len(due),
            executed=0,
            retried=0,
            failed=0,
            dry_run=True,
            outcomes=tuple(
                ScheduledTransferOutcome(
                    scheduled_transfer_id=item.id,
                    status=item.status,
                    attempt=item.attempt_count,
                )
                for item in due
            ),
        )

    def run_due(self, *, limit: int | None = None) -> MaintenanceRunResult:
        """Claim every due schedule and execute each in its own transaction.

        The claim commits on its own so no other worker can take the same rows,
        and each schedule then runs alone: one failure never stops the batch.
        """
        now = self._clock()
        owner = _lease_owner()
        lease_until = now + timedelta(seconds=self._lease_seconds)
        with self._unit_of_work_factory() as unit:
            claimed = unit.scheduled_transfers.claim_due(
                now=now,
                owner=owner,
                lease_until=lease_until,
                limit=limit or self._batch_size,
            )
            unit.commit()
        outcomes = [self._execute_claimed(item, owner) for item in claimed]
        return MaintenanceRunResult(
            claimed=len(claimed),
            executed=sum(
                outcome.status is ScheduledTransferStatus.ASSUMED_EXECUTED
                for outcome in outcomes
            ),
            retried=sum(
                outcome.status is ScheduledTransferStatus.SCHEDULED
                for outcome in outcomes
            ),
            failed=sum(
                outcome.status is ScheduledTransferStatus.FAILED for outcome in outcomes
            ),
            outcomes=tuple(outcomes),
        )

    def _execute_claimed(
        self, claimed: ScheduledTransfer, owner: str
    ) -> ScheduledTransferOutcome:
        """Execute one claimed schedule, recording why it did not work.

        Nothing here logs an amount, a name, a card, or an account: a schedule
        ID, an attempt number, a lease owner, and a stable error code are the
        whole vocabulary.
        """
        try:
            entry = self._post_claimed(claimed, owner)
        except ScribeError as error:
            return self._record_failure(claimed, owner, error)
        except Exception:  # pragma: no cover - defensive, never silences a bug
            _LOGGER.exception(
                "Scheduled transfer failed unexpectedly schedule_id=%s attempt=%s",
                claimed.id,
                claimed.attempt_count,
            )
            return self._record_failure(
                claimed, owner, ScheduledTransferError("unexpected failure")
            )
        _LOGGER.info(
            "Scheduled transfer assumed executed schedule_id=%s attempt=%s owner=%s",
            claimed.id,
            claimed.attempt_count,
            owner,
        )
        return ScheduledTransferOutcome(
            scheduled_transfer_id=claimed.id,
            status=ScheduledTransferStatus.ASSUMED_EXECUTED,
            attempt=claimed.attempt_count,
            entry_id=entry.id,
        )

    def _post_claimed(self, claimed: ScheduledTransfer, owner: str) -> JournalEntry:
        """Post one movement and complete its schedule in a single commit."""
        now = self._clock()
        with self._unit_of_work_factory() as unit:
            schedule = _require_schedule(unit, claimed.id)
            if schedule.status is not ScheduledTransferStatus.PROCESSING:
                raise ScheduledTransferStateError(
                    "this scheduled transfer is no longer being executed"
                )
            if schedule.due_at_utc > now:
                raise ScheduledTransferStateError(
                    "this scheduled transfer is not due yet"
                )
            ensure_schedule_transition(
                schedule.status, ScheduledTransferStatus.ASSUMED_EXECUTED
            )
            entry = self._journal.stage_balanced(
                unit,
                entry_type=(
                    JournalEntryType.CARD_PAYMENT
                    if schedule.purpose is ScheduledTransferPurpose.CARD_AUTOPAY
                    else JournalEntryType.FUNDING_TRANSFER
                ),
                # The accounting date is the due instant, never the moment a
                # sleeping Mac woke up and noticed (ADR-B050).
                effective_at=schedule.due_at_utc,
                idempotency_key=scheduled_transfer_execute_key(schedule.id),
                description=_description(schedule),
                postings=self._postings(unit, schedule),
            )
            unit.scheduled_transfers.finish(
                schedule.id,
                expected_version=claimed.version,
                lease_owner=owner,
                status=ScheduledTransferStatus.ASSUMED_EXECUTED,
                executed_entry_id=entry.id,
                processed_at=now,
            )
            for (
                commitment_id
            ) in unit.scheduled_transfer_commitments.covered_commitment_ids(
                schedule.id
            ):
                unit.commitments.refresh_status(commitment_id)
            unit.commit()
        return entry

    def _record_failure(
        self, claimed: ScheduledTransfer, owner: str, error: ScribeError
    ) -> ScheduledTransferOutcome:
        """Send a claimed schedule back to the queue, or stop retrying it.

        A failed schedule keeps its reservation: the instruction the bank was
        given has not been withdrawn, so the money is still leaving unless the
        user cancels the schedule (ADR-B059).
        """
        code = type(error).__name__
        retryable = isinstance(error, DatabaseError | ConcurrentUpdateError) or getattr(
            error, "retryable", False
        )
        now = self._clock()
        status = (
            ScheduledTransferStatus.SCHEDULED
            if retryable
            else ScheduledTransferStatus.FAILED
        )
        next_attempt = (
            now
            + timedelta(
                seconds=retry_delay_seconds(
                    attempt=max(1, claimed.attempt_count),
                    base_seconds=self._retry_base_seconds,
                    max_seconds=self._retry_max_seconds,
                )
            )
            if retryable
            else None
        )
        _LOGGER.warning(
            "Scheduled transfer attempt failed schedule_id=%s attempt=%s "
            "owner=%s error_code=%s retryable=%s",
            claimed.id,
            claimed.attempt_count,
            owner,
            code,
            retryable,
        )
        try:
            with self._unit_of_work_factory() as unit:
                unit.scheduled_transfers.finish(
                    claimed.id,
                    expected_version=claimed.version,
                    lease_owner=owner,
                    status=status,
                    next_attempt_at=next_attempt,
                    last_error_code=code,
                    last_error_message=_FAILURE_MESSAGES.get(
                        code, _DEFAULT_FAILURE_MESSAGE
                    ),
                )
                unit.commit()
        except ScribeError:
            # Another worker took the lease over and already recorded an
            # outcome. Overwriting it is exactly what the lease check exists to
            # prevent, so this attempt simply ends.
            _LOGGER.warning(
                "Scheduled transfer outcome was not recorded schedule_id=%s owner=%s",
                claimed.id,
                owner,
            )
        return ScheduledTransferOutcome(
            scheduled_transfer_id=claimed.id,
            status=status,
            attempt=claimed.attempt_count,
            error_code=code,
        )

    # -------------------------------------------------------------- postings

    def _postings(
        self, unit: UnitOfWork, schedule: ScheduledTransfer
    ) -> tuple[PostingDraft, ...]:
        """Build the balanced entry one schedule's purpose calls for."""
        coverage = self._validated_coverage_for(unit, schedule)
        source_ledger = _require_ledger_account(unit, schedule.source_account_id)
        if source_ledger.kind is not LedgerAccountKind.ASSET:
            raise ScheduledTransferShapeError(
                "a scheduled transfer leaves an asset account"
            )
        debits = schedule_wallet_debits(
            amount_minor=schedule.amount_minor,
            source_wallet_id=schedule.source_wallet_id,
            coverage=coverage,
        )
        postings = [
            PostingDraft(
                ledger_account_id=source_ledger.id,
                amount=Money(-amount, schedule.currency),
                wallet_id=wallet_id,
            )
            for wallet_id, amount in debits
        ]
        destination = _require_ledger_account(unit, schedule.destination_account_id)
        if schedule.purpose is ScheduledTransferPurpose.CARD_AUTOPAY:
            if destination.kind is not LedgerAccountKind.LIABILITY:
                raise ScheduledTransferShapeError(
                    "a card payment credits a card liability account"
                )
            postings.append(
                PostingDraft(
                    ledger_account_id=destination.id,
                    amount=Money(schedule.amount_minor, schedule.currency),
                )
            )
        else:
            if destination.kind is not LedgerAccountKind.ASSET:
                raise ScheduledTransferShapeError(
                    "a clearing transfer credits an asset account"
                )
            postings.append(
                PostingDraft(
                    ledger_account_id=destination.id,
                    amount=Money(schedule.amount_minor, schedule.currency),
                    wallet_id=schedule.destination_wallet_id,
                )
            )
        return tuple(postings)

    def _validated_coverage_for(
        self, unit: UnitOfWork, schedule: ScheduledTransfer
    ) -> tuple[ScheduleCoverage, ...]:
        """Re-check at execution what creation already checked.

        A card, a wallet, or an account can be reconfigured between scheduling
        and 06:00, and posting the movement anyway would settle the wrong debt.
        A shrunken *requirement* is deliberately not re-checked: a refund never
        retracts an instruction the bank already has (ADR-B060).
        """
        links = unit.scheduled_transfer_commitments.list_for_schedule(schedule.id)
        lines = tuple(
            ScheduleCommitmentLine(
                funding_commitment_id=link.funding_commitment_id,
                amount_minor=link.amount_minor,
            )
            for link in links
        )
        coverage = _coverage(unit, schedule, lines)
        if schedule.purpose is ScheduledTransferPurpose.CLEARING_FUNDING:
            ensure_clearing_coverage_exact(
                amount_minor=schedule.amount_minor,
                covered_minor=sum(item.amount_minor for item in coverage),
            )
        return coverage

    def _validated_coverage(
        self,
        unit: UnitOfWork,
        draft: ScheduledTransferDraft,
        lines: tuple[ScheduleCommitmentLine, ...],
    ) -> tuple[ScheduleCoverage, ...]:
        """Check a request's coverage before any row exists."""
        return _coverage(unit, draft, lines)


def _coverage(
    unit: UnitOfWork,
    schedule: ScheduledTransfer | ScheduledTransferDraft,
    lines: tuple[ScheduleCommitmentLine, ...],
) -> tuple[ScheduleCoverage, ...]:
    """Return the validated commitment shares one movement settles."""
    seen: set[int] = set()
    coverage: list[ScheduleCoverage] = []
    for line in lines:
        amount = validate_minor_units(
            line.amount_minor, label="covered commitment amount", minimum=1
        )
        if line.funding_commitment_id in seen:
            raise ScheduledTransferShapeError(
                "one schedule covers each commitment at most once"
            )
        seen.add(line.funding_commitment_id)
        commitment = unit.commitments.get(line.funding_commitment_id)
        if commitment is None:
            raise ScheduledTransferShapeError("funding commitment was not found")
        if commitment.currency != schedule.currency:
            raise CurrencyMismatchError(
                "a scheduled transfer settles commitments in its own currency"
            )
        wallet = unit.wallets.get(commitment.source_wallet_id)
        if wallet is None or not wallet.is_active:
            raise WalletAssignmentError("the commitment's source wallet is unusable")
        if wallet.financial_account_id != schedule.source_account_id:
            raise ScheduledTransferShapeError(
                "a scheduled transfer only settles commitments reserved in the "
                "account it leaves"
            )
        if schedule.purpose is ScheduledTransferPurpose.CARD_AUTOPAY:
            _ensure_autopay_commitment(unit, schedule, commitment)
        else:
            _ensure_clearing_commitment(schedule, commitment)
        coverage.append(
            ScheduleCoverage(
                funding_commitment_id=commitment.id,
                source_wallet_id=wallet.id,
                amount_minor=amount,
            )
        )
    return tuple(coverage)


def _ensure_clearing_commitment(
    schedule: ScheduledTransfer | ScheduledTransferDraft,
    commitment: FundingCommitment,
) -> None:
    """A clearing transfer only funds what really has to move banks."""
    if commitment.settlement_account_id != schedule.destination_account_id:
        raise ScheduledTransferShapeError(
            "a clearing transfer must arrive in the commitment's settlement account"
        )
    if commitment.fulfillment_mode not in _TRANSFERABLE_MODES:
        raise ScheduledTransferShapeError(
            "money reserved inside its own settlement account needs no transfer"
        )


def _ensure_autopay_commitment(
    unit: UnitOfWork,
    schedule: ScheduledTransfer | ScheduledTransferDraft,
    commitment: FundingCommitment,
) -> None:
    """An autopay only settles purchases made on the card it is paying."""
    if commitment.settlement_account_id != schedule.source_account_id:
        raise ScheduledTransferShapeError(
            "a card payment settles commitments reserved in its settlement account"
        )
    if commitment.fulfillment_mode is not CommitmentFulfillmentMode.RESERVE_ONLY:
        raise ScheduledTransferShapeError(
            "money reserved in another bank has to be cleared before it pays a card"
        )
    event = unit.financial_events.get(commitment.financial_event_id)
    if event is None:  # pragma: no cover - guarded by the foreign key
        raise ScheduledTransferShapeError("the reserved event was not found")
    if event.card_id != schedule.card_id:
        raise ScheduledTransferShapeError(
            "a card payment only settles purchases made on that card"
        )


def _description(schedule: ScheduledTransfer) -> str:
    """Return non-sensitive display text for the posted entry."""
    if schedule.purpose is ScheduledTransferPurpose.CARD_AUTOPAY:
        return "Card autopay (assumed at due time)"
    return "Clearing funding transfer (assumed at due time)"


def _lease_owner() -> str:
    """Return a token unique to one claim, bounded to the stored column."""
    return f"maintenance:{uuid.uuid4().hex}"[:64]


def _require_schedule(
    unit: UnitOfWork, scheduled_transfer_id: int
) -> ScheduledTransfer:
    schedule = unit.scheduled_transfers.get(scheduled_transfer_id)
    if schedule is None:
        raise ScheduledTransferError("scheduled transfer was not found")
    return schedule


def _require_account(unit: UnitOfWork, account_id: int) -> FinancialAccount:
    account = unit.financial_accounts.get(account_id)
    if account is None or not account.is_active:
        raise LedgerConfigurationError("financial account was not found")
    return account


def _require_wallet(unit: UnitOfWork, wallet_id: int, account_id: int) -> Wallet:
    wallet = unit.wallets.get(wallet_id)
    if wallet is None or not wallet.is_active:
        raise WalletAssignmentError("wallet was not found")
    if wallet.financial_account_id != account_id:
        raise WalletAssignmentError("wallet belongs to a different financial account")
    return wallet


def _require_ledger_account(unit: UnitOfWork, account_id: int) -> LedgerAccount:
    ledger_account = unit.ledger_accounts.get_for_financial_account(account_id)
    if ledger_account is None:
        raise LedgerConfigurationError(
            "this financial account has no ledger account yet"
        )
    return ledger_account


def _require_ready_card(unit: UnitOfWork, card_id: int) -> Card:
    card = unit.cards.get(card_id)
    if card is None:
        raise CardConfigurationError("card was not found")
    if not card.is_ready:
        raise CardConfigurationError(
            "configure the card's liability and settlement accounts first"
        )
    return card

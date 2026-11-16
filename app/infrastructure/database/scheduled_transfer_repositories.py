"""SQLAlchemy repositories for assumed bank movements and their coverage.

Every state change here is one conditional statement whose ``rowcount`` is
checked. A read followed by an unconditional write would let a worker whose
lease expired overwrite the result of the worker that took it over, and for
money that is not a stale display but a duplicated or lost movement.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.domain.budget_enums import (
    CommitmentStatus,
    ScheduledExecutionPolicy,
    ScheduledTransferPurpose,
    ScheduledTransferStatus,
)
from app.domain.budget_models import (
    ScheduledTransfer,
    ScheduledTransferCommitment,
    ScheduledTransferCommitmentDraft,
    ScheduledTransferDraft,
)
from app.domain.budget_rules import (
    ACTIVE_SCHEDULE_STATUSES,
    EXECUTED_SCHEDULE_STATUSES,
    derive_commitment_status,
)
from app.domain.errors import (
    ConcurrentUpdateError,
    DatabaseError,
    ScheduledTransferCoverageError,
    ScheduledTransferError,
    ScheduledTransferStateError,
    ScribeError,
)
from app.infrastructure.database.budget_models import (
    FundingCommitmentModel,
    ScheduledTransferCommitmentModel,
    ScheduledTransferModel,
)

_FROZEN_INSTRUCTION = "instruction cannot be changed"
_IMMUTABLE_LINK = "commitment link is immutable"
_OVER_COVERAGE = "cover more than this commitment still requires"

#: Statuses whose links still occupy a commitment's coverage capacity, as SQL
#: literals. Cancelled and reversed schedules deliberately do not.
ACTIVE_STATUS_VALUES = tuple(sorted(item.value for item in ACTIVE_SCHEDULE_STATUSES))
EXECUTED_STATUS_VALUES = tuple(
    sorted(item.value for item in EXECUTED_SCHEDULE_STATUSES)
)
LIVE_STATUS_VALUES = tuple(sorted(ACTIVE_STATUS_VALUES + EXECUTED_STATUS_VALUES))


def _translated(error: SQLAlchemyError) -> ScribeError:
    """Turn a database defence into the stable error the service also raises."""
    message = str(error)
    if _FROZEN_INSTRUCTION in message:
        return ScheduledTransferError(
            "a scheduled transfer's instruction is frozen; cancel it and "
            "schedule a replacement instead"
        )
    if _IMMUTABLE_LINK in message:
        return ScheduledTransferError(
            "a scheduled transfer's commitment coverage cannot be rewritten"
        )
    if _OVER_COVERAGE in message:
        return ScheduledTransferCoverageError(
            "this schedule would cover more than the commitment still requires"
        )
    if "scheduled_transfers.idempotency_key" in message:
        return ScheduledTransferError(
            "this idempotency token already belongs to another schedule"
        )
    if "scheduled_transfers.executed_entry_id" in message:
        return ScheduledTransferError(
            "this journal entry already belongs to another schedule"
        )
    if "scheduled_transfers.reversal_entry_id" in message:
        return ScheduledTransferError(
            "this reversal already belongs to another schedule"
        )
    if "scheduled_transfer_commitments" in message:
        return ScheduledTransferError("this schedule already covers that commitment")
    return DatabaseError("database write failed")


def scheduled_transfer_record(model: ScheduledTransferModel) -> ScheduledTransfer:
    """Translate one persisted instruction into its domain record."""
    return ScheduledTransfer(
        id=model.id,
        purpose=ScheduledTransferPurpose(model.purpose),
        source_account_id=model.source_account_id,
        source_wallet_id=model.source_wallet_id,
        destination_account_id=model.destination_account_id,
        destination_wallet_id=model.destination_wallet_id,
        card_id=model.card_id,
        amount_minor=model.amount_minor,
        currency=model.currency,
        scheduled_local_date=model.scheduled_local_date,
        scheduled_local_time=model.scheduled_local_time,
        timezone=model.timezone,
        due_at_utc=model.due_at_utc,
        execution_policy=ScheduledExecutionPolicy(model.execution_policy),
        status=ScheduledTransferStatus(model.status),
        idempotency_key=model.idempotency_key,
        executed_entry_id=model.executed_entry_id,
        reversal_entry_id=model.reversal_entry_id,
        lease_owner=model.lease_owner,
        lease_expires_at=model.lease_expires_at,
        attempt_count=model.attempt_count,
        last_attempt_at=model.last_attempt_at,
        next_attempt_at=model.next_attempt_at,
        processed_at=model.processed_at,
        cancelled_at=model.cancelled_at,
        reversed_at=model.reversed_at,
        last_error_code=model.last_error_code,
        last_error_message=model.last_error_message,
        version=model.version,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def scheduled_transfer_commitment_record(
    model: ScheduledTransferCommitmentModel,
) -> ScheduledTransferCommitment:
    """Translate one persisted coverage link into its domain record."""
    return ScheduledTransferCommitment(
        scheduled_transfer_id=model.scheduled_transfer_id,
        funding_commitment_id=model.funding_commitment_id,
        amount_minor=model.amount_minor,
        created_at=model.created_at,
    )


class SqlAlchemyScheduledTransferRepository:
    """SQLAlchemy repository for one local clearing schedule at a time."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, draft: ScheduledTransferDraft) -> ScheduledTransfer:
        """Record one bank instruction; its economic columns never move again."""
        model = ScheduledTransferModel(
            purpose=draft.purpose.value,
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
            execution_policy=draft.execution_policy.value,
            status=ScheduledTransferStatus.SCHEDULED.value,
            idempotency_key=draft.idempotency_key,
            attempt_count=0,
            version=1,
        )
        self._session.add(model)
        try:
            self._session.flush()
            self._session.refresh(model)
        except SQLAlchemyError as error:
            raise _translated(error) from error
        return scheduled_transfer_record(model)

    def get(self, scheduled_transfer_id: int) -> ScheduledTransfer | None:
        """Return one schedule by internal ID."""
        model = self._session.get(ScheduledTransferModel, scheduled_transfer_id)
        return None if model is None else scheduled_transfer_record(model)

    def get_by_idempotency_key(self, key: str) -> ScheduledTransfer | None:
        """Return the schedule one client token already created, if any."""
        model = self._session.scalar(
            select(ScheduledTransferModel).where(
                ScheduledTransferModel.idempotency_key == key
            )
        )
        return None if model is None else scheduled_transfer_record(model)

    def list_by_status(
        self,
        statuses: frozenset[ScheduledTransferStatus],
        *,
        limit: int,
        offset: int = 0,
    ) -> tuple[ScheduledTransfer, ...]:
        """Return a bounded page of schedules in due order, newest last."""
        models = self._session.scalars(
            select(ScheduledTransferModel)
            .where(
                ScheduledTransferModel.status.in_(
                    sorted(item.value for item in statuses)
                )
            )
            .order_by(
                ScheduledTransferModel.due_at_utc.desc(),
                ScheduledTransferModel.id.desc(),
            )
            .limit(limit)
            .offset(offset)
        ).all()
        return tuple(scheduled_transfer_record(model) for model in models)

    def count_by_status(self, statuses: frozenset[ScheduledTransferStatus]) -> int:
        """Return how many schedules a bounded page was taken from."""
        total = self._session.execute(
            select(func.count())
            .select_from(ScheduledTransferModel)
            .where(
                ScheduledTransferModel.status.in_(
                    sorted(item.value for item in statuses)
                )
            )
        ).scalar_one()
        return int(total)

    def count_due(self, now: datetime) -> int:
        """Return how many schedules a run right now would claim."""
        total = self._session.execute(
            select(func.count())
            .select_from(ScheduledTransferModel)
            .where(claimable_condition(now))
        ).scalar_one()
        return int(total)

    def list_due(self, now: datetime, *, limit: int) -> tuple[ScheduledTransfer, ...]:
        """Return what a run right now would claim, without claiming it."""
        models = self._session.scalars(
            select(ScheduledTransferModel)
            .where(claimable_condition(now))
            .order_by(ScheduledTransferModel.due_at_utc, ScheduledTransferModel.id)
            .limit(limit)
        ).all()
        return tuple(scheduled_transfer_record(model) for model in models)

    def claim_due(
        self,
        *,
        now: datetime,
        owner: str,
        lease_until: datetime,
        limit: int,
    ) -> tuple[ScheduledTransfer, ...]:
        """Take exclusive ownership of every due schedule, in one statement.

        ``owner`` must be unique to this call: the claimed rows are read back
        by owner and lease expiry, so reusing a token would also return rows a
        previous batch is still executing. Two coordinators racing here cannot
        both win, because the second one's ``WHERE`` no longer matches the rows
        the first already moved out of ``scheduled``.
        """
        selectable = (
            select(ScheduledTransferModel.id)
            .where(claimable_condition(now))
            .order_by(ScheduledTransferModel.due_at_utc, ScheduledTransferModel.id)
            .limit(limit)
        )
        try:
            result = cast(
                CursorResult[Any],
                self._session.execute(
                    update(ScheduledTransferModel)
                    .where(ScheduledTransferModel.id.in_(selectable))
                    .values(
                        status=ScheduledTransferStatus.PROCESSING.value,
                        lease_owner=owner,
                        lease_expires_at=lease_until,
                        attempt_count=ScheduledTransferModel.attempt_count + 1,
                        last_attempt_at=now,
                        next_attempt_at=None,
                        version=ScheduledTransferModel.version + 1,
                    )
                    .execution_options(synchronize_session=False),
                ),
            )
        except SQLAlchemyError as error:
            raise _translated(error) from error
        if result.rowcount == 0:
            return ()
        models = self._session.scalars(
            select(ScheduledTransferModel)
            .where(
                ScheduledTransferModel.lease_owner == owner,
                ScheduledTransferModel.lease_expires_at == lease_until,
                ScheduledTransferModel.status
                == ScheduledTransferStatus.PROCESSING.value,
            )
            .order_by(ScheduledTransferModel.due_at_utc, ScheduledTransferModel.id)
        ).all()
        return tuple(scheduled_transfer_record(model) for model in models)

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
    ) -> ScheduledTransfer:
        """Complete one claimed schedule, only if this worker still owns it.

        The lease owner *and* the version are both re-checked, so a worker
        whose lease expired and was taken over can never write its own outcome
        over the outcome of the worker that took it.
        """
        return self._conditional_update(
            scheduled_transfer_id,
            extra_where=(
                ScheduledTransferModel.version == expected_version,
                ScheduledTransferModel.status
                == ScheduledTransferStatus.PROCESSING.value,
                ScheduledTransferModel.lease_owner == lease_owner,
            ),
            values={
                "status": status.value,
                "executed_entry_id": executed_entry_id,
                "processed_at": processed_at,
                "next_attempt_at": next_attempt_at,
                "last_error_code": last_error_code,
                "last_error_message": last_error_message,
                "lease_owner": None,
                "lease_expires_at": None,
                "version": ScheduledTransferModel.version + 1,
            },
            stale_message="this schedule is no longer owned by this worker",
        )

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
    ) -> ScheduledTransfer:
        """Move one schedule forward from exactly the state it was read in."""
        values: dict[str, Any] = {
            "status": status.value,
            "version": ScheduledTransferModel.version + 1,
        }
        if cancelled_at is not None:
            values["cancelled_at"] = cancelled_at
        if reversal_entry_id is not None:
            values["reversal_entry_id"] = reversal_entry_id
        if reversed_at is not None:
            values["reversed_at"] = reversed_at
        if clear_error:
            values["last_error_code"] = None
            values["last_error_message"] = None
            values["next_attempt_at"] = None
        return self._conditional_update(
            scheduled_transfer_id,
            extra_where=(
                ScheduledTransferModel.version == expected_version,
                ScheduledTransferModel.status == expected_status.value,
            ),
            values=values,
            stale_message="this schedule changed after the page was loaded",
        )

    def _conditional_update(
        self,
        scheduled_transfer_id: int,
        *,
        extra_where: tuple[Any, ...],
        values: dict[str, Any],
        stale_message: str,
    ) -> ScheduledTransfer:
        try:
            result = cast(
                CursorResult[Any],
                self._session.execute(
                    update(ScheduledTransferModel)
                    .where(
                        ScheduledTransferModel.id == scheduled_transfer_id,
                        *extra_where,
                    )
                    .values(**values)
                ),
            )
        except SQLAlchemyError as error:
            raise _translated(error) from error
        if result.rowcount != 1:
            model = self._session.get(ScheduledTransferModel, scheduled_transfer_id)
            if model is None:
                raise ScheduledTransferError("scheduled transfer was not found")
            raise ConcurrentUpdateError(stale_message)
        model = self._session.get(ScheduledTransferModel, scheduled_transfer_id)
        if model is None:  # pragma: no cover - just updated
            raise ScheduledTransferError("scheduled transfer disappeared")
        self._session.refresh(model)
        return scheduled_transfer_record(model)


def claimable_condition(now: datetime) -> Any:
    """Return the condition a schedule must meet to be executed right now.

    Either it is due and its backoff has elapsed, or a previous worker claimed
    it and let its lease expire — a Mac that slept mid-execution, for example.
    """
    return or_(
        and_(
            ScheduledTransferModel.status == ScheduledTransferStatus.SCHEDULED.value,
            ScheduledTransferModel.due_at_utc <= now,
            or_(
                ScheduledTransferModel.next_attempt_at.is_(None),
                ScheduledTransferModel.next_attempt_at <= now,
            ),
        ),
        and_(
            ScheduledTransferModel.status == ScheduledTransferStatus.PROCESSING.value,
            ScheduledTransferModel.lease_expires_at.is_not(None),
            ScheduledTransferModel.lease_expires_at <= now,
        ),
    )


class SqlAlchemyScheduledTransferCommitmentRepository:
    """SQLAlchemy repository for the immutable coverage of one schedule."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(
        self, draft: ScheduledTransferCommitmentDraft
    ) -> ScheduledTransferCommitment:
        """Link one commitment share to one schedule, within its requirement.

        The database checks the requirement too: the service and the insert are
        two statements, so a second window could otherwise slip a second
        schedule past a check the first had already passed.
        """
        model = ScheduledTransferCommitmentModel(
            scheduled_transfer_id=draft.scheduled_transfer_id,
            funding_commitment_id=draft.funding_commitment_id,
            amount_minor=draft.amount_minor,
        )
        self._session.add(model)
        try:
            self._session.flush()
            self._session.refresh(model)
        except SQLAlchemyError as error:
            raise _translated(error) from error
        return scheduled_transfer_commitment_record(model)

    def list_for_schedule(
        self, scheduled_transfer_id: int
    ) -> tuple[ScheduledTransferCommitment, ...]:
        """Return everything one schedule says it covers."""
        models = self._session.scalars(
            select(ScheduledTransferCommitmentModel)
            .where(
                ScheduledTransferCommitmentModel.scheduled_transfer_id
                == scheduled_transfer_id
            )
            .order_by(ScheduledTransferCommitmentModel.funding_commitment_id)
        ).all()
        return tuple(scheduled_transfer_commitment_record(model) for model in models)

    def list_for_commitment(
        self, funding_commitment_id: int
    ) -> tuple[ScheduledTransferCommitment, ...]:
        """Return every schedule share pointed at one commitment."""
        models = self._session.scalars(
            select(ScheduledTransferCommitmentModel)
            .where(
                ScheduledTransferCommitmentModel.funding_commitment_id
                == funding_commitment_id
            )
            .order_by(ScheduledTransferCommitmentModel.scheduled_transfer_id)
        ).all()
        return tuple(scheduled_transfer_commitment_record(model) for model in models)

    def covered_commitment_ids(self, scheduled_transfer_id: int) -> tuple[int, ...]:
        """Return which commitments one schedule's status change affects."""
        rows = self._session.scalars(
            select(ScheduledTransferCommitmentModel.funding_commitment_id)
            .where(
                ScheduledTransferCommitmentModel.scheduled_transfer_id
                == scheduled_transfer_id
            )
            .order_by(ScheduledTransferCommitmentModel.funding_commitment_id)
        ).all()
        return tuple(int(row) for row in rows)

    def coverage(self, funding_commitment_id: int) -> tuple[int, int]:
        """Return one commitment's active and already executed coverage."""
        return (
            self._coverage_for(funding_commitment_id, ACTIVE_STATUS_VALUES),
            self._coverage_for(funding_commitment_id, EXECUTED_STATUS_VALUES),
        )

    def _coverage_for(
        self, funding_commitment_id: int, statuses: tuple[str, ...]
    ) -> int:
        total = self._session.execute(
            select(
                func.coalesce(
                    func.sum(ScheduledTransferCommitmentModel.amount_minor), 0
                )
            )
            .join(
                ScheduledTransferModel,
                ScheduledTransferModel.id
                == ScheduledTransferCommitmentModel.scheduled_transfer_id,
            )
            .where(
                ScheduledTransferCommitmentModel.funding_commitment_id
                == funding_commitment_id,
                ScheduledTransferModel.status.in_(statuses),
            )
        ).scalar_one()
        return int(total)


def refresh_commitment_status(session: Session, commitment_id: int) -> CommitmentStatus:
    """Recompute one commitment's status from what actually covers it.

    Every path that changes coverage — scheduling, cancelling, executing,
    reversing, and refunding — comes through here, so no two services can leave
    the same row describing itself differently (ADR-B059).
    """
    model = session.get(FundingCommitmentModel, commitment_id)
    if model is None:
        raise ScheduledTransferError("funding commitment was not found")
    coverage = SqlAlchemyScheduledTransferCommitmentRepository(session)
    active_minor, executed_minor = coverage.coverage(commitment_id)
    status = derive_commitment_status(
        required_minor=model.amount_minor - model.released_minor,
        active_minor=active_minor,
        executed_minor=executed_minor,
    )
    if model.status != status.value:
        session.execute(
            update(FundingCommitmentModel)
            .where(FundingCommitmentModel.id == commitment_id)
            .values(status=status.value)
        )
        session.refresh(model)
    return status


def ensure_claimable_now(schedule: ScheduledTransfer, now: datetime) -> None:
    """Refuse to execute a schedule before the instant it is due.

    The claim query already filters on this, but a direct service call would
    otherwise post a movement the bank has not been asked to make yet.
    """
    if schedule.due_at_utc > now:
        raise ScheduledTransferStateError("this scheduled transfer is not due yet")

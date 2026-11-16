"""SQLAlchemy repositories for the subscription requirement register."""

from __future__ import annotations

from datetime import date
from typing import Any, cast

from sqlalchemy import or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.domain.budget_enums import SubscriptionCadence
from app.domain.budget_models import (
    Subscription,
    SubscriptionDraft,
    SubscriptionPricePeriod,
    SubscriptionPricePeriodDraft,
    SubscriptionPricePeriodUpdate,
    SubscriptionUpdate,
    normalize_currency,
)
from app.domain.errors import (
    ConcurrentUpdateError,
    DatabaseError,
    ScribeError,
    SubscriptionError,
    SubscriptionOverlapError,
)
from app.infrastructure.database.budget_models import (
    SubscriptionModel,
    SubscriptionPricePeriodModel,
)

_PRICE_OVERLAP_MESSAGE = "subscription price periods must not overlap"
_INTERVAL_OVERLAP_MESSAGE = "subscription intervals must not overlap"
_DUPLICATE_NAME_CONSTRAINT = "subscriptions.name"


def _translated(error: SQLAlchemyError) -> ScribeError:
    """Turn a database defence into the same stable error the service raises.

    The service refuses both overlaps and a second running interval of one
    name inside the transaction that writes the row, but each check and its
    write are two statements: a concurrent writer can still lose the race at
    the trigger or the unique index. Translating here keeps that outcome a
    recognizable application error instead of a leaked SQLAlchemy traceback.
    """
    message = str(error)
    for trigger_message in (_PRICE_OVERLAP_MESSAGE, _INTERVAL_OVERLAP_MESSAGE):
        if trigger_message in message:
            return SubscriptionOverlapError(trigger_message)
    if _DUPLICATE_NAME_CONSTRAINT in message:
        return SubscriptionError("an active subscription already uses this name")
    return DatabaseError("database write failed")


def _flush_and_refresh(session: Session, model: Any) -> None:
    """Write one row now, so a constraint or trigger reports here."""
    try:
        session.flush()
        session.refresh(model)
    except SQLAlchemyError as error:
        raise _translated(error) from error


def subscription_record(model: SubscriptionModel) -> Subscription:
    """Translate one persisted subscription into its domain record."""
    return Subscription(
        id=model.id,
        name=model.name,
        budget_category_id=model.budget_category_id,
        funding_wallet_id=model.funding_wallet_id,
        currency=model.currency,
        billing_cadence=SubscriptionCadence(model.billing_cadence),
        cadence_months=model.cadence_months,
        billing_month=model.billing_month,
        billing_day=model.billing_day,
        active_from=model.active_from,
        active_to=model.active_to,
        is_active=model.is_active,
        version=model.version,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def price_period_record(
    model: SubscriptionPricePeriodModel,
) -> SubscriptionPricePeriod:
    """Translate one persisted price period into its domain record."""
    return SubscriptionPricePeriod(
        id=model.id,
        subscription_id=model.subscription_id,
        amount_minor=model.amount_minor,
        effective_from=model.effective_from,
        effective_to=model.effective_to,
        version=model.version,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class SqlAlchemySubscriptionRepository:
    """SQLAlchemy repository for the canonical subscription register."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, draft: SubscriptionDraft) -> Subscription:
        """Register one subscription; the draft has already validated itself."""
        model = SubscriptionModel(
            name=draft.name,
            budget_category_id=draft.budget_category_id,
            funding_wallet_id=draft.funding_wallet_id,
            currency=draft.currency,
            billing_cadence=draft.billing_cadence.value,
            cadence_months=draft.cadence_months,
            billing_month=draft.billing_month,
            billing_day=draft.billing_day,
            active_from=draft.active_from,
            active_to=draft.active_to,
            is_active=True,
        )
        self._session.add(model)
        _flush_and_refresh(self._session, model)
        return subscription_record(model)

    def get(self, subscription_id: int) -> Subscription | None:
        """Return one subscription by internal ID."""
        model = self._session.get(SubscriptionModel, subscription_id)
        return None if model is None else subscription_record(model)

    def list_by_name(self, name: str) -> tuple[Subscription, ...]:
        """Return every interval one service has held, active or not.

        The name identifies the service, so this is what an overlap check has
        to compare against: a stopped interval whose end month is still in the
        future would otherwise be invisible to it.
        """
        models = self._session.scalars(
            select(SubscriptionModel)
            .where(SubscriptionModel.name == name.strip())
            .order_by(SubscriptionModel.active_from, SubscriptionModel.id)
        ).all()
        return tuple(subscription_record(model) for model in models)

    def list_all(self) -> tuple[Subscription, ...]:
        """Return every subscription, deactivated ones included."""
        models = self._session.scalars(
            select(SubscriptionModel).order_by(
                SubscriptionModel.currency,
                SubscriptionModel.name,
                SubscriptionModel.id,
            )
        ).all()
        return tuple(subscription_record(model) for model in models)

    def list_by_currency(self, currency: str) -> tuple[Subscription, ...]:
        """Return every subscription billed in exactly one currency."""
        models = self._session.scalars(
            select(SubscriptionModel)
            .where(SubscriptionModel.currency == normalize_currency(currency))
            .order_by(SubscriptionModel.name, SubscriptionModel.id)
        ).all()
        return tuple(subscription_record(model) for model in models)

    def list_currencies(self) -> tuple[str, ...]:
        """Return the distinct currencies the register holds, never summed."""
        rows = self._session.scalars(
            select(SubscriptionModel.currency)
            .distinct()
            .order_by(SubscriptionModel.currency)
        ).all()
        return tuple(rows)

    def save(self, change: SubscriptionUpdate) -> Subscription:
        """Apply one optimistic correction or deactivation.

        The version check is part of the statement, so an interleaved writer
        cannot slip between a read and a write. A constraint the statement
        still loses a race against is translated, never leaked.
        """
        try:
            result = cast(
                CursorResult[Any],
                self._session.execute(
                    update(SubscriptionModel)
                    .where(
                        SubscriptionModel.id == change.subscription_id,
                        SubscriptionModel.version == change.expected_version,
                    )
                    .values(
                        name=change.name,
                        budget_category_id=change.budget_category_id,
                        funding_wallet_id=change.funding_wallet_id,
                        currency=change.currency,
                        billing_cadence=change.billing_cadence.value,
                        cadence_months=change.cadence_months,
                        billing_month=change.billing_month,
                        billing_day=change.billing_day,
                        active_from=change.active_from,
                        active_to=change.active_to,
                        is_active=change.is_active,
                        version=SubscriptionModel.version + 1,
                    )
                ),
            )
        except SQLAlchemyError as error:
            raise _translated(error) from error
        if result.rowcount != 1:
            if self._session.get(SubscriptionModel, change.subscription_id) is None:
                raise SubscriptionError("subscription was not found")
            raise ConcurrentUpdateError(
                "subscription changed after this page was loaded"
            )
        model = self._session.get(SubscriptionModel, change.subscription_id)
        if model is None:  # pragma: no cover - just updated
            raise SubscriptionError("subscription disappeared")
        self._session.refresh(model)
        return subscription_record(model)


class SqlAlchemySubscriptionPricePeriodRepository:
    """SQLAlchemy repository for effective-dated subscription prices."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, draft: SubscriptionPricePeriodDraft) -> SubscriptionPricePeriod:
        """Insert one price period; the draft has already validated itself."""
        model = SubscriptionPricePeriodModel(
            subscription_id=draft.subscription_id,
            amount_minor=draft.amount_minor,
            effective_from=draft.effective_from,
            effective_to=draft.effective_to,
        )
        self._session.add(model)
        _flush_and_refresh(self._session, model)
        return price_period_record(model)

    def get(self, price_period_id: int) -> SubscriptionPricePeriod | None:
        """Return one price period by internal ID."""
        model = self._session.get(SubscriptionPricePeriodModel, price_period_id)
        return None if model is None else price_period_record(model)

    def list_for_subscription(
        self, subscription_id: int
    ) -> tuple[SubscriptionPricePeriod, ...]:
        """Return one subscription's price history, oldest range first."""
        models = self._session.scalars(
            select(SubscriptionPricePeriodModel)
            .where(SubscriptionPricePeriodModel.subscription_id == subscription_id)
            .order_by(
                SubscriptionPricePeriodModel.effective_from,
                SubscriptionPricePeriodModel.id,
            )
        ).all()
        return tuple(price_period_record(model) for model in models)

    def list_overlapping(
        self,
        *,
        subscription_id: int,
        effective_from: date,
        effective_to: date | None,
        exclude_id: int | None = None,
    ) -> tuple[SubscriptionPricePeriod, ...]:
        """Return the ranges a proposed half-open range would collide with.

        Adjacency is legal: the upper bound is excluded, so ``[Jan, Apr)`` and
        ``[Apr, ...)`` are not reported here. The comparison runs inside the
        caller's transaction, so the answer describes the same snapshot the
        insert or update is written into.
        """
        statement = select(SubscriptionPricePeriodModel).where(
            SubscriptionPricePeriodModel.subscription_id == subscription_id,
            or_(
                SubscriptionPricePeriodModel.effective_to.is_(None),
                SubscriptionPricePeriodModel.effective_to > effective_from,
            ),
        )
        if effective_to is not None:
            statement = statement.where(
                SubscriptionPricePeriodModel.effective_from < effective_to
            )
        if exclude_id is not None:
            statement = statement.where(SubscriptionPricePeriodModel.id != exclude_id)
        models = self._session.scalars(
            statement.order_by(SubscriptionPricePeriodModel.effective_from)
        ).all()
        return tuple(price_period_record(model) for model in models)

    def save(self, change: SubscriptionPricePeriodUpdate) -> SubscriptionPricePeriod:
        """Apply one optimistic price correction; history is never deleted."""
        try:
            result = cast(
                CursorResult[Any],
                self._session.execute(
                    update(SubscriptionPricePeriodModel)
                    .where(
                        SubscriptionPricePeriodModel.id == change.price_period_id,
                        SubscriptionPricePeriodModel.version == change.expected_version,
                    )
                    .values(
                        amount_minor=change.amount_minor,
                        effective_from=change.effective_from,
                        effective_to=change.effective_to,
                        version=SubscriptionPricePeriodModel.version + 1,
                    )
                ),
            )
        except SQLAlchemyError as error:
            raise _translated(error) from error
        if result.rowcount != 1:
            existing = self._session.get(
                SubscriptionPricePeriodModel, change.price_period_id
            )
            if existing is None:
                raise SubscriptionError("subscription price period was not found")
            raise ConcurrentUpdateError("price changed after this page was loaded")
        model = self._session.get(SubscriptionPricePeriodModel, change.price_period_id)
        if model is None:  # pragma: no cover - just updated
            raise SubscriptionError("subscription price period disappeared")
        self._session.refresh(model)
        return price_period_record(model)

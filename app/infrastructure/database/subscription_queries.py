"""Indexed SQLAlchemy read models for the subscription register."""

from __future__ import annotations

from datetime import date

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.application.queries.subscriptions import (
    SubscriptionRegisterRow,
    SubscriptionRequirementInput,
)
from app.domain.budget_models import SubscriptionPricePeriod, normalize_currency
from app.infrastructure.database.budget_models import (
    BudgetCategoryModel,
    FinancialAccountModel,
    SubscriptionModel,
    SubscriptionPricePeriodModel,
    WalletModel,
)
from app.infrastructure.database.subscription_repositories import (
    price_period_record,
    subscription_record,
)


class SqlAlchemySubscriptionReadRepository:
    """Short-session reads for the register page, the CLI, and previews."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def register(self) -> tuple[SubscriptionRegisterRow, ...]:
        """Return every subscription with its names and its price history."""
        with self._session_factory() as session:
            rows = self._named_subscriptions(session)
            prices = self._prices_by_subscription(session)
        return tuple(
            SubscriptionRegisterRow(
                subscription=subscription_record(row[0]),
                budget_category_name=row[1],
                funding_wallet_name=row[2],
                funding_account_name=row[3],
                prices=prices.get(row[0].id, ()),
            )
            for row in rows
        )

    def currencies(self) -> tuple[str, ...]:
        """Return the distinct currencies the register holds."""
        with self._session_factory() as session:
            codes = session.scalars(
                select(SubscriptionModel.currency)
                .distinct()
                .order_by(SubscriptionModel.currency)
            ).all()
        return tuple(codes)

    def requirement_inputs(
        self, *, currency: str, selected_month: date
    ) -> tuple[SubscriptionRequirementInput, ...]:
        """Return one currency's subscriptions and each month's own price.

        The price is the one whose half-open range covers the first day of the
        selected month, so a historical preview reads the price that was
        effective then rather than the latest one.
        """
        code = normalize_currency(currency)
        with self._session_factory() as session:
            rows = self._named_subscriptions(session, currency=code)
            prices = {
                model.subscription_id: model
                for model in session.scalars(
                    select(SubscriptionPricePeriodModel).where(
                        SubscriptionPricePeriodModel.effective_from <= selected_month,
                        or_(
                            SubscriptionPricePeriodModel.effective_to.is_(None),
                            SubscriptionPricePeriodModel.effective_to > selected_month,
                        ),
                    )
                ).all()
            }
        return tuple(
            SubscriptionRequirementInput(
                subscription=subscription_record(row[0]),
                budget_category_name=row[1],
                funding_wallet_name=row[2],
                price=(
                    price_period_record(prices[row[0].id])
                    if row[0].id in prices
                    else None
                ),
            )
            for row in rows
        )

    @staticmethod
    def _named_subscriptions(
        session: Session, *, currency: str | None = None
    ) -> list[tuple[SubscriptionModel, str, str, str]]:
        statement = (
            select(
                SubscriptionModel,
                BudgetCategoryModel.name,
                WalletModel.name,
                FinancialAccountModel.display_name,
            )
            .join(
                BudgetCategoryModel,
                BudgetCategoryModel.id == SubscriptionModel.budget_category_id,
            )
            .join(WalletModel, WalletModel.id == SubscriptionModel.funding_wallet_id)
            .join(
                FinancialAccountModel,
                FinancialAccountModel.id == WalletModel.financial_account_id,
            )
            .order_by(SubscriptionModel.name, SubscriptionModel.id)
        )
        if currency is not None:
            statement = statement.where(SubscriptionModel.currency == currency)
        return [
            (row[0], row[1], row[2], row[3]) for row in session.execute(statement).all()
        ]

    @staticmethod
    def _prices_by_subscription(
        session: Session,
    ) -> dict[int, tuple[SubscriptionPricePeriod, ...]]:
        models = session.scalars(
            select(SubscriptionPricePeriodModel).order_by(
                SubscriptionPricePeriodModel.subscription_id,
                SubscriptionPricePeriodModel.effective_from,
                SubscriptionPricePeriodModel.id,
            )
        ).all()
        grouped: dict[int, list[SubscriptionPricePeriod]] = {}
        for model in models:
            grouped.setdefault(model.subscription_id, []).append(
                price_period_record(model)
            )
        return {key: tuple(value) for key, value in grouped.items()}

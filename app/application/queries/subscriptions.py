"""Read boundary for the subscription register and its monthly requirement."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol

from app.domain.budget_enums import SubscriptionCadence
from app.domain.budget_models import Subscription, SubscriptionPricePeriod


@dataclass(frozen=True)
class SubscriptionRegisterRow:
    """One subscription with the names its management screen needs."""

    subscription: Subscription
    budget_category_name: str
    funding_wallet_name: str
    funding_account_name: str
    prices: tuple[SubscriptionPricePeriod, ...]

    @property
    def configuration_is_frozen(self) -> bool:
        """Return whether editing would reinterpret an existing price.

        Mirrors the rule `ManageSubscriptions.update` enforces, so the page
        can show a fixed value instead of offering a control the service will
        refuse.
        """
        return bool(self.prices)

    @property
    def anchor_is_frozen(self) -> bool:
        """Return whether `active_from` is load-bearing as a cycle anchor."""
        return (
            self.configuration_is_frozen
            and self.subscription.billing_cadence is SubscriptionCadence.CUSTOM
        )


@dataclass(frozen=True)
class SubscriptionRequirementInput:
    """One subscription plus the price effective in the selected month.

    ``price`` is ``None`` when no period covers that month, which is evidence
    in itself rather than a reason to hide the subscription.
    """

    subscription: Subscription
    budget_category_name: str
    funding_wallet_name: str
    price: SubscriptionPricePeriod | None


@dataclass(frozen=True)
class DefaultFundingWalletView:
    """The configured default subscription wallet, and whether it is usable."""

    wallet_id: int | None
    wallet_name: str | None
    financial_account_name: str | None
    currency: str | None
    problem: str | None = None

    @property
    def is_configured(self) -> bool:
        """Return whether a default wallet has been chosen at all."""
        return self.wallet_id is not None

    @property
    def is_usable(self) -> bool:
        """Return whether the configured wallet can still prefill a form."""
        return self.wallet_id is not None and self.problem is None


class SubscriptionReadRepository(Protocol):
    """Aggregate the register without exposing SQLAlchemy to callers."""

    def register(self) -> tuple[SubscriptionRegisterRow, ...]: ...

    def currencies(self) -> tuple[str, ...]: ...

    def requirement_inputs(
        self, *, currency: str, selected_month: date
    ) -> tuple[SubscriptionRequirementInput, ...]: ...


class SubscriptionQueries:
    """Read-only views backing the local subscription page and CLI."""

    def __init__(self, read_repository: SubscriptionReadRepository) -> None:
        self._read_repository = read_repository

    def register(self) -> tuple[SubscriptionRegisterRow, ...]:
        """Return every subscription, deactivated ones included."""
        return self._read_repository.register()

    def currencies(self) -> tuple[str, ...]:
        """Return each currency present, so none of them is ever summed."""
        return self._read_repository.currencies()

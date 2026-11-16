"""Read boundary for calculated account and wallet ledger balances."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.domain.budget_enums import (
    PaymentMethod,
    SourceDiscrepancy,
    SourceLinkReason,
)
from app.domain.budget_models import (
    Card,
    FinancialAccountBalance,
    FinancialEvent,
)


class LedgerBalanceReadRepository(Protocol):
    """Aggregate ledger balances without exposing SQLAlchemy to callers."""

    def account_balances(self) -> tuple[FinancialAccountBalance, ...]: ...


class MoneyBalanceQueries:
    """Currency-safe balance views calculated from immutable postings."""

    def __init__(self, read_repository: LedgerBalanceReadRepository) -> None:
        self._read_repository = read_repository

    def account_balances(self) -> tuple[FinancialAccountBalance, ...]:
        """Return each active account balance and its wallet composition."""
        return self._read_repository.account_balances()


@dataclass(frozen=True)
class CardConfigurationView:
    """One card row with resolved local account and wallet names."""

    card: Card
    issuer_name: str
    liability_account_name: str | None
    settlement_account_name: str | None
    default_funding_wallet_name: str | None


@dataclass(frozen=True)
class ReviewEventView:
    """One event awaiting allocation, with stable review reason codes."""

    event: FinancialEvent
    card_display: str | None
    reasons: tuple[SourceLinkReason, ...]
    discrepancies: tuple[SourceDiscrepancy, ...]
    #: The wallet this card is configured to be paid from. It is the only
    #: defensible default for the funding choice: the card's own settlement
    #: account holds it, so reserving there needs no transfer at all.
    default_funding_wallet_id: int | None = None

    @property
    def needs_card_setup(self) -> bool:
        """Return whether a card expense still has no configured card."""
        return (
            self.event.payment_method is PaymentMethod.CREDIT_CARD
            and self.event.card_id is None
        )


class MoneyReviewReadRepository(Protocol):
    """Aggregate review and card views without exposing SQLAlchemy."""

    def cards(self) -> tuple[CardConfigurationView, ...]: ...

    def events_needing_review(self) -> tuple[ReviewEventView, ...]: ...


class MoneyReviewQueries:
    """Read-only views backing the local cards and review pages."""

    def __init__(self, read_repository: MoneyReviewReadRepository) -> None:
        self._read_repository = read_repository

    def cards(self) -> tuple[CardConfigurationView, ...]:
        """Return every configured card with resolved display names."""
        return self._read_repository.cards()

    def events_needing_review(self) -> tuple[ReviewEventView, ...]:
        """Return canonical events still waiting for allocation."""
        return self._read_repository.events_needing_review()

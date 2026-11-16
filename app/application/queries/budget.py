"""Read boundary for one month of budget, funding, and outstanding work."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.domain.budget_models import BudgetPeriod

UNCATEGORIZED_LABEL = "未分類"
UNPLANNED_LABEL_SUFFIX = " · 未編列"


@dataclass(frozen=True)
class CategoryBudgetView:
    """One category's month: what was planned, spent, and left.

    A row also exists for spending with no target and for spending with no
    category at all. Leaving either out would make the page understate what
    was spent, which is the one number a budget must never round down.
    """

    budget_category_id: int | None
    name: str
    currency: str
    target_minor: int
    buffer_minor: int
    net_spend_minor: int
    funding_wallet_id: int | None
    funding_wallet_name: str | None
    target_id: int | None = None
    target_version: int | None = None
    month_end_destination_wallet_id: int | None = None

    @property
    def is_planned(self) -> bool:
        """Return whether this row has a target for the month."""
        return self.target_id is not None

    @property
    def remaining_minor(self) -> int:
        """Return what is left of the target, never below zero."""
        return max(0, self.target_minor - self.net_spend_minor)

    @property
    def overspend_minor(self) -> int:
        """Return how far past the target this category went, if at all."""
        return max(0, self.net_spend_minor - self.target_minor)

    @property
    def remaining_buffer_minor(self) -> int:
        """Return the cushion an overspend has not already consumed."""
        return max(0, self.buffer_minor - self.overspend_minor)


@dataclass(frozen=True)
class WalletFundingView:
    """One wallet's balance, what is reserved from it, and what is free."""

    wallet_id: int
    name: str
    financial_account_name: str
    currency: str
    balance_minor: int
    reserved_minor: int

    @property
    def available_minor(self) -> int:
        """Return the balance no open commitment is already holding."""
        return self.balance_minor - self.reserved_minor


@dataclass(frozen=True)
class OpenCommitmentView:
    """One reservation still waiting to be settled."""

    commitment_id: int
    financial_event_id: int
    merchant: str | None
    occurred_at: str
    currency: str
    outstanding_minor: int
    source_wallet_name: str
    settlement_account_name: str
    fulfillment_mode: str


@dataclass(frozen=True)
class BudgetMonthView:
    """Everything the local budget page shows for one calendar month."""

    period: BudgetPeriod | None
    label: str
    currency: str
    categories: tuple[CategoryBudgetView, ...]
    wallets: tuple[WalletFundingView, ...]
    open_commitments: tuple[OpenCommitmentView, ...]
    events_needing_allocation: int

    @property
    def total_target_minor(self) -> int:
        """Return the month's spendable target; the buffer is not part of it."""
        return sum(item.target_minor for item in self.categories)

    @property
    def total_buffer_minor(self) -> int:
        """Return the cushion held beside the target, shown separately."""
        return sum(item.buffer_minor for item in self.categories)

    @property
    def total_buffer_remaining_minor(self) -> int:
        """Return the cushion still intact across every category."""
        return sum(item.remaining_buffer_minor for item in self.categories)

    @property
    def total_spend_minor(self) -> int:
        """Return net spending, refunds already subtracted.

        Includes unplanned and uncategorized spending, so this total always
        matches what the ledger recorded for the month.
        """
        return sum(item.net_spend_minor for item in self.categories)

    @property
    def unplanned_spend_minor(self) -> int:
        """Return spending that no target for this month covered."""
        return sum(
            item.net_spend_minor for item in self.categories if not item.is_planned
        )

    @property
    def total_remaining_minor(self) -> int:
        """Return what is left of the month's target across categories."""
        return sum(item.remaining_minor for item in self.categories)


class BudgetReadRepository(Protocol):
    """Aggregate one month of budget without exposing SQLAlchemy."""

    def month(self, *, year: int, month: int, currency: str) -> BudgetMonthView: ...


class BudgetQueries:
    """Read-only views backing the local budget page and CLI."""

    def __init__(self, read_repository: BudgetReadRepository) -> None:
        self._read_repository = read_repository

    def month(self, *, year: int, month: int, currency: str) -> BudgetMonthView:
        """Return one calendar month in exactly one currency."""
        return self._read_repository.month(year=year, month=month, currency=currency)

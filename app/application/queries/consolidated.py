"""Read-only composition for the consolidated local money dashboard.

The dashboard does not invent a second accounting model.  It composes the
already-reviewed B3--B7 read models, then derives only presentation-safe sums
that remain separated by currency.  Gmail is not involved, so cached money
data remains available while the network is offline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from app.application.queries.budget import BudgetMonthView, BudgetQueries
from app.application.queries.dashboard import DashboardQueries, DashboardSnapshot
from app.application.queries.spending import (
    AccountRow,
    DailyActivityView,
    SourceLinkReviewView,
    SpendingQueries,
)
from app.domain.budget_enums import FinancialAccountType
from app.domain.budget_rules import holds_wallets


@dataclass(frozen=True)
class CurrencyPosition:
    """One currency's physical assets, reservations, and card liability."""

    currency: str
    asset_minor: int
    reserved_minor: int
    card_liability_minor: int

    @property
    def available_minor(self) -> int:
        """Return asset money not already reserved for an obligation."""
        return self.asset_minor - self.reserved_minor


@dataclass(frozen=True)
class OverviewAccount:
    """One account card with a stable, user-readable reconciliation state."""

    row: AccountRow
    state: str

    @property
    def needs_attention(self) -> bool:
        """Return whether the overview should point at reconciliation."""
        return self.state in {"not_observed", "difference"}


@dataclass(frozen=True)
class ConsolidatedMoneyOverview:
    """All read-only information rendered by the local landing page."""

    generated_at: datetime
    positions: tuple[CurrencyPosition, ...]
    accounts: tuple[OverviewAccount, ...]
    today: DailyActivityView
    budget: BudgetMonthView
    source_links: SourceLinkReviewView
    parsed_transactions: DashboardSnapshot

    @property
    def reconciliation_attention(self) -> tuple[OverviewAccount, ...]:
        """Return only accounts whose latest evidence needs attention."""
        return tuple(account for account in self.accounts if account.needs_attention)

    @property
    def asset_accounts(self) -> tuple[OverviewAccount, ...]:
        """Return the accounts that actually hold money."""
        return tuple(
            item
            for item in self.accounts
            if holds_wallets(item.row.account.account_type)
        )

    @property
    def card_liability_accounts(self) -> tuple[OverviewAccount, ...]:
        """Return what is owed to card issuers, kept apart from assets.

        A liability is not a balance you can spend, and reading it in the same
        list as a bank account invites exactly that mistake — which is why the
        dashboard never nets one against the other (ADR-B013, ADR-B071).
        """
        return tuple(
            item
            for item in self.accounts
            if item.row.account.account_type is FinancialAccountType.CARD_LIABILITY
        )

    @property
    def today_waiting_confirmation(self) -> int:
        """Return today's parsed transactions nobody has confirmed yet."""
        return self.parsed_transactions.unconfirmed_today_count

    @property
    def today_waiting_allocation(self) -> int:
        """Return confirmed events still waiting for a category and wallet."""
        return self.budget.events_needing_allocation

    @property
    def today_is_waiting_on_review(self) -> bool:
        """Return whether an empty day is unreviewed work rather than no spending.

        A day's total is derived from posted expense lines (ADR-B064), so a
        parsed Gmail notification contributes nothing until it is confirmed and
        then allocated. Both steps are the user's, and a zero that hides them
        reads as "you spent nothing today" when the truth is "two queues are
        waiting" (ADR-B092).
        """
        return not self.today.rows and bool(
            self.today_waiting_confirmation or self.today_waiting_allocation
        )

    @property
    def attention_count(self) -> int:
        """Count actionable queues without counting any money twice."""
        return (
            len(self.reconciliation_attention)
            + self.source_links.pending_total
            + self.budget.events_needing_allocation
        )


class ConsolidatedMoneyQueries:
    """Compose existing read services into the local product overview."""

    def __init__(
        self,
        *,
        spending: SpendingQueries,
        budget: BudgetQueries,
        parsed_transactions: DashboardQueries,
        default_currency: str,
        local_timezone: str,
    ) -> None:
        self._spending = spending
        self._budget = budget
        self._parsed_transactions = parsed_transactions
        self._default_currency = default_currency
        self._timezone = ZoneInfo(local_timezone)

    def overview(
        self, *, now: datetime, recent_days: int = 7
    ) -> ConsolidatedMoneyOverview:
        """Return a currency-safe snapshot without Gmail or network access.

        ``recent_days`` only widens the parsed-notification list; the
        positions, today's spending, and the budget month are unaffected.
        """
        local_day = now.astimezone(self._timezone).date()
        accounts = self._spending.accounts()
        return ConsolidatedMoneyOverview(
            generated_at=now,
            positions=currency_positions(accounts),
            accounts=tuple(
                OverviewAccount(row=row, state=reconciliation_state(row))
                for row in accounts
            ),
            today=self._spending.daily(day=local_day, page_size=5),
            budget=self._budget.month(
                year=local_day.year,
                month=local_day.month,
                currency=self._default_currency,
            ),
            source_links=self._spending.source_links(pending_limit=1, decided_limit=1),
            parsed_transactions=self._parsed_transactions.dashboard(
                now=now, recent_days=recent_days
            ),
        )


def currency_positions(
    accounts: tuple[AccountRow, ...],
) -> tuple[CurrencyPosition, ...]:
    """Aggregate account positions per currency, never across currencies.

    Assets and card liabilities stay in separate fields because subtracting a
    negative liability from an available bank balance would make the word
    "available" untrue.  Liability balances retain their signed ledger value.
    """
    grouped: dict[str, list[int]] = {}
    for row in accounts:
        values = grouped.setdefault(row.account.currency, [0, 0, 0])
        if holds_wallets(row.account.account_type):
            values[0] += row.balance_minor
            values[1] += row.reserved_minor
        elif row.account.account_type is FinancialAccountType.CARD_LIABILITY:
            values[2] += row.balance_minor
    return tuple(
        CurrencyPosition(
            currency=currency,
            asset_minor=values[0],
            reserved_minor=values[1],
            card_liability_minor=values[2],
        )
        for currency, values in sorted(grouped.items())
    )


def reconciliation_state(row: AccountRow) -> str:
    """Return a stable display state for one physical account."""
    if not holds_wallets(row.account.account_type):
        return "ledger_only"
    if row.latest_snapshot is None:
        return "not_observed"
    if row.latest_snapshot.difference_minor != 0:
        return "difference"
    return "reconciled"

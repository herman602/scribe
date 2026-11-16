"""Read boundary for the salary plan, its months, and its frozen evidence.

A historical run is displayed from the snapshot it froze, never by asking the
subscription register what it says today (ADR-B045). Nothing in this module
recalculates an amount.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.domain.budget_enums import SalaryRunItemStatus, SalaryRunStatus
from app.domain.budget_models import (
    BudgetCategory,
    FinancialEvent,
    SalaryAllocationRun,
    SalaryAllocationRunItem,
    SalaryItemEvidence,
    SalaryPlanTemplate,
    SalaryPlanTemplateItem,
    SalarySubscriptionEvidence,
)


@dataclass(frozen=True)
class SalaryTemplateItemRow:
    """One template destination with the names its screen has to show."""

    item: SalaryPlanTemplateItem
    destination_wallet_name: str
    destination_account_name: str
    budget_category_name: str | None
    #: Where the destination account sits in the arrangement the accounts
    #: sheet stores, so the plan groups in the same order the user reads
    #: their banks in (ADR-B088).
    destination_account_order: int = 0


@dataclass(frozen=True)
class SalaryTemplateRow:
    """One template, its salary account, and its destinations."""

    template: SalaryPlanTemplate
    source_account_name: str
    source_institution_code: str
    source_wallet_id: int | None
    source_wallet_name: str | None
    items: tuple[SalaryTemplateItemRow, ...]

    @property
    def plan_groups(self) -> tuple[tuple[str, tuple[SalaryTemplateItemRow, ...]], ...]:
        """Return the active destinations grouped by the bank they land in.

        Groups follow the arrangement the accounts sheet stores rather than
        the spelling of a bank's name, so the plan reads in the same order as
        the accounts page (ADR-B088, ADR-B090). Within a group the plan keeps
        its own item order.
        """
        active = [entry for entry in self.items if entry.item.is_active]
        ordered = sorted(active, key=lambda entry: entry.destination_account_order)
        groups: list[tuple[str, tuple[SalaryTemplateItemRow, ...]]] = []
        for entry in ordered:
            name = entry.destination_account_name
            if groups and groups[-1][0] == name:
                groups[-1] = (name, (*groups[-1][1], entry))
            else:
                groups.append((name, (entry,)))
        return tuple(groups)

    @property
    def planned_fixed_total_minor(self) -> int:
        """Return what the fixed destinations add up to every month.

        A calculated destination is deliberately absent: its amount is decided
        when a month is generated, so including a stale guess here would state
        a plan total the generation would not honour.
        """
        return sum(
            entry.item.fixed_amount_minor or 0
            for entry in self.items
            if entry.item.is_active
        )

    @property
    def is_usable(self) -> bool:
        """Return whether a month could actually be generated from this plan."""
        return (
            self.template.is_active
            and self.source_wallet_id is not None
            and bool(self.items)
        )


@dataclass(frozen=True)
class SalaryAccountOption:
    """One account a template could receive a salary into."""

    financial_account_id: int
    display_name: str
    institution_code: str
    currency: str
    has_unallocated_wallet: bool


@dataclass(frozen=True)
class SalaryWalletOption:
    """One wallet a template line could pay into."""

    wallet_id: int
    wallet_name: str
    financial_account_id: int
    account_name: str
    institution_code: str
    currency: str
    is_unallocated: bool


@dataclass(frozen=True)
class EligibleSalaryEventRow:
    """One posted deposit whose structure really is a salary in this account."""

    event: FinancialEvent
    entry_id: int
    entry_effective_at: datetime


@dataclass(frozen=True)
class SalaryWalletPosition:
    """The three different numbers a salary wallet has at once."""

    wallet_id: int
    wallet_name: str
    account_name: str
    currency: str
    balance_minor: int
    reserved_minor: int

    @property
    def available_minor(self) -> int:
        """Return what is left once every reservation is counted."""
        return self.balance_minor - self.reserved_minor


@dataclass(frozen=True)
class SalaryRunItemView:
    """One frozen line with its decoded evidence, never a recalculation."""

    item: SalaryAllocationRunItem
    evidence: SalaryItemEvidence

    @property
    def is_waiting_transfer(self) -> bool:
        """Return whether this line still needs a real bank transfer."""
        return self.item.status is SalaryRunItemStatus.WAITING_TRANSFER


@dataclass(frozen=True)
class SalaryRunView:
    """One generation, everything it froze, and its source wallet position."""

    run: SalaryAllocationRun
    items: tuple[SalaryRunItemView, ...]
    evidence: SalarySubscriptionEvidence
    source: SalaryWalletPosition
    salary_event: FinancialEvent | None

    @property
    def label(self) -> str:
        """Return the ``yyyy-mm`` month this generation was frozen for."""
        return self.evidence.label

    @property
    def is_draft(self) -> bool:
        """Return whether this generation can still be approved or replaced."""
        return self.run.status is SalaryRunStatus.DRAFT

    @property
    def waiting_transfer_minor(self) -> int:
        """Return what this run still reserves for real bank transfers."""
        return sum(
            view.item.amount_minor for view in self.items if view.is_waiting_transfer
        )

    @property
    def subscription_total_minor(self) -> int:
        """Return the frozen subscription requirement for this month."""
        return self.evidence.total_minor


@dataclass(frozen=True)
class SalaryMonthView:
    """One calendar month of salary allocation for one chosen template."""

    year: int
    month: int
    currency: str
    templates: tuple[SalaryTemplateRow, ...]
    selected: SalaryTemplateRow | None
    live_run: SalaryRunView | None
    generations: tuple[SalaryAllocationRun, ...]
    eligible_events: tuple[EligibleSalaryEventRow, ...]
    period_is_closed: bool
    #: The applied cutover instant, when one exists. A month wholly at or
    #: before it was already settled into the opening balances.
    opening_cutoff_at: datetime | None = None

    @property
    def label(self) -> str:
        """Return the ``yyyy-mm`` label used by routes and the CLI."""
        return f"{self.year:04d}-{self.month:02d}"

    @property
    def is_before_opening_cutoff(self) -> bool:
        """Return whether this whole month precedes the opening boundary.

        A salary received before cutover is already inside the opening
        balances, so allocating it again would count the same money twice.
        The month is not broken and not missing data; there is deliberately
        nothing here (ADR-B083, ADR-B092).
        """
        if self.opening_cutoff_at is None:
            return False
        cutoff = self.opening_cutoff_at
        return (self.year, self.month) < (cutoff.year, cutoff.month)

    @property
    def has_nothing_to_do(self) -> bool:
        """Return whether the month is empty rather than merely unfinished."""
        return self.live_run is None and not self.eligible_events


class SalaryReadRepository(Protocol):
    """Aggregate the salary plan without exposing SQLAlchemy to callers."""

    def templates(self) -> tuple[SalaryTemplateRow, ...]: ...

    def template(self, template_id: int) -> SalaryTemplateRow | None: ...

    def account_options(self) -> tuple[SalaryAccountOption, ...]: ...

    def wallet_options(self, currency: str) -> tuple[SalaryWalletOption, ...]: ...

    def category_options(self) -> tuple[BudgetCategory, ...]: ...

    def eligible_salary_events(
        self, template_id: int
    ) -> tuple[EligibleSalaryEventRow, ...]: ...

    def month(
        self, *, year: int, month: int, template_id: int | None, currency: str
    ) -> SalaryMonthView: ...

    def run(self, run_id: int) -> SalaryRunView | None: ...


class SalaryQueries:
    """Read-only views backing the local allocation pages and the CLI."""

    def __init__(self, read_repository: SalaryReadRepository) -> None:
        self._read_repository = read_repository

    def templates(self) -> tuple[SalaryTemplateRow, ...]:
        """Return every template, deactivated ones included."""
        return self._read_repository.templates()

    def template(self, template_id: int) -> SalaryTemplateRow | None:
        """Return one template and its destinations."""
        return self._read_repository.template(template_id)

    def account_options(self) -> tuple[SalaryAccountOption, ...]:
        """Return the accounts a salary could be received into."""
        return self._read_repository.account_options()

    def wallet_options(self, currency: str) -> tuple[SalaryWalletOption, ...]:
        """Return the wallets one currency's plan could pay into."""
        return self._read_repository.wallet_options(currency)

    def category_options(self) -> tuple[BudgetCategory, ...]:
        """Return every budget category a line may be attributed to."""
        return self._read_repository.category_options()

    def eligible_salary_events(
        self, template_id: int
    ) -> tuple[EligibleSalaryEventRow, ...]:
        """Return the posted deposits this template could distribute."""
        return self._read_repository.eligible_salary_events(template_id)

    def month(
        self, *, year: int, month: int, template_id: int | None, currency: str
    ) -> SalaryMonthView:
        """Return one month's plan, its live generation, and its history."""
        return self._read_repository.month(
            year=year, month=month, template_id=template_id, currency=currency
        )

    def run(self, run_id: int) -> SalaryRunView | None:
        """Return one generation exactly as it was frozen."""
        return self._read_repository.run(run_id)

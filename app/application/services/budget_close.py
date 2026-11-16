"""Preview and commit the end of one budget month.

Closing a month is a reclassification, never a payment: it may only move
leftover target money between wallets of the *same* physical account. A
destination in another account needs a real bank transfer, so the close
reports it and posts nothing for that line.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.application.services.ledger import JournalService
from app.domain.budget_enums import BudgetPeriodStatus, JournalEntryType
from app.domain.budget_models import (
    BudgetPeriod,
    JournalEntry,
    LedgerAccount,
    Money,
    PostingDraft,
    Wallet,
)
from app.domain.budget_rules import (
    is_wallet_managed,
    period_bounds,
    remaining_buffer_minor,
    sweepable_minor,
)
from app.domain.errors import (
    BudgetPeriodError,
    LedgerConfigurationError,
)
from app.domain.ports.repositories import UnitOfWork

type UnitOfWorkFactory = Callable[[], UnitOfWork]
type Clock = Callable[[], datetime]


def close_idempotency_key(period_id: int) -> str:
    """Return the stable key for one month's single sweep entry."""
    return f"budget_close:budget_period:{period_id}"


@dataclass(frozen=True)
class CategoryCloseLine:
    """What one category leaves behind when its month ends."""

    budget_category_id: int
    category_name: str
    target_minor: int
    buffer_minor: int
    net_spend_minor: int
    unspent_minor: int
    remaining_buffer_minor: int
    sweep_minor: int
    funding_wallet_id: int
    destination_wallet_id: int | None
    requires_manual_transfer: bool
    limited_by_available: bool

    @property
    def overspend_minor(self) -> int:
        """Return how far past the target this category went, if at all."""
        return max(0, self.net_spend_minor - self.target_minor)


@dataclass(frozen=True)
class BudgetClosePreview:
    """Everything closing this month would do, before anything is written."""

    period: BudgetPeriod
    lines: tuple[CategoryCloseLine, ...]
    already_closed: bool

    @property
    def total_sweep_minor(self) -> int:
        """Return the sweep total; every line is in the period currency."""
        return sum(line.sweep_minor for line in self.lines)

    @property
    def manual_transfers(self) -> tuple[CategoryCloseLine, ...]:
        """Return the lines a real bank transfer has to settle by hand."""
        return tuple(line for line in self.lines if line.requires_manual_transfer)


@dataclass(frozen=True)
class BudgetCloseResult:
    """The committed outcome of closing one month."""

    period: BudgetPeriod
    preview: BudgetClosePreview
    entry: JournalEntry | None


class CloseBudgetPeriod:
    """Report, then optionally commit, the sweep that ends one month."""

    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        journal: JournalService,
        *,
        local_timezone: str,
        clock: Clock = lambda: datetime.now(UTC),
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._journal = journal
        self._timezone = ZoneInfo(local_timezone)
        self._clock = clock

    def preview(self, *, year: int, month: int, currency: str) -> BudgetClosePreview:
        """Return what closing this month would move, writing nothing."""
        with self._unit_of_work_factory() as unit:
            return self._build_preview(unit, year=year, month=month, currency=currency)

    def execute(self, *, year: int, month: int, currency: str) -> BudgetCloseResult:
        """Commit the sweep and close the month, or replay a closed month."""
        with self._unit_of_work_factory() as unit:
            preview = self._build_preview(
                unit, year=year, month=month, currency=currency
            )
            period = preview.period
            existing = unit.journal.get_by_idempotency_key(
                close_idempotency_key(period.id)
            )
            if preview.already_closed:
                # Closing twice is a no-op that answers with the first result,
                # so a retried request never sweeps a second time.
                return BudgetCloseResult(period=period, preview=preview, entry=existing)

            postings = self._sweep_postings(unit, preview)
            entry = None
            if postings:
                entry = self._journal.stage_balanced(
                    unit,
                    entry_type=JournalEntryType.BUDGET_CLOSE,
                    effective_at=period_bounds(year, month, self._timezone)[1],
                    idempotency_key=close_idempotency_key(period.id),
                    description=f"Month-end sweep {period.label}",
                    postings=postings,
                )
            closed = unit.budget_periods.close(period.id, closed_at=self._clock())
            unit.commit()
        return BudgetCloseResult(period=closed, preview=preview, entry=entry)

    def _build_preview(
        self, unit: UnitOfWork, *, year: int, month: int, currency: str
    ) -> BudgetClosePreview:
        period = unit.budget_periods.find(year=year, month=month, currency=currency)
        if period is None:
            raise BudgetPeriodError("this month has no budget to close")
        start, end = period_bounds(year, month, self._timezone)
        spend = unit.budget_ledger.net_spend_by_category(
            start=start, end=end, currency=period.currency
        )
        targets = unit.budget_targets.list_for_period(period.id)
        # Every buffer funded by a wallet is withheld before anything is swept,
        # not just the first one seen, so two categories sharing a wallet
        # cannot between them sweep a cushion away. Only what an overspend has
        # not already consumed is withheld: money the wallet no longer holds
        # must not be reserved a second time.
        buffers: dict[int, int] = {}
        for target in targets:
            buffers[target.funding_wallet_id] = buffers.get(
                target.funding_wallet_id, 0
            ) + remaining_buffer_minor(
                buffer_minor=target.buffer_minor,
                target_minor=target.target_minor,
                net_spend_minor=spend.get(target.budget_category_id, 0),
            )
        available: dict[int, int] = {}
        lines: list[CategoryCloseLine] = []
        for target in targets:
            category = unit.budget_categories.get(target.budget_category_id)
            if category is None:
                raise LedgerConfigurationError("budget category was not found")
            funding = self._require_wallet(unit, target.funding_wallet_id)
            unspent = sweepable_minor(
                target_minor=target.target_minor,
                net_spend_minor=spend.get(target.budget_category_id, 0),
            )
            if funding.id not in available:
                available[funding.id] = max(
                    0,
                    unit.budget_ledger.wallet_balance(funding.id)
                    - unit.budget_ledger.reserved_minor(funding.id)
                    - buffers[funding.id],
                )
            destination_id = target.month_end_destination_wallet_id
            manual = False
            if destination_id is not None:
                destination = self._require_wallet(unit, destination_id)
                manual = (
                    destination.financial_account_id != funding.financial_account_id
                )
            sweep = 0 if destination_id is None or manual else unspent
            sweep = min(sweep, available[funding.id])
            available[funding.id] -= sweep
            lines.append(
                CategoryCloseLine(
                    budget_category_id=target.budget_category_id,
                    category_name=category.name,
                    target_minor=target.target_minor,
                    buffer_minor=target.buffer_minor,
                    net_spend_minor=spend.get(target.budget_category_id, 0),
                    unspent_minor=unspent,
                    remaining_buffer_minor=remaining_buffer_minor(
                        buffer_minor=target.buffer_minor,
                        target_minor=target.target_minor,
                        net_spend_minor=spend.get(target.budget_category_id, 0),
                    ),
                    sweep_minor=sweep,
                    funding_wallet_id=funding.id,
                    destination_wallet_id=destination_id,
                    requires_manual_transfer=manual,
                    limited_by_available=sweep < unspent and not manual,
                )
            )
        return BudgetClosePreview(
            period=period,
            lines=tuple(lines),
            already_closed=period.status is BudgetPeriodStatus.CLOSED,
        )

    def _sweep_postings(
        self, unit: UnitOfWork, preview: BudgetClosePreview
    ) -> tuple[PostingDraft, ...]:
        """Build one balanced wallet-to-wallet reclassification per line."""
        currency = preview.period.currency
        postings: list[PostingDraft] = []
        for line in preview.lines:
            if line.sweep_minor <= 0 or line.destination_wallet_id is None:
                continue
            funding = self._require_wallet(unit, line.funding_wallet_id)
            ledger = self._require_wallet_ledger(unit, funding)
            postings.append(
                PostingDraft(
                    ledger_account_id=ledger.id,
                    amount=Money(-line.sweep_minor, currency),
                    wallet_id=funding.id,
                )
            )
            postings.append(
                PostingDraft(
                    ledger_account_id=ledger.id,
                    amount=Money(line.sweep_minor, currency),
                    wallet_id=line.destination_wallet_id,
                )
            )
        return tuple(postings)

    @staticmethod
    def _require_wallet(unit: UnitOfWork, wallet_id: int) -> Wallet:
        wallet = unit.wallets.get(wallet_id)
        if wallet is None:
            raise LedgerConfigurationError("budget wallet was not found")
        return wallet

    @staticmethod
    def _require_wallet_ledger(unit: UnitOfWork, wallet: Wallet) -> LedgerAccount:
        ledger = unit.ledger_accounts.get_for_financial_account(
            wallet.financial_account_id
        )
        if ledger is None or not is_wallet_managed(ledger):
            raise LedgerConfigurationError(
                "this wallet's account has no wallet-managed ledger account"
            )
        return ledger

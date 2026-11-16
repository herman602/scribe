"""Shared B7 test values and service bundles.

These live outside `conftest.py` so tests can import them by name without the
file being seen under two module names by the type checker.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from app.application.queries.spending import SpendingQueries
from app.application.services.bank_movements import RecordBankMovement
from app.application.services.event_allocation import (
    AllocateFinancialEvent,
    RedoAllocation,
)
from app.application.services.financial_event_intake import FinancialEventIntake
from app.application.services.manual_money import (
    CreateManualExpense,
    ReverseFinancialEvent,
)
from app.application.services.reconciliation import (
    AdjustReconciliation,
    RecordBalanceSnapshot,
)
from app.application.services.source_review import ReviewSourceLinks
from app.domain.ports.repositories import UnitOfWork

type UnitOfWorkFactory = Callable[[], UnitOfWork]

#: One fixed instant every B7 test measures from, so nothing depends on today.
B7_NOW = datetime(2026, 8, 9, 4, 30, tzinfo=UTC)


@dataclass(frozen=True)
class B7World:
    """Identifiers of the accounts, wallets, card, and categories B7 uses."""

    bank_account_id: int
    bank_unallocated_id: int
    bank_food_wallet_id: int
    bank_clearing_wallet_id: int
    cash_account_id: int
    cash_wallet_id: int
    liability_account_id: int
    card_id: int
    food_id: int
    transport_id: int
    gmail_account_id: int


@dataclass(frozen=True)
class B7Services:
    """The B7 commands and queries, all sharing one database."""

    factory: UnitOfWorkFactory
    allocate: AllocateFinancialEvent
    redo: RedoAllocation
    expenses: CreateManualExpense
    reversal: ReverseFinancialEvent
    movements: RecordBankMovement
    snapshots: RecordBalanceSnapshot
    reconciliation: AdjustReconciliation
    intake: FinancialEventIntake
    review: ReviewSourceLinks
    spending: SpendingQueries

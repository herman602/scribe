"""Manage the spending purposes and monthly targets a budget is made of.

Categories are currency-neutral; a target is always one category in one month
in exactly one currency, funded from one explicitly named wallet (ADR-B032).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.domain.budget_enums import BudgetPeriodStatus
from app.domain.budget_models import (
    BudgetCategory,
    BudgetCategoryDraft,
    BudgetCategoryUpdate,
    BudgetPeriod,
    BudgetTarget,
    BudgetTargetDraft,
)
from app.domain.errors import (
    BudgetPeriodError,
    LedgerConfigurationError,
)
from app.domain.ports.repositories import UnitOfWork

type UnitOfWorkFactory = Callable[[], UnitOfWork]

MAX_CATEGORY_DEPTH = 3


@dataclass(frozen=True)
class AddCategoryRequest:
    """One new spending purpose, optionally under an existing parent."""

    name: str
    parent_id: int | None = None


@dataclass(frozen=True)
class UpdateCategoryRequest:
    """One optimistic rename, re-parent, or activation change."""

    category_id: int
    expected_version: int
    name: str
    parent_id: int | None
    is_active: bool


@dataclass(frozen=True)
class SetTargetRequest:
    """One month's target and buffer for one category."""

    year: int
    month: int
    currency: str
    budget_category_id: int
    target_minor: int
    buffer_minor: int
    funding_wallet_id: int
    month_end_destination_wallet_id: int | None = None
    expected_version: int | None = None


class ManageBudgetPlan:
    """Create categories and set the targets a month is measured against."""

    def __init__(self, unit_of_work_factory: UnitOfWorkFactory) -> None:
        self._unit_of_work_factory = unit_of_work_factory

    def add_category(self, request: AddCategoryRequest) -> BudgetCategory:
        """Create one spending purpose below an optional active parent."""
        name = request.name.strip()
        if not name:
            raise LedgerConfigurationError("a budget category needs a name")
        with self._unit_of_work_factory() as unit:
            self._require_parent_depth(unit, request.parent_id)
            for existing in unit.budget_categories.list_all():
                if existing.name == name and existing.parent_id == request.parent_id:
                    raise LedgerConfigurationError(
                        "a sibling budget category already uses this name"
                    )
            category = unit.budget_categories.add(
                BudgetCategoryDraft(name=name, parent_id=request.parent_id)
            )
            unit.commit()
        return category

    def list_categories(self) -> tuple[BudgetCategory, ...]:
        """Return every category, active or not, for management screens."""
        with self._unit_of_work_factory() as unit:
            return unit.budget_categories.list_all()

    def update_category(self, request: UpdateCategoryRequest) -> BudgetCategory:
        """Rename, re-parent, or deactivate one category optimistically."""
        name = request.name.strip()
        if not name:
            raise LedgerConfigurationError("a budget category needs a name")
        with self._unit_of_work_factory() as unit:
            current = unit.budget_categories.get(request.category_id)
            if current is None:
                raise LedgerConfigurationError("budget category was not found")
            if request.parent_id == request.category_id:
                raise LedgerConfigurationError("a category cannot be its own parent")
            self._require_parent_depth(unit, request.parent_id)
            self._reject_cycle(unit, request.category_id, request.parent_id)
            # Deactivating a used category is allowed and never deletes it:
            # history has to stay readable, so the row is only hidden from
            # new allocations.
            category = unit.budget_categories.save(
                BudgetCategoryUpdate(
                    category_id=request.category_id,
                    expected_version=request.expected_version,
                    name=name,
                    parent_id=request.parent_id,
                    is_active=request.is_active,
                )
            )
            unit.commit()
        return category

    def set_target(self, request: SetTargetRequest) -> BudgetTarget:
        """Set one category's target and buffer for one open month."""
        with self._unit_of_work_factory() as unit:
            period = unit.budget_periods.get_or_create(
                year=request.year, month=request.month, currency=request.currency
            )
            if period.status is BudgetPeriodStatus.CLOSED:
                raise BudgetPeriodError("a closed month no longer accepts a target")
            category = unit.budget_categories.get(request.budget_category_id)
            if category is None:
                raise LedgerConfigurationError("budget category was not found")
            if not category.is_active:
                raise LedgerConfigurationError(
                    "an inactive budget category cannot be given a target"
                )
            self._require_wallet_currency(
                unit, request.funding_wallet_id, period.currency
            )
            if request.month_end_destination_wallet_id is not None:
                self._require_wallet_currency(
                    unit, request.month_end_destination_wallet_id, period.currency
                )
            target = unit.budget_targets.upsert(
                BudgetTargetDraft(
                    budget_period_id=period.id,
                    budget_category_id=request.budget_category_id,
                    target_minor=request.target_minor,
                    buffer_minor=request.buffer_minor,
                    funding_wallet_id=request.funding_wallet_id,
                    month_end_destination_wallet_id=(
                        request.month_end_destination_wallet_id
                    ),
                    expected_version=request.expected_version,
                )
            )
            unit.commit()
        return target

    def find_target(
        self, *, year: int, month: int, currency: str, budget_category_id: int
    ) -> BudgetTarget | None:
        """Return one category's existing target, so a caller can correct it."""
        with self._unit_of_work_factory() as unit:
            period = unit.budget_periods.find(year=year, month=month, currency=currency)
            if period is None:
                return None
            for target in unit.budget_targets.list_for_period(period.id):
                if target.budget_category_id == budget_category_id:
                    return target
            return None

    def period(self, *, year: int, month: int, currency: str) -> BudgetPeriod:
        """Return one month, creating it the first time it is looked at."""
        with self._unit_of_work_factory() as unit:
            period = unit.budget_periods.get_or_create(
                year=year, month=month, currency=currency
            )
            unit.commit()
        return period

    @staticmethod
    def _require_wallet_currency(
        unit: UnitOfWork, wallet_id: int, currency: str
    ) -> None:
        """A wallet funds a target only in its own account's currency."""
        wallet = unit.wallets.get(wallet_id)
        if wallet is None:
            raise LedgerConfigurationError("funding wallet was not found")
        account = unit.financial_accounts.get(wallet.financial_account_id)
        if account is None:
            raise LedgerConfigurationError("funding account was not found")
        if account.currency != currency:
            raise LedgerConfigurationError(
                "a target must be funded from a wallet in the same currency"
            )

    @staticmethod
    def _require_parent_depth(unit: UnitOfWork, parent_id: int | None) -> None:
        """Keep the tree shallow enough to stay navigable in one screen."""
        depth = 1
        current = parent_id
        while current is not None:
            parent = unit.budget_categories.get(current)
            if parent is None:
                raise LedgerConfigurationError("parent budget category was not found")
            depth += 1
            if depth > MAX_CATEGORY_DEPTH:
                raise LedgerConfigurationError(
                    f"budget categories nest at most {MAX_CATEGORY_DEPTH} levels"
                )
            current = parent.parent_id

    @staticmethod
    def _reject_cycle(
        unit: UnitOfWork, category_id: int, parent_id: int | None
    ) -> None:
        """Re-parenting must never make a category its own ancestor."""
        current = parent_id
        while current is not None:
            if current == category_id:
                raise LedgerConfigurationError(
                    "a category cannot be moved under its own descendant"
                )
            parent = unit.budget_categories.get(current)
            if parent is None:
                return
            current = parent.parent_id

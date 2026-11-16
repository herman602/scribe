"""Idempotent one-click and batch transaction confirmation."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from app.application.services.financial_event_intake import (
    FinancialEventIntake,
    IntakeResult,
)
from app.domain.ports.repositories import UnitOfWork

type UnitOfWorkFactory = Callable[[], UnitOfWork]

_MAX_CONFIRMATION_BATCH = 500


@dataclass(frozen=True)
class ConfirmationResult:
    """Counts for one atomic confirmation request."""

    requested_count: int
    confirmed_count: int
    intake: IntakeResult | None = None


class ConfirmTransactions:
    """Confirm pending active transactions without touching parsed/manual values.

    Confirmation and financial-event intake share one Unit of Work, so either
    both commit or neither does.
    """

    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        *,
        intake: FinancialEventIntake | None = None,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._intake = intake

    def execute(self, transaction_ids: Iterable[int]) -> ConfirmationResult:
        """Deduplicate, validate and atomically confirm the selected transactions."""
        normalized = frozenset(transaction_ids)
        if not normalized:
            raise ValueError("請至少選擇一筆交易")
        if any(transaction_id < 1 for transaction_id in normalized):
            raise ValueError("交易 ID 必須是正整數")
        if len(normalized) > _MAX_CONFIRMATION_BATCH:
            raise ValueError(f"一次最多確認 {_MAX_CONFIRMATION_BATCH} 筆交易")

        with self._unit_of_work_factory() as unit:
            confirmed_count = unit.transactions.confirm_pending(normalized)
            intake_result = (
                self._intake.stage(unit, normalized)
                if self._intake is not None
                else None
            )
            unit.commit()
        return ConfirmationResult(
            requested_count=len(normalized),
            confirmed_count=confirmed_count,
            intake=intake_result,
        )

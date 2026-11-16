"""Manual transaction review and override command."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from app.application.services.financial_event_intake import FinancialEventIntake
from app.domain.enums import TransactionType
from app.domain.models import Transaction, TransactionReviewDraft
from app.domain.ports.repositories import UnitOfWork

type UnitOfWorkFactory = Callable[[], UnitOfWork]

_EDITABLE_FIELDS = {
    "transaction_type",
    "merchant",
    "amount_minor",
    "currency",
    "transaction_at",
}


@dataclass(frozen=True)
class ReviewTransactionRequest:
    """Typed review form after presentation-layer decoding."""

    transaction_id: int
    expected_updated_at: datetime
    override_fields: frozenset[str]
    transaction_type: TransactionType
    merchant: str | None
    amount_minor: int | None
    currency: str | None
    transaction_at: datetime | None
    manually_confirmed: bool
    manually_ignored: bool
    user_note: str | None


class ReviewTransaction:
    """Validate intent and commit one optimistic manual review.

    A review that confirms the transaction stages its financial event, and
    every review reconciles the bridge afterwards, so unconfirming or ignoring
    an already bridged transaction records a discrepancy instead of silently
    leaving a stale event. All of it shares one Unit of Work.
    """

    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        *,
        intake: FinancialEventIntake | None = None,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._intake = intake

    def execute(self, request: ReviewTransactionRequest) -> Transaction:
        """Apply explicit overrides; unchecked fields return to parsed values."""
        if request.transaction_id < 1:
            raise ValueError("transaction ID must be positive")
        if not request.override_fields <= _EDITABLE_FIELDS:
            raise ValueError("unsupported override field")
        merchant = request.merchant.strip() if request.merchant else None
        currency = request.currency.strip().upper() if request.currency else None
        note = request.user_note.strip() if request.user_note else None
        if merchant is not None and len(merchant) > 500:
            raise ValueError("merchant is too long")
        if note is not None and len(note) > 4000:
            raise ValueError("note is too long")
        draft = TransactionReviewDraft(
            transaction_id=request.transaction_id,
            expected_updated_at=request.expected_updated_at,
            override_fields=request.override_fields,
            transaction_type=request.transaction_type,
            merchant=merchant,
            amount_minor=request.amount_minor,
            currency=currency,
            transaction_at=request.transaction_at,
            manually_confirmed=request.manually_confirmed,
            manually_ignored=request.manually_ignored,
            user_note=note,
        )
        with self._unit_of_work_factory() as unit:
            result = unit.transactions.apply_review(draft)
            if self._intake is not None:
                if result.manually_confirmed:
                    self._intake.stage(unit, (result.id,))
                self._intake.refresh_for_email(unit, result.email_id)
            unit.commit()
            return result

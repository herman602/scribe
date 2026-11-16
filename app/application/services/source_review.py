"""Decide whether a bank notification and a typed expense are one thing.

Every command here writes provenance and nothing else: no journal entry, no
allocation, and no second canonical event — except the one path where a person
explicitly says the two observations are different money (ADR-B062).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from app.application.services.financial_event_intake import (
    FinancialEventIntake,
    StagedEvent,
)
from app.domain.budget_enums import (
    FinancialEventOrigin,
    FinancialEventStatus,
    PaymentMethod,
    SourceLinkReason,
    SourceLinkStatus,
)
from app.domain.budget_matching import MatchObservation, score_match
from app.domain.budget_models import FinancialEventSourceLink
from app.domain.budget_rules import payment_method_for_transaction
from app.domain.enums import EmailSourceStatus, TransactionSourceStatus
from app.domain.errors import SourceLinkError
from app.domain.ports.repositories import UnitOfWork

type UnitOfWorkFactory = Callable[[], UnitOfWork]
type Clock = Callable[[], datetime]


@dataclass(frozen=True)
class SeparateEventResult:
    """The second canonical event a person explicitly asked for."""

    staged: StagedEvent
    rejected_link_ids: tuple[int, ...]


class ReviewSourceLinks:
    """Confirm, reject, or split one proposed pairing, under review control."""

    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        intake: FinancialEventIntake,
        *,
        clock: Clock = lambda: datetime.now(UTC),
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._intake = intake
        self._clock = clock

    def confirm(self, link_id: int) -> FinancialEventSourceLink:
        """Accept one pairing after re-proving it in the same transaction.

        The page the user clicked on may be minutes old. Every mandatory gate
        is therefore recomputed against the rows as they are now, so a source
        that was re-parsed into a different amount, currency, card, or time can
        never be confirmed on the strength of a stale screen.
        """
        with self._unit_of_work_factory() as unit:
            link = self._require_link(unit, link_id)
            if link.status is SourceLinkStatus.CONFIRMED:
                return link
            if link.status is SourceLinkStatus.REJECTED:
                raise SourceLinkError(
                    "this pairing was already rejected; record it separately instead"
                )
            self._require_gates_still_pass(unit, link)
            now = self._clock()
            confirmed = unit.source_links.confirm(
                link.id,
                reviewed_at=now,
                reasons=(SourceLinkReason.USER_CONFIRMED_MATCH,),
            )
            self._reject_siblings(unit, link, reviewed_at=now)
            unit.commit()
        return confirmed

    def reject(self, link_id: int) -> FinancialEventSourceLink:
        """Record that one pairing is not the same money, and keep saying so."""
        with self._unit_of_work_factory() as unit:
            link = self._require_link(unit, link_id)
            if link.status is SourceLinkStatus.REJECTED:
                return link
            if link.status is SourceLinkStatus.CONFIRMED:
                raise SourceLinkError("a confirmed pairing cannot be rejected here")
            rejected = unit.source_links.reject(
                link.id,
                reviewed_at=self._clock(),
                reasons=(SourceLinkReason.USER_REJECTED_MATCH,),
            )
            unit.commit()
        return rejected

    def declare_separate(self, transaction_id: int) -> SeparateEventResult:
        """Record this notification as its own money, not as a duplicate.

        This is the only command that may create a second canonical event for a
        transaction whose candidates were rejected. The event is left needing
        allocation, so the existing review flow decides where it belongs; it is
        deliberately not posted here.
        """
        with self._unit_of_work_factory() as unit:
            now = self._clock()
            rejected: list[int] = []
            for link in unit.source_links.list_for_transaction(transaction_id):
                if link.status is SourceLinkStatus.CONFIRMED:
                    raise SourceLinkError(
                        "this notification already belongs to a recorded event"
                    )
                if link.status is SourceLinkStatus.CANDIDATE:
                    unit.source_links.reject(
                        link.id,
                        reviewed_at=now,
                        reasons=(SourceLinkReason.USER_REJECTED_MATCH,),
                    )
                    rejected.append(link.id)
            staged = self._intake.stage_declared_separate(unit, transaction_id)
            unit.commit()
        return SeparateEventResult(staged=staged, rejected_link_ids=tuple(rejected))

    @staticmethod
    def _require_link(unit: UnitOfWork, link_id: int) -> FinancialEventSourceLink:
        link = unit.source_links.get(link_id)
        if link is None:
            raise SourceLinkError("source link was not found")
        return link

    @staticmethod
    def _reject_siblings(
        unit: UnitOfWork, link: FinancialEventSourceLink, *, reviewed_at: datetime
    ) -> None:
        """Close every other proposal for the same notification.

        One bank notification describes one movement, so once it belongs to an
        event the remaining proposals are answered rather than left waiting.
        """
        for sibling in unit.source_links.list_for_transaction(link.transaction_id):
            if sibling.id != link.id and sibling.status is SourceLinkStatus.CANDIDATE:
                unit.source_links.reject(
                    sibling.id,
                    reviewed_at=reviewed_at,
                    reasons=(SourceLinkReason.USER_REJECTED_MATCH,),
                )

    def _require_gates_still_pass(
        self, unit: UnitOfWork, link: FinancialEventSourceLink
    ) -> None:
        """Recompute every mandatory gate against the current rows."""
        event = unit.financial_events.get(link.financial_event_id)
        transaction = unit.transactions.get(link.transaction_id)
        if event is None or transaction is None:
            raise SourceLinkError("this pairing no longer has both sides")
        email = unit.emails.get(transaction.email_id)
        if (
            email is None
            or email.source_status is not EmailSourceStatus.ACTIVE
            or transaction.source_status is not TransactionSourceStatus.ACTIVE
            or transaction.manually_ignored
            or not transaction.manually_confirmed
        ):
            raise SourceLinkError(
                "this notification is no longer a confirmed source observation"
            )
        if (
            event.origin is not FinancialEventOrigin.MANUAL
            or event.payment_method is not PaymentMethod.CREDIT_CARD
            or event.status is not FinancialEventStatus.POSTED
        ):
            raise SourceLinkError(
                "only a posted, hand-entered card expense can be matched here"
            )
        observation = self._intake.observe_source(unit, transaction, email)
        if observation is None:
            raise SourceLinkError("this notification no longer describes money")
        if (
            payment_method_for_transaction(
                card_last_four=transaction.card_last_four,
                account_last_four=transaction.account_last_four,
            )
            is PaymentMethod.BANK_ACCOUNT
        ):
            # An account debit and a card purchase are different instruments.
            # A notification with no last-four at all is neither: it is a card
            # notification Scribe could not attribute, and a person is exactly
            # the right authority to decide it (ADR-B062).
            raise SourceLinkError(
                "this notification is a bank account debit, not a card purchase"
            )
        score = score_match(
            observation,
            MatchObservation(
                amount_minor=event.amount_minor,
                currency=event.currency,
                event_type=event.event_type,
                occurred_at=event.occurred_at,
                card_id=event.card_id,
                merchant=event.merchant,
            ),
        )
        if not score.eligible:
            raise SourceLinkError(
                "these two observations no longer agree on amount, currency, "
                "kind, card, or time"
            )

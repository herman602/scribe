"""Promote confirmed parsed transactions into canonical financial events.

Intake never posts a journal entry. It creates one canonical event plus one
confirmed provenance link so a later milestone can allocate and post it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from app.domain.budget_enums import (
    FinancialEventOrigin,
    FinancialEventStatus,
    IntakeSkipReason,
    PaymentMethod,
    RecognitionBasis,
    SourceDiscrepancy,
    SourceLinkReason,
    SourceLinkStatus,
)
from app.domain.budget_matching import (
    AUTO_LINK_SCORE_BPS,
    MATCH_TIME_WINDOW,
    MAX_SCORE_BPS,
    MatchObservation,
    MatchScore,
    candidate_link_draft,
    score_match,
)
from app.domain.budget_models import (
    CardIdentity,
    FinancialEvent,
    FinancialEventDraft,
    FinancialEventSourceLink,
    FinancialEventUpdate,
    SourceLinkDraft,
)
from app.domain.budget_rules import (
    event_accepts_source_refresh,
    event_type_for_transaction_type,
    payment_method_for_transaction,
)
from app.domain.enums import EmailSourceStatus, TransactionSourceStatus
from app.domain.errors import IdempotencyConflictError, SourceLinkError
from app.domain.models import EmailMessage, Transaction
from app.domain.ports.repositories import UnitOfWork

type UnitOfWorkFactory = Callable[[], UnitOfWork]
type Clock = Callable[[], datetime]

INTAKE_KEY_PREFIX = "gmail_transaction"


def intake_idempotency_key(transaction_id: int) -> str:
    """Return the stable intake key; Gmail thread IDs never participate."""
    return f"{INTAKE_KEY_PREFIX}:{transaction_id}"


@dataclass(frozen=True)
class StagedEvent:
    """One parsed transaction promoted into a canonical event."""

    transaction_id: int
    event_id: int | None
    card_id: int | None
    reasons: tuple[SourceLinkReason, ...]


@dataclass(frozen=True)
class SkippedTransaction:
    """One parsed transaction deliberately not staged, with a stable code."""

    transaction_id: int
    reason: IntakeSkipReason


@dataclass(frozen=True)
class MatchedTransaction:
    """One parsed transaction recognized as money already recorded by hand.

    Matching creates provenance and nothing else: no second event, no
    allocation, and no journal entry (ADR-B062).
    """

    transaction_id: int
    event_id: int
    score_bps: int


@dataclass(frozen=True)
class PendingTransaction:
    """One parsed transaction whose ownership a person still has to decide.

    A pending transaction never falls through to direct intake on a later run;
    doing so would post the same purchase twice the moment a retry happened.
    """

    transaction_id: int
    candidate_event_ids: tuple[int, ...]


@dataclass(frozen=True)
class IntakeResult:
    """What intake did, or would do, for one explicit set of transactions."""

    staged: tuple[StagedEvent, ...]
    skipped: tuple[SkippedTransaction, ...]
    dry_run: bool
    reconciled: tuple[int, ...] = ()
    matched: tuple[MatchedTransaction, ...] = ()
    pending: tuple[PendingTransaction, ...] = ()

    @property
    def staged_count(self) -> int:
        """Return how many events were created, or would be created."""
        return len(self.staged)

    @property
    def skipped_count(self) -> int:
        """Return how many transactions were deliberately not staged."""
        return len(self.skipped)

    @property
    def matched_count(self) -> int:
        """Return how many transactions were linked to an existing event."""
        return len(self.matched)

    @property
    def pending_count(self) -> int:
        """Return how many transactions are waiting for a review decision."""
        return len(self.pending)

    @property
    def reconciled_count(self) -> int:
        """Return how many already staged events this run reconciled.

        This counts the events an apply run passed through reconciliation, not
        the ones whose fields actually changed. A dry run always reports zero,
        because it reconciles nothing. A precise preview would split this into
        would-reconcile and updated counts.
        """
        return len(self.reconciled)


@dataclass(frozen=True)
class _DerivedEvent:
    update: FinancialEventUpdate
    reasons: tuple[SourceLinkReason, ...]


class FinancialEventIntake:
    """Stage confirmed Gmail transactions and keep their events auditable."""

    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        *,
        clock: Clock = lambda: datetime.now(UTC),
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._clock = clock

    def stage(
        self,
        unit: UnitOfWork,
        transaction_ids: Iterable[int],
        *,
        dry_run: bool = False,
    ) -> IntakeResult:
        """Stage inside a caller's transaction; the caller owns the commit.

        Confirmation and intake therefore share one Unit of Work, so a failure
        in either rolls the whole batch back.
        """
        staged: list[StagedEvent] = []
        skipped: list[SkippedTransaction] = []
        matched: list[MatchedTransaction] = []
        pending: list[PendingTransaction] = []
        reconciled: list[int] = []
        for transaction_id in sorted(set(transaction_ids)):
            outcome = self._stage_one(
                unit, transaction_id, dry_run=dry_run, reconciled=reconciled
            )
            if isinstance(outcome, SkippedTransaction):
                skipped.append(outcome)
            elif isinstance(outcome, MatchedTransaction):
                matched.append(outcome)
            elif isinstance(outcome, PendingTransaction):
                pending.append(outcome)
            else:
                staged.append(outcome)
        return IntakeResult(
            staged=tuple(staged),
            skipped=tuple(skipped),
            dry_run=dry_run,
            reconciled=tuple(reconciled),
            matched=tuple(matched),
            pending=tuple(pending),
        )

    def backfill(self, *, dry_run: bool = True) -> IntakeResult:
        """Stage every already confirmed transaction in one explicit run.

        Apply mode always commits. Staging is not the only write: reconciling
        an already staged event can update the event and its mapping reasons,
        and committing only when something new was staged silently discarded
        that work. Committing an empty transaction is harmless.
        """
        with self._unit_of_work_factory() as unit:
            transaction_ids = unit.transactions.list_confirmed_ids()
            result = self.stage(unit, transaction_ids, dry_run=dry_run)
            if not dry_run:
                unit.commit()
        return result

    def refresh_for_email(self, unit: UnitOfWork, email_id: int) -> None:
        """Reconcile events after a parser rerun changed one email's rows.

        A posted, reversed, or ignored event is never modified; the changed
        source only records stable discrepancy codes for review.
        """
        for link in unit.source_links.list_for_email(email_id):
            if link.status is not SourceLinkStatus.CONFIRMED:
                continue
            event = unit.financial_events.get(link.financial_event_id)
            transaction = unit.transactions.get(link.transaction_id)
            if event is None or transaction is None:  # pragma: no cover
                continue
            email = unit.emails.get(transaction.email_id)
            if email is None:  # pragma: no cover
                continue
            self._reconcile(unit, link, event, transaction, email)

    def _reconcile(
        self,
        unit: UnitOfWork,
        link: FinancialEventSourceLink,
        event: FinancialEvent,
        transaction: Transaction,
        email: EmailMessage,
    ) -> None:
        """Realign one bridged event with its current source observation."""
        if not self._source_still_eligible(transaction, email):
            unit.source_links.record_discrepancies(
                link.id, (SourceDiscrepancy.SOURCE_NO_LONGER_CONFIRMED,)
            )
            return
        derived = self._derive(unit, transaction, email)
        if derived is None:
            unit.source_links.record_discrepancies(
                link.id, (SourceDiscrepancy.SOURCE_NO_LONGER_CONFIRMED,)
            )
            return
        differences = _differences(event, derived.update)
        # A rerun may correct a Gmail-origin event that has not posted yet. It
        # may never rewrite a hand-entered one: the person who typed their own
        # cash is the authority on it, and a bank mail that merely matched it is
        # a second observation of the same money rather than a correction of it.
        refreshable = (
            event.origin is FinancialEventOrigin.GMAIL_TRANSACTION
            and event_accepts_source_refresh(event.status)
        )
        # Mapping reasons describe the mapping as it stands now, so configuring
        # a card later clears card_not_configured, while the provenance decision
        # that created the link is always kept. Discrepancies remain the
        # accumulating audit trail.
        reasons = (*_provenance_reasons(link), *derived.reasons)
        if differences and not refreshable:
            reasons += (SourceLinkReason.SOURCE_CHANGED,)
        unit.source_links.replace_reasons(link.id, reasons)
        if not differences:
            return
        if refreshable:
            unit.financial_events.refresh_source_values(event.id, derived.update)
        else:
            unit.source_links.record_discrepancies(link.id, differences)

    @staticmethod
    def _owning_link(
        unit: UnitOfWork, event: FinancialEvent, transaction_id: int
    ) -> FinancialEventSourceLink:
        """Prove one key already belongs to exactly this Gmail transaction.

        A matching key alone never proves ownership. When origin, recognition
        basis, or the confirmed link disagree, the whole confirming transaction
        is rolled back instead of silently reporting success.
        """
        if (
            event.origin is not FinancialEventOrigin.GMAIL_TRANSACTION
            or event.recognition_basis is not RecognitionBasis.GMAIL_NOTIFICATION
        ):
            raise IdempotencyConflictError(
                "this intake key already records a different kind of event"
            )
        link = unit.source_links.get_confirmed_for_transaction(transaction_id)
        if link is None or link.financial_event_id != event.id:
            raise IdempotencyConflictError(
                "this intake key is not linked to its parsed transaction"
            )
        return link

    def _stage_one(
        self,
        unit: UnitOfWork,
        transaction_id: int,
        *,
        dry_run: bool,
        reconciled: list[int],
    ) -> StagedEvent | SkippedTransaction | MatchedTransaction | PendingTransaction:
        transaction = unit.transactions.get(transaction_id)
        if transaction is None:
            return SkippedTransaction(transaction_id, IntakeSkipReason.NOT_FOUND)
        if transaction.source_status is not TransactionSourceStatus.ACTIVE:
            return SkippedTransaction(transaction_id, IntakeSkipReason.SUPERSEDED)
        if transaction.manually_ignored:
            return SkippedTransaction(transaction_id, IntakeSkipReason.MANUALLY_IGNORED)
        if not transaction.manually_confirmed:
            return SkippedTransaction(transaction_id, IntakeSkipReason.NOT_CONFIRMED)
        email = unit.emails.get(transaction.email_id)
        if email is None or email.source_status is not EmailSourceStatus.ACTIVE:
            return SkippedTransaction(transaction_id, IntakeSkipReason.SOURCE_DELETED)
        missing = _missing_value_reason(transaction)
        if missing is not None:
            return SkippedTransaction(transaction_id, missing)
        key = intake_idempotency_key(transaction_id)
        existing = unit.financial_events.get_by_idempotency_key(key)
        if existing is not None:
            link = self._owning_link(unit, existing, transaction_id)
            if not dry_run:
                self._reconcile(unit, link, existing, transaction, email)
                reconciled.append(transaction_id)
            return SkippedTransaction(transaction_id, IntakeSkipReason.ALREADY_STAGED)
        derived = self._derive(unit, transaction, email)
        if derived is None:  # pragma: no cover - guarded by _missing_value_reason
            return SkippedTransaction(
                transaction_id, IntakeSkipReason.UNSUPPORTED_TRANSACTION_TYPE
            )
        # Matching runs before anything is created (ADR-B062). Creating a
        # Gmail-origin event first and merging afterwards would have posted the
        # same purchase twice and left the merge to guess which entry to undo.
        decided = self._existing_decision(unit, transaction_id)
        if decided is not None:
            return decided
        candidates = self._eligible_candidates(unit, derived)
        automatic = _automatic_match(candidates)
        if automatic is not None:
            event, score = automatic
            if not dry_run:
                unit.source_links.add(
                    SourceLinkDraft(
                        financial_event_id=event.id,
                        transaction_id=transaction_id,
                        status=SourceLinkStatus.CONFIRMED,
                        match_score_bps=score.score_bps,
                        reasons=(SourceLinkReason.AUTO_EXACT_MATCH, *score.reasons),
                        reviewed_at=self._clock(),
                    )
                )
            return MatchedTransaction(
                transaction_id=transaction_id,
                event_id=event.id,
                score_bps=score.score_bps,
            )
        if candidates:
            if not dry_run:
                for event, score in candidates:
                    unit.source_links.upsert_candidate(
                        candidate_link_draft(
                            financial_event_id=event.id,
                            transaction_id=transaction_id,
                            score=score,
                        )
                    )
            return PendingTransaction(
                transaction_id=transaction_id,
                candidate_event_ids=tuple(event.id for event, _ in candidates),
            )
        if dry_run:
            return StagedEvent(
                transaction_id=transaction_id,
                event_id=None,
                card_id=derived.update.card_id,
                reasons=derived.reasons,
            )
        event = unit.financial_events.add(
            FinancialEventDraft(
                origin=FinancialEventOrigin.GMAIL_TRANSACTION,
                event_type=derived.update.event_type,
                payment_method=derived.update.payment_method,
                recognition_basis=RecognitionBasis.GMAIL_NOTIFICATION,
                occurred_at=derived.update.occurred_at,
                amount_minor=derived.update.amount_minor,
                currency=derived.update.currency,
                idempotency_key=key,
                merchant=derived.update.merchant,
                card_id=derived.update.card_id,
                status=FinancialEventStatus.NEEDS_ALLOCATION,
            )
        )
        unit.source_links.add(
            SourceLinkDraft(
                financial_event_id=event.id,
                transaction_id=transaction_id,
                status=SourceLinkStatus.CONFIRMED,
                match_score_bps=MAX_SCORE_BPS,
                reasons=(SourceLinkReason.DIRECT_GMAIL_INTAKE, *derived.reasons),
                reviewed_at=self._clock(),
            )
        )
        return StagedEvent(
            transaction_id=transaction_id,
            event_id=event.id,
            card_id=derived.update.card_id,
            reasons=derived.reasons,
        )

    @staticmethod
    def _existing_decision(
        unit: UnitOfWork, transaction_id: int
    ) -> MatchedTransaction | PendingTransaction | None:
        """Return the decision this transaction already carries, if any.

        A confirmed link to a hand-entered event means the money is already
        recorded. A candidate link means a person still has to decide, and a
        rejected one means they decided these are *not* the same money — which
        does not license creating a second canonical event behind their back.
        Only an explicit "record separately" may do that.
        """
        links = unit.source_links.list_for_transaction(transaction_id)
        if not links:
            return None
        for link in links:
            if link.status is SourceLinkStatus.CONFIRMED:
                event = unit.financial_events.get(link.financial_event_id)
                if event is not None and event.origin is FinancialEventOrigin.MANUAL:
                    return MatchedTransaction(
                        transaction_id=transaction_id,
                        event_id=event.id,
                        score_bps=link.match_score_bps,
                    )
        candidates = tuple(
            link.financial_event_id
            for link in links
            if link.status is SourceLinkStatus.CANDIDATE
        )
        if candidates:
            return PendingTransaction(
                transaction_id=transaction_id, candidate_event_ids=candidates
            )
        if any(link.status is SourceLinkStatus.REJECTED for link in links):
            return PendingTransaction(
                transaction_id=transaction_id, candidate_event_ids=()
            )
        return None

    def observe_source(
        self, unit: UnitOfWork, transaction: Transaction, email: EmailMessage
    ) -> MatchObservation | None:
        """Return the comparable observation one parsed transaction makes.

        Review shares this with intake on purpose: a card identity resolved one
        way at intake and another way at confirmation would let a pairing pass a
        gate it never really passed.
        """
        derived = self._derive(unit, transaction, email)
        if derived is None:
            return None
        return MatchObservation(
            amount_minor=derived.update.amount_minor,
            currency=derived.update.currency,
            event_type=derived.update.event_type,
            occurred_at=derived.update.occurred_at,
            card_id=derived.update.card_id,
            merchant=derived.update.merchant,
        )

    @staticmethod
    def _eligible_candidates(
        unit: UnitOfWork, derived: _DerivedEvent
    ) -> tuple[tuple[FinancialEvent, MatchScore], ...]:
        """Return the posted manual card expenses this source could describe.

        Only an explicit *bank-account* notification is excluded: an account
        debit and a card purchase are different instruments, and pairing them
        would release a card reservation for money that never touched the card.

        A notification that carries no last-four at all is not a bank-account
        notification — it is a card notification whose card Scribe could not
        identify, and it is exactly the case a duplicate would otherwise be
        created for. It is compared, and because neither side can then claim
        `exact_card` it can never reach the auto-link score; it goes to review
        (ADR-B062).
        """
        update = derived.update
        if update.payment_method is PaymentMethod.BANK_ACCOUNT:
            return ()
        source = MatchObservation(
            amount_minor=update.amount_minor,
            currency=update.currency,
            event_type=update.event_type,
            occurred_at=update.occurred_at,
            card_id=update.card_id,
            merchant=update.merchant,
        )
        events = unit.financial_events.list_match_candidates(
            origin=FinancialEventOrigin.MANUAL,
            payment_method=PaymentMethod.CREDIT_CARD,
            status=FinancialEventStatus.POSTED,
            currency=update.currency,
            amount_minor=update.amount_minor,
            window_start=update.occurred_at - MATCH_TIME_WINDOW,
            window_end=update.occurred_at + MATCH_TIME_WINDOW,
        )
        results: list[tuple[FinancialEvent, MatchScore]] = []
        for event in events:
            if any(
                link.status is SourceLinkStatus.CONFIRMED
                for link in unit.source_links.list_for_event(event.id)
            ):
                continue
            score = score_match(
                source,
                MatchObservation(
                    amount_minor=event.amount_minor,
                    currency=event.currency,
                    event_type=event.event_type,
                    occurred_at=event.occurred_at,
                    card_id=event.card_id,
                    merchant=event.merchant,
                ),
            )
            if score.eligible:
                results.append((event, score))
        return tuple(results)

    def stage_declared_separate(
        self, unit: UnitOfWork, transaction_id: int
    ) -> StagedEvent:
        """Create the second canonical event a person explicitly asked for.

        This is the only path that may produce a Gmail-origin event for a
        transaction whose candidates were all rejected: the user has looked at
        both and said they are different money.
        """
        transaction = unit.transactions.get(transaction_id)
        if transaction is None:
            raise SourceLinkError("parsed transaction was not found")
        email = unit.emails.get(transaction.email_id)
        if email is None or not self._source_still_eligible(transaction, email):
            raise SourceLinkError(
                "this transaction is no longer a confirmed source observation"
            )
        key = intake_idempotency_key(transaction_id)
        if unit.financial_events.get_by_idempotency_key(key) is not None:
            raise SourceLinkError("this transaction already has its own event")
        derived = self._derive(unit, transaction, email)
        if derived is None:
            raise SourceLinkError("this transaction cannot become a money event")
        event = unit.financial_events.add(
            FinancialEventDraft(
                origin=FinancialEventOrigin.GMAIL_TRANSACTION,
                event_type=derived.update.event_type,
                payment_method=derived.update.payment_method,
                recognition_basis=RecognitionBasis.GMAIL_NOTIFICATION,
                occurred_at=derived.update.occurred_at,
                amount_minor=derived.update.amount_minor,
                currency=derived.update.currency,
                idempotency_key=key,
                merchant=derived.update.merchant,
                card_id=derived.update.card_id,
                status=FinancialEventStatus.NEEDS_ALLOCATION,
            )
        )
        unit.source_links.add(
            SourceLinkDraft(
                financial_event_id=event.id,
                transaction_id=transaction_id,
                status=SourceLinkStatus.CONFIRMED,
                match_score_bps=MAX_SCORE_BPS,
                reasons=(SourceLinkReason.USER_DECLARED_SEPARATE, *derived.reasons),
                reviewed_at=self._clock(),
            )
        )
        return StagedEvent(
            transaction_id=transaction_id,
            event_id=event.id,
            card_id=derived.update.card_id,
            reasons=derived.reasons,
        )

    @staticmethod
    def _source_still_eligible(transaction: Transaction, email: EmailMessage) -> bool:
        return (
            transaction.source_status is TransactionSourceStatus.ACTIVE
            and transaction.manually_confirmed
            and not transaction.manually_ignored
            and email.source_status is EmailSourceStatus.ACTIVE
        )

    def _derive(
        self, unit: UnitOfWork, transaction: Transaction, email: EmailMessage
    ) -> _DerivedEvent | None:
        if _missing_value_reason(transaction) is not None:
            return None
        event_type = event_type_for_transaction_type(transaction.transaction_type)
        amount_minor = transaction.amount_minor
        currency = transaction.currency
        occurred_at = transaction.transaction_at
        if (
            event_type is None
            or amount_minor is None
            or currency is None
            or occurred_at is None
        ):  # pragma: no cover - guarded by _missing_value_reason
            return None
        card_id, reasons = self._resolve_card(unit, transaction, email)
        return _DerivedEvent(
            update=FinancialEventUpdate(
                event_type=event_type,
                payment_method=payment_method_for_transaction(
                    card_last_four=transaction.card_last_four,
                    account_last_four=transaction.account_last_four,
                ),
                occurred_at=occurred_at,
                amount_minor=amount_minor,
                currency=currency,
                merchant=transaction.merchant,
                card_id=card_id,
            ),
            reasons=reasons,
        )

    @staticmethod
    def _resolve_card(
        unit: UnitOfWork, transaction: Transaction, email: EmailMessage
    ) -> tuple[int | None, tuple[SourceLinkReason, ...]]:
        """Map issuer plus last four to a local card without ever guessing."""
        last_four = transaction.card_last_four
        if last_four is None:
            return None, ()
        rule = (
            unit.sender_rules.get(email.matched_sender_rule_id)
            if email.matched_sender_rule_id is not None
            else None
        )
        if rule is None:
            same_suffix = unit.cards.list_active_by_last_four(
                gmail_account_id=email.gmail_account_id, last_four=last_four
            )
            if same_suffix:
                return None, (SourceLinkReason.CARD_AMBIGUOUS,)
            return None, (SourceLinkReason.CARD_UNRESOLVED,)
        card = unit.cards.get_by_identity(
            CardIdentity(
                gmail_account_id=email.gmail_account_id,
                issuer_code=rule.institution_key,
                last_four=last_four,
            )
        )
        if card is None:
            return None, (SourceLinkReason.CARD_NOT_CONFIGURED,)
        return card.id, ()


#: Reasons that say who decided a link exists, rather than what was compared.
#: They survive every parser rerun: a rerun re-derives evidence, not consent.
_PROVENANCE_REASONS = frozenset(
    {
        SourceLinkReason.DIRECT_GMAIL_INTAKE,
        SourceLinkReason.AUTO_EXACT_MATCH,
        SourceLinkReason.USER_CONFIRMED_MATCH,
        SourceLinkReason.USER_DECLARED_SEPARATE,
    }
)


def _provenance_reasons(
    link: FinancialEventSourceLink,
) -> tuple[SourceLinkReason, ...]:
    """Return the decision codes a rerun must not discard."""
    kept = tuple(reason for reason in link.reasons if reason in _PROVENANCE_REASONS)
    return kept or (SourceLinkReason.DIRECT_GMAIL_INTAKE,)


def _automatic_match(
    candidates: tuple[tuple[FinancialEvent, MatchScore], ...],
) -> tuple[FinancialEvent, MatchScore] | None:
    """Return the one candidate that may link without review, or None.

    Two conditions, both mandatory: exactly one eligible candidate exists at
    all, and it agrees on every piece of evidence there is. A second candidate
    of any strength makes the decision a person's, because linking the wrong
    one silently merges two real purchases into one.
    """
    if len(candidates) != 1:
        return None
    event, score = candidates[0]
    if not score.eligible or score.score_bps != AUTO_LINK_SCORE_BPS:
        return None
    return event, score


def _missing_value_reason(transaction: Transaction) -> IntakeSkipReason | None:
    """Return why one transaction cannot become money, without guessing."""
    if event_type_for_transaction_type(transaction.transaction_type) is None:
        return IntakeSkipReason.UNSUPPORTED_TRANSACTION_TYPE
    if transaction.amount_minor is None:
        return IntakeSkipReason.MISSING_AMOUNT
    if transaction.amount_minor <= 0:
        return IntakeSkipReason.NON_POSITIVE_AMOUNT
    if transaction.currency is None:
        return IntakeSkipReason.MISSING_CURRENCY
    if transaction.transaction_at is None:
        return IntakeSkipReason.MISSING_OCCURRED_AT
    return None


def _differences(
    event: FinancialEvent, update: FinancialEventUpdate
) -> tuple[SourceDiscrepancy, ...]:
    """Return stable codes naming each field whose observation changed."""
    changed: list[SourceDiscrepancy] = []
    if event.amount_minor != update.amount_minor:
        changed.append(SourceDiscrepancy.AMOUNT)
    if event.currency != update.currency:
        changed.append(SourceDiscrepancy.CURRENCY)
    if event.merchant != update.merchant:
        changed.append(SourceDiscrepancy.MERCHANT)
    if event.occurred_at != update.occurred_at:
        changed.append(SourceDiscrepancy.OCCURRED_AT)
    if event.event_type is not update.event_type:
        changed.append(SourceDiscrepancy.EVENT_TYPE)
    if event.card_id != update.card_id:
        changed.append(SourceDiscrepancy.CARD)
    return tuple(changed)

"""Repository behaviour B7 depends on: snapshots, decisions, and reversal."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app.domain.budget_enums import (
    BalanceSnapshotSource,
    FinancialAccountType,
    FinancialEventOrigin,
    FinancialEventStatus,
    FinancialEventType,
    PaymentMethod,
    RecognitionBasis,
    SourceLinkReason,
    SourceLinkStatus,
)
from app.domain.budget_models import (
    BalanceSnapshotDraft,
    FinancialAccountDraft,
    FinancialEventDraft,
    SourceLinkDraft,
)
from app.domain.enums import TransactionType
from app.domain.errors import (
    ConcurrentUpdateError,
    FinancialEventError,
    SourceLinkError,
)
from app.domain.models import EmailSnapshot, TransactionDraft
from app.domain.ports.repositories import UnitOfWork
from app.infrastructure.database.engine import Database
from app.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork

OBSERVED_AT = datetime(2026, 8, 9, 4, 30, tzinfo=UTC)

type UnitOfWorkFactory = Callable[[], UnitOfWork]


@pytest.fixture
def factory(migrated_database: Database) -> UnitOfWorkFactory:
    def build() -> UnitOfWork:
        return SqlAlchemyUnitOfWork(migrated_database.session_factory)

    return build


def _account(factory: UnitOfWorkFactory) -> int:
    with factory() as unit:
        account = unit.financial_accounts.add(
            FinancialAccountDraft(
                institution_code="test_bank",
                display_name="測試帳戶",
                account_type=FinancialAccountType.CHECKING,
                currency="TWD",
            )
        )
        unit.commit()
        return account.id


def _event(
    factory: UnitOfWorkFactory,
    *,
    key: str,
    amount_minor: int = 45_000,
    occurred_at: datetime = OBSERVED_AT,
    status: FinancialEventStatus = FinancialEventStatus.POSTED,
    origin: FinancialEventOrigin = FinancialEventOrigin.MANUAL,
    payment_method: PaymentMethod = PaymentMethod.CREDIT_CARD,
) -> int:
    with factory() as unit:
        event = unit.financial_events.add(
            FinancialEventDraft(
                origin=origin,
                event_type=FinancialEventType.PURCHASE,
                payment_method=payment_method,
                recognition_basis=RecognitionBasis.OBSERVED_MANUAL,
                occurred_at=occurred_at,
                amount_minor=amount_minor,
                currency="TWD",
                idempotency_key=key,
                merchant="測試商店",
                status=status,
            )
        )
        unit.commit()
        return event.id


class TestBalanceSnapshots:
    """Observations are written once and only read afterwards."""

    def test_a_snapshot_round_trips_with_its_source_and_note(
        self, factory: UnitOfWorkFactory
    ) -> None:
        account_id = _account(factory)

        with factory() as unit:
            snapshot = unit.balance_snapshots.add(
                BalanceSnapshotDraft(
                    financial_account_id=account_id,
                    observed_balance_minor=-1_500,
                    observed_at=OBSERVED_AT,
                    idempotency_key="balance_snapshot:one",
                    note="  透支  ",
                )
            )
            unit.commit()
            snapshot_id = snapshot.id

        with factory() as unit:
            stored = unit.balance_snapshots.get(snapshot_id)
            assert stored is not None
            assert stored.observed_balance_minor == -1_500
            assert stored.source is BalanceSnapshotSource.MANUAL_BANK_APP
            assert stored.note == "透支"
            assert stored.observed_at == OBSERVED_AT

    def test_the_repository_offers_no_way_to_change_one(
        self, factory: UnitOfWorkFactory
    ) -> None:
        with factory() as unit:
            repository = unit.balance_snapshots
        for forbidden in ("save", "update", "delete", "remove"):
            assert not hasattr(repository, forbidden)

    def test_a_key_finds_the_observation_it_recorded(
        self, factory: UnitOfWorkFactory
    ) -> None:
        account_id = _account(factory)
        with factory() as unit:
            unit.balance_snapshots.add(
                BalanceSnapshotDraft(
                    financial_account_id=account_id,
                    observed_balance_minor=0,
                    observed_at=OBSERVED_AT,
                    idempotency_key="balance_snapshot:one",
                )
            )
            unit.commit()

        with factory() as unit:
            assert (
                unit.balance_snapshots.get_by_idempotency_key("balance_snapshot:one")
                is not None
            )
            assert unit.balance_snapshots.get_by_idempotency_key("missing") is None

    def test_the_list_is_newest_first_and_bounded(
        self, factory: UnitOfWorkFactory
    ) -> None:
        account_id = _account(factory)
        with factory() as unit:
            for index in range(3):
                unit.balance_snapshots.add(
                    BalanceSnapshotDraft(
                        financial_account_id=account_id,
                        observed_balance_minor=index,
                        observed_at=OBSERVED_AT + timedelta(hours=index),
                        idempotency_key=f"balance_snapshot:{index}",
                    )
                )
            unit.commit()

        with factory() as unit:
            rows = unit.balance_snapshots.list_for_account(account_id, limit=2)
            latest = unit.balance_snapshots.latest_for_account(account_id)
        assert [row.observed_balance_minor for row in rows] == [2, 1]
        assert latest is not None
        assert latest.observed_balance_minor == 2

    def test_the_as_of_balance_ignores_later_entries(
        self, factory: UnitOfWorkFactory
    ) -> None:
        account_id = _account(factory)

        with factory() as unit:
            assert (
                unit.reconciliation_ledger.physical_balance_as_of(
                    financial_account_id=account_id, as_of=OBSERVED_AT
                )
                == 0
            )


class TestConditionalEventTransitions:
    """A reversal may only happen once, and only from posted."""

    def test_mark_reversed_moves_a_posted_event_exactly_once(
        self, factory: UnitOfWorkFactory
    ) -> None:
        event_id = _event(factory, key="one")

        with factory() as unit:
            event = unit.financial_events.get(event_id)
            assert event is not None
            reversed_event = unit.financial_events.mark_reversed(
                event_id, event.version
            )
            unit.commit()

        assert reversed_event.status is FinancialEventStatus.REVERSED
        with (
            factory() as unit,
            pytest.raises(FinancialEventError, match="already reversed"),
        ):
            unit.financial_events.mark_reversed(event_id, reversed_event.version)

    def test_a_stale_version_is_a_concurrency_error(
        self, factory: UnitOfWorkFactory
    ) -> None:
        event_id = _event(factory, key="one")

        with factory() as unit, pytest.raises(ConcurrentUpdateError):
            unit.financial_events.mark_reversed(event_id, 99)

    def test_a_draft_event_has_nothing_to_reverse(
        self, factory: UnitOfWorkFactory
    ) -> None:
        event_id = _event(
            factory, key="one", status=FinancialEventStatus.NEEDS_ALLOCATION
        )

        with factory() as unit, pytest.raises(FinancialEventError):
            unit.financial_events.mark_reversed(event_id, 1)


class TestMatchCandidates:
    """The candidate query gates on amount, currency, and a bounded window."""

    def test_only_exact_amounts_inside_the_window_are_returned(
        self, factory: UnitOfWorkFactory
    ) -> None:
        wanted = _event(factory, key="wanted")
        _event(factory, key="other-amount", amount_minor=45_001)
        _event(
            factory,
            key="too-old",
            occurred_at=OBSERVED_AT - timedelta(hours=100),
        )
        _event(factory, key="not-posted", status=FinancialEventStatus.READY)
        _event(factory, key="gmail", origin=FinancialEventOrigin.GMAIL_TRANSACTION)
        _event(factory, key="cash", payment_method=PaymentMethod.CASH)

        with factory() as unit:
            found = unit.financial_events.list_match_candidates(
                origin=FinancialEventOrigin.MANUAL,
                payment_method=PaymentMethod.CREDIT_CARD,
                status=FinancialEventStatus.POSTED,
                currency="TWD",
                amount_minor=45_000,
                window_start=OBSERVED_AT - timedelta(hours=72),
                window_end=OBSERVED_AT + timedelta(hours=72),
            )

        assert [event.id for event in found] == [wanted]


def _transaction(factory: UnitOfWorkFactory) -> int:
    """Create one parsed transaction a provenance link may point at."""
    with factory() as unit:
        account = unit.accounts.get_or_create("user@example.test")
        outcome = unit.emails.upsert_from_gmail(
            EmailSnapshot(
                gmail_account_id=account.id,
                gmail_message_id="gmail-repo-1",
                gmail_thread_id="thread-repo",
                gmail_history_id="1",
                sender="notice@example-bank.test",
                recipients=("user@example.test",),
                subject="消費通知",
                gmail_internal_date_ms=1_754_000_000_000,
            )
        )
        transaction = unit.transactions.add(
            TransactionDraft(
                email_id=outcome.email.id,
                source_key="row-1",
                transaction_type=TransactionType.PURCHASE,
                confidence=Decimal("0.95"),
                parser_name="tests.fake",
                parser_version="1",
                merchant="測試商店",
                amount_minor=45_000,
                currency="TWD",
                card_last_four="4321",
                transaction_at=OBSERVED_AT,
            )
        )
        unit.commit()
        return transaction.id


class TestSourceLinkDecisions:
    """Candidates are upserted; a decision is never silently reopened."""

    def _candidate(
        self, factory: UnitOfWorkFactory, *, event_id: int, transaction_id: int
    ) -> int:
        with factory() as unit:
            link = unit.source_links.upsert_candidate(
                SourceLinkDraft(
                    financial_event_id=event_id,
                    transaction_id=transaction_id,
                    status=SourceLinkStatus.CANDIDATE,
                    match_score_bps=5_000,
                    reasons=(SourceLinkReason.COMPATIBLE_EVENT_TYPE,),
                )
            )
            unit.commit()
            return link.id

    def test_only_a_candidate_may_be_upserted(self, factory: UnitOfWorkFactory) -> None:
        event_id = _event(factory, key="one")

        with factory() as unit, pytest.raises(SourceLinkError):
            unit.source_links.upsert_candidate(
                SourceLinkDraft(
                    financial_event_id=event_id,
                    transaction_id=1,
                    status=SourceLinkStatus.CONFIRMED,
                    match_score_bps=10_000,
                )
            )

    def test_a_missing_link_is_reported(self, factory: UnitOfWorkFactory) -> None:
        with factory() as unit:
            assert unit.source_links.get(999) is None
            with pytest.raises(SourceLinkError):
                unit.source_links.confirm(999, reviewed_at=OBSERVED_AT, reasons=())

    def test_upserting_the_same_candidate_twice_keeps_one_row(
        self, factory: UnitOfWorkFactory
    ) -> None:
        """A retry refreshes the proposal rather than inserting a second one."""
        event_id = _event(factory, key="one")
        transaction_id = _transaction(factory)
        first = self._candidate(
            factory, event_id=event_id, transaction_id=transaction_id
        )

        with factory() as unit:
            again = unit.source_links.upsert_candidate(
                SourceLinkDraft(
                    financial_event_id=event_id,
                    transaction_id=transaction_id,
                    status=SourceLinkStatus.CANDIDATE,
                    match_score_bps=7_500,
                    reasons=(SourceLinkReason.EXACT_MERCHANT,),
                )
            )
            unit.commit()
            rows = unit.source_links.list_for_transaction(transaction_id)

        assert again.id == first
        assert again.match_score_bps == 7_500
        assert len(rows) == 1

    def test_a_decided_pairing_is_never_reopened_by_an_upsert(
        self, factory: UnitOfWorkFactory
    ) -> None:
        """Only an explicit review decision may change a decided link."""
        event_id = _event(factory, key="one")
        transaction_id = _transaction(factory)
        link_id = self._candidate(
            factory, event_id=event_id, transaction_id=transaction_id
        )
        with factory() as unit:
            unit.source_links.reject(
                link_id,
                reviewed_at=OBSERVED_AT,
                reasons=(SourceLinkReason.USER_REJECTED_MATCH,),
            )
            unit.commit()

        with factory() as unit:
            again = unit.source_links.upsert_candidate(
                SourceLinkDraft(
                    financial_event_id=event_id,
                    transaction_id=transaction_id,
                    status=SourceLinkStatus.CANDIDATE,
                    match_score_bps=10_000,
                    reasons=(SourceLinkReason.EXACT_MERCHANT,),
                )
            )
            unit.commit()
            rows = unit.source_links.list_for_transaction(transaction_id)

        assert again.status is SourceLinkStatus.REJECTED
        assert again.match_score_bps != 10_000
        assert SourceLinkReason.USER_REJECTED_MATCH in again.reasons
        assert len(rows) == 1

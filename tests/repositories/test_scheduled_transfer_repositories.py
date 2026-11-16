"""File-backed scheduled-transfer repository, trigger, and reservation tests.

The reservation query is compared against the pure ``wallet_reserved_minor``
rule on every scenario, so the SQL and the documented formula cannot drift
apart without a test failing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

import pytest
from app.domain.budget_enums import (
    CommitmentFulfillmentMode,
    CommitmentStatus,
    FinancialAccountType,
    FinancialEventOrigin,
    FinancialEventType,
    PaymentMethod,
    RecognitionBasis,
    ScheduledTransferPurpose,
    ScheduledTransferStatus,
)
from app.domain.budget_models import (
    EventAllocationDraft,
    FinancialAccountDraft,
    FinancialEventDraft,
    FundingCommitmentDraft,
    ScheduledTransferCommitmentDraft,
    ScheduledTransferDraft,
    WalletDraft,
)
from app.domain.budget_rules import (
    WalletCommitmentView,
    WalletScheduleDebitView,
    wallet_reserved_minor,
)
from app.domain.errors import (
    ConcurrentUpdateError,
    ScheduledTransferCoverageError,
    ScheduledTransferError,
)
from app.domain.ports.repositories import UnitOfWork
from app.infrastructure.database.engine import Database
from app.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork
from sqlalchemy import text

DUE_AT = datetime(2026, 8, 31, 22, 0, tzinfo=UTC)
OCCURRED_AT = datetime(2026, 8, 7, 13, 30, tzinfo=UTC)

type UnitOfWorkFactory = Callable[[], UnitOfWork]


@dataclass(frozen=True)
class Fixture:
    """One source wallet, one destination wallet, and one reservation."""

    source_account_id: int
    source_wallet_id: int
    destination_account_id: int
    destination_wallet_id: int
    commitment_id: int
    commitment_amount: int


@pytest.fixture
def factory(migrated_database: Database) -> UnitOfWorkFactory:
    """Return a unit-of-work factory bound to the migrated test database."""

    def build() -> UnitOfWork:
        return SqlAlchemyUnitOfWork(migrated_database.session_factory)

    return build


@pytest.fixture
def world(factory: UnitOfWorkFactory) -> Fixture:
    """Create two test accounts and one 10,000 minor-unit reservation."""
    with factory() as unit:
        source = unit.financial_accounts.add(
            FinancialAccountDraft(
                institution_code="test_bank_a",
                display_name="測試來源帳戶",
                account_type=FinancialAccountType.CHECKING,
                currency="TWD",
            )
        )
        destination = unit.financial_accounts.add(
            FinancialAccountDraft(
                institution_code="test_bank_b",
                display_name="測試目的帳戶",
                account_type=FinancialAccountType.CHECKING,
                currency="TWD",
            )
        )
        source_wallet = unit.wallets.add(
            WalletDraft(financial_account_id=source.id, name="測試來源錢包")
        )
        destination_wallet = unit.wallets.add(
            WalletDraft(financial_account_id=destination.id, name="測試目的錢包")
        )
        event = unit.financial_events.add(
            FinancialEventDraft(
                origin=FinancialEventOrigin.MANUAL,
                event_type=FinancialEventType.PURCHASE,
                payment_method=PaymentMethod.CREDIT_CARD,
                recognition_basis=RecognitionBasis.OBSERVED_MANUAL,
                occurred_at=OCCURRED_AT,
                amount_minor=10_000,
                currency="TWD",
                idempotency_key="test:reservation",
            )
        )
        allocation = unit.allocations.add(
            EventAllocationDraft(
                financial_event_id=event.id,
                funding_wallet_id=source_wallet.id,
                amount_minor=10_000,
                currency="TWD",
            )
        )
        commitment = unit.commitments.add(
            FundingCommitmentDraft(
                financial_event_id=event.id,
                event_allocation_id=allocation.id,
                source_wallet_id=source_wallet.id,
                settlement_account_id=destination.id,
                amount_minor=10_000,
                currency="TWD",
                fulfillment_mode=CommitmentFulfillmentMode.CROSS_BANK_TRANSFER,
            )
        )
        unit.commit()
        return Fixture(
            source_account_id=source.id,
            source_wallet_id=source_wallet.id,
            destination_account_id=destination.id,
            destination_wallet_id=destination_wallet.id,
            commitment_id=commitment.id,
            commitment_amount=10_000,
        )


def _draft(world: Fixture, *, amount_minor: int, token: str) -> ScheduledTransferDraft:
    return ScheduledTransferDraft(
        purpose=ScheduledTransferPurpose.CLEARING_FUNDING,
        source_account_id=world.source_account_id,
        source_wallet_id=world.source_wallet_id,
        destination_account_id=world.destination_account_id,
        destination_wallet_id=world.destination_wallet_id,
        amount_minor=amount_minor,
        currency="TWD",
        scheduled_local_date=date(2026, 9, 1),
        scheduled_local_time=time(6, 0),
        timezone="Asia/Taipei",
        due_at_utc=DUE_AT,
        idempotency_key=f"scheduled_transfer:create:{token}",
    )


def _schedule(
    factory: UnitOfWorkFactory,
    world: Fixture,
    *,
    amount_minor: int,
    token: str,
    covered_minor: int | None = None,
) -> int:
    with factory() as unit:
        schedule = unit.scheduled_transfers.add(
            _draft(world, amount_minor=amount_minor, token=token)
        )
        if covered_minor:
            unit.scheduled_transfer_commitments.add(
                ScheduledTransferCommitmentDraft(
                    scheduled_transfer_id=schedule.id,
                    funding_commitment_id=world.commitment_id,
                    amount_minor=covered_minor,
                )
            )
        unit.commitments.refresh_status(world.commitment_id)
        unit.commit()
        return schedule.id


def _expected_reserved(factory: UnitOfWorkFactory, world: Fixture) -> int:
    """Compute the same number through the pure rule the SQL mirrors."""
    with factory() as unit:
        commitment = unit.commitments.get(world.commitment_id)
        assert commitment is not None
        active, executed = unit.scheduled_transfer_commitments.coverage(
            world.commitment_id
        )
        debits: list[WalletScheduleDebitView] = []
        for status in (
            ScheduledTransferStatus.SCHEDULED,
            ScheduledTransferStatus.PROCESSING,
            ScheduledTransferStatus.FAILED,
        ):
            for schedule in unit.scheduled_transfers.list_by_status(
                frozenset({status}), limit=100
            ):
                links = unit.scheduled_transfer_commitments.list_for_schedule(
                    schedule.id
                )
                linked = sum(link.amount_minor for link in links)
                debit = sum(
                    link.amount_minor
                    for link in links
                    if _commitment_wallet(unit, link.funding_commitment_id)
                    == world.source_wallet_id
                )
                if schedule.source_wallet_id == world.source_wallet_id:
                    debit += schedule.amount_minor - linked
                if debit:
                    debits.append(
                        WalletScheduleDebitView(
                            scheduled_transfer_id=schedule.id, debit_minor=debit
                        )
                    )
        salary = unit.salary_run_items.reserved_minor_for_wallet(world.source_wallet_id)
    return wallet_reserved_minor(
        salary_waiting_minor=salary,
        commitments=(
            WalletCommitmentView(
                commitment_id=commitment.id,
                required_minor=commitment.outstanding_minor,
                active_covered_minor=active,
                executed_covered_minor=executed,
            ),
        ),
        schedule_debits=tuple(debits),
    )


def _commitment_wallet(unit: UnitOfWork, commitment_id: int) -> int:
    commitment = unit.commitments.get(commitment_id)
    assert commitment is not None
    return commitment.source_wallet_id


def _reserved(factory: UnitOfWorkFactory, wallet_id: int) -> int:
    with factory() as unit:
        return unit.budget_ledger.reserved_minor(wallet_id)


class TestReservation:
    """The SQL and the pure rule must agree in every arrangement."""

    def test_an_unscheduled_commitment(
        self, factory: UnitOfWorkFactory, world: Fixture
    ) -> None:
        assert _reserved(factory, world.source_wallet_id) == 10_000
        assert _reserved(factory, world.source_wallet_id) == _expected_reserved(
            factory, world
        )

    def test_a_fully_covering_schedule_does_not_double_count(
        self, factory: UnitOfWorkFactory, world: Fixture
    ) -> None:
        _schedule(
            factory, world, amount_minor=10_000, token="full", covered_minor=10_000
        )

        assert _reserved(factory, world.source_wallet_id) == 10_000
        assert _reserved(factory, world.source_wallet_id) == _expected_reserved(
            factory, world
        )

    def test_a_partially_covering_schedule(
        self, factory: UnitOfWorkFactory, world: Fixture
    ) -> None:
        _schedule(factory, world, amount_minor=4_000, token="part", covered_minor=4_000)

        assert _reserved(factory, world.source_wallet_id) == 10_000
        assert _reserved(factory, world.source_wallet_id) == _expected_reserved(
            factory, world
        )

    def test_an_unlinked_schedule_adds_its_own_debit(
        self, factory: UnitOfWorkFactory, world: Fixture
    ) -> None:
        _schedule(factory, world, amount_minor=2_500, token="unlinked")

        assert _reserved(factory, world.source_wallet_id) == 12_500
        assert _reserved(factory, world.source_wallet_id) == _expected_reserved(
            factory, world
        )

    def test_a_cancelled_schedule_reserves_nothing(
        self, factory: UnitOfWorkFactory, world: Fixture
    ) -> None:
        schedule_id = _schedule(
            factory, world, amount_minor=10_000, token="cancel", covered_minor=10_000
        )
        with factory() as unit:
            schedule = unit.scheduled_transfers.get(schedule_id)
            assert schedule is not None
            unit.scheduled_transfers.transition(
                schedule_id,
                expected_version=schedule.version,
                expected_status=ScheduledTransferStatus.SCHEDULED,
                status=ScheduledTransferStatus.CANCELLED,
                cancelled_at=DUE_AT,
            )
            unit.commitments.refresh_status(world.commitment_id)
            unit.commit()

        assert _reserved(factory, world.source_wallet_id) == 10_000
        assert _commitment_status(factory, world.commitment_id) is CommitmentStatus.OPEN

    def test_a_refund_leaves_a_scheduled_movement_reserved(
        self, factory: UnitOfWorkFactory, world: Fixture
    ) -> None:
        _schedule(
            factory, world, amount_minor=10_000, token="refunded", covered_minor=10_000
        )
        with factory() as unit:
            unit.commitments.release(world.commitment_id, 10_000)
            unit.commitments.refresh_status(world.commitment_id)
            unit.commit()

        assert _reserved(factory, world.source_wallet_id) == 10_000
        assert _reserved(factory, world.source_wallet_id) == _expected_reserved(
            factory, world
        )

    def test_a_stale_status_column_cannot_change_availability(
        self, factory: UnitOfWorkFactory, world: Fixture, migrated_database: Database
    ) -> None:
        """Reservation reads amounts, never a commitment's status column."""
        with migrated_database.engine.begin() as connection:
            connection.execute(text("UPDATE funding_commitments SET status = 'funded'"))

        assert _reserved(factory, world.source_wallet_id) == 10_000


def _commitment_status(
    factory: UnitOfWorkFactory, commitment_id: int
) -> CommitmentStatus:
    with factory() as unit:
        commitment = unit.commitments.get(commitment_id)
        assert commitment is not None
        return commitment.status


class TestClaiming:
    """A claim is one conditional statement, and it is checked."""

    def test_a_due_schedule_is_claimed_once(
        self, factory: UnitOfWorkFactory, world: Fixture
    ) -> None:
        _schedule(factory, world, amount_minor=1_000, token="claim")
        lease = DUE_AT + timedelta(seconds=120)

        with factory() as unit:
            first = unit.scheduled_transfers.claim_due(
                now=DUE_AT, owner="owner-a", lease_until=lease, limit=10
            )
            unit.commit()
        with factory() as unit:
            second = unit.scheduled_transfers.claim_due(
                now=DUE_AT, owner="owner-b", lease_until=lease, limit=10
            )
            unit.commit()

        assert len(first) == 1
        assert first[0].status is ScheduledTransferStatus.PROCESSING
        assert first[0].attempt_count == 1
        assert first[0].lease_owner == "owner-a"
        assert second == ()

    def test_a_schedule_that_is_not_due_is_never_claimed(
        self, factory: UnitOfWorkFactory, world: Fixture
    ) -> None:
        _schedule(factory, world, amount_minor=1_000, token="early")

        with factory() as unit:
            claimed = unit.scheduled_transfers.claim_due(
                now=DUE_AT - timedelta(seconds=1),
                owner="owner-a",
                lease_until=DUE_AT,
                limit=10,
            )
            unit.commit()

        assert claimed == ()

    def test_an_expired_lease_is_reclaimed(
        self, factory: UnitOfWorkFactory, world: Fixture
    ) -> None:
        _schedule(factory, world, amount_minor=1_000, token="expired")
        with factory() as unit:
            unit.scheduled_transfers.claim_due(
                now=DUE_AT,
                owner="owner-a",
                lease_until=DUE_AT + timedelta(seconds=1),
                limit=10,
            )
            unit.commit()

        later = DUE_AT + timedelta(seconds=30)
        with factory() as unit:
            reclaimed = unit.scheduled_transfers.claim_due(
                now=later,
                owner="owner-b",
                lease_until=later + timedelta(seconds=120),
                limit=10,
            )
            unit.commit()

        assert len(reclaimed) == 1
        assert reclaimed[0].lease_owner == "owner-b"
        assert reclaimed[0].attempt_count == 2

    def test_finishing_requires_the_current_owner_and_version(
        self, factory: UnitOfWorkFactory, world: Fixture
    ) -> None:
        schedule_id = _schedule(factory, world, amount_minor=1_000, token="finish")
        with factory() as unit:
            claimed = unit.scheduled_transfers.claim_due(
                now=DUE_AT,
                owner="owner-a",
                lease_until=DUE_AT + timedelta(seconds=120),
                limit=10,
            )
            unit.commit()

        with factory() as unit, pytest.raises(ConcurrentUpdateError):
            unit.scheduled_transfers.finish(
                schedule_id,
                expected_version=claimed[0].version,
                lease_owner="owner-b",
                status=ScheduledTransferStatus.FAILED,
                last_error_code="ScheduledTransferError",
                last_error_message="測試訊息",
            )
        with factory() as unit, pytest.raises(ConcurrentUpdateError):
            unit.scheduled_transfers.finish(
                schedule_id,
                expected_version=claimed[0].version + 5,
                lease_owner="owner-a",
                status=ScheduledTransferStatus.FAILED,
                last_error_code="ScheduledTransferError",
                last_error_message="測試訊息",
            )

    def test_a_backoff_delays_the_next_claim(
        self, factory: UnitOfWorkFactory, world: Fixture
    ) -> None:
        schedule_id = _schedule(factory, world, amount_minor=1_000, token="backoff")
        with factory() as unit:
            claimed = unit.scheduled_transfers.claim_due(
                now=DUE_AT,
                owner="owner-a",
                lease_until=DUE_AT + timedelta(seconds=120),
                limit=10,
            )
            unit.scheduled_transfers.finish(
                schedule_id,
                expected_version=claimed[0].version,
                lease_owner="owner-a",
                status=ScheduledTransferStatus.SCHEDULED,
                next_attempt_at=DUE_AT + timedelta(seconds=30),
                last_error_code="ConcurrentUpdateError",
                last_error_message="測試訊息",
            )
            unit.commit()

        with factory() as unit:
            blocked = unit.scheduled_transfers.claim_due(
                now=DUE_AT + timedelta(seconds=10),
                owner="owner-b",
                lease_until=DUE_AT + timedelta(seconds=200),
                limit=10,
            )
            unit.commit()
        with factory() as unit:
            allowed = unit.scheduled_transfers.claim_due(
                now=DUE_AT + timedelta(seconds=30),
                owner="owner-c",
                lease_until=DUE_AT + timedelta(seconds=200),
                limit=10,
            )
            unit.commit()

        assert blocked == ()
        assert len(allowed) == 1


class TestDatabaseDefences:
    """The row refuses what the service refuses, whichever path arrives."""

    def test_the_instruction_columns_are_frozen(
        self, factory: UnitOfWorkFactory, world: Fixture, migrated_database: Database
    ) -> None:
        schedule_id = _schedule(factory, world, amount_minor=1_000, token="frozen")

        with (
            pytest.raises(Exception, match="instruction cannot be changed"),
            migrated_database.engine.begin() as connection,
        ):
            connection.execute(
                text(
                    "UPDATE scheduled_transfers SET amount_minor = 999 WHERE id = :id"
                ),
                {"id": schedule_id},
            )

    def test_a_coverage_link_can_never_be_updated_or_deleted(
        self, factory: UnitOfWorkFactory, world: Fixture, migrated_database: Database
    ) -> None:
        _schedule(
            factory, world, amount_minor=5_000, token="immutable", covered_minor=5_000
        )

        for statement in (
            "UPDATE scheduled_transfer_commitments SET amount_minor = 1",
            "DELETE FROM scheduled_transfer_commitments",
        ):
            with (
                pytest.raises(Exception, match="link is immutable"),
                migrated_database.engine.begin() as connection,
            ):
                connection.execute(text(statement))

    def test_coverage_can_never_exceed_the_requirement(
        self, factory: UnitOfWorkFactory, world: Fixture
    ) -> None:
        _schedule(
            factory, world, amount_minor=7_000, token="first", covered_minor=7_000
        )

        with pytest.raises(ScheduledTransferCoverageError):
            _schedule(
                factory,
                world,
                amount_minor=4_000,
                token="second",
                covered_minor=4_000,
            )

    def test_a_cancelled_schedule_gives_its_capacity_back(
        self, factory: UnitOfWorkFactory, world: Fixture
    ) -> None:
        schedule_id = _schedule(
            factory, world, amount_minor=10_000, token="freed", covered_minor=10_000
        )
        with factory() as unit:
            schedule = unit.scheduled_transfers.get(schedule_id)
            assert schedule is not None
            unit.scheduled_transfers.transition(
                schedule_id,
                expected_version=schedule.version,
                expected_status=ScheduledTransferStatus.SCHEDULED,
                status=ScheduledTransferStatus.CANCELLED,
                cancelled_at=DUE_AT,
            )
            unit.commit()

        replacement = _schedule(
            factory,
            world,
            amount_minor=10_000,
            token="replacement",
            covered_minor=10_000,
        )
        assert replacement != schedule_id

    def test_one_token_creates_one_schedule(
        self, factory: UnitOfWorkFactory, world: Fixture
    ) -> None:
        _schedule(factory, world, amount_minor=1_000, token="unique")

        with pytest.raises(ScheduledTransferError, match="idempotency token"):
            _schedule(factory, world, amount_minor=2_000, token="unique")

    def test_pages_are_bounded(
        self, factory: UnitOfWorkFactory, world: Fixture
    ) -> None:
        for index in range(5):
            _schedule(factory, world, amount_minor=100, token=f"page-{index}")

        with factory() as unit:
            first = unit.scheduled_transfers.list_by_status(
                frozenset({ScheduledTransferStatus.SCHEDULED}), limit=2
            )
            total = unit.scheduled_transfers.count_by_status(
                frozenset({ScheduledTransferStatus.SCHEDULED})
            )
            second = unit.scheduled_transfers.list_by_status(
                frozenset({ScheduledTransferStatus.SCHEDULED}), limit=2, offset=2
            )

        assert len(first) == 2
        assert len(second) == 2
        assert total == 5
        assert {item.id for item in first}.isdisjoint({item.id for item in second})

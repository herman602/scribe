"""Opening cutover protects earlier events from a second accounting effect."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.application.services.event_allocation import (
    AllocateFinancialEventRequest,
    AllocationLine,
)
from app.application.services.ledger import JournalService
from app.domain.budget_enums import (
    FinancialEventOrigin,
    FinancialEventStatus,
    FinancialEventType,
    JournalEntryType,
    PaymentMethod,
    RecognitionBasis,
)
from app.domain.budget_models import (
    FinancialEvent,
    FinancialEventDraft,
    Money,
    PostingDraft,
)
from app.domain.errors import PreOpeningEventError
from app.infrastructure.database.budget_models import (
    EventAllocationModel,
    FundingCommitmentModel,
    JournalEntryModel,
)
from app.infrastructure.database.budget_queries import (
    SqlAlchemyBudgetReadRepository,
    SqlAlchemyMoneyReviewReadRepository,
)
from app.infrastructure.database.engine import Database
from sqlalchemy import func, select

from tests.b7_support import B7Services, B7World

CUTOFF = datetime(2026, 8, 14, 15, 10, tzinfo=UTC)


def _apply_cutover(b7: B7Services) -> None:
    with b7.factory() as unit:
        unit.import_batches.add(
            source_kind="test_sheet_readonly",
            format_version=1,
            content_sha256="a" * 64,
            source_locator_hash="b" * 64,
            observed_at=CUTOFF,
            cutoff_at=CUTOFF,
            row_count=0,
            warning_count=0,
            applied_at=CUTOFF + timedelta(minutes=1),
        )
        unit.commit()


def _event(
    b7: B7Services,
    world: B7World,
    *,
    occurred_at: datetime,
    key: str,
) -> FinancialEvent:
    with b7.factory() as unit:
        event = unit.financial_events.add(
            FinancialEventDraft(
                origin=FinancialEventOrigin.GMAIL_TRANSACTION,
                event_type=FinancialEventType.PURCHASE,
                payment_method=PaymentMethod.CREDIT_CARD,
                recognition_basis=RecognitionBasis.GMAIL_NOTIFICATION,
                occurred_at=occurred_at,
                merchant="測試期初交易",
                amount_minor=10_000,
                currency="TWD",
                card_id=world.card_id,
                status=FinancialEventStatus.NEEDS_ALLOCATION,
                idempotency_key=key,
            )
        )
        unit.commit()
        return event


def _request(
    event: FinancialEvent, world: B7World, *, token: str
) -> AllocateFinancialEventRequest:
    return AllocateFinancialEventRequest(
        financial_event_id=event.id,
        expected_version=event.version,
        client_token=token,
        lines=(
            AllocationLine(
                amount_minor=event.amount_minor,
                funding_wallet_id=world.bank_food_wallet_id,
                budget_category_id=world.food_id,
            ),
        ),
    )


def test_an_event_at_the_opening_cutoff_cannot_be_posted_twice(
    b7: B7Services,
    b7_world: B7World,
    migrated_database: Database,
) -> None:
    _apply_cutover(b7)
    event = _event(b7, b7_world, occurred_at=CUTOFF, key="pre-opening-event")
    with migrated_database.session_factory() as session:
        before = {
            model: int(session.scalar(select(func.count()).select_from(model)) or 0)
            for model in (
                JournalEntryModel,
                EventAllocationModel,
                FundingCommitmentModel,
            )
        }

    with pytest.raises(PreOpeningEventError, match="historical evidence"):
        b7.allocate.execute(_request(event, b7_world, token="blocked"))

    with migrated_database.session_factory() as session:
        after = {
            model: int(session.scalar(select(func.count()).select_from(model)) or 0)
            for model in (
                JournalEntryModel,
                EventAllocationModel,
                FundingCommitmentModel,
            )
        }
    assert after == before
    with b7.factory() as unit:
        unchanged = unit.financial_events.get(event.id)
        assert unchanged is not None
        assert unchanged.status is FinancialEventStatus.NEEDS_ALLOCATION
        assert unit.journal.get_by_financial_event(event.id) is None


def test_only_events_after_the_cutoff_appear_and_remain_allocatable(
    b7: B7Services,
    b7_world: B7World,
    migrated_database: Database,
) -> None:
    _apply_cutover(b7)
    historical = _event(
        b7,
        b7_world,
        occurred_at=CUTOFF - timedelta(seconds=1),
        key="historical-event",
    )
    current = _event(
        b7,
        b7_world,
        occurred_at=CUTOFF + timedelta(seconds=1),
        key="current-event",
    )
    review = SqlAlchemyMoneyReviewReadRepository(
        migrated_database.session_factory
    ).events_needing_review()
    budget = SqlAlchemyBudgetReadRepository(
        migrated_database.session_factory, local_timezone="Asia/Taipei"
    ).month(year=2026, month=8, currency="TWD")

    assert tuple(row.event.id for row in review) == (current.id,)
    assert historical.id not in {row.event.id for row in review}
    assert budget.events_needing_allocation == 1

    result = b7.allocate.execute(_request(current, b7_world, token="allowed"))
    assert result.event.status is FinancialEventStatus.POSTED


def test_a_late_event_cannot_backdate_its_entry_into_the_opening_position(
    b7: B7Services,
    b7_world: B7World,
) -> None:
    """The shared journal boundary protects non-allocation event writers too."""
    _apply_cutover(b7)
    event = _event(
        b7,
        b7_world,
        occurred_at=CUTOFF + timedelta(seconds=1),
        key="late-event-with-old-entry",
    )
    journal = JournalService(b7.factory)

    with b7.factory() as unit:
        ledger = unit.ledger_accounts.get_for_financial_account(
            b7_world.bank_account_id
        )
        assert ledger is not None
        with pytest.raises(PreOpeningEventError, match="historical evidence"):
            journal.stage_balanced(
                unit,
                entry_type=JournalEntryType.INTERNAL_TRANSFER,
                effective_at=CUTOFF,
                idempotency_key="backdated-event-entry",
                description="test invariant",
                postings=(
                    PostingDraft(
                        ledger_account_id=ledger.id,
                        amount=Money(-1, "TWD"),
                        wallet_id=b7_world.bank_unallocated_id,
                    ),
                    PostingDraft(
                        ledger_account_id=ledger.id,
                        amount=Money(1, "TWD"),
                        wallet_id=b7_world.bank_food_wallet_id,
                    ),
                ),
                financial_event_id=event.id,
            )
        assert unit.journal.get_by_financial_event(event.id) is None

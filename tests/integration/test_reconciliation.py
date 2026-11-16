"""B7 acceptance tests for balance snapshots and reasoned adjustments."""

from __future__ import annotations

from datetime import timedelta

import pytest
from app.application.queries.spending import CANDIDATE_AMOUNT_MATCHES
from app.application.services.bank_movements import BankMovementRequest
from app.application.services.ledger import (
    JournalService,
    LedgerSetupService,
    OpenFinancialAccountRequest,
    OpeningPositionRequest,
)
from app.application.services.reconciliation import (
    BalanceSnapshotRequest,
    ReconciliationAdjustmentRequest,
)
from app.domain.budget_enums import (
    BalanceSnapshotSource,
    BankMovementKind,
    FinancialAccountType,
    FinancialEventOrigin,
    FinancialEventStatus,
    FinancialEventType,
    JournalEntryType,
    LedgerAccountKind,
    PaymentMethod,
    RecognitionBasis,
)
from app.domain.budget_models import (
    FinancialEventDraft,
    JournalEntryDraft,
    LedgerAccountDraft,
    Money,
    OpeningWalletSplit,
    PostingDraft,
)
from app.domain.errors import (
    BalanceSnapshotError,
    IdempotencyConflictError,
    ReconciliationError,
    StaleReconciliationError,
)
from app.infrastructure.database.engine import Database
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

from tests.b7_support import B7_NOW, B7Services, B7World

LATER = B7_NOW + timedelta(hours=6)
EVEN_LATER = B7_NOW + timedelta(hours=12)


def _snapshot(
    b7: B7Services,
    world: B7World,
    *,
    balance_minor: int,
    token: str = "snap-1",
    observed_at: object = None,
    note: str | None = None,
) -> int:
    snapshot = b7.snapshots.execute(
        BalanceSnapshotRequest(
            financial_account_id=world.bank_account_id,
            observed_balance_minor=balance_minor,
            observed_at=observed_at or LATER,  # type: ignore[arg-type]
            client_token=token,
            note=note,
        )
    )
    return snapshot.id


def test_a_difference_is_narrowed_to_the_entries_that_could_produce_it(
    b7: B7Services, b7_world: B7World
) -> None:
    """Report the gap and where to look, without accusing any one entry.

    A gap in a Sheet gives nothing to work from. Here the entries all carry
    an amount, when they were decided, and what they came from, so the two
    shapes that produce a gap can be offered: an entry worth exactly the
    difference, and one recorded after the observation it would change.
    """
    ledger_balance = 1_000_000
    with b7.factory() as unit:
        opening = unit.journal.get_by_idempotency_key(
            f"opening_balance:financial_account:{b7_world.bank_account_id}"
        )
    assert opening is not None

    # Agreeing first: nothing to explain, and nothing offered.
    _snapshot(b7, b7_world, balance_minor=ledger_balance, token="agrees")
    agreed = b7.spending.explain_difference(b7_world.bank_account_id)
    assert agreed.difference_minor == 0
    assert agreed.candidates == ()

    # Now an observation short by exactly what the opening entry posted,
    # which is what one missing line in the bank's own history looks like.
    _snapshot(
        b7,
        b7_world,
        balance_minor=0,
        token="short",
        observed_at=EVEN_LATER,
    )
    explained = b7.spending.explain_difference(b7_world.bank_account_id)

    # Observed minus ledger, signed: the bank is short of what Scribe holds.
    assert explained.difference_minor == -ledger_balance
    matches = [
        candidate
        for candidate in explained.candidates
        if candidate.reason == CANDIDATE_AMOUNT_MATCHES
    ]
    assert matches, "an entry worth exactly the gap should be offered"
    assert all(abs(candidate.amount_minor) == ledger_balance for candidate in matches)
    # Every candidate carries what it takes to check against a statement.
    assert all(candidate.journal_entry_id > 0 for candidate in matches)


def test_an_account_with_no_observation_explains_nothing(
    b7: B7Services, b7_world: B7World
) -> None:
    """Without an observation there is no difference to attribute."""
    explanation = b7.spending.explain_difference(b7_world.bank_account_id)

    assert explanation.snapshot is None
    assert explanation.candidates == ()
    assert explanation.difference_minor == 0


def test_a_snapshot_records_evidence_without_posting_anything(
    b7: B7Services, b7_world: B7World
) -> None:
    snapshot_id = _snapshot(b7, b7_world, balance_minor=1_000_000)

    with b7.factory() as unit:
        snapshot = unit.balance_snapshots.get(snapshot_id)
        assert snapshot is not None
        assert snapshot.source is BalanceSnapshotSource.MANUAL_BANK_APP
        assert (
            unit.reconciliation_ledger.physical_balance_as_of(
                financial_account_id=b7_world.bank_account_id, as_of=LATER
            )
            == 1_000_000
        )
    view = b7.snapshots.view(snapshot_id)
    assert view.is_reconciled
    assert view.difference_minor == 0


@pytest.mark.parametrize("balance", [0, -25_000])
def test_a_snapshot_accepts_zero_and_negative_balances(
    b7: B7Services, b7_world: B7World, balance: int
) -> None:
    snapshot_id = _snapshot(b7, b7_world, balance_minor=balance)

    with b7.factory() as unit:
        snapshot = unit.balance_snapshots.get(snapshot_id)
        assert snapshot is not None
        assert snapshot.observed_balance_minor == balance


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE balance_snapshots SET observed_balance_minor = 1 WHERE id = :id",
        "DELETE FROM balance_snapshots WHERE id = :id",
    ],
)
def test_a_snapshot_row_cannot_be_updated_or_deleted(
    b7: B7Services,
    b7_world: B7World,
    migrated_database: Database,
    statement: str,
) -> None:
    """Revision 0014 refuses both at the row level (ADR-B066)."""
    snapshot_id = _snapshot(b7, b7_world, balance_minor=1_000_000)

    with (
        migrated_database.engine.begin() as connection,
        pytest.raises(DatabaseError, match="immutable"),
    ):
        connection.execute(text(statement), {"id": snapshot_id})


def test_a_replayed_snapshot_returns_the_first_observation(
    b7: B7Services, b7_world: B7World
) -> None:
    first = _snapshot(b7, b7_world, balance_minor=1_000_000, note="早上看的")
    second = _snapshot(b7, b7_world, balance_minor=1_000_000, note="早上看的")

    assert first == second


def test_the_same_snapshot_token_with_another_balance_is_a_conflict(
    b7: B7Services, b7_world: B7World
) -> None:
    _snapshot(b7, b7_world, balance_minor=1_000_000)

    with pytest.raises(IdempotencyConflictError):
        _snapshot(b7, b7_world, balance_minor=999_999)


def test_an_unknown_account_is_refused(b7: B7Services, b7_world: B7World) -> None:
    with pytest.raises(BalanceSnapshotError):
        b7.snapshots.execute(
            BalanceSnapshotRequest(
                financial_account_id=99_999,
                observed_balance_minor=1,
                observed_at=LATER,
                client_token="missing",
            )
        )


def test_the_difference_uses_the_balance_as_of_the_observation(
    b7: B7Services, b7_world: B7World
) -> None:
    """A movement after the observation must not change what it showed."""
    snapshot_id = _snapshot(b7, b7_world, balance_minor=1_000_000)
    b7.movements.execute(
        BankMovementRequest(
            kind=BankMovementKind.EXTERNAL_WITHDRAWAL,
            occurred_at=LATER + timedelta(hours=1),
            amount_minor=200_000,
            currency="TWD",
            wallet_id=b7_world.bank_unallocated_id,
            description="之後才發生的提款",
            client_token="later",
        )
    )

    view = b7.snapshots.view(snapshot_id)

    assert view.ledger_balance_minor == 1_000_000
    assert view.difference_minor == 0


def test_a_positive_difference_raises_the_account_against_equity(
    b7: B7Services, b7_world: B7World
) -> None:
    snapshot_id = _snapshot(b7, b7_world, balance_minor=1_050_000)

    result = b7.reconciliation.execute(
        ReconciliationAdjustmentRequest(
            snapshot_id=snapshot_id,
            expected_difference_minor=50_000,
            reason="找不到來源的入帳",
            client_token="adjust-1",
        )
    )

    assert result.difference_minor == 50_000
    assert result.event.event_type is FinancialEventType.ADJUSTMENT
    assert result.event.status is FinancialEventStatus.POSTED
    assert result.event.amount_minor == 50_000
    assert result.entry.entry_type is JournalEntryType.ADJUSTMENT
    assert result.entry.effective_at == LATER
    with b7.factory() as unit:
        by_kind = {}
        for posting in result.entry.postings:
            ledger = unit.ledger_accounts.get(posting.ledger_account_id)
            assert ledger is not None
            by_kind[ledger.kind] = posting
    assert by_kind[LedgerAccountKind.ASSET].amount_minor == 50_000
    assert by_kind[LedgerAccountKind.EQUITY].amount_minor == -50_000
    assert by_kind[LedgerAccountKind.ASSET].wallet_id == b7_world.bank_unallocated_id
    assert b7.snapshots.view(snapshot_id).is_reconciled


def test_a_negative_difference_lowers_the_account(
    b7: B7Services, b7_world: B7World
) -> None:
    snapshot_id = _snapshot(b7, b7_world, balance_minor=940_000)

    result = b7.reconciliation.execute(
        ReconciliationAdjustmentRequest(
            snapshot_id=snapshot_id,
            expected_difference_minor=-60_000,
            reason="銀行少了一筆",
            client_token="adjust-1",
        )
    )

    assert result.difference_minor == -60_000
    assert result.event.amount_minor == 60_000
    with b7.factory() as unit:
        asset = next(
            posting
            for posting in result.entry.postings
            if unit.ledger_accounts.get(posting.ledger_account_id).kind  # type: ignore[union-attr]
            is LedgerAccountKind.ASSET
        )
    assert asset.amount_minor == -60_000


def test_a_zero_difference_is_refused(b7: B7Services, b7_world: B7World) -> None:
    snapshot_id = _snapshot(b7, b7_world, balance_minor=1_000_000)

    with pytest.raises(ReconciliationError):
        b7.reconciliation.execute(
            ReconciliationAdjustmentRequest(
                snapshot_id=snapshot_id,
                expected_difference_minor=0,
                reason="沒有差額",
                client_token="adjust-1",
            )
        )


def test_a_reason_is_required(b7: B7Services, b7_world: B7World) -> None:
    snapshot_id = _snapshot(b7, b7_world, balance_minor=1_050_000)

    with pytest.raises(ValueError):
        b7.reconciliation.execute(
            ReconciliationAdjustmentRequest(
                snapshot_id=snapshot_id,
                expected_difference_minor=50_000,
                reason="   ",
                client_token="adjust-1",
            )
        )


def test_a_stale_expected_difference_is_a_conflict(
    b7: B7Services, b7_world: B7World
) -> None:
    snapshot_id = _snapshot(b7, b7_world, balance_minor=1_050_000)

    with pytest.raises(StaleReconciliationError):
        b7.reconciliation.execute(
            ReconciliationAdjustmentRequest(
                snapshot_id=snapshot_id,
                expected_difference_minor=40_000,
                reason="舊的差額",
                client_token="adjust-1",
            )
        )


def test_a_backdated_movement_can_make_an_old_difference_zero(
    b7: B7Services, b7_world: B7World
) -> None:
    snapshot_id = _snapshot(b7, b7_world, balance_minor=1_050_000)
    # The user finds the missing deposit and records it before adjusting.
    b7.movements.execute(
        BankMovementRequest(
            kind=BankMovementKind.EXTERNAL_DEPOSIT,
            occurred_at=LATER - timedelta(hours=1),
            amount_minor=50_000,
            currency="TWD",
            wallet_id=b7_world.bank_unallocated_id,
            description="補記的匯入",
            client_token="backdated",
        )
    )

    with pytest.raises(ReconciliationError):
        b7.reconciliation.execute(
            ReconciliationAdjustmentRequest(
                snapshot_id=snapshot_id,
                expected_difference_minor=50_000,
                reason="以為找不到",
                client_token="adjust-1",
            )
        )
    assert b7.snapshots.view(snapshot_id).is_reconciled


def test_a_replayed_adjustment_returns_the_first_one(
    b7: B7Services, b7_world: B7World
) -> None:
    snapshot_id = _snapshot(b7, b7_world, balance_minor=1_050_000)
    request = ReconciliationAdjustmentRequest(
        snapshot_id=snapshot_id,
        expected_difference_minor=50_000,
        reason="找不到來源的入帳",
        client_token="adjust-1",
    )
    first = b7.reconciliation.execute(request)

    second = b7.reconciliation.execute(request)

    assert second.event.id == first.event.id
    assert second.entry.id == first.entry.id
    with b7.factory() as unit:
        snapshot = unit.balance_snapshots.get(snapshot_id)
        assert snapshot is not None
        assert snapshot.observed_balance_minor == 1_050_000


def test_an_adjustment_token_reused_for_another_reason_is_a_conflict(
    b7: B7Services, b7_world: B7World
) -> None:
    snapshot_id = _snapshot(b7, b7_world, balance_minor=1_050_000)
    b7.reconciliation.execute(
        ReconciliationAdjustmentRequest(
            snapshot_id=snapshot_id,
            expected_difference_minor=50_000,
            reason="找不到來源的入帳",
            client_token="adjust-1",
        )
    )

    with pytest.raises(IdempotencyConflictError):
        b7.reconciliation.execute(
            ReconciliationAdjustmentRequest(
                snapshot_id=snapshot_id,
                expected_difference_minor=50_000,
                reason="另一個理由",
                client_token="adjust-1",
            )
        )


def test_each_currency_reconciles_on_its_own(b7: B7Services, b7_world: B7World) -> None:
    setup = LedgerSetupService(b7.factory)
    yen = setup.open_account(
        OpenFinancialAccountRequest(
            institution_code="test_bank",
            display_name="日圓帳戶",
            account_type=FinancialAccountType.SAVINGS,
            currency="JPY",
        )
    )
    JournalService(b7.factory).record_opening_position(
        OpeningPositionRequest(
            financial_account_id=yen.account.id,
            balance_minor=100_000,
            observed_at=B7_NOW,
            splits=(OpeningWalletSplit(wallet_name="尚未分配", amount_minor=100_000),),
        )
    )
    snapshot = b7.snapshots.execute(
        BalanceSnapshotRequest(
            financial_account_id=yen.account.id,
            observed_balance_minor=101_000,
            observed_at=LATER,
            client_token="yen-snap",
        )
    )

    result = b7.reconciliation.execute(
        ReconciliationAdjustmentRequest(
            snapshot_id=snapshot.id,
            expected_difference_minor=1_000,
            reason="日圓利息",
            client_token="yen-adjust",
        )
    )

    assert result.event.currency == "JPY"
    assert all(posting.currency == "JPY" for posting in result.entry.postings)


def test_a_card_liability_account_can_never_be_snapshotted(
    b7: B7Services, b7_world: B7World
) -> None:
    """It holds no wallets, so no adjustment could ever settle the gap."""
    with pytest.raises(BalanceSnapshotError):
        b7.snapshots.execute(
            BalanceSnapshotRequest(
                financial_account_id=b7_world.liability_account_id,
                observed_balance_minor=-45_000,
                observed_at=LATER,
                client_token="liability-snap",
            )
        )

    with b7.factory() as unit:
        assert (
            unit.balance_snapshots.latest_for_account(b7_world.liability_account_id)
            is None
        )


def test_the_reconcile_view_never_offers_an_account_it_cannot_settle(
    b7: B7Services, b7_world: B7World
) -> None:
    offered = {row.account.id for row in b7.spending.reconciliation().accounts}

    assert b7_world.bank_account_id in offered
    assert b7_world.cash_account_id in offered
    assert b7_world.liability_account_id not in offered


def test_the_reconcile_view_uses_wallet_purpose_not_its_display_name(
    b7: B7Services, b7_world: B7World, migrated_database: Database
) -> None:
    with migrated_database.engine.begin() as connection:
        connection.execute(
            text("UPDATE wallets SET name = :name WHERE id = :id"),
            {"name": "待分類資金", "id": b7_world.bank_unallocated_id},
        )

    offered = {row.account.id for row in b7.spending.reconciliation().accounts}

    assert b7_world.bank_account_id in offered


def test_one_token_cannot_answer_for_a_second_identical_snapshot(
    b7: B7Services, b7_world: B7World
) -> None:
    """Two observations are two pieces of evidence, even when they agree."""
    first = _snapshot(b7, b7_world, balance_minor=1_050_000, token="snap-1")
    second = _snapshot(b7, b7_world, balance_minor=1_050_000, token="snap-2")
    assert first != second
    b7.reconciliation.execute(
        ReconciliationAdjustmentRequest(
            snapshot_id=first,
            expected_difference_minor=50_000,
            reason="找不到來源的入帳",
            client_token="adjust-1",
        )
    )

    # The first adjustment already closed the gap both observations describe,
    # so the second one has nothing left to post and must say so.
    with pytest.raises(ReconciliationError):
        b7.reconciliation.execute(
            ReconciliationAdjustmentRequest(
                snapshot_id=second,
                expected_difference_minor=50_000,
                reason="找不到來源的入帳",
                client_token="adjust-1",
            )
        )
    with b7.factory() as unit:
        adjustments = [
            event
            for event in unit.financial_events.list_by_status(
                frozenset({FinancialEventStatus.POSTED})
            )
            if event.event_type is FinancialEventType.ADJUSTMENT
        ]
    assert len(adjustments) == 1


def test_a_second_snapshot_with_a_real_gap_is_adjusted_on_its_own(
    b7: B7Services, b7_world: B7World
) -> None:
    """Binding the token to a snapshot must not block a genuine second one."""
    first = _snapshot(b7, b7_world, balance_minor=1_050_000, token="snap-1")
    b7.reconciliation.execute(
        ReconciliationAdjustmentRequest(
            snapshot_id=first,
            expected_difference_minor=50_000,
            reason="第一次差額",
            client_token="adjust-1",
        )
    )
    second = _snapshot(
        b7,
        b7_world,
        balance_minor=1_070_000,
        token="snap-2",
        observed_at=LATER + timedelta(hours=1),
    )

    result = b7.reconciliation.execute(
        ReconciliationAdjustmentRequest(
            snapshot_id=second,
            expected_difference_minor=20_000,
            reason="第二次差額",
            client_token="adjust-2",
        )
    )

    assert result.difference_minor == 20_000
    assert b7.snapshots.view(second).is_reconciled


def test_an_adjustment_token_owned_by_another_event_is_a_conflict(
    b7: B7Services, b7_world: B7World
) -> None:
    """A look-alike event holding the token must not be replayed as ours."""
    snapshot_id = _snapshot(b7, b7_world, balance_minor=1_050_000)
    with b7.factory() as unit:
        unit.financial_events.add(
            FinancialEventDraft(
                origin=FinancialEventOrigin.MANUAL,
                event_type=FinancialEventType.ADJUSTMENT,
                payment_method=PaymentMethod.OTHER,
                recognition_basis=RecognitionBasis.OBSERVED_MANUAL,
                occurred_at=LATER,
                amount_minor=50_000,
                currency="TWD",
                idempotency_key=(f"reconciliation_adjustment:{snapshot_id}:squatter"),
                status=FinancialEventStatus.NEEDS_ALLOCATION,
                note="找不到來源的入帳",
            )
        )
        unit.commit()

    with pytest.raises(IdempotencyConflictError):
        b7.reconciliation.execute(
            ReconciliationAdjustmentRequest(
                snapshot_id=snapshot_id,
                expected_difference_minor=50_000,
                reason="找不到來源的入帳",
                client_token="squatter",
            )
        )


def test_an_adjustment_token_whose_entry_moves_another_wallet_conflicts(
    b7: B7Services, b7_world: B7World
) -> None:
    """The stored postings are verified, not merely the stored event."""
    snapshot_id = _snapshot(b7, b7_world, balance_minor=1_050_000)
    with b7.factory() as unit:
        squatter = unit.financial_events.add(
            FinancialEventDraft(
                origin=FinancialEventOrigin.MANUAL,
                event_type=FinancialEventType.ADJUSTMENT,
                payment_method=PaymentMethod.OTHER,
                recognition_basis=RecognitionBasis.OBSERVED_MANUAL,
                occurred_at=LATER,
                amount_minor=50_000,
                currency="TWD",
                idempotency_key=f"reconciliation_adjustment:{snapshot_id}:squatter",
                status=FinancialEventStatus.NEEDS_ALLOCATION,
                note="找不到來源的入帳",
            )
        )
        physical = unit.ledger_accounts.get_for_financial_account(
            b7_world.bank_account_id
        )
        equity = unit.ledger_accounts.add(
            LedgerAccountDraft(
                code="equity:reconciliation:TWD",
                name="Reconciliation difference (TWD)",
                kind=LedgerAccountKind.EQUITY,
                currency="TWD",
            )
        )
        assert physical is not None
        # Same account, same amount, same instant — but the money is put in a
        # different wallet than an adjustment of this snapshot would use.
        unit.journal.add(
            JournalEntryDraft(
                entry_type=JournalEntryType.ADJUSTMENT,
                effective_at=LATER,
                recorded_at=LATER,
                idempotency_key=(
                    f"reconciliation_adjustment:financial_event:{squatter.id}"
                ),
                description="找不到來源的入帳",
                postings=(
                    PostingDraft(
                        ledger_account_id=physical.id,
                        amount=Money(50_000, "TWD"),
                        wallet_id=b7_world.bank_food_wallet_id,
                    ),
                    PostingDraft(
                        ledger_account_id=equity.id,
                        amount=Money(-50_000, "TWD"),
                    ),
                ),
                financial_event_id=squatter.id,
            )
        )
        unit.financial_events.mark_posted(squatter.id, squatter.version)
        unit.commit()

    with pytest.raises(IdempotencyConflictError):
        b7.reconciliation.execute(
            ReconciliationAdjustmentRequest(
                snapshot_id=snapshot_id,
                expected_difference_minor=50_000,
                reason="找不到來源的入帳",
                client_token="squatter",
            )
        )

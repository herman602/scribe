"""B7 acceptance tests for already-observed bank movements."""

from __future__ import annotations

from datetime import timedelta

import pytest
from app.application.queries.budget import BudgetQueries
from app.application.queries.money import MoneyBalanceQueries
from app.application.services.bank_movements import (
    AccountTransferRequest,
    BankMovementRequest,
    ManualRefundRequest,
)
from app.application.services.manual_money import ManualExpenseRequest
from app.domain.budget_enums import (
    BankMovementKind,
    CommitmentStatus,
    FinancialEventStatus,
    FinancialEventType,
    JournalEntryType,
    LedgerAccountKind,
    PaymentMethod,
)
from app.domain.errors import (
    BankMovementError,
    CurrencyMismatchError,
    FinancialEventError,
    IdempotencyConflictError,
    LedgerConfigurationError,
)
from app.infrastructure.database.budget_queries import (
    SqlAlchemyBudgetReadRepository,
    SqlAlchemyLedgerBalanceReadRepository,
)
from app.infrastructure.database.engine import Database
from sqlalchemy import text

from tests.b7_support import B7_NOW, B7Services, B7World


def _movement(world: B7World, **overrides: object) -> BankMovementRequest:
    values: dict[str, object] = {
        "kind": BankMovementKind.INTEREST_INCOME,
        "occurred_at": B7_NOW,
        "amount_minor": 1_200,
        "currency": "TWD",
        "wallet_id": world.bank_unallocated_id,
        "description": "活期利息",
        "client_token": "movement-token",
    }
    values.update(overrides)
    return BankMovementRequest(**values)  # type: ignore[arg-type]


def _kinds(b7: B7Services, entry_postings: object) -> dict[LedgerAccountKind, int]:
    with b7.factory() as unit:
        result: dict[LedgerAccountKind, int] = {}
        for posting in entry_postings:  # type: ignore[attr-defined]
            ledger = unit.ledger_accounts.get(posting.ledger_account_id)
            assert ledger is not None
            result[ledger.kind] = result.get(ledger.kind, 0) + posting.amount_minor
        return result


def _account_balance(database: Database, account_id: int) -> int:
    balances = MoneyBalanceQueries(
        SqlAlchemyLedgerBalanceReadRepository(database.session_factory)
    )
    return next(
        item.balance_minor
        for item in balances.account_balances()
        if item.financial_account_id == account_id
    )


def test_interest_raises_the_wallet_against_income(
    b7: B7Services, b7_world: B7World, migrated_database: Database
) -> None:
    result = b7.movements.execute(_movement(b7_world))

    assert result.event.event_type is FinancialEventType.INTEREST_INCOME
    assert result.event.status is FinancialEventStatus.POSTED
    assert result.entry.entry_type is JournalEntryType.INCOME
    totals = _kinds(b7, result.entry.postings)
    assert totals[LedgerAccountKind.ASSET] == 1_200
    assert totals[LedgerAccountKind.INCOME] == -1_200
    assert _account_balance(migrated_database, b7_world.bank_account_id) == 1_001_200


def test_an_external_deposit_uses_the_external_clearing_account(
    b7: B7Services, b7_world: B7World
) -> None:
    result = b7.movements.execute(
        _movement(
            b7_world,
            kind=BankMovementKind.EXTERNAL_DEPOSIT,
            description="家人匯入",
            amount_minor=250_000,
        )
    )

    assert result.entry.entry_type is JournalEntryType.EXTERNAL_MOVEMENT
    totals = _kinds(b7, result.entry.postings)
    assert totals[LedgerAccountKind.ASSET] == 250_000
    assert totals[LedgerAccountKind.CLEARING] == -250_000


def test_an_external_withdrawal_lowers_the_wallet(
    b7: B7Services, b7_world: B7World, migrated_database: Database
) -> None:
    result = b7.movements.execute(
        _movement(
            b7_world,
            kind=BankMovementKind.EXTERNAL_WITHDRAWAL,
            description="提款",
            amount_minor=30_000,
        )
    )

    totals = _kinds(b7, result.entry.postings)
    assert totals[LedgerAccountKind.ASSET] == -30_000
    assert totals[LedgerAccountKind.CLEARING] == 30_000
    assert _account_balance(migrated_database, b7_world.bank_account_id) == 970_000


def test_a_cash_withdrawal_is_immediately_counted_as_monthly_cash_spending(
    b7: B7Services, b7_world: B7World, migrated_database: Database
) -> None:
    result = b7.movements.execute(
        _movement(
            b7_world,
            kind=BankMovementKind.CASH_WITHDRAWAL_SPENDING,
            description="提款列入生活費",
            amount_minor=100_000,
            budget_category_id=b7_world.food_id,
            client_token="cash-withdrawal-spending",
        )
    )

    assert result.event.event_type is FinancialEventType.WITHDRAWAL
    assert result.event.payment_method is PaymentMethod.CASH
    assert result.entry.entry_type is JournalEntryType.EXPENSE
    totals = _kinds(b7, result.entry.postings)
    assert totals[LedgerAccountKind.EXPENSE] == 100_000
    assert totals[LedgerAccountKind.ASSET] == -100_000
    assert _account_balance(migrated_database, b7_world.bank_account_id) == 900_000
    assert _account_balance(migrated_database, b7_world.cash_account_id) == 50_000

    daily = b7.spending.daily(day=B7_NOW.date())
    assert daily.total_rows == 1
    assert daily.rows[0].merchant == "提款列入生活費"
    assert daily.rows[0].payment_method is PaymentMethod.CASH
    assert daily.totals[0].amount_minor == 100_000
    budget = BudgetQueries(
        SqlAlchemyBudgetReadRepository(
            migrated_database.session_factory, local_timezone="Asia/Taipei"
        )
    ).month(year=2026, month=8, currency="TWD")
    assert budget.total_spend_minor == 100_000
    with b7.factory() as unit:
        assert unit.commitments.list_for_event(result.event.id) == ()


def test_a_consumed_cash_withdrawal_replays_but_cannot_come_from_a_cash_account(
    b7: B7Services, b7_world: B7World
) -> None:
    request = _movement(
        b7_world,
        kind=BankMovementKind.CASH_WITHDRAWAL_SPENDING,
        description="提款列入生活費",
        amount_minor=100_000,
        budget_category_id=b7_world.food_id,
        client_token="cash-withdrawal-replay",
    )
    first = b7.movements.execute(request)
    replay = b7.movements.execute(request)
    assert replay.entry.id == first.entry.id

    with pytest.raises(BankMovementError, match="checking or savings"):
        b7.movements.execute(
            _movement(
                b7_world,
                kind=BankMovementKind.CASH_WITHDRAWAL_SPENDING,
                description="錯誤的皮夾提款",
                wallet_id=b7_world.cash_wallet_id,
                budget_category_id=b7_world.food_id,
                client_token="cash-account-withdrawal",
            )
        )


@pytest.mark.parametrize(
    "kind",
    [
        BankMovementKind.DIRECT_DEBIT,
        BankMovementKind.FEE,
        BankMovementKind.BILL_PAYMENT,
    ],
)
def test_bank_paid_spending_debits_its_category(
    b7: B7Services, b7_world: B7World, kind: BankMovementKind
) -> None:
    result = b7.movements.execute(
        _movement(
            b7_world,
            kind=kind,
            description="電費",
            amount_minor=88_000,
            budget_category_id=b7_world.transport_id,
        )
    )

    assert result.entry.entry_type is JournalEntryType.EXPENSE
    totals = _kinds(b7, result.entry.postings)
    assert totals[LedgerAccountKind.EXPENSE] == 88_000
    assert totals[LedgerAccountKind.ASSET] == -88_000
    with b7.factory() as unit:
        allocations = unit.allocations.list_for_event(result.event.id)
        assert len(allocations) == 1
        assert allocations[0].funding_wallet_id == b7_world.bank_unallocated_id


def test_bank_paid_spending_requires_a_category(
    b7: B7Services, b7_world: B7World
) -> None:
    with pytest.raises(BankMovementError):
        b7.movements.execute(
            _movement(b7_world, kind=BankMovementKind.FEE, description="手續費")
        )


def test_a_non_spending_movement_refuses_a_category(
    b7: B7Services, b7_world: B7World
) -> None:
    with pytest.raises(BankMovementError):
        b7.movements.execute(_movement(b7_world, budget_category_id=b7_world.food_id))


def test_a_movement_may_not_use_a_wallet_of_another_currency(
    b7: B7Services, b7_world: B7World
) -> None:
    with pytest.raises(CurrencyMismatchError):
        b7.movements.execute(_movement(b7_world, currency="JPY"))


def test_an_unknown_wallet_is_refused(b7: B7Services, b7_world: B7World) -> None:
    with pytest.raises(LedgerConfigurationError):
        b7.movements.execute(_movement(b7_world, wallet_id=99_999))


def test_a_replayed_movement_returns_the_first_one(
    b7: B7Services, b7_world: B7World
) -> None:
    first = b7.movements.execute(_movement(b7_world))
    second = b7.movements.execute(_movement(b7_world))

    assert second.event.id == first.event.id
    assert second.entry.id == first.entry.id


def test_the_same_movement_token_with_another_amount_is_a_conflict(
    b7: B7Services, b7_world: B7World
) -> None:
    b7.movements.execute(_movement(b7_world))

    with pytest.raises(IdempotencyConflictError):
        b7.movements.execute(_movement(b7_world, amount_minor=1_201))


def test_a_movement_may_make_a_wallet_negative(
    b7: B7Services, b7_world: B7World, migrated_database: Database
) -> None:
    """Bank reality wins: an observed withdrawal is recorded, not refused."""
    b7.movements.execute(
        _movement(
            b7_world,
            kind=BankMovementKind.EXTERNAL_WITHDRAWAL,
            description="全額轉出",
            amount_minor=1_500_000,
        )
    )

    assert _account_balance(migrated_database, b7_world.bank_account_id) == -500_000


def test_a_tracked_transfer_moves_wallets_without_spending(
    b7: B7Services, b7_world: B7World
) -> None:
    result = b7.movements.transfer(
        AccountTransferRequest(
            source_wallet_id=b7_world.bank_unallocated_id,
            destination_wallet_id=b7_world.bank_food_wallet_id,
            occurred_at=B7_NOW,
            amount_minor=100_000,
            currency="TWD",
            description="撥到飲食",
            client_token="transfer-token",
        )
    )

    assert result.event.event_type is FinancialEventType.TRANSFER
    assert result.event.payment_method is PaymentMethod.TRANSFER
    assert result.entry.entry_type is JournalEntryType.INTERNAL_TRANSFER
    totals = _kinds(b7, result.entry.postings)
    assert LedgerAccountKind.EXPENSE not in totals
    with b7.factory() as unit:
        assert unit.allocations.list_for_event(result.event.id) == ()
        assert unit.commitments.list_for_event(result.event.id) == ()
    moved = {
        posting.wallet_id: posting.amount_minor for posting in result.entry.postings
    }
    assert moved[b7_world.bank_food_wallet_id] == 100_000
    assert moved[b7_world.bank_unallocated_id] == -100_000


def test_a_transfer_needs_two_different_wallets(
    b7: B7Services, b7_world: B7World
) -> None:
    with pytest.raises(BankMovementError):
        b7.movements.transfer(
            AccountTransferRequest(
                source_wallet_id=b7_world.bank_unallocated_id,
                destination_wallet_id=b7_world.bank_unallocated_id,
                occurred_at=B7_NOW,
                amount_minor=1_000,
                currency="TWD",
                description="自己轉自己",
                client_token="same-wallet",
            )
        )


def test_a_cross_currency_transfer_is_refused(
    b7: B7Services, b7_world: B7World
) -> None:
    with pytest.raises(CurrencyMismatchError):
        b7.movements.transfer(
            AccountTransferRequest(
                source_wallet_id=b7_world.bank_unallocated_id,
                destination_wallet_id=b7_world.cash_wallet_id,
                occurred_at=B7_NOW,
                amount_minor=1_000,
                currency="JPY",
                description="換幣",
                client_token="cross-currency",
            )
        )


def _posted_card_expense(b7: B7Services, world: B7World) -> int:
    result = b7.expenses.execute(
        ManualExpenseRequest(
            payment_method=PaymentMethod.CREDIT_CARD,
            occurred_at=B7_NOW,
            merchant="咖啡廳",
            amount_minor=45_000,
            currency="TWD",
            funding_wallet_id=world.bank_unallocated_id,
            client_token="card-token",
            budget_category_id=world.food_id,
            card_id=world.card_id,
        )
    )
    return result.event.id


def test_a_refund_returns_to_the_original_share_and_releases_it(
    b7: B7Services, b7_world: B7World
) -> None:
    original_id = _posted_card_expense(b7, b7_world)

    result = b7.movements.refund(
        ManualRefundRequest(
            original_event_id=original_id,
            occurred_at=B7_NOW + timedelta(hours=1),
            amount_minor=15_000,
            client_token="refund-token",
        )
    )

    assert result.event.event_type is FinancialEventType.REFUND
    assert result.event.refund_of_event_id == original_id
    assert result.entry.entry_type is JournalEntryType.REFUND
    totals = _kinds(b7, result.entry.postings)
    assert totals[LedgerAccountKind.EXPENSE] == -15_000
    assert totals[LedgerAccountKind.LIABILITY] == 15_000
    with b7.factory() as unit:
        commitments = unit.commitments.list_for_event(original_id)
        assert commitments[0].released_minor == 15_000
        assert commitments[0].status is CommitmentStatus.OPEN


def test_refunds_cannot_exceed_the_expense_they_refund(
    b7: B7Services, b7_world: B7World
) -> None:
    original_id = _posted_card_expense(b7, b7_world)
    b7.movements.refund(
        ManualRefundRequest(
            original_event_id=original_id,
            occurred_at=B7_NOW,
            amount_minor=30_000,
            client_token="refund-one",
        )
    )

    with pytest.raises(FinancialEventError):
        b7.movements.refund(
            ManualRefundRequest(
                original_event_id=original_id,
                occurred_at=B7_NOW,
                amount_minor=20_000,
                client_token="refund-two",
            )
        )


def test_a_refund_of_a_refund_is_refused(b7: B7Services, b7_world: B7World) -> None:
    original_id = _posted_card_expense(b7, b7_world)
    refund = b7.movements.refund(
        ManualRefundRequest(
            original_event_id=original_id,
            occurred_at=B7_NOW,
            amount_minor=10_000,
            client_token="refund-one",
        )
    )

    with pytest.raises(FinancialEventError):
        b7.movements.refund(
            ManualRefundRequest(
                original_event_id=refund.event.id,
                occurred_at=B7_NOW,
                amount_minor=1_000,
                client_token="refund-of-refund",
            )
        )


def test_a_replayed_refund_returns_the_first_one(
    b7: B7Services, b7_world: B7World
) -> None:
    original_id = _posted_card_expense(b7, b7_world)
    first = b7.movements.refund(
        ManualRefundRequest(
            original_event_id=original_id,
            occurred_at=B7_NOW,
            amount_minor=10_000,
            client_token="refund-one",
        )
    )
    second = b7.movements.refund(
        ManualRefundRequest(
            original_event_id=original_id,
            occurred_at=B7_NOW,
            amount_minor=10_000,
            client_token="refund-one",
        )
    )

    assert second.event.id == first.event.id
    assert second.entry.id == first.entry.id


def test_no_bank_movement_kind_produces_an_adjustment(
    b7: B7Services, b7_world: B7World
) -> None:
    """`adjustment` belongs to reconciliation alone (ADR-B065)."""
    assert not hasattr(BankMovementKind, "ADJUSTMENT")
    assert "adjustment" not in {kind.value for kind in BankMovementKind}


class TestReplayIsBoundToWhereTheMoneyWent:
    """A token answers for its own money position, or it conflicts."""

    def test_the_same_token_with_another_wallet_is_a_conflict(
        self, b7: B7Services, b7_world: B7World
    ) -> None:
        b7.movements.execute(_movement(b7_world))

        with pytest.raises(IdempotencyConflictError):
            b7.movements.execute(
                _movement(b7_world, wallet_id=b7_world.bank_food_wallet_id)
            )

    def test_the_first_wallet_keeps_the_money_after_a_refused_replay(
        self, b7: B7Services, b7_world: B7World, migrated_database: Database
    ) -> None:
        first = b7.movements.execute(_movement(b7_world))
        with pytest.raises(IdempotencyConflictError):
            b7.movements.execute(
                _movement(b7_world, wallet_id=b7_world.bank_food_wallet_id)
            )

        with b7.factory() as unit:
            entry = unit.journal.get(first.entry.id)
            assert entry is not None
            wallets = {
                posting.wallet_id
                for posting in entry.postings
                if posting.wallet_id is not None
            }
        assert wallets == {b7_world.bank_unallocated_id}

    def test_the_same_token_with_another_category_is_a_conflict(
        self, b7: B7Services, b7_world: B7World
    ) -> None:
        spending = {
            "kind": BankMovementKind.FEE,
            "description": "手續費",
            "amount_minor": 5_000,
            "budget_category_id": b7_world.food_id,
        }
        b7.movements.execute(_movement(b7_world, **spending))

        with pytest.raises(IdempotencyConflictError):
            b7.movements.execute(
                _movement(
                    b7_world,
                    **{**spending, "budget_category_id": b7_world.transport_id},
                )
            )

    def test_the_same_token_with_another_spending_wallet_is_a_conflict(
        self, b7: B7Services, b7_world: B7World
    ) -> None:
        spending = {
            "kind": BankMovementKind.FEE,
            "description": "手續費",
            "amount_minor": 5_000,
            "budget_category_id": b7_world.food_id,
        }
        b7.movements.execute(_movement(b7_world, **spending))

        with pytest.raises(IdempotencyConflictError):
            b7.movements.execute(
                _movement(b7_world, **spending, wallet_id=b7_world.bank_food_wallet_id)
            )

    def test_a_transfer_token_with_swapped_endpoints_is_a_conflict(
        self, b7: B7Services, b7_world: B7World
    ) -> None:
        forward = AccountTransferRequest(
            source_wallet_id=b7_world.bank_unallocated_id,
            destination_wallet_id=b7_world.bank_food_wallet_id,
            occurred_at=B7_NOW,
            amount_minor=100_000,
            currency="TWD",
            description="撥到飲食",
            client_token="transfer-token",
        )
        b7.movements.transfer(forward)

        with pytest.raises(IdempotencyConflictError):
            b7.movements.transfer(
                AccountTransferRequest(
                    source_wallet_id=b7_world.bank_food_wallet_id,
                    destination_wallet_id=b7_world.bank_unallocated_id,
                    occurred_at=B7_NOW,
                    amount_minor=100_000,
                    currency="TWD",
                    description="撥到飲食",
                    client_token="transfer-token",
                )
            )

    def test_a_token_owned_by_another_kind_of_entry_is_a_conflict(
        self, b7: B7Services, b7_world: B7World
    ) -> None:
        """An interest token cannot answer for a deposit of the same size."""
        b7.movements.execute(_movement(b7_world))

        with pytest.raises(IdempotencyConflictError):
            b7.movements.execute(
                _movement(b7_world, kind=BankMovementKind.EXTERNAL_DEPOSIT)
            )

    def test_an_identical_request_still_replays(
        self, b7: B7Services, b7_world: B7World
    ) -> None:
        first = b7.movements.execute(_movement(b7_world))
        second = b7.movements.execute(_movement(b7_world))

        assert (second.event.id, second.entry.id) == (first.event.id, first.entry.id)

    def test_an_identical_transfer_still_replays(
        self, b7: B7Services, b7_world: B7World
    ) -> None:
        request = AccountTransferRequest(
            source_wallet_id=b7_world.bank_unallocated_id,
            destination_wallet_id=b7_world.bank_food_wallet_id,
            occurred_at=B7_NOW,
            amount_minor=100_000,
            currency="TWD",
            description="撥到飲食",
            client_token="transfer-token",
        )
        first = b7.movements.transfer(request)
        second = b7.movements.transfer(request)

        assert (second.event.id, second.entry.id) == (first.event.id, first.entry.id)

    def test_an_identical_spending_movement_still_replays(
        self, b7: B7Services, b7_world: B7World
    ) -> None:
        spending = {
            "kind": BankMovementKind.DIRECT_DEBIT,
            "description": "電費",
            "amount_minor": 88_000,
            "budget_category_id": b7_world.transport_id,
        }
        first = b7.movements.execute(_movement(b7_world, **spending))
        second = b7.movements.execute(_movement(b7_world, **spending))

        assert (second.event.id, second.entry.id) == (first.event.id, first.entry.id)
        with b7.factory() as unit:
            assert len(unit.allocations.list_for_event(first.event.id)) == 1

    def test_a_spending_replay_checks_the_journal_wallet(
        self, b7: B7Services, b7_world: B7World, migrated_database: Database
    ) -> None:
        request = _movement(
            b7_world,
            kind=BankMovementKind.FEE,
            description="手續費",
            amount_minor=5_000,
            budget_category_id=b7_world.food_id,
        )
        first = b7.movements.execute(request)
        with migrated_database.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE journal_postings SET wallet_id = :wallet_id "
                    "WHERE journal_entry_id = :entry_id AND wallet_id IS NOT NULL"
                ),
                {
                    "wallet_id": b7_world.bank_food_wallet_id,
                    "entry_id": first.entry.id,
                },
            )

        with pytest.raises(IdempotencyConflictError):
            b7.movements.execute(request)

    def test_a_spending_replay_checks_the_journal_key(
        self, b7: B7Services, b7_world: B7World, migrated_database: Database
    ) -> None:
        request = _movement(
            b7_world,
            kind=BankMovementKind.FEE,
            description="手續費",
            amount_minor=5_000,
            budget_category_id=b7_world.food_id,
        )
        first = b7.movements.execute(request)
        with migrated_database.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE journal_entries SET idempotency_key = :key WHERE id = :id"
                ),
                {"key": "test:foreign-entry", "id": first.entry.id},
            )

        with pytest.raises(IdempotencyConflictError):
            b7.movements.execute(request)

    def test_an_identical_replay_survives_later_deactivation(
        self, b7: B7Services, b7_world: B7World, migrated_database: Database
    ) -> None:
        request = _movement(b7_world)
        first = b7.movements.execute(request)
        with migrated_database.engine.begin() as connection:
            connection.execute(
                text("UPDATE wallets SET is_active = 0 WHERE id = :id"),
                {"id": b7_world.bank_unallocated_id},
            )
            connection.execute(
                text("UPDATE financial_accounts SET is_active = 0 WHERE id = :id"),
                {"id": b7_world.bank_account_id},
            )

        second = b7.movements.execute(request)

        assert (second.event.id, second.entry.id) == (first.event.id, first.entry.id)


class TestRefundReplayCanonicalEvidence:
    """A refund token owns its exact split and journal evidence."""

    @staticmethod
    def _request(b7: B7Services, world: B7World) -> ManualRefundRequest:
        return ManualRefundRequest(
            original_event_id=_posted_card_expense(b7, world),
            occurred_at=B7_NOW,
            amount_minor=10_000,
            client_token="refund-evidence",
        )

    def test_a_refund_replay_checks_the_exact_allocation(
        self, b7: B7Services, b7_world: B7World, migrated_database: Database
    ) -> None:
        request = self._request(b7, b7_world)
        first = b7.movements.refund(request)
        with migrated_database.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE event_allocations SET budget_category_id = :category "
                    "WHERE financial_event_id = :event"
                ),
                {"category": b7_world.transport_id, "event": first.event.id},
            )

        with pytest.raises(IdempotencyConflictError):
            b7.movements.refund(request)

    def test_a_refund_replay_checks_the_journal_key(
        self, b7: B7Services, b7_world: B7World, migrated_database: Database
    ) -> None:
        request = self._request(b7, b7_world)
        first = b7.movements.refund(request)
        with migrated_database.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE journal_entries SET idempotency_key = :key WHERE id = :id"
                ),
                {"key": "test:foreign-refund", "id": first.entry.id},
            )

        with pytest.raises(IdempotencyConflictError):
            b7.movements.refund(request)

    def test_a_refund_replay_checks_the_posting_category(
        self, b7: B7Services, b7_world: B7World, migrated_database: Database
    ) -> None:
        request = self._request(b7, b7_world)
        first = b7.movements.refund(request)
        with migrated_database.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE journal_postings SET budget_category_id = :category "
                    "WHERE journal_entry_id = :entry AND budget_category_id IS NOT NULL"
                ),
                {"category": b7_world.transport_id, "entry": first.entry.id},
            )

        with pytest.raises(IdempotencyConflictError):
            b7.movements.refund(request)

    def test_a_later_partial_refund_still_replays_its_deterministic_split(
        self, b7: B7Services, b7_world: B7World
    ) -> None:
        original_id = _posted_card_expense(b7, b7_world)
        b7.movements.refund(
            ManualRefundRequest(
                original_event_id=original_id,
                occurred_at=B7_NOW,
                amount_minor=10_000,
                client_token="refund-first",
            )
        )
        request = ManualRefundRequest(
            original_event_id=original_id,
            occurred_at=B7_NOW + timedelta(minutes=1),
            amount_minor=15_000,
            client_token="refund-second",
        )

        first = b7.movements.refund(request)
        replay = b7.movements.refund(request)

        assert (replay.event.id, replay.entry.id) == (first.event.id, first.entry.id)

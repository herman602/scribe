"""B7 acceptance tests for hand-entered expenses and their reversal."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.application.queries.budget import BudgetQueries
from app.application.queries.money import MoneyBalanceQueries
from app.application.services.bank_movements import ManualRefundRequest
from app.application.services.event_allocation import (
    AllocateFinancialEventRequest,
    AllocationLine,
    RedoAllocationRequest,
)
from app.application.services.manual_money import (
    ManualExpenseRequest,
    ReverseEventRequest,
    UpdateFinancialEventNote,
    UpdateFinancialEventNoteRequest,
)
from app.domain.budget_enums import (
    CommitmentStatus,
    FinancialEventOrigin,
    FinancialEventStatus,
    FinancialEventType,
    JournalEntryType,
    LedgerAccountKind,
    PaymentMethod,
    RecognitionBasis,
    WalletPurpose,
)
from app.domain.budget_models import (
    BudgetCategoryUpdate,
    CardDraft,
    FinancialEventDraft,
    JournalEntryDraft,
    SalaryAllocationRunDraft,
    SalaryPlanTemplateDraft,
    WalletDraft,
)
from app.domain.errors import (
    BudgetPeriodError,
    CardConfigurationError,
    ConcurrentUpdateError,
    CurrencyMismatchError,
    EventReversalError,
    FinancialEventError,
    IdempotencyConflictError,
    LedgerConfigurationError,
    ManualExpenseError,
)
from app.infrastructure.database.budget_queries import (
    SqlAlchemyBudgetReadRepository,
    SqlAlchemyLedgerBalanceReadRepository,
)
from app.infrastructure.database.engine import Database

from tests.b7_support import B7_NOW, B7Services, B7World


def _cash_request(world: B7World, **overrides: object) -> ManualExpenseRequest:
    values: dict[str, object] = {
        "payment_method": PaymentMethod.CASH,
        "occurred_at": B7_NOW,
        "merchant": "早餐店",
        "amount_minor": 12_000,
        "currency": "TWD",
        "funding_wallet_id": world.cash_wallet_id,
        "client_token": "cash-token",
        "budget_category_id": world.food_id,
    }
    values.update(overrides)
    return ManualExpenseRequest(**values)  # type: ignore[arg-type]


def _card_request(world: B7World, **overrides: object) -> ManualExpenseRequest:
    values: dict[str, object] = {
        "payment_method": PaymentMethod.CREDIT_CARD,
        "occurred_at": B7_NOW,
        "merchant": "咖啡廳",
        "amount_minor": 45_000,
        "currency": "TWD",
        "funding_wallet_id": world.bank_unallocated_id,
        "client_token": "card-token",
        "budget_category_id": world.food_id,
        "card_id": world.card_id,
    }
    values.update(overrides)
    return ManualExpenseRequest(**values)  # type: ignore[arg-type]


def test_a_cash_expense_debits_the_category_and_credits_the_wallet(
    b7: B7Services, b7_world: B7World, migrated_database: Database
) -> None:
    result = b7.expenses.execute(_cash_request(b7_world))

    assert result.event.origin is FinancialEventOrigin.MANUAL
    assert result.event.recognition_basis is RecognitionBasis.OBSERVED_MANUAL
    assert result.event.event_type is FinancialEventType.PURCHASE
    assert result.event.status is FinancialEventStatus.POSTED
    assert result.entry.entry_type is JournalEntryType.EXPENSE
    assert result.commitments == ()

    with b7.factory() as unit:
        kinds = {
            unit.ledger_accounts.get(posting.ledger_account_id).kind: posting  # type: ignore[union-attr]
            for posting in result.entry.postings
        }
    assert kinds[LedgerAccountKind.EXPENSE].amount_minor == 12_000
    assert kinds[LedgerAccountKind.ASSET].amount_minor == -12_000
    assert kinds[LedgerAccountKind.ASSET].wallet_id == b7_world.cash_wallet_id

    balances = MoneyBalanceQueries(
        SqlAlchemyLedgerBalanceReadRepository(migrated_database.session_factory)
    )
    cash = next(
        item
        for item in balances.account_balances()
        if item.financial_account_id == b7_world.cash_account_id
    )
    assert cash.balance_minor == 50_000 - 12_000


def test_a_card_expense_reserves_funding_without_debiting_the_bank(
    b7: B7Services, b7_world: B7World, migrated_database: Database
) -> None:
    result = b7.expenses.execute(_card_request(b7_world))

    assert len(result.commitments) == 1
    commitment = result.commitments[0]
    assert commitment.amount_minor == 45_000
    assert commitment.status is CommitmentStatus.OPEN
    assert commitment.source_wallet_id == b7_world.bank_unallocated_id

    balances = MoneyBalanceQueries(
        SqlAlchemyLedgerBalanceReadRepository(migrated_database.session_factory)
    )
    bank = next(
        item
        for item in balances.account_balances()
        if item.financial_account_id == b7_world.bank_account_id
    )
    liability = next(
        item
        for item in balances.account_balances()
        if item.financial_account_id == b7_world.liability_account_id
    )
    # The bank has not moved: a card purchase is a liability, not a debit.
    assert bank.balance_minor == 1_000_000
    assert liability.balance_minor == -45_000


def test_redeciding_a_dedicated_expense_returns_it_to_monthly_spending(
    b7: B7Services, b7_world: B7World, migrated_database: Database
) -> None:
    """The treatment in force decides this, not the one that was replaced.

    A dedicated expense is funded from an envelope, so it is deliberately
    outside the calendar month's allowance (ADR-B078). Redoing it as an
    ordinary expense has to put it back, and the superseded dedicated row
    stays in place, so reading the *event* for any dedicated row at all kept
    it out for good (ADR-B103).
    """
    result = b7.expenses.execute(
        _card_request(
            b7_world,
            funding_wallet_id=b7_world.bank_food_wallet_id,
            clearing_wallet_id=b7_world.bank_clearing_wallet_id,
        )
    )
    budget = BudgetQueries(
        SqlAlchemyBudgetReadRepository(
            migrated_database.session_factory, local_timezone="Asia/Taipei"
        )
    )
    assert b7.spending.daily(day=B7_NOW.date()).total_rows == 0
    assert budget.month(year=2026, month=8, currency="TWD").total_spend_minor == 0

    undone = b7.redo.execute(
        RedoAllocationRequest(
            financial_event_id=result.event.id,
            expected_version=result.event.version,
        )
    )
    b7.allocate.execute(
        AllocateFinancialEventRequest(
            financial_event_id=result.event.id,
            expected_version=undone.event.version,
            client_token="dedicated-redecided",
            lines=(
                AllocationLine(
                    amount_minor=45_000,
                    funding_wallet_id=b7_world.bank_unallocated_id,
                    budget_category_id=b7_world.food_id,
                ),
            ),
        )
    )

    assert b7.spending.daily(day=B7_NOW.date()).total_rows == 1
    assert budget.month(year=2026, month=8, currency="TWD").total_spend_minor == 45_000
    detail = b7.spending.event(result.event.id)
    assert detail is not None
    assert detail.is_dedicated_wallet_spending is False


def test_dedicated_card_spending_moves_the_envelope_but_not_monthly_spending(
    b7: B7Services, b7_world: B7World, migrated_database: Database
) -> None:
    result = b7.expenses.execute(
        _card_request(
            b7_world,
            funding_wallet_id=b7_world.bank_food_wallet_id,
            clearing_wallet_id=b7_world.bank_clearing_wallet_id,
        )
    )

    with b7.factory() as unit:
        route = unit.allocations.get_dedicated(result.allocations[0].id)
        assert route is not None
        assert route.clearing_wallet_id == b7_world.bank_clearing_wallet_id
        assert result.commitments[0].source_wallet_id == (
            b7_world.bank_clearing_wallet_id
        )
    balances = MoneyBalanceQueries(
        SqlAlchemyLedgerBalanceReadRepository(migrated_database.session_factory)
    ).account_balances()
    bank = next(
        item
        for item in balances
        if item.financial_account_id == b7_world.bank_account_id
    )
    wallet_balances = {item.wallet_id: item.balance_minor for item in bank.wallets}
    assert bank.balance_minor == 1_000_000
    assert wallet_balances[b7_world.bank_food_wallet_id] == 255_000
    assert wallet_balances[b7_world.bank_clearing_wallet_id] == 45_000
    assert len(result.entry.postings) == 4

    daily = b7.spending.daily(day=B7_NOW.date())
    budget = BudgetQueries(
        SqlAlchemyBudgetReadRepository(
            migrated_database.session_factory, local_timezone="Asia/Taipei"
        )
    ).month(year=2026, month=8, currency="TWD")
    assert daily.total_rows == 0
    assert daily.totals == ()
    assert budget.total_spend_minor == 0

    refund = b7.movements.refund(
        ManualRefundRequest(
            original_event_id=result.event.id,
            occurred_at=B7_NOW + timedelta(hours=1),
            amount_minor=45_000,
            client_token="dedicated-refund",
        )
    )
    assert len(refund.entry.postings) == 4
    after = MoneyBalanceQueries(
        SqlAlchemyLedgerBalanceReadRepository(migrated_database.session_factory)
    ).account_balances()
    bank_after = next(
        item for item in after if item.financial_account_id == b7_world.bank_account_id
    )
    after_wallets = {item.wallet_id: item.balance_minor for item in bank_after.wallets}
    assert after_wallets[b7_world.bank_food_wallet_id] == 300_000
    assert after_wallets[b7_world.bank_clearing_wallet_id] == 0
    assert b7.spending.daily(day=B7_NOW.date()).total_rows == 0


def test_dedicated_treatment_rejects_cash_and_a_non_hub_destination(
    b7: B7Services, b7_world: B7World
) -> None:
    with pytest.raises(ManualExpenseError, match="only available for card"):
        b7.expenses.execute(
            _cash_request(
                b7_world,
                clearing_wallet_id=b7_world.bank_clearing_wallet_id,
            )
        )

    with pytest.raises(LedgerConfigurationError, match="clearing-hub"):
        b7.expenses.execute(
            _card_request(
                b7_world,
                client_token="not-a-hub",
                clearing_wallet_id=b7_world.bank_unallocated_id,
            )
        )

    with b7.factory() as unit:
        other_hub = unit.wallets.add(
            WalletDraft(
                financial_account_id=b7_world.cash_account_id,
                name="其他帳單暫存",
                purpose=WalletPurpose.CLEARING_HUB,
            )
        )
        unit.commit()
    with pytest.raises(FinancialEventError, match="real transfer"):
        b7.expenses.execute(
            _card_request(
                b7_world,
                client_token="cross-account-hub",
                funding_wallet_id=b7_world.bank_food_wallet_id,
                clearing_wallet_id=other_hub.id,
            )
        )


def test_one_event_cannot_mix_monthly_and_dedicated_shares(
    b7: B7Services, b7_world: B7World
) -> None:
    with b7.factory() as unit:
        event = unit.financial_events.add(
            FinancialEventDraft(
                origin=FinancialEventOrigin.MANUAL,
                event_type=FinancialEventType.PURCHASE,
                payment_method=PaymentMethod.CREDIT_CARD,
                recognition_basis=RecognitionBasis.OBSERVED_MANUAL,
                occurred_at=B7_NOW,
                amount_minor=45_000,
                currency="TWD",
                idempotency_key="mixed-treatment",
                merchant="測試混合",
                card_id=b7_world.card_id,
            )
        )
        unit.commit()

    with pytest.raises(FinancialEventError, match="cannot mix"):
        b7.allocate.execute(
            AllocateFinancialEventRequest(
                financial_event_id=event.id,
                expected_version=event.version,
                client_token="mixed-treatment",
                lines=(
                    AllocationLine(
                        amount_minor=20_000,
                        funding_wallet_id=b7_world.bank_food_wallet_id,
                        budget_category_id=b7_world.food_id,
                        clearing_wallet_id=b7_world.bank_clearing_wallet_id,
                    ),
                    AllocationLine(
                        amount_minor=25_000,
                        funding_wallet_id=b7_world.bank_unallocated_id,
                        budget_category_id=b7_world.transport_id,
                    ),
                ),
            )
        )

    with b7.factory() as unit:
        assert unit.allocations.list_for_event(event.id) == ()
        assert unit.journal.get_by_financial_event(event.id) is None


def test_replay_compares_the_dedicated_clearing_destination(
    b7: B7Services, b7_world: B7World
) -> None:
    request = _card_request(
        b7_world,
        funding_wallet_id=b7_world.bank_food_wallet_id,
        clearing_wallet_id=b7_world.bank_clearing_wallet_id,
    )
    first = b7.expenses.execute(request)
    replay = b7.expenses.execute(request)
    assert replay.entry.id == first.entry.id

    with pytest.raises(IdempotencyConflictError, match="different split"):
        b7.expenses.execute(
            _card_request(
                b7_world,
                clearing_wallet_id=None,
            )
        )


def test_dedicated_spending_may_overdraw_its_source_envelope(
    b7: B7Services, b7_world: B7World, migrated_database: Database
) -> None:
    b7.expenses.execute(
        _card_request(
            b7_world,
            amount_minor=350_000,
            client_token="overdraw-dedicated",
            funding_wallet_id=b7_world.bank_food_wallet_id,
            clearing_wallet_id=b7_world.bank_clearing_wallet_id,
        )
    )
    balances = MoneyBalanceQueries(
        SqlAlchemyLedgerBalanceReadRepository(migrated_database.session_factory)
    ).account_balances()
    bank = next(
        item
        for item in balances
        if item.financial_account_id == b7_world.bank_account_id
    )
    wallets = {item.wallet_id: item.balance_minor for item in bank.wallets}
    assert wallets[b7_world.bank_food_wallet_id] == -50_000
    assert wallets[b7_world.bank_clearing_wallet_id] == 350_000


def test_a_replayed_submission_returns_the_first_expense(
    b7: B7Services, b7_world: B7World
) -> None:
    first = b7.expenses.execute(_cash_request(b7_world))
    second = b7.expenses.execute(_cash_request(b7_world))

    assert second.event.id == first.event.id
    assert second.entry.id == first.entry.id
    with b7.factory() as unit:
        assert len(unit.allocations.list_for_event(first.event.id)) == 1


def test_a_note_can_be_added_changed_and_cleared_without_rewriting_the_ledger(
    b7: B7Services, b7_world: B7World
) -> None:
    result = b7.expenses.execute(_cash_request(b7_world))
    notes = UpdateFinancialEventNote(b7.factory)
    with b7.factory() as unit:
        original_entry = unit.journal.get_by_financial_event(result.event.id)
        original_allocations = unit.allocations.list_for_event(result.event.id)

    added = notes.execute(
        UpdateFinancialEventNoteRequest(
            financial_event_id=result.event.id,
            expected_version=result.event.version,
            note="  和同事吃早餐  ",
        )
    )
    changed = notes.execute(
        UpdateFinancialEventNoteRequest(
            financial_event_id=added.id,
            expected_version=added.version,
            note="改成自己吃早餐",
        )
    )
    cleared = notes.execute(
        UpdateFinancialEventNoteRequest(
            financial_event_id=changed.id,
            expected_version=changed.version,
            note="   ",
        )
    )

    assert added.note == "和同事吃早餐"
    assert changed.note == "改成自己吃早餐"
    assert cleared.note is None
    with b7.factory() as unit:
        assert unit.journal.get_by_financial_event(result.event.id) == original_entry
        assert unit.allocations.list_for_event(result.event.id) == original_allocations


def test_a_stale_note_edit_never_overwrites_the_newer_note(
    b7: B7Services, b7_world: B7World
) -> None:
    result = b7.expenses.execute(_cash_request(b7_world))
    notes = UpdateFinancialEventNote(b7.factory)
    notes.execute(
        UpdateFinancialEventNoteRequest(
            financial_event_id=result.event.id,
            expected_version=result.event.version,
            note="先儲存的內容",
        )
    )

    with pytest.raises(ConcurrentUpdateError):
        notes.execute(
            UpdateFinancialEventNoteRequest(
                financial_event_id=result.event.id,
                expected_version=result.event.version,
                note="過期頁面內容",
            )
        )

    with b7.factory() as unit:
        stored = unit.financial_events.get(result.event.id)
    assert stored is not None
    assert stored.note == "先儲存的內容"


def test_an_invalid_note_edit_writes_nothing(b7: B7Services, b7_world: B7World) -> None:
    result = b7.expenses.execute(_cash_request(b7_world))

    with pytest.raises(
        ValueError, match="financial event note must be at most 500 characters"
    ):
        UpdateFinancialEventNote(b7.factory).execute(
            UpdateFinancialEventNoteRequest(
                financial_event_id=result.event.id,
                expected_version=result.event.version,
                note="字" * 501,
            )
        )

    with b7.factory() as unit:
        stored = unit.financial_events.get(result.event.id)
    assert stored is not None
    assert stored.note is None
    assert stored.version == result.event.version


@pytest.mark.parametrize(
    "overrides",
    [
        {"amount_minor": 12_001},
        {"merchant": "另一家店"},
        {"occurred_at": B7_NOW + timedelta(minutes=1)},
        {"currency": "JPY"},
        {"note": "另一段備註"},
    ],
)
def test_the_same_token_with_a_different_payload_is_a_conflict(
    b7: B7Services, b7_world: B7World, overrides: dict[str, object]
) -> None:
    b7.expenses.execute(_cash_request(b7_world))

    with pytest.raises(IdempotencyConflictError):
        b7.expenses.execute(_cash_request(b7_world, **overrides))


def test_the_same_token_with_a_different_wallet_is_a_conflict(
    b7: B7Services, b7_world: B7World
) -> None:
    b7.expenses.execute(_cash_request(b7_world))

    with pytest.raises(IdempotencyConflictError):
        b7.expenses.execute(
            _cash_request(b7_world, funding_wallet_id=b7_world.bank_unallocated_id)
        )


def test_cash_may_not_be_spent_from_a_bank_wallet(
    b7: B7Services, b7_world: B7World
) -> None:
    with pytest.raises(ManualExpenseError):
        b7.expenses.execute(
            _cash_request(b7_world, funding_wallet_id=b7_world.bank_unallocated_id)
        )


def test_a_card_expense_requires_a_card_and_cash_forbids_one(
    b7: B7Services, b7_world: B7World
) -> None:
    with pytest.raises(ManualExpenseError):
        b7.expenses.execute(_card_request(b7_world, card_id=None))
    with pytest.raises(ManualExpenseError):
        b7.expenses.execute(_cash_request(b7_world, card_id=b7_world.card_id))


def test_a_bank_transfer_payment_method_is_refused(
    b7: B7Services, b7_world: B7World
) -> None:
    with pytest.raises(ManualExpenseError):
        b7.expenses.execute(
            _cash_request(b7_world, payment_method=PaymentMethod.BANK_ACCOUNT)
        )


def test_a_wallet_in_another_currency_is_refused(
    b7: B7Services, b7_world: B7World
) -> None:
    with pytest.raises(CurrencyMismatchError):
        b7.expenses.execute(_cash_request(b7_world, currency="JPY"))


def test_an_unknown_wallet_is_refused(b7: B7Services, b7_world: B7World) -> None:
    with pytest.raises(LedgerConfigurationError):
        b7.expenses.execute(_cash_request(b7_world, funding_wallet_id=99_999))


def test_an_unconfigured_card_cannot_post(b7: B7Services, b7_world: B7World) -> None:
    with b7.factory() as unit:
        card = unit.cards.add(
            CardDraft(
                gmail_account_id=b7_world.gmail_account_id,
                issuer_code="other_bank",
                display_name="未設定卡",
                last_four="9999",
            )
        )
        unit.commit()
        unconfigured_id = card.id

    with pytest.raises(CardConfigurationError):
        b7.expenses.execute(_card_request(b7_world, card_id=unconfigured_id))


@pytest.mark.parametrize("amount", [True, 12.5, "12000"])
def test_a_non_integer_amount_is_rejected(
    b7: B7Services, b7_world: B7World, amount: object
) -> None:
    with pytest.raises(TypeError):
        b7.expenses.execute(_cash_request(b7_world, amount_minor=amount))


def test_a_failed_allocation_leaves_no_event_behind(
    b7: B7Services, b7_world: B7World
) -> None:
    """A deactivated category makes allocation fail after the event was staged."""
    with b7.factory() as unit:
        category = unit.budget_categories.get(b7_world.transport_id)
        assert category is not None
        unit.budget_categories.save(
            BudgetCategoryUpdate(
                category_id=category.id,
                expected_version=category.version,
                name=category.name,
                parent_id=None,
                is_active=False,
            )
        )
        unit.commit()

    with pytest.raises(BudgetPeriodError):
        b7.expenses.execute(
            _cash_request(b7_world, budget_category_id=b7_world.transport_id)
        )

    with b7.factory() as unit:
        assert (
            unit.financial_events.get_by_idempotency_key("manual_expense:cash-token")
            is None
        )
        assert (
            unit.journal.get_by_idempotency_key(
                "allocation:financial_event:1:cash-token"
            )
            is None
        )


def test_reversing_a_card_expense_releases_its_reservation(
    b7: B7Services, b7_world: B7World, migrated_database: Database
) -> None:
    posted = b7.expenses.execute(_card_request(b7_world))

    result = b7.reversal.execute(
        ReverseEventRequest(
            financial_event_id=posted.event.id,
            expected_version=posted.event.version,
        )
    )

    assert result.event.status is FinancialEventStatus.REVERSED
    assert result.reversal_entry.entry_type is JournalEntryType.REVERSAL
    assert result.reversal_entry.reversal_of_id == posted.entry.id
    assert result.reversal_entry.effective_at == posted.entry.effective_at
    assert all(item.outstanding_minor == 0 for item in result.commitments)
    assert all(item.status is CommitmentStatus.CANCELLED for item in result.commitments)

    balances = MoneyBalanceQueries(
        SqlAlchemyLedgerBalanceReadRepository(migrated_database.session_factory)
    )
    liability = next(
        item
        for item in balances.account_balances()
        if item.financial_account_id == b7_world.liability_account_id
    )
    assert liability.balance_minor == 0


def test_the_original_event_and_its_entry_are_never_rewritten(
    b7: B7Services, b7_world: B7World
) -> None:
    posted = b7.expenses.execute(_cash_request(b7_world))
    original_signature = sorted(
        (item.ledger_account_id, item.amount_minor) for item in posted.entry.postings
    )

    b7.reversal.execute(
        ReverseEventRequest(
            financial_event_id=posted.event.id,
            expected_version=posted.event.version,
        )
    )

    with b7.factory() as unit:
        entry = unit.journal.get(posted.entry.id)
        event = unit.financial_events.get(posted.event.id)
        assert entry is not None and event is not None
        assert (
            sorted(
                (item.ledger_account_id, item.amount_minor) for item in entry.postings
            )
            == original_signature
        )
        assert event.amount_minor == posted.event.amount_minor
        assert event.merchant == posted.event.merchant
        assert len(unit.allocations.list_for_event(event.id)) == 1


def test_a_reversal_retry_replays_even_with_a_stale_version(
    b7: B7Services, b7_world: B7World
) -> None:
    posted = b7.expenses.execute(_cash_request(b7_world))
    first = b7.reversal.execute(
        ReverseEventRequest(
            financial_event_id=posted.event.id,
            expected_version=posted.event.version,
        )
    )

    second = b7.reversal.execute(
        ReverseEventRequest(
            financial_event_id=posted.event.id,
            # Deliberately the version the page was rendered with, which the
            # first reversal already moved.
            expected_version=posted.event.version,
        )
    )

    assert second.reversal_entry.id == first.reversal_entry.id
    assert second.event.status is FinancialEventStatus.REVERSED


def test_a_stale_version_before_any_reversal_is_a_conflict(
    b7: B7Services, b7_world: B7World
) -> None:
    posted = b7.expenses.execute(_cash_request(b7_world))

    with pytest.raises(ConcurrentUpdateError):
        b7.reversal.execute(
            ReverseEventRequest(
                financial_event_id=posted.event.id,
                expected_version=posted.event.version + 5,
            )
        )


def test_only_a_posted_manual_event_may_be_reversed(
    b7: B7Services, b7_world: B7World
) -> None:
    posted = b7.expenses.execute(_cash_request(b7_world))
    b7.reversal.execute(
        ReverseEventRequest(
            financial_event_id=posted.event.id,
            expected_version=posted.event.version,
        )
    )
    with b7.factory() as unit:
        gmail_event = unit.financial_events.add(
            FinancialEventDraft(
                origin=FinancialEventOrigin.GMAIL_TRANSACTION,
                event_type=FinancialEventType.PURCHASE,
                payment_method=PaymentMethod.CREDIT_CARD,
                recognition_basis=RecognitionBasis.GMAIL_NOTIFICATION,
                occurred_at=datetime(2026, 8, 9, 5, 0, tzinfo=UTC),
                amount_minor=1_000,
                currency="TWD",
                idempotency_key="gmail_transaction:9999",
            )
        )
        unit.commit()
        gmail_event_id = gmail_event.id

    with pytest.raises(EventReversalError):
        b7.reversal.execute(
            ReverseEventRequest(financial_event_id=gmail_event_id, expected_version=1)
        )


def test_the_deposit_a_live_salary_run_used_cannot_be_reversed(
    b7: B7Services, b7_world: B7World
) -> None:
    """B5 froze a whole month from this deposit; B7 must not cancel it."""
    with b7.factory() as unit:
        deposit = unit.financial_events.add(
            FinancialEventDraft(
                origin=FinancialEventOrigin.MANUAL,
                event_type=FinancialEventType.DEPOSIT,
                payment_method=PaymentMethod.BANK_ACCOUNT,
                recognition_basis=RecognitionBasis.OBSERVED_MANUAL,
                occurred_at=B7_NOW,
                amount_minor=100_000,
                currency="TWD",
                idempotency_key="salary_receipt:test",
                status=FinancialEventStatus.POSTED,
            )
        )
        entry = unit.journal.add(
            JournalEntryDraft(
                entry_type=JournalEntryType.SALARY_RECEIPT,
                effective_at=B7_NOW,
                recorded_at=B7_NOW,
                idempotency_key="salary_receipt:financial_event:test",
                description="",
                postings=(),
                financial_event_id=deposit.id,
            )
        )
        template = unit.salary_templates.add(
            SalaryPlanTemplateDraft(
                name="測試範本",
                currency="TWD",
                source_account_id=b7_world.bank_account_id,
            )
        )
        period = unit.budget_periods.get_or_create(year=2026, month=8, currency="TWD")
        unit.salary_runs.add(
            SalaryAllocationRunDraft(
                template_id=template.id,
                template_name_snapshot=template.name,
                template_version=template.version,
                budget_period_id=period.id,
                salary_event_id=deposit.id,
                source_account_id=b7_world.bank_account_id,
                source_wallet_id=b7_world.bank_unallocated_id,
                currency="TWD",
                generation=1,
                salary_amount_minor=100_000,
                planned_amount_minor=0,
                remainder_minor=100_000,
                subscription_evidence_schema_version=1,
                subscription_evidence_json="{}",
                generated_at=B7_NOW,
            )
        )
        unit.commit()
        event_id, version = deposit.id, deposit.version
        assert entry.id

    with pytest.raises(EventReversalError, match="salary"):
        b7.reversal.execute(
            ReverseEventRequest(financial_event_id=event_id, expected_version=version)
        )

    with b7.factory() as unit:
        still = unit.financial_events.get(event_id)
        assert still is not None
        assert still.status is FinancialEventStatus.POSTED

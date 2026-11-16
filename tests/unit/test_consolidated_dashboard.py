"""Pure B8 aggregation rules: currency safety and account status."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

from app.application.queries.budget import BudgetMonthView
from app.application.queries.consolidated import (
    ConsolidatedMoneyOverview,
    OverviewAccount,
    currency_positions,
    reconciliation_state,
)
from app.application.queries.dashboard import DashboardSnapshot
from app.application.queries.spending import (
    AccountRow,
    AccountSnapshotRow,
    AccountWalletRow,
    DailyActivityView,
    SourceLinkReviewView,
)
from app.domain.budget_enums import (
    BalanceSnapshotSource,
    FinancialAccountType,
    WalletPurpose,
)
from app.domain.budget_models import BalanceSnapshot, FinancialAccount

NOW = datetime(2026, 8, 10, 4, tzinfo=UTC)


def _account(
    identifier: int,
    *,
    currency: str,
    account_type: FinancialAccountType,
    balance_minor: int,
    reserved_minor: int = 0,
    observed_minor: int | None = None,
) -> AccountRow:
    account = FinancialAccount(
        id=identifier,
        institution_code=f"test_bank_{identifier}",
        display_name=f"測試帳戶 {identifier}",
        account_type=account_type,
        currency=currency,
        masked_reference=None,
        is_active=True,
        version=1,
        created_at=NOW,
        updated_at=NOW,
    )
    wallet = (
        (
            AccountWalletRow(
                wallet_id=identifier,
                name="測試錢包",
                currency=currency,
                balance_minor=balance_minor,
                reserved_minor=reserved_minor,
                purpose=WalletPurpose.GENERAL,
            ),
        )
        if account_type is not FinancialAccountType.CARD_LIABILITY
        else ()
    )
    latest = None
    if observed_minor is not None:
        latest = AccountSnapshotRow(
            snapshot=BalanceSnapshot(
                id=identifier,
                financial_account_id=identifier,
                observed_balance_minor=observed_minor,
                observed_at=NOW,
                source=BalanceSnapshotSource.MANUAL_BANK_APP,
                note=None,
                idempotency_key=f"test:{identifier}",
                created_at=NOW,
                updated_at=NOW,
            ),
            ledger_balance_minor=balance_minor,
            currency=currency,
            account_display_name=account.display_name,
        )
    return AccountRow(
        account=account,
        balance_minor=balance_minor,
        wallets=wallet,
        latest_snapshot=latest,
    )


def test_positions_never_mix_currencies_or_liabilities_into_available_money() -> None:
    rows = (
        _account(
            1,
            currency="TWD",
            account_type=FinancialAccountType.CHECKING,
            balance_minor=100_000,
            reserved_minor=25_000,
        ),
        _account(
            2,
            currency="TWD",
            account_type=FinancialAccountType.CARD_LIABILITY,
            balance_minor=-30_000,
        ),
        _account(
            3,
            currency="JPY",
            account_type=FinancialAccountType.SAVINGS,
            balance_minor=8_000,
            reserved_minor=500,
        ),
    )

    positions = {item.currency: item for item in currency_positions(rows)}

    assert positions["TWD"].asset_minor == 100_000
    assert positions["TWD"].reserved_minor == 25_000
    assert positions["TWD"].available_minor == 75_000
    assert positions["TWD"].card_liability_minor == -30_000
    assert positions["JPY"].available_minor == 7_500
    assert positions["JPY"].card_liability_minor == 0


def test_reconciliation_state_uses_evidence_and_excludes_liabilities() -> None:
    assert (
        reconciliation_state(
            _account(
                1,
                currency="TWD",
                account_type=FinancialAccountType.CHECKING,
                balance_minor=10_000,
            )
        )
        == "not_observed"
    )
    assert (
        reconciliation_state(
            _account(
                2,
                currency="TWD",
                account_type=FinancialAccountType.CHECKING,
                balance_minor=10_000,
                observed_minor=10_000,
            )
        )
        == "reconciled"
    )
    assert (
        reconciliation_state(
            _account(
                3,
                currency="TWD",
                account_type=FinancialAccountType.CHECKING,
                balance_minor=10_000,
                observed_minor=9_000,
            )
        )
        == "difference"
    )
    assert (
        reconciliation_state(
            _account(
                4,
                currency="TWD",
                account_type=FinancialAccountType.CARD_LIABILITY,
                balance_minor=-2_000,
            )
        )
        == "ledger_only"
    )


def _overview(
    *, rows: tuple[object, ...], unconfirmed_today: int, needing_allocation: int
) -> ConsolidatedMoneyOverview:
    """Build an overview whose only interesting parts are the waiting queues."""
    daily = SimpleNamespace(rows=rows)
    return ConsolidatedMoneyOverview(
        generated_at=NOW,
        positions=(),
        accounts=(),
        today=cast(DailyActivityView, daily),
        budget=cast(
            BudgetMonthView,
            SimpleNamespace(events_needing_allocation=needing_allocation),
        ),
        source_links=cast(SourceLinkReviewView, SimpleNamespace(pending_total=0)),
        parsed_transactions=cast(
            DashboardSnapshot,
            SimpleNamespace(unconfirmed_today_count=unconfirmed_today),
        ),
    )


def test_an_empty_day_with_queued_work_is_waiting_rather_than_spendless() -> None:
    waiting = _overview(rows=(), unconfirmed_today=5, needing_allocation=28)

    assert waiting.today_is_waiting_on_review
    assert waiting.today_waiting_confirmation == 5
    assert waiting.today_waiting_allocation == 28


def test_an_empty_day_with_nothing_queued_really_is_spendless() -> None:
    quiet = _overview(rows=(), unconfirmed_today=0, needing_allocation=0)

    assert not quiet.today_is_waiting_on_review


def test_a_day_that_already_posted_something_is_never_reported_as_waiting() -> None:
    """Queued work elsewhere must not annotate a day that does show spending."""
    posted = _overview(
        rows=("one posted row",), unconfirmed_today=5, needing_allocation=28
    )

    assert not posted.today_is_waiting_on_review


def _overview_with(accounts: tuple[OverviewAccount, ...]) -> ConsolidatedMoneyOverview:
    return ConsolidatedMoneyOverview(
        generated_at=NOW,
        positions=(),
        accounts=accounts,
        today=cast(DailyActivityView, SimpleNamespace(rows=())),
        budget=cast(BudgetMonthView, SimpleNamespace(events_needing_allocation=0)),
        source_links=cast(SourceLinkReviewView, SimpleNamespace(pending_total=0)),
        parsed_transactions=cast(
            DashboardSnapshot, SimpleNamespace(unconfirmed_today_count=0)
        ),
    )


def test_a_card_debt_is_never_listed_among_the_accounts_that_hold_money() -> None:
    """Reading a liability beside a bank balance invites spending it."""
    bank = OverviewAccount(
        row=_account(
            1,
            currency="TWD",
            account_type=FinancialAccountType.SAVINGS,
            balance_minor=500_000,
        ),
        state="reconciled",
    )
    card = OverviewAccount(
        row=_account(
            2,
            currency="TWD",
            account_type=FinancialAccountType.CARD_LIABILITY,
            balance_minor=-120_000,
        ),
        state="ledger_only",
    )
    overview = _overview_with((bank, card))

    assert overview.asset_accounts == (bank,)
    assert overview.card_liability_accounts == (card,)

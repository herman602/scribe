"""B7 money CLI: daily, expenses, movements, matching, and reconciliation."""

from __future__ import annotations

from pathlib import Path

import pytest
from app.presentation.cli.app import cli
from typer.testing import CliRunner, Result

runner = CliRunner()

OCCURRED_AT = "2026-08-09T12:30"
LOCAL_DAY = "2026-08-09"


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return an initialized environment with a cash account and a category."""
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(cli, ["init"]).exit_code == 0
    assert (
        runner.invoke(
            cli,
            [
                "money",
                "account-add",
                "--institution",
                "wallet",
                "--name",
                "皮夾",
                "--type",
                "cash",
                "--currency",
                "twd",
            ],
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(
            cli,
            [
                "money",
                "opening-balance",
                "--account-id",
                "1",
                "--amount-minor",
                "50000",
                "--observed-at",
                "2026-08-09T08:00",
                "--split",
                "尚未分配=50000",
            ],
        ).exit_code
        == 0
    )
    assert runner.invoke(cli, ["money", "category-add", "飲食"]).exit_code == 0
    return tmp_path


def _expense(*, token: str = "cli-1", amount: str = "12000") -> Result:
    return runner.invoke(
        cli,
        [
            "money",
            "expense-add",
            "--merchant",
            "早餐店",
            "--amount-minor",
            amount,
            "--currency",
            "TWD",
            "--funding-wallet-id",
            "1",
            "--budget-category-id",
            "1",
            "--client-token",
            token,
            "--occurred-at",
            OCCURRED_AT,
        ],
    )


def test_money_help_lists_every_b7_command(world: Path) -> None:
    result = runner.invoke(cli, ["money", "--help"])

    assert result.exit_code == 0
    for command in (
        "daily",
        "expense-add",
        "bank-movement-add",
        "account-transfer",
        "refund-add",
        "event-reverse",
        "source-links",
        "source-link-confirm",
        "source-link-reject",
        "source-link-separate",
        "snapshot-add",
        "reconcile-show",
        "reconciliation-adjust",
    ):
        assert command in result.stdout


def test_expense_add_posts_and_daily_shows_it(world: Path) -> None:
    added = _expense()

    assert added.exit_code == 0, added.stdout
    assert "event_id=1" in added.stdout
    assert "entry_id=" in added.stdout

    daily = runner.invoke(cli, ["money", "daily", "--day", LOCAL_DAY])

    assert daily.exit_code == 0
    assert "早餐店" in daily.stdout
    assert "net_minor=12000" in daily.stdout
    assert "method=cash" in daily.stdout


def test_expense_add_replays_the_same_token(world: Path) -> None:
    first = _expense()
    second = _expense()

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert "event_id=1" in second.stdout

    daily = runner.invoke(cli, ["money", "daily", "--day", LOCAL_DAY])
    assert daily.stdout.count("entry_id=") == 1


def test_expense_add_refuses_a_conflicting_token(world: Path) -> None:
    _expense()

    conflict = _expense(amount="13000")

    assert conflict.exit_code == 1
    assert "Could not record the expense" in conflict.stderr


def test_the_daily_filter_selects_one_instrument(world: Path) -> None:
    _expense()

    card_only = runner.invoke(
        cli, ["money", "daily", "--day", LOCAL_DAY, "--method", "card"]
    )

    assert card_only.exit_code == 0
    assert "No cash or card spending on this day." in card_only.stdout


def test_event_reverse_cancels_a_posted_expense(world: Path) -> None:
    _expense()

    reversed_result = runner.invoke(
        cli, ["money", "event-reverse", "1", "--expected-version", "2"]
    )

    assert reversed_result.exit_code == 0, reversed_result.stdout
    assert "status=reversed" in reversed_result.stdout
    daily = runner.invoke(cli, ["money", "daily", "--day", LOCAL_DAY])
    assert "net_minor=0" in daily.stdout


def test_a_bank_movement_is_recorded_with_its_own_entry_type(world: Path) -> None:
    assert (
        runner.invoke(
            cli,
            [
                "money",
                "account-add",
                "--institution",
                "test_bank",
                "--name",
                "測試支票帳戶",
                "--type",
                "checking",
                "--currency",
                "twd",
            ],
        ).exit_code
        == 0
    )

    interest = runner.invoke(
        cli,
        [
            "money",
            "bank-movement-add",
            "--kind",
            "interest_income",
            "--description",
            "活期利息",
            "--amount-minor",
            "1200",
            "--currency",
            "TWD",
            "--wallet-id",
            "2",
            "--client-token",
            "cli-interest",
            "--occurred-at",
            OCCURRED_AT,
        ],
    )

    assert interest.exit_code == 0, interest.stdout
    assert "entry_type=income" in interest.stdout
    assert "type=interest_income" in interest.stdout


def test_a_cash_withdrawal_is_one_expense_and_never_funds_the_cash_wallet(
    world: Path,
) -> None:
    assert (
        runner.invoke(
            cli,
            [
                "money",
                "account-add",
                "--institution",
                "test_bank",
                "--name",
                "國泰薪轉測試帳戶",
                "--type",
                "checking",
                "--currency",
                "twd",
            ],
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(
            cli,
            [
                "money",
                "opening-balance",
                "--account-id",
                "2",
                "--amount-minor",
                "200000",
                "--observed-at",
                "2026-08-09T08:00",
                "--split",
                "尚未分配=200000",
            ],
        ).exit_code
        == 0
    )

    withdrawal = runner.invoke(
        cli,
        [
            "money",
            "bank-movement-add",
            "--kind",
            "cash_withdrawal_spending",
            "--description",
            "提款列入生活費",
            "--amount-minor",
            "100000",
            "--currency",
            "TWD",
            "--wallet-id",
            "2",
            "--budget-category-id",
            "1",
            "--client-token",
            "cli-cash-withdrawal",
            "--occurred-at",
            OCCURRED_AT,
        ],
    )

    assert withdrawal.exit_code == 0, withdrawal.stdout
    assert "type=withdrawal" in withdrawal.stdout
    assert "entry_type=expense" in withdrawal.stdout
    daily = runner.invoke(cli, ["money", "daily", "--day", LOCAL_DAY])
    assert "提款列入生活費" in daily.stdout
    assert "net_minor=100000" in daily.stdout
    assert "method=cash" in daily.stdout
    balances = runner.invoke(cli, ["money", "balances"])
    assert "皮夾" in balances.stdout
    assert "皮夾                   TWD 500" in balances.stdout


def test_a_transfer_is_recorded_as_an_internal_transfer(world: Path) -> None:
    assert (
        runner.invoke(
            cli, ["money", "wallet-add", "--account-id", "1", "--name", "零用"]
        ).exit_code
        == 0
    )

    transfer = runner.invoke(
        cli,
        [
            "money",
            "account-transfer",
            "--source-wallet-id",
            "1",
            "--destination-wallet-id",
            "2",
            "--amount-minor",
            "10000",
            "--currency",
            "TWD",
            "--description",
            "分到零用",
            "--client-token",
            "cli-transfer",
            "--occurred-at",
            OCCURRED_AT,
        ],
    )

    assert transfer.exit_code == 0, transfer.stdout
    assert "entry_type=internal_transfer" in transfer.stdout
    daily = runner.invoke(cli, ["money", "daily", "--day", LOCAL_DAY])
    assert "分到零用" not in daily.stdout


def test_snapshot_and_reconcile_report_the_difference(world: Path) -> None:
    snapshot = runner.invoke(
        cli,
        [
            "money",
            "snapshot-add",
            "--account-id",
            "1",
            "--observed-balance-minor",
            "55000",
            "--observed-at",
            "2026-08-09T20:00",
            "--client-token",
            "cli-snap",
        ],
    )

    assert snapshot.exit_code == 0, snapshot.stdout
    assert "snapshot_id=1" in snapshot.stdout
    assert "A snapshot is evidence only; no balance moved." in snapshot.stdout

    show = runner.invoke(cli, ["money", "reconcile-show"])

    assert show.exit_code == 0
    assert "difference_minor=5000" in show.stdout


def test_a_negative_observed_balance_is_accepted(world: Path) -> None:
    snapshot = runner.invoke(
        cli,
        [
            "money",
            "snapshot-add",
            "--account-id",
            "1",
            "--observed-balance-minor",
            "-1000",
            "--observed-at",
            "2026-08-09T20:00",
            "--client-token",
            "cli-negative",
        ],
    )

    assert snapshot.exit_code == 0, snapshot.stdout
    assert "observed_minor=-1000" in snapshot.stdout


def test_a_reasoned_adjustment_closes_the_difference(world: Path) -> None:
    runner.invoke(
        cli,
        [
            "money",
            "snapshot-add",
            "--account-id",
            "1",
            "--observed-balance-minor",
            "55000",
            "--observed-at",
            "2026-08-09T20:00",
            "--client-token",
            "cli-snap",
        ],
    )

    adjust = runner.invoke(
        cli,
        [
            "money",
            "reconciliation-adjust",
            "1",
            "--expected-difference-minor",
            "5000",
            "--reason",
            "找不到來源",
            "--client-token",
            "cli-adjust",
        ],
    )

    assert adjust.exit_code == 0, adjust.stdout
    assert "difference_minor=5000" in adjust.stdout
    assert (
        "difference_minor=0" in runner.invoke(cli, ["money", "reconcile-show"]).stdout
    )


def test_a_stale_adjustment_is_refused(world: Path) -> None:
    runner.invoke(
        cli,
        [
            "money",
            "snapshot-add",
            "--account-id",
            "1",
            "--observed-balance-minor",
            "55000",
            "--observed-at",
            "2026-08-09T20:00",
            "--client-token",
            "cli-snap",
        ],
    )

    adjust = runner.invoke(
        cli,
        [
            "money",
            "reconciliation-adjust",
            "1",
            "--expected-difference-minor",
            "4000",
            "--reason",
            "舊的差額",
            "--client-token",
            "cli-adjust",
        ],
    )

    assert adjust.exit_code == 1
    assert "Could not adjust the difference" in adjust.stderr


def test_source_links_reports_an_empty_queue(world: Path) -> None:
    result = runner.invoke(cli, ["money", "source-links"])

    assert result.exit_code == 0
    assert "pending=0" in result.stdout
    assert "No pairing is waiting for a decision." in result.stdout


def test_confirming_an_unknown_link_fails_cleanly(world: Path) -> None:
    result = runner.invoke(cli, ["money", "source-link-confirm", "99"])

    assert result.exit_code == 1
    assert "Could not confirm the pairing" in result.stderr
    assert "Traceback" not in result.output

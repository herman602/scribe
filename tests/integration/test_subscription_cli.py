"""Recovery and preview commands for the subscription register."""

from __future__ import annotations

from pathlib import Path

import pytest
from app.presentation.cli.app import cli
from typer.testing import CliRunner

runner = CliRunner()


@pytest.fixture
def register(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return an initialized environment with one wallet and one category."""
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(cli, ["init"]).exit_code == 0
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
                "TWD",
            ],
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(
            cli,
            ["money", "wallet-add", "--account-id", "1", "--name", "測試訂閱錢包"],
        ).exit_code
        == 0
    )
    assert runner.invoke(cli, ["money", "category-add", "測試訂閱"]).exit_code == 0
    return tmp_path


def test_money_help_lists_the_subscription_commands(register: Path) -> None:
    result = runner.invoke(cli, ["money", "--help"])

    assert result.exit_code == 0
    for command in (
        "subscription-add",
        "subscription-list",
        "subscription-update",
        "subscription-deactivate",
        "subscription-resume",
        "subscription-price-add",
        "subscription-price-update",
        "subscription-default-wallet-set",
        "subscription-default-wallet-show",
        "subscription-requirement",
    ):
        assert command in result.stdout


def test_a_monthly_subscription_is_added_listed_and_previewed(
    register: Path,
) -> None:
    added = runner.invoke(
        cli,
        [
            "money",
            "subscription-add",
            "測試影音",
            "--category-id",
            "1",
            "--funding-wallet-id",
            "2",
            "--cadence",
            "monthly",
        ],
    )
    priced = runner.invoke(
        cli,
        [
            "money",
            "subscription-price-add",
            "1",
            "--amount-minor",
            "39000",
            "--effective-from",
            "2026-01",
        ],
    )
    listed = runner.invoke(cli, ["money", "subscription-list"])
    preview = runner.invoke(
        cli,
        [
            "money",
            "subscription-requirement",
            "--period",
            "2026-08",
            "--currency",
            "TWD",
        ],
    )

    assert added.exit_code == 0, added.stdout
    assert "subscription_id=1" in added.stdout
    assert priced.exit_code == 0, priced.stdout
    assert "price_id=1" in priced.stdout
    assert listed.exit_code == 0
    assert "測試影音" in listed.stdout
    assert "2026-01" in listed.stdout
    assert preview.exit_code == 0, preview.stdout
    assert "total=TWD 390" in preview.stdout
    assert "reason=monthly_price" in preview.stdout
    assert "wallet=測試訂閱錢包" in preview.stdout
    assert "category=測試訂閱" in preview.stdout


def test_a_yearly_cycle_is_previewed_with_its_exact_share(register: Path) -> None:
    runner.invoke(
        cli,
        [
            "money",
            "subscription-add",
            "測試雲端",
            "--category-id",
            "1",
            "--funding-wallet-id",
            "2",
            "--cadence",
            "yearly",
            "--billing-month",
            "3",
        ],
    )
    runner.invoke(
        cli,
        [
            "money",
            "subscription-price-add",
            "1",
            "--amount-minor",
            "100000",
            "--effective-from",
            "2026-01",
        ],
    )

    march = runner.invoke(
        cli, ["money", "subscription-requirement", "--period", "2026-03"]
    )
    august = runner.invoke(
        cli, ["money", "subscription-requirement", "--period", "2026-08"]
    )

    # TWD displays without decimals, so the exact minor units are what the
    # preview has to be read on.
    assert "contribution_minor=8334" in march.stdout
    assert "cycle=1/12" in march.stdout
    assert "cycle_price_minor=100000" in march.stdout
    assert "contribution_minor=8333" in august.stdout
    assert "cycle=6/12" in august.stdout


def test_a_yearly_subscription_without_its_anchor_is_refused(
    register: Path,
) -> None:
    result = runner.invoke(
        cli,
        [
            "money",
            "subscription-add",
            "測試雲端",
            "--category-id",
            "1",
            "--funding-wallet-id",
            "2",
            "--cadence",
            "yearly",
        ],
    )

    assert result.exit_code == 1
    assert "billing month as anchor" in result.stderr


def test_an_overlapping_price_fails_without_writing_anything(
    register: Path,
) -> None:
    runner.invoke(
        cli,
        [
            "money",
            "subscription-add",
            "測試影音",
            "--category-id",
            "1",
            "--funding-wallet-id",
            "2",
        ],
    )
    runner.invoke(
        cli,
        [
            "money",
            "subscription-price-add",
            "1",
            "--amount-minor",
            "39000",
            "--effective-from",
            "2026-01",
            "--effective-to",
            "2026-07",
        ],
    )

    overlapping = runner.invoke(
        cli,
        [
            "money",
            "subscription-price-add",
            "1",
            "--amount-minor",
            "49000",
            "--effective-from",
            "2026-04",
        ],
    )
    listed = runner.invoke(cli, ["money", "subscription-list"])

    assert overlapping.exit_code == 1
    assert "must not overlap" in overlapping.stderr
    assert listed.stdout.count("price_id=") == 1


def test_editing_without_the_expected_version_is_refused(register: Path) -> None:
    runner.invoke(
        cli,
        [
            "money",
            "subscription-add",
            "測試影音",
            "--category-id",
            "1",
            "--funding-wallet-id",
            "2",
        ],
    )
    runner.invoke(
        cli,
        [
            "money",
            "subscription-price-add",
            "1",
            "--amount-minor",
            "39000",
            "--effective-from",
            "2026-01",
        ],
    )

    stale_subscription = runner.invoke(
        cli,
        [
            "money",
            "subscription-update",
            "1",
            "--expected-version",
            "99",
            "--name",
            "被丟棄的名稱",
        ],
    )
    stale_price = runner.invoke(
        cli,
        [
            "money",
            "subscription-price-update",
            "1",
            "--expected-version",
            "99",
            "--amount-minor",
            "1",
        ],
    )
    listed = runner.invoke(cli, ["money", "subscription-list"])

    assert stale_subscription.exit_code == 1
    assert stale_price.exit_code == 1
    assert "被丟棄的名稱" not in listed.stdout
    assert "TWD 390" in listed.stdout


def test_deactivating_keeps_history_and_zeroes_later_months(
    register: Path,
) -> None:
    runner.invoke(
        cli,
        [
            "money",
            "subscription-add",
            "測試影音",
            "--category-id",
            "1",
            "--funding-wallet-id",
            "2",
            "--active-from",
            "2026-01",
        ],
    )
    runner.invoke(
        cli,
        [
            "money",
            "subscription-price-add",
            "1",
            "--amount-minor",
            "39000",
            "--effective-from",
            "2026-01",
        ],
    )

    stopped = runner.invoke(
        cli,
        [
            "money",
            "subscription-deactivate",
            "1",
            "--expected-version",
            "1",
            "--effective-from",
            "2026-08",
        ],
    )
    july = runner.invoke(
        cli, ["money", "subscription-requirement", "--period", "2026-07"]
    )
    august = runner.invoke(
        cli, ["money", "subscription-requirement", "--period", "2026-08"]
    )
    listed = runner.invoke(cli, ["money", "subscription-list"])

    assert stopped.exit_code == 0, stopped.stdout
    assert "active=False" in stopped.stdout
    assert "total=TWD 390" in july.stdout
    assert "total=TWD 0" in august.stdout
    assert "reason=no_longer_active" in august.stdout
    assert "(inactive)" in listed.stdout


def test_the_default_wallet_is_shown_set_and_validated(register: Path) -> None:
    before = runner.invoke(cli, ["money", "subscription-default-wallet-show"])
    refused = runner.invoke(
        cli, ["money", "subscription-default-wallet-set", "--wallet-id", "9999"]
    )
    saved = runner.invoke(
        cli, ["money", "subscription-default-wallet-set", "--wallet-id", "2"]
    )
    after = runner.invoke(cli, ["money", "subscription-default-wallet-show"])

    assert "No default subscription funding wallet" in before.stdout
    assert refused.exit_code == 1
    assert saved.exit_code == 0, saved.stdout
    assert "No money moved" in saved.stdout
    assert "wallet_id=2" in after.stdout
    assert "測試訂閱錢包" in after.stdout
    assert "usable=True" in after.stdout


def test_each_currency_is_previewed_on_its_own(register: Path) -> None:
    runner.invoke(
        cli,
        [
            "money",
            "account-add",
            "--institution",
            "test_bank",
            "--name",
            "測試日圓帳戶",
            "--type",
            "checking",
            "--currency",
            "JPY",
        ],
    )
    runner.invoke(
        cli, ["money", "wallet-add", "--account-id", "2", "--name", "測試日圓錢包"]
    )
    runner.invoke(
        cli,
        [
            "money",
            "subscription-add",
            "測試影音",
            "--category-id",
            "1",
            "--funding-wallet-id",
            "2",
        ],
    )
    runner.invoke(
        cli,
        [
            "money",
            "subscription-add",
            "測試日本影音",
            "--category-id",
            "1",
            "--funding-wallet-id",
            "4",
            "--currency",
            "JPY",
        ],
    )
    for subscription_id, amount in ((1, 39000), (2, 1200)):
        runner.invoke(
            cli,
            [
                "money",
                "subscription-price-add",
                str(subscription_id),
                "--amount-minor",
                str(amount),
                "--effective-from",
                "2026-01",
            ],
        )

    preview = runner.invoke(
        cli, ["money", "subscription-requirement", "--period", "2026-08"]
    )

    assert "JPY total=JPY 1,200" in preview.stdout
    assert "TWD total=TWD 390" in preview.stdout
    assert "TWD 1,590" not in preview.stdout


def test_a_wallet_in_another_currency_cannot_fund_a_subscription(
    register: Path,
) -> None:
    runner.invoke(
        cli,
        [
            "money",
            "account-add",
            "--institution",
            "test_bank",
            "--name",
            "測試日圓帳戶",
            "--type",
            "checking",
            "--currency",
            "JPY",
        ],
    )
    runner.invoke(
        cli, ["money", "wallet-add", "--account-id", "2", "--name", "測試日圓錢包"]
    )

    result = runner.invoke(
        cli,
        [
            "money",
            "subscription-add",
            "測試影音",
            "--category-id",
            "1",
            "--funding-wallet-id",
            "4",
            "--currency",
            "TWD",
        ],
    )

    assert result.exit_code == 1
    assert "own currency" in result.stderr


def test_an_unusable_period_or_missing_row_is_reported_plainly(
    register: Path,
) -> None:
    bad_period = runner.invoke(
        cli, ["money", "subscription-requirement", "--period", "2026-13"]
    )
    missing_price = runner.invoke(
        cli,
        [
            "money",
            "subscription-price-update",
            "9999",
            "--expected-version",
            "1",
            "--amount-minor",
            "1",
        ],
    )
    missing_subscription = runner.invoke(
        cli,
        ["money", "subscription-update", "9999", "--expected-version", "1"],
    )

    assert bad_period.exit_code == 1
    assert missing_price.exit_code == 1
    assert "was not found" in missing_price.stderr
    assert missing_subscription.exit_code == 1
    assert "was not found" in missing_subscription.stderr


def test_a_priced_subscription_refuses_a_history_rewriting_edit(
    register: Path,
) -> None:
    runner.invoke(
        cli,
        [
            "money",
            "subscription-add",
            "測試影音",
            "--category-id",
            "1",
            "--funding-wallet-id",
            "2",
        ],
    )
    runner.invoke(
        cli,
        [
            "money",
            "subscription-price-add",
            "1",
            "--amount-minor",
            "99900",
            "--effective-from",
            "2026-01",
        ],
    )
    before = runner.invoke(
        cli, ["money", "subscription-requirement", "--period", "2026-02"]
    )

    refused = runner.invoke(
        cli,
        [
            "money",
            "subscription-update",
            "1",
            "--expected-version",
            "1",
            "--cadence",
            "yearly",
            "--cadence-months",
            "12",
            "--billing-month",
            "3",
        ],
    )
    renamed = runner.invoke(
        cli,
        [
            "money",
            "subscription-update",
            "1",
            "--expected-version",
            "1",
            "--name",
            "測試影音 Plus",
        ],
    )
    after = runner.invoke(
        cli, ["money", "subscription-requirement", "--period", "2026-02"]
    )

    assert refused.exit_code == 1
    assert "cannot change" in refused.stderr
    assert renamed.exit_code == 0, renamed.stderr
    assert "contribution_minor=99900" in before.stdout
    assert "contribution_minor=99900" in after.stdout
    assert "cadence=monthly" in after.stdout
    assert "測試影音 Plus" in after.stdout


def test_resuming_creates_a_new_interval_and_keeps_the_stop(
    register: Path,
) -> None:
    runner.invoke(
        cli,
        [
            "money",
            "subscription-add",
            "測試影音",
            "--category-id",
            "1",
            "--funding-wallet-id",
            "2",
            "--active-from",
            "2026-01",
        ],
    )
    runner.invoke(
        cli,
        [
            "money",
            "subscription-price-add",
            "1",
            "--amount-minor",
            "39000",
            "--effective-from",
            "2026-01",
        ],
    )
    runner.invoke(
        cli,
        [
            "money",
            "subscription-deactivate",
            "1",
            "--expected-version",
            "1",
            "--effective-from",
            "2026-08",
        ],
    )

    too_early = runner.invoke(
        cli,
        ["money", "subscription-resume", "1", "--active-from", "2026-07"],
    )
    resumed = runner.invoke(
        cli,
        ["money", "subscription-resume", "1", "--active-from", "2026-11"],
    )
    runner.invoke(
        cli,
        [
            "money",
            "subscription-price-add",
            "2",
            "--amount-minor",
            "49000",
            "--effective-from",
            "2026-11",
        ],
    )
    july = runner.invoke(
        cli, ["money", "subscription-requirement", "--period", "2026-07"]
    )
    september = runner.invoke(
        cli, ["money", "subscription-requirement", "--period", "2026-09"]
    )
    november = runner.invoke(
        cli, ["money", "subscription-requirement", "--period", "2026-11"]
    )
    listed = runner.invoke(cli, ["money", "subscription-list"])

    assert too_early.exit_code == 1
    assert "no earlier than" in too_early.stderr
    assert resumed.exit_code == 0, resumed.stderr
    assert "subscription_id=2" in resumed.stdout
    # The stop stays in the record and the gap stays a gap.
    assert "total=TWD 390" in july.stdout
    assert "total=TWD 0" in september.stdout
    assert "reason=no_longer_active" in september.stdout
    assert "reason=not_yet_active" in september.stdout
    assert "total=TWD 490" in november.stdout
    assert "(inactive)" in listed.stdout
    assert listed.stdout.count("測試影音") == 2


def test_an_overlapping_interval_reports_a_short_message_not_a_traceback(
    register: Path,
) -> None:
    runner.invoke(
        cli,
        [
            "money",
            "subscription-add",
            "測試影音",
            "--category-id",
            "1",
            "--funding-wallet-id",
            "2",
            "--active-from",
            "2026-01",
        ],
    )
    runner.invoke(
        cli,
        [
            "money",
            "subscription-price-add",
            "1",
            "--amount-minor",
            "39000",
            "--effective-from",
            "2026-01",
        ],
    )
    runner.invoke(
        cli,
        [
            "money",
            "subscription-deactivate",
            "1",
            "--expected-version",
            "1",
            "--effective-from",
            "2026-11",
        ],
    )

    # The stopped interval still owns every month up to 2026-11.
    overlapping = runner.invoke(
        cli,
        [
            "money",
            "subscription-add",
            "測試影音",
            "--category-id",
            "1",
            "--funding-wallet-id",
            "2",
            "--active-from",
            "2026-08",
        ],
    )
    resumed = runner.invoke(
        cli,
        ["money", "subscription-resume", "1", "--active-from", "2026-08"],
    )
    adjacent = runner.invoke(
        cli,
        ["money", "subscription-resume", "1", "--active-from", "2026-11"],
    )
    september = runner.invoke(
        cli, ["money", "subscription-requirement", "--period", "2026-09"]
    )

    assert overlapping.exit_code == 1
    assert "must not overlap" in overlapping.stderr
    assert "Traceback" not in overlapping.stderr
    assert "IntegrityError" not in overlapping.stderr
    assert resumed.exit_code == 1
    assert "no earlier than" in resumed.stderr
    assert adjacent.exit_code == 0, adjacent.stderr
    # September lies inside the first interval only, so it is charged once
    # even though the second interval is already on record.
    assert "total=TWD 390" in september.stdout
    assert september.stdout.count("contribution_minor=39000") == 1
    assert "reason=not_yet_active" in september.stdout

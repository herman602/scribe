"""Recovery and setup commands for the monthly salary allocation."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from app.presentation.cli.app import cli
from typer.testing import CliRunner, Result

runner = CliRunner()

SALARY_MINOR = "100000"
LIVING_MINOR = "30000"
SUBSCRIPTION_MINOR = "39000"


def _run(*arguments: str) -> Result:
    return runner.invoke(cli, list(arguments))


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Initialize a local database with two banks, wallets, and a subscription."""
    monkeypatch.chdir(tmp_path)
    assert _run("init").exit_code == 0
    for institution, name in (
        ("test_bank_a", "測試薪轉帳戶"),
        ("test_bank_b", "測試訂閱帳戶"),
    ):
        assert (
            _run(
                "money",
                "account-add",
                "--institution",
                institution,
                "--name",
                name,
                "--type",
                "checking",
                "--currency",
                "TWD",
            ).exit_code
            == 0
        )
    assert (
        _run(
            "money", "wallet-add", "--account-id", "1", "--name", "測試生活費錢包"
        ).exit_code
        == 0
    )
    assert (
        _run(
            "money", "wallet-add", "--account-id", "2", "--name", "測試訂閱錢包"
        ).exit_code
        == 0
    )
    assert _run("money", "category-add", "測試分類").exit_code == 0
    assert (
        _run(
            "money",
            "subscription-add",
            "測試影音",
            "--category-id",
            "1",
            "--funding-wallet-id",
            "4",
            "--currency",
            "TWD",
        ).exit_code
        == 0
    )
    assert (
        _run(
            "money",
            "subscription-price-add",
            "1",
            "--amount-minor",
            SUBSCRIPTION_MINOR,
            "--effective-from",
            "2026-01",
        ).exit_code
        == 0
    )
    return tmp_path


@pytest.fixture
def plan(ledger: Path) -> Path:
    """Add one template with a same-account line and a cross-bank line."""
    assert (
        _run(
            "money",
            "salary-template-add",
            "測試薪資範本",
            "--source-account-id",
            "1",
            "--currency",
            "TWD",
        ).exit_code
        == 0
    )
    assert (
        _run(
            "money",
            "salary-template-item-add",
            "1",
            "--name",
            "測試生活費",
            "--destination-wallet-id",
            "3",
            "--mode",
            "fixed",
            "--fixed-amount-minor",
            LIVING_MINOR,
            "--category-id",
            "1",
        ).exit_code
        == 0
    )
    assert (
        _run(
            "money",
            "salary-template-item-add",
            "1",
            "--name",
            "測試訂閱",
            "--destination-wallet-id",
            "4",
            "--mode",
            "subscription_requirement",
            "--display-order",
            "1",
        ).exit_code
        == 0
    )
    assert (
        _run(
            "money",
            "salary-template-item-add",
            "1",
            "--name",
            "測試手動",
            "--destination-wallet-id",
            "1",
            "--mode",
            "manual",
            "--display-order",
            "2",
        ).exit_code
        != 0
    )
    return ledger


def _version(text: str, label: str = "version") -> str:
    match = re.search(rf"\b{label}=(\d+)", text)
    assert match is not None, text
    return match.group(1)


def test_money_help_lists_every_salary_command(ledger: Path) -> None:
    result = _run("money", "--help")

    assert result.exit_code == 0
    for command in (
        "salary-template-list",
        "salary-template-add",
        "salary-template-update",
        "salary-template-item-add",
        "salary-template-item-update",
        "salary-template-item-deactivate",
        "salary-receipt",
        "salary-events",
        "salary-generate",
        "salary-regenerate",
        "salary-show",
        "salary-approve",
        "salary-cancel",
        "salary-mark-transferred",
    ):
        assert command in result.stdout


def test_the_whole_flow_runs_from_the_command_line(plan: Path) -> None:
    listed = _run("money", "salary-template-list")
    assert listed.exit_code == 0
    assert "測試薪資範本" in listed.stdout
    assert "測試生活費" in listed.stdout
    assert "fixed_minor=30000" in listed.stdout

    receipt = _run(
        "money",
        "salary-receipt",
        "--template-id",
        "1",
        "--amount-minor",
        SALARY_MINOR,
        "--occurred-at",
        "2026-08-05T09:00",
        "--token",
        "cli-token-1",
    )
    assert receipt.exit_code == 0
    assert "amount_minor=100000" in receipt.stdout
    assert "TWD 1,000" in receipt.stdout
    assert "created=True" in receipt.stdout

    events = _run("money", "salary-events", "--template-id", "1")
    assert events.exit_code == 0
    assert "event_id=1" in events.stdout

    generated = _run(
        "money",
        "salary-generate",
        "2026-08",
        "--template-id",
        "1",
        "--salary-event-id",
        "1",
    )
    assert generated.exit_code == 0
    assert "generation=1" in generated.stdout
    assert "status=draft" in generated.stdout
    assert "planned_minor=69000" in generated.stdout
    assert "remainder_minor=31000" in generated.stdout

    shown = _run("money", "salary-show", "1")
    assert shown.exit_code == 0
    assert "2026-08" in shown.stdout
    assert "path=reserve_only" in shown.stdout
    assert "path=cross_bank_transfer" in shown.stdout
    assert "測試訂閱錢包" in shown.stdout
    assert "contribution_minor=39000" in shown.stdout

    approved = _run(
        "money", "salary-approve", "1", "--expected-version", _version(shown.stdout)
    )
    assert approved.exit_code == 0
    assert "status=partially_funded" in approved.stdout
    assert "reclassification_entry=" in approved.stdout

    shown = _run("money", "salary-show", "1")
    waiting = re.search(r"item_id=(\d+).*status=waiting_transfer", shown.stdout)
    assert waiting is not None
    item_id = waiting.group(1)
    item_version = re.search(rf"item_id={item_id}.*?version=(\d+)", shown.stdout)
    assert item_version is not None

    transferred = _run(
        "money",
        "salary-mark-transferred",
        item_id,
        "--expected-version",
        item_version.group(1),
        "--effective-at",
        "2026-08-06T18:30",
    )
    assert transferred.exit_code == 0
    assert "status=transferred" in transferred.stdout
    assert "status=funded" in transferred.stdout

    balances = _run("money", "balances")
    assert balances.exit_code == 0
    assert "測試訂閱錢包" in balances.stdout


def test_the_same_receipt_token_records_only_once(plan: Path) -> None:
    first = _run(
        "money",
        "salary-receipt",
        "--template-id",
        "1",
        "--amount-minor",
        SALARY_MINOR,
        "--occurred-at",
        "2026-08-05T09:00",
        "--token",
        "cli-token-2",
    )
    second = _run(
        "money",
        "salary-receipt",
        "--template-id",
        "1",
        "--amount-minor",
        SALARY_MINOR,
        "--occurred-at",
        "2026-08-05T09:00",
        "--token",
        "cli-token-2",
    )
    conflict = _run(
        "money",
        "salary-receipt",
        "--template-id",
        "1",
        "--amount-minor",
        "999",
        "--occurred-at",
        "2026-08-05T09:00",
        "--token",
        "cli-token-2",
    )

    assert first.exit_code == 0
    assert "created=True" in first.stdout
    assert second.exit_code == 0
    assert "created=False" in second.stdout
    assert conflict.exit_code == 1
    assert "Could not record the salary receipt" in conflict.stderr
    assert "Traceback" not in conflict.stderr
    assert "999" not in conflict.stderr


def test_a_stale_version_is_refused_without_a_traceback(plan: Path) -> None:
    result = _run(
        "money", "salary-template-item-deactivate", "1", "--expected-version", "99"
    )

    assert result.exit_code == 1
    assert "Could not deactivate the salary item" in result.stderr
    assert "Traceback" not in result.stderr
    assert "測試生活費" in _run("money", "salary-template-list").stdout


def test_a_manual_line_needs_an_explicit_amount_on_the_command_line(
    plan: Path,
) -> None:
    assert (
        _run(
            "money",
            "salary-template-item-add",
            "1",
            "--name",
            "測試手動",
            "--destination-wallet-id",
            "3",
            "--mode",
            "manual",
        ).exit_code
        != 0
    )
    # A manual line into a wallet that is free of other lines is accepted.
    assert (
        _run(
            "money",
            "salary-template-item-update",
            "1",
            "--expected-version",
            "1",
            "--mode",
            "manual",
        ).exit_code
        == 0
    )
    _run(
        "money",
        "salary-receipt",
        "--template-id",
        "1",
        "--amount-minor",
        SALARY_MINOR,
        "--occurred-at",
        "2026-08-05T09:00",
        "--token",
        "cli-token-3",
    )

    missing = _run(
        "money",
        "salary-generate",
        "2026-08",
        "--template-id",
        "1",
        "--salary-event-id",
        "1",
    )
    assert missing.exit_code == 1
    assert "needs an explicit amount" in missing.stderr

    malformed = _run(
        "money",
        "salary-generate",
        "2026-08",
        "--template-id",
        "1",
        "--salary-event-id",
        "1",
        "--manual",
        "1",
    )
    assert malformed.exit_code == 1
    assert "ITEM_ID=123456" in malformed.stderr

    fractional = _run(
        "money",
        "salary-generate",
        "2026-08",
        "--template-id",
        "1",
        "--salary-event-id",
        "1",
        "--manual",
        "1=12.5",
    )
    assert fractional.exit_code == 1
    assert "integer minor-unit" in fractional.stderr

    accepted = _run(
        "money",
        "salary-generate",
        "2026-08",
        "--template-id",
        "1",
        "--salary-event-id",
        "1",
        "--manual",
        "1=25000",
    )
    assert accepted.exit_code == 0
    assert "planned_minor=64000" in accepted.stdout


def test_a_draft_can_be_regenerated_and_cancelled(plan: Path) -> None:
    _run(
        "money",
        "salary-receipt",
        "--template-id",
        "1",
        "--amount-minor",
        SALARY_MINOR,
        "--occurred-at",
        "2026-08-05T09:00",
        "--token",
        "cli-token-4",
    )
    generated = _run(
        "money",
        "salary-generate",
        "2026-08",
        "--template-id",
        "1",
        "--salary-event-id",
        "1",
    )
    assert generated.exit_code == 0

    regenerated = _run("money", "salary-regenerate", "1", "--expected-version", "1")
    assert regenerated.exit_code == 0
    assert "generation=2" in regenerated.stdout

    cancelled = _run("money", "salary-cancel", "2", "--expected-version", "1")
    assert cancelled.exit_code == 0
    assert "status=cancelled" in cancelled.stdout
    assert "reserves nothing" in cancelled.stdout


def test_the_salary_commands_release_their_database_engine(plan: Path) -> None:
    """A leaked SQLite connection would resurface as a ResourceWarning."""
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error", ResourceWarning)
        result = _run("money", "salary-template-list")

    assert result.exit_code == 0

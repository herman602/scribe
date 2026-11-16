"""Typer money sub-application tests."""

from __future__ import annotations

import json
import re
from pathlib import Path

import app.presentation.web.app
import pytest
from app.presentation.cli.app import cli
from click.utils import strip_ansi
from typer.testing import CliRunner

runner = CliRunner()


@pytest.fixture
def initialized_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return an initialized, migrated local environment."""
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli, ["init"])
    assert result.exit_code == 0
    return tmp_path


def test_money_help_lists_ledger_commands(initialized_home: Path) -> None:
    result = runner.invoke(cli, ["money", "--help"])

    assert result.exit_code == 0
    assert "account-add" in result.stdout
    assert "wallet-add" in result.stdout
    assert "opening-balance" in result.stdout
    assert "balances" in result.stdout


def test_money_commands_open_an_account_and_report_balances(
    initialized_home: Path,
) -> None:
    account = runner.invoke(
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
            "--masked-reference",
            "****4321",
        ],
    )
    wallet = runner.invoke(
        cli,
        ["money", "wallet-add", "--account-id", "1", "--name", "房租"],
    )
    opening = runner.invoke(
        cli,
        [
            "money",
            "opening-balance",
            "--account-id",
            "1",
            "--amount-minor",
            "500000",
            "--observed-at",
            "2026-08-07T21:30",
            "--split",
            "尚未分配=300000",
            "--split",
            "房租=200000",
        ],
    )
    repeated = runner.invoke(
        cli,
        [
            "money",
            "opening-balance",
            "--account-id",
            "1",
            "--amount-minor",
            "500000",
            "--observed-at",
            "2026-08-07T21:30",
            "--split",
            "尚未分配=300000",
            "--split",
            "房租=200000",
        ],
    )
    balances = runner.invoke(cli, ["money", "balances"])
    structured = runner.invoke(cli, ["money", "balances", "--json"])

    assert account.exit_code == 0
    assert "account_id=1" in account.stdout
    assert "ledger=asset:test_bank:1" in account.stdout
    assert "尚未分配 (unallocated)" in account.stdout
    assert wallet.exit_code == 0
    assert "wallet_id=2" in wallet.stdout
    assert opening.exit_code == 0
    assert "type=opening_balance" in opening.stdout
    assert "postings=3" in opening.stdout
    assert repeated.exit_code == 0
    assert "entry_id=1" in repeated.stdout
    assert balances.exit_code == 0
    assert "TWD 5,000" in balances.stdout
    assert "TWD 3,000" in balances.stdout
    assert "TWD 2,000" in balances.stdout
    # The JSON shape carries the exact minor units, so a reader never has to
    # parse a formatted amount back into a number.
    assert structured.exit_code == 0
    payload = json.loads(structured.stdout)
    assert [item["balance_minor"] for item in payload["accounts"]] == [500_000]
    assert {wallet["name"] for wallet in payload["accounts"][0]["wallets"]} == {
        "尚未分配",
        "房租",
    }


def test_money_balances_reports_an_empty_ledger(initialized_home: Path) -> None:
    result = runner.invoke(cli, ["money", "balances"])

    assert result.exit_code == 0
    assert "No financial accounts are configured." in result.stdout


def test_opening_balance_cli_preserves_a_negative_wallet_and_skips_zero_posting(
    initialized_home: Path,
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
                "測試帳戶",
                "--type",
                "checking",
                "--currency",
                "TWD",
            ],
        ).exit_code
        == 0
    )
    for name in ("預算", "已透支", "空錢包"):
        assert (
            runner.invoke(
                cli, ["money", "wallet-add", "--account-id", "1", "--name", name]
            ).exit_code
            == 0
        )

    opening = runner.invoke(
        cli,
        [
            "money",
            "opening-balance",
            "--account-id",
            "1",
            "--amount-minor",
            "500000",
            "--observed-at",
            "2026-08-07T21:30",
            "--split",
            "預算=700000",
            "--split",
            "已透支=-200000",
            "--split",
            "空錢包=0",
        ],
    )
    balances = runner.invoke(cli, ["money", "balances"])

    assert opening.exit_code == 0
    assert "postings=3" in opening.stdout
    assert balances.exit_code == 0
    assert "TWD 5,000" in balances.stdout
    assert "TWD 7,000" in balances.stdout
    assert "TWD -2,000" in balances.stdout
    assert "空錢包" in balances.stdout
    assert "TWD 0" in balances.stdout


def test_money_commands_reject_invalid_input(initialized_home: Path) -> None:
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
    )
    duplicate = runner.invoke(
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
    )
    full_number = runner.invoke(
        cli,
        [
            "money",
            "account-add",
            "--institution",
            "test_bank",
            "--name",
            "另一個帳戶",
            "--type",
            "savings",
            "--currency",
            "TWD",
            "--masked-reference",
            "00112233445566",
        ],
    )
    bad_split = runner.invoke(
        cli,
        [
            "money",
            "opening-balance",
            "--account-id",
            "1",
            "--amount-minor",
            "500000",
            "--observed-at",
            "2026-08-07T21:30",
            "--split",
            "尚未分配",
        ],
    )
    bad_amount = runner.invoke(
        cli,
        [
            "money",
            "opening-balance",
            "--account-id",
            "1",
            "--amount-minor",
            "500000",
            "--observed-at",
            "2026-08-07T21:30",
            "--split",
            "尚未分配=5000.00",
        ],
    )
    bad_time = runner.invoke(
        cli,
        [
            "money",
            "opening-balance",
            "--account-id",
            "1",
            "--amount-minor",
            "500000",
            "--observed-at",
            "not-a-time",
            "--split",
            "尚未分配=500000",
        ],
    )
    missing_wallet_account = runner.invoke(
        cli, ["money", "wallet-add", "--account-id", "99", "--name", "房租"]
    )

    assert duplicate.exit_code == 1
    assert "already exists" in duplicate.stderr
    assert full_number.exit_code == 1
    assert "full account number" in full_number.stderr
    assert "00112233445566" not in full_number.stderr
    assert bad_split.exit_code == 1
    assert "Wallet name=123456" in bad_split.stderr
    assert bad_amount.exit_code == 1
    assert "integer minor-unit" in bad_amount.stderr
    assert bad_time.exit_code == 1
    assert "ISO 8601" in bad_time.stderr
    assert missing_wallet_account.exit_code == 1
    assert "financial account was not found" in missing_wallet_account.stderr


def test_money_commands_require_a_migrated_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli, ["money", "balances"])

    assert result.exit_code == 1
    assert "Database is unavailable" in result.stderr


def test_money_help_lists_the_b2_commands(initialized_home: Path) -> None:
    result = runner.invoke(cli, ["money", "--help"])

    assert result.exit_code == 0
    assert "intake-confirmed" in result.stdout
    assert "cards-backfill" in result.stdout


def test_intake_and_card_backfill_default_to_a_dry_run(
    initialized_home: Path,
) -> None:
    _seed_confirmed_transaction(initialized_home)

    preview = runner.invoke(cli, ["money", "intake-confirmed"])
    cards_preview = runner.invoke(cli, ["money", "cards-backfill"])
    applied = runner.invoke(cli, ["money", "intake-confirmed", "--apply"])
    repeated = runner.invoke(cli, ["money", "intake-confirmed", "--apply"])

    assert preview.exit_code == 0
    assert "mode=dry-run staged=1 reconciled=0 skipped=0" in preview.stdout
    assert "reasons=card_not_configured" in preview.stdout
    assert "No financial event was created" in preview.stdout
    assert cards_preview.exit_code == 0
    assert "mode=dry-run created=0" in cards_preview.stdout
    assert "never guessed" in cards_preview.stdout
    assert applied.exit_code == 0
    assert "mode=apply staged=1 reconciled=0 skipped=0" in applied.stdout
    assert repeated.exit_code == 0
    assert "mode=apply staged=0 reconciled=1 skipped=1" in repeated.stdout
    assert "reason=already_staged" in repeated.stdout


def test_card_backfill_creates_identities_from_explicit_settings(
    initialized_home: Path,
) -> None:
    _seed_confirmed_transaction(initialized_home, mark_supplementary=True)

    applied = runner.invoke(cli, ["money", "cards-backfill", "--apply"])
    repeated = runner.invoke(cli, ["money", "cards-backfill", "--apply"])

    assert applied.exit_code == 0
    assert "mode=apply created=1 already_present=0" in applied.stdout
    assert repeated.exit_code == 0
    assert "mode=apply created=0 already_present=1" in repeated.stdout


def _seed_confirmed_transaction(
    home: Path, *, mark_supplementary: bool = False
) -> None:
    """Seed one confirmed card transaction in the CLI's own database."""
    from datetime import UTC, datetime
    from decimal import Decimal

    from app.domain.enums import SenderMatchType, TransactionType
    from app.domain.models import EmailSnapshot, SenderRuleDraft, TransactionDraft
    from app.infrastructure.config.settings import AppSettings
    from app.infrastructure.database.engine import create_database
    from app.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork

    settings = AppSettings(
        data_dir=home / "data",
        database_path=home / "data/app.sqlite3",
        log_dir=home / "data/logs",
    )
    database = create_database(settings)
    try:
        with SqlAlchemyUnitOfWork(database.session_factory) as unit:
            account = unit.accounts.get_or_create("user@example.test")
            rule = unit.sender_rules.upsert(
                SenderRuleDraft(
                    gmail_account_id=account.id,
                    institution_key="test_bank",
                    institution_name="測試銀行",
                    match_type=SenderMatchType.EXACT_ADDRESS,
                    match_value="notice@example-bank.test",
                )
            )
            outcome = unit.emails.upsert_from_gmail(
                EmailSnapshot(
                    gmail_account_id=account.id,
                    gmail_message_id="gmail-cli-b2",
                    gmail_thread_id="thread-cli-b2",
                    gmail_history_id="500",
                    sender="notice@example-bank.test",
                    recipients=("user@example.test",),
                    subject="消費通知",
                    gmail_internal_date_ms=1_754_000_000_000,
                    matched_sender_rule_id=rule.id,
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
                    merchant="測試咖啡",
                    amount_minor=128000,
                    currency="TWD",
                    card_last_four="4321",
                    transaction_at=datetime(2026, 8, 7, 13, 30, tzinfo=UTC),
                )
            )
            unit.transactions.confirm_pending(frozenset({transaction.id}))
            if mark_supplementary:
                unit.supplementary_card_settings.mark(
                    account_id=account.id,
                    institution_key="test_bank",
                    card_last_four="4321",
                )
            unit.commit()
    finally:
        database.engine.dispose()


def test_intake_apply_persists_a_late_card_mapping(initialized_home: Path) -> None:
    """Regression: the CLI apply path must commit reconciliation-only work."""
    _seed_confirmed_transaction(initialized_home, mark_supplementary=True)

    staged = runner.invoke(cli, ["money", "intake-confirmed", "--apply"])
    backfilled = runner.invoke(cli, ["money", "cards-backfill", "--apply"])
    reconciled = runner.invoke(cli, ["money", "intake-confirmed", "--apply"])
    review = runner.invoke(cli, ["money", "intake-confirmed"])

    assert staged.exit_code == 0
    assert "staged=1 reconciled=0" in staged.stdout
    assert backfilled.exit_code == 0
    assert "created=1" in backfilled.stdout
    assert reconciled.exit_code == 0
    assert "staged=0 reconciled=1" in reconciled.stdout
    assert review.exit_code == 0
    assert _persisted_card_mapping(initialized_home) is not None


def _persisted_card_mapping(home: Path) -> int | None:
    """Return the card ID stored on the bridged event, read in a new session."""
    from app.infrastructure.config.settings import AppSettings
    from app.infrastructure.database.engine import create_database
    from app.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork

    settings = AppSettings(
        data_dir=home / "data",
        database_path=home / "data/app.sqlite3",
        log_dir=home / "data/logs",
    )
    database = create_database(settings)
    try:
        with SqlAlchemyUnitOfWork(database.session_factory) as unit:
            event = unit.financial_events.get_by_idempotency_key("gmail_transaction:1")
            assert event is not None
            link = unit.source_links.get_confirmed_for_transaction(1)
            assert link is not None
            assert [reason.value for reason in link.reasons] == ["direct_gmail_intake"]
            return event.card_id
    finally:
        database.engine.dispose()


def test_money_help_lists_the_b3_budget_commands(initialized_home: Path) -> None:
    result = runner.invoke(cli, ["money", "--help"])

    assert result.exit_code == 0
    for command in ("category-add", "category-list", "target-set", "budget-close"):
        assert command in result.stdout


def _budget_world() -> None:
    """Open one account with a second wallet and an opening position."""
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
    assert (
        runner.invoke(
            cli, ["money", "wallet-add", "--account-id", "1", "--name", "儲蓄"]
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
                "500000",
                "--observed-at",
                "2026-08-07T21:30",
                "--split",
                "尚未分配=300000",
                "--split",
                "儲蓄=200000",
            ],
        ).exit_code
        == 0
    )


def test_budget_cli_creates_categories_and_sets_a_target(
    initialized_home: Path,
) -> None:
    _budget_world()

    created = runner.invoke(cli, ["money", "category-add", "飲食"])
    nested = runner.invoke(cli, ["money", "category-add", "外食", "--parent-id", "1"])
    duplicate = runner.invoke(cli, ["money", "category-add", "飲食"])
    listed = runner.invoke(cli, ["money", "category-list"])
    target = runner.invoke(
        cli,
        [
            "money",
            "target-set",
            "2026-08",
            "--category-id",
            "1",
            "--target-minor",
            "300000",
            "--buffer-minor",
            "50000",
            "--funding-wallet-id",
            "1",
        ],
    )
    shown = runner.invoke(cli, ["money", "budget-show", "2026-08"])

    assert created.exit_code == 0
    assert "Created category 1: 飲食" in created.stdout
    assert nested.exit_code == 0
    assert "parent=1" in listed.stdout
    assert duplicate.exit_code == 1
    assert "already uses this name" in duplicate.stderr
    assert target.exit_code == 0
    assert "target=TWD 3,000" in target.stdout
    assert "buffer=TWD 500" in target.stdout
    assert shown.exit_code == 0
    # The buffer is reported beside the target, never folded into it.
    assert "target=TWD 3,000" in shown.stdout
    assert "remaining=TWD 3,000" in shown.stdout
    assert "buffer=TWD 500" in shown.stdout


def test_budget_cli_rejects_a_malformed_period(initialized_home: Path) -> None:
    _budget_world()
    assert runner.invoke(cli, ["money", "category-add", "飲食"]).exit_code == 0

    for period in ("2026-13", "26-08", "August"):
        result = runner.invoke(
            cli,
            [
                "money",
                "target-set",
                period,
                "--category-id",
                "1",
                "--target-minor",
                "1000",
                "--funding-wallet-id",
                "1",
            ],
        )
        assert result.exit_code == 1, period
        assert "yyyy-mm" in result.stderr, period


def test_budget_close_defaults_to_a_dry_run(initialized_home: Path) -> None:
    _budget_world()
    assert runner.invoke(cli, ["money", "category-add", "飲食"]).exit_code == 0
    assert (
        runner.invoke(
            cli,
            [
                "money",
                "target-set",
                "2026-08",
                "--category-id",
                "1",
                "--target-minor",
                "100000",
                "--funding-wallet-id",
                "1",
                "--destination-wallet-id",
                "2",
            ],
        ).exit_code
        == 0
    )

    preview = runner.invoke(cli, ["money", "budget-close", "2026-08"])
    balances_before = runner.invoke(cli, ["money", "balances"])
    applied = runner.invoke(cli, ["money", "budget-close", "2026-08", "--apply"])
    repeated = runner.invoke(cli, ["money", "budget-close", "2026-08", "--apply"])
    balances_after = runner.invoke(cli, ["money", "balances"])

    assert preview.exit_code == 0
    assert "mode=dry-run" in preview.stdout
    assert "sweep=TWD 1,000" in preview.stdout
    assert "already_closed=False" in preview.stdout
    # A preview writes nothing at all.
    assert "TWD 3,000" in balances_before.stdout
    assert applied.exit_code == 0
    assert "mode=apply" in applied.stdout
    assert repeated.exit_code == 0
    assert "already_closed=True" in repeated.stdout
    assert "TWD 2,000" in balances_after.stdout
    assert "TWD 3,000" in balances_after.stdout


def test_budget_close_refuses_an_unplanned_month(initialized_home: Path) -> None:
    _budget_world()

    result = runner.invoke(cli, ["money", "budget-close", "2026-08"])

    assert result.exit_code == 1
    assert "no budget to close" in result.stderr


def test_target_set_requires_the_version_it_saw_before_correcting(
    initialized_home: Path,
) -> None:
    _budget_world()
    assert runner.invoke(cli, ["money", "category-add", "飲食"]).exit_code == 0
    base = [
        "money",
        "target-set",
        "2026-08",
        "--category-id",
        "1",
        "--funding-wallet-id",
        "1",
    ]

    created = runner.invoke(cli, [*base, "--target-minor", "300000"])
    blind = runner.invoke(cli, [*base, "--target-minor", "400000"])
    corrected = runner.invoke(
        cli, [*base, "--target-minor", "400000", "--expected-version", "1"]
    )
    stale = runner.invoke(
        cli, [*base, "--target-minor", "500000", "--expected-version", "1"]
    )
    shown = runner.invoke(cli, ["money", "budget-show", "2026-08"])

    assert created.exit_code == 0
    assert "version=1" in created.stdout
    # A blind rewrite would silently discard whatever another window saved.
    assert blind.exit_code == 1
    assert "--expected-version 1" in blind.stderr
    assert corrected.exit_code == 0
    assert "version=2" in corrected.stdout
    assert stale.exit_code == 1
    assert "TWD 4,000" in shown.stdout
    assert "version=2" in shown.stdout


def test_budget_show_counts_spending_with_no_target(initialized_home: Path) -> None:
    _budget_world()
    assert runner.invoke(cli, ["money", "category-add", "飲食"]).exit_code == 0
    assert runner.invoke(cli, ["money", "category-add", "雜項"]).exit_code == 0
    assert (
        runner.invoke(
            cli,
            [
                "money",
                "target-set",
                "2026-08",
                "--category-id",
                "1",
                "--target-minor",
                "100000",
                "--funding-wallet-id",
                "1",
            ],
        ).exit_code
        == 0
    )

    shown = runner.invoke(cli, ["money", "budget-show", "2026-08"])

    assert shown.exit_code == 0
    assert "buffer_left=" in shown.stdout


def test_money_status_reports_the_shape_of_local_state(initialized_home: Path) -> None:
    """One read answers what an arriving operator needs before deciding."""
    runner.invoke(
        cli,
        [
            "money",
            "account-add",
            "--institution",
            "test_bank",
            "--name",
            "測試帳戶",
            "--type",
            "checking",
            "--currency",
            "TWD",
        ],
    )

    result = runner.invoke(cli, ["money", "status", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["migration_revision"]
    assert [item["display_name"] for item in payload["accounts"]] == ["測試帳戶"]
    assert payload["cards"] == []
    assert payload["waiting"]["events_needing_allocation"] == 0
    # Reading state must never write any of it.
    assert runner.invoke(cli, ["money", "status", "--json"]).stdout == result.stdout


def test_every_web_write_has_a_cli_command(initialized_home: Path) -> None:
    """The CLI must cover what the browser can do, or an operator is stuck.

    Enumerated rather than asserted by count, so a new web route that grows a
    write without a command shows up here as a named gap.
    """
    del initialized_home
    # The fixture changes the working directory, so locate the module from the
    # package itself rather than from a relative path.
    web = (Path(app.presentation.web.app.__file__)).read_text()
    posts = set(re.findall(r'@application\.post\("([^"]+)"', web))

    covered = {
        "/allocations/{label}/generate",
        "/allocations/{label}/salary-receipt",
        "/allocations/items/{run_item_id}/mark-transferred",
        "/allocations/runs/{run_id}/approve",
        "/allocations/runs/{run_id}/cancel",
        "/allocations/runs/{run_id}/regenerate",
        "/allocations/templates",
        "/allocations/templates/{template_id}",
        "/allocations/templates/{template_id}/amounts",
        "/allocations/templates/{template_id}/items",
        "/allocations/templates/items/{template_item_id}",
        "/allocations/templates/items/{template_item_id}/deactivate",
        "/budget/{label}/close",
        "/budget/{label}/targets",
        "/budget/categories",
        "/budget/categories/{category_id}",
        "/cards",
        "/cards/{card_id}",
        "/cards/import-candidates/{import_row_id}/assign",
        "/clearing/autopay",
        "/clearing/run-due",
        "/clearing/schedules",
        "/clearing/schedules/{scheduled_transfer_id}/cancel",
        "/clearing/schedules/{scheduled_transfer_id}/retry",
        "/clearing/schedules/{scheduled_transfer_id}/reverse",
        "/emails/{email_id}/reparse",
        "/money/account-transfers",
        "/money/accounts",
        "/money/accounts/order",
        "/money/accounts/{account_id}/order",
        "/money/accounts/{account_id}/wallets/rename",
        "/money/bank-movements",
        "/money/events/{event_id}/allocation-redo",
        "/money/events/{event_id}/note",
        "/money/events/{event_id}/reverse",
        "/money/expenses",
        "/money/reconcile/snapshots",
        "/money/reconcile/{snapshot_id}/adjust",
        "/money/refunds",
        "/money/review/{event_id}/allocate",
        "/money/source-links/{link_id}/confirm",
        "/money/source-links/{link_id}/reject",
        "/money/source-transactions/{transaction_id}/separate",
        "/money/wallets/{wallet_id}/balance",
        "/subscriptions",
        "/subscriptions/default-wallet",
        "/subscriptions/prices/{price_period_id}",
        "/subscriptions/{subscription_id}",
        "/subscriptions/{subscription_id}/deactivate",
        "/subscriptions/{subscription_id}/prices",
        "/subscriptions/{subscription_id}/resume",
        "/sync/start/full",
        "/sync/start/incremental",
        "/sync/{run_id}/cancel",
        "/transactions/confirm",
        "/transactions/{transaction_id}/reparse",
        "/transactions/{transaction_id}/review",
        "/transactions/{transaction_id}/supplementary-card",
    }

    assert posts - covered == set(), "a web write has no CLI equivalent"


def test_posting_commands_preview_before_they_post(initialized_home: Path) -> None:
    """A model driving this must be able to look before it moves money."""
    del initialized_home
    for command in ("allocate", "transaction-confirm", "wallet-reallocate"):
        result = runner.invoke(cli, ["money", command, "--help"])
        assert result.exit_code == 0
        visible_output = strip_ansi(result.stdout)
        assert "--apply" in visible_output
        assert "--dry-run" in visible_output


def _identifier(pattern: str, output: str) -> int:
    """Return the one id a command reported, or fail saying what it printed."""
    match = re.search(pattern, output)
    assert match is not None, output
    return int(match.group(1))


def _account(
    name: str, *, institution: str = "test_bank", kind: str = "checking"
) -> int:
    """Open one account through the CLI and return its id."""
    result = runner.invoke(
        cli,
        [
            "money",
            "account-add",
            "--institution",
            institution,
            "--name",
            name,
            "--type",
            kind,
            "--currency",
            "TWD",
        ],
    )
    assert result.exit_code == 0, result.stdout
    return _identifier(r"account_id=(\d+)", result.stdout)


def _wallet(account_id: int, name: str) -> int:
    result = runner.invoke(
        cli, ["money", "wallet-add", "--account-id", str(account_id), "--name", name]
    )
    assert result.exit_code == 0, result.stdout
    return _identifier(r"wallet_id=(\d+)", result.stdout)


class TestArrangementCommands:
    """Arranging is presentation: it stores an order and moves nothing."""

    def test_wallet_order_stores_the_sequence_it_was_given(
        self, initialized_home: Path
    ) -> None:
        account_id = _account("測試帳戶")
        food = _wallet(account_id, "飲食")
        travel = _wallet(account_id, "旅遊")
        unallocated = json.loads(
            runner.invoke(cli, ["money", "balances", "--json"]).stdout
        )["accounts"][0]["wallets"]
        residual = next(
            item["wallet_id"] for item in unallocated if item["name"] == "尚未分配"
        )

        result = runner.invoke(
            cli,
            [
                "money",
                "wallet-order",
                str(account_id),
                "--position",
                f"{travel}:1",
                "--position",
                f"{food}:2",
                "--position",
                f"{residual}:3",
            ],
        )

        assert result.exit_code == 0
        names = [
            item["name"]
            for item in json.loads(
                runner.invoke(cli, ["money", "balances", "--json"]).stdout
            )["accounts"][0]["wallets"]
        ]
        assert names == ["旅遊", "飲食", "尚未分配"]

    def test_wallet_order_refuses_a_partial_arrangement(
        self, initialized_home: Path
    ) -> None:
        account_id = _account("測試帳戶")
        food = _wallet(account_id, "飲食")

        result = runner.invoke(
            cli, ["money", "wallet-order", str(account_id), "--position", f"{food}:1"]
        )

        assert result.exit_code == 1
        assert "exactly once" in result.output

    def test_account_order_stores_the_sequence_it_was_given(
        self, initialized_home: Path
    ) -> None:
        first = _account("先開的")
        second = _account("後開的", institution="other_bank")

        result = runner.invoke(
            cli,
            [
                "money",
                "account-order",
                "--position",
                f"{second}:1",
                "--position",
                f"{first}:2",
            ],
        )

        assert result.exit_code == 0
        names = [
            item["display_name"]
            for item in json.loads(
                runner.invoke(cli, ["money", "status", "--json"]).stdout
            )["accounts"]
        ]
        assert names == ["後開的", "先開的"]


def _gmail_account(home: Path) -> int:
    """Create the mailbox identity a card is registered against."""
    from app.infrastructure.config.settings import AppSettings
    from app.infrastructure.database.engine import create_database
    from app.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork

    settings = AppSettings(
        data_dir=home / "data",
        database_path=home / "data/app.sqlite3",
        log_dir=home / "data/logs",
    )
    database = create_database(settings)
    try:
        with SqlAlchemyUnitOfWork(database.session_factory) as unit:
            account = unit.accounts.get_or_create("user@example.test")
            unit.commit()
            return account.id
    finally:
        database.engine.dispose()


class TestCardCommands:
    """A card is registered, then configured; readiness needs both accounts."""

    def test_a_card_is_registered_incomplete_and_configured_ready(
        self, initialized_home: Path
    ) -> None:
        settlement = _account("結帳帳戶")
        liability = _account("卡費", institution="test_bank", kind="card_liability")
        gmail_account_id = _gmail_account(initialized_home)

        registered = runner.invoke(
            cli,
            [
                "money",
                "card-register",
                "--gmail-account-id",
                str(gmail_account_id),
                "--issuer",
                "test_bank",
                "--name",
                "測試卡",
                "--last-four",
                "4321",
                "--holder-role",
                "primary",
            ],
        )
        assert registered.exit_code == 0, registered.output
        assert "status=incomplete" in registered.stdout
        card_id = _identifier(r"card_id=(\d+)", registered.stdout)

        configured = runner.invoke(
            cli,
            [
                "money",
                "card-configure",
                str(card_id),
                "--expected-version",
                "1",
                "--name",
                "測試卡",
                "--holder-role",
                "primary",
                "--liability-account-id",
                str(liability),
                "--settlement-account-id",
                str(settlement),
            ],
        )

        assert configured.exit_code == 0, configured.output
        assert "status=ready" in configured.stdout
        cards = json.loads(runner.invoke(cli, ["money", "status", "--json"]).stdout)[
            "cards"
        ]
        assert [card["is_ready"] for card in cards] == [True]

    def test_a_stale_card_version_is_refused(self, initialized_home: Path) -> None:
        _account("結帳帳戶")
        gmail_account_id = _gmail_account(initialized_home)
        registered = runner.invoke(
            cli,
            [
                "money",
                "card-register",
                "--gmail-account-id",
                str(gmail_account_id),
                "--issuer",
                "test_bank",
                "--name",
                "測試卡",
                "--last-four",
                "4321",
            ],
        )
        card_id = _identifier(r"card_id=(\d+)", registered.stdout)

        result = runner.invoke(
            cli,
            [
                "money",
                "card-configure",
                str(card_id),
                "--expected-version",
                "99",
                "--name",
                "測試卡",
                "--holder-role",
                "primary",
            ],
        )

        assert result.exit_code == 1


class TestCategoryAndNoteCommands:
    """Correcting a category and annotating an event both use their version."""

    def test_a_category_is_renamed_and_deactivated(
        self, initialized_home: Path
    ) -> None:
        added = runner.invoke(cli, ["money", "category-add", "飲食"])
        category_id = _identifier(r"category (\d+)", added.stdout)

        renamed = runner.invoke(
            cli,
            [
                "money",
                "category-update",
                str(category_id),
                "--expected-version",
                "1",
                "--name",
                "餐飲",
            ],
        )
        deactivated = runner.invoke(
            cli,
            [
                "money",
                "category-update",
                str(category_id),
                "--expected-version",
                "2",
                "--name",
                "餐飲",
                "--inactive",
            ],
        )

        assert renamed.exit_code == 0, renamed.stdout
        assert "active=True" in renamed.stdout
        assert deactivated.exit_code == 0, deactivated.stdout
        assert "active=False" in deactivated.stdout
        assert "餐飲" in runner.invoke(cli, ["money", "category-list"]).stdout

    def test_a_stale_category_version_is_refused(self, initialized_home: Path) -> None:
        added = runner.invoke(cli, ["money", "category-add", "飲食"])
        category_id = _identifier(r"category (\d+)", added.stdout)

        result = runner.invoke(
            cli,
            [
                "money",
                "category-update",
                str(category_id),
                "--expected-version",
                "99",
                "--name",
                "餐飲",
            ],
        )

        assert result.exit_code == 1


class TestWalletReallocateCommand:
    """A cell edit divides one account and never resizes it."""

    def _seeded_account(self) -> tuple[int, int, int]:
        account_id = _account("測試帳戶")
        food = _wallet(account_id, "飲食")
        runner.invoke(
            cli,
            [
                "money",
                "opening-balance",
                "--account-id",
                str(account_id),
                "--amount-minor",
                "500000",
                "--observed-at",
                "2026-08-01T00:00:00+08:00",
                "--split",
                "尚未分配=300000",
                "--split",
                "飲食=200000",
            ],
        )
        residual = next(
            item["wallet_id"]
            for item in json.loads(
                runner.invoke(cli, ["money", "balances", "--json"]).stdout
            )["accounts"][0]["wallets"]
            if item["name"] == "尚未分配"
        )
        return account_id, food, residual

    def test_a_preview_moves_nothing(self, initialized_home: Path) -> None:
        _, food, residual = self._seeded_account()

        result = runner.invoke(
            cli,
            [
                "money",
                "wallet-reallocate",
                str(food),
                "--counterpart",
                str(residual),
                "--expected-minor",
                "200000",
                "--target",
                "2500",
                "--currency",
                "TWD",
                "--client-token",
                "cli-1",
            ],
        )

        assert result.exit_code == 0
        assert "mode=dry-run" in result.stdout
        assert "Nothing was posted" in result.stdout
        after = json.loads(runner.invoke(cli, ["money", "balances", "--json"]).stdout)[
            "accounts"
        ][0]
        assert after["balance_minor"] == 500_000

    def test_applying_moves_between_wallets_and_keeps_the_total(
        self, initialized_home: Path
    ) -> None:
        _, food, residual = self._seeded_account()

        result = runner.invoke(
            cli,
            [
                "money",
                "wallet-reallocate",
                str(food),
                "--counterpart",
                str(residual),
                "--expected-minor",
                "200000",
                "--target",
                "2500",
                "--currency",
                "TWD",
                "--client-token",
                "cli-1",
                "--apply",
            ],
        )

        assert result.exit_code == 0, result.stdout
        account = json.loads(
            runner.invoke(cli, ["money", "balances", "--json"]).stdout
        )["accounts"][0]
        wallets = {item["name"]: item["balance_minor"] for item in account["wallets"]}
        assert wallets["飲食"] == 250_000
        assert wallets["尚未分配"] == 250_000
        assert account["balance_minor"] == 500_000

    def test_a_wallet_may_be_driven_negative_as_the_web_allows(
        self, initialized_home: Path
    ) -> None:
        """A wallet can be overdrawn, so the CLI must accept what the Web does."""
        _, food, residual = self._seeded_account()

        result = runner.invoke(
            cli,
            [
                "money",
                "wallet-reallocate",
                str(food),
                "--counterpart",
                str(residual),
                "--expected-minor",
                "200000",
                "--target",
                "-500",
                "--currency",
                "TWD",
                "--client-token",
                "cli-negative",
                "--apply",
            ],
        )

        assert result.exit_code == 0, result.output
        wallets = {
            item["name"]: item["balance_minor"]
            for item in json.loads(
                runner.invoke(cli, ["money", "balances", "--json"]).stdout
            )["accounts"][0]["wallets"]
        }
        assert wallets["飲食"] == -50_000
        assert wallets["尚未分配"] == 550_000

    def test_a_stale_expected_balance_is_refused(self, initialized_home: Path) -> None:
        _, food, residual = self._seeded_account()

        result = runner.invoke(
            cli,
            [
                "money",
                "wallet-reallocate",
                str(food),
                "--counterpart",
                str(residual),
                "--expected-minor",
                "199999",
                "--target",
                "2500",
                "--currency",
                "TWD",
                "--client-token",
                "cli-1",
                "--apply",
            ],
        )

        assert result.exit_code == 1
        assert "reload and retry" in result.output


class TestDailyLoopCommands:
    """The two steps the whole product is used for, driven without a browser."""

    def _staged_event(self, home: Path) -> tuple[int, int]:
        """Seed a confirmed card transaction and stage its event."""
        _seed_confirmed_transaction(home)
        account_id = _account("結帳帳戶")
        liability_id = _account("卡費", institution="test_bank", kind="card_liability")
        wallet_id = _wallet(account_id, "飲食")
        # A card purchase cannot post until its card says where it settles.
        registered = runner.invoke(
            cli,
            [
                "money",
                "card-register",
                "--gmail-account-id",
                str(_gmail_account(home)),
                "--issuer",
                "test_bank",
                "--name",
                "測試卡",
                "--last-four",
                "4321",
            ],
        )
        card_id = _identifier(r"card_id=(\d+)", registered.stdout)
        runner.invoke(
            cli,
            [
                "money",
                "card-configure",
                str(card_id),
                "--expected-version",
                "1",
                "--name",
                "測試卡",
                "--holder-role",
                "primary",
                "--liability-account-id",
                str(liability_id),
                "--settlement-account-id",
                str(account_id),
            ],
        )
        runner.invoke(
            cli,
            [
                "money",
                "opening-balance",
                "--account-id",
                str(account_id),
                "--amount-minor",
                "500000",
                "--observed-at",
                "2026-08-01T00:00:00+08:00",
                "--split",
                "尚未分配=300000",
                "--split",
                "飲食=200000",
            ],
        )
        # Intake runs last: registering a card never backfills events that
        # were staged before it existed.
        applied = runner.invoke(cli, ["money", "intake-confirmed", "--apply"])
        assert applied.exit_code == 0, applied.output
        return 1, wallet_id

    def test_a_preview_names_the_mismatch_before_anything_posts(
        self, initialized_home: Path
    ) -> None:
        """An operator that computed the wrong amount must see it first."""
        event_id, wallet_id = self._staged_event(initialized_home)

        result = runner.invoke(
            cli,
            [
                "money",
                "allocate",
                str(event_id),
                "--expected-version",
                "1",
                "--client-token",
                "cli-1",
                "--line",
                f"100:{wallet_id}",
            ],
        )

        assert result.exit_code == 0, result.output
        # The event is TWD 1,280; the line is TWD 100. Both are printed.
        assert "total=TWD 100" in result.stdout
        assert "event_amount=TWD 1,280" in result.stdout
        assert "Nothing was posted" in result.stdout

    def test_an_applied_allocation_posts_a_balanced_entry(
        self, initialized_home: Path
    ) -> None:
        event_id, wallet_id = self._staged_event(initialized_home)

        result = runner.invoke(
            cli,
            [
                "money",
                "allocate",
                str(event_id),
                "--expected-version",
                "1",
                "--client-token",
                "cli-1",
                "--line",
                f"1280:{wallet_id}",
                "--apply",
            ],
        )

        assert result.exit_code == 0, result.output
        assert "mode=apply" in result.stdout
        assert "status=posted" in result.stdout

    def test_an_incomplete_split_is_refused_without_posting(
        self, initialized_home: Path
    ) -> None:
        event_id, wallet_id = self._staged_event(initialized_home)

        result = runner.invoke(
            cli,
            [
                "money",
                "allocate",
                str(event_id),
                "--expected-version",
                "1",
                "--client-token",
                "cli-1",
                "--line",
                f"100:{wallet_id}",
                "--apply",
            ],
        )

        assert result.exit_code == 1
        assert "Could not allocate" in result.output

    def test_confirming_previews_the_ids_it_was_given(
        self, initialized_home: Path
    ) -> None:
        result = runner.invoke(cli, ["money", "transaction-confirm", "7", "7", "3"])

        assert result.exit_code == 0
        # Deduplicated and ordered, so the caller sees its own request back.
        assert "requested=2" in result.stdout
        assert "transaction_id=3" in result.stdout
        assert "Nothing changed" in result.stdout

    def test_confirming_nothing_is_refused(self, initialized_home: Path) -> None:
        result = runner.invoke(cli, ["money", "transaction-confirm", "--apply"])

        assert result.exit_code != 0


class TestReadBeforeWriteCommands:
    """A write that quotes a version needs a read that reports one."""

    def test_a_transaction_reports_the_token_its_review_demands(
        self, initialized_home: Path
    ) -> None:
        _seed_confirmed_transaction(initialized_home)

        result = runner.invoke(cli, ["money", "transaction-show", "1", "--json"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["id"] == 1
        assert payload["updated_at"]
        assert payload["email_id"] >= 1

    def test_an_unknown_transaction_is_refused(self, initialized_home: Path) -> None:
        result = runner.invoke(cli, ["money", "transaction-show", "999"])

        assert result.exit_code == 1

    def test_reviewing_a_transaction_takes_only_the_named_overrides(
        self, initialized_home: Path
    ) -> None:
        _seed_confirmed_transaction(initialized_home)
        shown = json.loads(
            runner.invoke(cli, ["money", "transaction-show", "1", "--json"]).stdout
        )

        result = runner.invoke(
            cli,
            [
                "money",
                "transaction-review",
                "1",
                "--expected-updated-at",
                shown["updated_at"],
                "--type",
                "purchase",
                "--override",
                "merchant",
                "--merchant",
                "改過的商家",
                "--confirmed",
            ],
        )

        assert result.exit_code == 0, result.output
        after = json.loads(
            runner.invoke(cli, ["money", "transaction-show", "1", "--json"]).stdout
        )
        assert after["merchant"] == "改過的商家"
        assert after["manual_overrides"] == ["merchant"]

    def test_a_stale_review_token_is_refused(self, initialized_home: Path) -> None:
        _seed_confirmed_transaction(initialized_home)

        result = runner.invoke(
            cli,
            [
                "money",
                "transaction-review",
                "1",
                "--expected-updated-at",
                "2020-01-01T00:00:00+00:00",
                "--type",
                "purchase",
            ],
        )

        assert result.exit_code == 1

    def test_a_supplementary_card_is_marked_and_unmarked(
        self, initialized_home: Path
    ) -> None:
        _seed_confirmed_transaction(initialized_home)

        marked = runner.invoke(
            cli, ["money", "supplementary-card-set", "1", "--supplementary"]
        )
        unmarked = runner.invoke(
            cli, ["money", "supplementary-card-set", "1", "--primary"]
        )

        assert marked.exit_code == 0, marked.output
        assert "supplementary=True" in marked.stdout
        assert unmarked.exit_code == 0, unmarked.output
        assert "supplementary=False" in unmarked.stdout

    def test_reparsing_an_email_reads_retained_text_only(
        self, initialized_home: Path
    ) -> None:
        _seed_confirmed_transaction(initialized_home)
        email_id = json.loads(
            runner.invoke(cli, ["money", "transaction-show", "1", "--json"]).stdout
        )["email_id"]

        result = runner.invoke(cli, ["money", "email-reparse", str(email_id)])

        assert result.exit_code == 0, result.output
        assert f"email_id={email_id}" in result.stdout
        assert "classification=" in result.stdout

    def test_reparsing_an_unknown_email_is_refused(
        self, initialized_home: Path
    ) -> None:
        result = runner.invoke(cli, ["money", "email-reparse", "999"])

        assert result.exit_code == 1

    def test_cancelling_a_sync_that_is_not_running_is_refused(
        self, initialized_home: Path
    ) -> None:
        """Cancellation is persisted, so an unknown run must not look accepted."""
        result = runner.invoke(cli, ["money", "sync-cancel", "no-such-run"])

        assert result.exit_code == 1
        assert "Could not cancel" in result.output


class TestEventNoteCommand:
    """A note is user metadata; it never touches the posting."""

    def test_a_note_is_written_and_cleared_under_its_version(
        self, initialized_home: Path
    ) -> None:
        _seed_confirmed_transaction(initialized_home)
        runner.invoke(cli, ["money", "intake-confirmed", "--apply"])
        before = json.loads(
            runner.invoke(cli, ["money", "event-show", "1", "--json"]).stdout
        )

        written = runner.invoke(
            cli,
            [
                "money",
                "event-note",
                "1",
                "--expected-version",
                str(before["version"]),
                "--note",
                "對帳用備註",
            ],
        )
        after = json.loads(
            runner.invoke(cli, ["money", "event-show", "1", "--json"]).stdout
        )
        cleared = runner.invoke(
            cli,
            ["money", "event-note", "1", "--expected-version", str(after["version"])],
        )

        assert written.exit_code == 0, written.output
        assert after["note"] == "對帳用備註"
        assert after["status"] == before["status"], "a note must not post anything"
        assert cleared.exit_code == 0, cleared.output
        assert (
            json.loads(
                runner.invoke(cli, ["money", "event-show", "1", "--json"]).stdout
            )["note"]
            is None
        )

    def test_a_stale_event_version_is_refused(self, initialized_home: Path) -> None:
        _seed_confirmed_transaction(initialized_home)
        runner.invoke(cli, ["money", "intake-confirmed", "--apply"])

        result = runner.invoke(
            cli, ["money", "event-note", "1", "--expected-version", "99", "--note", "x"]
        )

        assert result.exit_code == 1

    def test_an_unknown_event_is_refused(self, initialized_home: Path) -> None:
        result = runner.invoke(cli, ["money", "event-show", "999"])

        assert result.exit_code == 1

"""Clearing hub commands: diagnostics, scheduling, dry runs, and reversal."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from app.application.services.cards import (
    ConfigureCardRequest,
    ManageCards,
    RegisterCardRequest,
)
from app.application.services.event_allocation import (
    AllocateFinancialEvent,
    AllocateFinancialEventRequest,
    AllocationLine,
)
from app.application.services.ledger import JournalService
from app.domain.budget_enums import (
    CardHolderRole,
    FinancialEventOrigin,
    FinancialEventType,
    PaymentMethod,
    RecognitionBasis,
)
from app.domain.budget_models import BudgetCategoryDraft, FinancialEventDraft
from app.infrastructure.config.settings import AppSettings
from app.infrastructure.database.engine import create_database
from app.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork
from app.presentation.cli.app import cli
from typer.testing import CliRunner, Result

runner = CliRunner()

OCCURRED_AT = datetime(2026, 8, 7, 13, 30, tzinfo=UTC)
CROSS_BANK_MINOR = 8_000


def _run(*arguments: str) -> Result:
    return runner.invoke(cli, list(arguments))


def _yesterday() -> str:
    return (datetime.now(UTC).date() - timedelta(days=1)).isoformat()


def _tomorrow() -> str:
    return (datetime.now(UTC).date() + timedelta(days=1)).isoformat()


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Initialize a local database with two test banks, a card, and a reservation."""
    monkeypatch.chdir(tmp_path)
    assert _run("init").exit_code == 0
    for institution, name, kind in (
        ("test_bank_a", "測試主要帳戶", "checking"),
        ("test_bank_b", "測試次要帳戶", "checking"),
        ("test_bank_a", "測試卡負債", "card_liability"),
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
                kind,
                "--currency",
                "TWD",
            ).exit_code
            == 0
        )
    assert (
        _run(
            "money",
            "wallet-add",
            "--account-id",
            "1",
            "--name",
            "測試帳單暫存",
            "--purpose",
            "clearing_hub",
        ).exit_code
        == 0
    )
    assert (
        _run(
            "money", "wallet-add", "--account-id", "2", "--name", "測試次要預算"
        ).exit_code
        == 0
    )
    assert (
        _run(
            "money",
            "opening-balance",
            "--account-id",
            "1",
            "--amount-minor",
            "500000",
            "--observed-at",
            "2026-08-07T21:30",
            "--split",
            "尚未分配=400000",
            "--split",
            "測試帳單暫存=100000",
        ).exit_code
        == 0
    )
    assert (
        _run(
            "money",
            "opening-balance",
            "--account-id",
            "2",
            "--amount-minor",
            "300000",
            "--observed-at",
            "2026-08-07T21:30",
            "--split",
            "尚未分配=100000",
            "--split",
            "測試次要預算=200000",
        ).exit_code
        == 0
    )
    _seed_card_reservation()


def _seed_card_reservation() -> None:
    """Create one card and one cross-bank reservation the CLI can schedule.

    B6 adds no card or purchase command, so the reservation is built through
    the same services the web pages use, against the database the CLI just
    initialized.
    """
    settings = AppSettings()
    database = create_database(settings)
    try:

        def factory() -> SqlAlchemyUnitOfWork:
            return SqlAlchemyUnitOfWork(database.session_factory)

        with factory() as unit:
            gmail_account = unit.accounts.get_or_create("user@example.test")
            category = unit.budget_categories.add(BudgetCategoryDraft(name="測試分類"))
            unit.commit()
            gmail_account_id = gmail_account.id
            category_id = category.id
        cards = ManageCards(factory)
        card = cards.register(
            RegisterCardRequest(
                gmail_account_id=gmail_account_id,
                issuer_code="test_bank_a",
                display_name="測試卡",
                last_four="4321",
                holder_role=CardHolderRole.PRIMARY,
            )
        )
        cards.configure(
            ConfigureCardRequest(
                card_id=card.id,
                expected_version=card.version,
                display_name="測試卡",
                holder_role=CardHolderRole.PRIMARY,
                liability_account_id=3,
                settlement_account_id=1,
                default_funding_wallet_id=1,
            )
        )
        with factory() as unit:
            event = unit.financial_events.add(
                FinancialEventDraft(
                    origin=FinancialEventOrigin.GMAIL_TRANSACTION,
                    event_type=FinancialEventType.PURCHASE,
                    payment_method=PaymentMethod.CREDIT_CARD,
                    recognition_basis=RecognitionBasis.GMAIL_NOTIFICATION,
                    occurred_at=OCCURRED_AT,
                    amount_minor=CROSS_BANK_MINOR,
                    currency="TWD",
                    idempotency_key="test:cross-bank",
                    merchant="測試商店",
                    card_id=card.id,
                )
            )
            unit.commit()
            event_id = event.id
        AllocateFinancialEvent(factory, JournalService(factory)).execute(
            AllocateFinancialEventRequest(
                financial_event_id=event_id,
                expected_version=1,
                client_token="token-cross-bank",
                lines=(
                    AllocationLine(
                        amount_minor=CROSS_BANK_MINOR,
                        funding_wallet_id=4,
                        budget_category_id=category_id,
                    ),
                ),
            )
        )
    finally:
        database.engine.dispose()


def _schedule(*, scheduled_date: str, token: str, overdue: bool) -> Result:
    arguments = [
        "money",
        "clearing-schedule",
        "--source-account-id",
        "2",
        "--source-wallet-id",
        "2",
        "--destination-account-id",
        "1",
        "--destination-wallet-id",
        "3",
        "--amount-minor",
        str(CROSS_BANK_MINOR),
        "--date",
        scheduled_date,
        "--client-token",
        token,
        "--cover",
        f"1:{CROSS_BANK_MINOR}",
    ]
    if overdue:
        arguments.append("--allow-overdue")
    return _run(*arguments)


def test_clearing_show_reports_positions_without_claiming_a_confirmation(
    ledger: None,
) -> None:
    result = _run("money", "clearing-show")

    assert result.exit_code == 0
    assert "測試帳單暫存" in result.stdout
    assert "reserved=" in result.stdout
    assert "available=" in result.stdout
    assert "not confirmed by a bank" in result.stdout
    assert "bank confirmed" not in result.stdout


def test_an_already_due_schedule_requires_allow_overdue(ledger: None) -> None:
    refused = _schedule(scheduled_date=_yesterday(), token="cli-overdue", overdue=False)
    accepted = _schedule(scheduled_date=_yesterday(), token="cli-overdue", overdue=True)

    assert refused.exit_code == 1
    assert "--allow-overdue" in refused.stderr
    assert accepted.exit_code == 0
    assert "status=scheduled" in accepted.stdout
    assert "Nothing has moved yet" in accepted.stdout


def test_run_due_is_a_dry_run_by_default_and_writes_nothing(ledger: None) -> None:
    assert (
        _schedule(scheduled_date=_yesterday(), token="cli-dry", overdue=True).exit_code
        == 0
    )
    before = _run("money", "clearing-show").stdout

    dry = _run("money", "run-due")
    after = _run("money", "clearing-show").stdout

    assert dry.exit_code == 0
    assert "mode=dry-run" in dry.stdout
    assert "claimed=1" in dry.stdout
    assert "Dry run: nothing was claimed, posted, or changed." in dry.stdout
    assert after == before


def test_run_due_apply_executes_and_records_the_processed_time(
    ledger: None,
) -> None:
    assert (
        _schedule(
            scheduled_date=_yesterday(), token="cli-apply", overdue=True
        ).exit_code
        == 0
    )

    applied = _run("money", "run-due", "--apply")
    repeated = _run("money", "run-due", "--apply")
    shown = _run("money", "clearing-show")

    assert applied.exit_code == 0
    assert "mode=apply claimed=1 executed=1" in applied.stdout
    assert "not confirmed by a bank" in applied.stdout
    assert "claimed=0" in repeated.stdout
    assert "status=assumed_executed" in shown.stdout


def test_a_future_schedule_is_never_claimed_by_a_dry_run(ledger: None) -> None:
    assert (
        _schedule(
            scheduled_date=_tomorrow(), token="cli-future", overdue=False
        ).exit_code
        == 0
    )

    dry = _run("money", "run-due")

    assert "claimed=0" in dry.stdout


def test_cancel_and_reverse_move_the_schedule_through_its_lifecycle(
    ledger: None,
) -> None:
    assert (
        _schedule(
            scheduled_date=_tomorrow(), token="cli-cancel", overdue=False
        ).exit_code
        == 0
    )

    cancelled = _run("money", "schedule-cancel", "1", "--expected-version", "1")

    assert cancelled.exit_code == 0
    assert "status=cancelled" in cancelled.stdout
    assert "released" in cancelled.stdout

    assert (
        _schedule(
            scheduled_date=_yesterday(), token="cli-reverse", overdue=True
        ).exit_code
        == 0
    )
    assert _run("money", "run-due", "--apply").exit_code == 0
    # Creating writes version 1, claiming writes 2, and finishing writes 3.
    reversed_result = _run("money", "schedule-reverse", "2", "--expected-version", "3")

    assert reversed_result.exit_code == 0
    assert "status=reversed" in reversed_result.stdout
    assert "reversal_entry=" in reversed_result.stdout


def test_resending_a_reversal_succeeds_again(ledger: None) -> None:
    """The same command twice reports the same reversal, not a failure."""
    assert (
        _schedule(
            scheduled_date=_yesterday(), token="cli-resend", overdue=True
        ).exit_code
        == 0
    )
    assert _run("money", "run-due", "--apply").exit_code == 0

    first = _run("money", "schedule-reverse", "1", "--expected-version", "3")
    second = _run("money", "schedule-reverse", "1", "--expected-version", "3")

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert "status=reversed" in second.stdout
    entry = [line for line in first.stdout.splitlines() if "reversal_entry=" in line]
    repeat = [line for line in second.stdout.splitlines() if "reversal_entry=" in line]
    assert entry == repeat


def test_a_stale_version_is_refused(ledger: None) -> None:
    assert (
        _schedule(
            scheduled_date=_tomorrow(), token="cli-stale", overdue=False
        ).exit_code
        == 0
    )

    result = _run("money", "schedule-cancel", "1", "--expected-version", "99")

    assert result.exit_code == 1
    assert "Could not cancel the schedule" in result.stderr


def test_retrying_something_that_did_not_fail_is_refused(ledger: None) -> None:
    assert (
        _schedule(
            scheduled_date=_tomorrow(), token="cli-retry", overdue=False
        ).exit_code
        == 0
    )

    result = _run("money", "schedule-retry", "1", "--expected-version", "1")

    assert result.exit_code == 1
    assert "only a failed scheduled transfer is retried by hand" in result.stderr


def test_coverage_is_written_as_commitment_id_and_amount(ledger: None) -> None:
    result = _run(
        "money",
        "clearing-schedule",
        "--source-account-id",
        "2",
        "--source-wallet-id",
        "2",
        "--destination-account-id",
        "1",
        "--destination-wallet-id",
        "3",
        "--amount-minor",
        str(CROSS_BANK_MINOR),
        "--date",
        _tomorrow(),
        "--client-token",
        "cli-bad-cover",
        "--cover",
        "not-a-pair",
    )

    assert result.exit_code == 1
    assert "commitment_id:amount_minor" in result.stderr


def test_the_scheduled_date_must_be_a_calendar_date(ledger: None) -> None:
    result = _run(
        "money",
        "clearing-schedule",
        "--source-account-id",
        "2",
        "--source-wallet-id",
        "2",
        "--destination-account-id",
        "1",
        "--destination-wallet-id",
        "3",
        "--amount-minor",
        str(CROSS_BANK_MINOR),
        "--date",
        "2026-13-40",
        "--client-token",
        "cli-bad-date",
    )

    assert result.exit_code == 1


def test_autopay_schedules_a_card_payment_from_its_settlement_account(
    ledger: None,
) -> None:
    result = _run(
        "money",
        "autopay-schedule",
        "--card-id",
        "1",
        "--source-wallet-id",
        "1",
        "--amount-minor",
        "5000",
        "--date",
        _tomorrow(),
        "--client-token",
        "cli-autopay",
    )

    assert result.exit_code == 0
    assert "purpose=card_autopay" in result.stdout
    assert "Nothing has moved yet" in result.stdout


def test_every_command_disposes_its_engine(
    ledger: None, recwarn: pytest.WarningsRecorder
) -> None:
    """A leaked SQLite pool would surface here as a ResourceWarning."""
    assert _run("money", "clearing-show").exit_code == 0
    assert _run("money", "run-due").exit_code == 0

    assert [
        warning
        for warning in recwarn.list
        if issubclass(warning.category, ResourceWarning)
    ] == []


def test_a_local_date_is_read_in_the_configured_timezone(ledger: None) -> None:
    scheduled = _schedule(
        scheduled_date=_tomorrow(), token="cli-timezone", overdue=False
    )

    assert scheduled.exit_code == 0
    assert "Asia/Taipei" in scheduled.stdout
    assert "06:00" in scheduled.stdout
    tomorrow = date.fromisoformat(_tomorrow())
    assert f"local={tomorrow.isoformat()}" in scheduled.stdout

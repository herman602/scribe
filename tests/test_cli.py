"""Typer command tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.application.services.gmail_full_sync import FullSyncResult
from app.application.services.gmail_incremental_sync import IncrementalSyncResult
from app.domain.enums import RunStatus, SyncMode
from app.domain.errors import GmailAuthenticationError
from app.domain.gmail import GmailAccountProfile
from app.infrastructure.database.locking import SharedDatabaseLock
from app.presentation.cli.app import cli
from typer.testing import CliRunner

runner = CliRunner()


def test_cli_help_lists_milestone_four_commands() -> None:
    result = runner.invoke(cli, ["--help"])

    assert result.exit_code == 0
    assert "init" in result.stdout
    assert "doctor" in result.stdout
    assert "auth" in result.stdout
    assert "serve" in result.stdout
    assert "sync" in result.stdout
    assert "sender-rules" in result.stdout
    assert "service" in result.stdout


def test_init_and_doctor_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    initialized = runner.invoke(cli, ["init"])
    initialized_again = runner.invoke(cli, ["init"])
    diagnosed = runner.invoke(cli, ["doctor"])

    assert initialized.exit_code == 0
    assert (tmp_path / "config.toml").is_file()
    assert (tmp_path / "data/logs").is_dir()
    assert (tmp_path / "data/app.sqlite3").is_file()
    assert "revision=0022" in initialized.stdout
    assert "Local foundation initialized" in initialized.stdout
    assert initialized_again.exit_code == 0
    assert "revision=0022" in initialized_again.stdout
    assert diagnosed.exit_code == 1
    assert "PASS  Python version" in diagnosed.stdout
    assert "PASS  SQLite connection" in diagnosed.stdout
    assert "PASS  migration status" in diagnosed.stdout
    assert "FAIL  Gmail credentials" in diagnosed.stdout
    assert "FAIL  Gmail token" in diagnosed.stdout


def test_auth_command_reports_profile_without_exposing_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(cli, ["init"]).exit_code == 0
    profile = GmailAccountProfile(
        email_address="user@example.com",
        messages_total=10,
        threads_total=4,
        history_id="123",
    )
    service = SimpleNamespace(
        execute=lambda: SimpleNamespace(profile=profile),
    )
    monkeypatch.setattr(
        "app.presentation.cli.app.build_gmail_authentication_service",
        lambda settings: service,
    )

    result = runner.invoke(cli, ["auth"])

    assert result.exit_code == 0
    assert "Authorized Gmail account: user@example.com" in result.stdout
    assert "10 messages, 4 threads" in result.stdout
    assert "fixture-access-token" not in result.stdout


def test_auth_command_reports_safe_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(cli, ["init"]).exit_code == 0
    service = SimpleNamespace(
        execute=lambda: (_ for _ in ()).throw(
            GmailAuthenticationError("authorization rejected")
        ),
    )
    monkeypatch.setattr(
        "app.presentation.cli.app.build_gmail_authentication_service",
        lambda settings: service,
    )

    result = runner.invoke(cli, ["auth"])

    assert result.exit_code == 1
    assert "authorization rejected" in result.stderr


def test_label_processed_command_reports_backfill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(cli, ["init"]).exit_code == 0
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "gmail_labeling_enabled = false",
            "gmail_labeling_enabled = true",
        ),
        encoding="utf-8",
    )
    service = SimpleNamespace(
        backfill=lambda: SimpleNamespace(
            queued_count=3,
            succeeded_count=3,
            pending_count=0,
            failed_count=0,
            error_message=None,
        )
    )
    monkeypatch.setattr(
        "app.presentation.cli.app.build_processed_label_service",
        lambda settings: service,
    )

    result = runner.invoke(cli, ["label-processed"])

    assert result.exit_code == 0
    assert "label=Scribe/已處理" in result.stdout
    assert "queued=3 succeeded=3" in result.stdout


def test_serve_rejects_public_host_override() -> None:
    result = runner.invoke(cli, ["serve", "--host", "0.0.0.0"])

    assert result.exit_code == 2
    assert "Configuration error" in result.stderr


def test_incremental_sync_command_reports_message_level_history_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(cli, ["init"]).exit_code == 0
    service = SimpleNamespace(
        execute=lambda: IncrementalSyncResult(
            run_id="fixture-incremental",
            status=RunStatus.SUCCEEDED,
            mode=SyncMode.INCREMENTAL,
            start_history_id="100",
            end_history_id="110",
            pages_processed=1,
            discovered_count=3,
            new_count=1,
            updated_count=1,
            deleted_count=1,
            failed_count=0,
            parser_failed_count=0,
        )
    )
    monkeypatch.setattr(
        "app.presentation.cli.app.build_incremental_sync_service",
        lambda settings: service,
    )

    result = runner.invoke(cli, ["sync"])

    assert result.exit_code == 0
    assert "mode=incremental" in result.stdout
    assert "deleted=1" in result.stdout
    assert "processed by Gmail message ID" in result.stdout


def test_incremental_sync_rejects_full_only_options() -> None:
    result = runner.invoke(cli, ["sync", "--query", "newer_than:1y"])

    assert result.exit_code == 2
    assert "apply only to sync --full" in result.stderr


def test_full_sync_command_reports_message_level_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(cli, ["init"]).exit_code == 0
    service = SimpleNamespace(
        execute=lambda request: FullSyncResult(
            run_id="fixture-run",
            status=RunStatus.SUCCEEDED,
            query="{from:notice@bank.example}",
            thread_expansion_enabled=True,
            pages_processed=2,
            discovered_count=3,
            new_count=3,
            updated_count=0,
            failed_count=0,
            baseline_history_id="100",
        )
    )
    monkeypatch.setattr(
        "app.presentation.cli.app.build_full_sync_service",
        lambda settings: service,
    )

    result = runner.invoke(
        cli,
        ["sync", "--full", "--query", "newer_than:2y"],
    )

    assert result.exit_code == 0
    assert "run_id=fixture-run status=succeeded" in result.stdout
    assert "sender-filtered threads expanded message by message" in result.stdout


def test_schema_describes_tables_without_needing_a_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mapped metadata answers before anything has been migrated."""
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli, ["schema", "--table", "wallets"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    columns = {column["name"] for column in payload["tables"]["wallets"]["columns"]}
    assert {"id", "financial_account_id", "name", "display_order"} <= columns
    assert not (tmp_path / "data/app.sqlite3").exists()


def test_schema_refuses_a_table_that_does_not_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli, ["schema", "--table", "no_such_table"])

    assert result.exit_code == 1


def test_doctor_json_carries_the_same_checks_as_its_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    runner.invoke(cli, ["init"])

    text = runner.invoke(cli, ["doctor"])
    structured = runner.invoke(cli, ["doctor", "--json"])

    payload = json.loads(structured.stdout)
    assert [check["name"] for check in payload["checks"]] == [
        line.split(": ", 1)[0].split("  ", 1)[1]
        for line in text.stdout.strip().splitlines()
    ]
    assert {check["status"] for check in payload["checks"]} <= {
        "PASS",
        "WARN",
        "FAIL",
    }


def test_backup_writes_a_verified_copy_and_lists_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    runner.invoke(cli, ["init"])

    created = runner.invoke(cli, ["backup"])
    listed = runner.invoke(cli, ["backup-list", "--json"])

    assert created.exit_code == 0, created.output
    assert "verified=True" in created.stdout
    payload = json.loads(listed.stdout)
    assert len(payload["backups"]) == 1
    assert payload["backups"][0]["has_manifest"] is True


def test_restore_previews_and_refuses_without_the_typed_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one command that discards the database needs more than a flag."""
    monkeypatch.chdir(tmp_path)
    runner.invoke(cli, ["init"])
    runner.invoke(cli, ["backup"])
    backup_path = json.loads(runner.invoke(cli, ["backup-list", "--json"]).stdout)[
        "backups"
    ][0]["path"]

    previewed = runner.invoke(cli, ["restore", backup_path])
    flagged = runner.invoke(cli, ["restore", backup_path, "--apply"])
    confirmed = runner.invoke(
        cli, ["restore", backup_path, "--apply", "--confirm", "REPLACE"]
    )

    assert previewed.exit_code == 0
    assert "Nothing was replaced" in previewed.stdout
    assert flagged.exit_code == 1
    assert "--confirm REPLACE" in flagged.output
    assert confirmed.exit_code == 0, confirmed.output
    assert "rollback_copy=" in confirmed.stdout


def test_restoring_a_file_that_is_not_a_database_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    runner.invoke(cli, ["init"])
    broken = tmp_path / "broken.sqlite3"
    broken.write_bytes(b"not a database")

    result = runner.invoke(
        cli, ["restore", str(broken), "--apply", "--confirm", "REPLACE"]
    )

    assert result.exit_code == 1
    assert "Could not read" in result.output


def test_restore_refuses_while_the_database_is_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A serving process holds the claim, and no flag overrides it."""
    monkeypatch.chdir(tmp_path)
    runner.invoke(cli, ["init"])
    runner.invoke(cli, ["backup"])
    backup_path = json.loads(runner.invoke(cli, ["backup-list", "--json"]).stdout)[
        "backups"
    ][0]["path"]

    with SharedDatabaseLock(tmp_path / "data/app.sqlite3"):
        result = runner.invoke(
            cli, ["restore", backup_path, "--apply", "--confirm", "REPLACE"]
        )

    assert result.exit_code == 1
    assert "still in use" in result.output
    assert "--force" not in runner.invoke(cli, ["restore", "--help"]).stdout

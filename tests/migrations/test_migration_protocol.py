"""ADR-B027 migration protocol: rollback, pre-commit checks, and pragmas."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from alembic import command
from app.domain.enums import TransactionType
from app.domain.models import EmailSnapshot, TransactionDraft
from app.infrastructure.config.settings import AppSettings
from app.infrastructure.database.engine import create_database
from app.infrastructure.database.migrations import (
    build_alembic_config,
    inspect_migration_status,
    upgrade_database,
)
from app.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork
from sqlalchemy import Connection, inspect, text
from sqlalchemy.exc import IntegrityError

B3_TABLES = {
    "budget_categories",
    "budget_periods",
    "budget_targets",
    "event_allocations",
    "funding_commitments",
    "journal_entry_types",
}


def _config_at(settings: AppSettings, revision: str) -> Any:
    """Return an Alembic config with the database parked at one revision."""
    config = build_alembic_config(settings.database_path)
    upgrade_database(settings)
    command.downgrade(config, revision)
    return config


def _seed_journal(settings: AppSettings) -> None:
    """Seed the relationships 0007 must preserve.

    Gmail-side rows go through the repositories, whose models B3 does not
    change. Everything B3 does change is inserted as raw SQL, because the ORM
    already carries columns that do not exist at revision 0006.
    """
    database = create_database(settings)
    try:
        with SqlAlchemyUnitOfWork(database.session_factory) as unit:
            account = unit.accounts.get_or_create("user@example.test")
            outcome = unit.emails.upsert_from_gmail(
                EmailSnapshot(
                    gmail_account_id=account.id,
                    gmail_message_id="gmail-protocol-001",
                    gmail_thread_id="thread-protocol-001",
                    gmail_history_id="500",
                    sender="notice@example-bank.test",
                    recipients=("user@example.test",),
                    subject="消費通知",
                    gmail_internal_date_ms=1_754_000_000_000,
                )
            )
            unit.transactions.add(
                TransactionDraft(
                    email_id=outcome.email.id,
                    source_key="row-1",
                    transaction_type=TransactionType.PURCHASE,
                    confidence=Decimal("0.95"),
                    parser_name="tests.fake",
                    parser_version="1",
                    amount_minor=500000,
                    currency="TWD",
                    transaction_at=datetime(2026, 8, 7, 13, 30, tzinfo=UTC),
                )
            )
            unit.commit()
        with database.engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO financial_events (origin, event_type, payment_method,"
                " recognition_basis, occurred_at, merchant, amount_minor, currency,"
                " status, version, idempotency_key, created_at, updated_at) VALUES"
                " ('gmail_transaction','purchase','bank_account','gmail_notification',"
                "'2026-08-07 13:30:00.000000','測試咖啡',500000,'TWD',"
                "'needs_allocation',1,'gmail_transaction:1',"
                "'2026-08-07 13:00:00','2026-08-07 13:00:00')"
            )
            connection.exec_driver_sql(
                "INSERT INTO financial_event_source_links (financial_event_id,"
                " transaction_id, status, match_score_bps, reason_json, reviewed_at,"
                " created_at, updated_at) VALUES (1,1,'confirmed',10000,"
                '\'{"schema_version": 1, "reasons": ["direct_gmail_intake"],'
                " \"discrepancies\": []}','2026-08-08 02:00:00',"
                "'2026-08-07 13:00:00','2026-08-07 13:00:00')"
            )
            connection.exec_driver_sql(
                "INSERT INTO financial_accounts (institution_code, display_name,"
                " account_type, currency, is_active, version, created_at, updated_at)"
                " VALUES ('test_bank','測試帳戶','checking','TWD',1,1,"
                "'2026-08-07 13:00:00','2026-08-07 13:00:00')"
            )
            connection.exec_driver_sql(
                "INSERT INTO wallets (financial_account_id, name, purpose, is_active,"
                " version, created_at, updated_at) VALUES (1,'Unallocated',"
                "'unallocated',1,1,'2026-08-07 13:00:00','2026-08-07 13:00:00')"
            )
            for code, kind, owner in (
                ("asset:test_bank:1", "asset", "1"),
                ("equity:opening:TWD", "equity", "NULL"),
            ):
                connection.exec_driver_sql(
                    "INSERT INTO ledger_accounts (code, name, kind, currency,"
                    f" financial_account_id, created_at, updated_at) VALUES ('{code}',"
                    f"'n','{kind}','TWD',{owner},'2026-08-07 13:00:00',"
                    "'2026-08-07 13:00:00')"
                )
            # The opening entry carries a non-NULL financial_event_id, so a
            # rebuild that dropped or nulled the link would be caught.
            for entry_type, key, reversal, event, description in (
                (
                    "opening_balance",
                    "opening_balance:financial_account:1",
                    "NULL",
                    "1",
                    "期初餘額",
                ),
                ("reversal", "reversal:journal_entry:1", "1", "NULL", "沖銷"),
            ):
                connection.exec_driver_sql(
                    "INSERT INTO journal_entries (entry_type, effective_at,"
                    " recorded_at, idempotency_key, reversal_of_id, description,"
                    " financial_event_id, created_at, updated_at) VALUES"
                    f" ('{entry_type}','2026-08-07 13:30:00.000000',"
                    f"'2026-08-08 02:00:00.000000','{key}',{reversal},"
                    f"'{description}',{event},'2026-08-07 13:00:00',"
                    "'2026-08-07 13:00:00')"
                )
            for entry, ledger, wallet, amount in (
                (1, 1, "1", 500000),
                (1, 2, "NULL", -500000),
                (2, 1, "1", -500000),
                (2, 2, "NULL", 500000),
            ):
                connection.exec_driver_sql(
                    "INSERT INTO journal_postings (journal_entry_id,"
                    " ledger_account_id, wallet_id, amount_minor, currency,"
                    f" created_at, updated_at) VALUES ({entry},{ledger},{wallet},"
                    f"{amount},'TWD','2026-08-07 13:00:00','2026-08-07 13:00:00')"
                )
    finally:
        database.engine.dispose()


def _snapshot(settings: AppSettings) -> dict[str, Any]:
    database = create_database(settings)
    try:
        inspector = inspect(database.engine)
        with database.engine.connect() as connection:
            return {
                "tables": set(inspector.get_table_names()),
                "entries": connection.exec_driver_sql(
                    "SELECT id, entry_type, effective_at, recorded_at,"
                    " idempotency_key, reversal_of_id, description,"
                    " financial_event_id, created_at, updated_at"
                    " FROM journal_entries ORDER BY id"
                ).all(),
                "postings": connection.exec_driver_sql(
                    "SELECT id, journal_entry_id, ledger_account_id, wallet_id,"
                    " amount_minor, currency, created_at, updated_at"
                    " FROM journal_postings ORDER BY id"
                ).all(),
                "events": connection.exec_driver_sql(
                    "SELECT id, origin, event_type, payment_method,"
                    " recognition_basis, occurred_at, merchant, amount_minor,"
                    " currency, status, version, idempotency_key, created_at,"
                    " updated_at FROM financial_events ORDER BY id"
                ).all(),
                "source_links": connection.exec_driver_sql(
                    "SELECT id, financial_event_id, transaction_id, status,"
                    " match_score_bps, reason_json, reviewed_at, created_at,"
                    " updated_at FROM financial_event_source_links ORDER BY id"
                ).all(),
                "entries_fk": sorted(
                    (row[2], row[3], row[6])
                    for row in connection.exec_driver_sql(
                        "PRAGMA foreign_key_list(journal_entries)"
                    ).all()
                ),
                "postings_fk": sorted(
                    (row[2], row[3], row[6])
                    for row in connection.exec_driver_sql(
                        "PRAGMA foreign_key_list(journal_postings)"
                    ).all()
                ),
                "indexes": sorted(
                    row[1]
                    for row in connection.exec_driver_sql(
                        "PRAGMA index_list(journal_entries)"
                    ).all()
                ),
                "triggers": sorted(
                    row[0]
                    for row in connection.exec_driver_sql(
                        "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                    ).all()
                ),
                "fk_check": connection.exec_driver_sql(
                    "PRAGMA foreign_key_check"
                ).all(),
            }
    finally:
        database.engine.dispose()


def _upgrade_with(
    settings: AppSettings, config: Any, hook: Callable[[Connection], None]
) -> None:
    """Upgrade while injecting a failure at the last safe moment."""
    config.attributes["verify_before_commit"] = hook
    try:
        command.upgrade(config, "0007")
    finally:
        config.attributes.pop("verify_before_commit", None)


def test_a_failure_after_ddl_rolls_back_schema_data_and_revision(
    database_settings: AppSettings,
) -> None:
    """Alembic's SQLite impl defaults transactional_ddl off; we override it."""
    config = _config_at(database_settings, "0006")
    _seed_journal(database_settings)
    before = _snapshot(database_settings)

    def _explode(connection: Connection) -> None:
        connection.exec_driver_sql(
            "INSERT INTO budget_categories (name, is_active, version, created_at,"
            " updated_at) VALUES ('會被回滾', 1, 1, '2026-08-09', '2026-08-09')"
        )
        raise RuntimeError("migration failed after applying schema")

    with pytest.raises(RuntimeError, match="failed after applying schema"):
        _upgrade_with(database_settings, config, _explode)

    after = _snapshot(database_settings)
    assert inspect_migration_status(database_settings).current_revision == "0006"
    assert not (B3_TABLES & after["tables"])
    assert after["tables"] == before["tables"]
    assert after["entries"] == before["entries"]
    assert after["postings"] == before["postings"]
    assert after["fk_check"] == []


def test_an_orphan_created_while_enforcement_is_off_blocks_the_commit(
    database_settings: AppSettings,
) -> None:
    """Foreign keys are unenforced during a migration, so the check must bite."""
    config = _config_at(database_settings, "0006")
    _seed_journal(database_settings)
    before = _snapshot(database_settings)

    def _orphan(connection: Connection) -> None:
        connection.exec_driver_sql(
            "INSERT INTO journal_postings (journal_entry_id, ledger_account_id,"
            " wallet_id, amount_minor, currency, created_at, updated_at)"
            " VALUES (9999, 1, 1, 1, 'TWD', '2026-08-09', '2026-08-09')"
        )

    with pytest.raises(RuntimeError, match="foreign key violations"):
        _upgrade_with(database_settings, config, _orphan)

    assert inspect_migration_status(database_settings).current_revision == "0006"
    after = _snapshot(database_settings)
    assert not (B3_TABLES & after["tables"])
    assert after["postings"] == before["postings"]
    assert after["entries"] == before["entries"]
    assert after["fk_check"] == []


def test_a_new_connection_always_enforces_foreign_keys_again(
    database_settings: AppSettings,
) -> None:
    """The protocol restores enforcement even when a migration failed."""
    config = _config_at(database_settings, "0006")
    _seed_journal(database_settings)

    def _explode(connection: Connection) -> None:
        del connection
        raise RuntimeError("deliberate failure")

    with pytest.raises(RuntimeError, match="deliberate failure"):
        _upgrade_with(database_settings, config, _explode)
    upgrade_database(database_settings)

    database = create_database(database_settings)
    try:
        with database.engine.connect() as connection:
            assert connection.scalar(text("PRAGMA foreign_keys")) == 1
    finally:
        database.engine.dispose()
    assert inspect_migration_status(database_settings).is_current


def test_b3_round_trip_preserves_every_journal_row_and_relationship(
    database_settings: AppSettings,
) -> None:
    """0006 -> 0007 -> 0006 with a reversal, postings, and the event link."""
    config = _config_at(database_settings, "0006")
    _seed_journal(database_settings)
    before = _snapshot(database_settings)

    command.upgrade(config, "0007")
    upgraded = _snapshot(database_settings)
    command.downgrade(config, "0006")
    rolled = _snapshot(database_settings)

    for state in (before, upgraded, rolled):
        assert state["fk_check"] == []
    assert upgraded["entries"] == before["entries"] == rolled["entries"]
    assert upgraded["postings"] == before["postings"] == rolled["postings"]
    assert upgraded["events"] == before["events"] == rolled["events"]
    assert upgraded["source_links"] == before["source_links"] == rolled["source_links"]
    # The seeded opening entry keeps its non-NULL event link throughout.
    assert before["entries"][0][7] == 1
    assert upgraded["entries"][0][7] == 1
    assert upgraded["postings_fk"] != before["postings_fk"]  # gains the category
    assert ("journal_entries", "journal_entry_id", "RESTRICT") in upgraded[
        "postings_fk"
    ]
    assert rolled["postings_fk"] == before["postings_fk"]
    assert upgraded["tables"] >= B3_TABLES
    assert not (B3_TABLES & rolled["tables"])
    assert upgraded["triggers"] == []
    assert rolled["triggers"] == before["triggers"]
    assert upgrade_database(database_settings).is_current


def test_0007_registry_foreign_keys_indexes_and_checks(
    database_settings: AppSettings,
) -> None:
    """The rebuilt journal keeps every guarantee it had, plus the registry."""
    upgrade_database(database_settings)
    state = _snapshot(database_settings)
    database = create_database(database_settings)
    try:
        inspector = inspect(database.engine)
        checks = {
            item["name"] for item in inspector.get_check_constraints("journal_entries")
        }
        with database.engine.connect() as connection:
            codes = [
                row[0]
                for row in connection.exec_driver_sql(
                    "SELECT code FROM journal_entry_types ORDER BY code"
                ).all()
            ]
    finally:
        database.engine.dispose()

    assert ("journal_entry_types", "entry_type", "RESTRICT") in state["entries_fk"]
    assert ("financial_events", "financial_event_id", "RESTRICT") in state["entries_fk"]
    assert ("journal_entries", "reversal_of_id", "RESTRICT") in state["entries_fk"]
    # Not unique since 0020: an event may carry the entry that replaced the
    # one it had, so the invariant moved to the posting service (ADR-B103).
    assert "ix_journal_entries_financial_event_id" in state["indexes"]
    assert "uq_journal_entries_financial_event" not in state["indexes"]
    assert "ix_journal_entries_entry_type" in state["indexes"]
    # The B2 pairing triggers became a real check constraint in 0007. Later
    # revisions own unrelated triggers, so only these two must be absent.
    assert not {
        "journal_entry_reversal_pairing_insert",
        "journal_entry_reversal_pairing_update",
    } & set(state["triggers"])
    assert "ck_journal_entries_journal_entry_reversal_pairing" in checks
    assert "ck_journal_entries_journal_entry_reversal_not_self" in checks
    # A superset, not an exact list: adding an entry type by inserting one
    # registry row is exactly what ADR-B028 made possible, so a later
    # milestone legitimately extends this table without rebuilding it.
    assert set(codes) >= {
        "adjustment",
        "budget_close",
        "expense",
        "internal_transfer",
        "opening_balance",
        "refund",
        "reversal",
    }
    assert codes == sorted(set(codes))


def test_0007_rejects_unknown_types_and_mislabelled_reversals(
    database_settings: AppSettings,
) -> None:
    """The registry replaces the old IN (...) check without weakening it."""
    upgrade_database(database_settings)
    database = create_database(database_settings)
    insert = (
        "INSERT INTO journal_entries (entry_type, effective_at, recorded_at,"
        " idempotency_key, reversal_of_id, description, created_at, updated_at)"
        " VALUES (?, '2026-01-01', '2026-01-01', ?, ?, '', '2026-01-01','2026-01-01')"
    )
    try:
        with database.engine.begin() as connection:
            connection.exec_driver_sql(insert, ("opening_balance", "opening", None))
        for entry_type, key, reversal in (
            ("bogus", "unknown-type", None),
            ("adjustment", "mislabelled", 1),
            ("reversal", "target-less", None),
        ):
            with (
                database.engine.begin() as connection,
                pytest.raises(IntegrityError, match=r"constraint|CHECK|FOREIGN KEY"),
            ):
                connection.exec_driver_sql(insert, (entry_type, key, reversal))
        with database.engine.begin() as connection:
            connection.exec_driver_sql(insert, ("expense", "expense-1", None))
            connection.exec_driver_sql(insert, ("refund", "refund-1", None))
            connection.exec_driver_sql(insert, ("budget_close", "close-1", None))
    finally:
        database.engine.dispose()


def test_offline_sql_migrations_are_refused(database_settings: AppSettings) -> None:
    """--sql cannot execute or verify the required pragmas."""
    upgrade_database(database_settings)
    config = build_alembic_config(database_settings.database_path)

    with pytest.raises(Exception, match="Offline --sql migrations are not supported"):
        command.upgrade(config, "head", sql=True)


def test_0007_rejects_a_malformed_refund_link(
    database_settings: AppSettings,
) -> None:
    """Row-level shape only; a refund of a refund is a B3B service rule."""
    upgrade_database(database_settings)
    database = create_database(database_settings)
    insert = (
        "INSERT INTO financial_events (origin, event_type, payment_method,"
        " recognition_basis, occurred_at, amount_minor, currency, status, version,"
        " idempotency_key, refund_of_event_id, created_at, updated_at) VALUES"
        " ('gmail_transaction', ?, 'bank_account', 'gmail_notification',"
        " '2026-08-07 13:30:00', 1000, 'TWD', 'needs_allocation', 1, ?, ?,"
        " '2026-08-07', '2026-08-07')"
    )
    try:
        with database.engine.begin() as connection:
            connection.exec_driver_sql(insert, ("purchase", "original", None))
        with (
            database.engine.begin() as connection,
            pytest.raises(IntegrityError, match="refund_shape"),
        ):
            connection.exec_driver_sql(insert, ("purchase", "not-a-refund", 1))
        with (
            database.engine.begin() as connection,
            pytest.raises(IntegrityError, match="refund_shape"),
        ):
            connection.exec_driver_sql(
                "INSERT INTO financial_events (id, origin, event_type,"
                " payment_method, recognition_basis, occurred_at, amount_minor,"
                " currency, status, version, idempotency_key, refund_of_event_id,"
                " created_at, updated_at) VALUES (9, 'gmail_transaction','refund',"
                "'bank_account','gmail_notification','2026-08-07 13:30:00',1000,"
                "'TWD','needs_allocation',1,'self-refund',9,'2026-08-07',"
                "'2026-08-07')"
            )
        with database.engine.begin() as connection:
            connection.exec_driver_sql(insert, ("refund", "valid-refund", 1))
    finally:
        database.engine.dispose()

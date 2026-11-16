"""Revision 0008 upgrade, downgrade, and data-preservation tests."""

from __future__ import annotations

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

B4_TABLES = {"subscriptions", "subscription_price_periods"}
B4_TRIGGERS = {
    "subscription_price_no_overlap_insert",
    "subscription_price_no_overlap_update",
}
_PRESERVED_TABLES = (
    "emails",
    "transactions",
    "financial_events",
    "financial_event_source_links",
    "journal_entries",
    "journal_postings",
    "budget_categories",
    "budget_periods",
    "budget_targets",
    "event_allocations",
    "funding_commitments",
    "journal_entry_types",
)


def _seed_existing_rows(settings: AppSettings) -> dict[str, int]:
    """Seed Gmail, ledger, and B3 rows that revision 0008 must not touch."""
    database = create_database(settings)
    try:
        with SqlAlchemyUnitOfWork(database.session_factory) as unit:
            account = unit.accounts.get_or_create("user@example.test")
            outcome = unit.emails.upsert_from_gmail(
                EmailSnapshot(
                    gmail_account_id=account.id,
                    gmail_message_id="gmail-subscription-001",
                    gmail_thread_id="thread-subscription-001",
                    gmail_history_id="500",
                    sender="notice@example-bank.test",
                    recipients=("user@example.test",),
                    subject="消費通知",
                    gmail_internal_date_ms=1_754_000_000_000,
                    received_at=datetime(2026, 8, 1, 3, 0, tzinfo=UTC),
                    body_text="已去識別化的測試內容",
                )
            )
            unit.transactions.add(
                TransactionDraft(
                    email_id=outcome.email.id,
                    source_key="row-1",
                    transaction_type=TransactionType.PURCHASE,
                    confidence=Decimal("0.9"),
                    parser_name="tests.fake",
                    parser_version="1",
                    amount_minor=12800,
                    currency="TWD",
                )
            )
            unit.commit()
        with database.engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO budget_categories (name, is_active, version, created_at,"
                " updated_at) VALUES ('測試訂閱', 1, 1, '2026-08-09', '2026-08-09')"
            )
            connection.exec_driver_sql(
                "INSERT INTO financial_accounts (institution_code, display_name,"
                " account_type, currency, is_active, version, created_at, updated_at)"
                " VALUES ('test_bank','測試支票帳戶','checking','TWD',1,1,"
                "'2026-08-09','2026-08-09')"
            )
            connection.exec_driver_sql(
                "INSERT INTO wallets (financial_account_id, name, purpose, is_active,"
                " version, created_at, updated_at) VALUES (1,'測試訂閱錢包',"
                "'budget',1,1,'2026-08-09','2026-08-09')"
            )
        return _row_counts(settings)
    finally:
        database.engine.dispose()


def _row_counts(settings: AppSettings) -> dict[str, int]:
    database = create_database(settings)
    try:
        with database.engine.connect() as connection:
            return {
                table: connection.scalar(text(f"SELECT COUNT(*) FROM {table}")) or 0
                for table in (*_PRESERVED_TABLES, "financial_accounts", "wallets")
            }
    finally:
        database.engine.dispose()


def _snapshot(settings: AppSettings) -> dict[str, Any]:
    database = create_database(settings)
    try:
        inspector = inspect(database.engine)
        with database.engine.connect() as connection:
            return {
                "tables": set(inspector.get_table_names()),
                "triggers": {
                    row[0]
                    for row in connection.exec_driver_sql(
                        "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                    ).all()
                },
                "integrity": connection.scalar(text("PRAGMA integrity_check")),
                "fk_check": connection.exec_driver_sql(
                    "PRAGMA foreign_key_check"
                ).all(),
                "foreign_keys": connection.scalar(text("PRAGMA foreign_keys")),
            }
    finally:
        database.engine.dispose()


def _config_at(settings: AppSettings, revision: str) -> Any:
    config = build_alembic_config(settings.database_path)
    upgrade_database(settings)
    command.downgrade(config, revision)
    return config


def test_upgrading_0007_to_0008_preserves_every_existing_row(
    database_settings: AppSettings,
) -> None:
    config = _config_at(database_settings, "0007")
    seeded = _seed_existing_rows(database_settings)
    before = _snapshot(database_settings)

    command.upgrade(config, "0008")

    after = _snapshot(database_settings)
    assert not (B4_TABLES & before["tables"])
    assert after["tables"] >= B4_TABLES
    assert after["triggers"] >= B4_TRIGGERS
    assert after["integrity"] == "ok"
    assert after["fk_check"] == []
    assert after["foreign_keys"] == 1
    assert _row_counts(database_settings) == seeded
    assert inspect_migration_status(database_settings).current_revision == "0008"


def test_downgrading_0008_removes_only_the_subscription_register(
    database_settings: AppSettings,
) -> None:
    config = _config_at(database_settings, "0007")
    seeded = _seed_existing_rows(database_settings)
    command.upgrade(config, "0008")
    before = _snapshot(database_settings)

    command.downgrade(config, "0007")

    after = _snapshot(database_settings)
    assert before["tables"] >= B4_TABLES
    assert not (B4_TABLES & after["tables"])
    assert after["tables"] | B4_TABLES == before["tables"]
    assert not (B4_TRIGGERS & after["triggers"])
    assert after["integrity"] == "ok"
    assert after["fk_check"] == []
    assert after["foreign_keys"] == 1
    assert _row_counts(database_settings) == seeded
    assert inspect_migration_status(database_settings).current_revision == "0007"


def test_upgrade_downgrade_and_re_upgrade_leave_the_same_schema(
    database_settings: AppSettings,
) -> None:
    config = _config_at(database_settings, "0007")
    seeded = _seed_existing_rows(database_settings)
    command.upgrade(config, "0008")
    first = _schema_objects(database_settings)

    command.downgrade(config, "0007")
    command.upgrade(config, "0008")

    second = _schema_objects(database_settings)
    assert first == second
    assert _row_counts(database_settings) == seeded
    assert upgrade_database(database_settings).is_current


def _schema_objects(settings: AppSettings) -> dict[str, Any]:
    database = create_database(settings)
    try:
        with database.engine.connect() as connection:
            return {
                "indexes": {
                    table: sorted(
                        row[1]
                        for row in connection.exec_driver_sql(
                            f"PRAGMA index_list({table})"
                        ).all()
                    )
                    for table in sorted(B4_TABLES)
                },
                "foreign_keys": {
                    table: sorted(
                        (row[2], row[3], row[6])
                        for row in connection.exec_driver_sql(
                            f"PRAGMA foreign_key_list({table})"
                        ).all()
                    )
                    for table in sorted(B4_TABLES)
                },
                "sql": sorted(
                    row[0]
                    for row in connection.exec_driver_sql(
                        "SELECT sql FROM sqlite_master WHERE name LIKE 'subscription%'"
                        " AND sql IS NOT NULL"
                    ).all()
                ),
            }
    finally:
        database.engine.dispose()


def test_0008_creates_the_indexes_and_restrict_foreign_keys_it_needs(
    database_settings: AppSettings,
) -> None:
    upgrade_database(database_settings)
    objects = _schema_objects(database_settings)

    assert {
        "ix_subscriptions_name_activity",
        "ix_subscriptions_currency_active",
        "ix_subscriptions_funding_wallet_id",
        "ix_subscriptions_budget_category_id",
    } <= set(objects["indexes"]["subscriptions"])
    assert "ix_subscription_price_periods_lookup" in set(
        objects["indexes"]["subscription_price_periods"]
    )
    assert objects["foreign_keys"]["subscriptions"] == [
        ("budget_categories", "budget_category_id", "RESTRICT"),
        ("wallets", "funding_wallet_id", "RESTRICT"),
    ]
    assert objects["foreign_keys"]["subscription_price_periods"] == [
        ("subscriptions", "subscription_id", "RESTRICT")
    ]


def test_a_failure_after_0008_rolls_back_schema_data_and_revision(
    database_settings: AppSettings,
) -> None:
    """Foreign keys are unenforced during a migration, so the check must bite."""
    config = _config_at(database_settings, "0007")
    seeded = _seed_existing_rows(database_settings)

    def _explode(connection: Connection) -> None:
        connection.exec_driver_sql(
            "INSERT INTO subscriptions (name, budget_category_id, funding_wallet_id,"
            " currency, billing_cadence, cadence_months, is_active, version,"
            " created_at, updated_at) VALUES ('會被回滾', 1, 1, 'TWD', 'monthly', 1,"
            " 1, 1, '2026-08-09', '2026-08-09')"
        )
        raise RuntimeError("migration failed after applying schema")

    config.attributes["verify_before_commit"] = _explode
    try:
        with pytest.raises(RuntimeError, match="failed after applying schema"):
            command.upgrade(config, "0008")
    finally:
        config.attributes.pop("verify_before_commit", None)

    after = _snapshot(database_settings)
    assert inspect_migration_status(database_settings).current_revision == "0007"
    assert not (B4_TABLES & after["tables"])
    assert not (B4_TRIGGERS & after["triggers"])
    assert after["foreign_keys"] == 1
    assert _row_counts(database_settings) == seeded


def test_an_orphan_created_while_enforcement_is_off_blocks_0008(
    database_settings: AppSettings,
) -> None:
    config = _config_at(database_settings, "0007")
    seeded = _seed_existing_rows(database_settings)

    def _orphan(connection: Connection) -> None:
        connection.exec_driver_sql(
            "INSERT INTO subscriptions (name, budget_category_id, funding_wallet_id,"
            " currency, billing_cadence, cadence_months, is_active, version,"
            " created_at, updated_at) VALUES ('孤兒', 9999, 9999, 'TWD', 'monthly',"
            " 1, 1, 1, '2026-08-09', '2026-08-09')"
        )

    config.attributes["verify_before_commit"] = _orphan
    try:
        with pytest.raises(RuntimeError, match="foreign key violations"):
            command.upgrade(config, "0008")
    finally:
        config.attributes.pop("verify_before_commit", None)

    after = _snapshot(database_settings)
    assert inspect_migration_status(database_settings).current_revision == "0007"
    assert not (B4_TABLES & after["tables"])
    assert after["foreign_keys"] == 1
    assert _row_counts(database_settings) == seeded


def test_the_overlap_triggers_are_enforced_by_the_database_itself(
    database_settings: AppSettings,
) -> None:
    upgrade_database(database_settings)
    _seed_existing_rows(database_settings)
    database = create_database(database_settings)
    insert_price = (
        "INSERT INTO subscription_price_periods (subscription_id, amount_minor,"
        " effective_from, effective_to, version, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, 1, '2026-08-09', '2026-08-09')"
    )
    try:
        with database.engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO subscriptions (name, budget_category_id,"
                " funding_wallet_id, currency, billing_cadence, cadence_months,"
                " is_active, version, created_at, updated_at) VALUES ('測試影音',"
                " 1, 1, 'TWD', 'monthly', 1, 1, 1, '2026-08-09', '2026-08-09')"
            )
            connection.exec_driver_sql(
                insert_price, (1, 39000, "2026-01-01", "2026-07-01")
            )
            # Adjacency is legal: the upper bound is excluded.
            connection.exec_driver_sql(insert_price, (1, 49000, "2026-07-01", None))
        with (
            database.engine.begin() as connection,
            pytest.raises(IntegrityError, match="must not overlap"),
        ):
            connection.exec_driver_sql(insert_price, (1, 1, "2026-04-01", "2026-05-01"))
        with (
            database.engine.begin() as connection,
            pytest.raises(IntegrityError, match="must not overlap"),
        ):
            connection.exec_driver_sql(
                "UPDATE subscription_price_periods SET effective_from = '2026-02-01'"
                " WHERE id = 2"
            )
        with database.engine.connect() as connection:
            rows = connection.exec_driver_sql(
                "SELECT effective_from FROM subscription_price_periods ORDER BY id"
            ).all()
    finally:
        database.engine.dispose()

    assert [row[0] for row in rows] == ["2026-01-01", "2026-07-01"]


B4B_TRIGGERS = {
    "subscription_no_overlapping_interval_insert",
    "subscription_no_overlapping_interval_update",
}
_INSERT_SUBSCRIPTION = (
    "INSERT INTO subscriptions (name, budget_category_id, funding_wallet_id,"
    " currency, billing_cadence, cadence_months, active_from, active_to,"
    " is_active, version, created_at, updated_at) VALUES (?, 1, 1,"
    " 'TWD', 'monthly', 1, ?, ?, ?, 1, '2026-08-09', '2026-08-09')"
)


def _seed_subscriptions(
    settings: AppSettings, rows: tuple[tuple[str, str | None, str | None, int], ...]
) -> None:
    """Insert subscription intervals directly, as revision 0008 would allow."""
    database = create_database(settings)
    try:
        with database.engine.begin() as connection:
            for row in rows:
                connection.exec_driver_sql(_INSERT_SUBSCRIPTION, row)
    finally:
        database.engine.dispose()


def _subscription_rows(settings: AppSettings) -> list[tuple[Any, ...]]:
    database = create_database(settings)
    try:
        with database.engine.connect() as connection:
            return [
                tuple(row)
                for row in connection.exec_driver_sql(
                    "SELECT id, name, active_from, active_to, is_active, version,"
                    " created_at, updated_at FROM subscriptions ORDER BY id"
                ).all()
            ]
    finally:
        database.engine.dispose()


def _subscription_indexes(settings: AppSettings) -> set[str]:
    database = create_database(settings)
    try:
        with database.engine.connect() as connection:
            return {
                row[1]
                for row in connection.exec_driver_sql(
                    "PRAGMA index_list(subscriptions)"
                ).all()
            }
    finally:
        database.engine.dispose()


def _create_active_name_index(settings: AppSettings) -> None:
    """Recreate the 0008 index by hand, as a repaired database would have."""
    database = create_database(settings)
    try:
        with database.engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE UNIQUE INDEX uq_subscriptions_active_name"
                " ON subscriptions (name) WHERE is_active = 1"
            )
    finally:
        database.engine.dispose()


def test_0010_restores_the_index_a_published_0009_had_dropped(
    database_settings: AppSettings,
) -> None:
    """The first shape a database at 0009 can have: the index is gone."""
    config = _config_at(database_settings, "0009")
    seeded = _seed_existing_rows(database_settings)
    _seed_subscriptions(
        database_settings,
        (
            ("測試影音", "2026-01-01", "2026-07-01", 0),
            ("測試影音", "2026-07-01", None, 1),
        ),
    )
    before = _subscription_rows(database_settings)
    assert "uq_subscriptions_active_name" not in _subscription_indexes(
        database_settings
    )

    command.upgrade(config, "0010")

    state = _snapshot(database_settings)
    assert "uq_subscriptions_active_name" in _subscription_indexes(database_settings)
    assert "ix_subscriptions_name_activity" in _subscription_indexes(database_settings)
    assert state["triggers"] >= B4B_TRIGGERS
    assert state["integrity"] == "ok"
    assert state["fk_check"] == []
    assert _subscription_rows(database_settings) == before
    assert _row_counts(database_settings) == seeded
    assert inspect_migration_status(database_settings).current_revision == "0010"


def test_0010_accepts_a_database_whose_index_was_already_restored(
    database_settings: AppSettings,
) -> None:
    """The second shape: repaired out of band, so the index already exists."""
    config = _config_at(database_settings, "0009")
    seeded = _seed_existing_rows(database_settings)
    _seed_subscriptions(database_settings, (("測試影音", "2026-01-01", None, 1),))
    _create_active_name_index(database_settings)
    before_indexes = _subscription_indexes(database_settings)
    before = _subscription_rows(database_settings)

    command.upgrade(config, "0010")

    assert "uq_subscriptions_active_name" in before_indexes
    assert _subscription_indexes(database_settings) == before_indexes
    assert _subscription_rows(database_settings) == before
    assert _row_counts(database_settings) == seeded
    assert _snapshot(database_settings)["integrity"] == "ok"
    assert inspect_migration_status(database_settings).current_revision == "0010"


def test_0010_refuses_an_index_of_that_name_it_did_not_install(
    database_settings: AppSettings,
) -> None:
    config = _config_at(database_settings, "0009")
    _seed_existing_rows(database_settings)
    database = create_database(database_settings)
    try:
        with database.engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE INDEX uq_subscriptions_active_name ON subscriptions (name)"
            )
    finally:
        database.engine.dispose()

    with pytest.raises(Exception, match="is not the one revision 0010 installs"):
        command.upgrade(config, "0010")

    assert inspect_migration_status(database_settings).current_revision == "0009"


def test_0010_round_trips_through_0008_with_real_subscription_rows(
    database_settings: AppSettings,
) -> None:
    """Every state 0010 can hold must still be expressible at 0008."""
    config = _config_at(database_settings, "0008")
    seeded = _seed_existing_rows(database_settings)
    _seed_subscriptions(
        database_settings,
        (
            ("測試影音", "2026-01-01", "2026-07-01", 0),
            ("測試影音", "2026-07-01", None, 1),
            ("測試雲端", None, None, 1),
        ),
    )
    command.upgrade(config, "0010")
    upgraded_rows = _subscription_rows(database_settings)
    upgraded_indexes = _subscription_indexes(database_settings)

    command.downgrade(config, "0008")
    rolled_rows = _subscription_rows(database_settings)
    rolled_indexes = _subscription_indexes(database_settings)
    rolled = _snapshot(database_settings)
    command.upgrade(config, "0010")

    assert len(upgraded_rows) == 3
    assert rolled_rows == upgraded_rows
    assert _subscription_rows(database_settings) == upgraded_rows
    assert "uq_subscriptions_active_name" in rolled_indexes
    assert "ix_subscriptions_name_activity" not in rolled_indexes
    assert _subscription_indexes(database_settings) == upgraded_indexes
    assert not (B4B_TRIGGERS & rolled["triggers"])
    assert _snapshot(database_settings)["triggers"] >= B4B_TRIGGERS
    assert rolled["integrity"] == "ok"
    assert rolled["fk_check"] == []
    assert inspect_migration_status(database_settings).current_revision == "0010"
    assert _row_counts(database_settings) == seeded
    assert upgrade_database(database_settings).is_current


def test_0010_downgrade_returns_the_schema_0009_was_published_with(
    database_settings: AppSettings,
) -> None:
    config = _config_at(database_settings, "0009")
    published = _subscription_indexes(database_settings)
    published_state = _snapshot(database_settings)

    command.upgrade(config, "0010")
    command.downgrade(config, "0009")

    assert "uq_subscriptions_active_name" not in published
    assert _subscription_indexes(database_settings) == published
    assert _snapshot(database_settings)["triggers"] == published_state["triggers"]
    assert inspect_migration_status(database_settings).current_revision == "0009"


@pytest.mark.parametrize(
    ("seed_at", "rows", "expected"),
    [
        # Overlapping rows can only be created before 0009 installs its
        # triggers; 0009 itself then accepts them, because it validates
        # nothing that already exists.
        (
            "0008",
            (
                ("測試影音", "2026-01-01", "2026-11-01", 0),
                ("測試影音", "2026-08-01", None, 1),
            ),
            "cover the same month twice",
        ),
        # Two running intervals of one name can only be created once 0009 has
        # dropped the unique index. They are adjacent, so no trigger objects.
        (
            "0009",
            (
                ("測試影音", "2026-01-01", "2026-04-01", 1),
                ("測試影音", "2026-04-01", None, 1),
            ),
            "running under one name at the same time",
        ),
    ],
)
def test_0010_refuses_to_upgrade_over_rows_that_already_break_it(
    database_settings: AppSettings,
    seed_at: str,
    rows: tuple[tuple[str, str | None, str | None, int], ...],
    expected: str,
) -> None:
    """Neither invariant can be installed over rows that already break it."""
    config = _config_at(database_settings, seed_at)
    seeded = _seed_existing_rows(database_settings)
    _seed_subscriptions(database_settings, rows)
    if seed_at == "0008":
        command.upgrade(config, "0009")
    before = _subscription_rows(database_settings)
    before_indexes = _subscription_indexes(database_settings)

    with pytest.raises(Exception, match=expected) as failure:
        command.upgrade(config, "0010")

    message = str(failure.value)
    assert "1/2" in message
    # Row identifiers only: a name is user data and never belongs in a
    # migration error.
    assert "測試影音" not in message
    assert inspect_migration_status(database_settings).current_revision == "0009"
    assert _subscription_indexes(database_settings) == before_indexes
    assert _subscription_rows(database_settings) == before
    assert _row_counts(database_settings) == seeded
    assert _snapshot(database_settings)["integrity"] == "ok"


def test_illegal_rows_roll_the_whole_upgrade_back_to_0008(
    database_settings: AppSettings,
) -> None:
    """0009 installs no guard, so 0010 is where a bad 0008 database stops.

    Every pending migration runs in one transaction, so failing in 0010 must
    undo 0009 as well and leave the database exactly at 0008.
    """
    config = _config_at(database_settings, "0008")
    seeded = _seed_existing_rows(database_settings)
    _seed_subscriptions(
        database_settings,
        (
            ("測試影音", "2026-01-01", "2026-11-01", 0),
            ("測試影音", "2026-08-01", None, 1),
        ),
    )
    before = _subscription_rows(database_settings)
    before_indexes = _subscription_indexes(database_settings)

    with pytest.raises(Exception, match="cover the same month twice"):
        command.upgrade(config, "head")

    state = _snapshot(database_settings)
    assert inspect_migration_status(database_settings).current_revision == "0008"
    assert "uq_subscriptions_active_name" in before_indexes
    assert _subscription_indexes(database_settings) == before_indexes
    assert "ix_subscriptions_name_activity" not in _subscription_indexes(
        database_settings
    )
    assert not (B4B_TRIGGERS & state["triggers"])
    assert _subscription_rows(database_settings) == before
    assert _row_counts(database_settings) == seeded
    assert state["integrity"] == "ok"
    assert state["fk_check"] == []
    assert state["foreign_keys"] == 1


def test_the_interval_triggers_are_enforced_by_the_database_itself(
    database_settings: AppSettings,
) -> None:
    upgrade_database(database_settings)
    _seed_existing_rows(database_settings)
    database = create_database(database_settings)
    try:
        with database.engine.begin() as connection:
            connection.exec_driver_sql(
                _INSERT_SUBSCRIPTION, ("測試影音", "2026-01-01", "2026-07-01", 0)
            )
            # Adjacent is legal; the upper bound is excluded.
            connection.exec_driver_sql(
                _INSERT_SUBSCRIPTION, ("測試影音", "2026-07-01", "2026-10-01", 1)
            )
        for start, end, active in (
            ("2026-04-01", "2026-06-01", 0),
            ("2026-09-01", "2026-12-01", 0),
            (None, "2026-02-01", 0),
        ):
            with (
                database.engine.begin() as connection,
                pytest.raises(IntegrityError, match="intervals must not overlap"),
            ):
                connection.exec_driver_sql(
                    _INSERT_SUBSCRIPTION, ("測試影音", start, end, active)
                )
        with (
            database.engine.begin() as connection,
            pytest.raises(IntegrityError, match="intervals must not overlap"),
        ):
            connection.exec_driver_sql(
                "UPDATE subscriptions SET active_to = '2026-09-01' WHERE id = 1"
            )
        # Adjacent but also running: the interval trigger is satisfied and
        # the 0008 unique index is what refuses it. Neither invariant covers
        # the other.
        with (
            database.engine.begin() as connection,
            pytest.raises(IntegrityError, match="UNIQUE constraint"),
        ):
            connection.exec_driver_sql(
                _INSERT_SUBSCRIPTION, ("測試影音", "2026-10-01", None, 1)
            )
        with database.engine.connect() as connection:
            rows = connection.exec_driver_sql(
                "SELECT active_from, active_to FROM subscriptions ORDER BY id"
            ).all()
    finally:
        database.engine.dispose()

    assert [tuple(row) for row in rows] == [
        ("2026-01-01", "2026-07-01"),
        ("2026-07-01", "2026-10-01"),
    ]

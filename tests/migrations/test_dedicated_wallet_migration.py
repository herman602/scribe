"""Revision 0015 schema, immutability, round-trip, and refusal tests."""

from __future__ import annotations

import pytest
from alembic import command
from app.application.services.manual_money import ManualExpenseRequest
from app.domain.budget_enums import PaymentMethod
from app.infrastructure.config.settings import AppSettings
from app.infrastructure.database.engine import Database, create_database
from app.infrastructure.database.migrations import (
    build_alembic_config,
    inspect_migration_status,
    upgrade_database,
)
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from tests.b7_support import B7_NOW, B7Services, B7World

TABLE = "dedicated_wallet_allocations"
TRIGGERS = {
    "dedicated_wallet_allocation_valid_route",
    "dedicated_wallet_allocation_no_update",
    "dedicated_wallet_allocation_no_delete",
}


def _triggers(database: Database) -> set[str]:
    with database.engine.connect() as connection:
        return {
            row[0]
            for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            ).all()
        }


def _post_dedicated(b7: B7Services, world: B7World) -> int:
    result = b7.expenses.execute(
        ManualExpenseRequest(
            payment_method=PaymentMethod.CREDIT_CARD,
            occurred_at=B7_NOW,
            merchant="測試專款支出",
            amount_minor=12_000,
            currency="TWD",
            funding_wallet_id=world.bank_food_wallet_id,
            budget_category_id=world.food_id,
            client_token="migration-dedicated",
            card_id=world.card_id,
            clearing_wallet_id=world.bank_clearing_wallet_id,
        )
    )
    return result.allocations[0].id


def test_0015_creates_immutable_restrictive_routing_evidence(
    migrated_database: Database,
) -> None:
    inspector = inspect(migrated_database.engine)
    assert TABLE in inspector.get_table_names()
    assert _triggers(migrated_database) >= TRIGGERS
    foreign_keys = inspector.get_foreign_keys(TABLE)
    assert {item["referred_table"] for item in foreign_keys} == {
        "event_allocations",
        "wallets",
    }
    assert {item["options"].get("ondelete") for item in foreign_keys} == {"RESTRICT"}


def test_0015_round_trips_when_no_dedicated_history_exists(
    database_settings: AppSettings,
) -> None:
    upgrade_database(database_settings)
    config = build_alembic_config(database_settings.database_path)

    command.downgrade(config, "0014")
    database = create_database(database_settings)
    try:
        assert TABLE not in inspect(database.engine).get_table_names()
    finally:
        database.engine.dispose()
    assert inspect_migration_status(database_settings).current_revision == "0014"

    command.upgrade(config, "0015")
    assert inspect_migration_status(database_settings).current_revision == "0015"


def test_0015_rejects_a_raw_route_that_is_not_a_same_account_card_clearing_move(
    b7: B7Services,
    b7_world: B7World,
    migrated_database: Database,
) -> None:
    result = b7.expenses.execute(
        ManualExpenseRequest(
            payment_method=PaymentMethod.CREDIT_CARD,
            occurred_at=B7_NOW,
            merchant="測試一般支出",
            amount_minor=12_000,
            currency="TWD",
            funding_wallet_id=b7_world.bank_food_wallet_id,
            budget_category_id=b7_world.food_id,
            client_token="migration-invalid-route",
            card_id=b7_world.card_id,
        )
    )

    with (
        pytest.raises(IntegrityError, match="invalid route"),
        migrated_database.engine.begin() as connection,
    ):
        connection.execute(
            text(
                f"INSERT INTO {TABLE} "
                "(event_allocation_id, clearing_wallet_id) "
                "VALUES (:allocation, :wallet)"
            ),
            {
                "allocation": result.allocations[0].id,
                "wallet": b7_world.bank_unallocated_id,
            },
        )


def test_used_dedicated_history_is_immutable_and_refuses_downgrade(
    b7: B7Services,
    b7_world: B7World,
    migrated_database: Database,
    database_settings: AppSettings,
) -> None:
    allocation_id = _post_dedicated(b7, b7_world)
    with (
        pytest.raises(IntegrityError, match="immutable"),
        migrated_database.engine.begin() as connection,
    ):
        connection.execute(
            text(
                f"UPDATE {TABLE} SET clearing_wallet_id = "
                "clearing_wallet_id WHERE event_allocation_id = :allocation_id"
            ),
            {"allocation_id": allocation_id},
        )

    with (
        pytest.raises(IntegrityError, match="immutable"),
        migrated_database.engine.begin() as connection,
    ):
        connection.execute(
            text(f"DELETE FROM {TABLE} WHERE event_allocation_id = :allocation_id"),
            {"allocation_id": allocation_id},
        )

    config = build_alembic_config(database_settings.database_path)
    with pytest.raises(RuntimeError, match="history exists"):
        command.downgrade(config, "0014")

    assert inspect_migration_status(database_settings).current_revision == "0022"
    with migrated_database.engine.connect() as connection:
        assert connection.scalar(text(f"SELECT COUNT(*) FROM {TABLE}")) == 1
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []

"""Revisions 0021 and 0022: the backfill, its direction, and the refusals."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from app.infrastructure.config.settings import AppSettings
from app.infrastructure.database.engine import create_database
from app.infrastructure.database.migrations import (
    build_alembic_config,
    inspect_migration_status,
    upgrade_database,
)
from sqlalchemy import inspect

TABLE = "event_allocations"
COLUMN = "journal_entry_id"
ROUTE_TRIGGER = "dedicated_wallet_allocation_valid_route"

_ACCOUNT = (
    "INSERT INTO financial_accounts (institution_code, display_name, account_type,"
    " currency, is_active, version, created_at, updated_at) VALUES"
    " ('test_bank','測試帳戶','checking','TWD',1,1,'2026-09-01','2026-09-01')"
)
_WALLET = (
    "INSERT INTO wallets (financial_account_id, name, purpose, is_active, version,"
    " display_order, created_at, updated_at) VALUES"
    " (1,'尚未分配','unallocated',1,1,1,'2026-09-01','2026-09-01')"
)
_EVENT = (
    "INSERT INTO financial_events (origin, event_type, payment_method,"
    " recognition_basis, occurred_at, merchant, amount_minor, currency, status,"
    " version, idempotency_key, created_at, updated_at) VALUES"
    " ('manual','purchase','cash','observed_manual','2026-09-01 10:00:00','商店',"
    "12000,'TWD','posted',1,'manual:1','2026-09-01','2026-09-01')"
)


def _entry(
    entry_id: int,
    *,
    entry_type: str,
    key: str,
    reversal: str,
    event: str,
    written_at: str,
) -> str:
    return (
        "INSERT INTO journal_entries (id, entry_type, effective_at, recorded_at,"
        " idempotency_key, reversal_of_id, description, financial_event_id,"
        f" created_at, updated_at) VALUES ({entry_id},'{entry_type}',"
        f"'2026-09-01 10:00:00','{written_at}','{key}',{reversal},'',{event},"
        f"'{written_at}','{written_at}')"
    )


def _allocation(allocation_id: int, *, written_at: str) -> str:
    return (
        "INSERT INTO event_allocations (id, financial_event_id, budget_category_id,"
        " funding_wallet_id, amount_minor, currency, allocation_source, created_at,"
        f" updated_at) VALUES ({allocation_id},1,NULL,1,12000,'TWD','manual',"
        f"'{written_at}','{written_at}')"
    )


def _redone(*, first: str, second: str, second_entry: str) -> tuple[str, ...]:
    """Describe a redone allocation as the services actually write one.

    The allocation rows come first and the entry built from them a moment
    later, so `second_entry` may fall on the far side of a second boundary
    from `second`. Giving both the same timestamp — which `0021` was the only
    revision tested with — hides which direction the backfill resolves in.
    """
    return (
        _ACCOUNT,
        _WALLET,
        _EVENT,
        _allocation(1, written_at=first),
        _entry(
            1,
            entry_type="expense",
            key="allocation:financial_event:1:first",
            reversal="NULL",
            event="1",
            written_at=first,
        ),
        _entry(
            2,
            entry_type="reversal",
            key="allocation_redo:journal_entry:1",
            reversal="1",
            event="NULL",
            written_at="2026-09-01 11:00:00",
        ),
        _allocation(2, written_at=second),
        _entry(
            3,
            entry_type="expense",
            key="allocation:financial_event:1:second",
            reversal="NULL",
            event="1",
            written_at=second_entry,
        ),
    )


#: A second generation whose entry landed in the next second from its rows.
_REDONE = _redone(
    first="2026-09-01 10:00:00",
    second="2026-09-01 12:00:00",
    second_entry="2026-09-01 12:00:01",
)

#: A whole generation posted, taken back, and posted again inside one second,
#: which no timestamp can separate.
_REDONE_WITHIN_ONE_SECOND = _redone(
    first="2026-09-01 10:00:00",
    second="2026-09-01 10:00:00",
    second_entry="2026-09-01 10:00:00",
)

#: The first allocation generation itself crosses a timestamp boundary.  A
#: repair after 0021 is too late for this shape if 0021 refuses the row before
#: Alembic can reach the repair revision.
_FIRST_GENERATION_CROSSES_SECOND = (
    _ACCOUNT,
    _WALLET,
    _EVENT,
    _allocation(1, written_at="2026-09-01 10:00:00"),
    _entry(
        1,
        entry_type="expense",
        key="allocation:financial_event:1:first",
        reversal="NULL",
        event="1",
        written_at="2026-09-01 10:00:01",
    ),
)


def _execute(settings: AppSettings, statements: tuple[str, ...]) -> None:
    """Write raw rows, because the mapped models are already ahead of 0020."""
    database = create_database(settings)
    try:
        with database.engine.begin() as connection:
            for statement in statements:
                connection.exec_driver_sql(statement)
    finally:
        database.engine.dispose()


def _links(settings: AppSettings) -> list[tuple[int, int | None]]:
    database = create_database(settings)
    try:
        with database.engine.connect() as connection:
            return [
                (int(row[0]), None if row[1] is None else int(row[1]))
                for row in connection.exec_driver_sql(
                    f"SELECT id, {COLUMN} FROM {TABLE} ORDER BY id"
                ).all()
            ]
    finally:
        database.engine.dispose()


def _columns(settings: AppSettings) -> set[str]:
    database = create_database(settings)
    try:
        return {item["name"] for item in inspect(database.engine).get_columns(TABLE)}
    finally:
        database.engine.dispose()


def _triggers(settings: AppSettings) -> set[str]:
    database = create_database(settings)
    try:
        with database.engine.connect() as connection:
            return {
                row[0]
                for row in connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                ).all()
            }
    finally:
        database.engine.dispose()


def _at(settings: AppSettings, revision: str) -> Config:
    upgrade_database(settings)
    config = build_alembic_config(settings.database_path)
    command.downgrade(config, revision)
    return config


def test_the_link_gives_each_generation_the_entry_that_posted_it(
    database_settings: AppSettings,
) -> None:
    """The superseded row keeps the superseded entry, not the newest one."""
    config = _at(database_settings, "0020")
    _execute(database_settings, _REDONE)

    command.upgrade(config, "head")

    # Allocation 1 was written with entry 1 and allocation 2 with entry 3.
    # Entry 2 is the cancellation and carries no decision of its own.
    assert _links(database_settings) == [(1, 1), (2, 3)]
    database = create_database(database_settings)
    try:
        inspector = inspect(database.engine)
        entry_fk = next(
            item
            for item in inspector.get_foreign_keys(TABLE)
            if item["referred_table"] == "journal_entries"
        )
        indexes = {item["name"] for item in inspector.get_indexes(TABLE)}
        with database.engine.connect() as connection:
            violations = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
    finally:
        database.engine.dispose()
    assert entry_fk["options"].get("ondelete") == "RESTRICT"
    assert indexes >= {
        "ix_event_allocations_financial_event_id",
        "ix_event_allocations_journal_entry_id",
    }
    # The rebuild put back the trigger that reads this table from another one.
    assert ROUTE_TRIGGER in _triggers(database_settings)
    assert violations == []


def test_the_link_round_trips_and_derives_the_same_links_again(
    database_settings: AppSettings,
) -> None:
    """Going back loses only a link the timestamps can rebuild."""
    config = _at(database_settings, "0020")
    _execute(database_settings, _REDONE)
    command.upgrade(config, "head")
    before = _links(database_settings)

    command.downgrade(config, "0020")
    assert inspect_migration_status(database_settings).current_revision == "0020"
    assert COLUMN not in _columns(database_settings)
    assert ROUTE_TRIGGER in _triggers(database_settings)

    command.upgrade(config, "head")
    assert _links(database_settings) == before
    assert upgrade_database(database_settings).is_current


def test_0021_refuses_when_an_allocation_has_no_entry_to_name(
    database_settings: AppSettings,
) -> None:
    """A row it cannot resolve stops the revision instead of being guessed.

    Which decision is in force decides a wallet balance, so a link the
    timestamps cannot settle is a reason to change nothing at all.
    """
    config = _at(database_settings, "0020")
    # An allocation whose event was never posted: no entry can carry it.
    _execute(
        database_settings,
        (_ACCOUNT, _WALLET, _EVENT, _allocation(1, written_at="2026-09-01 10:00:00")),
    )

    with pytest.raises(Exception, match="could not be matched"):
        command.upgrade(config, "0021")

    # The whole revision rolled back, schema and revision marker together.
    assert inspect_migration_status(database_settings).current_revision == "0020"
    assert COLUMN not in _columns(database_settings)
    assert ROUTE_TRIGGER in _triggers(database_settings)


def test_0022_repairs_a_database_stamped_by_the_old_0021(
    database_settings: AppSettings,
) -> None:
    """`0022` repairs a DB that already ran the originally published `0021`.

    An entry's postings are built from the allocation rows, so the rows are
    written first and the entry a moment later. The old backfill looked in the
    opposite direction and could stamp both generations with entry 1. Simulate
    that already-stamped state directly: changing the source of `0021` cannot
    rerun a revision Alembic already considers complete.
    """
    config = _at(database_settings, "0020")
    _execute(database_settings, _REDONE)

    command.upgrade(config, "0021")
    assert _links(database_settings) == [(1, 1), (2, 3)]
    _execute(
        database_settings,
        ("UPDATE event_allocations SET journal_entry_id = 1 WHERE id = 2",),
    )
    # The defect left by the originally published 0021.
    assert _links(database_settings) == [(1, 1), (2, 1)]

    command.upgrade(config, "0022")
    assert _links(database_settings) == [(1, 1), (2, 3)]


def test_a_first_generation_crossing_a_second_reaches_head(
    database_settings: AppSettings,
) -> None:
    """A repair revision is useful only if every valid 0020 DB can reach it."""
    config = _at(database_settings, "0020")
    _execute(database_settings, _FIRST_GENERATION_CROSSES_SECOND)

    command.upgrade(config, "head")

    assert inspect_migration_status(database_settings).current_revision == "0022"
    assert _links(database_settings) == [(1, 1)]


def test_a_cross_second_first_generation_round_trips_through_0020(
    database_settings: AppSettings,
) -> None:
    """Losing and rebuilding the link must not strand a valid database."""
    config = _at(database_settings, "0020")
    _execute(database_settings, _FIRST_GENERATION_CROSSES_SECOND)
    command.upgrade(config, "head")
    before = _links(database_settings)

    command.downgrade(config, "0020")
    assert inspect_migration_status(database_settings).current_revision == "0020"
    assert COLUMN not in _columns(database_settings)

    command.upgrade(config, "head")
    assert inspect_migration_status(database_settings).current_revision == "0022"
    assert _links(database_settings) == before


def test_0022_leaves_a_database_that_never_ran_the_faulty_backfill_alone(
    database_settings: AppSettings,
) -> None:
    """Re-deriving is idempotent, so both histories end up identical.

    A database whose rows and entries share a timestamp to the second — every
    one written before this was noticed — already holds the right links, and
    running the repair over it changes nothing.
    """
    config = _at(database_settings, "0020")
    _execute(
        database_settings,
        _redone(
            first="2026-09-01 10:00:00",
            second="2026-09-01 12:00:00",
            second_entry="2026-09-01 12:00:00",
        ),
    )

    command.upgrade(config, "0021")
    already_right = _links(database_settings)
    assert already_right == [(1, 1), (2, 3)]

    command.upgrade(config, "0022")
    assert _links(database_settings) == already_right


def test_0022_refuses_when_two_generations_cannot_be_told_apart(
    database_settings: AppSettings,
) -> None:
    """A split posted and redone inside one second is refused, not guessed.

    Both generations resolve to the same entry and the other entry of the
    same event is left carrying no allocation, which is what says the
    timestamps cannot separate them. Picking one would misstate a wallet
    balance for good.
    """
    config = _at(database_settings, "0020")
    _execute(database_settings, _REDONE_WITHIN_ONE_SECOND)
    command.upgrade(config, "0021")

    with pytest.raises(Exception, match="carrying none"):
        command.upgrade(config, "0022")

    # Refused, so the revision marker stays behind and nothing was rewritten.
    assert inspect_migration_status(database_settings).current_revision == "0021"

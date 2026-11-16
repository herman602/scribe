"""SQLite engine, connection policy, and session factory."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from sqlalchemy import URL, Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.infrastructure.config.settings import AppSettings
from app.infrastructure.database.locking import SharedDatabaseLock


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _configure_connection(
    dbapi_connection: sqlite3.Connection, connection_record: object
) -> None:
    del connection_record
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute("PRAGMA synchronous = FULL")
        cursor.execute("PRAGMA busy_timeout = 5000")
        cursor.execute("PRAGMA journal_mode = WAL")
    finally:
        cursor.close()


@dataclass(frozen=True)
class Database:
    """Configured SQLAlchemy engine and short-lived session factory."""

    engine: Engine
    session_factory: sessionmaker[Session]


def create_database(settings: AppSettings) -> Database:
    """Create a SQLite engine configured for local durability and concurrency.

    Opening the database also claims it: every process that reads or writes —
    the Web application, the scheduled sync, and every CLI command — holds the
    shared advisory lock until its engine is disposed or it exits. The claim
    lives here rather than in each caller so that a new entry point cannot
    forget it, which is exactly how the scheduled sync went unprotected
    (ADR-B100).
    """
    database_path = settings.database_path.expanduser().resolve()
    database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    url = URL.create("sqlite+pysqlite", database=str(database_path))
    engine = create_engine(
        url,
        connect_args={"check_same_thread": False},
        hide_parameters=True,
        json_serializer=_canonical_json,
        pool_pre_ping=True,
    )
    event.listen(engine, "connect", _configure_connection)
    claim = SharedDatabaseLock(database_path)
    claim.acquire()

    def _release(_engine: Engine) -> None:
        claim.release()

    event.listen(engine, "engine_disposed", _release)
    return Database(
        engine=engine,
        session_factory=sessionmaker(
            bind=engine,
            class_=Session,
            expire_on_commit=False,
            autoflush=False,
        ),
    )

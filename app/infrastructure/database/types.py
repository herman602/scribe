"""Database type adapters preserving domain invariants."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator[datetime]):
    """Store UTC-naive values in SQLite and return UTC-aware datetimes."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(
        self, value: datetime | None, dialect: Dialect
    ) -> datetime | None:
        """Normalize an aware datetime to UTC before persistence."""
        del dialect
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("database datetimes must be timezone-aware")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(
        self, value: datetime | None, dialect: Dialect
    ) -> datetime | None:
        """Attach UTC to SQLite's timezone-naive datetime representation."""
        del dialect
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

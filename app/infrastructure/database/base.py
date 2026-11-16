"""Declarative base and shared SQLAlchemy metadata."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.infrastructure.database.types import UTCDateTime

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base for all infrastructure ORM mappings."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    """UTC creation and modification timestamps."""

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        server_default=func.current_timestamp(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )

"""Persistence ports for reviewed cutover evidence."""

from __future__ import annotations

from datetime import date, datetime
from typing import Protocol

from app.domain.budget_enums import PaymentMethod
from app.domain.cutover import ImportBatch, ImportRowEvidence


class ImportBatchRepository(Protocol):
    """Persist and replay one normalized source snapshot."""

    def get_by_hash(self, content_sha256: str) -> ImportBatch | None: ...

    def latest_applied(self) -> ImportBatch | None:
        """Return the newest immutable opening boundary, if one was applied."""
        ...

    def add(
        self,
        *,
        source_kind: str,
        format_version: int,
        content_sha256: str,
        source_locator_hash: str,
        observed_at: datetime,
        cutoff_at: datetime,
        row_count: int,
        warning_count: int,
        applied_at: datetime,
    ) -> ImportBatch: ...


class ImportRowRepository(Protocol):
    """Persist normalized per-row provenance without the original Sheet."""

    def get(self, import_row_id: int) -> ImportRowEvidence | None: ...

    def list_pending_card_candidates(self) -> tuple[ImportRowEvidence, ...]: ...

    def add(
        self,
        *,
        batch_id: int,
        entity_kind: str,
        source_row_key: str,
        normalized_payload_json: str,
        status: str,
        reason_code: str | None = None,
        created_entity_type: str | None = None,
        created_entity_id: int | None = None,
    ) -> int: ...


class LegacyDailyActivityRepository(Protocol):
    """Persist immutable pre-opening daily rows."""

    def add(
        self,
        *,
        import_row_id: int,
        activity_date: date,
        sequence: int,
        amount_minor: int,
        currency: str,
        payment_method: PaymentMethod,
        note: str | None,
    ) -> int: ...

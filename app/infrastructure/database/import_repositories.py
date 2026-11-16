"""SQLAlchemy repositories for cutover audit evidence."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.domain.budget_enums import PaymentMethod
from app.domain.cutover import (
    ImportBatch,
    ImportBatchStatus,
    ImportRowEvidence,
    ImportRowStatus,
)
from app.infrastructure.database.import_models import (
    ImportBatchModel,
    ImportRowModel,
    LegacyDailyActivityModel,
)


def _batch(model: ImportBatchModel) -> ImportBatch:
    return ImportBatch(
        id=model.id,
        content_sha256=model.content_sha256,
        observed_at=model.observed_at,
        cutoff_at=model.cutoff_at,
        status=ImportBatchStatus(model.status),
        row_count=model.row_count,
        warning_count=model.warning_count,
        applied_at=model.applied_at,
    )


class SqlAlchemyImportBatchRepository:
    """Persist one applied document identity."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_hash(self, content_sha256: str) -> ImportBatch | None:
        model = self._session.scalar(
            select(ImportBatchModel).where(
                ImportBatchModel.content_sha256 == content_sha256
            )
        )
        return None if model is None else _batch(model)

    def latest_applied(self) -> ImportBatch | None:
        """Return the greatest applied cutoff without trusting insertion order."""
        model = self._session.scalar(
            select(ImportBatchModel)
            .where(ImportBatchModel.status == ImportBatchStatus.APPLIED.value)
            .order_by(desc(ImportBatchModel.cutoff_at), desc(ImportBatchModel.id))
            .limit(1)
        )
        return None if model is None else _batch(model)

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
    ) -> ImportBatch:
        model = ImportBatchModel(
            source_kind=source_kind,
            format_version=format_version,
            content_sha256=content_sha256,
            source_locator_hash=source_locator_hash,
            observed_at=observed_at,
            cutoff_at=cutoff_at,
            status=ImportBatchStatus.APPLIED.value,
            row_count=row_count,
            warning_count=warning_count,
            applied_at=applied_at,
        )
        self._session.add(model)
        self._session.flush()
        return _batch(model)


class SqlAlchemyImportRowRepository:
    """Persist a normalized row and return its local identity."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @staticmethod
    def _record(model: ImportRowModel) -> ImportRowEvidence:
        return ImportRowEvidence(
            id=model.id,
            batch_id=model.batch_id,
            entity_kind=model.entity_kind,
            source_row_key=model.source_row_key,
            normalized_payload_json=model.normalized_payload_json,
            status=ImportRowStatus(model.status),
            reason_code=model.reason_code,
        )

    def get(self, import_row_id: int) -> ImportRowEvidence | None:
        model = self._session.get(ImportRowModel, import_row_id)
        return None if model is None else self._record(model)

    def list_pending_card_candidates(self) -> tuple[ImportRowEvidence, ...]:
        models = self._session.scalars(
            select(ImportRowModel)
            .where(
                ImportRowModel.status == "pending",
                ImportRowModel.reason_code == "card_mapping_required",
                ImportRowModel.entity_kind.in_(
                    ("card_cycle", "scheduled_transfer_candidate")
                ),
            )
            .order_by(ImportRowModel.entity_kind, ImportRowModel.source_row_key)
        ).all()
        return tuple(self._record(model) for model in models)

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
    ) -> int:
        model = ImportRowModel(
            batch_id=batch_id,
            entity_kind=entity_kind,
            source_row_key=source_row_key,
            normalized_payload_json=normalized_payload_json,
            status=status,
            reason_code=reason_code,
            created_entity_type=created_entity_type,
            created_entity_id=created_entity_id,
        )
        self._session.add(model)
        self._session.flush()
        return model.id


class SqlAlchemyLegacyDailyActivityRepository:
    """Persist one immutable pre-opening row."""

    def __init__(self, session: Session) -> None:
        self._session = session

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
    ) -> int:
        model = LegacyDailyActivityModel(
            import_row_id=import_row_id,
            activity_date=activity_date,
            sequence=sequence,
            amount_minor=amount_minor,
            currency=currency,
            payment_method=payment_method.value,
            note=note,
            inclusion_mode="pre_opening_history",
        )
        self._session.add(model)
        self._session.flush()
        return model.id

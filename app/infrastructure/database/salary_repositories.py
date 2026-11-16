"""SQLAlchemy repositories for the salary plan and its frozen monthly runs.

Every state change here is one conditional statement. A read followed by an
unconditional save would let a second window overwrite what the first just
approved, and for money that is not a stale display but a lost decision.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from sqlalchemy import func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.domain.budget_enums import (
    CommitmentFulfillmentMode,
    SalaryCalculationMode,
    SalaryRunItemStatus,
    SalaryRunStatus,
)
from app.domain.budget_models import (
    SalaryAllocationRun,
    SalaryAllocationRunDraft,
    SalaryAllocationRunItem,
    SalaryAllocationRunItemDraft,
    SalaryPlanTemplate,
    SalaryPlanTemplateDraft,
    SalaryPlanTemplateItem,
    SalaryPlanTemplateItemDraft,
    SalaryPlanTemplateItemUpdate,
    SalaryPlanTemplateUpdate,
)
from app.domain.errors import (
    ConcurrentUpdateError,
    DatabaseError,
    SalaryAllocationError,
    SalaryPlanError,
    ScribeError,
)
from app.infrastructure.database.budget_models import (
    SalaryAllocationRunItemModel,
    SalaryAllocationRunModel,
    SalaryPlanTemplateItemModel,
    SalaryPlanTemplateModel,
)

_FROZEN_SNAPSHOT = "snapshot cannot be changed"
#: SQLite reports a violated unique index by its columns rather than by its
#: name, so these are the column lists each index produces. The longer
#: generation identity is listed before the live-period index, because the
#: shorter message is a prefix of the longer one.
_ITEMS = "salary_plan_template_items"
_RUNS = "salary_allocation_runs"
_RUN_ITEMS = "salary_allocation_run_items"
_UNIQUE_MESSAGES: tuple[tuple[str, type[ScribeError], str], ...] = (
    (
        f"{_ITEMS}.template_id, {_ITEMS}.destination_wallet_id",
        SalaryPlanError,
        "this template already sends money to that wallet",
    ),
    (
        f"{_ITEMS}.template_id, {_ITEMS}.name",
        SalaryPlanError,
        "this template already has an item with that name",
    ),
    (
        "salary_plan_templates.name, salary_plan_templates.currency",
        SalaryPlanError,
        "an active template already uses this name and currency",
    ),
    (
        f"{_RUNS}.template_id, {_RUNS}.budget_period_id, {_RUNS}.generation",
        SalaryAllocationError,
        "this generation of the month already exists",
    ),
    (
        f"{_RUNS}.template_id, {_RUNS}.budget_period_id",
        SalaryAllocationError,
        "this template already has a run for this month",
    ),
    (
        f"{_RUNS}.salary_event_id",
        SalaryAllocationError,
        "this salary is already distributed by a run",
    ),
    (
        f"{_RUN_ITEMS}.run_id, {_RUN_ITEMS}.template_item_id",
        SalaryAllocationError,
        "this run already froze that template item",
    ),
    (
        f"{_RUN_ITEMS}.transfer_entry_id",
        SalaryAllocationError,
        "this journal entry already belongs to another salary line",
    ),
)

#: A run whose items still hold money back in the source wallet. A draft
#: reserves nothing, and a cancelled run reserves nothing either.
_RESERVING_RUN_STATUSES = (
    SalaryRunStatus.APPROVED.value,
    SalaryRunStatus.PARTIALLY_FUNDED.value,
)


def _translated(error: SQLAlchemyError) -> ScribeError:
    """Turn a database defence into the stable error the service also raises.

    Each service check and its write are two statements, so a concurrent
    writer can still lose the race at the index. Translating here keeps that
    outcome a recognizable application error instead of a leaked traceback.
    """
    message = str(error)
    if _FROZEN_SNAPSHOT in message:
        return SalaryAllocationError(
            "a salary run snapshot is frozen and cannot be rewritten"
        )
    for columns, error_type, description in _UNIQUE_MESSAGES:
        if columns in message:
            return error_type(description)
    return DatabaseError("database write failed")


def _flush_and_refresh(session: Session, model: Any) -> None:
    """Write one row now, so a constraint or trigger reports here."""
    try:
        session.flush()
        session.refresh(model)
    except SQLAlchemyError as error:
        raise _translated(error) from error


def salary_template_record(model: SalaryPlanTemplateModel) -> SalaryPlanTemplate:
    """Translate one persisted template into its domain record."""
    return SalaryPlanTemplate(
        id=model.id,
        name=model.name,
        currency=model.currency,
        source_account_id=model.source_account_id,
        is_active=model.is_active,
        version=model.version,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def salary_template_item_record(
    model: SalaryPlanTemplateItemModel,
) -> SalaryPlanTemplateItem:
    """Translate one persisted template item into its domain record."""
    return SalaryPlanTemplateItem(
        id=model.id,
        template_id=model.template_id,
        name=model.name,
        destination_wallet_id=model.destination_wallet_id,
        budget_category_id=model.budget_category_id,
        calculation_mode=SalaryCalculationMode(model.calculation_mode),
        fixed_amount_minor=model.fixed_amount_minor,
        display_order=model.display_order,
        is_active=model.is_active,
        version=model.version,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def salary_run_record(model: SalaryAllocationRunModel) -> SalaryAllocationRun:
    """Translate one persisted run into its domain record."""
    return SalaryAllocationRun(
        id=model.id,
        template_id=model.template_id,
        template_name_snapshot=model.template_name_snapshot,
        template_version=model.template_version,
        budget_period_id=model.budget_period_id,
        salary_event_id=model.salary_event_id,
        source_account_id=model.source_account_id,
        source_wallet_id=model.source_wallet_id,
        currency=model.currency,
        generation=model.generation,
        salary_amount_minor=model.salary_amount_minor,
        planned_amount_minor=model.planned_amount_minor,
        remainder_minor=model.remainder_minor,
        subscription_evidence_schema_version=(
            model.subscription_evidence_schema_version
        ),
        subscription_evidence_json=model.subscription_evidence_json,
        status=SalaryRunStatus(model.status),
        approval_entry_id=model.approval_entry_id,
        regenerated_from_run_id=model.regenerated_from_run_id,
        request_fingerprint=model.request_fingerprint,
        generated_at=model.generated_at,
        approved_at=model.approved_at,
        cancelled_at=model.cancelled_at,
        version=model.version,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def salary_run_item_record(
    model: SalaryAllocationRunItemModel,
) -> SalaryAllocationRunItem:
    """Translate one persisted run item into its domain record."""
    return SalaryAllocationRunItem(
        id=model.id,
        run_id=model.run_id,
        template_item_id=model.template_item_id,
        template_item_version=model.template_item_version,
        name_snapshot=model.name_snapshot,
        destination_wallet_id=model.destination_wallet_id,
        destination_account_id=model.destination_account_id,
        destination_wallet_name_snapshot=model.destination_wallet_name_snapshot,
        destination_account_name_snapshot=model.destination_account_name_snapshot,
        budget_category_id=model.budget_category_id,
        budget_category_name_snapshot=model.budget_category_name_snapshot,
        calculation_mode=SalaryCalculationMode(model.calculation_mode),
        amount_minor=model.amount_minor,
        currency=model.currency,
        display_order=model.display_order,
        fulfillment_mode=CommitmentFulfillmentMode(model.fulfillment_mode),
        status=SalaryRunItemStatus(model.status),
        calculation_evidence_json=model.calculation_evidence_json,
        transfer_entry_id=model.transfer_entry_id,
        transferred_at=model.transferred_at,
        version=model.version,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class SqlAlchemySalaryPlanTemplateRepository:
    """SQLAlchemy repository for the current salary distribution templates."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, draft: SalaryPlanTemplateDraft) -> SalaryPlanTemplate:
        """Persist one template; the draft has already validated itself."""
        model = SalaryPlanTemplateModel(
            name=draft.name,
            currency=draft.currency,
            source_account_id=draft.source_account_id,
            is_active=True,
        )
        self._session.add(model)
        _flush_and_refresh(self._session, model)
        return salary_template_record(model)

    def get(self, template_id: int) -> SalaryPlanTemplate | None:
        """Return one template by internal ID."""
        model = self._session.get(SalaryPlanTemplateModel, template_id)
        return None if model is None else salary_template_record(model)

    def list_all(self) -> tuple[SalaryPlanTemplate, ...]:
        """Return every template, deactivated ones included."""
        models = self._session.scalars(
            select(SalaryPlanTemplateModel).order_by(
                SalaryPlanTemplateModel.currency,
                SalaryPlanTemplateModel.name,
                SalaryPlanTemplateModel.id,
            )
        ).all()
        return tuple(salary_template_record(model) for model in models)

    def save(self, change: SalaryPlanTemplateUpdate) -> SalaryPlanTemplate:
        """Apply one optimistic rename or activation change.

        The currency and the salary account are absent on purpose: moving a
        template to another account would reinterpret the frozen lines it has
        already produced, so that is a new template (ADR-B042).
        """
        return self._conditional_update(
            change.template_id,
            change.expected_version,
            {
                "name": change.name,
                "is_active": change.is_active,
                "version": SalaryPlanTemplateModel.version + 1,
            },
        )

    def touch_version(
        self, template_id: int, expected_version: int
    ) -> SalaryPlanTemplate:
        """Raise a template's version because one of its items changed.

        An item is part of the plan, so a form that was rendered before the
        item moved is stale even though the template row itself still reads
        the same.
        """
        return self._conditional_update(
            template_id,
            expected_version,
            {"version": SalaryPlanTemplateModel.version + 1},
        )

    def has_items_or_runs(self, template_id: int) -> bool:
        """Return whether anything already depends on this template's shape."""
        for statement in (
            select(SalaryPlanTemplateItemModel.id).where(
                SalaryPlanTemplateItemModel.template_id == template_id
            ),
            select(SalaryAllocationRunModel.id).where(
                SalaryAllocationRunModel.template_id == template_id
            ),
        ):
            if self._session.scalar(statement) is not None:
                return True
        return False

    def _conditional_update(
        self, template_id: int, expected_version: int, values: dict[str, Any]
    ) -> SalaryPlanTemplate:
        try:
            result = cast(
                CursorResult[Any],
                self._session.execute(
                    update(SalaryPlanTemplateModel)
                    .where(
                        SalaryPlanTemplateModel.id == template_id,
                        SalaryPlanTemplateModel.version == expected_version,
                    )
                    .values(**values)
                ),
            )
        except SQLAlchemyError as error:
            raise _translated(error) from error
        if result.rowcount != 1:
            if self._session.get(SalaryPlanTemplateModel, template_id) is None:
                raise SalaryPlanError("salary template was not found")
            raise ConcurrentUpdateError("template changed after this page was loaded")
        model = self._session.get(SalaryPlanTemplateModel, template_id)
        if model is None:  # pragma: no cover - just updated
            raise SalaryPlanError("salary template disappeared")
        self._session.refresh(model)
        return salary_template_record(model)


class SqlAlchemySalaryPlanTemplateItemRepository:
    """SQLAlchemy repository for one template's destinations."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, draft: SalaryPlanTemplateItemDraft) -> SalaryPlanTemplateItem:
        """Persist one destination; the draft has already validated itself."""
        model = SalaryPlanTemplateItemModel(
            template_id=draft.template_id,
            name=draft.name,
            destination_wallet_id=draft.destination_wallet_id,
            budget_category_id=draft.budget_category_id,
            calculation_mode=draft.calculation_mode.value,
            fixed_amount_minor=draft.fixed_amount_minor,
            display_order=draft.display_order,
            is_active=True,
        )
        self._session.add(model)
        _flush_and_refresh(self._session, model)
        return salary_template_item_record(model)

    def get(self, template_item_id: int) -> SalaryPlanTemplateItem | None:
        """Return one template item by internal ID."""
        model = self._session.get(SalaryPlanTemplateItemModel, template_item_id)
        return None if model is None else salary_template_item_record(model)

    def list_for_template(
        self, template_id: int, *, active_only: bool = False
    ) -> tuple[SalaryPlanTemplateItem, ...]:
        """Return one template's destinations in display order."""
        statement = select(SalaryPlanTemplateItemModel).where(
            SalaryPlanTemplateItemModel.template_id == template_id
        )
        if active_only:
            statement = statement.where(SalaryPlanTemplateItemModel.is_active.is_(True))
        models = self._session.scalars(
            statement.order_by(
                SalaryPlanTemplateItemModel.display_order,
                SalaryPlanTemplateItemModel.id,
            )
        ).all()
        return tuple(salary_template_item_record(model) for model in models)

    def save(self, change: SalaryPlanTemplateItemUpdate) -> SalaryPlanTemplateItem:
        """Apply one optimistic correction of a destination."""
        return self._conditional_update(
            change.template_item_id,
            change.expected_version,
            {
                "name": change.name,
                "destination_wallet_id": change.destination_wallet_id,
                "budget_category_id": change.budget_category_id,
                "calculation_mode": change.calculation_mode.value,
                "fixed_amount_minor": change.fixed_amount_minor,
                "display_order": change.display_order,
                "is_active": change.is_active,
                "version": SalaryPlanTemplateItemModel.version + 1,
            },
        )

    def deactivate(
        self, template_item_id: int, expected_version: int
    ) -> SalaryPlanTemplateItem:
        """Stop using one destination without deleting the runs that used it."""
        return self._conditional_update(
            template_item_id,
            expected_version,
            {
                "is_active": False,
                "version": SalaryPlanTemplateItemModel.version + 1,
            },
        )

    def find_active_conflict(
        self,
        *,
        template_id: int,
        name: str,
        destination_wallet_id: int,
        exclude_id: int | None = None,
    ) -> SalaryPlanTemplateItem | None:
        """Return the active item a proposed name or destination collides with."""
        statement = select(SalaryPlanTemplateItemModel).where(
            SalaryPlanTemplateItemModel.template_id == template_id,
            SalaryPlanTemplateItemModel.is_active.is_(True),
            or_(
                SalaryPlanTemplateItemModel.name == name.strip(),
                SalaryPlanTemplateItemModel.destination_wallet_id
                == destination_wallet_id,
            ),
        )
        if exclude_id is not None:
            statement = statement.where(SalaryPlanTemplateItemModel.id != exclude_id)
        model = self._session.scalar(statement.order_by(SalaryPlanTemplateItemModel.id))
        return None if model is None else salary_template_item_record(model)

    def is_referenced(self, template_item_id: int) -> bool:
        """Return whether a generated month already froze this destination."""
        return (
            self._session.scalar(
                select(SalaryAllocationRunItemModel.id).where(
                    SalaryAllocationRunItemModel.template_item_id == template_item_id
                )
            )
            is not None
        )

    def _conditional_update(
        self, template_item_id: int, expected_version: int, values: dict[str, Any]
    ) -> SalaryPlanTemplateItem:
        try:
            result = cast(
                CursorResult[Any],
                self._session.execute(
                    update(SalaryPlanTemplateItemModel)
                    .where(
                        SalaryPlanTemplateItemModel.id == template_item_id,
                        SalaryPlanTemplateItemModel.version == expected_version,
                    )
                    .values(**values)
                ),
            )
        except SQLAlchemyError as error:
            raise _translated(error) from error
        if result.rowcount != 1:
            if self._session.get(SalaryPlanTemplateItemModel, template_item_id) is None:
                raise SalaryPlanError("salary template item was not found")
            raise ConcurrentUpdateError("this item changed after the page was loaded")
        model = self._session.get(SalaryPlanTemplateItemModel, template_item_id)
        if model is None:  # pragma: no cover - just updated
            raise SalaryPlanError("salary template item disappeared")
        self._session.refresh(model)
        return salary_template_item_record(model)


class SqlAlchemySalaryAllocationRunRepository:
    """SQLAlchemy repository for frozen monthly salary distributions."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, draft: SalaryAllocationRunDraft) -> SalaryAllocationRun:
        """Insert one generation; its snapshot columns are never updated again."""
        model = SalaryAllocationRunModel(
            template_id=draft.template_id,
            template_name_snapshot=draft.template_name_snapshot,
            template_version=draft.template_version,
            budget_period_id=draft.budget_period_id,
            salary_event_id=draft.salary_event_id,
            source_account_id=draft.source_account_id,
            source_wallet_id=draft.source_wallet_id,
            currency=draft.currency,
            generation=draft.generation,
            salary_amount_minor=draft.salary_amount_minor,
            planned_amount_minor=draft.planned_amount_minor,
            remainder_minor=draft.remainder_minor,
            subscription_evidence_schema_version=(
                draft.subscription_evidence_schema_version
            ),
            subscription_evidence_json=draft.subscription_evidence_json,
            status=SalaryRunStatus.DRAFT.value,
            regenerated_from_run_id=draft.regenerated_from_run_id,
            request_fingerprint=draft.request_fingerprint,
            generated_at=draft.generated_at,
        )
        self._session.add(model)
        _flush_and_refresh(self._session, model)
        return salary_run_record(model)

    def get(self, run_id: int) -> SalaryAllocationRun | None:
        """Return one run by internal ID."""
        model = self._session.get(SalaryAllocationRunModel, run_id)
        return None if model is None else salary_run_record(model)

    def find_live(
        self, *, template_id: int, budget_period_id: int
    ) -> SalaryAllocationRun | None:
        """Return the generation that currently owns one template and month."""
        model = self._session.scalar(
            select(SalaryAllocationRunModel).where(
                SalaryAllocationRunModel.template_id == template_id,
                SalaryAllocationRunModel.budget_period_id == budget_period_id,
                SalaryAllocationRunModel.status != SalaryRunStatus.CANCELLED.value,
            )
        )
        return None if model is None else salary_run_record(model)

    def find_live_by_salary_event(
        self, salary_event_id: int
    ) -> SalaryAllocationRun | None:
        """Return the generation that already distributes one salary."""
        model = self._session.scalar(
            select(SalaryAllocationRunModel).where(
                SalaryAllocationRunModel.salary_event_id == salary_event_id,
                SalaryAllocationRunModel.status != SalaryRunStatus.CANCELLED.value,
            )
        )
        return None if model is None else salary_run_record(model)

    def find_regenerated_from(self, run_id: int) -> SalaryAllocationRun | None:
        """Return the generation that was written to replace one draft.

        This is what makes a resubmitted regeneration recognizable: a later
        generation of the same month created by some other path does not carry
        this link, so it can never be mistaken for this request's own result
        (ADR-B049).
        """
        model = self._session.scalar(
            select(SalaryAllocationRunModel).where(
                SalaryAllocationRunModel.regenerated_from_run_id == run_id
            )
        )
        return None if model is None else salary_run_record(model)

    def list_for_period(self, budget_period_id: int) -> tuple[SalaryAllocationRun, ...]:
        """Return every generation recorded for one budget month."""
        models = self._session.scalars(
            select(SalaryAllocationRunModel)
            .where(SalaryAllocationRunModel.budget_period_id == budget_period_id)
            .order_by(
                SalaryAllocationRunModel.template_id,
                SalaryAllocationRunModel.generation,
            )
        ).all()
        return tuple(salary_run_record(model) for model in models)

    def list_generations(
        self, *, template_id: int, budget_period_id: int
    ) -> tuple[SalaryAllocationRun, ...]:
        """Return one template's history for one month, oldest generation first."""
        models = self._session.scalars(
            select(SalaryAllocationRunModel)
            .where(
                SalaryAllocationRunModel.template_id == template_id,
                SalaryAllocationRunModel.budget_period_id == budget_period_id,
            )
            .order_by(SalaryAllocationRunModel.generation)
        ).all()
        return tuple(salary_run_record(model) for model in models)

    def next_generation(self, *, template_id: int, budget_period_id: int) -> int:
        """Return the generation number a regeneration would take next."""
        highest = self._session.execute(
            select(
                func.coalesce(func.max(SalaryAllocationRunModel.generation), 0)
            ).where(
                SalaryAllocationRunModel.template_id == template_id,
                SalaryAllocationRunModel.budget_period_id == budget_period_id,
            )
        ).scalar_one()
        return int(highest) + 1

    def cancel_draft(
        self, run_id: int, *, expected_version: int, cancelled_at: datetime
    ) -> SalaryAllocationRun:
        """Cancel one draft; an approved run is never cancelled or rewritten."""
        return self._conditional_update(
            run_id,
            expected_version=expected_version,
            expected_status=SalaryRunStatus.DRAFT,
            values={
                "status": SalaryRunStatus.CANCELLED.value,
                "cancelled_at": cancelled_at,
                "version": SalaryAllocationRunModel.version + 1,
            },
        )

    def approve(
        self,
        run_id: int,
        *,
        expected_version: int,
        status: SalaryRunStatus,
        approval_entry_id: int | None,
        approved_at: datetime,
    ) -> SalaryAllocationRun:
        """Move one draft to the status its lines have just reached."""
        return self._conditional_update(
            run_id,
            expected_version=expected_version,
            expected_status=SalaryRunStatus.DRAFT,
            values={
                "status": status.value,
                "approval_entry_id": approval_entry_id,
                "approved_at": approved_at,
                "version": SalaryAllocationRunModel.version + 1,
            },
        )

    def refresh_status(
        self,
        run_id: int,
        *,
        expected_version: int,
        expected_status: SalaryRunStatus,
        status: SalaryRunStatus,
    ) -> SalaryAllocationRun:
        """Re-derive one run's status after a transfer was confirmed."""
        return self._conditional_update(
            run_id,
            expected_version=expected_version,
            expected_status=expected_status,
            values={
                "status": status.value,
                "version": SalaryAllocationRunModel.version + 1,
            },
        )

    def _conditional_update(
        self,
        run_id: int,
        *,
        expected_version: int,
        expected_status: SalaryRunStatus,
        values: dict[str, Any],
    ) -> SalaryAllocationRun:
        try:
            result = cast(
                CursorResult[Any],
                self._session.execute(
                    update(SalaryAllocationRunModel)
                    .where(
                        SalaryAllocationRunModel.id == run_id,
                        SalaryAllocationRunModel.version == expected_version,
                        SalaryAllocationRunModel.status == expected_status.value,
                    )
                    .values(**values)
                ),
            )
        except SQLAlchemyError as error:
            raise _translated(error) from error
        if result.rowcount != 1:
            model = self._session.get(SalaryAllocationRunModel, run_id)
            if model is None:
                raise SalaryAllocationError("salary allocation run was not found")
            if model.status != expected_status.value:
                raise SalaryAllocationError(
                    f"a {model.status} salary run no longer accepts this change"
                )
            raise ConcurrentUpdateError("this run changed after the page was loaded")
        model = self._session.get(SalaryAllocationRunModel, run_id)
        if model is None:  # pragma: no cover - just updated
            raise SalaryAllocationError("salary allocation run disappeared")
        self._session.refresh(model)
        return salary_run_record(model)


class SqlAlchemySalaryAllocationRunItemRepository:
    """SQLAlchemy repository for the frozen lines of one run."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, draft: SalaryAllocationRunItemDraft) -> SalaryAllocationRunItem:
        """Insert one frozen line; its snapshot is never updated again."""
        model = SalaryAllocationRunItemModel(
            run_id=draft.run_id,
            template_item_id=draft.template_item_id,
            template_item_version=draft.template_item_version,
            name_snapshot=draft.name_snapshot,
            destination_wallet_id=draft.destination_wallet_id,
            destination_account_id=draft.destination_account_id,
            destination_wallet_name_snapshot=draft.destination_wallet_name_snapshot,
            destination_account_name_snapshot=draft.destination_account_name_snapshot,
            budget_category_id=draft.budget_category_id,
            budget_category_name_snapshot=draft.budget_category_name_snapshot,
            calculation_mode=draft.calculation_mode.value,
            amount_minor=draft.amount_minor,
            currency=draft.currency,
            display_order=draft.display_order,
            fulfillment_mode=draft.fulfillment_mode.value,
            status=SalaryRunItemStatus.DRAFT.value,
            calculation_evidence_json=draft.calculation_evidence_json,
        )
        self._session.add(model)
        _flush_and_refresh(self._session, model)
        return salary_run_item_record(model)

    def get(self, run_item_id: int) -> SalaryAllocationRunItem | None:
        """Return one frozen line by internal ID."""
        model = self._session.get(SalaryAllocationRunItemModel, run_item_id)
        return None if model is None else salary_run_item_record(model)

    def list_for_run(self, run_id: int) -> tuple[SalaryAllocationRunItem, ...]:
        """Return one run's lines in the order the template asked for."""
        models = self._session.scalars(
            select(SalaryAllocationRunItemModel)
            .where(SalaryAllocationRunItemModel.run_id == run_id)
            .order_by(
                SalaryAllocationRunItemModel.display_order,
                SalaryAllocationRunItemModel.id,
            )
        ).all()
        return tuple(salary_run_item_record(model) for model in models)

    def transition(
        self,
        run_item_id: int,
        *,
        expected_version: int,
        expected_status: SalaryRunItemStatus,
        status: SalaryRunItemStatus,
        transfer_entry_id: int | None = None,
        transferred_at: datetime | None = None,
    ) -> SalaryAllocationRunItem:
        """Move one line forward, only from the exact state it was read in."""
        try:
            result = cast(
                CursorResult[Any],
                self._session.execute(
                    update(SalaryAllocationRunItemModel)
                    .where(
                        SalaryAllocationRunItemModel.id == run_item_id,
                        SalaryAllocationRunItemModel.version == expected_version,
                        SalaryAllocationRunItemModel.status == expected_status.value,
                    )
                    .values(
                        status=status.value,
                        transfer_entry_id=transfer_entry_id,
                        transferred_at=transferred_at,
                        version=SalaryAllocationRunItemModel.version + 1,
                    )
                ),
            )
        except SQLAlchemyError as error:
            raise _translated(error) from error
        if result.rowcount != 1:
            model = self._session.get(SalaryAllocationRunItemModel, run_item_id)
            if model is None:
                raise SalaryAllocationError("salary run item was not found")
            if model.status != expected_status.value:
                raise SalaryAllocationError(
                    f"a {model.status} salary item no longer accepts this change"
                )
            raise ConcurrentUpdateError("this item changed after the page was loaded")
        model = self._session.get(SalaryAllocationRunItemModel, run_item_id)
        if model is None:  # pragma: no cover - just updated
            raise SalaryAllocationError("salary run item disappeared")
        self._session.refresh(model)
        return salary_run_item_record(model)

    def get_by_transfer_entry(self, entry_id: int) -> SalaryAllocationRunItem | None:
        """Return the single line one posted transfer belongs to."""
        model = self._session.scalar(
            select(SalaryAllocationRunItemModel).where(
                SalaryAllocationRunItemModel.transfer_entry_id == entry_id
            )
        )
        return None if model is None else salary_run_item_record(model)

    def list_waiting_for_source_wallet(
        self, wallet_id: int
    ) -> tuple[SalaryAllocationRunItem, ...]:
        """Return the lines still holding money back in one source wallet."""
        models = self._session.scalars(
            self._waiting_statement(wallet_id, SalaryAllocationRunItemModel).order_by(
                SalaryAllocationRunItemModel.id
            )
        ).all()
        return tuple(salary_run_item_record(model) for model in models)

    def reserved_minor_for_wallet(self, wallet_id: int) -> int:
        """Return what waiting salary transfers still reserve in one wallet."""
        total = self._session.execute(
            self._waiting_statement(
                wallet_id,
                func.coalesce(func.sum(SalaryAllocationRunItemModel.amount_minor), 0),
            )
        ).scalar_one()
        return int(total)

    @staticmethod
    def _waiting_statement(wallet_id: int, selected: Any) -> Any:
        """Select waiting lines whose run still draws on one source wallet.

        The wallet lives on the run, not on the line, so this always joins.
        Restricting the run's own status too keeps a cancelled or already
        funded generation from being counted as a live reservation.
        """
        return (
            select(selected)
            .join(
                SalaryAllocationRunModel,
                SalaryAllocationRunModel.id == SalaryAllocationRunItemModel.run_id,
            )
            .where(
                SalaryAllocationRunModel.source_wallet_id == wallet_id,
                SalaryAllocationRunModel.status.in_(_RESERVING_RUN_STATUSES),
                SalaryAllocationRunItemModel.status
                == SalaryRunItemStatus.WAITING_TRANSFER.value,
            )
        )

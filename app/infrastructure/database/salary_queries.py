"""Indexed SQLAlchemy read models for the salary plan and its frozen months."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.application.queries.salary import (
    EligibleSalaryEventRow,
    SalaryAccountOption,
    SalaryMonthView,
    SalaryRunItemView,
    SalaryRunView,
    SalaryTemplateItemRow,
    SalaryTemplateRow,
    SalaryWalletOption,
    SalaryWalletPosition,
)
from app.domain.budget_enums import (
    BudgetPeriodStatus,
    CommitmentStatus,
    FinancialAccountType,
    FinancialEventStatus,
    FinancialEventType,
    JournalEntryType,
    LedgerAccountKind,
    SalaryRunItemStatus,
    SalaryRunStatus,
    WalletPurpose,
)
from app.domain.budget_models import (
    BudgetCategory,
    SalaryAllocationRun,
    normalize_currency,
)
from app.domain.budget_rules import (
    SalaryPostingView,
    deserialize_item_evidence,
    deserialize_subscription_evidence,
    holds_wallets,
    salary_event_problem,
)
from app.infrastructure.database.budget_models import (
    BudgetCategoryModel,
    BudgetPeriodModel,
    FinancialAccountModel,
    FinancialEventModel,
    FundingCommitmentModel,
    JournalEntryModel,
    JournalPostingModel,
    LedgerAccountModel,
    SalaryAllocationRunItemModel,
    SalaryAllocationRunModel,
    SalaryPlanTemplateItemModel,
    SalaryPlanTemplateModel,
    WalletModel,
)
from app.infrastructure.database.budget_repositories import (
    budget_category_record,
    financial_event_record,
)
from app.infrastructure.database.import_models import ImportBatchModel
from app.infrastructure.database.salary_repositories import (
    salary_run_item_record,
    salary_run_record,
    salary_template_item_record,
    salary_template_record,
)

_UNFUNDED_COMMITMENT_STATUSES = (
    CommitmentStatus.OPEN.value,
    CommitmentStatus.PARTIALLY_SCHEDULED.value,
    CommitmentStatus.SCHEDULED.value,
)
_RESERVING_RUN_STATUSES = (
    SalaryRunStatus.APPROVED.value,
    SalaryRunStatus.PARTIALLY_FUNDED.value,
)


class SqlAlchemySalaryReadRepository:
    """Short-session reads for the allocation pages, the CLI, and previews."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def templates(self) -> tuple[SalaryTemplateRow, ...]:
        """Return every template with its salary account and destinations."""
        with self._session_factory() as session:
            return tuple(
                self._template_row(session, model)
                for model in session.scalars(
                    select(SalaryPlanTemplateModel).order_by(
                        SalaryPlanTemplateModel.currency,
                        SalaryPlanTemplateModel.name,
                        SalaryPlanTemplateModel.id,
                    )
                ).all()
            )

    def template(self, template_id: int) -> SalaryTemplateRow | None:
        """Return one template and its destinations, or None."""
        with self._session_factory() as session:
            model = session.get(SalaryPlanTemplateModel, template_id)
            return None if model is None else self._template_row(session, model)

    def account_options(self) -> tuple[SalaryAccountOption, ...]:
        """Return active asset accounts and whether each can hold a salary."""
        with self._session_factory() as session:
            accounts = session.scalars(
                select(FinancialAccountModel)
                .where(FinancialAccountModel.is_active.is_(True))
                .order_by(
                    FinancialAccountModel.institution_code,
                    FinancialAccountModel.display_name,
                    FinancialAccountModel.id,
                )
            ).all()
            unallocated = {
                row[0]
                for row in session.execute(
                    select(WalletModel.financial_account_id).where(
                        WalletModel.purpose == WalletPurpose.UNALLOCATED.value,
                        WalletModel.is_active.is_(True),
                    )
                ).all()
            }
        return tuple(
            SalaryAccountOption(
                financial_account_id=account.id,
                display_name=account.display_name,
                institution_code=account.institution_code,
                currency=account.currency,
                has_unallocated_wallet=account.id in unallocated,
            )
            for account in accounts
            if holds_wallets(FinancialAccountType(account.account_type))
        )

    def wallet_options(self, currency: str) -> tuple[SalaryWalletOption, ...]:
        """Return active wallets a plan in one currency could pay into."""
        code = normalize_currency(currency)
        with self._session_factory() as session:
            rows = session.execute(
                select(WalletModel, FinancialAccountModel)
                .join(
                    FinancialAccountModel,
                    FinancialAccountModel.id == WalletModel.financial_account_id,
                )
                .where(
                    WalletModel.is_active.is_(True),
                    FinancialAccountModel.is_active.is_(True),
                    FinancialAccountModel.currency == code,
                )
                .order_by(
                    FinancialAccountModel.institution_code,
                    FinancialAccountModel.display_name,
                    WalletModel.name,
                    WalletModel.id,
                )
            ).all()
        return tuple(
            SalaryWalletOption(
                wallet_id=wallet.id,
                wallet_name=wallet.name,
                financial_account_id=account.id,
                account_name=account.display_name,
                institution_code=account.institution_code,
                currency=account.currency,
                is_unallocated=wallet.purpose == WalletPurpose.UNALLOCATED.value,
            )
            for wallet, account in rows
            if holds_wallets(FinancialAccountType(account.account_type))
        )

    def category_options(self) -> tuple[BudgetCategory, ...]:
        """Return every budget category, deactivated ones included."""
        with self._session_factory() as session:
            models = session.scalars(
                select(BudgetCategoryModel).order_by(
                    BudgetCategoryModel.parent_id, BudgetCategoryModel.name
                )
            ).all()
        return tuple(budget_category_record(model) for model in models)

    def eligible_salary_events(
        self, template_id: int
    ) -> tuple[EligibleSalaryEventRow, ...]:
        """Return the posted deposits one template could actually distribute.

        Eligibility is decided from the shape of the posting, never from a
        merchant name, an Email subject, or a bank display name.
        """
        with self._session_factory() as session:
            template = session.get(SalaryPlanTemplateModel, template_id)
            if template is None:
                return ()
            wallet = session.scalar(
                select(WalletModel).where(
                    WalletModel.financial_account_id == template.source_account_id,
                    WalletModel.purpose == WalletPurpose.UNALLOCATED.value,
                    WalletModel.is_active.is_(True),
                )
            )
            if wallet is None:
                return ()
            used = {
                row[0]
                for row in session.execute(
                    select(SalaryAllocationRunModel.salary_event_id).where(
                        SalaryAllocationRunModel.status
                        != SalaryRunStatus.CANCELLED.value
                    )
                ).all()
            }
            rows = session.execute(
                select(FinancialEventModel, JournalEntryModel)
                .join(
                    JournalEntryModel,
                    JournalEntryModel.financial_event_id == FinancialEventModel.id,
                )
                .where(
                    FinancialEventModel.status == FinancialEventStatus.POSTED.value,
                    FinancialEventModel.event_type == FinancialEventType.DEPOSIT.value,
                    FinancialEventModel.currency == template.currency,
                )
                .order_by(
                    FinancialEventModel.occurred_at.desc(),
                    FinancialEventModel.id.desc(),
                )
            ).all()
            eligible: list[EligibleSalaryEventRow] = []
            for event_model, entry_model in rows:
                event = financial_event_record(event_model)
                problem = salary_event_problem(
                    event_type=event.event_type,
                    status=event.status,
                    currency=event.currency,
                    amount_minor=event.amount_minor,
                    expected_currency=template.currency,
                    source_account_id=template.source_account_id,
                    source_wallet_id=wallet.id,
                    entry_type=JournalEntryType(entry_model.entry_type),
                    postings=self._posting_views(session, entry_model.id),
                    already_used=event.id in used,
                )
                if problem is None:
                    eligible.append(
                        EligibleSalaryEventRow(
                            event=event,
                            entry_id=entry_model.id,
                            entry_effective_at=entry_model.effective_at,
                        )
                    )
        return tuple(eligible)

    def month(
        self, *, year: int, month: int, template_id: int | None, currency: str
    ) -> SalaryMonthView:
        """Return one month's plan, its live generation, and its history."""
        code = normalize_currency(currency)
        templates = self.templates()
        selected = self._selected_template(templates, template_id, code)
        with self._session_factory() as session:
            period = session.scalar(
                select(BudgetPeriodModel).where(
                    BudgetPeriodModel.year == year,
                    BudgetPeriodModel.month == month,
                    BudgetPeriodModel.currency
                    == (code if selected is None else selected.template.currency),
                )
            )
            generations: tuple[SalaryAllocationRun, ...] = ()
            live_id: int | None = None
            if period is not None and selected is not None:
                models = session.scalars(
                    select(SalaryAllocationRunModel)
                    .where(
                        SalaryAllocationRunModel.template_id == selected.template.id,
                        SalaryAllocationRunModel.budget_period_id == period.id,
                    )
                    .order_by(SalaryAllocationRunModel.generation)
                ).all()
                generations = tuple(salary_run_record(model) for model in models)
                live_id = next(
                    (
                        run.id
                        for run in generations
                        if run.status is not SalaryRunStatus.CANCELLED
                    ),
                    None,
                )
            cutoff_at = session.scalar(
                select(ImportBatchModel.cutoff_at)
                .where(ImportBatchModel.applied_at.is_not(None))
                .order_by(ImportBatchModel.applied_at.desc())
                .limit(1)
            )
        return SalaryMonthView(
            opening_cutoff_at=cutoff_at,
            year=year,
            month=month,
            currency=code if selected is None else selected.template.currency,
            templates=templates,
            selected=selected,
            live_run=None if live_id is None else self.run(live_id),
            generations=generations,
            eligible_events=(
                ()
                if selected is None
                else self.eligible_salary_events(selected.template.id)
            ),
            period_is_closed=(
                period is not None and period.status == BudgetPeriodStatus.CLOSED.value
            ),
        )

    def run(self, run_id: int) -> SalaryRunView | None:
        """Return one generation exactly as it was frozen."""
        with self._session_factory() as session:
            model = session.get(SalaryAllocationRunModel, run_id)
            if model is None:
                return None
            run = salary_run_record(model)
            items = tuple(
                SalaryRunItemView(
                    item=salary_run_item_record(item_model),
                    evidence=deserialize_item_evidence(
                        item_model.calculation_evidence_json
                    ),
                )
                for item_model in session.scalars(
                    select(SalaryAllocationRunItemModel)
                    .where(SalaryAllocationRunItemModel.run_id == run.id)
                    .order_by(
                        SalaryAllocationRunItemModel.display_order,
                        SalaryAllocationRunItemModel.id,
                    )
                ).all()
            )
            event_model = session.get(FinancialEventModel, run.salary_event_id)
            return SalaryRunView(
                run=run,
                items=items,
                evidence=deserialize_subscription_evidence(
                    run.subscription_evidence_json
                ),
                source=self._wallet_position(session, run.source_wallet_id),
                salary_event=(
                    None if event_model is None else financial_event_record(event_model)
                ),
            )

    @staticmethod
    def _selected_template(
        templates: tuple[SalaryTemplateRow, ...],
        template_id: int | None,
        currency: str,
    ) -> SalaryTemplateRow | None:
        """Pick the requested plan, or the only usable one in this currency."""
        if template_id is not None:
            return next(
                (row for row in templates if row.template.id == template_id), None
            )
        usable = tuple(
            row
            for row in templates
            if row.template.is_active and row.template.currency == currency
        )
        return usable[0] if len(usable) == 1 else None

    @staticmethod
    def _posting_views(
        session: Session, entry_id: int
    ) -> tuple[SalaryPostingView, ...]:
        rows = session.execute(
            select(JournalPostingModel, LedgerAccountModel)
            .join(
                LedgerAccountModel,
                LedgerAccountModel.id == JournalPostingModel.ledger_account_id,
            )
            .where(JournalPostingModel.journal_entry_id == entry_id)
            .order_by(JournalPostingModel.id)
        ).all()
        return tuple(
            SalaryPostingView(
                ledger_account_id=ledger.id,
                ledger_kind=LedgerAccountKind(ledger.kind),
                financial_account_id=ledger.financial_account_id,
                wallet_id=posting.wallet_id,
                amount_minor=posting.amount_minor,
                currency=posting.currency,
                budget_category_id=posting.budget_category_id,
            )
            for posting, ledger in rows
        )

    @staticmethod
    def _wallet_position(session: Session, wallet_id: int) -> SalaryWalletPosition:
        """Return the actual, reserved, and available amounts of one wallet.

        The three are deliberately separate numbers. Showing only one of them
        is what makes a plan look funded when the money is still promised
        elsewhere (ADR-B013).
        """
        row = session.execute(
            select(WalletModel, FinancialAccountModel)
            .join(
                FinancialAccountModel,
                FinancialAccountModel.id == WalletModel.financial_account_id,
            )
            .where(WalletModel.id == wallet_id)
        ).first()
        balance = int(
            session.execute(
                select(
                    func.coalesce(func.sum(JournalPostingModel.amount_minor), 0)
                ).where(JournalPostingModel.wallet_id == wallet_id)
            ).scalar_one()
        )
        committed = int(
            session.execute(
                select(
                    func.coalesce(
                        func.sum(
                            FundingCommitmentModel.amount_minor
                            - FundingCommitmentModel.released_minor
                        ),
                        0,
                    )
                ).where(
                    FundingCommitmentModel.source_wallet_id == wallet_id,
                    FundingCommitmentModel.status.in_(_UNFUNDED_COMMITMENT_STATUSES),
                )
            ).scalar_one()
        )
        waiting = int(
            session.execute(
                select(
                    func.coalesce(
                        func.sum(SalaryAllocationRunItemModel.amount_minor), 0
                    )
                )
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
            ).scalar_one()
        )
        wallet, account = (None, None) if row is None else (row[0], row[1])
        return SalaryWalletPosition(
            wallet_id=wallet_id,
            wallet_name="" if wallet is None else wallet.name,
            account_name="" if account is None else account.display_name,
            currency="" if account is None else account.currency,
            balance_minor=balance,
            reserved_minor=committed + waiting,
        )

    @staticmethod
    def _template_row(
        session: Session, model: SalaryPlanTemplateModel
    ) -> SalaryTemplateRow:
        account = session.get(FinancialAccountModel, model.source_account_id)
        wallet = session.scalar(
            select(WalletModel).where(
                WalletModel.financial_account_id == model.source_account_id,
                WalletModel.purpose == WalletPurpose.UNALLOCATED.value,
                WalletModel.is_active.is_(True),
            )
        )
        rows = session.execute(
            select(
                SalaryPlanTemplateItemModel,
                WalletModel.name,
                FinancialAccountModel.display_name,
                BudgetCategoryModel.name,
                FinancialAccountModel.display_order,
            )
            .join(
                WalletModel,
                WalletModel.id == SalaryPlanTemplateItemModel.destination_wallet_id,
            )
            .join(
                FinancialAccountModel,
                FinancialAccountModel.id == WalletModel.financial_account_id,
            )
            .outerjoin(
                BudgetCategoryModel,
                BudgetCategoryModel.id
                == SalaryPlanTemplateItemModel.budget_category_id,
            )
            .where(SalaryPlanTemplateItemModel.template_id == model.id)
            .order_by(
                SalaryPlanTemplateItemModel.display_order,
                SalaryPlanTemplateItemModel.id,
            )
        ).all()
        return SalaryTemplateRow(
            template=salary_template_record(model),
            source_account_name="" if account is None else account.display_name,
            source_institution_code=(
                "" if account is None else account.institution_code
            ),
            source_wallet_id=None if wallet is None else wallet.id,
            source_wallet_name=None if wallet is None else wallet.name,
            items=tuple(
                SalaryTemplateItemRow(
                    item=salary_template_item_record(item_model),
                    destination_wallet_name=wallet_name,
                    destination_account_name=account_name,
                    budget_category_name=category_name,
                    destination_account_order=account_order,
                )
                for (
                    item_model,
                    wallet_name,
                    account_name,
                    category_name,
                    account_order,
                ) in rows
            ),
        )

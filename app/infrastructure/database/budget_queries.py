"""Indexed SQLAlchemy read models for calculated ledger balances."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, aliased, sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from app.application.queries.budget import (
    UNCATEGORIZED_LABEL,
    UNPLANNED_LABEL_SUFFIX,
    BudgetMonthView,
    CategoryBudgetView,
    OpenCommitmentView,
    WalletFundingView,
)
from app.application.queries.money import CardConfigurationView, ReviewEventView
from app.domain.budget_enums import (
    CommitmentStatus,
    FinancialAccountType,
    FinancialEventStatus,
    LedgerAccountKind,
    SourceDiscrepancy,
    SourceLinkReason,
    WalletPurpose,
)
from app.domain.budget_models import (
    FinancialAccountBalance,
    WalletBalance,
    normalize_currency,
)
from app.domain.budget_rules import period_bounds
from app.infrastructure.database.allocation_scope import not_dedicated
from app.infrastructure.database.budget_models import (
    BudgetCategoryModel,
    BudgetPeriodModel,
    BudgetTargetModel,
    CardModel,
    FinancialAccountModel,
    FinancialEventModel,
    FinancialEventSourceLinkModel,
    FundingCommitmentModel,
    JournalEntryModel,
    JournalPostingModel,
    LedgerAccountModel,
    WalletModel,
)
from app.infrastructure.database.budget_repositories import (
    budget_period_record,
    card_record,
    financial_event_record,
    source_link_record,
)
from app.infrastructure.database.import_models import ImportBatchModel
from app.infrastructure.database.models import SenderRuleModel


def _after_applied_cutover() -> ColumnElement[bool]:
    """Return a SQL predicate excluding events represented by opening balances."""
    cutoff = (
        select(func.max(ImportBatchModel.cutoff_at))
        .where(ImportBatchModel.status == "applied")
        .scalar_subquery()
    )
    return or_(cutoff.is_(None), FinancialEventModel.occurred_at > cutoff)


class SqlAlchemyLedgerBalanceReadRepository:
    """Short-session balance reads derived only from posted journal lines."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def account_balances(self) -> tuple[FinancialAccountBalance, ...]:
        """Return active account balances with their wallet composition."""
        with self._session_factory() as session:
            rows = session.execute(
                select(
                    FinancialAccountModel.id,
                    FinancialAccountModel.institution_code,
                    FinancialAccountModel.display_name,
                    FinancialAccountModel.account_type,
                    FinancialAccountModel.currency,
                    LedgerAccountModel.code,
                    func.coalesce(
                        select(func.sum(JournalPostingModel.amount_minor))
                        .where(
                            JournalPostingModel.ledger_account_id
                            == LedgerAccountModel.id
                        )
                        .scalar_subquery(),
                        0,
                    ),
                )
                .join(
                    LedgerAccountModel,
                    LedgerAccountModel.financial_account_id == FinancialAccountModel.id,
                )
                .where(FinancialAccountModel.is_active.is_(True))
                # The same arrangement the sheet reads in (ADR-B088).
                .order_by(
                    FinancialAccountModel.display_order,
                    FinancialAccountModel.id,
                )
            ).all()
            wallets = self._wallet_balances(session)
        return tuple(
            FinancialAccountBalance(
                financial_account_id=row[0],
                institution_code=row[1],
                display_name=row[2],
                account_type=FinancialAccountType(row[3]),
                currency=row[4],
                ledger_account_code=row[5],
                balance_minor=int(row[6]),
                wallets=wallets.get(row[0], ()),
            )
            for row in rows
        )

    @staticmethod
    def _wallet_balances(
        session: Session,
    ) -> dict[int, tuple[WalletBalance, ...]]:
        rows = session.execute(
            select(
                WalletModel.financial_account_id,
                WalletModel.id,
                WalletModel.name,
                WalletModel.purpose,
                WalletModel.is_active,
                FinancialAccountModel.currency,
                func.coalesce(
                    select(func.sum(JournalPostingModel.amount_minor))
                    .where(JournalPostingModel.wallet_id == WalletModel.id)
                    .scalar_subquery(),
                    0,
                ),
            )
            .join(
                FinancialAccountModel,
                FinancialAccountModel.id == WalletModel.financial_account_id,
            )
            # The arrangement its owner stored, so this read and the accounts
            # sheet cannot disagree about the order of the same wallets
            # (ADR-B087).
            .order_by(
                WalletModel.financial_account_id,
                WalletModel.display_order,
                WalletModel.id,
            )
        ).all()
        grouped: dict[int, list[WalletBalance]] = {}
        for row in rows:
            grouped.setdefault(row[0], []).append(
                WalletBalance(
                    wallet_id=row[1],
                    name=row[2],
                    purpose=WalletPurpose(row[3]),
                    is_active=row[4],
                    currency=row[5],
                    balance_minor=int(row[6]),
                )
            )
        return {account_id: tuple(items) for account_id, items in grouped.items()}


def _lookup(names: dict[int, str], key: int | None) -> str | None:
    """Return one display name, tolerating an unset optional reference."""
    return None if key is None else names.get(key)


class SqlAlchemyMoneyReviewReadRepository:
    """Short-session reads for the local card and review pages."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def cards(self) -> tuple[CardConfigurationView, ...]:
        """Return card rows with resolved account and wallet display names."""
        with self._session_factory() as session:
            cards = session.scalars(
                select(CardModel).order_by(
                    CardModel.issuer_code, CardModel.last_four, CardModel.id
                )
            ).all()
            accounts = {
                row[0]: row[1]
                for row in session.execute(
                    select(FinancialAccountModel.id, FinancialAccountModel.display_name)
                ).all()
            }
            wallets = {
                row[0]: row[1]
                for row in session.execute(
                    select(WalletModel.id, WalletModel.name)
                ).all()
            }
            issuer_names = {
                (row[0], row[1]): row[2]
                for row in session.execute(
                    select(
                        SenderRuleModel.gmail_account_id,
                        SenderRuleModel.institution_key,
                        SenderRuleModel.institution_name,
                    )
                ).all()
            }
        return tuple(
            CardConfigurationView(
                card=card_record(model),
                issuer_name=issuer_names.get(
                    (model.gmail_account_id, model.issuer_code), model.issuer_code
                ),
                liability_account_name=_lookup(accounts, model.liability_account_id),
                settlement_account_name=_lookup(accounts, model.settlement_account_id),
                default_funding_wallet_name=_lookup(
                    wallets, model.default_funding_wallet_id
                ),
            )
            for model in cards
        )

    def events_needing_review(self) -> tuple[ReviewEventView, ...]:
        """Return events awaiting allocation with their stable reason codes."""
        with self._session_factory() as session:
            events = session.scalars(
                select(FinancialEventModel)
                .where(
                    FinancialEventModel.status
                    == FinancialEventStatus.NEEDS_ALLOCATION.value,
                    _after_applied_cutover(),
                )
                .order_by(
                    FinancialEventModel.occurred_at.desc(),
                    FinancialEventModel.id.desc(),
                )
            ).all()
            links = session.scalars(
                select(FinancialEventSourceLinkModel).order_by(
                    FinancialEventSourceLinkModel.id
                )
            ).all()
            card_models = session.scalars(select(CardModel)).all()
            issuer_names = {
                (row[0], row[1]): row[2]
                for row in session.execute(
                    select(
                        SenderRuleModel.gmail_account_id,
                        SenderRuleModel.institution_key,
                        SenderRuleModel.institution_name,
                    )
                ).all()
            }
            cards = {}
            card_default_wallets: dict[int, int | None] = {}
            for card_model in card_models:
                issuer_name = issuer_names.get(
                    (card_model.gmail_account_id, card_model.issuer_code),
                    card_model.issuer_code,
                )
                cards[card_model.id] = f"{issuer_name} ****{card_model.last_four}"
                card_default_wallets[card_model.id] = (
                    card_model.default_funding_wallet_id
                )
        by_event: dict[int, list[FinancialEventSourceLinkModel]] = {}
        for link in links:
            by_event.setdefault(link.financial_event_id, []).append(link)
        views: list[ReviewEventView] = []
        for model in events:
            reasons: list[SourceLinkReason] = []
            discrepancies: list[SourceDiscrepancy] = []
            for link in by_event.get(model.id, ()):
                record = source_link_record(link)
                reasons.extend(record.reasons)
                discrepancies.extend(record.discrepancies)
            views.append(
                ReviewEventView(
                    event=financial_event_record(model),
                    card_display=(
                        cards.get(model.card_id) if model.card_id is not None else None
                    ),
                    reasons=tuple(dict.fromkeys(reasons)),
                    discrepancies=tuple(dict.fromkeys(discrepancies)),
                    default_funding_wallet_id=(
                        card_default_wallets.get(model.card_id)
                        if model.card_id is not None
                        else None
                    ),
                )
            )
        return tuple(views)


class SqlAlchemyBudgetReadRepository:
    """Short-session reads assembling one month of budget in one currency."""

    def __init__(
        self, session_factory: sessionmaker[Session], *, local_timezone: str
    ) -> None:
        self._session_factory = session_factory
        self._timezone = ZoneInfo(local_timezone)

    def month(self, *, year: int, month: int, currency: str) -> BudgetMonthView:
        """Return targets, spending, funding, and outstanding work."""
        code = normalize_currency(currency)
        start, end = period_bounds(year, month, self._timezone)
        with self._session_factory() as session:
            model = session.scalars(
                select(BudgetPeriodModel).where(
                    BudgetPeriodModel.year == year,
                    BudgetPeriodModel.month == month,
                    BudgetPeriodModel.currency == code,
                )
            ).one_or_none()
            spend = self._net_spend(session, start=start, end=end, currency=code)
            categories = self._categories(
                session,
                period_id=model.id if model is not None else None,
                spend=spend,
                currency=code,
            )
            return BudgetMonthView(
                period=budget_period_record(model) if model is not None else None,
                label=f"{year:04d}-{month:02d}",
                currency=code,
                categories=categories,
                wallets=self._wallets(session, currency=code),
                open_commitments=self._open_commitments(session, currency=code),
                events_needing_allocation=self._needing_allocation(session),
            )

    @staticmethod
    def _net_spend(
        session: Session, *, start: datetime, end: datetime, currency: str
    ) -> dict[int | None, int]:
        """Return net spending per category, with `None` for uncategorized.

        Only expense ledger lines are counted, so an asset or liability leg of
        the same entry is never mistaken for spending.
        """
        original = aliased(JournalEntryModel)
        event_id = func.coalesce(
            JournalEntryModel.financial_event_id, original.financial_event_id
        )
        excluded_as_dedicated = not_dedicated(event_id)
        rows = session.execute(
            select(
                JournalPostingModel.budget_category_id,
                func.sum(JournalPostingModel.amount_minor),
            )
            .join(
                JournalEntryModel,
                JournalEntryModel.id == JournalPostingModel.journal_entry_id,
            )
            .join(
                original,
                original.id == JournalEntryModel.reversal_of_id,
                isouter=True,
            )
            .join(
                LedgerAccountModel,
                LedgerAccountModel.id == JournalPostingModel.ledger_account_id,
            )
            .where(
                LedgerAccountModel.kind == LedgerAccountKind.EXPENSE.value,
                JournalPostingModel.currency == currency,
                JournalEntryModel.effective_at >= start,
                JournalEntryModel.effective_at < end,
                excluded_as_dedicated,
            )
            .group_by(JournalPostingModel.budget_category_id)
        ).all()
        return {
            (int(row[0]) if row[0] is not None else None): int(row[1] or 0)
            for row in rows
        }

    @staticmethod
    def _categories(
        session: Session,
        *,
        period_id: int | None,
        spend: dict[int | None, int],
        currency: str,
    ) -> tuple[CategoryBudgetView, ...]:
        """Return one row per target, then one per uncovered kind of spending.

        Spending is reported whether or not a target exists, and whether or not
        a category was chosen. A month with no budget period at all still shows
        what was spent, so the page can never read as zero while the ledger
        says otherwise.
        """
        rows = (
            session.execute(
                select(
                    BudgetTargetModel.budget_category_id,
                    BudgetCategoryModel.name,
                    BudgetTargetModel.target_minor,
                    BudgetTargetModel.buffer_minor,
                    BudgetTargetModel.funding_wallet_id,
                    WalletModel.name,
                    BudgetTargetModel.id,
                    BudgetTargetModel.version,
                    BudgetTargetModel.month_end_destination_wallet_id,
                )
                .join(
                    BudgetCategoryModel,
                    BudgetCategoryModel.id == BudgetTargetModel.budget_category_id,
                )
                .join(
                    WalletModel, WalletModel.id == BudgetTargetModel.funding_wallet_id
                )
                .where(BudgetTargetModel.budget_period_id == period_id)
                .order_by(BudgetCategoryModel.name, BudgetTargetModel.id)
            ).all()
            if period_id is not None
            else []
        )
        planned = tuple(
            CategoryBudgetView(
                budget_category_id=row[0],
                name=row[1],
                currency=currency,
                target_minor=int(row[2]),
                buffer_minor=int(row[3]),
                net_spend_minor=spend.get(row[0], 0),
                funding_wallet_id=row[4],
                funding_wallet_name=row[5],
                target_id=row[6],
                target_version=row[7],
                month_end_destination_wallet_id=row[8],
            )
            for row in rows
        )
        covered = {item.budget_category_id for item in planned}
        uncovered = sorted(
            key for key in spend if key not in covered and key is not None
        )
        names = (
            {
                model.id: model.name
                for model in session.scalars(
                    select(BudgetCategoryModel).where(
                        BudgetCategoryModel.id.in_(uncovered)
                    )
                ).all()
            }
            if uncovered
            else {}
        )
        unplanned = tuple(
            CategoryBudgetView(
                budget_category_id=category_id,
                name=f"{names.get(category_id, str(category_id))}"
                f"{UNPLANNED_LABEL_SUFFIX}",
                currency=currency,
                target_minor=0,
                buffer_minor=0,
                net_spend_minor=spend[category_id],
                funding_wallet_id=None,
                funding_wallet_name=None,
            )
            for category_id in uncovered
        )
        uncategorized = (
            (
                CategoryBudgetView(
                    budget_category_id=None,
                    name=UNCATEGORIZED_LABEL,
                    currency=currency,
                    target_minor=0,
                    buffer_minor=0,
                    net_spend_minor=spend[None],
                    funding_wallet_id=None,
                    funding_wallet_name=None,
                ),
            )
            if None in spend
            else ()
        )
        return planned + unplanned + uncategorized

    @staticmethod
    def _wallets(session: Session, *, currency: str) -> tuple[WalletFundingView, ...]:
        rows = session.execute(
            select(
                WalletModel.id,
                WalletModel.name,
                FinancialAccountModel.display_name,
                FinancialAccountModel.currency,
                func.coalesce(
                    select(func.sum(JournalPostingModel.amount_minor))
                    .where(JournalPostingModel.wallet_id == WalletModel.id)
                    .scalar_subquery(),
                    0,
                ),
                func.coalesce(
                    select(
                        func.sum(
                            FundingCommitmentModel.amount_minor
                            - FundingCommitmentModel.released_minor
                        )
                    )
                    .where(
                        FundingCommitmentModel.source_wallet_id == WalletModel.id,
                        FundingCommitmentModel.status.in_(_OPEN_COMMITMENT_STATUSES),
                    )
                    .scalar_subquery(),
                    0,
                ),
            )
            .join(
                FinancialAccountModel,
                FinancialAccountModel.id == WalletModel.financial_account_id,
            )
            .where(
                WalletModel.is_active.is_(True),
                FinancialAccountModel.currency == currency,
            )
            .order_by(FinancialAccountModel.display_name, WalletModel.name)
        ).all()
        return tuple(
            WalletFundingView(
                wallet_id=row[0],
                name=row[1],
                financial_account_name=row[2],
                currency=row[3],
                balance_minor=int(row[4]),
                reserved_minor=int(row[5]),
            )
            for row in rows
        )

    @staticmethod
    def _open_commitments(
        session: Session, *, currency: str
    ) -> tuple[OpenCommitmentView, ...]:
        rows = session.execute(
            select(
                FundingCommitmentModel.id,
                FundingCommitmentModel.financial_event_id,
                FinancialEventModel.merchant,
                FinancialEventModel.occurred_at,
                FundingCommitmentModel.currency,
                FundingCommitmentModel.amount_minor
                - FundingCommitmentModel.released_minor,
                WalletModel.name,
                FinancialAccountModel.display_name,
                FundingCommitmentModel.fulfillment_mode,
            )
            .join(
                FinancialEventModel,
                FinancialEventModel.id == FundingCommitmentModel.financial_event_id,
            )
            .join(
                WalletModel, WalletModel.id == FundingCommitmentModel.source_wallet_id
            )
            .join(
                FinancialAccountModel,
                FinancialAccountModel.id
                == FundingCommitmentModel.settlement_account_id,
            )
            .where(
                FundingCommitmentModel.currency == currency,
                FundingCommitmentModel.status.in_(_OPEN_COMMITMENT_STATUSES),
                FundingCommitmentModel.amount_minor
                > FundingCommitmentModel.released_minor,
            )
            .order_by(
                FinancialEventModel.occurred_at.desc(), FundingCommitmentModel.id.desc()
            )
        ).all()
        return tuple(
            OpenCommitmentView(
                commitment_id=row[0],
                financial_event_id=row[1],
                merchant=row[2],
                occurred_at=row[3].isoformat(),
                currency=row[4],
                outstanding_minor=int(row[5]),
                source_wallet_name=row[6],
                settlement_account_name=row[7],
                fulfillment_mode=row[8],
            )
            for row in rows
        )

    @staticmethod
    def _needing_allocation(session: Session) -> int:
        total = session.execute(
            select(func.count(FinancialEventModel.id)).where(
                FinancialEventModel.status
                == FinancialEventStatus.NEEDS_ALLOCATION.value,
                _after_applied_cutover(),
            )
        ).scalar_one()
        return int(total)


_OPEN_COMMITMENT_STATUSES = (
    CommitmentStatus.OPEN.value,
    CommitmentStatus.PARTIALLY_SCHEDULED.value,
    CommitmentStatus.SCHEDULED.value,
)

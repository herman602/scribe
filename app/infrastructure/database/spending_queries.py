"""Indexed SQLAlchemy read models for daily spending and reconciliation.

Every list is bounded and every aggregate stays inside one currency. Amounts
come from posted expense ledger lines, so a refund nets out by construction, a
reversal subtracts at its own effective instant, and a multi-category purchase
contributes exactly its parts (ADR-B064).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime

from sqlalchemy import Select, case, func, select
from sqlalchemy.orm import Session, aliased, sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from app.application.queries.spending import (
    CANDIDATE_AMOUNT_MATCHES,
    CANDIDATE_RECORDED_LATE,
    AccountActivityRow,
    AccountDetailView,
    AccountRow,
    AccountSnapshotRow,
    AccountWalletRow,
    DailyActivityRow,
    DailyCategoryShare,
    DailyTotal,
    DifferenceCandidate,
    DifferenceExplanation,
    EventDetailView,
    EventHistoryRow,
    LegacyDailyActivityRow,
    ReconcileAccountRow,
    ReconcileView,
    SourceLinkReviewView,
    SourceLinkRow,
)
from app.domain.budget_enums import (
    FinancialEventStatus,
    FinancialEventType,
    JournalEntryType,
    LedgerAccountKind,
    PaymentMethod,
    SourceLinkStatus,
    WalletPurpose,
)
from app.domain.budget_models import JournalEntry
from app.domain.budget_rules import holds_wallets
from app.infrastructure.database.allocation_scope import not_dedicated
from app.infrastructure.database.budget_models import (
    BalanceSnapshotModel,
    BudgetCategoryModel,
    CardModel,
    DedicatedWalletAllocationModel,
    EventAllocationModel,
    FinancialAccountModel,
    FinancialEventModel,
    FinancialEventSourceLinkModel,
    JournalEntryModel,
    JournalPostingModel,
    LedgerAccountModel,
    WalletModel,
)
from app.infrastructure.database.budget_repositories import (
    SqlAlchemyBudgetLedgerRepository,
    balance_snapshot_record,
    financial_account_record,
    financial_event_record,
    journal_entry_record,
    source_link_record,
)
from app.infrastructure.database.import_models import LegacyDailyActivityModel
from app.infrastructure.database.models import SenderRuleModel, TransactionModel

UNCATEGORIZED_LABEL = "未分類"


def _card_display(issuer_code: str | None, last_four: str | None) -> str | None:
    """Return the only card identity Scribe stores: issuer plus last four."""
    if issuer_code is None or last_four is None:
        return None
    return f"{issuer_code} ••{last_four}"


def _not_a_cancelled_pair() -> tuple[ColumnElement[bool], ...]:
    """Return the predicates that leave a cancellation and its target out.

    A cancellation and the entry it cancelled add up to nothing, so neither
    belongs on a list answering "what was spent today". `daily_totals` is a
    separate query and still counts both, which is why hiding the pair cannot
    make the list disagree with the total. A refund is not a cancellation and
    still appears.
    """
    canceller = aliased(JournalEntryModel)
    return (
        JournalEntryModel.entry_type != JournalEntryType.REVERSAL.value,
        JournalEntryModel.id.not_in(
            select(canceller.reversal_of_id).where(
                canceller.reversal_of_id.is_not(None)
            )
        ),
    )


class SqlAlchemySpendingReadRepository:
    """Short-session reads for the daily, account, and reconciliation pages."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    # ----- daily activity -------------------------------------------------

    @staticmethod
    def _spending_entries(
        *, start: datetime, end: datetime, payment_methods: tuple[PaymentMethod, ...]
    ) -> Select[tuple[int]]:
        """Return the entry ids that are spending inside one local day.

        A reversal has no event of its own, so the event is reached through the
        entry it cancels. Only entries with at least one expense ledger line
        qualify, which is exactly what keeps a transfer or a deposit out of a
        spending total without naming those types anywhere.

        This is what the list is paged against, so it applies the same
        cancellation-pair rule `daily_rows` does. It did not, and a day
        holding a cancellation or a redone allocation then reported more rows
        than the list could ever show.
        """
        original = aliased(JournalEntryModel)
        return (
            select(JournalEntryModel.id)
            .join(
                original,
                original.id == JournalEntryModel.reversal_of_id,
                isouter=True,
            )
            .join(
                FinancialEventModel,
                FinancialEventModel.id
                == func.coalesce(
                    JournalEntryModel.financial_event_id,
                    original.financial_event_id,
                ),
            )
            .join(
                JournalPostingModel,
                JournalPostingModel.journal_entry_id == JournalEntryModel.id,
            )
            .join(
                LedgerAccountModel,
                LedgerAccountModel.id == JournalPostingModel.ledger_account_id,
            )
            .where(
                JournalEntryModel.effective_at >= start,
                JournalEntryModel.effective_at < end,
                LedgerAccountModel.kind == LedgerAccountKind.EXPENSE.value,
                FinancialEventModel.payment_method.in_(
                    [method.value for method in payment_methods]
                ),
                not_dedicated(
                    func.coalesce(
                        JournalEntryModel.financial_event_id,
                        original.financial_event_id,
                    )
                ),
                *_not_a_cancelled_pair(),
            )
            .group_by(JournalEntryModel.id)
        )

    def daily_count(
        self,
        *,
        start: datetime,
        end: datetime,
        payment_methods: tuple[PaymentMethod, ...],
    ) -> int:
        """Return how many spending rows one local day holds."""
        with self._session_factory() as session:
            entries = self._spending_entries(
                start=start, end=end, payment_methods=payment_methods
            ).subquery()
            total = session.execute(
                select(func.count()).select_from(entries)
            ).scalar_one()
        return int(total)

    def daily_rows(
        self,
        *,
        start: datetime,
        end: datetime,
        payment_methods: tuple[PaymentMethod, ...],
        limit: int,
        offset: int,
    ) -> tuple[DailyActivityRow, ...]:
        """Return one bounded page of a local day, newest entry first."""
        original = aliased(JournalEntryModel)
        with self._session_factory() as session:
            rows = session.execute(
                select(
                    JournalEntryModel.id,
                    JournalEntryModel.effective_at,
                    JournalEntryModel.entry_type,
                    FinancialEventModel.id,
                    FinancialEventModel.event_type,
                    FinancialEventModel.status,
                    FinancialEventModel.payment_method,
                    FinancialEventModel.merchant,
                    FinancialEventModel.note,
                    FinancialEventModel.currency,
                    CardModel.issuer_code,
                    CardModel.last_four,
                    func.coalesce(func.sum(JournalPostingModel.amount_minor), 0),
                )
                .join(
                    original,
                    original.id == JournalEntryModel.reversal_of_id,
                    isouter=True,
                )
                .join(
                    FinancialEventModel,
                    FinancialEventModel.id
                    == func.coalesce(
                        JournalEntryModel.financial_event_id,
                        original.financial_event_id,
                    ),
                )
                .join(
                    CardModel, CardModel.id == FinancialEventModel.card_id, isouter=True
                )
                .join(
                    JournalPostingModel,
                    JournalPostingModel.journal_entry_id == JournalEntryModel.id,
                )
                .join(
                    LedgerAccountModel,
                    LedgerAccountModel.id == JournalPostingModel.ledger_account_id,
                )
                .where(
                    JournalEntryModel.effective_at >= start,
                    JournalEntryModel.effective_at < end,
                    LedgerAccountModel.kind == LedgerAccountKind.EXPENSE.value,
                    FinancialEventModel.payment_method.in_(
                        [method.value for method in payment_methods]
                    ),
                    not_dedicated(
                        func.coalesce(
                            JournalEntryModel.financial_event_id,
                            original.financial_event_id,
                        )
                    ),
                    *_not_a_cancelled_pair(),
                )
                .group_by(JournalEntryModel.id)
                # Newest first, and a stable tie-break so two entries at the
                # same instant never swap places between two page loads.
                .order_by(
                    JournalEntryModel.effective_at.desc(),
                    JournalEntryModel.id.desc(),
                )
                .limit(limit)
                .offset(offset)
            ).all()
            entry_ids = [int(row[0]) for row in rows]
            categories = self._categories_for(session, entry_ids)
            wallets = self._wallets_for(session, entry_ids)
        return tuple(
            DailyActivityRow(
                journal_entry_id=int(row[0]),
                effective_at=row[1],
                entry_type=JournalEntryType(row[2]),
                financial_event_id=int(row[3]),
                event_type=FinancialEventType(row[4]),
                event_status=FinancialEventStatus(row[5]),
                payment_method=PaymentMethod(row[6]),
                merchant=row[7],
                note=row[8],
                currency=row[9],
                card_display=_card_display(row[10], row[11]),
                amount_minor=int(row[12]),
                wallet_display=wallets.get(int(row[0])),
                categories=categories.get(int(row[0]), ()),
            )
            for row in rows
        )

    def daily_totals(self, *, start: datetime, end: datetime) -> tuple[DailyTotal, ...]:
        """Return one day's net spending per instrument and per currency.

        Refunds and reversals are negative expense lines, so they subtract here
        without any special case. Currencies are grouped, never summed
        together: this application has no exchange rate and must not invent one.
        """
        original = aliased(JournalEntryModel)
        with self._session_factory() as session:
            rows = session.execute(
                select(
                    FinancialEventModel.payment_method,
                    JournalPostingModel.currency,
                    func.coalesce(func.sum(JournalPostingModel.amount_minor), 0),
                )
                .join(
                    original,
                    original.id == JournalEntryModel.reversal_of_id,
                    isouter=True,
                )
                .select_from(JournalEntryModel)
                .join(
                    FinancialEventModel,
                    FinancialEventModel.id
                    == func.coalesce(
                        JournalEntryModel.financial_event_id,
                        original.financial_event_id,
                    ),
                )
                .join(
                    JournalPostingModel,
                    JournalPostingModel.journal_entry_id == JournalEntryModel.id,
                )
                .join(
                    LedgerAccountModel,
                    LedgerAccountModel.id == JournalPostingModel.ledger_account_id,
                )
                .where(
                    JournalEntryModel.effective_at >= start,
                    JournalEntryModel.effective_at < end,
                    LedgerAccountModel.kind == LedgerAccountKind.EXPENSE.value,
                    FinancialEventModel.payment_method.in_(
                        [
                            PaymentMethod.CASH.value,
                            PaymentMethod.CREDIT_CARD.value,
                        ]
                    ),
                    not_dedicated(
                        func.coalesce(
                            JournalEntryModel.financial_event_id,
                            original.financial_event_id,
                        )
                    ),
                )
                .group_by(
                    FinancialEventModel.payment_method, JournalPostingModel.currency
                )
                .order_by(
                    FinancialEventModel.payment_method, JournalPostingModel.currency
                )
            ).all()
        return tuple(
            DailyTotal(
                payment_method=PaymentMethod(row[0]),
                currency=row[1],
                amount_minor=int(row[2]),
            )
            for row in rows
        )

    def legacy_daily_rows(
        self,
        *,
        day: date,
        payment_methods: tuple[PaymentMethod, ...],
        limit: int,
    ) -> tuple[LegacyDailyActivityRow, ...]:
        """Return bounded pre-opening history for one exact local date."""
        with self._session_factory() as session:
            rows = session.scalars(
                select(LegacyDailyActivityModel)
                .where(
                    LegacyDailyActivityModel.activity_date == day,
                    LegacyDailyActivityModel.payment_method.in_(
                        [method.value for method in payment_methods]
                    ),
                )
                .order_by(
                    LegacyDailyActivityModel.sequence.desc(),
                    LegacyDailyActivityModel.id.desc(),
                )
                .limit(limit)
            ).all()
        return tuple(
            LegacyDailyActivityRow(
                id=row.id,
                activity_date=row.activity_date,
                sequence=row.sequence,
                payment_method=PaymentMethod(row.payment_method),
                amount_minor=row.amount_minor,
                currency=row.currency,
                note=row.note,
            )
            for row in rows
        )

    @staticmethod
    def _categories_for(
        session: Session, entry_ids: Sequence[int]
    ) -> dict[int, tuple[DailyCategoryShare, ...]]:
        """Return each entry's category split, uncategorized lines included."""
        if not entry_ids:
            return {}
        rows = session.execute(
            select(
                JournalPostingModel.journal_entry_id,
                JournalPostingModel.budget_category_id,
                BudgetCategoryModel.name,
                func.sum(JournalPostingModel.amount_minor),
            )
            .join(
                LedgerAccountModel,
                LedgerAccountModel.id == JournalPostingModel.ledger_account_id,
            )
            .join(
                BudgetCategoryModel,
                BudgetCategoryModel.id == JournalPostingModel.budget_category_id,
                isouter=True,
            )
            .where(
                JournalPostingModel.journal_entry_id.in_(entry_ids),
                LedgerAccountModel.kind == LedgerAccountKind.EXPENSE.value,
            )
            .group_by(
                JournalPostingModel.journal_entry_id,
                JournalPostingModel.budget_category_id,
                BudgetCategoryModel.name,
            )
            .order_by(JournalPostingModel.journal_entry_id)
        ).all()
        grouped: dict[int, list[DailyCategoryShare]] = {}
        for row in rows:
            grouped.setdefault(int(row[0]), []).append(
                DailyCategoryShare(
                    budget_category_id=None if row[1] is None else int(row[1]),
                    name=row[2] or UNCATEGORIZED_LABEL,
                    amount_minor=int(row[3]),
                )
            )
        return {entry_id: tuple(items) for entry_id, items in grouped.items()}

    @staticmethod
    def _wallets_for(session: Session, entry_ids: Sequence[int]) -> dict[int, str]:
        """Return the wallet and account a cash entry actually moved."""
        if not entry_ids:
            return {}
        rows = session.execute(
            select(
                JournalPostingModel.journal_entry_id,
                WalletModel.name,
                FinancialAccountModel.display_name,
            )
            .join(WalletModel, WalletModel.id == JournalPostingModel.wallet_id)
            .join(
                FinancialAccountModel,
                FinancialAccountModel.id == WalletModel.financial_account_id,
            )
            .where(JournalPostingModel.journal_entry_id.in_(entry_ids))
            .order_by(JournalPostingModel.journal_entry_id, JournalPostingModel.id)
        ).all()
        displays: dict[int, str] = {}
        for row in rows:
            displays.setdefault(int(row[0]), f"{row[2]} · {row[1]}")
        return displays

    # ----- accounts -------------------------------------------------------

    def accounts(self) -> tuple[AccountRow, ...]:
        """Return every active account, its wallets, and its latest evidence."""
        with self._session_factory() as session:
            return tuple(self._account_rows(session))

    def _account_rows(self, session: Session) -> list[AccountRow]:
        ledger = SqlAlchemyBudgetLedgerRepository(session)
        accounts = session.scalars(
            select(FinancialAccountModel)
            .where(FinancialAccountModel.is_active.is_(True))
            # The arrangement its owner chose (ADR-B088); institution code and
            # display name describe how a bank is spelled, not where its column
            # belongs.
            .order_by(
                FinancialAccountModel.display_order,
                FinancialAccountModel.id,
            )
        ).all()
        rows: list[AccountRow] = []
        for account in accounts:
            wallets = session.scalars(
                select(WalletModel)
                .where(
                    WalletModel.financial_account_id == account.id,
                    WalletModel.is_active.is_(True),
                )
                # The arrangement its owner chose, not name order: a sheet
                # column is arranged deliberately, and sorting Chinese names by
                # code point would scramble it (ADR-B085, ADR-B087).
                .order_by(WalletModel.display_order, WalletModel.id)
            ).all()
            balance = session.execute(
                select(func.coalesce(func.sum(JournalPostingModel.amount_minor), 0))
                .join(
                    LedgerAccountModel,
                    LedgerAccountModel.id == JournalPostingModel.ledger_account_id,
                )
                .where(LedgerAccountModel.financial_account_id == account.id)
            ).scalar_one()
            rows.append(
                AccountRow(
                    account=financial_account_record(account),
                    balance_minor=int(balance),
                    wallets=tuple(
                        AccountWalletRow(
                            wallet_id=wallet.id,
                            name=wallet.name,
                            currency=account.currency,
                            balance_minor=ledger.wallet_balance(wallet.id),
                            reserved_minor=ledger.reserved_minor(wallet.id),
                            purpose=WalletPurpose(wallet.purpose),
                            version=wallet.version,
                        )
                        for wallet in wallets
                    ),
                    latest_snapshot=self._latest_snapshot(session, account.id),
                )
            )
        return rows

    def account_detail(
        self, account_id: int, *, activity_limit: int, snapshot_limit: int
    ) -> AccountDetailView | None:
        """Return one account's position, evidence, and recent movements."""
        with self._session_factory() as session:
            model = session.get(FinancialAccountModel, account_id)
            if model is None:
                return None
            row = next(
                (
                    candidate
                    for candidate in self._account_rows(session)
                    if candidate.account.id == account_id
                ),
                None,
            )
            if row is None:
                return None
            snapshots = tuple(
                self._snapshot_row(session, snapshot)
                for snapshot in session.scalars(
                    select(BalanceSnapshotModel)
                    .where(BalanceSnapshotModel.financial_account_id == account_id)
                    .order_by(
                        BalanceSnapshotModel.observed_at.desc(),
                        BalanceSnapshotModel.id.desc(),
                    )
                    .limit(snapshot_limit)
                ).all()
            )
            activity = self._account_activity(session, account_id, limit=activity_limit)
        return AccountDetailView(row=row, snapshots=snapshots, activity=activity)

    @staticmethod
    def _account_activity(
        session: Session, account_id: int, *, limit: int
    ) -> tuple[AccountActivityRow, ...]:
        original = aliased(JournalEntryModel)
        rows = session.execute(
            select(
                JournalEntryModel.id,
                JournalEntryModel.effective_at,
                JournalEntryModel.entry_type,
                JournalEntryModel.description,
                func.sum(JournalPostingModel.amount_minor),
                JournalPostingModel.currency,
                func.group_concat(func.distinct(WalletModel.name)),
                func.coalesce(
                    JournalEntryModel.financial_event_id,
                    original.financial_event_id,
                ),
            )
            .join(
                JournalPostingModel,
                JournalPostingModel.journal_entry_id == JournalEntryModel.id,
            )
            .join(
                LedgerAccountModel,
                LedgerAccountModel.id == JournalPostingModel.ledger_account_id,
            )
            .join(
                original,
                original.id == JournalEntryModel.reversal_of_id,
                isouter=True,
            )
            .join(
                WalletModel,
                WalletModel.id == JournalPostingModel.wallet_id,
                isouter=True,
            )
            .where(LedgerAccountModel.financial_account_id == account_id)
            .group_by(JournalEntryModel.id, JournalPostingModel.currency)
            .order_by(
                JournalEntryModel.effective_at.desc(), JournalEntryModel.id.desc()
            )
            .limit(limit)
        ).all()
        return tuple(
            AccountActivityRow(
                journal_entry_id=int(row[0]),
                effective_at=row[1],
                entry_type=JournalEntryType(row[2]),
                description=row[3],
                amount_minor=int(row[4]),
                currency=row[5],
                wallet_display=row[6],
                financial_event_id=None if row[7] is None else int(row[7]),
            )
            for row in rows
        )

    def _latest_snapshot(
        self, session: Session, account_id: int
    ) -> AccountSnapshotRow | None:
        model = session.scalars(
            select(BalanceSnapshotModel)
            .where(BalanceSnapshotModel.financial_account_id == account_id)
            .order_by(
                BalanceSnapshotModel.observed_at.desc(),
                BalanceSnapshotModel.id.desc(),
            )
            .limit(1)
        ).first()
        return None if model is None else self._snapshot_row(session, model)

    @staticmethod
    def _snapshot_row(
        session: Session,
        model: BalanceSnapshotModel,
        *,
        account: FinancialAccountModel | None = None,
    ) -> AccountSnapshotRow:
        """Compare one observation with the ledger *as of that instant*.

        Using the current balance instead would blame today's spending for a
        gap that was already there yesterday (ADR-B067).
        """
        balance = session.execute(
            select(func.coalesce(func.sum(JournalPostingModel.amount_minor), 0))
            .join(
                JournalEntryModel,
                JournalEntryModel.id == JournalPostingModel.journal_entry_id,
            )
            .join(
                LedgerAccountModel,
                LedgerAccountModel.id == JournalPostingModel.ledger_account_id,
            )
            .where(
                LedgerAccountModel.financial_account_id == model.financial_account_id,
                JournalEntryModel.effective_at <= model.observed_at,
            )
        ).scalar_one()
        owner = account or session.get(
            FinancialAccountModel, model.financial_account_id
        )
        return AccountSnapshotRow(
            snapshot=balance_snapshot_record(model),
            ledger_balance_minor=int(balance),
            currency="" if owner is None else owner.currency,
            account_display_name="" if owner is None else owner.display_name,
        )

    def explain_difference(self, account_id: int) -> DifferenceExplanation:
        """Return the newest difference for one account and where to look.

        Two shapes are offered. An entry whose amount equals the gap is what
        one missing or one duplicated posting looks like. An entry effective
        before the observation but recorded after it is subtler: the
        comparison is made as of the observed instant (ADR-B067), so such an
        entry changes a difference that was zero when it was written down.
        """
        with self._session_factory() as session:
            model = session.scalars(
                select(BalanceSnapshotModel)
                .where(BalanceSnapshotModel.financial_account_id == account_id)
                .order_by(
                    BalanceSnapshotModel.observed_at.desc(),
                    BalanceSnapshotModel.id.desc(),
                )
                .limit(1)
            ).first()
            if model is None:
                return DifferenceExplanation(snapshot=None, candidates=())
            row = self._snapshot_row(session, model)
            if row.difference_minor == 0:
                return DifferenceExplanation(snapshot=row, candidates=())

            gap = abs(row.difference_minor)
            entries = session.execute(
                select(
                    JournalEntryModel.id,
                    JournalEntryModel.effective_at,
                    JournalEntryModel.recorded_at,
                    JournalEntryModel.description,
                    JournalEntryModel.financial_event_id,
                    FinancialEventModel.merchant,
                    func.coalesce(func.sum(JournalPostingModel.amount_minor), 0),
                )
                .join(
                    JournalPostingModel,
                    JournalPostingModel.journal_entry_id == JournalEntryModel.id,
                )
                .join(
                    LedgerAccountModel,
                    LedgerAccountModel.id == JournalPostingModel.ledger_account_id,
                )
                .join(
                    FinancialEventModel,
                    FinancialEventModel.id == JournalEntryModel.financial_event_id,
                    isouter=True,
                )
                .where(LedgerAccountModel.financial_account_id == account_id)
                .group_by(JournalEntryModel.id)
                .order_by(JournalEntryModel.recorded_at.desc())
            ).all()

            candidates: list[DifferenceCandidate] = []
            for (
                entry_id,
                effective_at,
                recorded_at,
                description,
                event_id,
                merchant,
                amount,
            ) in entries:
                reasons: list[str] = []
                if abs(int(amount)) == gap:
                    reasons.append(CANDIDATE_AMOUNT_MATCHES)
                if (
                    effective_at <= model.observed_at
                    and recorded_at > model.observed_at
                ):
                    reasons.append(CANDIDATE_RECORDED_LATE)
                for reason in reasons:
                    candidates.append(
                        DifferenceCandidate(
                            journal_entry_id=int(entry_id),
                            effective_at=effective_at,
                            recorded_at=recorded_at,
                            amount_minor=int(amount),
                            currency=row.currency,
                            description=description,
                            financial_event_id=(
                                None if event_id is None else int(event_id)
                            ),
                            merchant=merchant,
                            reason=reason,
                        )
                    )
        return DifferenceExplanation(snapshot=row, candidates=tuple(candidates))

    # ----- matching review ------------------------------------------------

    def source_links(
        self, *, pending_limit: int, decided_limit: int
    ) -> SourceLinkReviewView:
        """Return proposals awaiting a decision, plus the recent decisions."""
        with self._session_factory() as session:
            pending = self._link_rows(
                session, (SourceLinkStatus.CANDIDATE,), limit=pending_limit
            )
            decided = self._link_rows(
                session,
                (SourceLinkStatus.CONFIRMED, SourceLinkStatus.REJECTED),
                limit=decided_limit,
            )
            pending_total = int(
                session.execute(
                    select(func.count(FinancialEventSourceLinkModel.id)).where(
                        FinancialEventSourceLinkModel.status
                        == SourceLinkStatus.CANDIDATE.value
                    )
                ).scalar_one()
            )
        return SourceLinkReviewView(
            pending=pending, decided=decided, pending_total=pending_total
        )

    @staticmethod
    def _link_rows(
        session: Session,
        statuses: tuple[SourceLinkStatus, ...],
        *,
        limit: int,
    ) -> tuple[SourceLinkRow, ...]:
        rows = session.execute(
            select(
                FinancialEventSourceLinkModel,
                TransactionModel,
                FinancialEventModel,
                CardModel.issuer_code,
                CardModel.last_four,
            )
            .join(
                TransactionModel,
                TransactionModel.id == FinancialEventSourceLinkModel.transaction_id,
            )
            .join(
                FinancialEventModel,
                FinancialEventModel.id
                == FinancialEventSourceLinkModel.financial_event_id,
            )
            .join(CardModel, CardModel.id == FinancialEventModel.card_id, isouter=True)
            .where(
                FinancialEventSourceLinkModel.status.in_(
                    [status.value for status in statuses]
                )
            )
            .order_by(FinancialEventSourceLinkModel.id.desc())
            .limit(limit)
        ).all()
        return tuple(
            SourceLinkRow(
                link_id=row[0].id,
                status=SourceLinkStatus(row[0].status),
                score_bps=row[0].match_score_bps,
                reasons=source_link_record(row[0]).reasons,
                discrepancies=source_link_record(row[0]).discrepancies,
                reviewed_at=row[0].reviewed_at,
                transaction_id=row[1].id,
                email_id=row[1].email_id,
                source_merchant=row[1].merchant,
                source_amount_minor=row[1].amount_minor,
                source_currency=row[1].currency,
                source_occurred_at=row[1].transaction_at,
                source_card_last_four=row[1].card_last_four,
                event=financial_event_record(row[2]),
                event_card_display=_card_display(row[3], row[4]),
            )
            for row in rows
        )

    # ----- reconciliation -------------------------------------------------

    def reconciliation(self, *, snapshot_limit: int) -> ReconcileView:
        """Return each account's newest observation and the remaining gap."""
        with self._session_factory() as session:
            # Only accounts an adjustment could ever settle are offered, so a
            # card liability can never receive an immutable observation that
            # nothing is able to correct afterwards (ADR-B066).
            reconcilable_account_ids = set(
                session.scalars(
                    select(WalletModel.financial_account_id)
                    .join(
                        LedgerAccountModel,
                        LedgerAccountModel.financial_account_id
                        == WalletModel.financial_account_id,
                    )
                    .where(
                        WalletModel.purpose == WalletPurpose.UNALLOCATED.value,
                        WalletModel.is_active.is_(True),
                        LedgerAccountModel.kind == LedgerAccountKind.ASSET.value,
                    )
                    .distinct()
                ).all()
            )
            accounts = tuple(
                ReconcileAccountRow(
                    account=row.account,
                    balance_minor=row.balance_minor,
                    latest=row.latest_snapshot,
                )
                for row in self._account_rows(session)
                if holds_wallets(row.account.account_type)
                and row.account.id in reconcilable_account_ids
            )
            snapshots = tuple(
                self._snapshot_row(session, model)
                for model in session.scalars(
                    select(BalanceSnapshotModel)
                    .order_by(
                        BalanceSnapshotModel.observed_at.desc(),
                        BalanceSnapshotModel.id.desc(),
                    )
                    .limit(snapshot_limit)
                ).all()
            )
        return ReconcileView(accounts=accounts, snapshots=snapshots)

    # ----- one event ------------------------------------------------------

    def event_detail(self, event_id: int) -> EventDetailView | None:
        """Return one canonical event with its entry, split, and provenance."""
        with self._session_factory() as session:
            model = session.get(FinancialEventModel, event_id)
            if model is None:
                return None
            # An event whose allocation was redone carries the superseded
            # entry, its reversal, and the one in force (ADR-B103), so the
            # entry this page is about is the newest not itself reversed.
            # Taking the oldest instead would describe the decision that was
            # taken back: its categories, its wallet, and a reversal notice
            # for money that is still posted.
            canceller = aliased(JournalEntryModel)
            cancelled = select(canceller.reversal_of_id).where(
                canceller.reversal_of_id.is_not(None)
            )
            entry_model = session.scalars(
                select(JournalEntryModel)
                .where(
                    JournalEntryModel.financial_event_id == event_id,
                    JournalEntryModel.id.not_in(cancelled),
                )
                .order_by(JournalEntryModel.id.desc())
            ).first()
            if entry_model is None:
                # Nothing is in force, which is what a cancelled event looks
                # like; the entry it cancelled is what the page reports.
                entry_model = session.scalars(
                    select(JournalEntryModel)
                    .where(JournalEntryModel.financial_event_id == event_id)
                    .order_by(JournalEntryModel.id.desc())
                ).first()
            reversal_model = (
                None
                if entry_model is None
                else session.scalars(
                    select(JournalEntryModel).where(
                        JournalEntryModel.reversal_of_id == entry_model.id
                    )
                ).first()
            )
            card = (
                None if model.card_id is None else session.get(CardModel, model.card_id)
            )
            card_issuer_name = (
                None
                if card is None
                else session.scalar(
                    select(SenderRuleModel.institution_name).where(
                        SenderRuleModel.gmail_account_id == card.gmail_account_id,
                        SenderRuleModel.institution_key == card.issuer_code,
                    )
                )
            )
            categories = (
                ()
                if entry_model is None
                else self._categories_for(session, [entry_model.id]).get(
                    entry_model.id, ()
                )
            )
            wallet_display = (
                None
                if entry_model is None
                else self._wallets_for(session, [entry_model.id]).get(entry_model.id)
            )
            # Everything about the split is read through the entry this page
            # is about. A redone allocation leaves the superseded rows in
            # place (ADR-B103), and the entry is what names the live ones.
            allocations = (
                ()
                if entry_model is None
                else tuple(
                    session.scalars(
                        select(EventAllocationModel)
                        .where(EventAllocationModel.journal_entry_id == entry_model.id)
                        .order_by(EventAllocationModel.id)
                    ).all()
                )
            )
            dedicated_route = (
                None
                if not allocations
                else session.execute(
                    select(
                        EventAllocationModel.funding_wallet_id,
                        DedicatedWalletAllocationModel.clearing_wallet_id,
                    )
                    .join(
                        DedicatedWalletAllocationModel,
                        DedicatedWalletAllocationModel.event_allocation_id
                        == EventAllocationModel.id,
                    )
                    .where(EventAllocationModel.id.in_(item.id for item in allocations))
                    .order_by(EventAllocationModel.id.desc())
                    .limit(1)
                ).first()
            )
            dedicated_source_wallet_display = None
            clearing_wallet_display = None
            if dedicated_route is not None:
                dedicated_source_wallet_display = self._wallet_display(
                    session, int(dedicated_route[0])
                )
                clearing_wallet_display = self._wallet_display(
                    session, int(dedicated_route[1])
                )
            links = self._links_for_event(session, event_id)
            entry = None if entry_model is None else _entry_record(session, entry_model)
            reversal = (
                None
                if reversal_model is None
                else _entry_record(session, reversal_model)
            )
            in_force = (
                None
                if entry_model is None or reversal_model is not None
                else entry_model.id
            )
            history = self._event_history(
                session, event_id, currency=model.currency, in_force_id=in_force
            )
            # A cancelled event's last split is not a decision in force, so
            # `in_force` rather than `entry_model` is what this asks about.
            has_allocation = bool(allocations) and in_force is not None
        return EventDetailView(
            event=financial_event_record(model),
            entry=entry,
            reversal_entry=reversal,
            history=history,
            card_display=(
                None
                if card is None
                else _card_display(card_issuer_name or card.issuer_code, card.last_four)
            ),
            categories=categories,
            wallet_display=wallet_display,
            is_dedicated_wallet_spending=dedicated_route is not None,
            dedicated_source_wallet_display=dedicated_source_wallet_display,
            clearing_wallet_display=clearing_wallet_display,
            source_links=links,
            has_allocation=has_allocation,
        )

    def _event_history(
        self,
        session: Session,
        event_id: int,
        *,
        currency: str,
        in_force_id: int | None,
    ) -> tuple[EventHistoryRow, ...]:
        """Return every entry ever posted for one event, oldest first.

        A cancellation carries no event of its own, so it is reached through
        `reversal_of_id` exactly as the daily list reaches it.
        """
        original = aliased(JournalEntryModel)
        rows = session.execute(
            select(
                JournalEntryModel.id,
                JournalEntryModel.entry_type,
                JournalEntryModel.effective_at,
                JournalEntryModel.recorded_at,
            )
            .join(
                original,
                original.id == JournalEntryModel.reversal_of_id,
                isouter=True,
            )
            .where(
                func.coalesce(
                    JournalEntryModel.financial_event_id,
                    original.financial_event_id,
                )
                == event_id
            )
            .order_by(JournalEntryModel.id)
        ).all()
        amounts = self._history_amounts(session, [int(row[0]) for row in rows])
        return tuple(
            EventHistoryRow(
                journal_entry_id=int(entry_id),
                entry_type=JournalEntryType(entry_type),
                effective_at=effective_at,
                recorded_at=recorded_at,
                amount_minor=amounts.get(int(entry_id), 0),
                currency=currency,
                is_in_force=int(entry_id) == in_force_id,
            )
            for entry_id, entry_type, effective_at, recorded_at in rows
        )

    @staticmethod
    def _history_amounts(session: Session, entry_ids: list[int]) -> dict[int, int]:
        """Return the one figure that describes what each entry did.

        The expense side is what a purchase is about, and reading it signed
        makes a cancellation the negative of what it took back rather than a
        repeat of the original figure (ADR-B064). An entry that has no expense
        side is not spending at all — a deposit, an interest credit, a
        reconciliation adjustment — and the money side is what describes it:
        positive when it came in, negative when it went out or was taken back.
        Joining the expense side alone left every one of those events with an
        empty history, which is how a page meant to explain a balance showed
        nothing for the entries most likely to be questioned.

        An internal transfer has two money sides that cancel out, so it is
        reported by what it moved; the entry's type and its cancellation
        marker are what say in which direction.
        """
        if not entry_ids:
            return {}
        rows = session.execute(
            select(
                JournalPostingModel.journal_entry_id,
                LedgerAccountModel.kind,
                func.sum(JournalPostingModel.amount_minor),
                func.sum(
                    case(
                        (
                            JournalPostingModel.amount_minor > 0,
                            JournalPostingModel.amount_minor,
                        ),
                        else_=0,
                    )
                ),
            )
            .join(
                LedgerAccountModel,
                LedgerAccountModel.id == JournalPostingModel.ledger_account_id,
            )
            .where(JournalPostingModel.journal_entry_id.in_(entry_ids))
            .group_by(JournalPostingModel.journal_entry_id, LedgerAccountModel.kind)
        ).all()
        totals: dict[int, dict[str, int]] = {}
        gross: dict[int, int] = {}
        for entry_id, kind, total, positive in rows:
            totals.setdefault(int(entry_id), {})[str(kind)] = int(total or 0)
            gross[int(entry_id)] = gross.get(int(entry_id), 0) + int(positive or 0)
        amounts: dict[int, int] = {}
        for entry_id, by_kind in totals.items():
            expense = by_kind.get(LedgerAccountKind.EXPENSE.value)
            if expense is not None:
                amounts[entry_id] = expense
                continue
            money = by_kind.get(LedgerAccountKind.ASSET.value, 0) + by_kind.get(
                LedgerAccountKind.LIABILITY.value, 0
            )
            amounts[entry_id] = money if money else gross.get(entry_id, 0)
        return amounts

    @staticmethod
    def _wallet_display(session: Session, wallet_id: int) -> str:
        """Describe one wallet without exposing an account number."""
        wallet = session.get(WalletModel, wallet_id)
        if wallet is None:  # pragma: no cover - protected by RESTRICT foreign keys
            return "—"
        account = session.get(FinancialAccountModel, wallet.financial_account_id)
        if account is None:  # pragma: no cover - protected by RESTRICT foreign keys
            return wallet.name
        return f"{account.display_name} · {wallet.name}"

    @staticmethod
    def _links_for_event(session: Session, event_id: int) -> tuple[SourceLinkRow, ...]:
        rows = session.execute(
            select(
                FinancialEventSourceLinkModel,
                TransactionModel,
                FinancialEventModel,
                CardModel.issuer_code,
                CardModel.last_four,
            )
            .join(
                TransactionModel,
                TransactionModel.id == FinancialEventSourceLinkModel.transaction_id,
            )
            .join(
                FinancialEventModel,
                FinancialEventModel.id
                == FinancialEventSourceLinkModel.financial_event_id,
            )
            .join(CardModel, CardModel.id == FinancialEventModel.card_id, isouter=True)
            .where(FinancialEventSourceLinkModel.financial_event_id == event_id)
            .order_by(FinancialEventSourceLinkModel.id)
        ).all()
        return tuple(
            SourceLinkRow(
                link_id=row[0].id,
                status=SourceLinkStatus(row[0].status),
                score_bps=row[0].match_score_bps,
                reasons=source_link_record(row[0]).reasons,
                discrepancies=source_link_record(row[0]).discrepancies,
                reviewed_at=row[0].reviewed_at,
                transaction_id=row[1].id,
                email_id=row[1].email_id,
                source_merchant=row[1].merchant,
                source_amount_minor=row[1].amount_minor,
                source_currency=row[1].currency,
                source_occurred_at=row[1].transaction_at,
                source_card_last_four=row[1].card_last_four,
                event=financial_event_record(row[2]),
                event_card_display=_card_display(row[3], row[4]),
            )
            for row in rows
        )


def _entry_record(session: Session, model: JournalEntryModel) -> JournalEntry:
    """Return one entry with its postings, using the shared translation."""
    postings = session.scalars(
        select(JournalPostingModel)
        .where(JournalPostingModel.journal_entry_id == model.id)
        .order_by(JournalPostingModel.id)
    ).all()
    return journal_entry_record(model, tuple(postings))

"""Timezone-correct, currency-separated statistics read repository."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, select
from sqlalchemy.orm import Session, sessionmaker

from app.application.queries.dashboard import TransactionListItem
from app.application.queries.statistics import (
    AmountPoint,
    CurrencyStatistics,
    RankedAmount,
    StatisticsCriteria,
    StatisticsSnapshot,
    TypeCount,
)
from app.domain.budget_enums import CardHolderRole
from app.domain.enums import TransactionSourceStatus, TransactionType
from app.domain.transactions import SPENDING_TRANSACTION_TYPES
from app.infrastructure.database.budget_models import CardModel
from app.infrastructure.database.models import (
    EmailModel,
    SenderRuleModel,
    TransactionModel,
)


@dataclass(frozen=True)
class _ReportRow:
    """Minimal transaction projection used for local-calendar aggregation."""

    transaction_at: datetime
    transaction_type: TransactionType
    amount_minor: int | None
    currency: str | None
    merchant: str | None
    card_last_four: str | None
    institution_key: str | None
    institution_name: str | None


class SqlAlchemyStatisticsReadRepository:
    """Read one bounded year, then aggregate with exact IANA date boundaries."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        local_timezone: str,
    ) -> None:
        self._session_factory = session_factory
        self._timezone = ZoneInfo(local_timezone)

    def statistics(self, criteria: StatisticsCriteria) -> StatisticsSnapshot:
        """Return complete zero-filled trends without mixing currency amounts."""
        period_start = criteria.as_of - timedelta(days=criteria.daily_days - 1)
        monthly_start = _shift_month(
            criteria.as_of.replace(day=1),
            -(criteria.monthly_months - 1),
        )
        earliest = min(period_start, monthly_start)
        start_utc = datetime.combine(earliest, time.min, self._timezone).astimezone(UTC)
        end_utc = datetime.combine(
            criteria.as_of + timedelta(days=1),
            time.min,
            self._timezone,
        ).astimezone(UTC)
        with self._session_factory() as session:
            projected_rows = session.execute(
                select(
                    TransactionModel.transaction_at,
                    TransactionModel.transaction_type,
                    TransactionModel.amount_minor,
                    TransactionModel.currency,
                    TransactionModel.merchant,
                    TransactionModel.card_last_four,
                    SenderRuleModel.institution_key,
                    SenderRuleModel.institution_name,
                )
                .join(EmailModel, EmailModel.id == TransactionModel.email_id)
                .outerjoin(
                    SenderRuleModel,
                    SenderRuleModel.id == EmailModel.matched_sender_rule_id,
                )
                .where(
                    TransactionModel.source_status
                    == TransactionSourceStatus.ACTIVE.value,
                    TransactionModel.manually_ignored.is_(False),
                    TransactionModel.transaction_at >= start_utc,
                    TransactionModel.transaction_at < end_utc,
                )
            ).all()
            unconfirmed = self._unconfirmed(session)
        rows = tuple(
            _ReportRow(
                transaction_at=transaction_at,
                transaction_type=TransactionType(transaction_type),
                amount_minor=amount_minor,
                currency=currency,
                merchant=merchant,
                card_last_four=card_last_four,
                institution_key=institution_key,
                institution_name=institution_name,
            )
            for (
                transaction_at,
                transaction_type,
                amount_minor,
                currency,
                merchant,
                card_last_four,
                institution_key,
                institution_name,
            ) in projected_rows
            if transaction_at is not None
        )
        spending = tuple(
            row
            for row in rows
            if row.transaction_type in SPENDING_TRANSACTION_TYPES
            and row.amount_minor is not None
            and row.currency is not None
        )
        currencies = tuple(
            self._currency_statistics(
                currency,
                spending,
                criteria,
                period_start=period_start,
                monthly_start=monthly_start,
            )
            for currency in sorted({row.currency for row in spending if row.currency})
        )
        selected_rows = tuple(
            row
            for row in rows
            if row.transaction_at.astimezone(self._timezone).date() >= period_start
        )
        type_counts = Counter(row.transaction_type for row in selected_rows)
        return StatisticsSnapshot(
            period_start=period_start,
            period_end=criteria.as_of,
            daily_days=criteria.daily_days,
            currencies=currencies,
            type_distribution=tuple(
                TypeCount(transaction_type=item, count=type_counts[item])
                for item in TransactionType
                if type_counts[item]
            ),
            unconfirmed_transactions=unconfirmed,
        )

    def _currency_statistics(
        self,
        currency: str,
        spending: tuple[_ReportRow, ...],
        criteria: StatisticsCriteria,
        *,
        period_start: date,
        monthly_start: date,
    ) -> CurrencyStatistics:
        rows = tuple(row for row in spending if row.currency == currency)
        daily: dict[date, int] = defaultdict(int)
        monthly: dict[str, int] = defaultdict(int)
        period_rows: list[_ReportRow] = []
        for row in rows:
            local_date = row.transaction_at.astimezone(self._timezone).date()
            amount = row.amount_minor
            if amount is None:  # narrowed by caller; kept defensive
                continue
            if local_date >= monthly_start:
                monthly[local_date.strftime("%Y-%m")] += amount
            if local_date >= period_start:
                daily[local_date] += amount
                period_rows.append(row)
        merchant_ranking = self._ranking(
            ((row.merchant, row.amount_minor) for row in period_rows if row.merchant),
            limit=criteria.ranking_limit,
        )
        card_ranking = self._card_ranking(
            period_rows,
            limit=criteria.ranking_limit,
        )
        return CurrencyStatistics(
            currency=currency,
            period_total_minor=sum(row.amount_minor or 0 for row in period_rows),
            transaction_count=len(period_rows),
            daily_trend=tuple(
                AmountPoint(
                    period=(period_start + timedelta(days=offset)).isoformat(),
                    amount_minor=daily[period_start + timedelta(days=offset)],
                )
                for offset in range(criteria.daily_days)
            ),
            monthly_trend=tuple(
                AmountPoint(
                    period=month.strftime("%Y-%m"),
                    amount_minor=monthly[month.strftime("%Y-%m")],
                )
                for month in _months(monthly_start, criteria.monthly_months)
            ),
            merchant_ranking=merchant_ranking,
            card_ranking=card_ranking,
        )

    @staticmethod
    def _card_ranking(
        rows: Iterable[_ReportRow],
        *,
        limit: int,
    ) -> tuple[RankedAmount, ...]:
        totals: dict[tuple[str, str], int] = defaultdict(int)
        counts: Counter[tuple[str, str]] = Counter()
        names: dict[tuple[str, str], str] = {}
        for row in rows:
            if row.card_last_four is None or row.amount_minor is None:
                continue
            institution_key = row.institution_key or "unknown"
            identity = (institution_key, row.card_last_four)
            names[identity] = (
                row.institution_name or row.institution_key or "未辨識銀行"
            )
            totals[identity] += row.amount_minor
            counts[identity] += 1
        ordered = sorted(
            totals,
            key=lambda identity: (
                -totals[identity],
                -counts[identity],
                names[identity].casefold(),
                identity[1],
            ),
        )
        return tuple(
            RankedAmount(
                label=f"•••• {identity[1]} ({names[identity]})",
                count=counts[identity],
                amount_minor=totals[identity],
            )
            for identity in ordered[:limit]
        )

    @staticmethod
    def _ranking(
        values: Iterable[tuple[str | None, int | None]],
        *,
        limit: int,
    ) -> tuple[RankedAmount, ...]:
        totals: dict[str, int] = defaultdict(int)
        counts: Counter[str] = Counter()
        for label, amount in values:
            if not label or amount is None:
                continue
            totals[label] += amount
            counts[label] += 1
        ordered = sorted(
            totals,
            key=lambda label: (-totals[label], -counts[label], label.casefold()),
        )
        return tuple(
            RankedAmount(
                label=label,
                count=counts[label],
                amount_minor=totals[label],
            )
            for label in ordered[:limit]
        )

    @staticmethod
    def _unconfirmed(session: Session) -> tuple[TransactionListItem, ...]:
        rows = session.execute(
            select(
                TransactionModel,
                EmailModel,
                SenderRuleModel,
                CardModel.id,
            )
            .join(EmailModel, EmailModel.id == TransactionModel.email_id)
            .outerjoin(
                SenderRuleModel,
                SenderRuleModel.id == EmailModel.matched_sender_rule_id,
            )
            .outerjoin(
                CardModel,
                and_(
                    CardModel.gmail_account_id == EmailModel.gmail_account_id,
                    CardModel.issuer_code == SenderRuleModel.institution_key,
                    CardModel.last_four == TransactionModel.card_last_four,
                    # Being supplementary is part of the match, so a join hit
                    # means exactly that and the projection stays a presence
                    # test.
                    CardModel.holder_role == CardHolderRole.SUPPLEMENTARY.value,
                ),
            )
            .where(
                TransactionModel.source_status == TransactionSourceStatus.ACTIVE.value,
                TransactionModel.manually_confirmed.is_(False),
                TransactionModel.manually_ignored.is_(False),
            )
            .order_by(
                TransactionModel.transaction_at.desc(),
                TransactionModel.id.desc(),
            )
            .limit(20)
        ).all()
        return tuple(
            TransactionListItem(
                id=transaction.id,
                email_id=email.id,
                transaction_at=transaction.transaction_at,
                merchant=transaction.merchant,
                amount_minor=transaction.amount_minor,
                currency=transaction.currency,
                transaction_type=TransactionType(transaction.transaction_type),
                card_last_four=transaction.card_last_four,
                institution_key=(
                    sender_rule.institution_key if sender_rule is not None else None
                ),
                institution_name=(
                    sender_rule.institution_name if sender_rule is not None else None
                ),
                is_supplementary_card=supplementary_card_id is not None,
                confidence=transaction.confidence,
                parser_name=transaction.parser_name,
                parser_version=transaction.parser_version,
                manually_confirmed=transaction.manually_confirmed,
                manually_ignored=transaction.manually_ignored,
                subject=email.subject,
                sender=email.sender,
            )
            for transaction, email, sender_rule, supplementary_card_id in rows
        )


def _shift_month(value: date, offset: int) -> date:
    index = value.year * 12 + value.month - 1 + offset
    return date(index // 12, index % 12 + 1, 1)


def _months(start: date, count: int) -> tuple[date, ...]:
    return tuple(_shift_month(start, offset) for offset in range(count))

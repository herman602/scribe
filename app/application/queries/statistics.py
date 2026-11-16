"""Currency-safe reporting query contract and validation facade."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from app.application.queries.dashboard import TransactionListItem
from app.domain.enums import TransactionType

_ALLOWED_DAILY_WINDOWS = frozenset({30, 90, 180, 365})


@dataclass(frozen=True)
class StatisticsCriteria:
    """Bounded report window expressed in the configured local calendar."""

    as_of: date
    daily_days: int = 90
    monthly_months: int = 12
    ranking_limit: int = 10

    def __post_init__(self) -> None:
        if not 1 <= self.daily_days <= 366:
            raise ValueError("daily report window must be between 1 and 366 days")
        if not 1 <= self.monthly_months <= 120:
            raise ValueError("monthly report window must be between 1 and 120 months")
        if not 1 <= self.ranking_limit <= 100:
            raise ValueError("ranking limit must be between 1 and 100")


@dataclass(frozen=True)
class AmountPoint:
    """One period and integer minor-unit amount."""

    period: str
    amount_minor: int


@dataclass(frozen=True)
class RankedAmount:
    """One ranked label with count and currency-local amount."""

    label: str
    count: int
    amount_minor: int


@dataclass(frozen=True)
class TypeCount:
    """Transaction count distribution independent of currency."""

    transaction_type: TransactionType
    count: int


@dataclass(frozen=True)
class CurrencyStatistics:
    """All monetary statistics for exactly one currency."""

    currency: str
    period_total_minor: int
    transaction_count: int
    daily_trend: tuple[AmountPoint, ...]
    monthly_trend: tuple[AmountPoint, ...]
    merchant_ranking: tuple[RankedAmount, ...]
    card_ranking: tuple[RankedAmount, ...]


@dataclass(frozen=True)
class StatisticsSnapshot:
    """Complete report projection without cross-currency monetary totals."""

    period_start: date
    period_end: date
    daily_days: int
    currencies: tuple[CurrencyStatistics, ...]
    type_distribution: tuple[TypeCount, ...]
    unconfirmed_transactions: tuple[TransactionListItem, ...]


class StatisticsReadRepository(Protocol):
    """Infrastructure boundary for bounded report reads."""

    def statistics(self, criteria: StatisticsCriteria) -> StatisticsSnapshot: ...


class StatisticsQueryService:
    """Validate report bounds before invoking optimized infrastructure reads."""

    def __init__(self, repository: StatisticsReadRepository) -> None:
        self._repository = repository

    def report(
        self,
        *,
        now: datetime,
        daily_days: int = 90,
    ) -> StatisticsSnapshot:
        """Return a 12-month report and one allowlisted daily window."""
        if daily_days not in _ALLOWED_DAILY_WINDOWS:
            daily_days = 90
        return self._repository.statistics(
            StatisticsCriteria(
                as_of=now.date(),
                daily_days=daily_days,
                monthly_months=12,
                ranking_limit=10,
            )
        )

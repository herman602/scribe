"""Currency-safe statistics query and validation tests."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from app.application.queries.statistics import (
    StatisticsCriteria,
    StatisticsQueryService,
    StatisticsSnapshot,
)
from app.domain.enums import (
    SenderMatchType,
    TransactionSourceStatus,
    TransactionType,
)
from app.domain.models import EmailSnapshot, SenderRuleDraft, TransactionDraft
from app.infrastructure.database.engine import Database
from app.infrastructure.database.models import TransactionModel
from app.infrastructure.database.statistics_queries import (
    SqlAlchemyStatisticsReadRepository,
)
from app.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork


def _add_transaction(
    database: Database,
    *,
    email_id: int,
    source_key: str,
    transaction_type: TransactionType,
    amount_minor: int,
    currency: str,
    transaction_at: datetime,
    merchant: str,
    card_last_four: str,
) -> int:
    with SqlAlchemyUnitOfWork(database.session_factory) as unit:
        transaction = unit.transactions.add(
            TransactionDraft(
                email_id=email_id,
                source_key=source_key,
                transaction_type=transaction_type,
                amount_minor=amount_minor,
                currency=currency,
                transaction_at=transaction_at,
                merchant=merchant,
                card_last_four=card_last_four,
                confidence=Decimal("0.90"),
                parser_name="fixture",
                parser_version="1.0",
            )
        )
        unit.commit()
        return transaction.id


def _seed_statistics(database: Database) -> dict[str, int]:
    with SqlAlchemyUnitOfWork(database.session_factory) as unit:
        account = unit.accounts.get_or_create("reports@example.test")
        sender_rule = unit.sender_rules.upsert(
            SenderRuleDraft(
                gmail_account_id=account.id,
                institution_key="fixture_bank",
                institution_name="測試銀行",
                match_type=SenderMatchType.EXACT_ADDRESS,
                match_value="notice@bank.invalid",
            )
        )
        email = unit.emails.upsert_from_gmail(
            EmailSnapshot(
                gmail_account_id=account.id,
                gmail_message_id="reports-message",
                gmail_thread_id="reports-thread",
                gmail_history_id="700",
                sender="notice@bank.invalid",
                recipients=("reports@example.test",),
                subject="匿名統計測試",
                gmail_internal_date_ms=1_721_000_000_000,
                matched_sender_rule_id=sender_rule.id,
            )
        ).email
        unit.commit()

    values = {
        "overridden": _add_transaction(
            database,
            email_id=email.id,
            source_key="overridden",
            transaction_type=TransactionType.PURCHASE,
            amount_minor=10_000,
            currency="TWD",
            transaction_at=datetime(2026, 7, 27, 16, 30, tzinfo=UTC),
            merchant="匿名商家甲",
            card_last_four="1111",
        ),
        "fee": _add_transaction(
            database,
            email_id=email.id,
            source_key="fee",
            transaction_type=TransactionType.FEE,
            amount_minor=2_000,
            currency="TWD",
            transaction_at=datetime(2026, 6, 30, 16, 0, tzinfo=UTC),
            merchant="匿名商家甲",
            card_last_four="1111",
        ),
        "old": _add_transaction(
            database,
            email_id=email.id,
            source_key="old",
            transaction_type=TransactionType.PURCHASE,
            amount_minor=5_000,
            currency="TWD",
            transaction_at=datetime(2026, 5, 15, 4, 0, tzinfo=UTC),
            merchant="匿名商家乙",
            card_last_four="3333",
        ),
        "refund": _add_transaction(
            database,
            email_id=email.id,
            source_key="refund",
            transaction_type=TransactionType.REFUND,
            amount_minor=3_000,
            currency="TWD",
            transaction_at=datetime(2026, 7, 28, 3, 0, tzinfo=UTC),
            merchant="匿名商家甲",
            card_last_four="1111",
        ),
        "usd": _add_transaction(
            database,
            email_id=email.id,
            source_key="usd",
            transaction_type=TransactionType.PURCHASE,
            amount_minor=1_299,
            currency="USD",
            transaction_at=datetime(2026, 7, 28, 4, 0, tzinfo=UTC),
            merchant="Fixture Store",
            card_last_four="2222",
        ),
        "ignored": _add_transaction(
            database,
            email_id=email.id,
            source_key="ignored",
            transaction_type=TransactionType.PURCHASE,
            amount_minor=999_999,
            currency="TWD",
            transaction_at=datetime(2026, 7, 28, 5, 0, tzinfo=UTC),
            merchant="應排除商家",
            card_last_four="9999",
        ),
        "superseded": _add_transaction(
            database,
            email_id=email.id,
            source_key="superseded",
            transaction_type=TransactionType.PURCHASE,
            amount_minor=888_888,
            currency="TWD",
            transaction_at=datetime(2026, 7, 28, 6, 0, tzinfo=UTC),
            merchant="應排除商家",
            card_last_four="9999",
        ),
        "future": _add_transaction(
            database,
            email_id=email.id,
            source_key="future",
            transaction_type=TransactionType.PURCHASE,
            amount_minor=777_777,
            currency="TWD",
            transaction_at=datetime(2026, 7, 28, 16, 0, tzinfo=UTC),
            merchant="未來商家",
            card_last_four="7777",
        ),
    }
    with database.session_factory() as session:
        overridden = session.get(TransactionModel, values["overridden"])
        ignored = session.get(TransactionModel, values["ignored"])
        superseded = session.get(TransactionModel, values["superseded"])
        assert overridden is not None and ignored is not None and superseded is not None
        overridden.amount_minor = 15_000
        overridden.manual_overrides_json = {"amount_minor": 15_000}
        overridden.manually_confirmed = True
        ignored.manually_ignored = True
        superseded.source_status = TransactionSourceStatus.SUPERSEDED.value
        session.commit()
    return values


def test_statistics_separate_currencies_and_use_effective_values(
    migrated_database: Database,
) -> None:
    ids = _seed_statistics(migrated_database)
    repository = SqlAlchemyStatisticsReadRepository(
        migrated_database.session_factory,
        local_timezone="Asia/Taipei",
    )

    report = repository.statistics(
        StatisticsCriteria(
            as_of=date(2026, 7, 28),
            daily_days=30,
            monthly_months=12,
            ranking_limit=10,
        )
    )

    assert [item.currency for item in report.currencies] == ["TWD", "USD"]
    by_currency = {item.currency: item for item in report.currencies}
    twd = by_currency["TWD"]
    usd = by_currency["USD"]
    assert twd.period_total_minor == 17_000
    assert twd.transaction_count == 2
    assert usd.period_total_minor == 1_299
    assert len(twd.daily_trend) == 30
    assert len(twd.monthly_trend) == 12
    assert twd.daily_trend[-1].period == "2026-07-28"
    assert twd.daily_trend[-1].amount_minor == 15_000
    assert (
        next(
            point.amount_minor
            for point in twd.monthly_trend
            if point.period == "2026-05"
        )
        == 5_000
    )
    assert twd.merchant_ranking[0].label == "匿名商家甲"
    assert twd.merchant_ranking[0].amount_minor == 17_000
    assert twd.card_ranking[0].label == "•••• 1111 (測試銀行)"

    type_counts = {
        item.transaction_type: item.count for item in report.type_distribution
    }
    assert type_counts == {
        TransactionType.PURCHASE: 2,
        TransactionType.REFUND: 1,
        TransactionType.FEE: 1,
    }
    unconfirmed_ids = {item.id for item in report.unconfirmed_transactions}
    assert ids["overridden"] not in unconfirmed_ids
    assert ids["ignored"] not in unconfirmed_ids
    assert ids["superseded"] not in unconfirmed_ids
    assert ids["refund"] in unconfirmed_ids


def test_card_ranking_separates_same_suffix_by_institution(
    migrated_database: Database,
) -> None:
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        account = unit.accounts.get_or_create("cards-report@example.test")
        first_rule = unit.sender_rules.upsert(
            SenderRuleDraft(
                gmail_account_id=account.id,
                institution_key="first_fixture_bank",
                institution_name="測試銀行甲",
                match_type=SenderMatchType.EXACT_ADDRESS,
                match_value="first@bank.invalid",
            )
        )
        second_rule = unit.sender_rules.upsert(
            SenderRuleDraft(
                gmail_account_id=account.id,
                institution_key="second_fixture_bank",
                institution_name="測試銀行乙",
                match_type=SenderMatchType.EXACT_ADDRESS,
                match_value="second@bank.invalid",
            )
        )
        first_email = unit.emails.upsert_from_gmail(
            EmailSnapshot(
                gmail_account_id=account.id,
                gmail_message_id="first-card-report",
                gmail_thread_id="first-card-thread",
                gmail_history_id="801",
                sender="first@bank.invalid",
                recipients=("cards-report@example.test",),
                subject="匿名卡片統計甲",
                gmail_internal_date_ms=1_721_000_000_000,
                matched_sender_rule_id=first_rule.id,
            )
        ).email
        second_email = unit.emails.upsert_from_gmail(
            EmailSnapshot(
                gmail_account_id=account.id,
                gmail_message_id="second-card-report",
                gmail_thread_id="second-card-thread",
                gmail_history_id="802",
                sender="second@bank.invalid",
                recipients=("cards-report@example.test",),
                subject="匿名卡片統計乙",
                gmail_internal_date_ms=1_721_000_000_001,
                matched_sender_rule_id=second_rule.id,
            )
        ).email
        unit.commit()

    _add_transaction(
        migrated_database,
        email_id=first_email.id,
        source_key="same-suffix-first-bank",
        transaction_type=TransactionType.PURCHASE,
        amount_minor=20_000,
        currency="TWD",
        transaction_at=datetime(2026, 7, 27, 5, 0, tzinfo=UTC),
        merchant="匿名商家甲",
        card_last_four="1234",
    )
    _add_transaction(
        migrated_database,
        email_id=second_email.id,
        source_key="same-suffix-second-bank",
        transaction_type=TransactionType.PURCHASE,
        amount_minor=10_000,
        currency="TWD",
        transaction_at=datetime(2026, 7, 27, 6, 0, tzinfo=UTC),
        merchant="匿名商家乙",
        card_last_four="1234",
    )
    report = SqlAlchemyStatisticsReadRepository(
        migrated_database.session_factory,
        local_timezone="Asia/Taipei",
    ).statistics(
        StatisticsCriteria(
            as_of=date(2026, 7, 28),
            daily_days=30,
            monthly_months=12,
            ranking_limit=10,
        )
    )

    ranking = report.currencies[0].card_ranking
    assert [(item.label, item.amount_minor) for item in ranking] == [
        ("•••• 1234 (測試銀行甲)", 20_000),
        ("•••• 1234 (測試銀行乙)", 10_000),
    ]


def test_statistics_criteria_and_service_bound_report_windows() -> None:
    with pytest.raises(ValueError):
        StatisticsCriteria(as_of=date(2026, 7, 28), daily_days=0)
    with pytest.raises(ValueError):
        StatisticsCriteria(as_of=date(2026, 7, 28), monthly_months=0)
    with pytest.raises(ValueError):
        StatisticsCriteria(as_of=date(2026, 7, 28), ranking_limit=101)

    class CaptureRepository:
        criteria: StatisticsCriteria | None = None

        def statistics(self, criteria: StatisticsCriteria) -> StatisticsSnapshot:
            self.criteria = criteria
            return StatisticsSnapshot(
                period_start=criteria.as_of,
                period_end=criteria.as_of,
                daily_days=criteria.daily_days,
                currencies=(),
                type_distribution=(),
                unconfirmed_transactions=(),
            )

    repository = CaptureRepository()
    service = StatisticsQueryService(repository)
    report = service.report(
        now=datetime(2026, 7, 28, 12, 0, tzinfo=UTC),
        daily_days=31,
    )

    assert report.daily_days == 90
    assert repository.criteria is not None
    assert repository.criteria.monthly_months == 12

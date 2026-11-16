"""FastAPI dashboard integration and mutation-safety tests."""

from __future__ import annotations

import html
import json
import re
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app import __version__
from app.application.services.gmail_auth_health import GmailAuthHealthService
from app.bootstrap import build_web_application
from app.domain.budget_enums import CardHolderRole
from app.domain.budget_models import CardDraft
from app.domain.enums import (
    ClassificationStatus,
    ProcessingStatus,
    SenderMatchType,
    SyncMode,
    TransactionType,
)
from app.domain.models import EmailSnapshot, SenderRuleDraft, TransactionDraft
from app.infrastructure.config.settings import AppSettings
from app.infrastructure.database.engine import Database
from app.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork
from fastapi.testclient import TestClient


def _seed(database: Database) -> tuple[int, int]:
    with SqlAlchemyUnitOfWork(database.session_factory) as unit:
        account = unit.accounts.get_or_create("user@example.test")
        sender_rule = unit.sender_rules.upsert(
            SenderRuleDraft(
                gmail_account_id=account.id,
                institution_key="fixture-bank",
                institution_name="測試銀行",
                match_type=SenderMatchType.EXACT_ADDRESS,
                match_value="notify@bank.example",
            )
        )
        email = unit.emails.upsert_from_gmail(
            EmailSnapshot(
                gmail_account_id=account.id,
                gmail_message_id="gmail-dashboard-1",
                gmail_thread_id="thread-dashboard",
                gmail_history_id="500",
                sender="notify@bank.example",
                recipients=("user@example.test",),
                subject="消費通知 <script>alert(1)</script>",
                received_at=datetime.now(UTC),
                gmail_internal_date_ms=int(datetime.now(UTC).timestamp() * 1000),
                body_text="消費商店：測試商店\n金額：NT$1,280",  # noqa: RUF001
                matched_sender_rule_id=sender_rule.id,
            )
        ).email
        unit.emails.apply_processing_result(
            email.id,
            status=ProcessingStatus.PROCESSED,
            classification=ClassificationStatus.CONFIRMED,
            confidence=Decimal("0.95"),
            reason="Anonymized test parser matched",
            parser_name="fixture",
            parser_version="1.0",
            purge_body=False,
        )
        twd = unit.transactions.add(
            TransactionDraft(
                email_id=email.id,
                source_key="twd",
                transaction_type=TransactionType.PURCHASE,
                merchant="測試商店",
                amount_minor=128000,
                currency="TWD",
                card_last_four="1234",
                transaction_at=datetime.now(UTC),
                confidence=Decimal("0.95"),
                parser_name="fixture",
                parser_version="1.0",
            )
        )
        unit.transactions.add(
            TransactionDraft(
                email_id=email.id,
                source_key="usd",
                transaction_type=TransactionType.REFUND,
                merchant="Example Store",
                amount_minor=1299,
                currency="USD",
                card_last_four="1234",
                transaction_at=datetime.now(UTC),
                confidence=Decimal("0.80"),
                parser_name="fixture",
                parser_version="1.0",
            )
        )
        unit.commit()
        return email.id, twd.id


def _client(settings: AppSettings) -> TestClient:
    return TestClient(build_web_application(settings))


def _hidden(body: str, name: str) -> str:
    match = re.search(rf'name="{name}" value="([^"]+)"', body)
    assert match is not None
    return html.unescape(match.group(1))


def test_health_endpoint_is_non_sensitive(database_settings: AppSettings) -> None:
    with _client(database_settings) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "scribe",
        "version": __version__,
    }


def test_reauthorization_failure_is_a_red_banner_on_every_dashboard_page(
    migrated_database: Database,
    database_settings: AppSettings,
) -> None:
    health = GmailAuthHealthService(
        lambda: SqlAlchemyUnitOfWork(migrated_database.session_factory),
        now=lambda: datetime(2026, 8, 22, 12, tzinfo=UTC),
    )
    health.mark_reauthorization_required(reason_code="refresh_failed")

    with _client(database_settings) as client:
        responses = [
            client.get(path) for path in ("/", "/transactions", "/money/daily", "/more")
        ]

    for response in responses:
        assert response.status_code == 200
        assert 'class="oauth-banner oauth-banner-error"' in response.text
        assert "Gmail 授權已失效" in response.text
        assert "uv run python -m app auth" in response.text


def test_testing_mode_warns_on_day_six_but_production_mode_does_not(
    migrated_database: Database,
    database_settings: AppSettings,
) -> None:
    authorized_at = datetime.now(UTC) - timedelta(days=6, minutes=1)
    GmailAuthHealthService(
        lambda: SqlAlchemyUnitOfWork(migrated_database.session_factory),
        now=lambda: authorized_at,
    ).mark_authorized()

    testing = database_settings.model_copy(update={"gmail_oauth_testing_mode": True})
    with _client(testing) as client:
        warning = client.get("/")

    production = database_settings.model_copy(
        update={"gmail_oauth_testing_mode": False}
    )
    with _client(production) as client:
        no_warning = client.get("/")

    assert 'class="oauth-banner oauth-banner-error"' in warning.text
    assert "授權明天需要更新" in warning.text
    assert "oauth-banner" not in no_warning.text


def test_dashboard_separates_currencies_and_vendors_htmx(
    migrated_database: Database,
    database_settings: AppSettings,
) -> None:
    _seed(migrated_database)
    with _client(database_settings) as client:
        dashboard = client.get("/")
        stylesheet = client.get("/static/styles.css")
        selection_script = client.get("/static/transaction-selection.js")
        htmx = client.get("/static/vendor/htmx.min.js")

    assert dashboard.status_code == 200
    assert "TWD 1,280" in dashboard.text
    assert "TWD 1,280.00" not in dashboard.text
    assert "USD 12.99" in dashboard.text
    assert "僅限本機" in dashboard.text
    assert "過去 7 天交易" in dashboard.text
    assert '<details class="transaction-day" open>' in dashboard.text
    assert "今天 ·" in dashboard.text
    assert "2 筆" in dashboard.text
    assert '<table class="transaction-table batch-selectable">' in dashboard.text
    assert 'class="transaction-time" data-label="時間"' in dashboard.text
    assert 'action="/transactions/confirm"' in dashboard.text
    assert 'name="transaction_ids"' in dashboard.text
    assert 'class="selection-checkbox"' in dashboard.text
    assert '<span class="transaction-card-bank">測試銀行</span> ·' in dashboard.text
    assert "•••• 1234" in dashboard.text
    assert 'data-selection-toggle="dashboard-confirm-form"' in dashboard.text
    assert 'data-bulk-confirm="dashboard-confirm-form" hidden' in dashboard.text
    # The confirm controls belong to the sticky header, not the panel heading:
    # an expanded day is taller than the viewport, and scrolling back up to
    # reach a button is the problem they are there to avoid.
    assert '<div class="header-actions">' in dashboard.text
    assert dashboard.text.index(
        'data-selection-toggle="dashboard-confirm-form"'
    ) < dashboard.text.index("</header>")
    assert stylesheet.status_code == 200
    assert "--green" in stylesheet.text
    assert (
        ".transaction-day-content { border-top: 1px solid var(--line); "
        "padding-left: 0; }"
    ) in stylesheet.text
    assert ".transaction-table-wrap { overflow-x: visible; }" in stylesheet.text
    assert ".selection-checkbox {" in stylesheet.text
    assert "appearance: none;" in stylesheet.text
    assert ":not(.selection-active) .transaction-select" in stylesheet.text
    assert selection_script.status_code == 200
    assert 'form.classList.toggle("selection-active", active)' in selection_script.text
    assert "確認 ${count} 筆交易" in selection_script.text
    assert htmx.status_code == 200
    assert "htmx" in htmx.text


def test_refund_amounts_are_red_in_transaction_rows_and_detail(
    migrated_database: Database,
    database_settings: AppSettings,
) -> None:
    email_id, _ = _seed(migrated_database)
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        refund_id = next(
            item.id
            for item in unit.transactions.list_for_email(email_id)
            if item.transaction_type is TransactionType.REFUND
        )

    with _client(database_settings) as client:
        dashboard = client.get("/")
        detail = client.get(f"/transactions/{refund_id}")
        stylesheet = client.get("/static/styles.css")

    assert dashboard.status_code == 200
    assert (
        '<td class="amount transaction-amount amount-refund">USD 12.99</td>'
        in dashboard.text
    )
    assert (
        '<td class="amount transaction-amount amount-refund">TWD 1,280</td>'
        not in dashboard.text
    )
    assert detail.status_code == 200
    assert '<div class="hero-amount amount-refund">' in detail.text
    assert stylesheet.status_code == 200
    assert ".amount-refund { color: var(--red); }" in stylesheet.text


def test_dashboard_shows_all_transactions_in_seven_local_calendar_days(
    migrated_database: Database,
    database_settings: AppSettings,
) -> None:
    email_id, _ = _seed(migrated_database)
    now = datetime.now(UTC)
    timezone = ZoneInfo(database_settings.local_timezone)
    local_today = now.astimezone(timezone).date()
    start = datetime.combine(
        local_today - timedelta(days=6),
        time.min,
        timezone,
    ).astimezone(UTC)
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        for index in range(12):
            unit.transactions.add(
                TransactionDraft(
                    email_id=email_id,
                    source_key=f"seven-day-{index}",
                    transaction_type=TransactionType.PURCHASE,
                    merchant=f"IN-RANGE-{index}",
                    amount_minor=100,
                    currency="TWD",
                    transaction_at=start + timedelta(minutes=index),
                    confidence=Decimal("0.90"),
                    parser_name="fixture",
                    parser_version="1.0",
                )
            )
        unit.transactions.add(
            TransactionDraft(
                email_id=email_id,
                source_key="before-seven-day-window",
                transaction_type=TransactionType.PURCHASE,
                merchant="OUT-OF-RANGE",
                amount_minor=100,
                currency="TWD",
                transaction_at=start - timedelta(seconds=1),
                confidence=Decimal("0.90"),
                parser_name="fixture",
                parser_version="1.0",
            )
        )
        unit.commit()

    with _client(database_settings) as client:
        dashboard = client.get("/")

    assert dashboard.status_code == 200
    assert "IN-RANGE-0" in dashboard.text
    assert "IN-RANGE-11" in dashboard.text
    assert dashboard.text.index("IN-RANGE-11") < dashboard.text.index("IN-RANGE-0")
    assert "OUT-OF-RANGE" not in dashboard.text
    assert "12 筆" in dashboard.text


def test_reports_are_currency_safe_and_vendor_chartjs_locally(
    migrated_database: Database,
    database_settings: AppSettings,
) -> None:
    _seed(migrated_database)
    with _client(database_settings) as client:
        report = client.get("/reports?days=31")
        chartjs = client.get("/static/vendor/chart.umd.min.js")
        reports_js = client.get("/static/reports.js")
        license_file = client.get("/static/vendor/CHARTJS-LICENSE.md")

    assert report.status_code == 200
    assert "近 90 日" in report.text
    assert "TWD 1,280" in report.text
    assert "TWD 1,280.00" not in report.text
    assert "USD · 近 90 日" not in report.text
    assert "USD 12.99" in report.text  # Refund remains visible for manual review.
    assert "refund" in report.text
    assert "•••• 1234 (測試銀行)" in report.text
    assert "金額只在相同幣別內統計" in report.text
    chart_payload = re.search(
        r'<script id="statistics-data" type="application/json">(.*?)</script>',
        report.text,
    )
    assert chart_payload is not None
    chart_data = json.loads(chart_payload.group(1))
    twd_chart = next(
        item for item in chart_data["currencies"] if item["currency"] == "TWD"
    )
    assert twd_chart["minor_digits"] == 2
    assert twd_chart["display_digits"] == 0
    assert chartjs.status_code == 200
    assert "Chart.js v4.5.1" in chartjs.text
    assert reports_js.status_code == 200
    assert "renderStatistics" in reports_js.text
    assert license_file.status_code == 200
    assert "MIT License" in license_file.text


def test_the_transaction_list_names_a_confidence_only_when_it_is_low(
    migrated_database: Database,
    database_settings: AppSettings,
) -> None:
    """A confident parse is the normal case, so only a doubtful one is labelled.

    The fixture seeds one transaction at 0.95 and one at 0.80, so this asserts
    the rule rather than the absence of any number at all.
    """
    _seed(migrated_database)

    with _client(database_settings) as client:
        listing = client.get("/transactions")

    assert listing.status_code == 200
    assert "信心度 80%" in listing.text
    assert "95%" not in listing.text


def test_transaction_and_email_search_support_htmx_fragments(
    migrated_database: Database,
    database_settings: AppSettings,
) -> None:
    _seed(migrated_database)
    # The repository resolves date filters in the configured local timezone, so
    # a UTC date makes this test fail whenever the two disagree.
    today = datetime.now(ZoneInfo(database_settings.local_timezone)).date().isoformat()
    with _client(database_settings) as client:
        transactions = client.get(
            "/transactions"
            f"?date_from={today}&date_to={today}"
            "&merchant=Example&sender=bank.example"
            "&amount_min=1299&amount_max=1299&currency=USD"
            "&transaction_type=refund&confidence_min=0.80"
            "&review_status=unconfirmed&sort=amount_asc",
            headers={"HX-Request": "true"},
        )
        emails = client.get(
            "/emails?classification=confirmed",
            headers={"HX-Request": "true"},
        )

    assert transactions.status_code == 200
    assert "Example Store" in transactions.text
    assert "測試商店" not in transactions.text
    assert "<html" not in transactions.text
    assert emails.status_code == 200
    assert "notify@bank.example" in emails.text
    assert "<html" not in emails.text


def test_detail_escapes_email_html_and_review_preserves_explicit_override(
    migrated_database: Database,
    database_settings: AppSettings,
) -> None:
    _, transaction_id = _seed(migrated_database)
    with _client(database_settings) as client:
        detail = client.get(f"/transactions/{transaction_id}")
        csrf = _hidden(detail.text, "csrf_token")
        expected = _hidden(detail.text, "expected_updated_at")
        response = client.post(
            f"/transactions/{transaction_id}/review",
            data={
                "csrf_token": csrf,
                "expected_updated_at": expected,
                "override_fields": "merchant",
                "transaction_type": "purchase",
                "merchant": "人工商家",
                "amount_minor": "1280",
                "currency": "TWD",
                "transaction_at": "",
                "manually_confirmed": "1",
                "user_note": "已核對",
            },
            follow_redirects=False,
        )
        refreshed = client.get(f"/transactions/{transaction_id}")

    assert detail.status_code == 200
    assert "<script>alert(1)</script>" not in detail.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in detail.text
    assert response.status_code == 303
    assert "人工商家" in refreshed.text
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        transaction = unit.transactions.get(transaction_id)
    assert transaction is not None
    assert transaction.merchant == "人工商家"
    assert transaction.manually_confirmed


def test_supplementary_badge_reads_the_card_holder_role(
    migrated_database: Database,
    database_settings: AppSettings,
) -> None:
    """The badge follows the card's holder role, the only place it is stored.

    Writing the role onto the card is what the card setup page does, and that
    has to be enough on its own. The badge used to read a second store, so a
    card marked supplementary on that page showed no badge here at all
    (ADR-B101).
    """
    _seed(migrated_database)
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        account = unit.accounts.get_or_create("user@example.test")
        unit.cards.add(
            CardDraft(
                gmail_account_id=account.id,
                issuer_code="fixture-bank",
                display_name="測試銀行 ****1234",
                last_four="1234",
                holder_role=CardHolderRole.SUPPLEMENTARY,
            )
        )
        unit.commit()

    with _client(database_settings) as client:
        listing = client.get("/transactions")
        dashboard = client.get("/")
        report = client.get("/reports")

    badge = 'class="status supplementary">副卡'
    assert listing.text.count(badge) == 2
    assert badge in dashboard.text
    assert badge in report.text


def test_recent_window_widens_to_fourteen_or_thirty_days_and_defaults_to_seven(
    migrated_database: Database,
    database_settings: AppSettings,
) -> None:
    """The list offers 7, 14 and 30 local days and starts at 7.

    One notification sits inside each widening band, so every choice has to
    prove both halves of its contract: it reveals its own band, and it still
    excludes the band beyond it.
    """
    email_id, _ = _seed(migrated_database)
    timezone = ZoneInfo(database_settings.local_timezone)
    local_today = datetime.now(UTC).astimezone(timezone).date()

    def midday(days_ago: int) -> datetime:
        return datetime.combine(
            local_today - timedelta(days=days_ago), time(12), timezone
        ).astimezone(UTC)

    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        for label, days_ago in (("WITHIN-7", 3), ("WITHIN-14", 10), ("WITHIN-30", 20)):
            unit.transactions.add(
                TransactionDraft(
                    email_id=email_id,
                    source_key=f"window-{days_ago}",
                    transaction_type=TransactionType.PURCHASE,
                    merchant=label,
                    amount_minor=100,
                    currency="TWD",
                    transaction_at=midday(days_ago),
                    confidence=Decimal("0.90"),
                    parser_name="fixture",
                    parser_version="1.0",
                )
            )
        unit.commit()

    with _client(database_settings) as client:
        default = client.get("/")
        fortnight = client.get("/?days=14")
        month = client.get("/?days=30")
        unoffered = client.get("/?days=999")
        money_page = client.get("/money?days=30")

    assert "過去 7 天交易" in default.text
    assert "WITHIN-7" in default.text
    assert "WITHIN-14" not in default.text

    assert "過去 14 天交易" in fortnight.text
    assert "WITHIN-14" in fortnight.text
    assert "WITHIN-30" not in fortnight.text
    assert re.search(r'class="pill is-selected"[^>]*href="/\?days=14"', fortnight.text)

    assert "過去 30 天交易" in month.text
    assert "WITHIN-30" in month.text

    # An unoffered width is an unset preference, not an error: the page still
    # renders, at the default, rather than asking the database for a year.
    assert unoffered.status_code == 200
    assert "過去 7 天交易" in unoffered.text
    assert "WITHIN-14" not in unoffered.text

    # The same switch works on /money and keeps that path in its own links.
    assert "過去 30 天交易" in money_page.text
    assert 'href="/money?days=7"' in money_page.text


def test_day_row_flags_unconfirmed_notifications_and_totals_the_day_spending(
    migrated_database: Database,
    database_settings: AppSettings,
) -> None:
    """A day row says what is still waiting and what the day added up to.

    The seeded day holds one purchase and one refund, both unconfirmed, so a
    single page proves two separate rules: the flag counts every unconfirmed
    notification of any type, while the money counts consumption only — a
    refund is movement, not spending. Confirming the purchase then leaves one
    notification waiting with nothing unconfirmed left to spend, which is the
    case that tells the two figures apart.
    """
    _, transaction_id = _seed(migrated_database)

    with _client(database_settings) as client:
        before = client.get("/")
        confirmed = client.post(
            "/transactions/confirm",
            data={
                "csrf_token": _hidden(before.text, "csrf_token"),
                "return_to": "/",
                "continue_to_allocation": "1",
                "transaction_ids": [str(transaction_id)],
            },
            follow_redirects=False,
        )
        after = client.get("/")

    assert confirmed.status_code == 303
    assert '<span class="status pending">待確認 2 筆 · TWD 1,280</span>' in before.text
    assert '<span class="day-spending">TWD 1,280</span>' in before.text
    # The refund reaches neither figure, though it is still listed and counted.
    assert '<span class="day-spending">USD 12.99</span>' not in before.text
    assert "USD 12.99" in before.text

    assert '<span class="status pending">待確認 1 筆</span>' in after.text
    assert '<span class="day-spending">TWD 1,280</span>' in after.text


def test_batch_confirmation_from_dashboard_confirms_selected_transactions(
    migrated_database: Database,
    database_settings: AppSettings,
) -> None:
    email_id, _ = _seed(migrated_database)
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        transaction_ids = tuple(
            transaction.id for transaction in unit.transactions.list_for_email(email_id)
        )

    with _client(database_settings) as client:
        dashboard = client.get("/")
        transactions = client.get("/transactions?review_status=unconfirmed")
        confirmed = client.post(
            "/transactions/confirm",
            data={
                "csrf_token": _hidden(dashboard.text, "csrf_token"),
                "return_to": "/",
                "continue_to_allocation": "1",
                "transaction_ids": [str(value) for value in transaction_ids],
            },
            follow_redirects=False,
        )

    assert 'action="/transactions/confirm"' in transactions.text
    assert 'data-selection-toggle="transaction-list-confirm-form"' in transactions.text
    assert confirmed.status_code == 303
    assert confirmed.headers["location"] == "/money/review"
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        stored = tuple(unit.transactions.get(value) for value in transaction_ids)
    assert all(transaction is not None for transaction in stored)
    assert all(transaction.manually_confirmed for transaction in stored if transaction)


def test_detail_quick_confirmation_returns_to_safe_previous_page(
    migrated_database: Database,
    database_settings: AppSettings,
) -> None:
    _, transaction_id = _seed(migrated_database)
    return_to = "/transactions?review_status=unconfirmed"
    with _client(database_settings) as client:
        detail = client.get(
            f"/transactions/{transaction_id}",
            params={"return_to": return_to},
        )
        csrf = _hidden(detail.text, "csrf_token")
        confirmed = client.post(
            "/transactions/confirm",
            data={
                "csrf_token": csrf,
                "transaction_ids": str(transaction_id),
                "return_to": return_to,
            },
            follow_redirects=False,
        )
        confirmed_detail = client.get(f"/transactions/{transaction_id}")
        unsafe_return = client.post(
            "/transactions/confirm",
            data={
                "csrf_token": csrf,
                "transaction_ids": str(transaction_id),
                "return_to": "https://malicious.example/steal",
            },
            follow_redirects=False,
        )
        missing_selection = client.post(
            "/transactions/confirm",
            data={"csrf_token": csrf, "return_to": "/"},
        )

    assert detail.status_code == 200
    assert "確認並選擇扣款來源" in detail.text
    assert f'href="{return_to.replace("&", "&amp;")}"' in detail.text
    assert confirmed.status_code == 303
    assert confirmed.headers["location"] == return_to
    assert "這筆交易已確認" in confirmed_detail.text
    assert "確認並選擇扣款來源" not in confirmed_detail.text
    assert unsafe_return.status_code == 303
    assert unsafe_return.headers["location"] == "/transactions"
    assert missing_selection.status_code == 422
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        transaction = unit.transactions.get(transaction_id)
    assert transaction is not None
    assert transaction.manually_confirmed


def test_supplementary_card_setting_applies_by_account_bank_and_suffix(
    migrated_database: Database,
    database_settings: AppSettings,
) -> None:
    _, transaction_id = _seed(migrated_database)
    with _client(database_settings) as client:
        detail = client.get(f"/transactions/{transaction_id}")
        marked = client.post(
            f"/transactions/{transaction_id}/supplementary-card",
            data={
                "csrf_token": _hidden(detail.text, "csrf_token"),
                "supplementary": "1",
            },
            follow_redirects=False,
        )
        transaction_list = client.get("/transactions")
        marked_detail = client.get(f"/transactions/{transaction_id}")
        unmarked = client.post(
            f"/transactions/{transaction_id}/supplementary-card",
            data={
                "csrf_token": _hidden(marked_detail.text, "csrf_token"),
                "supplementary": "0",
            },
            follow_redirects=False,
        )
        unmarked_list = client.get("/transactions")

    assert "測試銀行" in detail.text
    assert "•••• 1234" in detail.text
    assert "標記為副卡" in detail.text
    assert marked.status_code == 303
    assert transaction_list.text.count('class="status supplementary">副卡') == 2
    assert "取消副卡標記" in marked_detail.text
    assert unmarked.status_code == 303
    assert 'class="status supplementary">副卡' not in unmarked_list.text


def test_review_rejects_csrf_cross_origin_and_stale_form(
    migrated_database: Database,
    database_settings: AppSettings,
) -> None:
    _, transaction_id = _seed(migrated_database)
    with _client(database_settings) as client:
        detail = client.get(f"/transactions/{transaction_id}")
        csrf = _hidden(detail.text, "csrf_token")
        expected = _hidden(detail.text, "expected_updated_at")
        form = {
            "csrf_token": csrf,
            "expected_updated_at": expected,
            "transaction_type": "purchase",
            "merchant": "測試商店",
            "amount_minor": "1280",
            "currency": "TWD",
            "transaction_at": "",
        }
        missing_csrf = client.post(
            f"/transactions/{transaction_id}/review",
            data={**form, "csrf_token": "wrong"},
        )
        cross_origin = client.post(
            f"/transactions/{transaction_id}/review",
            data=form,
            headers={"origin": "https://malicious.example"},
        )
        first = client.post(
            f"/transactions/{transaction_id}/review",
            data={**form, "manually_confirmed": "1"},
            follow_redirects=False,
        )
        stale = client.post(
            f"/transactions/{transaction_id}/review",
            data=form,
            follow_redirects=False,
        )

    assert missing_csrf.status_code == 422
    assert cross_origin.status_code == 422
    assert first.status_code == 303
    assert stale.status_code == 409


def test_same_origin_fetch_metadata_handles_opaque_browser_origin(
    migrated_database: Database,
    database_settings: AppSettings,
) -> None:
    _, transaction_id = _seed(migrated_database)
    with _client(database_settings) as client:
        detail = client.get(f"/transactions/{transaction_id}")
        form = {
            "csrf_token": _hidden(detail.text, "csrf_token"),
            "expected_updated_at": _hidden(detail.text, "expected_updated_at"),
            "transaction_type": "purchase",
            "merchant": "測試商店",
            "amount_minor": "1280",
            "currency": "TWD",
            "transaction_at": "",
        }
        response = client.post(
            f"/transactions/{transaction_id}/review",
            data=form,
            headers={
                "origin": "null",
                "sec-fetch-site": "same-origin",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303


def test_untrusted_host_is_rejected(database_settings: AppSettings) -> None:
    with _client(database_settings) as client:
        response = client.get("/health", headers={"host": "malicious.example"})

    assert response.status_code == 400


def test_explicit_lan_host_receives_full_access_and_same_origin_writes(
    migrated_database: Database,
    database_settings: AppSettings,
) -> None:
    _, transaction_id = _seed(migrated_database)
    settings = AppSettings(
        **{
            **database_settings.model_dump(),
            "host": "0.0.0.0",
            "lan_access_enabled": True,
            "lan_trusted_hosts": ("192.168.0.135",),
            "external_access_enabled": True,
            "external_hostname": "scribe.example.com",
        }
    )

    with TestClient(
        build_web_application(settings),
        base_url="http://192.168.0.135:8765",
    ) as client:
        dashboard = client.get("/")
        money = client.get("/money")
        confirmed = client.post(
            "/transactions/confirm",
            data={
                "csrf_token": _hidden(dashboard.text, "csrf_token"),
                "transaction_ids": str(transaction_id),
                "return_to": "/transactions",
            },
            headers={"origin": "http://192.168.0.135:8765"},
            follow_redirects=False,
        )
        untrusted = client.get("/health", headers={"host": "192.168.0.99:8765"})
        external_money = client.get("https://scribe.example.com/money")

    assert dashboard.status_code == 200
    assert "資金總覽" in dashboard.text
    assert money.status_code == 200
    assert confirmed.status_code == 303
    assert confirmed.headers["location"] == "/transactions"
    assert untrusted.status_code == 400
    assert external_money.status_code == 404
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        transaction = unit.transactions.get(transaction_id)
    assert transaction is not None
    assert transaction.manually_confirmed


def test_external_hostname_exposes_only_read_only_dashboard_and_reports(
    migrated_database: Database,
    database_settings: AppSettings,
) -> None:
    _, transaction_id = _seed(migrated_database)
    settings = AppSettings(
        **{
            **database_settings.model_dump(),
            "external_access_enabled": True,
            "external_hostname": "scribe.example.com",
        }
    )
    external_headers = {"host": "scribe.example.com"}

    with _client(settings) as client:
        root = client.get("/", headers=external_headers)
        report = client.get("/reports", headers=external_headers)
        stylesheet = client.get("/static/styles.css", headers=external_headers)
        transaction = client.get(
            f"/transactions/{transaction_id}",
            headers=external_headers,
        )
        sync = client.post("/sync/start/full", headers=external_headers)
        confirm = client.post(
            "/transactions/confirm",
            headers=external_headers,
        )
        local_transaction = client.get(f"/transactions/{transaction_id}")

    assert root.status_code == 200
    assert "唯讀交易總覽" in root.text
    assert "過去 7 天交易" in root.text
    # Widening the window is a read, so the read-only surface offers it too.
    assert 'href="/?days=30"' in root.text
    # The read-only surface says what is waiting and what the day cost, but
    # offers no way to answer it from outside.
    assert '<span class="status pending">待確認 2 筆 · TWD 1,280</span>' in root.text
    assert '<span class="day-spending">TWD 1,280</span>' in root.text
    assert "查看全部" not in root.text
    assert ">Emails<" not in root.text
    assert ">同步<" not in root.text
    assert 'action="/transactions/confirm"' not in root.text
    assert 'name="transaction_ids"' not in root.text
    assert f'href="/transactions/{transaction_id}"' not in root.text
    assert report.status_code == 200
    assert "外網唯讀" in report.text
    assert ">Emails<" not in report.text
    assert ">同步<" not in report.text
    assert "前往審核" not in report.text
    assert f'href="/transactions/{transaction_id}"' not in report.text
    assert "scribe_csrf" not in report.headers.get("set-cookie", "")
    assert report.headers["strict-transport-security"].startswith("max-age=")
    assert report.headers["cache-control"] == "private, no-store"
    assert report.headers["referrer-policy"] == "no-referrer"
    assert stylesheet.status_code == 200
    assert transaction.status_code == 404
    assert sync.status_code == 404
    assert confirm.status_code == 404
    assert local_transaction.status_code == 200


def test_external_access_can_confirm_only_with_signed_identity_and_csrf(
    migrated_database: Database,
    database_settings: AppSettings,
) -> None:
    _, transaction_id = _seed(migrated_database)
    settings = AppSettings(
        **{
            **database_settings.model_dump(),
            "external_access_enabled": True,
            "external_hostname": "scribe.example.com",
            "external_confirmation_enabled": True,
            "cloudflare_access_team_domain": "team.cloudflareaccess.com",
            "cloudflare_access_audience": "a" * 64,
            "cloudflare_access_allowed_email": "herman@example.com",
        }
    )

    with _client(settings) as client:
        root = client.get("https://scribe.example.com/")
        csrf_token = _hidden(root.text, "csrf_token")
        rejected = client.post(
            "https://scribe.example.com/transactions/confirm",
            data={
                "csrf_token": csrf_token,
                "transaction_ids": str(transaction_id),
                "return_to": "/",
            },
            headers={
                "origin": "https://scribe.example.com",
                "sec-fetch-site": "same-origin",
            },
        )
        with patch(
            "app.infrastructure.cloudflare.access.CloudflareAccessJwtVerifier.verify",
            return_value="herman@example.com",
        ):
            opaque_origin = client.post(
                "https://scribe.example.com/transactions/confirm",
                data={
                    "csrf_token": csrf_token,
                    "transaction_ids": str(transaction_id),
                    "return_to": "/",
                },
                headers={
                    "cf-access-jwt-assertion": "signed-fixture",
                    "origin": "null",
                },
            )
            wrong_origin = client.post(
                "https://scribe.example.com/transactions/confirm",
                data={
                    "csrf_token": csrf_token,
                    "transaction_ids": str(transaction_id),
                    "return_to": "/",
                },
                headers={
                    "cf-access-jwt-assertion": "signed-fixture",
                    "origin": "https://attacker.example",
                },
            )
            confirmed = client.post(
                "https://scribe.example.com/transactions/confirm",
                data={
                    "csrf_token": csrf_token,
                    "transaction_ids": str(transaction_id),
                    "return_to": "/",
                    "continue_to_allocation": "1",
                },
                headers={
                    "cf-access-jwt-assertion": "signed-fixture",
                    "origin": "https://scribe.example.com",
                },
                follow_redirects=False,
            )

    assert root.status_code == 200
    assert "Access 保護交易總覽" in root.text
    assert "Access 保護" in root.text
    assert 'action="/transactions/confirm"' in root.text
    assert 'name="transaction_ids"' in root.text
    # Same sticky-header placement as the local overview.
    assert '<div class="header-actions">' in root.text
    assert root.text.index(
        'data-selection-toggle="dashboard-confirm-form"'
    ) < root.text.index("</header>")
    assert "查看全部" not in root.text
    assert "scribe_csrf=" in root.headers["set-cookie"]
    assert "Secure" in root.headers["set-cookie"]
    assert root.headers["referrer-policy"] == "strict-origin"
    assert rejected.status_code == 403
    assert opaque_origin.status_code == 422
    assert wrong_origin.status_code == 422
    assert confirmed.status_code == 303
    assert confirmed.headers["location"] == "/"


def test_local_confirmation_continues_to_the_new_event_allocation(
    migrated_database: Database,
    database_settings: AppSettings,
) -> None:
    _, transaction_id = _seed(migrated_database)

    with _client(database_settings) as client:
        detail = client.get(f"/transactions/{transaction_id}")
        confirmed = client.post(
            "/transactions/confirm",
            data={
                "csrf_token": _hidden(detail.text, "csrf_token"),
                "transaction_ids": str(transaction_id),
                "return_to": f"/transactions/{transaction_id}",
                "continue_to_allocation": "1",
            },
            follow_redirects=False,
        )
        allocation = client.get(confirmed.headers["location"])

    assert confirmed.status_code == 303
    assert confirmed.headers["location"].startswith("/money/review?event_id=")
    assert allocation.status_code == 200
    assert "交易已確認。請選擇這筆要列入每月生活費" in allocation.text


def test_sync_page_shows_durable_progress_and_accepts_cancel(
    migrated_database: Database,
    database_settings: AppSettings,
) -> None:
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        account = unit.accounts.get_or_create("sync-page@example.test")
        unit.sync_states.get_or_create(account.id)
        run = unit.sync_runs.create(
            account_id=account.id,
            mode=SyncMode.FULL,
            query="newer_than:1y",
            start_history_id="900",
        )
        unit.commit()

    with _client(database_settings) as client:
        page = client.get("/sync")
        csrf = _hidden(page.text, "csrf_token")
        cancelled = client.post(
            f"/sync/{run.id}/cancel",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        progress = client.get("/sync/progress")

    assert page.status_code == 200
    assert "Gmail 同步" in page.text
    assert "newer_than:1y" not in page.text
    assert "900" in page.text
    assert cancelled.status_code == 303
    assert "cancel requested" in progress.text

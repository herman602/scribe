"""Local-only subscription register page, forms, and month preview tests."""

from __future__ import annotations

import html
import re
from datetime import date

import pytest
from app.application.services.budget_planning import (
    AddCategoryRequest,
    ManageBudgetPlan,
)
from app.application.services.ledger import (
    AddWalletRequest,
    LedgerSetupService,
    OpenFinancialAccountRequest,
)
from app.application.services.subscriptions import (
    AddPricePeriodRequest,
    AddSubscriptionRequest,
    ManageSubscriptions,
)
from app.bootstrap import build_web_application
from app.domain.budget_enums import FinancialAccountType, SubscriptionCadence
from app.infrastructure.config.settings import AppSettings
from app.infrastructure.database.engine import Database
from app.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork
from fastapi.testclient import TestClient

EXTERNAL_HOST = "scribe.example.com"
LOCAL_ONLY_PATHS = ("/subscriptions", "/subscriptions?period=2026-08")
_ORIGIN = {"origin": "http://testserver"}


def _hidden(body: str, name: str) -> str:
    match = re.search(rf'name="{name}" value="([^"]*)"', body)
    assert match is not None
    return html.unescape(match.group(1))


@pytest.fixture
def registered(
    migrated_database: Database, database_settings: AppSettings
) -> AppSettings:
    """Seed one TWD wallet, one JPY wallet, one USD wallet, and a category."""

    def factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(migrated_database.session_factory)

    setup = LedgerSetupService(factory)
    for name, currency, wallet in (
        ("測試支票帳戶", "TWD", "測試訂閱錢包"),
        ("測試日圓帳戶", "JPY", "測試日圓錢包"),
        ("測試美元帳戶", "USD", "測試美元錢包"),
    ):
        account = setup.open_account(
            OpenFinancialAccountRequest(
                institution_code="test_bank",
                display_name=name,
                account_type=FinancialAccountType.CHECKING,
                currency=currency,
            )
        )
        setup.add_wallet(
            AddWalletRequest(financial_account_id=account.account.id, name=wallet)
        )
    ManageBudgetPlan(factory).add_category(AddCategoryRequest(name="測試訂閱"))
    return database_settings


@pytest.fixture
def priced(
    registered: AppSettings, migrated_database: Database
) -> tuple[AppSettings, int]:
    """Add one monthly TWD subscription with one open-ended price."""

    def factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(migrated_database.session_factory)

    service = ManageSubscriptions(factory)
    subscription = service.add(
        AddSubscriptionRequest(
            name="測試影音",
            budget_category_id=1,
            funding_wallet_id=2,
            currency="TWD",
            billing_cadence=SubscriptionCadence.MONTHLY,
            cadence_months=1,
        )
    )
    service.add_price(
        AddPricePeriodRequest(
            subscription_id=subscription.id,
            amount_minor=39000,
            effective_from=date(2026, 1, 1),
        )
    )
    return registered, subscription.id


def test_the_page_previews_one_month_and_shows_its_evidence(
    priced: tuple[AppSettings, int],
) -> None:
    settings, _ = priced

    with TestClient(build_web_application(settings)) as client:
        page = client.get("/subscriptions?period=2026-08")

    assert page.status_code == 200
    assert "2026-08" in page.text
    assert "測試影音" in page.text
    assert "TWD 390" in page.text
    assert "測試訂閱錢包" in page.text
    assert "測試訂閱" in page.text
    assert "每月費用" in page.text


def test_a_subscription_can_be_created_edited_and_deactivated(
    registered: AppSettings,
) -> None:
    with TestClient(build_web_application(registered)) as client:
        page = client.get("/subscriptions?period=2026-08")
        token = _hidden(page.text, "csrf_token")
        created = client.post(
            "/subscriptions",
            data={
                "csrf_token": token,
                "period": "2026-08",
                "name": "測試影音",
                "budget_category_id": "1",
                "funding_wallet_id": "2",
                "currency": "TWD",
                "billing_cadence": "monthly",
                "cadence_months": "1",
            },
            headers=_ORIGIN,
            follow_redirects=False,
        )
        after_create = client.get("/subscriptions?period=2026-08")
        renamed = client.post(
            "/subscriptions/1",
            data={
                "csrf_token": token,
                "period": "2026-08",
                "version": "1",
                "name": "測試影音 Plus",
                "budget_category_id": "1",
                "funding_wallet_id": "2",
                "currency": "TWD",
                "billing_cadence": "yearly",
                "cadence_months": "12",
                "billing_month": "3",
            },
            headers=_ORIGIN,
            follow_redirects=False,
        )
        deactivated = client.post(
            "/subscriptions/1/deactivate",
            data={
                "csrf_token": token,
                "period": "2026-08",
                "version": "2",
                "active_to": "2026-09",
            },
            headers=_ORIGIN,
            follow_redirects=False,
        )
        after = client.get("/subscriptions?period=2026-10")

    assert created.status_code == 303
    assert "測試影音" in after_create.text
    assert renamed.status_code == 303
    assert deactivated.status_code == 303
    assert "測試影音 Plus" in after.text
    assert "已停用" in after.text
    assert "已停止" in after.text


def test_a_price_can_be_added_and_corrected_from_the_page(
    priced: tuple[AppSettings, int],
) -> None:
    settings, subscription_id = priced

    with TestClient(build_web_application(settings)) as client:
        page = client.get("/subscriptions?period=2026-08")
        token = _hidden(page.text, "csrf_token")
        corrected = client.post(
            "/subscriptions/prices/1",
            data={
                "csrf_token": token,
                "period": "2026-08",
                "version": "1",
                "amount": "390",
                "currency": "TWD",
                "effective_from": "2026-01",
                "effective_to": "2026-07",
            },
            headers=_ORIGIN,
            follow_redirects=False,
        )
        added = client.post(
            f"/subscriptions/{subscription_id}/prices",
            data={
                "csrf_token": token,
                "period": "2026-08",
                "amount": "490",
                "currency": "TWD",
                "effective_from": "2026-07",
            },
            headers=_ORIGIN,
            follow_redirects=False,
        )
        june = client.get("/subscriptions?period=2026-06")
        august = client.get("/subscriptions?period=2026-08")

    assert corrected.status_code == 303
    assert added.status_code == 303
    assert "TWD 390" in june.text
    assert "TWD 490" in august.text


def test_a_stale_version_is_a_conflict_for_a_subscription_and_a_price(
    priced: tuple[AppSettings, int],
) -> None:
    settings, _ = priced

    with TestClient(build_web_application(settings)) as client:
        page = client.get("/subscriptions?period=2026-08")
        token = _hidden(page.text, "csrf_token")
        client.post(
            "/subscriptions/prices/1",
            data={
                "csrf_token": token,
                "period": "2026-08",
                "version": "1",
                "amount": "410",
                "currency": "TWD",
                "effective_from": "2026-01",
            },
            headers=_ORIGIN,
            follow_redirects=False,
        )
        stale_price = client.post(
            "/subscriptions/prices/1",
            data={
                "csrf_token": token,
                "period": "2026-08",
                "version": "1",
                "amount": "1",
                "currency": "TWD",
                "effective_from": "2026-01",
            },
            headers=_ORIGIN,
            follow_redirects=False,
        )
        stale_subscription = client.post(
            "/subscriptions/1",
            data={
                "csrf_token": token,
                "period": "2026-08",
                "version": "99",
                "name": "被丟棄的名稱",
                "budget_category_id": "1",
                "funding_wallet_id": "2",
                "currency": "TWD",
                "billing_cadence": "monthly",
                "cadence_months": "1",
            },
            headers=_ORIGIN,
            follow_redirects=False,
        )
        after = client.get("/subscriptions?period=2026-08")

    assert stale_price.status_code == 409
    assert stale_subscription.status_code == 409
    assert "TWD 410" in after.text
    assert "被丟棄的名稱" not in after.text


def test_an_overlapping_price_is_refused_without_a_partial_write(
    priced: tuple[AppSettings, int],
) -> None:
    settings, subscription_id = priced

    with TestClient(build_web_application(settings)) as client:
        page = client.get("/subscriptions?period=2026-08")
        token = _hidden(page.text, "csrf_token")
        refused = client.post(
            f"/subscriptions/{subscription_id}/prices",
            data={
                "csrf_token": token,
                "period": "2026-08",
                "amount": "490",
                "currency": "TWD",
                "effective_from": "2026-04",
            },
            headers=_ORIGIN,
            follow_redirects=False,
        )
        after = client.get("/subscriptions?period=2026-04")

    assert refused.status_code == 422
    assert "must not overlap" in refused.text
    assert "TWD 390" in after.text
    assert "TWD 490" not in after.text


def test_a_mid_month_boundary_is_refused_by_the_form(
    priced: tuple[AppSettings, int],
) -> None:
    settings, subscription_id = priced

    with TestClient(build_web_application(settings)) as client:
        page = client.get("/subscriptions?period=2026-08")
        token = _hidden(page.text, "csrf_token")
        refused = client.post(
            f"/subscriptions/{subscription_id}/prices",
            data={
                "csrf_token": token,
                "amount": "490",
                "currency": "TWD",
                "effective_from": "2026-13",
            },
            headers=_ORIGIN,
            follow_redirects=False,
        )

    assert refused.status_code == 422


def test_each_currency_is_shown_and_totalled_separately(
    registered: AppSettings, migrated_database: Database
) -> None:
    def factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(migrated_database.session_factory)

    service = ManageSubscriptions(factory)
    for name, currency, wallet_id, amount in (
        ("測試影音", "TWD", 2, 39000),
        ("測試日本影音", "JPY", 4, 1200),
        ("測試雲端", "USD", 6, 999),
    ):
        subscription = service.add(
            AddSubscriptionRequest(
                name=name,
                budget_category_id=1,
                funding_wallet_id=wallet_id,
                currency=currency,
                billing_cadence=SubscriptionCadence.MONTHLY,
                cadence_months=1,
            )
        )
        service.add_price(
            AddPricePeriodRequest(
                subscription_id=subscription.id,
                amount_minor=amount,
                effective_from=date(2026, 1, 1),
            )
        )

    with TestClient(build_web_application(registered)) as client:
        page = client.get("/subscriptions?period=2026-08")
        only_jpy = client.get("/subscriptions?period=2026-08&currency=JPY")

    # TWD and JPY display without decimals; USD follows its exponent.
    assert "TWD 390" in page.text
    assert "JPY 1,200" in page.text
    assert "USD 9.99" in page.text
    assert "TWD 390.00" not in page.text
    assert "JPY 1,200.00" not in page.text
    # Filtering shows one currency's requirement section; the register below
    # still lists every subscription so nothing disappears from management.
    assert "TWD · 2026-08" in page.text
    assert "JPY · 2026-08" in only_jpy.text
    assert "TWD · 2026-08" not in only_jpy.text


def test_the_default_wallet_is_configurable_and_never_moves_money(
    registered: AppSettings,
) -> None:
    with TestClient(build_web_application(registered)) as client:
        page = client.get("/subscriptions")
        token = _hidden(page.text, "csrf_token")
        saved = client.post(
            "/subscriptions/default-wallet",
            data={"csrf_token": token, "funding_wallet_id": "2"},
            headers=_ORIGIN,
            follow_redirects=False,
        )
        refused = client.post(
            "/subscriptions/default-wallet",
            data={"csrf_token": token, "funding_wallet_id": "9999"},
            headers=_ORIGIN,
            follow_redirects=False,
        )
        after = client.get("/subscriptions")

    assert page.status_code == 200
    assert "尚未設定" in page.text
    assert saved.status_code == 303
    assert refused.status_code == 422
    assert "測試訂閱錢包" in after.text
    assert "不會移動任何資金" in after.text


def test_a_bad_month_label_is_a_not_found_rather_than_a_guess(
    registered: AppSettings,
) -> None:
    with TestClient(build_web_application(registered)) as client:
        bad = client.get("/subscriptions?period=2026-13")
        also_bad = client.get("/subscriptions?period=not-a-month")
        default = client.get("/subscriptions")

    assert bad.status_code == 404
    assert also_bad.status_code == 404
    assert default.status_code == 200


def test_a_write_without_csrf_or_from_another_origin_is_refused(
    registered: AppSettings,
) -> None:
    with TestClient(build_web_application(registered)) as client:
        page = client.get("/subscriptions")
        token = _hidden(page.text, "csrf_token")
        no_csrf = client.post(
            "/subscriptions/default-wallet",
            data={"funding_wallet_id": "2"},
            headers=_ORIGIN,
            follow_redirects=False,
        )
        foreign_origin = client.post(
            "/subscriptions/default-wallet",
            data={"csrf_token": token, "funding_wallet_id": "2"},
            headers={"origin": "https://evil.example"},
            follow_redirects=False,
        )

    assert no_csrf.status_code == 422
    assert foreign_origin.status_code == 422


def test_the_external_hostname_cannot_reach_any_subscription_route(
    migrated_database: Database, database_settings: AppSettings
) -> None:
    settings = database_settings.model_copy(
        update={
            "external_access_enabled": True,
            "external_hostname": EXTERNAL_HOST,
        }
    )
    external = {"host": EXTERNAL_HOST}

    with TestClient(build_web_application(settings)) as client:
        reads = {path: client.get(path, headers=external) for path in LOCAL_ONLY_PATHS}
        writes = {
            path: client.post(path, headers=external)
            for path in (
                "/subscriptions",
                "/subscriptions/1",
                "/subscriptions/1/deactivate",
                "/subscriptions/1/resume",
                "/subscriptions/1/prices",
                "/subscriptions/prices/1",
                "/subscriptions/default-wallet",
            )
        }
        local = client.get("/subscriptions")

    for path, response in reads.items():
        assert response.status_code == 404, path
        assert "訂閱" not in response.text
    for path, response in writes.items():
        assert response.status_code == 404, path
    assert local.status_code == 200


def test_the_page_fixes_configuration_once_a_price_exists(
    priced: tuple[AppSettings, int],
) -> None:
    """The form stops offering controls the service would have to refuse."""
    settings, _ = priced

    with TestClient(build_web_application(settings)) as client:
        page = client.get("/subscriptions?period=2026-08")
        token = _hidden(page.text, "csrf_token")
        tampered = client.post(
            "/subscriptions/1",
            data={
                "csrf_token": token,
                "period": "2026-08",
                "version": "1",
                "name": "測試影音",
                "budget_category_id": "1",
                "funding_wallet_id": "4",
                "currency": "JPY",
                "billing_cadence": "yearly",
                "cadence_months": "12",
                "billing_month": "3",
            },
            headers=_ORIGIN,
            follow_redirects=False,
        )
        after = client.get("/subscriptions?period=2026-08")

    assert "已經有價格區間" in page.text
    assert 'name="currency" value="TWD"' in page.text
    assert tampered.status_code == 422
    assert "cannot change" in tampered.text
    assert "TWD 390" in after.text
    assert "JPY" not in after.text.split("預覽月份")[1].split("預設扣款錢包")[0]


def test_the_page_resumes_as_a_new_interval_and_keeps_the_stop(
    priced: tuple[AppSettings, int],
) -> None:
    settings, subscription_id = priced

    with TestClient(build_web_application(settings)) as client:
        page = client.get("/subscriptions?period=2026-08")
        token = _hidden(page.text, "csrf_token")
        client.post(
            f"/subscriptions/{subscription_id}/deactivate",
            data={
                "csrf_token": token,
                "period": "2026-08",
                "version": "1",
                "active_to": "2026-08",
            },
            headers=_ORIGIN,
            follow_redirects=False,
        )
        stopped = client.get("/subscriptions?period=2026-08")
        too_early = client.post(
            f"/subscriptions/{subscription_id}/resume",
            data={
                "csrf_token": token,
                "period": "2026-08",
                "active_from": "2026-07",
            },
            headers=_ORIGIN,
            follow_redirects=False,
        )
        resumed = client.post(
            f"/subscriptions/{subscription_id}/resume",
            data={
                "csrf_token": token,
                "period": "2026-11",
                "active_from": "2026-11",
            },
            headers=_ORIGIN,
            follow_redirects=False,
        )
        client.post(
            "/subscriptions/2/prices",
            data={
                "csrf_token": token,
                "period": "2026-11",
                "amount": "490",
                "currency": "TWD",
                "effective_from": "2026-11",
            },
            headers=_ORIGIN,
            follow_redirects=False,
        )
        july = client.get("/subscriptions?period=2026-07")
        september = client.get("/subscriptions?period=2026-09")
        november = client.get("/subscriptions?period=2026-11")

    assert "重新啟用" in stopped.text
    assert too_early.status_code == 422
    assert resumed.status_code == 303
    # The stop is still on record and the gap between the intervals is a gap.
    assert "TWD 390" in july.text
    assert "已停止" in september.text
    assert "尚未開始" in september.text
    assert "TWD 490" in november.text


def test_resuming_into_a_month_another_interval_owns_is_a_stable_422(
    priced: tuple[AppSettings, int],
) -> None:
    settings, subscription_id = priced

    with TestClient(build_web_application(settings)) as client:
        page = client.get("/subscriptions?period=2026-08")
        token = _hidden(page.text, "csrf_token")
        client.post(
            f"/subscriptions/{subscription_id}/deactivate",
            data={
                "csrf_token": token,
                "period": "2026-08",
                "version": "1",
                "active_to": "2026-08",
            },
            headers=_ORIGIN,
            follow_redirects=False,
        )
        created = client.post(
            "/subscriptions",
            data={
                "csrf_token": token,
                "period": "2026-08",
                "name": "測試影音",
                "budget_category_id": "1",
                "funding_wallet_id": "2",
                "currency": "TWD",
                "billing_cadence": "monthly",
                "cadence_months": "1",
                "active_from": "2026-05",
            },
            headers=_ORIGIN,
            follow_redirects=False,
        )
        conflict = client.post(
            f"/subscriptions/{subscription_id}/resume",
            data={
                "csrf_token": token,
                "period": "2026-11",
                "active_from": "2026-05",
            },
            headers=_ORIGIN,
            follow_redirects=False,
        )
        after = client.get("/subscriptions?period=2026-11")

    assert created.status_code == 422
    assert "must not overlap" in created.text
    assert conflict.status_code == 422
    assert "no earlier than" in conflict.text
    assert "Traceback" not in conflict.text
    assert "IntegrityError" not in conflict.text
    assert after.status_code == 200

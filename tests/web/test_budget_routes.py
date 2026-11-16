"""Local-only budget page, allocation form, and month-close route tests."""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from urllib.parse import urlencode

import pytest
from app.application.services.budget_planning import (
    AddCategoryRequest,
    ManageBudgetPlan,
)
from app.application.services.cards import (
    ConfigureCardRequest,
    ManageCards,
    RegisterCardRequest,
)
from app.application.services.event_allocation import (
    AllocateFinancialEvent,
    AllocateFinancialEventRequest,
    AllocationLine,
)
from app.application.services.ledger import (
    AddWalletRequest,
    JournalService,
    LedgerSetupService,
    OpenFinancialAccountRequest,
    OpeningPositionRequest,
)
from app.bootstrap import build_web_application
from app.domain.budget_enums import (
    FinancialAccountType,
    FinancialEventOrigin,
    FinancialEventStatus,
    FinancialEventType,
    PaymentMethod,
    RecognitionBasis,
)
from app.domain.budget_models import FinancialEventDraft, OpeningWalletSplit
from app.infrastructure.config.settings import AppSettings
from app.infrastructure.database.engine import Database
from app.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork
from fastapi.testclient import TestClient

EXTERNAL_HOST = "scribe.example.com"
LOCAL_ONLY_PATHS = ("/budget", "/budget/2026-08")
OCCURRED_AT = datetime(2026, 8, 7, 13, 30, tzinfo=UTC)
_FORM_HEADERS = {
    "origin": "http://testserver",
    "content-type": "application/x-www-form-urlencoded",
}


def _hidden(body: str, name: str) -> str:
    match = re.search(rf'name="{name}" value="([^"]*)"', body)
    assert match is not None
    return html.unescape(match.group(1))


@pytest.fixture
def planned(migrated_database: Database, database_settings: AppSettings) -> AppSettings:
    """Seed one account, two wallets, and two categories."""

    def factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(migrated_database.session_factory)

    setup = LedgerSetupService(factory)
    bank = setup.open_account(
        OpenFinancialAccountRequest(
            institution_code="test_bank",
            display_name="測試支票帳戶",
            account_type=FinancialAccountType.CHECKING,
            currency="TWD",
        )
    )
    setup.add_wallet(
        AddWalletRequest(financial_account_id=bank.account.id, name="飲食")
    )
    JournalService(factory).record_opening_position(
        OpeningPositionRequest(
            financial_account_id=bank.account.id,
            balance_minor=1_000_000,
            observed_at=OCCURRED_AT,
            splits=(
                OpeningWalletSplit(wallet_name="尚未分配", amount_minor=500_000),
                OpeningWalletSplit(wallet_name="飲食", amount_minor=500_000),
            ),
        )
    )
    plan = ManageBudgetPlan(factory)
    plan.add_category(AddCategoryRequest(name="飲食"))
    return database_settings


def test_the_budget_page_sets_a_target_and_shows_it_separately_from_the_buffer(
    planned: AppSettings,
) -> None:
    with TestClient(build_web_application(planned)) as client:
        page = client.get("/budget/2026-08")
        token = _hidden(page.text, "csrf_token")
        saved = client.post(
            "/budget/2026-08/targets",
            data={
                "csrf_token": token,
                "currency": "TWD",
                "budget_category_id": "1",
                "target": "3000",
                "buffer": "500",
                "funding_wallet_id": "2",
            },
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )
        after = client.get("/budget/2026-08")

    assert page.status_code == 200
    assert saved.status_code == 303
    assert "TWD 3,000" in after.text
    assert "TWD 500" in after.text
    assert "不含在目標內" in after.text


def test_the_budget_page_redirects_to_the_current_month_and_rejects_a_bad_label(
    planned: AppSettings,
) -> None:
    with TestClient(build_web_application(planned)) as client:
        current = client.get("/budget", follow_redirects=False)
        bad = client.get("/budget/2026-13")
        also_bad = client.get("/budget/not-a-month")

    assert current.status_code == 303
    assert re.fullmatch(r"/budget/\d{4}-\d{2}", current.headers["location"])
    assert bad.status_code == 404
    assert also_bad.status_code == 404


def test_a_category_can_be_created_from_the_budget_page(
    planned: AppSettings,
) -> None:
    with TestClient(build_web_application(planned)) as client:
        page = client.get("/budget/2026-08")
        token = _hidden(page.text, "csrf_token")
        created = client.post(
            "/budget/categories",
            data={
                "csrf_token": token,
                "name": "交通",
                "parent_id": "",
                "return_to": "/budget/2026-08",
            },
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )
        duplicate = client.post(
            "/budget/categories",
            data={
                "csrf_token": token,
                "name": "交通",
                "parent_id": "",
                "return_to": "/budget/2026-08",
            },
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )
        after = client.get("/budget/2026-08")

    assert created.status_code == 303
    assert created.headers["location"] == "/budget/2026-08"
    assert duplicate.status_code == 422
    assert "交通" in after.text


def test_budget_routes_reject_a_missing_csrf_token(planned: AppSettings) -> None:
    with TestClient(build_web_application(planned)) as client:
        client.get("/budget/2026-08")
        response = client.post(
            "/budget/categories",
            data={"name": "交通"},
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )
        after = client.get("/budget/2026-08")

    assert response.status_code == 422
    assert "CSRF" in response.text
    assert "交通" not in after.text


def test_closing_a_month_from_the_page_is_previewed_first(
    planned: AppSettings,
) -> None:
    with TestClient(build_web_application(planned)) as client:
        page = client.get("/budget/2026-08")
        token = _hidden(page.text, "csrf_token")
        client.post(
            "/budget/2026-08/targets",
            data={
                "csrf_token": token,
                "currency": "TWD",
                "budget_category_id": "1",
                "target": "3000",
                "buffer": "0",
                "funding_wallet_id": "2",
                "month_end_destination_wallet_id": "1",
            },
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )
        with_preview = client.get("/budget/2026-08")
        closed = client.post(
            "/budget/2026-08/close",
            data={"csrf_token": token, "currency": "TWD"},
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )
        after = client.get("/budget/2026-08")

    assert "月底結算" in with_preview.text
    assert "合計掃入" in with_preview.text
    assert closed.status_code == 303
    assert "此月份已" in after.text
    # A closed month no longer offers the button that would sweep again.
    assert "結算這個月" not in after.text


def test_the_external_hostname_cannot_reach_any_budget_route(
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
            "/budget/categories": client.post("/budget/categories", headers=external),
            "/budget/categories/1": client.post(
                "/budget/categories/1", headers=external
            ),
            "/budget/2026-08/targets": client.post(
                "/budget/2026-08/targets", headers=external
            ),
            "/budget/2026-08/close": client.post(
                "/budget/2026-08/close", headers=external
            ),
            "/money/review/1/allocate": client.post(
                "/money/review/1/allocate", headers=external
            ),
        }

    for path, response in reads.items():
        assert response.status_code == 404, path
        assert "預算" not in response.text
    for path, response in writes.items():
        assert response.status_code == 404, path


def test_the_review_page_allocates_a_split_and_posts_once(
    migrated_database: Database, database_settings: AppSettings
) -> None:
    """A card purchase allocated from the page changes liability, not the bank."""

    def factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(migrated_database.session_factory)

    with factory() as unit:
        account_id = unit.accounts.get_or_create("user@example.test").id
        unit.commit()
    setup = LedgerSetupService(factory)
    bank = setup.open_account(
        OpenFinancialAccountRequest(
            institution_code="test_bank",
            display_name="測試支票帳戶",
            account_type=FinancialAccountType.CHECKING,
            currency="TWD",
        )
    )
    liability = setup.open_account(
        OpenFinancialAccountRequest(
            institution_code="test_bank",
            display_name="測試信用卡負債",
            account_type=FinancialAccountType.CARD_LIABILITY,
            currency="TWD",
        )
    )
    JournalService(factory).record_opening_position(
        OpeningPositionRequest(
            financial_account_id=bank.account.id,
            balance_minor=1_000_000,
            observed_at=OCCURRED_AT,
            splits=(
                OpeningWalletSplit(wallet_name="尚未分配", amount_minor=1_000_000),
            ),
        )
    )
    plan = ManageBudgetPlan(factory)
    food = plan.add_category(AddCategoryRequest(name="飲食"))
    transport = plan.add_category(AddCategoryRequest(name="交通"))
    cards = ManageCards(factory)
    card = cards.register(
        RegisterCardRequest(
            gmail_account_id=account_id,
            issuer_code="test_bank",
            display_name="測試卡",
            last_four="4321",
        )
    )
    cards.configure(
        ConfigureCardRequest(
            card_id=card.id,
            expected_version=card.version,
            display_name="測試卡",
            holder_role=card.holder_role,
            liability_account_id=liability.account.id,
            settlement_account_id=bank.account.id,
            default_funding_wallet_id=1,
        )
    )
    with factory() as unit:
        unit.financial_events.add(
            FinancialEventDraft(
                origin=FinancialEventOrigin.GMAIL_TRANSACTION,
                event_type=FinancialEventType.PURCHASE,
                payment_method=PaymentMethod.CREDIT_CARD,
                recognition_basis=RecognitionBasis.GMAIL_NOTIFICATION,
                occurred_at=OCCURRED_AT,
                amount_minor=128_000,
                currency="TWD",
                idempotency_key="gmail_transaction:1",
                merchant="測試咖啡",
                card_id=card.id,
                status=FinancialEventStatus.NEEDS_ALLOCATION,
            )
        )
        unit.commit()

    with TestClient(build_web_application(database_settings)) as client:
        page = client.get("/money/review")
        token = _hidden(page.text, "csrf_token")
        client_token = _hidden(page.text, "client_token")
        version = _hidden(page.text, "version")
        unbalanced = client.post(
            "/money/review/1/allocate",
            content=urlencode(
                [
                    ("csrf_token", token),
                    ("version", version),
                    ("client_token", client_token),
                    ("currency", "TWD"),
                    ("amount", "1000"),
                    ("budget_category_id", str(food.id)),
                    ("funding_wallet_id", "1"),
                ]
            ),
            headers=_FORM_HEADERS,
            follow_redirects=False,
        )
        allocated = client.post(
            "/money/review/1/allocate",
            content=urlencode(
                [
                    ("csrf_token", token),
                    ("version", version),
                    ("client_token", client_token),
                    ("currency", "TWD"),
                    ("amount", "1000"),
                    ("budget_category_id", str(food.id)),
                    ("funding_wallet_id", "1"),
                    ("amount", "280"),
                    ("budget_category_id", str(transport.id)),
                    ("funding_wallet_id", "1"),
                    # The unused third row must be ignored, not rejected.
                    ("amount", ""),
                    ("budget_category_id", ""),
                    ("funding_wallet_id", ""),
                ]
            ),
            headers=_FORM_HEADERS,
            follow_redirects=False,
        )
        after = client.get("/money/review")
        budget_page = client.get("/budget/2026-08")

    assert page.status_code == 200
    assert unbalanced.status_code == 422
    assert "total exactly" in unbalanced.text
    assert allocated.status_code == 303
    assert allocated.headers["location"] == "/money/review?allocated=1"
    assert "目前沒有待分配的事件" in after.text
    with factory() as unit:
        entry = unit.journal.get_by_financial_event(1)
        assert entry is not None
        assert sum(posting.amount_minor for posting in entry.postings) == 0
        assert len(unit.allocations.list_for_event(1)) == 2
        assert len(unit.commitments.list_for_event(1)) == 2
    # The card purchase reserved money instead of reducing the bank balance.
    assert "TWD 10,000" in budget_page.text
    assert "TWD 1,280" in budget_page.text


def test_an_existing_target_is_corrected_from_the_page_with_its_version(
    planned: AppSettings,
) -> None:
    with TestClient(build_web_application(planned)) as client:
        page = client.get("/budget/2026-08")
        token = _hidden(page.text, "csrf_token")
        client.post(
            "/budget/2026-08/targets",
            data={
                "csrf_token": token,
                "currency": "TWD",
                "budget_category_id": "1",
                "target": "3000",
                "buffer": "500",
                "funding_wallet_id": "2",
            },
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )
        with_target = client.get("/budget/2026-08")
        version = _hidden(with_target.text, "version")
        corrected = client.post(
            "/budget/2026-08/targets",
            data={
                "csrf_token": token,
                "currency": "TWD",
                "budget_category_id": "1",
                "target": "2500",
                "buffer": "500",
                "funding_wallet_id": "2",
                "version": version,
            },
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )
        stale = client.post(
            "/budget/2026-08/targets",
            data={
                "csrf_token": token,
                "currency": "TWD",
                "budget_category_id": "1",
                "target": "1",
                "buffer": "0",
                "funding_wallet_id": "2",
                "version": version,
            },
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )
        after = client.get("/budget/2026-08")

    assert "修改「飲食」的目標" in with_target.text
    assert corrected.status_code == 303
    assert stale.status_code == 409
    assert "TWD 2,500" in after.text


def test_a_category_can_be_renamed_and_deactivated_from_the_page(
    planned: AppSettings,
) -> None:
    with TestClient(build_web_application(planned)) as client:
        page = client.get("/budget/2026-08")
        token = _hidden(page.text, "csrf_token")
        version = _hidden(page.text, "version")
        renamed = client.post(
            "/budget/categories/1",
            data={
                "csrf_token": token,
                "version": version,
                "name": "飲食與飲料",
                "parent_id": "",
                "return_to": "/budget/2026-08",
            },
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )
        stale = client.post(
            "/budget/categories/1",
            data={
                "csrf_token": token,
                "version": version,
                "name": "再改一次",
                "parent_id": "",
                "is_active": "1",
                "return_to": "/budget/2026-08",
            },
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )
        after = client.get("/budget/2026-08")

    assert "管理分類" in page.text
    assert renamed.status_code == 303
    assert stale.status_code == 409
    assert "飲食與飲料" in after.text
    assert "再改一次" not in after.text


def test_the_page_reports_spending_with_no_target_and_no_category(
    migrated_database: Database, planned: AppSettings
) -> None:
    def factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(migrated_database.session_factory)

    with factory() as unit:
        event = unit.financial_events.add(
            FinancialEventDraft(
                origin=FinancialEventOrigin.MANUAL,
                event_type=FinancialEventType.PURCHASE,
                payment_method=PaymentMethod.BANK_ACCOUNT,
                recognition_basis=RecognitionBasis.OBSERVED_MANUAL,
                occurred_at=OCCURRED_AT,
                amount_minor=7_500,
                currency="TWD",
                idempotency_key="manual:uncategorized",
                merchant="未分類商店",
            )
        )
        unit.commit()
        event_id = event.id
    AllocateFinancialEvent(factory, JournalService(factory)).execute(
        AllocateFinancialEventRequest(
            financial_event_id=event_id,
            expected_version=1,
            client_token="token-uncategorized",
            lines=(AllocationLine(amount_minor=7_500, funding_wallet_id=1),),
        )
    )

    with TestClient(build_web_application(planned)) as client:
        page = client.get("/budget/2026-08")

    assert "未分類" in page.text
    assert "TWD 75" in page.text
    assert "有支出沒有對應這個月的預算目標" in page.text


def test_a_zero_target_is_accepted_because_the_domain_allows_it(
    planned: AppSettings,
) -> None:
    """Planning nothing for a category is a plan, not a malformed amount."""
    with TestClient(build_web_application(planned)) as client:
        token = _hidden(client.get("/budget/2026-08").text, "csrf_token")
        saved = client.post(
            "/budget/2026-08/targets",
            data={
                "csrf_token": token,
                "currency": "TWD",
                "budget_category_id": "1",
                "target": "0",
                "buffer": "0",
                "funding_wallet_id": "2",
            },
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )

    assert saved.status_code == 303

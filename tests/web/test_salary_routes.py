"""Local-only salary template and allocation pages, forms, and conflicts."""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime

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
from app.domain.ports.repositories import UnitOfWork
from app.infrastructure.config.settings import AppSettings
from app.infrastructure.database.engine import Database
from app.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork
from app.presentation.web.app import _manual_salary_amounts
from fastapi.testclient import TestClient
from httpx2 import Response

EXTERNAL_HOST = "scribe.example.com"
_ORIGIN = {"origin": "http://testserver"}
LOCAL_ONLY_GETS = (
    "/allocations",
    "/allocations/templates",
    "/allocations/2026-08",
)
LOCAL_ONLY_POSTS = (
    "/allocations/templates",
    "/allocations/templates/1",
    "/allocations/templates/1/items",
    "/allocations/templates/items/1",
    "/allocations/templates/items/1/deactivate",
    "/allocations/templates/1/amounts",
    "/allocations/2026-08/salary-receipt",
    "/allocations/2026-08/generate",
    "/allocations/runs/1/regenerate",
    "/allocations/runs/1/approve",
    "/allocations/runs/1/cancel",
    "/allocations/items/1/mark-transferred",
)

type UnitOfWorkFactory = Callable[[], UnitOfWork]


@dataclass(frozen=True)
class Seed:
    """Identifiers the salary forms need to post something real."""

    source_account_id: int
    source_wallet_id: int
    living_wallet_id: int
    subscription_wallet_id: int
    category_id: int


def _hidden(body: str, name: str) -> str:
    match = re.search(rf'name="{name}" value="([^"]*)"', body)
    assert match is not None, name
    return html.unescape(match.group(1))


@pytest.fixture
def seed(migrated_database: Database) -> Seed:
    """Seed two accounts, three wallets, a category, and one subscription."""

    def factory() -> UnitOfWork:
        return SqlAlchemyUnitOfWork(migrated_database.session_factory)

    setup = LedgerSetupService(factory)
    source = setup.open_account(
        OpenFinancialAccountRequest(
            institution_code="test_bank_a",
            display_name="測試薪轉帳戶",
            account_type=FinancialAccountType.CHECKING,
            currency="TWD",
        )
    )
    living = setup.add_wallet(
        AddWalletRequest(financial_account_id=source.account.id, name="測試生活費錢包")
    )
    other = setup.open_account(
        OpenFinancialAccountRequest(
            institution_code="test_bank_b",
            display_name="測試訂閱帳戶",
            account_type=FinancialAccountType.CHECKING,
            currency="TWD",
        )
    )
    subscription_wallet = setup.add_wallet(
        AddWalletRequest(financial_account_id=other.account.id, name="測試訂閱錢包")
    )
    category = ManageBudgetPlan(factory).add_category(
        AddCategoryRequest(name="測試分類")
    )
    subscriptions = ManageSubscriptions(factory)
    subscription = subscriptions.add(
        AddSubscriptionRequest(
            name="測試影音",
            budget_category_id=category.id,
            funding_wallet_id=subscription_wallet.id,
            currency="TWD",
            billing_cadence=SubscriptionCadence.MONTHLY,
            cadence_months=1,
        )
    )
    subscriptions.add_price(
        AddPricePeriodRequest(
            subscription_id=subscription.id,
            amount_minor=39_000,
            effective_from=date(2026, 1, 1),
        )
    )
    assert source.unallocated_wallet is not None
    return Seed(
        source_account_id=source.account.id,
        source_wallet_id=source.unallocated_wallet.id,
        living_wallet_id=living.id,
        subscription_wallet_id=subscription_wallet.id,
        category_id=category.id,
    )


@pytest.fixture
def client(
    migrated_database: Database, database_settings: AppSettings, seed: Seed
) -> Iterator[TestClient]:
    del migrated_database
    with TestClient(
        build_web_application(database_settings), base_url="http://testserver"
    ) as test_client:
        yield test_client


@pytest.fixture
def external_client(
    migrated_database: Database, database_settings: AppSettings, seed: Seed
) -> Iterator[TestClient]:
    del migrated_database
    settings = database_settings.model_copy(
        update={
            "external_access_enabled": True,
            "external_hostname": EXTERNAL_HOST,
            "external_confirmation_enabled": False,
        }
    )
    with TestClient(
        build_web_application(settings), base_url=f"https://{EXTERNAL_HOST}"
    ) as test_client:
        yield test_client


def _post(client: TestClient, path: str, data: dict[str, str]) -> Response:
    page = client.get("/allocations/templates")
    token = client.cookies.get("scribe_csrf") or _hidden(page.text, "csrf_token")
    return client.post(
        path,
        data={"csrf_token": token, **data},
        headers=_ORIGIN,
        follow_redirects=False,
    )


def _add_full_plan(client: TestClient, seed: Seed) -> None:
    """Add both a same-account fixed line and a cross-bank subscription line."""
    body = _create_template(client, seed)
    _post(
        client,
        "/allocations/templates/1/items",
        {
            "name": "測試生活費",
            "destination_wallet_id": str(seed.living_wallet_id),
            "calculation_mode": "fixed",
            "fixed_amount": "300",
            "item_currency": "TWD",
            "display_order": "0",
            "template_version": _hidden(body, "template_version"),
        },
    )
    page = client.get("/allocations/templates").text
    _post(
        client,
        "/allocations/templates/1/items",
        {
            "name": "測試訂閱",
            "destination_wallet_id": str(seed.subscription_wallet_id),
            "calculation_mode": "subscription_requirement",
            "display_order": "1",
            "template_version": _hidden(page, "template_version"),
        },
    )


def _create_template(client: TestClient, seed: Seed) -> str:
    response = _post(
        client,
        "/allocations/templates",
        {
            "name": "測試薪資範本",
            "currency": "TWD",
            "source_account_id": str(seed.source_account_id),
        },
    )
    assert response.status_code == 303
    return client.get("/allocations/templates").text


def test_the_template_page_renders_locally_and_lists_accounts(
    client: TestClient, seed: Seed
) -> None:
    response = client.get("/allocations/templates")

    assert response.status_code == 200
    assert "薪資範本" in response.text
    assert "測試薪轉帳戶" in response.text
    assert str(seed.source_account_id) in response.text


def test_creating_a_template_and_an_item_redirects_and_shows_them(
    client: TestClient, seed: Seed
) -> None:
    body = _create_template(client, seed)
    assert "測試薪資範本" in body
    template_id = 1
    template_version = _hidden(body, "template_version")

    response = _post(
        client,
        f"/allocations/templates/{template_id}/items",
        {
            "name": "測試生活費",
            "destination_wallet_id": str(seed.living_wallet_id),
            "calculation_mode": "fixed",
            "fixed_amount": "300",
            "item_currency": "TWD",
            "budget_category_id": str(seed.category_id),
            "display_order": "0",
            "template_version": template_version,
        },
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/allocations/templates?saved=1"
    page = client.get("/allocations/templates").text
    assert "測試生活費" in page
    assert "測試生活費錢包" in page
    # TWD stores cents but is displayed without decimals: 30000 minor = 300.
    assert "TWD 300" in page
    assert "TWD 300.00" not in page


def test_a_stale_item_form_is_a_conflict_not_a_silent_overwrite(
    client: TestClient, seed: Seed
) -> None:
    body = _create_template(client, seed)
    _post(
        client,
        "/allocations/templates/1/items",
        {
            "name": "測試生活費",
            "destination_wallet_id": str(seed.living_wallet_id),
            "calculation_mode": "fixed",
            "fixed_amount": "300",
            "item_currency": "TWD",
            "display_order": "0",
            "template_version": _hidden(body, "template_version"),
        },
    )

    response = _post(
        client,
        "/allocations/templates/items/1",
        {
            "version": "99",
            "name": "改名",
            "destination_wallet_id": str(seed.living_wallet_id),
            "calculation_mode": "fixed",
            "fixed_amount": "1",
            "item_currency": "TWD",
            "display_order": "0",
        },
    )

    assert response.status_code == 409
    assert "重新載入" in response.text
    assert "測試生活費" in client.get("/allocations/templates").text


def test_a_domain_validation_failure_is_a_422(client: TestClient, seed: Seed) -> None:
    _create_template(client, seed)

    response = _post(
        client,
        "/allocations/templates/1/items",
        {
            "name": "不合法",
            "destination_wallet_id": str(seed.source_wallet_id),
            "calculation_mode": "fixed",
            "fixed_amount": "1",
            "item_currency": "TWD",
            "display_order": "0",
        },
    )

    assert response.status_code == 422
    assert "無法新增薪資項目" in response.text


def test_a_missing_csrf_token_or_a_foreign_origin_is_refused(
    client: TestClient, seed: Seed
) -> None:
    client.get("/allocations/templates")

    without_token = client.post(
        "/allocations/templates",
        data={"name": "x", "currency": "TWD", "source_account_id": "1"},
        headers=_ORIGIN,
    )
    token = client.cookies.get("scribe_csrf")
    assert token is not None
    foreign = client.post(
        "/allocations/templates",
        data={
            "csrf_token": token,
            "name": "x",
            "currency": "TWD",
            "source_account_id": "1",
        },
        headers={"origin": "https://evil.example"},
    )

    assert without_token.status_code == 422
    assert foreign.status_code == 422


@pytest.mark.parametrize("path", LOCAL_ONLY_GETS)
def test_every_salary_page_is_invisible_to_the_external_hostname(
    external_client: TestClient, path: str
) -> None:
    response = external_client.get(path, follow_redirects=False)

    assert response.status_code == 404
    assert "Not found" in response.text


@pytest.mark.parametrize("path", LOCAL_ONLY_POSTS)
def test_every_salary_write_is_invisible_to_the_external_hostname(
    external_client: TestClient, path: str
) -> None:
    response = external_client.post(path, data={"csrf_token": "x"})

    assert response.status_code == 404


def test_the_month_page_walks_the_whole_flow_and_stays_frozen(
    client: TestClient, seed: Seed
) -> None:
    _add_full_plan(client, seed)

    page = client.get("/allocations/2026-08?template_id=1")
    assert page.status_code == 200
    token = _hidden(page.text, "client_token")

    received = _post(
        client,
        "/allocations/2026-08/salary-receipt",
        {
            "template_id": "1",
            "amount": "1000",
            "currency": "TWD",
            "occurred_at": "2026-08-05T09:00",
            "client_token": token,
            "period": "2026-08",
        },
    )
    assert received.status_code == 303
    assert (
        received.headers["location"] == "/allocations/2026-08?received=1&template_id=1"
    )

    month = client.get("/allocations/2026-08?template_id=1").text
    assert "測試薪轉帳戶" in month
    generated = _post(
        client,
        "/allocations/2026-08/generate",
        {"template_id": "1", "salary_event_id": "1", "period": "2026-08"},
    )
    assert generated.status_code == 303

    month = client.get("/allocations/2026-08?template_id=1").text
    assert "已凍結" in month
    assert "第 1 代" in month
    assert "範本版本" in month
    # Salary, planned, and remainder are three separate figures.
    assert "TWD 1,000" in month
    assert "TWD 690" in month
    assert "TWD 310" in month
    # A wallet and a bank are named, never a bare identifier.
    assert "測試訂閱帳戶" in month
    assert "測試訂閱錢包" in month
    assert "草稿" in month

    approved = _post(
        client,
        "/allocations/runs/1/approve",
        {"version": _hidden(month, "version"), "period": "2026-08", "template_id": "1"},
    )
    assert approved.status_code == 303
    month = client.get("/allocations/2026-08?template_id=1").text
    assert "待手動轉帳" in month
    assert "已在原帳戶分配" in month
    assert "已轉帳" in month

    transferred = _post(
        client,
        "/allocations/items/2/mark-transferred",
        {
            "version": "2",
            "effective_at": "2026-08-06T18:30",
            "period": "2026-08",
            "template_id": "1",
        },
    )
    assert transferred.status_code == 303
    month = client.get("/allocations/2026-08?template_id=1").text
    assert "已完成" in month


def test_a_stale_approval_form_is_a_conflict(client: TestClient, seed: Seed) -> None:
    _add_full_plan(client, seed)
    month = client.get("/allocations/2026-08?template_id=1")
    _post(
        client,
        "/allocations/2026-08/salary-receipt",
        {
            "template_id": "1",
            "amount": "1000",
            "currency": "TWD",
            "occurred_at": "2026-08-05T09:00",
            "client_token": _hidden(month.text, "client_token"),
        },
    )
    _post(
        client,
        "/allocations/2026-08/generate",
        {"template_id": "1", "salary_event_id": "1"},
    )

    response = _post(
        client,
        "/allocations/runs/1/approve",
        {"version": "99", "period": "2026-08", "template_id": "1"},
    )

    assert response.status_code == 409


def test_an_unknown_month_label_is_a_404(client: TestClient) -> None:
    assert client.get("/allocations/2026-13").status_code == 404
    assert client.get("/allocations/not-a-month").status_code == 404


def test_the_current_month_redirects_from_the_bare_path(client: TestClient) -> None:
    response = client.get("/allocations", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/allocations/")


def test_the_page_never_recalculates_a_frozen_snapshot_after_a_price_change(
    client: TestClient, seed: Seed, migrated_database: Database
) -> None:
    _add_full_plan(client, seed)
    month = client.get("/allocations/2026-08?template_id=1")
    _post(
        client,
        "/allocations/2026-08/salary-receipt",
        {
            "template_id": "1",
            "amount": "1000",
            "currency": "TWD",
            "occurred_at": "2026-08-05T09:00",
            "client_token": _hidden(month.text, "client_token"),
        },
    )
    _post(
        client,
        "/allocations/2026-08/generate",
        {"template_id": "1", "salary_event_id": "1"},
    )
    before = client.get("/allocations/2026-08?template_id=1").text

    with migrated_database.engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE subscription_price_periods SET amount_minor = 99000 WHERE id = 1"
        )

    after = client.get("/allocations/2026-08?template_id=1").text
    assert "TWD 390" in before
    assert "TWD 390" in after
    assert "TWD 990" not in after


def test_the_pages_carry_no_horizontal_overflow_helpers_by_accident(
    client: TestClient, seed: Seed
) -> None:
    """Core figures are stacked cards, not a wide table needing side-scroll."""
    _add_full_plan(client, seed)
    page = client.get("/allocations/templates").text
    month = client.get("/allocations/2026-08?template_id=1").text

    assert "<table" not in page
    assert "<table" not in month
    # The plan is a wrapping grid, and the structural editor it hides is still
    # the stacked-card form it always was.
    assert "plan-sheet" in page
    assert "salary-item-grid" in page
    assert "stacked-form" in page


class TestPlanSheet:
    """The plan is one grouped sheet whose amounts submit in a single pass."""

    def test_the_sheet_groups_destinations_by_the_bank_they_land_in(
        self, client: TestClient, seed: Seed
    ) -> None:
        _add_full_plan(client, seed)

        page = client.get("/allocations/templates").text

        assert 'class="plan-sheet"' in page
        assert 'aria-label="測試薪轉帳戶 的每月分配"' in page
        assert 'aria-label="測試訂閱帳戶 的每月分配"' in page
        # A fixed line is typed; a calculated one has no amount to type.
        assert 'name="amount_1"' in page
        assert 'name="amount_2"' not in page

    def test_one_submission_saves_a_changed_amount(
        self, client: TestClient, seed: Seed
    ) -> None:
        _add_full_plan(client, seed)
        page = client.get("/allocations/templates").text

        response = _post(
            client,
            "/allocations/templates/1/amounts",
            {
                "currency": "TWD",
                "template_version": _hidden(page, "template_version"),
                "amount_1": "450",
                "version_1": _hidden(page, "version_1"),
            },
        )

        assert response.status_code == 303
        saved = client.get("/allocations/templates").text
        assert "TWD 450" in saved

    def test_an_unchanged_amount_does_not_bump_a_version(
        self, client: TestClient, seed: Seed
    ) -> None:
        """Correcting one line must not invalidate every other line's form."""
        _add_full_plan(client, seed)
        page = client.get("/allocations/templates").text
        before = _hidden(page, "version_1")

        response = _post(
            client,
            "/allocations/templates/1/amounts",
            {
                "currency": "TWD",
                "template_version": _hidden(page, "template_version"),
                "amount_1": "300",
                "version_1": before,
            },
        )

        assert response.status_code == 303
        assert _hidden(client.get("/allocations/templates").text, "version_1") == before

    def test_a_stale_amount_is_refused(self, client: TestClient, seed: Seed) -> None:
        _add_full_plan(client, seed)
        page = client.get("/allocations/templates").text

        response = _post(
            client,
            "/allocations/templates/1/amounts",
            {
                "currency": "TWD",
                "template_version": _hidden(page, "template_version"),
                "amount_1": "450",
                "version_1": "99",
            },
        )

        assert response.status_code == 409

    def test_an_amount_for_a_calculated_line_is_refused(
        self, client: TestClient, seed: Seed
    ) -> None:
        _add_full_plan(client, seed)
        page = client.get("/allocations/templates").text

        response = _post(
            client,
            "/allocations/templates/1/amounts",
            {
                "currency": "TWD",
                "template_version": _hidden(page, "template_version"),
                "amount_2": "1000",
                "version_2": "1",
            },
        )

        assert response.status_code == 422
        assert "calculated" in response.text

    def test_the_structural_editor_is_still_available(
        self, client: TestClient, seed: Seed
    ) -> None:
        _add_full_plan(client, seed)

        page = client.get("/allocations/templates").text

        assert 'name="destination_wallet_id"' in page
        assert 'name="calculation_mode"' in page


class TestMonthEmptyState:
    """An empty month explains itself instead of showing bare headings."""

    def test_a_month_without_a_template_says_to_choose_one(
        self, client: TestClient
    ) -> None:
        page = client.get("/allocations/2026-08").text

        assert "還沒有選定薪資範本" in page

    def test_a_month_with_a_plan_but_no_receipt_says_to_record_one(
        self, client: TestClient, seed: Seed
    ) -> None:
        _add_full_plan(client, seed)

        page = client.get("/allocations/2026-08?template_id=1").text

        assert "還沒有這個月的薪資入帳" in page
        assert "測試薪轉帳戶" in page

    def test_a_month_before_the_opening_cutoff_says_it_needs_nothing(
        self, client: TestClient, seed: Seed, migrated_database: Database
    ) -> None:
        """A salary received before cutover already sits in the opening balances."""

        def factory() -> UnitOfWork:
            return SqlAlchemyUnitOfWork(migrated_database.session_factory)

        _add_full_plan(client, seed)
        with factory() as unit:
            unit.import_batches.add(
                source_kind="synthetic",
                format_version=1,
                content_sha256="3" * 64,
                source_locator_hash="4" * 64,
                observed_at=datetime(2026, 8, 14, 15, 10, tzinfo=UTC),
                cutoff_at=datetime(2026, 8, 14, 15, 10, tzinfo=UTC),
                row_count=0,
                warning_count=0,
                applied_at=datetime(2026, 8, 15, 1, 0, tzinfo=UTC),
            )
            unit.commit()

        before = client.get("/allocations/2026-07?template_id=1").text
        after = client.get("/allocations/2026-09?template_id=1").text

        assert "這個月不需要分配" in before
        assert "這個月不需要分配" not in after
        assert "還沒有這個月的薪資入帳" in after


class TestZeroIsAPlan:
    """Zero is a real planning answer, and the forms default to it."""

    def test_a_plan_sheet_amount_may_be_set_to_zero(
        self, client: TestClient, seed: Seed
    ) -> None:
        _add_full_plan(client, seed)
        page = client.get("/allocations/templates").text

        response = _post(
            client,
            "/allocations/templates/1/amounts",
            {
                "currency": "TWD",
                "template_version": _hidden(page, "template_version"),
                "amount_1": "0",
                "version_1": _hidden(page, "version_1"),
            },
        )

        assert response.status_code == 303, response.text
        assert "TWD 0" in client.get("/allocations/templates").text

    def test_a_fixed_item_may_be_added_with_no_amount_yet(
        self, client: TestClient, seed: Seed
    ) -> None:
        body = _create_template(client, seed)

        response = _post(
            client,
            "/allocations/templates/1/items",
            {
                "name": "尚未決定金額",
                "destination_wallet_id": str(seed.living_wallet_id),
                "calculation_mode": "fixed",
                "fixed_amount": "0",
                "item_currency": "TWD",
                "display_order": "0",
                "template_version": _hidden(body, "template_version"),
            },
        )

        assert response.status_code == 303, response.text


def test_a_manual_line_may_plan_nothing_this_month(
    client: TestClient, seed: Seed
) -> None:
    """The form ships zero as the default, so zero must be submittable."""
    body = _create_template(client, seed)
    _post(
        client,
        "/allocations/templates/1/items",
        {
            "name": "手動決定",
            "destination_wallet_id": str(seed.living_wallet_id),
            "calculation_mode": "manual",
            "item_currency": "TWD",
            "display_order": "0",
            "template_version": _hidden(body, "template_version"),
        },
    )

    lines = _manual_salary_amounts(
        {"manual_item_id": ["1"], "manual_amount": ["0"]}, "TWD"
    )

    assert lines[0].amount_minor == 0

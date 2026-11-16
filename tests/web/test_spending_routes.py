"""The local-only B7 pages: daily spending, accounts, matching, reconciling."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from app.application.services.cards import (
    ConfigureCardRequest,
    ManageCards,
    RegisterCardRequest,
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
    CardHolderRole,
    FinancialAccountType,
    FinancialEventOrigin,
    FinancialEventStatus,
    FinancialEventType,
    PaymentMethod,
    RecognitionBasis,
    WalletPurpose,
)
from app.domain.budget_models import (
    BudgetCategoryDraft,
    FinancialEventDraft,
    OpeningWalletSplit,
)
from app.domain.ports.repositories import UnitOfWork
from app.infrastructure.config.settings import AppSettings
from app.infrastructure.database.engine import Database
from app.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork
from fastapi.testclient import TestClient
from httpx2 import Response

EXTERNAL_HOST = "scribe.example.com"
_ORIGIN = {"origin": "http://testserver"}
OBSERVED_AT = datetime(2026, 8, 9, 4, 30, tzinfo=UTC)
LOCAL_DAY = "2026-08-09"

LOCAL_ONLY_GETS = (
    "/money",
    "/money/daily",
    "/money/expenses/new",
    "/money/accounts",
    "/money/accounts/1",
    "/money/accounts/1/order",
    "/money/accounts/order",
    "/money/events/1",
    "/money/bank-movements/new",
    "/money/source-links",
    "/money/reconcile",
)
LOCAL_ONLY_POSTS = (
    "/money/expenses",
    "/money/events/1/note",
    "/money/events/1/reverse",
    "/money/bank-movements",
    "/money/account-transfers",
    "/money/refunds",
    "/money/source-links/1/confirm",
    "/money/source-links/1/reject",
    "/money/source-transactions/1/separate",
    "/money/reconcile/snapshots",
    "/money/reconcile/1/adjust",
    "/money/wallets/1/balance",
    "/money/accounts/1/order",
    "/money/accounts/order",
)

type UnitOfWorkFactory = Callable[[], UnitOfWork]


@dataclass(frozen=True)
class Seed:
    """Identifiers the B7 forms need to post something real."""

    bank_account_id: int
    bank_unallocated_id: int
    bank_food_wallet_id: int
    bank_clearing_wallet_id: int
    cash_account_id: int
    cash_wallet_id: int
    card_id: int
    food_id: int


@pytest.fixture
def seed(migrated_database: Database) -> Seed:
    """Open the accounts, the card, and the categories the pages display."""

    def factory() -> UnitOfWork:
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
    cash = setup.open_account(
        OpenFinancialAccountRequest(
            institution_code="wallet",
            display_name="皮夾",
            account_type=FinancialAccountType.CASH,
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
    food_wallet = setup.add_wallet(
        AddWalletRequest(financial_account_id=bank.account.id, name="飲食")
    )
    clearing_wallet = setup.add_wallet(
        AddWalletRequest(
            financial_account_id=bank.account.id,
            name="帳單暫存",
            purpose=WalletPurpose.CLEARING_HUB,
        )
    )
    journal = JournalService(factory)
    journal.record_opening_position(
        OpeningPositionRequest(
            financial_account_id=bank.account.id,
            balance_minor=1_000_000,
            observed_at=OBSERVED_AT,
            splits=(
                OpeningWalletSplit(wallet_name="尚未分配", amount_minor=700_000),
                OpeningWalletSplit(wallet_name="飲食", amount_minor=300_000),
                OpeningWalletSplit(wallet_name="帳單暫存", amount_minor=0),
            ),
        )
    )
    journal.record_opening_position(
        OpeningPositionRequest(
            financial_account_id=cash.account.id,
            balance_minor=50_000,
            observed_at=OBSERVED_AT,
            splits=(OpeningWalletSplit(wallet_name="尚未分配", amount_minor=50_000),),
        )
    )
    with factory() as unit:
        gmail_account = unit.accounts.get_or_create("user@example.test")
        bank_unallocated = unit.wallets.get_unallocated(bank.account.id)
        cash_unallocated = unit.wallets.get_unallocated(cash.account.id)
        assert bank_unallocated is not None
        assert cash_unallocated is not None
        food = unit.budget_categories.add(BudgetCategoryDraft(name="飲食"))
        unit.commit()
        gmail_account_id = gmail_account.id
        bank_unallocated_id = bank_unallocated.id
        cash_wallet_id = cash_unallocated.id
        food_id = food.id

    cards = ManageCards(factory)
    card = cards.register(
        RegisterCardRequest(
            gmail_account_id=gmail_account_id,
            issuer_code="test_bank",
            display_name="測試卡",
            last_four="4321",
            holder_role=CardHolderRole.PRIMARY,
        )
    )
    cards.configure(
        ConfigureCardRequest(
            card_id=card.id,
            expected_version=card.version,
            display_name="測試卡",
            holder_role=CardHolderRole.PRIMARY,
            liability_account_id=liability.account.id,
            settlement_account_id=bank.account.id,
            default_funding_wallet_id=bank_unallocated_id,
        )
    )
    return Seed(
        bank_account_id=bank.account.id,
        bank_unallocated_id=bank_unallocated_id,
        bank_food_wallet_id=food_wallet.id,
        bank_clearing_wallet_id=clearing_wallet.id,
        cash_account_id=cash.account.id,
        cash_wallet_id=cash_wallet_id,
        card_id=card.id,
        food_id=food_id,
    )


@pytest.fixture
def client(
    migrated_database: Database, database_settings: AppSettings, seed: Seed
) -> Iterator[TestClient]:
    del migrated_database, seed
    with TestClient(
        build_web_application(database_settings), base_url="http://testserver"
    ) as test_client:
        yield test_client


@pytest.fixture
def external_client(
    migrated_database: Database, database_settings: AppSettings, seed: Seed
) -> Iterator[TestClient]:
    del migrated_database, seed
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


def _local_time(instant: datetime) -> str:
    return instant.astimezone(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%dT%H:%M")


def _post(
    client: TestClient, path: str, data: dict[str, str], *, page: str = "/money/daily"
) -> Response:
    loaded = client.get(page)
    token = client.cookies.get("scribe_csrf") or _hidden(loaded.text, "csrf_token")
    return client.post(
        path,
        data={"csrf_token": token, **data},
        headers=_ORIGIN,
        follow_redirects=False,
    )


def _hidden(page: str, name: str) -> str:
    match = re.search(rf'name="{name}" value="([^"]+)"', page)
    assert match is not None, f"{name} was not rendered"
    return match.group(1)


def _cash_form(seed: Seed, **overrides: str) -> dict[str, str]:
    values = {
        "payment_method": "cash",
        "merchant": "早餐店",
        "amount": "120",
        "currency": "TWD",
        "occurred_at": _local_time(OBSERVED_AT),
        "funding_wallet_id": str(seed.cash_wallet_id),
        "budget_category_id": str(seed.food_id),
        "client_token": "web-cash-1",
    }
    values.update(overrides)
    return values


def _dedicated_card_form(seed: Seed, **overrides: str) -> dict[str, str]:
    values = {
        "payment_method": "credit_card",
        "merchant": "測試專款商店",
        "amount": "450",
        "currency": "TWD",
        "occurred_at": _local_time(OBSERVED_AT),
        "funding_wallet_id": str(seed.bank_food_wallet_id),
        "budget_category_id": str(seed.food_id),
        "client_token": "web-dedicated-1",
        "card_id": str(seed.card_id),
        "spending_treatment": "dedicated_wallet",
        "clearing_wallet_id": str(seed.bank_clearing_wallet_id),
    }
    values.update(overrides)
    return values


class TestDailyPage:
    """The daily page shows a local day of cash and card spending."""

    def test_it_renders_the_date_strip_and_the_filter_pills(
        self, client: TestClient
    ) -> None:
        page = client.get(f"/money/daily?day={LOCAL_DAY}")

        assert page.status_code == 200
        assert 'class="date-strip"' in page.text
        assert "現金" in page.text
        assert "信用卡" in page.text
        assert f'href="/money/daily?day={LOCAL_DAY}&amp;method=cash"' in page.text

    def test_a_malformed_day_is_not_found(self, client: TestClient) -> None:
        page = client.get("/money/daily?day=not-a-day")

        assert page.status_code == 404

    def test_daily_is_in_the_primary_navigation(self, client: TestClient) -> None:
        page = client.get("/money/daily")

        assert '<a href="/money/daily"' in page.text


class TestManualExpense:
    """Recording an expense posts it and redirects back to the day."""

    def test_a_cash_expense_is_recorded_and_redirects(
        self, client: TestClient, seed: Seed
    ) -> None:
        response = _post(
            client, "/money/expenses", _cash_form(seed), page="/money/expenses/new"
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/money/daily?recorded=1"
        page = client.get(f"/money/daily?day={LOCAL_DAY}")
        assert "早餐店" in page.text
        assert "TWD 120" in page.text

    def test_the_form_takes_a_display_amount_not_minor_units(
        self, client: TestClient, seed: Seed
    ) -> None:
        _post(client, "/money/expenses", _cash_form(seed), page="/money/expenses/new")

        page = client.get(f"/money/daily?day={LOCAL_DAY}")
        assert "TWD 120" in page.text
        assert "TWD 12,000" not in page.text

    def test_a_dedicated_card_expense_stays_out_of_daily_and_moves_wallets(
        self, client: TestClient, seed: Seed
    ) -> None:
        form = client.get("/money/expenses/new?mode=card")
        assert "專款小錢包 → 帳單暫存" in form.text
        assert "帳單暫存" in form.text

        response = _post(
            client,
            "/money/expenses",
            _dedicated_card_form(seed),
            page="/money/expenses/new?mode=card",
        )
        assert response.status_code == 303
        daily = client.get(f"/money/daily?day={LOCAL_DAY}")
        assert "測試專款商店" not in daily.text
        accounts = client.get(f"/money/accounts/{seed.bank_account_id}")
        assert "TWD 2,550" in accounts.text
        assert "TWD 450" in accounts.text
        assert 'href="/money/events/1"' in accounts.text
        assert "飲食,帳單暫存" in accounts.text
        detail = client.get("/money/events/1")
        assert "專款小錢包" in detail.text
        assert "測試支票帳戶 · 飲食" in detail.text
        assert "測試支票帳戶 · 帳單暫存" in detail.text
        assert "這筆不列入每日生活消費與月預算" in detail.text

    def test_monthly_treatment_cannot_smuggle_a_clearing_wallet(
        self, client: TestClient, seed: Seed
    ) -> None:
        response = _post(
            client,
            "/money/expenses",
            _dedicated_card_form(seed, spending_treatment="monthly_budget"),
            page="/money/expenses/new?mode=card",
        )
        assert response.status_code == 422
        assert "cannot name a clearing wallet" in response.text


class TestReviewedExpenseTreatment:
    """A Gmail-origin expense receives the same explicit treatment at review."""

    def test_review_can_allocate_a_dedicated_purchase(
        self,
        client: TestClient,
        seed: Seed,
        migrated_database: Database,
    ) -> None:
        def factory() -> UnitOfWork:
            return SqlAlchemyUnitOfWork(migrated_database.session_factory)

        with factory() as unit:
            event = unit.financial_events.add(
                FinancialEventDraft(
                    origin=FinancialEventOrigin.GMAIL_TRANSACTION,
                    event_type=FinancialEventType.PURCHASE,
                    payment_method=PaymentMethod.CREDIT_CARD,
                    recognition_basis=RecognitionBasis.GMAIL_NOTIFICATION,
                    occurred_at=OBSERVED_AT,
                    amount_minor=45_000,
                    currency="TWD",
                    idempotency_key="review-dedicated",
                    merchant="測試通知商店",
                    card_id=seed.card_id,
                    status=FinancialEventStatus.NEEDS_ALLOCATION,
                )
            )
            unit.commit()

        page = client.get("/money/review")
        dedicated_form = re.search(
            r'<form[^>]+class="stacked-form dedicated-allocation-form".*?</form>',
            page.text,
            flags=re.DOTALL,
        )
        assert dedicated_form is not None
        assert "列入每月生活費" in page.text
        assert "目前為第一銀行" in page.text
        assert "測試支票帳戶" in dedicated_form.group()
        assert "飲食" in dedicated_form.group()
        assert "帳單暫存" in dedicated_form.group()
        assert "皮夾" not in dedicated_form.group()
        response = client.post(
            f"/money/review/{event.id}/allocate",
            data={
                "csrf_token": _hidden(page.text, "csrf_token"),
                "version": str(event.version),
                "client_token": _hidden(page.text, "client_token"),
                "spending_treatment": "dedicated_wallet",
                "clearing_wallet_id": str(seed.bank_clearing_wallet_id),
                "currency": "TWD",
                "amount": "450",
                "budget_category_id": str(seed.food_id),
                "funding_wallet_id": str(seed.bank_food_wallet_id),
            },
            headers=_ORIGIN,
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "測試通知商店" not in client.get(f"/money/daily?day={LOCAL_DAY}").text
        with factory() as unit:
            allocations = unit.allocations.list_for_event(event.id)
            assert len(allocations) == 1
            route = unit.allocations.get_dedicated(allocations[0].id)
            assert route is not None
            assert route.clearing_wallet_id == seed.bank_clearing_wallet_id

    def test_a_scientific_amount_is_rejected(
        self, client: TestClient, seed: Seed
    ) -> None:
        response = _post(
            client,
            "/money/expenses",
            _cash_form(seed, amount="1e3"),
            page="/money/expenses/new",
        )

        assert response.status_code == 422
        assert "Traceback" not in response.text

    def test_a_cash_expense_from_a_bank_wallet_is_rejected(
        self, client: TestClient, seed: Seed
    ) -> None:
        response = _post(
            client,
            "/money/expenses",
            _cash_form(seed, funding_wallet_id=str(seed.bank_unallocated_id)),
            page="/money/expenses/new",
        )

        assert response.status_code == 422

    def test_a_missing_csrf_token_is_refused(
        self, client: TestClient, seed: Seed
    ) -> None:
        client.get("/money/expenses/new")
        response = client.post(
            "/money/expenses",
            data=_cash_form(seed),
            headers=_ORIGIN,
            follow_redirects=False,
        )

        assert response.status_code == 422

    def test_a_foreign_origin_is_refused(self, client: TestClient, seed: Seed) -> None:
        page = client.get("/money/expenses/new")
        token = client.cookies.get("scribe_csrf") or _hidden(page.text, "csrf_token")
        response = client.post(
            "/money/expenses",
            data={"csrf_token": token, **_cash_form(seed)},
            headers={"origin": "https://evil.example"},
            follow_redirects=False,
        )

        assert response.status_code == 422

    def test_a_literal_null_origin_is_accepted_like_a_missing_one(
        self, client: TestClient, seed: Seed
    ) -> None:
        """Some browsers send `Origin: null` for a same-origin top-level POST.

        The matching SameSite=Strict cookie is what actually rules out a
        cross-site attacker (including the sandboxed-iframe technique that
        produces this same literal value), so this must not be refused.
        """
        page = client.get("/money/expenses/new")
        token = client.cookies.get("scribe_csrf") or _hidden(page.text, "csrf_token")
        response = client.post(
            "/money/expenses",
            data={"csrf_token": token, **_cash_form(seed)},
            headers={"origin": "null"},
            follow_redirects=False,
        )

        assert response.status_code == 303

    def test_dangerous_input_is_escaped_and_preserved(
        self, client: TestClient, seed: Seed
    ) -> None:
        _post(
            client,
            "/money/expenses",
            _cash_form(seed, merchant="<script>alert(1)</script>"),
            page="/money/expenses/new",
        )

        page = client.get(f"/money/daily?day={LOCAL_DAY}")
        assert "<script>alert(1)</script>" not in page.text
        assert "&lt;script&gt;" in page.text


class TestEventPageAndReversal:
    """One event page can cancel what it recorded."""

    def test_an_unknown_event_is_not_found(self, client: TestClient) -> None:
        assert client.get("/money/events/999").status_code == 404

    def test_a_posted_expense_can_be_reversed(
        self, client: TestClient, seed: Seed
    ) -> None:
        _post(client, "/money/expenses", _cash_form(seed), page="/money/expenses/new")
        detail = client.get("/money/events/1")
        assert detail.status_code == 200
        version = _hidden(detail.text, "version")

        response = _post(
            client,
            "/money/events/1/reverse",
            {"version": version},
            page="/money/events/1",
        )

        assert response.status_code == 303
        assert client.get(f"/money/daily?day={LOCAL_DAY}").status_code == 200

    def test_a_stale_version_conflicts(self, client: TestClient, seed: Seed) -> None:
        _post(client, "/money/expenses", _cash_form(seed), page="/money/expenses/new")

        response = _post(
            client,
            "/money/events/1/reverse",
            {"version": "99"},
            page="/money/events/1",
        )

        assert response.status_code == 409

    def test_a_note_can_be_saved_and_is_visible_in_the_daily_list(
        self, client: TestClient, seed: Seed
    ) -> None:
        _post(client, "/money/expenses", _cash_form(seed), page="/money/expenses/new")
        detail = client.get("/money/events/1")

        response = _post(
            client,
            "/money/events/1/note",
            {"version": _hidden(detail.text, "version"), "note": "和同事吃早餐"},
            page="/money/events/1",
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/money/events/1?note_saved=1"
        saved = client.get(response.headers["location"])
        assert "備註已儲存" in saved.text
        assert ">和同事吃早餐</textarea>" in saved.text
        daily = client.get(f"/money/daily?day={LOCAL_DAY}")
        assert 'class="money-card-note"' in daily.text
        assert "和同事吃早餐" in daily.text

    def test_an_empty_note_clears_what_was_saved(
        self, client: TestClient, seed: Seed
    ) -> None:
        _post(
            client,
            "/money/expenses",
            _cash_form(seed, note="原本的備註"),
            page="/money/expenses/new",
        )
        detail = client.get("/money/events/1")

        response = _post(
            client,
            "/money/events/1/note",
            {"version": _hidden(detail.text, "version"), "note": "   "},
            page="/money/events/1",
        )

        assert response.status_code == 303
        assert "原本的備註" not in client.get(f"/money/daily?day={LOCAL_DAY}").text

    def test_a_stale_note_form_conflicts_without_overwriting(
        self, client: TestClient, seed: Seed
    ) -> None:
        _post(client, "/money/expenses", _cash_form(seed), page="/money/expenses/new")
        original = client.get("/money/events/1")
        version = _hidden(original.text, "version")
        _post(
            client,
            "/money/events/1/note",
            {"version": version, "note": "較新的備註"},
            page="/money/events/1",
        )

        stale = _post(
            client,
            "/money/events/1/note",
            {"version": version, "note": "舊頁面的備註"},
            page="/money/events/1",
        )

        assert stale.status_code == 409
        detail = client.get("/money/events/1")
        assert "較新的備註" in detail.text
        assert "舊頁面的備註" not in detail.text

    def test_an_overlong_note_returns_the_form_and_escapes_its_value(
        self, client: TestClient, seed: Seed
    ) -> None:
        _post(client, "/money/expenses", _cash_form(seed), page="/money/expenses/new")
        dangerous = '"</textarea><script>alert(1)</script>' + "字" * 501

        response = _post(
            client,
            "/money/events/1/note",
            {"version": "2", "note": dangerous},
            page="/money/events/1",
        )

        assert response.status_code == 422
        assert "無法儲存備註" in response.text
        assert "<script>alert(1)</script>" not in response.text
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text


class TestAccountsAndMovements:
    """Accounts read as one sheet column each and accept observed movements."""

    def test_the_accounts_page_is_one_column_per_account(
        self, client: TestClient, seed: Seed
    ) -> None:
        page = client.get("/money/accounts")

        assert page.status_code == 200
        assert page.text.count('class="sheet-column"') == 3
        assert "測試支票帳戶" in page.text
        assert "總計" in page.text
        assert "可用" in page.text
        assert f'action="/money/wallets/{seed.bank_food_wallet_id}/balance"' in (
            page.text
        )

    def test_an_unknown_account_is_not_found(self, client: TestClient) -> None:
        assert client.get("/money/accounts/999").status_code == 404

    def test_interest_is_recorded_as_income(
        self, client: TestClient, seed: Seed
    ) -> None:
        response = _post(
            client,
            "/money/bank-movements",
            {
                "kind": "interest_income",
                "description": "活期利息",
                "amount": "12",
                "currency": "TWD",
                "occurred_at": _local_time(OBSERVED_AT),
                "wallet_id": str(seed.bank_unallocated_id),
                "client_token": "web-interest",
            },
            page="/money/bank-movements/new",
        )

        assert response.status_code == 303
        page = client.get(f"/money/accounts/{seed.bank_account_id}")
        assert "活期利息" in page.text

    def test_a_transfer_is_recorded_without_spending(
        self, client: TestClient, seed: Seed
    ) -> None:
        response = _post(
            client,
            "/money/account-transfers",
            {
                "source_wallet_id": str(seed.bank_unallocated_id),
                "destination_wallet_id": str(seed.bank_food_wallet_id),
                "amount": "1,000",
                "currency": "TWD",
                "occurred_at": _local_time(OBSERVED_AT),
                "description": "撥到飲食",
                "client_token": "web-transfer",
            },
            page="/money/bank-movements/new",
        )

        assert response.status_code == 303
        assert client.get(f"/money/daily?day={LOCAL_DAY}").text.count("撥到飲食") == 0

    def test_a_bank_movement_form_offers_no_adjustment(
        self, client: TestClient
    ) -> None:
        page = client.get("/money/bank-movements/new")

        assert page.status_code == 200
        assert 'value="adjustment"' not in page.text

    def test_the_form_explains_the_consumed_cash_withdrawal(
        self, client: TestClient
    ) -> None:
        page = client.get("/money/bank-movements/new")

        assert 'value="cash_withdrawal_spending"' in page.text
        assert "提款並全額列入生活費" in page.text
        assert "不建立或追蹤皮夾現金" in page.text

    def test_a_withdrawal_counts_as_cash_spending_without_funding_the_wallet(
        self, client: TestClient, seed: Seed
    ) -> None:
        response = _post(
            client,
            "/money/bank-movements",
            {
                "kind": "cash_withdrawal_spending",
                "description": "提款列入生活費",
                "amount": "1,000",
                "currency": "TWD",
                "occurred_at": _local_time(OBSERVED_AT),
                "wallet_id": str(seed.bank_unallocated_id),
                "budget_category_id": str(seed.food_id),
                "client_token": "web-cash-withdrawal",
            },
            page="/money/bank-movements/new",
        )

        assert response.status_code == 303
        daily = client.get(f"/money/daily?day={LOCAL_DAY}")
        assert "提款列入生活費" in daily.text
        assert "TWD 1,000" in daily.text
        bank = client.get(f"/money/accounts/{seed.bank_account_id}")
        cash = client.get(f"/money/accounts/{seed.cash_account_id}")
        assert "TWD 9,000" in bank.text
        assert "TWD 500" in cash.text


class TestWalletSheetEditing:
    """An edited sheet cell divides one account without resizing it."""

    @staticmethod
    def _cell(page: str, wallet_id: int) -> str:
        """Return the amount the sheet currently renders for one wallet."""
        match = re.search(
            rf'id="wallet-{wallet_id}" name="target_balance"\s+value="([^"]*)"', page
        )
        assert match is not None, f"wallet {wallet_id} was not rendered as a cell"
        return match.group(1)

    def _edit(
        self, client: TestClient, seed: Seed, **overrides: str
    ) -> tuple[int, str]:
        values = {
            "counterpart_wallet_id": str(seed.bank_unallocated_id),
            "expected_balance_minor": "300000",
            "target_balance": "4,000",
            "currency": "TWD",
            "client_token": "web-cell-1",
        }
        values.update(overrides)
        response = _post(
            client,
            f"/money/wallets/{seed.bank_food_wallet_id}/balance",
            values,
            page="/money/accounts",
        )
        return response.status_code, response.text

    def test_an_edited_cell_moves_money_inside_its_own_account(
        self, client: TestClient, seed: Seed
    ) -> None:
        status, _ = self._edit(client, seed)

        assert status == 303
        page = client.get("/money/accounts").text
        assert self._cell(page, seed.bank_food_wallet_id) == "4000"
        assert self._cell(page, seed.bank_unallocated_id) == "6000"
        # The column total is what the person was reading; a split may not
        # change it.
        assert "TWD 10,000" in page

    def test_an_edit_is_recorded_as_a_transfer_rather_than_an_adjustment(
        self, client: TestClient, seed: Seed
    ) -> None:
        self._edit(client, seed)

        account = client.get(f"/money/accounts/{seed.bank_account_id}")
        assert "重分配" in account.text
        assert client.get(f"/money/daily?day={LOCAL_DAY}").text.count("重分配") == 0

    def test_a_counterpart_in_another_account_is_refused(
        self, client: TestClient, seed: Seed
    ) -> None:
        status, body = self._edit(
            client, seed, counterpart_wallet_id=str(seed.cash_wallet_id)
        )

        assert status == 422
        assert "stays inside one financial account" in body
        page = client.get("/money/accounts").text
        assert self._cell(page, seed.bank_food_wallet_id) == "3000"

    def test_a_stale_column_is_refused_instead_of_posting(
        self, client: TestClient, seed: Seed
    ) -> None:
        status, body = self._edit(client, seed, expected_balance_minor="290000")

        assert status == 422
        assert "reload and retry" in body
        page = client.get("/money/accounts").text
        assert self._cell(page, seed.bank_food_wallet_id) == "3000"

    def test_retyping_the_same_number_posts_nothing(
        self, client: TestClient, seed: Seed
    ) -> None:
        status, _ = self._edit(client, seed, target_balance="3,000")

        assert status == 303
        account = client.get(f"/money/accounts/{seed.bank_account_id}")
        assert "重分配" not in account.text

    def test_resubmitting_one_edit_moves_the_money_once(
        self, client: TestClient, seed: Seed
    ) -> None:
        self._edit(client, seed)
        status, _ = self._edit(client, seed)

        assert status == 303
        page = client.get("/money/accounts").text
        assert self._cell(page, seed.bank_food_wallet_id) == "4000"
        assert self._cell(page, seed.bank_unallocated_id) == "6000"

    def test_a_negative_envelope_can_be_typed(
        self, client: TestClient, seed: Seed
    ) -> None:
        status, _ = self._edit(client, seed, target_balance="-500")

        assert status == 303
        page = client.get("/money/accounts").text
        assert self._cell(page, seed.bank_food_wallet_id) == "-500"
        assert self._cell(page, seed.bank_unallocated_id) == "10500"

    def test_a_single_wallet_account_offers_no_cell_to_edit(
        self, client: TestClient, seed: Seed
    ) -> None:
        page = client.get("/money/accounts").text

        assert f'id="wallet-{seed.cash_wallet_id}"' not in page


class TestWalletArrangement:
    """A sheet column reads in the order its owner typed, and nothing moves."""

    @staticmethod
    def _column_order(page: str) -> list[str]:
        return re.findall(r'class="sheet-cell-label"[^>]*>([^<]+)<', page)

    def test_the_arrangement_page_lists_every_wallet_of_the_account(
        self, client: TestClient, seed: Seed
    ) -> None:
        page = client.get(f"/money/accounts/{seed.bank_account_id}/order")

        assert page.status_code == 200
        assert f'name="position_{seed.bank_food_wallet_id}"' in page.text
        assert f'name="position_{seed.bank_unallocated_id}"' in page.text
        assert f'name="position_{seed.bank_clearing_wallet_id}"' in page.text

    def test_a_saved_arrangement_reorders_the_sheet_column(
        self, client: TestClient, seed: Seed
    ) -> None:
        response = _post(
            client,
            f"/money/accounts/{seed.bank_account_id}/order",
            {
                f"position_{seed.bank_food_wallet_id}": "1",
                f"position_{seed.bank_clearing_wallet_id}": "2",
                f"position_{seed.bank_unallocated_id}": "3",
            },
            page=f"/money/accounts/{seed.bank_account_id}/order",
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/money/accounts?arranged=wallets"
        page = client.get("/money/accounts").text
        assert self._column_order(page)[:3] == ["飲食", "帳單暫存", "尚未分配"]

    def test_an_arrangement_moves_no_money(
        self, client: TestClient, seed: Seed
    ) -> None:
        before = client.get("/money/accounts").text
        _post(
            client,
            f"/money/accounts/{seed.bank_account_id}/order",
            {
                f"position_{seed.bank_food_wallet_id}": "1",
                f"position_{seed.bank_clearing_wallet_id}": "2",
                f"position_{seed.bank_unallocated_id}": "3",
            },
            page=f"/money/accounts/{seed.bank_account_id}/order",
        )
        after = client.get("/money/accounts").text

        assert before.count("TWD 10,000") == after.count("TWD 10,000")
        account = client.get(f"/money/accounts/{seed.bank_account_id}")
        assert "重分配" not in account.text

    def test_a_repeated_position_falls_back_to_the_current_order(
        self, client: TestClient, seed: Seed
    ) -> None:
        response = _post(
            client,
            f"/money/accounts/{seed.bank_account_id}/order",
            {
                f"position_{seed.bank_unallocated_id}": "1",
                f"position_{seed.bank_food_wallet_id}": "1",
                f"position_{seed.bank_clearing_wallet_id}": "1",
            },
            page=f"/money/accounts/{seed.bank_account_id}/order",
        )

        assert response.status_code == 303
        page = client.get("/money/accounts").text
        assert self._column_order(page)[:3] == ["尚未分配", "飲食", "帳單暫存"]

    def test_an_arrangement_missing_a_wallet_is_refused(
        self, client: TestClient, seed: Seed
    ) -> None:
        response = _post(
            client,
            f"/money/accounts/{seed.bank_account_id}/order",
            {f"position_{seed.bank_food_wallet_id}": "1"},
            page=f"/money/accounts/{seed.bank_account_id}/order",
        )

        assert response.status_code == 422
        assert "exactly once" in response.text

    def test_an_unknown_account_has_no_arrangement_page(
        self, client: TestClient
    ) -> None:
        assert client.get("/money/accounts/999/order").status_code == 404


class TestWalletRename:
    """Names are retyped as one change, scoped to the account on the page."""

    @staticmethod
    def _rename_page(seed: Seed) -> str:
        return f"/money/accounts/{seed.bank_account_id}/wallets/rename"

    def _form(self, client: TestClient, seed: Seed) -> dict[str, str]:
        """Return the form as rendered, versions included."""
        page = client.get(self._rename_page(seed)).text
        return {
            **{
                f"name_{wallet_id}": name
                for wallet_id, name in re.findall(
                    r'name="name_(\d+)" value="([^"]+)"', page
                )
            },
            **{
                f"version_{wallet_id}": version
                for wallet_id, version in re.findall(
                    r'name="version_(\d+)" value="([^"]+)"', page
                )
            },
        }

    def test_the_page_submits_the_version_each_name_was_shown_with(
        self, client: TestClient, seed: Seed
    ) -> None:
        form = self._form(client, seed)

        assert form[f"name_{seed.bank_food_wallet_id}"] == "飲食"
        assert f"version_{seed.bank_food_wallet_id}" in form

    def test_renaming_several_wallets_at_once_stores_all_of_them(
        self, client: TestClient, seed: Seed
    ) -> None:
        form = self._form(client, seed)
        form[f"name_{seed.bank_food_wallet_id}"] = "吃飯"
        form[f"name_{seed.bank_clearing_wallet_id}"] = "卡費暫存"

        response = _post(
            client,
            self._rename_page(seed),
            form,
            page=self._rename_page(seed),
        )

        assert response.status_code == 303
        page = client.get(f"/money/accounts/{seed.bank_account_id}").text
        assert "吃飯" in page
        assert "卡費暫存" in page

    def test_a_refused_name_leaves_every_other_name_alone(
        self, client: TestClient, seed: Seed
    ) -> None:
        """One transaction: a duplicate cannot half-apply the submission.

        Renaming them one at a time stored the earlier names and then failed,
        so the page reported an error over a change that had partly happened.
        """
        form = self._form(client, seed)
        form[f"name_{seed.bank_food_wallet_id}"] = "吃飯"
        # Already the clearing wallet's name, so the whole submission fails.
        form[f"name_{seed.bank_unallocated_id}"] = "帳單暫存"

        response = _post(
            client,
            self._rename_page(seed),
            form,
            page=self._rename_page(seed),
        )

        assert response.status_code == 422
        page = client.get(f"/money/accounts/{seed.bank_account_id}").text
        assert "吃飯" not in page
        assert "飲食" in page

    def test_two_wallets_cannot_trade_names_in_one_submission(
        self, client: TestClient, seed: Seed
    ) -> None:
        form = self._form(client, seed)
        form[f"name_{seed.bank_food_wallet_id}"] = "帳單暫存"
        form[f"name_{seed.bank_clearing_wallet_id}"] = "飲食"

        response = _post(
            client,
            self._rename_page(seed),
            form,
            page=self._rename_page(seed),
        )

        assert response.status_code == 422
        assert "trading names" in response.text
        page = client.get(f"/money/accounts/{seed.bank_account_id}").text
        assert "飲食" in page

    def test_a_wallet_of_another_account_is_refused(
        self, client: TestClient, seed: Seed
    ) -> None:
        """The form is scoped to one account and so is the command."""
        form = self._form(client, seed)
        form[f"name_{seed.cash_wallet_id}"] = "被改到別的帳戶"
        form[f"version_{seed.cash_wallet_id}"] = "1"

        response = _post(
            client,
            self._rename_page(seed),
            form,
            page=self._rename_page(seed),
        )

        assert response.status_code == 422
        assert "does not belong to this account" in response.text
        assert "被改到別的帳戶" not in client.get("/money/accounts").text

    def test_a_page_loaded_before_someone_else_renamed_is_refused(
        self, client: TestClient, seed: Seed
    ) -> None:
        stale = self._form(client, seed)
        stale[f"name_{seed.bank_food_wallet_id}"] = "後送的名字"

        fresh = self._form(client, seed)
        fresh[f"name_{seed.bank_food_wallet_id}"] = "先送的名字"
        _post(client, self._rename_page(seed), fresh, page=self._rename_page(seed))

        response = _post(
            client, self._rename_page(seed), stale, page=self._rename_page(seed)
        )

        assert response.status_code == 409
        page = client.get(f"/money/accounts/{seed.bank_account_id}").text
        assert "先送的名字" in page
        assert "後送的名字" not in page


class TestAccountArrangement:
    """The sheet's columns read in the order their owner arranged them."""

    @staticmethod
    def _column_names(page: str) -> list[str]:
        return re.findall(r'aria-label="帳戶 ([^"]+)"', page)

    def test_the_arrangement_page_lists_every_account(
        self, client: TestClient, seed: Seed
    ) -> None:
        page = client.get("/money/accounts/order")

        assert page.status_code == 200
        assert f'name="position_{seed.bank_account_id}"' in page.text
        assert f'name="position_{seed.cash_account_id}"' in page.text

    def test_a_saved_arrangement_reorders_the_columns(
        self, client: TestClient, seed: Seed
    ) -> None:
        before = self._column_names(client.get("/money/accounts").text)
        assert before[0] == "測試支票帳戶"

        response = _post(
            client,
            "/money/accounts/order",
            {
                f"position_{seed.cash_account_id}": "1",
                f"position_{seed.bank_account_id}": "2",
                "position_3": "3",
            },
            page="/money/accounts/order",
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/money/accounts?arranged=accounts"
        assert self._column_names(client.get("/money/accounts").text)[:2] == [
            "皮夾",
            "測試支票帳戶",
        ]

    def test_an_arrangement_missing_an_account_is_refused(
        self, client: TestClient, seed: Seed
    ) -> None:
        response = _post(
            client,
            "/money/accounts/order",
            {f"position_{seed.bank_account_id}": "1"},
            page="/money/accounts/order",
        )

        assert response.status_code == 422
        assert "exactly once" in response.text

    def test_a_new_account_joins_the_end_of_the_sheet(
        self, client: TestClient, migrated_database: Database
    ) -> None:
        def factory() -> UnitOfWork:
            return SqlAlchemyUnitOfWork(migrated_database.session_factory)

        LedgerSetupService(factory).open_account(
            OpenFinancialAccountRequest(
                institution_code="later_bank",
                display_name="後來的銀行",
                account_type=FinancialAccountType.CHECKING,
                currency="TWD",
            )
        )

        assert self._column_names(client.get("/money/accounts").text)[-1] == (
            "後來的銀行"
        )

    def test_the_sheet_fills_its_width_without_a_horizontal_scrollbar(
        self, client: TestClient
    ) -> None:
        """Columns wrap and share the width; a strip would hide accounts."""
        page = client.get("/money/accounts").text

        assert 'class="ledger-sheet"' in page
        # The page opts out of the prose reading column, or six columns would
        # be squeezed into 1180px and wrap into a second band.
        assert 'class="shell shell-wide"' in page
        stylesheet = client.get("/static/styles.css").text
        # auto-fit, not auto-fill: auto-fill leaves the empty tracks that made
        # the columns huddle on the left instead of sharing the full width.
        assert "grid-template-columns: repeat(auto-fit, minmax(12rem, 1fr))" in (
            stylesheet
        )
        assert "overflow-x: auto" not in stylesheet.split(".ledger-sheet-wrap")[1][:120]


class TestReconcile:
    """Observations are recorded, compared as-of, and adjusted with a reason."""

    def test_an_observation_is_recorded_and_shown(
        self, client: TestClient, seed: Seed
    ) -> None:
        response = _post(
            client,
            "/money/reconcile/snapshots",
            {
                "financial_account_id": str(seed.bank_account_id),
                "observed_balance": "10,500",
                "observed_at": _local_time(OBSERVED_AT),
                "client_token": "web-snap",
            },
            page="/money/reconcile",
        )

        assert response.status_code == 303
        page = client.get("/money/reconcile")
        assert "TWD 10,500" in page.text
        assert "TWD 500" in page.text

    def test_a_negative_observation_is_accepted(
        self, client: TestClient, seed: Seed
    ) -> None:
        response = _post(
            client,
            "/money/reconcile/snapshots",
            {
                "financial_account_id": str(seed.bank_account_id),
                "observed_balance": "-1,000",
                "observed_at": _local_time(OBSERVED_AT),
                "client_token": "web-snap-negative",
            },
            page="/money/reconcile",
        )

        assert response.status_code == 303

    def test_a_reasoned_adjustment_closes_the_difference(
        self, client: TestClient, seed: Seed
    ) -> None:
        _post(
            client,
            "/money/reconcile/snapshots",
            {
                "financial_account_id": str(seed.bank_account_id),
                "observed_balance": "10,500",
                "observed_at": _local_time(OBSERVED_AT),
                "client_token": "web-snap",
            },
            page="/money/reconcile",
        )
        page = client.get("/money/reconcile")
        difference = _hidden(page.text, "expected_difference")

        response = _post(
            client,
            "/money/reconcile/1/adjust",
            {
                "expected_difference": difference,
                "reason": "找不到來源",
                "client_token": "web-adjust",
            },
            page="/money/reconcile",
        )

        assert response.status_code == 303
        assert "TWD 0" in client.get("/money/reconcile").text

    def test_a_stale_difference_conflicts(self, client: TestClient, seed: Seed) -> None:
        _post(
            client,
            "/money/reconcile/snapshots",
            {
                "financial_account_id": str(seed.bank_account_id),
                "observed_balance": "10,500",
                "observed_at": _local_time(OBSERVED_AT),
                "client_token": "web-snap",
            },
            page="/money/reconcile",
        )

        response = _post(
            client,
            "/money/reconcile/1/adjust",
            {
                "expected_difference": "1",
                "reason": "舊的差額",
                "client_token": "web-adjust",
            },
            page="/money/reconcile",
        )

        assert response.status_code == 409


class TestSourceLinks:
    """The matching page shows evidence, never Email bodies."""

    def test_it_renders_with_nothing_pending(self, client: TestClient) -> None:
        page = client.get("/money/source-links")

        assert page.status_code == 200
        assert "沒有待確認的比對" in page.text

    def test_an_unknown_link_is_rejected_without_a_traceback(
        self, client: TestClient
    ) -> None:
        response = _post(
            client,
            "/money/source-links/999/confirm",
            {},
            page="/money/source-links",
        )

        assert response.status_code == 422
        assert "Traceback" not in response.text


class TestLocalOnly:
    """Every B7 surface is absent from the external hostname (ADR-B068)."""

    @pytest.mark.parametrize("path", LOCAL_ONLY_GETS)
    def test_external_reads_are_not_found(
        self, external_client: TestClient, path: str
    ) -> None:
        response = external_client.get(path, follow_redirects=False)

        assert response.status_code == 404

    @pytest.mark.parametrize("path", LOCAL_ONLY_POSTS)
    def test_external_writes_are_not_found(
        self, external_client: TestClient, path: str
    ) -> None:
        response = external_client.post(path, data={}, follow_redirects=False)

        assert response.status_code == 404

    def test_the_navigation_hides_the_money_links_externally(
        self, external_client: TestClient
    ) -> None:
        page = external_client.get("/")

        assert '<a href="/money/daily"' not in page.text
        assert '<a href="/money/accounts"' not in page.text


class TestNoSensitiveDisclosure:
    """Errors and pages never leak amounts, merchants, or notes."""

    def test_a_rejected_expense_message_carries_no_amount_or_merchant(
        self, client: TestClient, seed: Seed
    ) -> None:
        """The form legitimately echoes what the user typed; the error may not.

        Returning the merchant and amount to the same local browser that just
        submitted them is the point of preserving safe input. What must never
        carry them is the explanation, because that is the string a screenshot,
        a bug report, or a future log line would repeat.
        """
        response = _post(
            client,
            "/money/expenses",
            _cash_form(
                seed,
                merchant="秘密商家",
                amount="9,999",
                funding_wallet_id=str(seed.bank_unallocated_id),
            ),
            page="/money/expenses/new",
        )

        assert response.status_code == 422
        banner = re.search(
            r'<p class="notice notice-error" role="alert">(.*?)</p>', response.text
        )
        assert banner is not None
        message = banner.group(1)
        assert "秘密商家" not in message
        assert "9,999" not in message
        assert "9999" not in message
        assert "Traceback" not in response.text

    def test_the_matching_page_shows_no_email_body(self, client: TestClient) -> None:
        page = client.get("/money/source-links")

        assert "body_text" not in page.text
        assert "body_html" not in page.text


class TestMobileLayout:
    """The pages are built for a phone, not for a wide table."""

    @pytest.mark.parametrize(
        "path",
        ["/money", "/money/daily", "/money/accounts", "/money/reconcile"],
    )
    def test_the_pages_use_cards_rather_than_a_wide_table(
        self, client: TestClient, path: str
    ) -> None:
        page = client.get(path)

        assert page.status_code == 200
        assert "<table" not in page.text

    def test_the_stylesheet_stacks_every_grid_at_phone_width(self) -> None:
        from pathlib import Path

        css = Path("app/presentation/web/static/styles.css").read_text(encoding="utf-8")
        phone = css.split("@media (max-width: 620px)")[-1]

        assert ".money-links { grid-template-columns: 1fr; }" in phone
        assert ".total-grid { grid-template-columns: 1fr; }" in phone
        # Touch targets: every interactive control is at least 44px tall.
        assert "min-height: 44px" in css

    def test_the_viewport_is_declared_once_for_every_page(
        self, client: TestClient
    ) -> None:
        page = client.get("/money/daily")

        assert (
            page.text.count(
                '<meta name="viewport" content="width=device-width, initial-scale=1">'
            )
            == 1
        )


def test_a_recorded_expense_appears_with_its_status(
    client: TestClient, seed: Seed
) -> None:
    _post(client, "/money/expenses", _cash_form(seed), page="/money/expenses/new")

    page = client.get("/money/events/1")

    assert "已入帳" in page.text


class TestExpenseFormStrictness:
    """The form decodes explicitly and keeps what was safely submitted."""

    def test_an_unknown_payment_method_is_refused(
        self, client: TestClient, seed: Seed
    ) -> None:
        response = _post(
            client,
            "/money/expenses",
            _cash_form(seed, payment_method="bank_account"),
            page="/money/expenses/new",
        )

        assert response.status_code == 422
        assert "cash or credit_card" in response.text

    def test_an_unknown_payment_method_never_posts_as_cash(
        self, client: TestClient, seed: Seed
    ) -> None:
        _post(
            client,
            "/money/expenses",
            _cash_form(seed, payment_method="credit_cardX"),
            page="/money/expenses/new",
        )

        assert client.get("/money/events/1").status_code == 404

    def test_a_missing_category_is_refused(
        self, client: TestClient, seed: Seed
    ) -> None:
        response = _post(
            client,
            "/money/expenses",
            _cash_form(seed, budget_category_id=""),
            page="/money/expenses/new",
        )

        assert response.status_code == 422
        assert client.get("/money/events/1").status_code == 404

    def test_the_form_offers_no_uncategorized_option(self, client: TestClient) -> None:
        page = client.get("/money/expenses/new")

        assert "未分類" not in page.text
        assert 'name="budget_category_id" required' in page.text

    def test_a_rejected_submission_returns_the_form_with_its_values(
        self, client: TestClient, seed: Seed
    ) -> None:
        response = _post(
            client,
            "/money/expenses",
            _cash_form(seed, merchant="早餐店", amount="1e3", note="現金付的"),
            page="/money/expenses/new",
        )

        assert response.status_code == 422
        assert 'action="/money/expenses"' in response.text
        assert 'value="早餐店"' in response.text
        assert 'value="1e3"' in response.text
        assert 'value="現金付的"' in response.text
        assert f'value="{seed.cash_wallet_id}" selected' in response.text
        assert f'value="{seed.food_id}" selected' in response.text

    def test_a_rejected_submission_escapes_what_it_echoes(
        self, client: TestClient, seed: Seed
    ) -> None:
        response = _post(
            client,
            "/money/expenses",
            _cash_form(seed, merchant='"><script>alert(1)</script>', amount="1e3"),
            page="/money/expenses/new",
        )

        assert response.status_code == 422
        assert "<script>alert(1)</script>" not in response.text
        assert "&lt;script&gt;" in response.text

    def test_a_rejected_card_submission_returns_the_card_form(
        self, client: TestClient, seed: Seed
    ) -> None:
        response = _post(
            client,
            "/money/expenses",
            _cash_form(
                seed,
                payment_method="credit_card",
                card_id=str(seed.card_id),
                amount="1e3",
            ),
            page="/money/expenses/new?mode=card",
        )

        assert response.status_code == 422
        assert 'name="card_id"' in response.text
        assert 'value="credit_card"' in response.text


class TestReconcileOffersOnlySettleableAccounts:
    """A card liability can never receive an unfixable observation."""

    def test_the_snapshot_form_lists_no_card_liability(
        self, client: TestClient
    ) -> None:
        page = client.get("/money/reconcile")

        assert page.status_code == 200
        assert "測試支票帳戶" in page.text
        assert "測試信用卡負債" not in page.text


def test_a_new_wallet_joins_the_end_of_its_column(
    client: TestClient, seed: Seed, migrated_database: Database
) -> None:
    """A wallet added later must not jump into the middle of an arrangement."""

    def factory() -> UnitOfWork:
        return SqlAlchemyUnitOfWork(migrated_database.session_factory)

    LedgerSetupService(factory).add_wallet(
        AddWalletRequest(financial_account_id=seed.bank_account_id, name="旅遊")
    )

    page = client.get("/money/accounts").text
    labels = re.findall(r'class="sheet-cell-label"[^>]*>([^<]+)<', page)
    assert labels[:4] == ["尚未分配", "飲食", "帳單暫存", "旅遊"]

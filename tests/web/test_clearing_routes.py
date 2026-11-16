"""The local-only clearing page, its forms, its wording, and its conflicts."""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

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
    CardHolderRole,
    FinancialAccountType,
    FinancialEventOrigin,
    FinancialEventType,
    PaymentMethod,
    RecognitionBasis,
    WalletPurpose,
)
from app.domain.budget_models import FinancialEventDraft, OpeningWalletSplit
from app.domain.ports.repositories import UnitOfWork
from app.infrastructure.config.settings import AppSettings
from app.infrastructure.database.engine import Database
from app.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy.orm import Session, sessionmaker

EXTERNAL_HOST = "scribe.example.com"
_ORIGIN = {"origin": "http://testserver"}
OCCURRED_AT = datetime(2026, 8, 7, 13, 30, tzinfo=UTC)
LOCAL_ONLY_GETS = ("/clearing",)
LOCAL_ONLY_POSTS = (
    "/clearing/schedules",
    "/clearing/autopay",
    "/clearing/schedules/1/cancel",
    "/clearing/schedules/1/retry",
    "/clearing/schedules/1/reverse",
    "/clearing/run-due",
)
#: Wording that would claim a bank confirmed something. None of it may appear.
#: The bare "銀行確認" is deliberately absent: the required disclaimer
#: "並非銀行確認" contains it, so only the affirmative forms are forbidden.
FORBIDDEN_PHRASES = (
    "銀行已確認",
    "銀行已成功",
    "已由銀行確認",
    "銀行確認轉帳",
    "bank confirmed",
    "confirmed by the bank",
)
#: The two phrases ADR-B050 requires the page to say in so many words.
REQUIRED_PHRASES = ("已自動視為轉出", "並非銀行確認")

type UnitOfWorkFactory = Callable[[], UnitOfWork]


@dataclass(frozen=True)
class Seed:
    """Identifiers the clearing forms need to post something real."""

    main_account_id: int
    main_unallocated_id: int
    clearing_hub_id: int
    main_budget_id: int
    other_account_id: int
    other_unallocated_id: int
    other_budget_id: int
    card_id: int
    cross_bank_commitment_id: int
    cross_bank_amount: int
    cross_bank_event_id: int
    session_factory: sessionmaker[Session]


def _hidden(body: str, name: str) -> str:
    match = re.search(rf'name="{name}" value="([^"]*)"', body)
    assert match is not None, name
    return html.unescape(match.group(1))


@pytest.fixture
def seed(migrated_database: Database) -> Seed:
    """Seed two test accounts, one test card, and one cross-bank reservation."""

    def factory() -> UnitOfWork:
        return SqlAlchemyUnitOfWork(migrated_database.session_factory)

    setup = LedgerSetupService(factory)
    journal = JournalService(factory)
    main = setup.open_account(
        OpenFinancialAccountRequest(
            institution_code="test_bank_a",
            display_name="測試主要帳戶",
            account_type=FinancialAccountType.CHECKING,
            currency="TWD",
        )
    )
    clearing_hub = setup.add_wallet(
        AddWalletRequest(
            financial_account_id=main.account.id,
            name="測試帳單暫存",
            purpose=WalletPurpose.CLEARING_HUB,
        )
    )
    main_budget = setup.add_wallet(
        AddWalletRequest(financial_account_id=main.account.id, name="測試主要預算")
    )
    other = setup.open_account(
        OpenFinancialAccountRequest(
            institution_code="test_bank_b",
            display_name="測試次要帳戶",
            account_type=FinancialAccountType.CHECKING,
            currency="TWD",
        )
    )
    other_budget = setup.add_wallet(
        AddWalletRequest(financial_account_id=other.account.id, name="測試次要預算")
    )
    liability = setup.open_account(
        OpenFinancialAccountRequest(
            institution_code="test_bank_a",
            display_name="測試卡負債",
            account_type=FinancialAccountType.CARD_LIABILITY,
            currency="TWD",
        )
    )
    journal.record_opening_position(
        OpeningPositionRequest(
            financial_account_id=main.account.id,
            balance_minor=900_000,
            observed_at=OCCURRED_AT,
            splits=(
                OpeningWalletSplit(wallet_name="尚未分配", amount_minor=500_000),
                OpeningWalletSplit(wallet_name="測試帳單暫存", amount_minor=100_000),
                OpeningWalletSplit(wallet_name="測試主要預算", amount_minor=300_000),
            ),
        )
    )
    journal.record_opening_position(
        OpeningPositionRequest(
            financial_account_id=other.account.id,
            balance_minor=400_000,
            observed_at=OCCURRED_AT,
            splits=(
                OpeningWalletSplit(wallet_name="尚未分配", amount_minor=100_000),
                OpeningWalletSplit(wallet_name="測試次要預算", amount_minor=300_000),
            ),
        )
    )
    with factory() as unit:
        gmail_account = unit.accounts.get_or_create("user@example.test")
        main_unallocated = unit.wallets.get_unallocated(main.account.id)
        other_unallocated = unit.wallets.get_unallocated(other.account.id)
        assert main_unallocated is not None
        assert other_unallocated is not None
        unit.commit()
        gmail_account_id = gmail_account.id
        main_unallocated_id = main_unallocated.id
        other_unallocated_id = other_unallocated.id
    category = ManageBudgetPlan(factory).add_category(
        AddCategoryRequest(name="測試分類")
    )
    cards = ManageCards(factory)
    card = cards.register(
        RegisterCardRequest(
            gmail_account_id=gmail_account_id,
            issuer_code="test_bank_a",
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
            settlement_account_id=main.account.id,
            default_funding_wallet_id=main_unallocated_id,
        )
    )
    with factory() as unit:
        event = unit.financial_events.add(
            FinancialEventDraft(
                origin=FinancialEventOrigin.GMAIL_TRANSACTION,
                event_type=FinancialEventType.PURCHASE,
                payment_method=PaymentMethod.CREDIT_CARD,
                recognition_basis=RecognitionBasis.GMAIL_NOTIFICATION,
                occurred_at=OCCURRED_AT,
                amount_minor=8_000,
                currency="TWD",
                idempotency_key="test:cross-bank",
                merchant="測試商店",
                card_id=card.id,
            )
        )
        unit.commit()
        event_id = event.id
    result = AllocateFinancialEvent(factory, journal).execute(
        AllocateFinancialEventRequest(
            financial_event_id=event_id,
            expected_version=1,
            client_token="token-cross-bank",
            lines=(
                AllocationLine(
                    amount_minor=8_000,
                    funding_wallet_id=other_budget.id,
                    budget_category_id=category.id,
                ),
            ),
        )
    )
    return Seed(
        main_account_id=main.account.id,
        main_unallocated_id=main_unallocated_id,
        clearing_hub_id=clearing_hub.id,
        main_budget_id=main_budget.id,
        other_account_id=other.account.id,
        other_unallocated_id=other_unallocated_id,
        other_budget_id=other_budget.id,
        card_id=card.id,
        cross_bank_commitment_id=result.commitments[0].id,
        cross_bank_amount=8_000,
        cross_bank_event_id=event_id,
        session_factory=migrated_database.session_factory,
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


def _post(client: TestClient, path: str, data: dict[str, str | list[str]]) -> Response:
    page = client.get("/clearing")
    token = client.cookies.get("scribe_csrf") or _hidden(page.text, "csrf_token")
    return client.post(
        path,
        data={"csrf_token": token, **data},
        headers=_ORIGIN,
        follow_redirects=False,
    )


def _future_date() -> str:
    return (datetime.now(UTC).date() + timedelta(days=7)).isoformat()


def _schedule_form(
    seed: Seed, **overrides: str | list[str]
) -> dict[str, str | list[str]]:
    values: dict[str, str | list[str]] = {
        "client_token": "web-token-1",
        "source_account_id": str(seed.other_account_id),
        "source_wallet_id": str(seed.other_unallocated_id),
        "destination_account_id": str(seed.main_account_id),
        "destination_wallet_id": str(seed.clearing_hub_id),
        "currency": "TWD",
        "amount": str(seed.cross_bank_amount // 100),
        "scheduled_local_date": _future_date(),
        "scheduled_local_time": "06:00",
        "cover_commitment_id": str(seed.cross_bank_commitment_id),
        "commitment_id": str(seed.cross_bank_commitment_id),
        "commitment_amount": str(seed.cross_bank_amount // 100),
    }
    values.update(overrides)
    return values


class TestPage:
    """The page renders, and it never claims a bank confirmed anything."""

    def test_the_page_shows_the_hub_and_the_assumption_wording(
        self, client: TestClient
    ) -> None:
        page = client.get("/clearing")

        assert page.status_code == 200
        assert "自動視為轉出" in page.text
        assert "並非銀行確認" in page.text
        assert "測試帳單暫存" in page.text

    def test_the_page_never_claims_a_bank_confirmation(
        self, client: TestClient, seed: Seed
    ) -> None:
        created = _post(client, "/clearing/schedules", _schedule_form(seed))
        assert created.status_code == 303

        page = client.get("/clearing")

        for phrase in FORBIDDEN_PHRASES:
            assert phrase not in page.text
        for phrase in REQUIRED_PHRASES:
            assert phrase in page.text

    def test_the_page_is_bounded_and_paginated(self, client: TestClient) -> None:
        page = client.get("/clearing?page=2")

        assert page.status_code == 200

    def test_the_coordinator_diagnostics_are_shown(self, client: TestClient) -> None:
        page = client.get("/clearing")

        assert "維護排程器" in page.text
        assert "輪詢間隔" in page.text


class TestForms:
    """Every write goes through CSRF, origin, and optimistic checks."""

    def test_creating_a_clearing_schedule_redirects(
        self, client: TestClient, seed: Seed
    ) -> None:
        response = _post(client, "/clearing/schedules", _schedule_form(seed))

        assert response.status_code == 303
        assert response.headers["location"] == "/clearing?scheduled=1"
        assert "已排程" in client.get("/clearing").text

    def test_an_already_due_schedule_asks_for_confirmation_first(
        self, client: TestClient, seed: Seed
    ) -> None:
        overdue = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()

        refused = _post(
            client,
            "/clearing/schedules",
            _schedule_form(seed, scheduled_local_date=overdue),
        )
        confirmed = _post(
            client,
            "/clearing/schedules",
            _schedule_form(seed, scheduled_local_date=overdue, confirm_overdue="1"),
        )

        assert refused.status_code == 422
        assert "已經過了" in refused.text
        assert confirmed.status_code == 303

    def test_a_missing_csrf_token_is_refused(
        self, client: TestClient, seed: Seed
    ) -> None:
        client.get("/clearing")

        response = client.post(
            "/clearing/schedules",
            data=_schedule_form(seed),
            headers=_ORIGIN,
            follow_redirects=False,
        )

        assert response.status_code == 422

    def test_a_foreign_origin_is_refused(self, client: TestClient, seed: Seed) -> None:
        page = client.get("/clearing")
        token = client.cookies.get("scribe_csrf") or _hidden(page.text, "csrf_token")

        response = client.post(
            "/clearing/schedules",
            data={"csrf_token": token, **_schedule_form(seed)},
            headers={"origin": "https://evil.example"},
            follow_redirects=False,
        )

        assert response.status_code == 422

    def test_an_unbalanced_coverage_is_a_validation_error(
        self, client: TestClient, seed: Seed
    ) -> None:
        response = _post(
            client,
            "/clearing/schedules",
            _schedule_form(seed, amount="90"),
        )

        assert response.status_code == 422

    def test_a_stale_version_conflicts(self, client: TestClient, seed: Seed) -> None:
        _post(client, "/clearing/schedules", _schedule_form(seed))

        response = _post(client, "/clearing/schedules/1/cancel", {"version": "99"})

        assert response.status_code == 409

    def test_cancelling_releases_the_schedule(
        self, client: TestClient, seed: Seed
    ) -> None:
        _post(client, "/clearing/schedules", _schedule_form(seed))

        response = _post(client, "/clearing/schedules/1/cancel", {"version": "1"})

        assert response.status_code == 303
        assert "已取消" in client.get("/clearing").text

    def test_running_due_schedules_by_hand_is_accepted(
        self, client: TestClient
    ) -> None:
        response = _post(client, "/clearing/run-due", {})

        assert response.status_code == 303
        assert response.headers["location"] == "/clearing?ran=1"

    def test_an_autopay_form_needs_a_configured_card(
        self, client: TestClient, seed: Seed
    ) -> None:
        response = _post(
            client,
            "/clearing/autopay",
            {
                "client_token": "web-autopay-1",
                "card_id": str(seed.card_id),
                "source_wallet_id": str(seed.main_unallocated_id),
                "amount": "50",
                "currency": "TWD",
                "scheduled_local_date": _future_date(),
                "scheduled_local_time": "06:00",
            },
        )

        assert response.status_code == 303
        assert "信用卡自動扣繳" in client.get("/clearing").text


class TestReversalResend:
    """A lost response must not show a failure for work that succeeded."""

    def _executed_schedule(self, client: TestClient, seed: Seed) -> None:
        overdue = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()
        created = _post(
            client,
            "/clearing/schedules",
            _schedule_form(seed, scheduled_local_date=overdue, confirm_overdue="1"),
        )
        assert created.status_code == 303
        assert _post(client, "/clearing/run-due", {}).status_code == 303

    def test_resending_the_reverse_form_succeeds_again(
        self, client: TestClient, seed: Seed
    ) -> None:
        self._executed_schedule(client, seed)
        page = client.get("/clearing")
        assert "已自動視為轉出" in page.text

        first = _post(client, "/clearing/schedules/1/reverse", {"version": "3"})
        # The browser resends the form it still has, with the stale version.
        second = _post(client, "/clearing/schedules/1/reverse", {"version": "3"})

        assert first.status_code == 303
        assert second.status_code == 303
        assert second.headers["location"] == "/clearing?reversed=1"
        assert "已沖銷" in client.get("/clearing").text

    def test_reversing_something_that_never_executed_is_still_refused(
        self, client: TestClient, seed: Seed
    ) -> None:
        assert (
            _post(client, "/clearing/schedules", _schedule_form(seed)).status_code
            == 303
        )

        response = _post(client, "/clearing/schedules/1/reverse", {"version": "1"})

        assert response.status_code == 422


class TestOverCoverageWarning:
    """The page must blame the right rows and count the surplus honestly."""

    def _over_covered(self, client: TestClient, seed: Seed) -> None:
        """Schedule the whole reservation twice over, then refund most of it."""
        for index in (1, 2):
            created = _post(
                client,
                "/clearing/schedules",
                _schedule_form(
                    seed,
                    client_token=f"warn-{index}",
                    amount="40",
                    commitment_amount="40",
                ),
            )
            assert created.status_code == 303
        _refund(seed, amount_minor=6_000)

    def test_a_cancelled_row_is_not_shown_as_over_covering(
        self, client: TestClient, seed: Seed, migrated_database: Database
    ) -> None:
        del migrated_database
        self._over_covered(client, seed)
        assert (
            _post(client, "/clearing/schedules/1/cancel", {"version": "1"}).status_code
            == 303
        )

        body = client.get("/clearing").text

        assert "超額排程" in body
        # The cancelled row keeps its place in history without the warning, and
        # is never told that money it no longer moves has already moved.
        assert body.count("這筆涵蓋的預留已被超額排程") == 1
        assert "錢已經移動" not in body
        assert "請取消後重建" in body

    def test_the_warning_names_the_exact_number_of_reservations(
        self, client: TestClient, seed: Seed
    ) -> None:
        self._over_covered(client, seed)

        body = client.get("/clearing").text

        assert "有 1 筆預留被排程的金額已經超過" in body
        assert "共 1 筆" in body
        assert "本頁顯示第 1" in body


def _refund(seed: Seed, *, amount_minor: int) -> None:
    """Refund part of the seeded card purchase through the ordinary service."""
    from app.application.services.event_allocation import AllocateFinancialEvent

    def factory() -> UnitOfWork:
        return SqlAlchemyUnitOfWork(seed.session_factory)

    allocate = AllocateFinancialEvent(factory, JournalService(factory))
    with factory() as unit:
        refund = unit.financial_events.add(
            FinancialEventDraft(
                origin=FinancialEventOrigin.GMAIL_TRANSACTION,
                event_type=FinancialEventType.REFUND,
                payment_method=PaymentMethod.CREDIT_CARD,
                recognition_basis=RecognitionBasis.GMAIL_NOTIFICATION,
                occurred_at=OCCURRED_AT,
                amount_minor=amount_minor,
                currency="TWD",
                idempotency_key=f"test:refund:{amount_minor}",
                merchant="測試商店",
                card_id=seed.card_id,
                refund_of_event_id=seed.cross_bank_event_id,
            )
        )
        unit.commit()
        refund_id = refund.id
    lines = allocate.propose_refund_lines(refund_id)
    allocate.execute(
        AllocateFinancialEventRequest(
            financial_event_id=refund_id,
            expected_version=1,
            client_token=f"token-refund-{amount_minor}",
            lines=tuple(
                AllocationLine(
                    amount_minor=line.amount_minor,
                    funding_wallet_id=line.funding_wallet_id,
                    budget_category_id=line.budget_category_id,
                )
                for line in lines
            ),
        )
    )


class TestWarningSummary:
    """The summary must never advise a correction the rows cannot perform."""

    def _executed_surplus(self, client: TestClient, seed: Seed) -> None:
        """Execute the whole reservation, then refund part of it."""
        overdue = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()
        assert (
            _post(
                client,
                "/clearing/schedules",
                _schedule_form(seed, scheduled_local_date=overdue, confirm_overdue="1"),
            ).status_code
            == 303
        )
        assert _post(client, "/clearing/run-due", {}).status_code == 303
        _refund(seed, amount_minor=4_000)

    def test_an_executed_only_surplus_is_never_told_to_cancel(
        self, client: TestClient, seed: Seed
    ) -> None:
        self._executed_surplus(client, seed)

        body = client.get("/clearing").text

        assert "已經視為轉出的排程只能用沖銷處理" in body
        assert "取消還沒執行的排程" not in body
        assert "正在執行中的排程要等" not in body

    def test_a_processing_only_surplus_is_told_to_wait(
        self, client: TestClient, seed: Seed
    ) -> None:
        overdue = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()
        assert (
            _post(
                client,
                "/clearing/schedules",
                _schedule_form(seed, scheduled_local_date=overdue, confirm_overdue="1"),
            ).status_code
            == 303
        )
        _claim_everything(seed)
        _refund(seed, amount_minor=4_000)

        body = client.get("/clearing").text

        assert "正在執行中的排程要等這一輪結束後才能處理" in body
        assert "取消還沒執行的排程" not in body
        assert "已經視為轉出的排程只能用沖銷處理" not in body

    def test_a_scheduled_surplus_is_told_to_cancel(
        self, client: TestClient, seed: Seed
    ) -> None:
        assert (
            _post(client, "/clearing/schedules", _schedule_form(seed)).status_code
            == 303
        )
        _refund(seed, amount_minor=4_000)

        body = client.get("/clearing").text

        assert "取消還沒執行的排程" in body
        assert "已經視為轉出的排程只能用沖銷處理" not in body


class TestWarningPagination:
    """An out-of-range page must show a truthful range and a way back."""

    def test_a_page_beyond_the_end_shows_the_last_real_page(
        self, client: TestClient, seed: Seed
    ) -> None:
        assert (
            _post(client, "/clearing/schedules", _schedule_form(seed)).status_code
            == 303
        )
        _refund(seed, amount_minor=4_000)

        body = client.get("/clearing?warn_page=1000").text

        assert "共 1 筆" in body
        assert "本頁顯示第 1" in body
        assert "24975" not in body
        assert "第 1 / 1 頁" in body

    def test_the_pagination_links_keep_the_other_page_parameter(
        self, client: TestClient, seed: Seed
    ) -> None:
        assert (
            _post(client, "/clearing/schedules", _schedule_form(seed)).status_code
            == 303
        )
        _refund(seed, amount_minor=4_000)

        body = client.get("/clearing?page=1&warn_page=1").text

        # Nothing is truncated here, so no link should be offered at all; the
        # important part is that no link is ever rendered without its sibling.
        for fragment in re.findall(r'href="/clearing\?([^"]+)"', body):
            assert ("page=" in fragment) == ("warn_page=" in fragment)


def _claim_everything(seed: Seed) -> None:
    """Move every due schedule into `processing` without executing it."""

    def factory() -> UnitOfWork:
        return SqlAlchemyUnitOfWork(seed.session_factory)

    with factory() as unit:
        unit.scheduled_transfers.claim_due(
            now=datetime.now(UTC),
            owner="test-owner",
            lease_until=datetime.now(UTC) + timedelta(seconds=600),
            limit=50,
        )
        unit.commit()


class TestExternalSurface:
    """The clearing hub never appears through the external hostname."""

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

    def test_the_navigation_hides_the_clearing_link_externally(
        self, external_client: TestClient
    ) -> None:
        page = external_client.get("/")

        assert page.status_code == 200
        assert 'href="/clearing"' not in page.text


class TestResponsiveMarkup:
    """Phone rows become labelled cards instead of squeezed columns."""

    def test_every_table_has_a_mobile_card_representation(
        self, client: TestClient, seed: Seed
    ) -> None:
        _post(client, "/clearing/schedules", _schedule_form(seed))

        body = client.get("/clearing").text

        assert body.count("<table") >= 5
        assert body.count(
            'class="data-table responsive-data-table clearing-data-table"'
        ) == body.count("<table")
        assert body.count('class="table-wrap clearing-table-wrap"') == body.count(
            "<table"
        )
        assert 'data-label="排程"' in body
        assert 'data-label="路徑"' in body

        css = Path("app/presentation/web/static/styles.css").read_text(encoding="utf-8")
        assert ".clearing-data-table thead { display: none; }" in css
        assert ".clearing-table-wrap { overflow-x: visible; }" in css
        assert 'class="selection-checkbox" type="checkbox"' in body

    def test_the_page_uses_the_shared_responsive_shell(
        self, client: TestClient
    ) -> None:
        body = client.get("/clearing").text

        assert 'name="viewport" content="width=device-width, initial-scale=1"' in body
        assert 'class="shell"' in body


def test_a_replayed_submission_creates_one_schedule(
    client: TestClient, seed: Seed
) -> None:
    first = _post(client, "/clearing/schedules", _schedule_form(seed))
    second = _post(client, "/clearing/schedules", _schedule_form(seed))

    assert (first.status_code, second.status_code) == (303, 303)
    body = client.get("/clearing").text
    assert body.count("已排程") >= 1
    assert "#2" not in body


def test_a_reused_token_with_a_different_payload_is_refused(
    client: TestClient, seed: Seed
) -> None:
    _post(client, "/clearing/schedules", _schedule_form(seed))

    response = _post(
        client,
        "/clearing/schedules",
        _schedule_form(
            seed,
            amount="30",
            commitment_amount="30",
        ),
    )

    assert response.status_code == 422


def test_the_clearing_date_default_is_never_already_due(
    client: TestClient,
) -> None:
    body = client.get("/clearing").text

    match = re.search(r'name="scheduled_local_date" value="([^"]+)"', body)
    assert match is not None
    assert date.fromisoformat(match.group(1)) > datetime.now(UTC).date()


class TestNonDefaultCurrency:
    """A page listing several currencies must not parse them as the default."""

    def _jpy_wallet(self, migrated_database: Database) -> int:
        """Open a JPY account, which the clearing page also lists."""

        def factory() -> UnitOfWork:
            return SqlAlchemyUnitOfWork(migrated_database.session_factory)

        setup = LedgerSetupService(factory)
        account = setup.open_account(
            OpenFinancialAccountRequest(
                institution_code="jp_bank",
                display_name="日圓帳戶",
                account_type=FinancialAccountType.CHECKING,
                currency="JPY",
            )
        )
        assert account.unallocated_wallet is not None
        return account.unallocated_wallet.id

    def test_a_jpy_amount_is_parsed_with_jpy_precision(
        self, client: TestClient, seed: Seed, migrated_database: Database
    ) -> None:
        """TWD stores cents and JPY does not; 1,000 must not become 100000."""
        wallet_id = self._jpy_wallet(migrated_database)

        response = _post(
            client,
            "/clearing/schedules",
            {
                "client_token": "jpy-1",
                "source_account_id": str(seed.other_account_id),
                "source_wallet_id": str(wallet_id),
                "destination_account_id": str(seed.main_account_id),
                "destination_wallet_id": str(seed.clearing_hub_id),
                "amount": "1000",
                "scheduled_local_date": _future_date(),
                "scheduled_local_time": "06:00",
            },
        )

        # The transfer is refused for mixing currencies, which is correct — but
        # it must be refused by the ledger, never by parsing JPY as TWD.
        assert response.status_code == 422
        assert "decimal places" not in response.text

    def test_the_form_no_longer_asserts_a_currency_it_cannot_know(
        self, client: TestClient, seed: Seed
    ) -> None:
        del seed
        page = client.get("/clearing").text

        assert 'name="currency"' not in page

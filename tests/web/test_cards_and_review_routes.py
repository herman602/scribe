"""Local-only card configuration and money review route tests."""

from __future__ import annotations

import html
import json
import re
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from app.application.services.budget_planning import (
    AddCategoryRequest,
    ManageBudgetPlan,
)
from app.application.services.financial_event_intake import FinancialEventIntake
from app.application.services.ledger import (
    AddWalletRequest,
    LedgerSetupService,
    OpenFinancialAccountRequest,
)
from app.bootstrap import build_web_application
from app.domain.budget_enums import (
    CardHolderRole,
    FinancialAccountType,
    FinancialEventStatus,
    ScheduledTransferStatus,
    WalletPurpose,
)
from app.domain.budget_models import (
    CardConfigurationDraft,
    CardDraft,
    CardIdentity,
)
from app.domain.enums import SenderMatchType, TransactionType
from app.domain.models import EmailSnapshot, SenderRuleDraft, TransactionDraft
from app.infrastructure.config.settings import AppSettings
from app.infrastructure.database.engine import Database, create_database
from app.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork
from fastapi.testclient import TestClient

EXTERNAL_HOST = "scribe.example.com"


def _major(amount_minor: int) -> str:
    """Return a TWD amount the way the form now asks for it, not in cents."""
    return str(amount_minor // 100)


LOCAL_ONLY_PATHS = ("/cards", "/money/review", "/money/accounts/new")


def _seed(database: Database) -> tuple[int, int]:
    """Return one (gmail_account_id, transaction_id) pair with a card source."""
    with SqlAlchemyUnitOfWork(database.session_factory) as unit:
        account = unit.accounts.get_or_create("user@example.test")
        rule = unit.sender_rules.upsert(
            SenderRuleDraft(
                gmail_account_id=account.id,
                institution_key="test_bank",
                institution_name="測試銀行",
                match_type=SenderMatchType.EXACT_ADDRESS,
                match_value="notice@example-bank.test",
            )
        )
        outcome = unit.emails.upsert_from_gmail(
            EmailSnapshot(
                gmail_account_id=account.id,
                gmail_message_id="gmail-web-b2",
                gmail_thread_id="thread-web-b2",
                gmail_history_id="500",
                sender="notice@example-bank.test",
                recipients=("user@example.test",),
                subject="消費通知",
                gmail_internal_date_ms=1_754_000_000_000,
                matched_sender_rule_id=rule.id,
            )
        )
        transaction = unit.transactions.add(
            TransactionDraft(
                email_id=outcome.email.id,
                source_key="row-1",
                transaction_type=TransactionType.PURCHASE,
                confidence=Decimal("0.95"),
                parser_name="tests.fake",
                parser_version="1",
                merchant="測試咖啡",
                amount_minor=128000,
                currency="TWD",
                card_last_four="4321",
                transaction_at=datetime(2026, 8, 7, 13, 30, tzinfo=UTC),
            )
        )
        unit.commit()
        return account.id, transaction.id


def _stage_event(database: Database, transaction_id: int) -> None:
    def factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(database.session_factory)

    with factory() as unit:
        unit.transactions.confirm_pending(frozenset({transaction_id}))
        FinancialEventIntake(factory).stage(unit, (transaction_id,))
        unit.commit()


def _hidden(body: str, name: str) -> str:
    match = re.search(rf'name="{name}" value="([^"]+)"', body)
    assert match is not None
    return html.unescape(match.group(1))


@pytest.fixture
def seeded(
    migrated_database: Database, database_settings: AppSettings
) -> tuple[AppSettings, int, int]:
    """Return settings plus one staged Gmail-origin event fixture."""
    account_id, transaction_id = _seed(migrated_database)
    _stage_event(migrated_database, transaction_id)
    return database_settings, account_id, transaction_id


def test_a_confirmed_notification_points_at_the_allocation_it_still_needs(
    seeded: tuple[AppSettings, int, int],
) -> None:
    """Confirming is the first of two steps, so the page names the second.

    The detail page used to say only "已確認", which left the allocation —
    the step that actually decides which wallet pays — with no entry point
    anywhere on the page the user was looking at.
    """
    settings, _, transaction_id = seeded

    with TestClient(build_web_application(settings)) as client:
        detail = client.get(f"/transactions/{transaction_id}")

    assert detail.status_code == 200
    assert "還沒選扣款來源" in detail.text
    assert "/money/review?event_id=" in detail.text
    # Nothing is posted yet, so no wallet may be claimed as the source.
    assert "這筆已入帳" not in detail.text


def test_the_funding_default_is_the_card_it_belongs_to_not_a_wallet_name(
    migrated_database: Database, database_settings: AppSettings
) -> None:
    """The preselected wallet comes from the card, not from matching a name.

    It used to preselect any wallet literally named 生活費, which could be a
    wallet at a different bank from the card's settlement account — funding
    the card from there is a cross-bank transfer the user then has to make
    for real. The card already names the wallet it is paid from, and that
    wallet sits in its own settlement account, so reserving there needs no
    transfer at all.
    """
    settings = database_settings
    gmail_account_id, transaction_id = _seed(migrated_database)

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
    decoy = setup.add_wallet(
        AddWalletRequest(financial_account_id=bank.account.id, name="生活費")
    )
    intended = setup.add_wallet(
        AddWalletRequest(financial_account_id=bank.account.id, name="卡費專用")
    )
    # A wallet named after a category is how someone who keeps one envelope
    # per kind of spending writes it, so the category is derivable from it.
    named_like_category = setup.add_wallet(
        AddWalletRequest(financial_account_id=bank.account.id, name="飲食")
    )
    # The dedicated-wallet half only renders for an account holding one.
    setup.add_wallet(
        AddWalletRequest(
            financial_account_id=bank.account.id,
            name="帳單暫存",
            purpose=WalletPurpose.CLEARING_HUB,
        )
    )
    liability = setup.open_account(
        OpenFinancialAccountRequest(
            institution_code="test_bank",
            display_name="測試卡費",
            account_type=FinancialAccountType.CARD_LIABILITY,
            currency="TWD",
        )
    )
    ManageBudgetPlan(factory).add_category(AddCategoryRequest(name="飲食"))

    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        unit.cards.add(
            CardDraft(
                gmail_account_id=gmail_account_id,
                issuer_code="test_bank",
                display_name="測試銀行 ****4321",
                last_four="4321",
                holder_role=CardHolderRole.PRIMARY,
            )
        )
        card = unit.cards.get_by_identity(
            CardIdentity(
                gmail_account_id=gmail_account_id,
                issuer_code="test_bank",
                last_four="4321",
            )
        )
        assert card is not None
        unit.cards.save_configuration(
            CardConfigurationDraft(
                card_id=card.id,
                expected_version=card.version,
                display_name=card.display_name,
                holder_role=card.holder_role,
                liability_account_id=liability.account.id,
                settlement_account_id=bank.account.id,
                default_funding_wallet_id=intended.id,
            )
        )
        unit.commit()
    _stage_event(migrated_database, transaction_id)

    with TestClient(build_web_application(settings)) as client:
        review = client.get("/money/review")

    assert review.status_code == 200
    assert f'value="{intended.id}" selected' in review.text
    assert f'value="{decoy.id}" selected' not in review.text
    # The wallet sharing a category's name carries it, so choosing the wallet
    # answers the category too; a wallet named nothing like one carries none.
    category_id = ManageBudgetPlan(factory).list_categories()[0].id
    assert (
        f'value="{named_like_category.id}" data-category="{category_id}"' in review.text
    )
    assert f'value="{intended.id}" data-category=' not in review.text
    # The category is folded away, since the wallet usually answers it. Being
    # folded and required, it must carry an empty option: without one the
    # browser would post whichever category came first, unseen.
    assert 'class="secondary-field"' in review.text
    assert '<option value="">請選擇分類</option>' in review.text


def test_review_page_lists_events_with_stable_reason_codes(
    seeded: tuple[AppSettings, int, int],
) -> None:
    settings, _, _ = seeded

    with TestClient(build_web_application(settings)) as client:
        response = client.get("/money/review")

    assert response.status_code == 200
    assert "測試咖啡" in response.text
    assert "待分配" in response.text
    assert "卡片尚未設定" in response.text
    assert "需要卡片設定" in response.text


def test_review_hides_and_rejects_an_event_already_inside_the_opening_position(
    seeded: tuple[AppSettings, int, int],
    migrated_database: Database,
) -> None:
    settings, _, _ = seeded
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        event = unit.financial_events.list_by_status(
            frozenset({FinancialEventStatus.NEEDS_ALLOCATION})
        )[0]
        unit.import_batches.add(
            source_kind="test_sheet_readonly",
            format_version=1,
            content_sha256="c" * 64,
            source_locator_hash="d" * 64,
            observed_at=event.occurred_at,
            cutoff_at=event.occurred_at,
            row_count=0,
            warning_count=0,
            applied_at=event.occurred_at,
        )
        unit.commit()

    with TestClient(build_web_application(settings)) as client:
        page = client.get("/money/review")
        token_page = client.get("/cards")
        rejected = client.post(
            f"/money/review/{event.id}/allocate",
            data={
                "csrf_token": _hidden(token_page.text, "csrf_token"),
                "version": str(event.version),
                "client_token": "must-not-post-history",
                "currency": event.currency,
                "amount": _major(event.amount_minor),
                "funding_wallet_id": "1",
                "budget_category_id": "1",
                "spending_treatment": "monthly_budget",
            },
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )

    assert page.status_code == 200
    assert "測試咖啡" not in page.text
    assert "目前沒有待分配的事件" in page.text
    assert rejected.status_code == 422
    assert "historical evidence" in rejected.text
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        assert unit.journal.get_by_financial_event(event.id) is None


def test_cards_page_creates_and_configures_a_card(
    seeded: tuple[AppSettings, int, int],
) -> None:
    settings, account_id, _ = seeded

    with TestClient(build_web_application(settings)) as client:
        empty = client.get("/cards")
        token = _hidden(empty.text, "csrf_token")
        created = client.post(
            "/cards",
            data={
                "csrf_token": token,
                "gmail_account_id": str(account_id),
                "issuer_code": "test_bank",
                "display_name": "測試卡",
                "last_four": "4321",
                "holder_role": "primary",
            },
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )
        listed = client.get("/cards")
        rejected = client.post(
            "/cards",
            data={
                "csrf_token": token,
                "gmail_account_id": str(account_id),
                "issuer_code": "test_bank",
                "display_name": "壞卡",
                "last_four": "12345678901234",
                "holder_role": "primary",
            },
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )
        stale = client.post(
            "/cards/1",
            data={
                "csrf_token": token,
                "version": "99",
                "display_name": "測試卡",
                "holder_role": "primary",
                "liability_account_id": "",
                "settlement_account_id": "",
                "default_funding_wallet_id": "",
            },
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )

    assert empty.status_code == 200
    assert "尚未設定任何卡片" in empty.text
    assert created.status_code == 303
    assert "****4321" in listed.text
    assert "測試銀行 · ****4321" in listed.text
    assert "尚未完成設定" in listed.text
    assert rejected.status_code == 422
    assert "12345678901234" not in rejected.text
    assert stale.status_code == 409


def test_card_routes_reject_a_missing_csrf_token(
    seeded: tuple[AppSettings, int, int],
) -> None:
    settings, account_id, _ = seeded

    with TestClient(build_web_application(settings)) as client:
        client.get("/cards")
        response = client.post(
            "/cards",
            data={
                "gmail_account_id": str(account_id),
                "issuer_code": "test_bank",
                "display_name": "測試卡",
                "last_four": "4321",
            },
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )
        listed = client.get("/cards")

    assert response.status_code == 422
    assert "CSRF" in response.text
    assert "尚未設定任何卡片" in listed.text


def test_the_complete_card_setup_can_be_done_in_the_browser(
    seeded: tuple[AppSettings, int, int], migrated_database: Database
) -> None:
    settings, gmail_account_id, _ = seeded
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        batch = unit.import_batches.add(
            source_kind="synthetic_readonly_sheet",
            format_version=1,
            content_sha256="a" * 64,
            source_locator_hash="b" * 64,
            observed_at=datetime(2026, 8, 15, tzinfo=UTC),
            cutoff_at=datetime(2026, 8, 15, tzinfo=UTC),
            row_count=2,
            warning_count=2,
            applied_at=datetime(2026, 8, 15, tzinfo=UTC),
        )
        cycle_id = unit.import_rows.add(
            batch_id=batch.id,
            entity_kind="card_cycle",
            source_row_key="test-bank-cycle",
            normalized_payload_json=json.dumps(
                {"statement_day": 10, "due_day": 25, "autopay_day": 26}
            ),
            status="pending",
            reason_code="card_mapping_required",
        )
        transfer_id = unit.import_rows.add(
            batch_id=batch.id,
            entity_kind="scheduled_transfer_candidate",
            source_row_key="test-bank-transfer",
            normalized_payload_json=json.dumps(
                {
                    "date": "2026-08-26",
                    "currency": "TWD",
                    "amount_minor": 128_000,
                    "destination": "測試銀行",
                }
            ),
            status="pending",
            reason_code="card_mapping_required",
        )
        unit.commit()

    with TestClient(build_web_application(settings)) as client:
        page = client.get("/cards")
        token = _hidden(page.text, "csrf_token")
        liability = client.post(
            "/money/accounts",
            data={
                "csrf_token": token,
                "return_to": "cards",
                "institution_code": "test_bank",
                "display_name": "測試卡負債",
                "account_type": "card_liability",
                "currency": "TWD",
                "masked_reference": "•••• 4321",
                "opening_balance": "1280",
                "observed_at": "2026-08-15T09:00",
            },
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )
        settlement = client.post(
            "/money/accounts",
            data={
                "csrf_token": token,
                "return_to": "cards",
                "institution_code": "test_bank",
                "display_name": "測試扣款帳戶",
                "account_type": "checking",
                "currency": "TWD",
                "opening_balance": "5000",
                "observed_at": "2026-08-15T09:00",
            },
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )
        card = client.post(
            "/cards",
            data={
                "csrf_token": token,
                "gmail_account_id": str(gmail_account_id),
                "issuer_code": "test_bank",
                "display_name": "測試主卡",
                "last_four": "4321",
                "holder_role": "primary",
            },
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )
        configured = client.post(
            "/cards/1",
            data={
                "csrf_token": token,
                "version": "1",
                "display_name": "測試主卡",
                "holder_role": "primary",
                "liability_account_id": "1",
                "settlement_account_id": "2",
                "default_funding_wallet_id": "1",
            },
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )
        assigned_cycle = client.post(
            f"/cards/import-candidates/{cycle_id}/assign",
            data={"csrf_token": token, "card_id": "1"},
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )
        assigned_transfer = client.post(
            f"/cards/import-candidates/{transfer_id}/assign",
            data={"csrf_token": token, "card_id": "1"},
            headers={"origin": "http://testserver"},
            follow_redirects=False,
        )
        result = client.get("/cards")

    assert liability.status_code == settlement.status_code == 303
    assert card.status_code == configured.status_code == 303
    assert assigned_cycle.status_code == assigned_transfer.status_code == 303
    assert "已完成設定" in result.text
    assert "目前沒有待指派的卡片提示" in result.text
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        assignments = unit.card_import_assignments.list_all()
        assert len(assignments) == 2
        assert unit.import_rows.get(cycle_id).status.value == "pending"  # type: ignore[union-attr]
        assert unit.import_rows.get(transfer_id).status.value == "pending"  # type: ignore[union-attr]
        assert (
            unit.scheduled_transfers.count_by_status(frozenset(ScheduledTransferStatus))
            == 0
        )


def test_external_hostname_cannot_reach_cards_or_review(
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
        creates = client.post("/cards", headers=external)
        configures = client.post("/cards/1", headers=external)
        creates_account = client.post("/money/accounts", headers=external)
        assigns = client.post("/cards/import-candidates/1/assign", headers=external)
        local_reads = {path: client.get(path) for path in LOCAL_ONLY_PATHS}

    for path, response in reads.items():
        assert response.status_code == 404, path
        assert "卡片" not in response.text
    assert creates.status_code == 404
    assert configures.status_code == 404
    assert creates_account.status_code == 404
    assert assigns.status_code == 404
    for path, response in local_reads.items():
        assert response.status_code == 200, path
    assert create_database(settings) is not None

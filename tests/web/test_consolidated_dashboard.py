"""B8 local landing page, navigation, and responsive integration."""

from __future__ import annotations

from pathlib import Path

from app.bootstrap import build_web_application
from app.infrastructure.config.settings import AppSettings
from app.infrastructure.database.engine import Database
from fastapi.testclient import TestClient
from pytest import MonkeyPatch
from sqlalchemy.engine import Engine

EXTERNAL_HOST = "scribe.example.com"


def test_local_root_and_money_render_the_same_offline_overview(
    migrated_database: Database, database_settings: AppSettings
) -> None:
    del migrated_database
    with TestClient(build_web_application(database_settings)) as client:
        root = client.get("/")
        money = client.get("/money")

    for page in (root, money):
        assert page.status_code == 200
        assert "資金總覽" in page.text
        assert "帳戶與小錢包" in page.text
        assert "本月預算" in page.text
        assert "過去 7 天交易" in page.text
        assert 'action="/transactions/confirm"' in page.text


def test_every_local_workflow_is_reachable_from_primary_or_more_navigation(
    migrated_database: Database, database_settings: AppSettings
) -> None:
    del migrated_database
    with TestClient(build_web_application(database_settings)) as client:
        root = client.get("/")
        more = client.get("/more")

    assert more.status_code == 200
    combined = root.text + more.text
    for path in (
        "/money/daily",
        "/money/accounts",
        "/budget",
        "/allocations",
        "/clearing",
        "/subscriptions",
        "/cards",
        "/transactions",
        "/emails",
        "/reports",
        "/sync",
        "/money/reconcile",
        "/money/source-links",
        "/money/review",
    ):
        assert f'href="{path}' in combined
    assert 'class="mobile-primary-nav"' in root.text


def test_external_root_remains_the_transaction_dashboard_and_hides_money(
    migrated_database: Database, database_settings: AppSettings
) -> None:
    del migrated_database
    settings = database_settings.model_copy(
        update={
            "external_access_enabled": True,
            "external_hostname": EXTERNAL_HOST,
        }
    )
    with TestClient(
        build_web_application(settings), base_url=f"https://{EXTERNAL_HOST}"
    ) as client:
        root = client.get("/")
        more = client.get("/more")
        money = client.get("/money")

    assert root.status_code == 200
    assert "唯讀交易總覽" in root.text
    assert "資金總覽" not in root.text
    assert "mobile-primary-nav" not in root.text
    assert more.status_code == 404
    assert money.status_code == 404


def test_web_lifespan_disposes_the_database_engine_it_owns(
    migrated_database: Database,
    database_settings: AppSettings,
    monkeypatch: MonkeyPatch,
) -> None:
    del migrated_database
    disposed: list[Engine] = []
    original_dispose = Engine.dispose

    def record_dispose(engine: Engine, close: bool = True) -> None:
        disposed.append(engine)
        original_dispose(engine, close=close)

    monkeypatch.setattr(Engine, "dispose", record_dispose)
    application = build_web_application(database_settings)

    with TestClient(application) as client:
        assert client.get("/health").status_code == 200

    assert len(disposed) == 1


def test_budget_tables_are_labelled_cards_at_phone_width(
    migrated_database: Database, database_settings: AppSettings
) -> None:
    del migrated_database
    with TestClient(build_web_application(database_settings)) as client:
        page = client.get("/budget", follow_redirects=True)

    assert page.status_code == 200
    template = Path("app/presentation/web/templates/budget.html").read_text(
        encoding="utf-8"
    )
    assert template.count('class="data-table responsive-data-table"') == 4
    assert 'data-label="分類"' in template
    css = Path("app/presentation/web/static/styles.css").read_text(encoding="utf-8")
    assert "@media (max-width: 760px)" in css
    assert ".mobile-primary-nav" in css
    assert ".responsive-data-table td::before" in css
    assert "min-height: 50px" in css


def test_every_legacy_table_and_checkbox_has_a_phone_interaction_shape() -> None:
    """Phone pages need labelled cards and real touch targets, not scroll hacks."""
    templates = Path("app/presentation/web/templates")
    email_results = (templates / "partials/email_results.html").read_text(
        encoding="utf-8"
    )
    sync_progress = (templates / "partials/sync_progress.html").read_text(
        encoding="utf-8"
    )
    all_templates = "\n".join(
        path.read_text(encoding="utf-8") for path in templates.rglob("*.html")
    )
    css = Path("app/presentation/web/static/styles.css").read_text(encoding="utf-8")

    assert 'class="responsive-data-table email-data-table"' in email_results
    assert 'data-label="收到時間"' in email_results
    assert 'data-label="信心度"' in email_results
    assert 'class="responsive-data-table sync-data-table"' in sync_progress
    assert 'data-label="時間 / 模式"' in sync_progress
    assert 'data-label="操作"' in sync_progress
    assert 'class="transaction-select-control"' in all_templates
    assert all_templates.count('type="checkbox"') == all_templates.count(
        'class="selection-checkbox" type="checkbox"'
    )
    assert ".transaction-select-control {" in css
    assert ".override-toggle {" in css
    assert ".review-flags label {" in css
    assert "overflow: hidden; padding: 0;" in css
    assert "clip: rect(0, 0, 0, 0);" in css

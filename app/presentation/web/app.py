"""Local-only FastAPI dashboard application."""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import __version__
from app.application.queries.budget import BudgetQueries
from app.application.queries.clearing import ClearingQueries
from app.application.queries.consolidated import ConsolidatedMoneyQueries
from app.application.queries.dashboard import (
    DashboardQueries,
    EmailSearchCriteria,
    MoneyTotal,
    TransactionListItem,
    TransactionSearchCriteria,
)
from app.application.queries.money import MoneyBalanceQueries, MoneyReviewQueries
from app.application.queries.salary import (
    SalaryQueries,
    SalaryTemplateRow,
    SalaryWalletOption,
)
from app.application.queries.spending import SpendingQueries
from app.application.queries.statistics import (
    StatisticsQueryService,
    StatisticsSnapshot,
)
from app.application.queries.subscriptions import SubscriptionQueries
from app.application.queries.sync import SyncDashboardQueries
from app.application.services.bank_movements import (
    AccountTransferRequest,
    BankMovementRequest,
    ManualRefundRequest,
    ReallocateWalletRequest,
    RecordBankMovement,
)
from app.application.services.budget_close import BudgetClosePreview, CloseBudgetPeriod
from app.application.services.budget_planning import (
    AddCategoryRequest,
    ManageBudgetPlan,
    SetTargetRequest,
    UpdateCategoryRequest,
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
    RedoAllocation,
    RedoAllocationRequest,
)
from app.application.services.gmail_auth_health import GmailAuthHealthService
from app.application.services.gmail_full_sync import FullSyncRequest
from app.application.services.ledger import (
    ArrangeAccountsRequest,
    ArrangeWalletsRequest,
    LedgerSetupService,
    OpenAccountWithOpeningRequest,
    OpenFinancialAccountRequest,
    RenameWalletsRequest,
)
from app.application.services.manual_money import (
    CreateManualExpense,
    ManualExpenseRequest,
    ReverseEventRequest,
    ReverseFinancialEvent,
    UpdateFinancialEventNote,
    UpdateFinancialEventNoteRequest,
)
from app.application.services.reconciliation import (
    AdjustReconciliation,
    BalanceSnapshotRequest,
    ReconciliationAdjustmentRequest,
    RecordBalanceSnapshot,
)
from app.application.services.salary_allocation import (
    AddSalaryTemplateItemRequest,
    AddSalaryTemplateRequest,
    ApproveSalaryAllocation,
    ApproveSalaryRunRequest,
    CancelSalaryDraftRequest,
    DeactivateSalaryTemplateItemRequest,
    GenerateSalaryAllocation,
    GenerateSalaryRunRequest,
    ManageSalaryTemplates,
    MarkSalaryTransferred,
    MarkSalaryTransferredRequest,
    RecordSalaryReceipt,
    RegenerateSalaryRunRequest,
    SalaryAmountEdit,
    SalaryReceiptRequest,
    SetSalaryAmountsRequest,
    UpdateSalaryTemplateItemRequest,
    UpdateSalaryTemplateRequest,
)
from app.application.services.scheduled_transfers import (
    ScheduleCardAutopayRequest,
    ScheduleClearingTransferRequest,
    ScheduleCommitmentLine,
    ScheduledTransferAction,
    ScheduleTransfers,
)
from app.application.services.source_review import ReviewSourceLinks
from app.application.services.stored_email_reparse import ReparseStoredEmail
from app.application.services.subscriptions import (
    AddPricePeriodRequest,
    AddSubscriptionRequest,
    CalculateSubscriptionRequirement,
    DeactivateSubscriptionRequest,
    ManageSubscriptions,
    ResumeSubscriptionRequest,
    UpdatePricePeriodRequest,
    UpdateSubscriptionRequest,
)
from app.application.services.supplementary_card_settings import (
    SetSupplementaryCard,
    SetSupplementaryCardRequest,
)
from app.application.services.sync_control import ControlBackgroundSync
from app.application.services.transaction_confirmation import (
    ConfirmationResult,
    ConfirmTransactions,
)
from app.application.services.transaction_review import (
    ReviewTransaction,
    ReviewTransactionRequest,
)
from app.domain.budget_enums import (
    BankMovementKind,
    CardHolderRole,
    FinancialAccountType,
    PaymentMethod,
    SalaryCalculationMode,
    SubscriptionCadence,
    WalletPurpose,
)
from app.domain.budget_models import (
    BudgetCategory,
    FinancialAccountBalance,
    ManualSalaryAmount,
)
from app.domain.budget_rules import SPENDING_BANK_MOVEMENTS, parse_period_label
from app.domain.enums import ClassificationStatus, ProcessingStatus, TransactionType
from app.domain.errors import (
    BudgetPeriodError,
    ConcurrentUpdateError,
    ScheduledTransferOverdueError,
    ScribeError,
    StaleReconciliationError,
)
from app.domain.money import currency_precision, parse_display_amount
from app.domain.transactions import SPENDING_TRANSACTION_TYPES
from app.infrastructure.config.settings import AppSettings
from app.presentation.web.access import (
    AccessAssertionVerifier,
    WebAccessMode,
    WebAccessPolicy,
    local_full_hosts,
)
from app.worker.maintenance import LocalMaintenanceCoordinator

_WEB_ROOT = Path(__file__).resolve().parent
_SAFE_SORTS = {
    "transaction_at_desc",
    "transaction_at_asc",
    "amount_desc",
    "amount_asc",
}
_SAFE_REVIEW_STATUSES = {"confirmed", "ignored", "unconfirmed"}
#: Windows the recent-notification list can be widened to. A value outside
#: this set falls back to the default, so an edited query string cannot ask
#: the database for an arbitrarily long span.
_RECENT_WINDOW_CHOICES = (7, 14, 30)
_DEFAULT_RECENT_WINDOW = 7
#: Display vocabulary for the stable requirement reason codes. The codes are
#: decided by the domain; only their wording lives here.
_REASON_LABELS = {
    "monthly_price": "每月費用",
    "cycle_share": "週期攤分",
    "zero_price": "價格為 0",
    "no_price_for_month": "這個月沒有生效價格",
    "not_yet_active": "尚未開始",
    "no_longer_active": "已停止",
}
_CADENCE_LABELS = {"monthly": "每月", "yearly": "每年", "custom": "自訂週期"}
_WALLET_PROBLEM_LABELS = {
    "wallet_missing": "這個錢包已不存在",
    "wallet_inactive": "這個錢包已停用",
    "wallet_account_missing": "這個錢包的帳戶已不存在",
    "wallet_account_inactive": "這個錢包的帳戶已停用",
    "wallet_not_asset": "這個錢包不屬於資產帳戶",
}
#: Display vocabulary for the salary lifecycles. The codes are decided by the
#: domain; only their wording lives here.
_RUN_STATUS_LABELS = {
    "draft": "草稿",
    "approved": "待手動轉帳",
    "partially_funded": "部分完成",
    "funded": "已完成",
    "cancelled": "已取消",
}
_RUN_ITEM_STATUS_LABELS = {
    "draft": "草稿",
    "not_required": "本月無需撥款",
    "allocated": "已在原帳戶分配",
    "waiting_transfer": "待手動轉帳",
    "transferred": "已轉帳",
}
_FULFILLMENT_LABELS = {
    "reserve_only": "同一帳戶內分配",
    "account_transfer": "同銀行不同帳戶需轉帳",
    "cross_bank_transfer": "跨銀行需轉帳",
}
_CALCULATION_MODE_LABELS = {
    "fixed": "固定金額",
    "subscription_requirement": "訂閱需求",
    "manual": "手動輸入",
}
#: Display vocabulary for the clearing lifecycle. `assumed_executed` is worded
#: as an assumption on purpose (ADR-B050): no wording here may suggest that a
#: bank confirmed anything.
_SCHEDULE_STATUS_LABELS = {
    "scheduled": "已排程",
    "processing": "執行中",
    "assumed_executed": "已自動視為轉出",
    "failed": "執行失敗",
    "cancelled": "已取消",
    "reversed": "已沖銷",
}
_SCHEDULE_PURPOSE_LABELS = {
    "clearing_funding": "帳單暫存撥款",
    "card_autopay": "信用卡自動扣繳",
}
#: Wording for the correction a row's surplus actually allows. The codes are
#: decided by the query model; only their wording lives here.
_WARNING_ACTION_LABELS = {
    "cancel_and_replace": "請取消後重建",
    "wait_for_run": "正在執行中。請等這一輪結束後再處理",
    "reverse": "錢已經移動。請用沖銷處理",
}
#: The same codes worded for the summary above the list, where they describe
#: what is possible across every over-covered reservation rather than one row.
_WARNING_SUMMARY_LABELS = {
    "cancel_and_replace": "取消還沒執行的排程。再重新建立一筆金額正確的",
    "wait_for_run": "正在執行中的排程要等這一輪結束後才能處理",
    "reverse": "已經視為轉出的排程只能用沖銷處理",
}
_BANK_MOVEMENT_KIND_LABELS = {
    "interest_income": "利息收入",
    "external_deposit": "外部存入 - 非收入分類",
    "external_withdrawal": "外部提款或匯出 - 不列生活費",
    "cash_withdrawal_spending": "提款並全額列入生活費",
    "direct_debit": "銀行直接扣款",
    "fee": "銀行手續費",
    "bill_payment": "銀行帳單扣款",
}
#: General-purpose Chinese vocabulary for the stable enum codes used across
#: the local pages, merged into one dict for the `label` filter below. A
#: code with no entry here falls back to itself, so an unmapped value never
#: crashes a page — it just still reads in English until added.
_GENERAL_LABELS: dict[str, str] = {
    **_REASON_LABELS,
    **_CADENCE_LABELS,
    **_RUN_STATUS_LABELS,
    **_RUN_ITEM_STATUS_LABELS,
    **_FULFILLMENT_LABELS,
    **_CALCULATION_MODE_LABELS,
    **_SCHEDULE_STATUS_LABELS,
    **_SCHEDULE_PURPOSE_LABELS,
    **_BANK_MOVEMENT_KIND_LABELS,
    # PaymentMethod
    "cash": "現金",
    "credit_card": "信用卡",
    "bank_account": "銀行帳戶",
    "transfer": "轉帳",
    "other": "其他",
    # FinancialEventType / TransactionType (shared codes; extra keys are
    # harmless for whichever enum is not using them)
    "purchase": "消費",
    "refund": "退款",
    "withdrawal": "提款",
    "deposit": "存入",
    "bill_payment": "帳單扣款",
    "subscription": "訂閱扣款",
    "fee": "手續費",
    "interest_income": "利息收入",
    "direct_debit": "自動扣款",
    "adjustment": "調整",
    "unknown": "未知",
    # FinancialEventStatus
    "draft": "草稿",
    "needs_allocation": "待分配",
    "ready": "待入帳",
    "posted": "已入帳",
    "ignored": "已忽略",
    "reversed": "已沖銷",
    # FinancialEventOrigin
    "gmail_transaction": "Gmail 交易",
    "manual": "手動輸入",
    "import": "匯入",
    "system": "系統產生",
    # SourceLinkStatus / general confirm-reject vocabulary
    "candidate": "待確認配對",
    "confirmed": "已確認",
    "rejected": "已判定不同筆",
    # SourceLinkReason
    "direct_gmail_intake": "Gmail 直接匯入",
    "card_unresolved": "卡片尚未對應",
    "card_ambiguous": "卡片對應不明確",
    "card_not_configured": "卡片尚未設定",
    "exact_card": "卡片完全相符",
    "compatible_event_type": "交易類型相容",
    "time_within_24h": "24 小時內",
    "time_within_72h": "72 小時內",
    "exact_merchant": "商家完全相符",
    "token_merchant": "商家部分相符",
    "auto_exact_match": "系統自動完全比對",
    "user_confirmed_match": "使用者確認同一筆",
    "user_rejected_match": "使用者判定不同筆",
    "user_declared_separate": "使用者宣告為獨立交易",
    "source_changed": "來源資料已變更",
    # SourceDiscrepancy
    "amount_minor": "金額",
    "currency": "幣別",
    "merchant": "商家",
    "occurred_at": "交易時間",
    "event_type": "交易類型",
    "card": "卡片",
    "source_no_longer_confirmed": "來源已不再是已確認狀態",
    # FinancialAccountType
    "checking": "支票帳戶",
    "savings": "儲蓄帳戶",
    "card_liability": "信用卡負債",
    # ClassificationStatus
    "likely": "可能是交易",
    "uncertain": "不確定",
    "not_transaction": "非交易",
    # TransactionSourceStatus
    "active": "使用中",
    "superseded": "已被取代",
    # JournalEntryType
    "opening_balance": "期初餘額",
    "internal_transfer": "帳戶內轉帳",
    "budget_close": "月結掃入",
    "salary_receipt": "薪資入帳",
    "funding_transfer": "撥款轉帳",
    "card_payment": "信用卡繳款",
    "income": "收入",
    "external_movement": "外部資金異動",
    # BalanceSnapshotSource
    "manual_bank_app": "手動輸入(銀行 App)",
    # CardHolderRole
    "primary": "正卡",
    "supplementary": "副卡",
    # ProcessingStatus
    "pending": "待處理",
    "processed": "已處理",
    "failed": "失敗",
    # SyncMode / RunStatus (background Gmail sync)
    "full": "完整同步",
    "incremental": "增量同步",
    "reconciliation": "對帳同步",
    "idle": "閒置",
    "running": "執行中",
    "succeeded": "成功",
    "queued": "排隊中",
    "partial": "部分完成",
    "cancelled": "已取消",
}
_CLEARING_PAGE_SIZE = 25


@dataclass(frozen=True)
class WebDependencies:
    """Application services required by presentation routes."""

    queries: DashboardQueries
    consolidated: ConsolidatedMoneyQueries
    confirm_transactions: ConfirmTransactions
    review_transaction: ReviewTransaction
    set_supplementary_card: SetSupplementaryCard
    reparse_email: ReparseStoredEmail
    statistics: StatisticsQueryService
    sync_queries: SyncDashboardQueries
    sync_control: ControlBackgroundSync
    money_review: MoneyReviewQueries
    money_balances: MoneyBalanceQueries
    manage_cards: ManageCards
    ledger_setup: LedgerSetupService
    budget: BudgetQueries
    manage_budget_plan: ManageBudgetPlan
    allocate_event: AllocateFinancialEvent
    redo_allocation: RedoAllocation
    close_budget_period: CloseBudgetPeriod
    manage_subscriptions: ManageSubscriptions
    subscription_register: SubscriptionQueries
    subscription_requirement: CalculateSubscriptionRequirement
    manage_salary_templates: ManageSalaryTemplates
    record_salary_receipt: RecordSalaryReceipt
    generate_salary_allocation: GenerateSalaryAllocation
    approve_salary_allocation: ApproveSalaryAllocation
    mark_salary_transferred: MarkSalaryTransferred
    salary: SalaryQueries
    schedule_transfers: ScheduleTransfers
    clearing: ClearingQueries
    manual_expense: CreateManualExpense
    update_event_note: UpdateFinancialEventNote
    reverse_event: ReverseFinancialEvent
    bank_movements: RecordBankMovement
    balance_snapshots: RecordBalanceSnapshot
    reconciliation: AdjustReconciliation
    source_review: ReviewSourceLinks
    spending: SpendingQueries
    gmail_auth_health: GmailAuthHealthService
    maintenance: LocalMaintenanceCoordinator | None = None
    external_access_verifier: AccessAssertionVerifier | None = None
    close: Callable[[], None] = lambda: None


@dataclass(frozen=True)
class RecentTransactionDay:
    """One local-calendar group for the home-page recent transaction list."""

    key: str
    label: str
    items: tuple[TransactionListItem, ...]
    #: What this day's consumption notifications add up to, by currency.
    #: Provisional: these are parsed notifications, not posted ledger lines,
    #: so this never substitutes for a confirmed total (ADR-B092).
    spending_totals: tuple[MoneyTotal, ...]
    #: The part of that figure still waiting for confirmation, by currency.
    unconfirmed_totals: tuple[MoneyTotal, ...]
    #: Notifications of any type still waiting for confirmation on this day.
    unconfirmed_count: int


class HealthResponse(BaseModel):
    """Non-sensitive health response."""

    status: Literal["ok"] = "ok"
    service: str = "scribe"
    version: str = __version__


def create_web_app(settings: AppSettings, dependencies: WebDependencies) -> FastAPI:
    """Create the local dashboard with explicit, testable dependencies."""
    access_policy = WebAccessPolicy.from_settings(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Catch up on due money at startup and stop cleanly at shutdown.

        A Mac that was asleep at 06:00 executes as soon as `serve` is running
        again. Maintenance is deliberately separate from Gmail sync, so a
        coordinator that cannot start never prevents the site from serving.
        """
        del app
        # The claim is taken when the engine is created, so this process
        # already holds it; it is released by `dependencies.close()`.
        if dependencies.maintenance is not None:
            dependencies.maintenance.start()
        try:
            yield
        finally:
            if dependencies.maintenance is not None:
                dependencies.maintenance.stop()
            dependencies.close()

    application = FastAPI(
        title="Scribe",
        version=__version__,
        docs_url="/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    application.state.settings = settings
    application.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=list(access_policy.trusted_hosts),
    )
    application.mount(
        "/static",
        StaticFiles(directory=_WEB_ROOT / "static"),
        name="static",
    )

    def gmail_auth_context(request: Request) -> dict[str, object]:
        del request
        return {
            "gmail_auth_notice": dependencies.gmail_auth_health.notice(
                testing_mode=settings.gmail_oauth_testing_mode
            )
        }

    templates = Jinja2Templates(
        directory=_WEB_ROOT / "templates",
        context_processors=[gmail_auth_context],
    )
    templates.env.filters["money"] = _format_money
    templates.env.filters["major"] = _format_major
    templates.env.filters["month"] = _format_month
    templates.env.filters["local_datetime"] = lambda value: _format_datetime(
        value, settings.local_timezone
    )
    templates.env.filters["datetime_local"] = lambda value: (
        value.astimezone(ZoneInfo(settings.local_timezone)).strftime("%Y-%m-%dT%H:%M")
        if value
        else ""
    )
    templates.env.filters["label"] = lambda value: _GENERAL_LABELS.get(value, value)

    @application.middleware("http")
    async def local_security(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        access_mode = access_policy.mode_for(request.url.hostname)
        request.state.access_mode = access_mode
        if not access_policy.permits(access_mode, request.method, request.url.path):
            return _external_not_found()
        if (
            access_mode is WebAccessMode.EXTERNAL_READ_ONLY
            and request.method.upper() == "POST"
        ):
            verifier = dependencies.external_access_verifier
            if verifier is None:
                return _external_forbidden()
            try:
                request.state.external_identity = await asyncio.to_thread(
                    verifier.verify,
                    request.headers.get("cf-access-jwt-assertion", ""),
                )
            except ValueError:
                return _external_forbidden()

        token = request.cookies.get("scribe_csrf") or secrets.token_urlsafe(32)
        request.state.csrf_token = token
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; "
            "form-action 'self'"
        )
        response.headers["Referrer-Policy"] = (
            "strict-origin"
            if (
                access_mode is WebAccessMode.EXTERNAL_READ_ONLY
                and settings.external_confirmation_enabled
            )
            else "no-referrer"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "private, no-store"
        if access_mode is WebAccessMode.EXTERNAL_READ_ONLY:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        if "scribe_csrf" not in request.cookies and (
            access_mode is WebAccessMode.LOCAL_FULL
            or settings.external_confirmation_enabled
        ):
            response.set_cookie(
                "scribe_csrf",
                token,
                httponly=True,
                samesite="strict",
                secure=access_mode is WebAccessMode.EXTERNAL_READ_ONLY,
                path="/",
            )
        return response

    def context(request: Request, **values: object) -> dict[str, object]:
        return {
            "version": __version__,
            "csrf_token": request.state.csrf_token,
            "request_target": _request_target(request),
            "external_read_only": (
                request.state.access_mode is WebAccessMode.EXTERNAL_READ_ONLY
            ),
            "can_confirm_transactions": (
                request.state.access_mode is WebAccessMode.LOCAL_FULL
                or settings.external_confirmation_enabled
            ),
            **values,
        }

    def templates_response(
        request: Request, name: str, values: dict[str, object]
    ) -> HTMLResponse:
        return templates.TemplateResponse(request=request, name=name, context=values)

    @application.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse()

    def local_money_overview(request: Request, window: int) -> HTMLResponse:
        """Render the offline-capable local landing page from cached reads."""
        now = datetime.now(UTC)
        overview = dependencies.consolidated.overview(now=now, recent_days=window)
        return templates_response(
            request,
            "money_home.html",
            context(
                request,
                overview=overview,
                recent_days=_group_recent_transactions(
                    overview.parsed_transactions.recent_transactions,
                    timezone=settings.local_timezone,
                    now=now,
                ),
                recent_window=window,
                recent_window_choices=_RECENT_WINDOW_CHOICES,
                cash=PaymentMethod.CASH,
                card=PaymentMethod.CREDIT_CARD,
            ),
        )

    @application.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, days: int | None = None) -> HTMLResponse:
        window = _recent_window(days)
        if request.state.access_mode is WebAccessMode.LOCAL_FULL:
            return local_money_overview(request, window)
        now = datetime.now(UTC)
        snapshot = dependencies.queries.dashboard(now=now, recent_days=window)
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context=context(
                request,
                dashboard=snapshot,
                recent_days=_group_recent_transactions(
                    snapshot.recent_transactions,
                    timezone=settings.local_timezone,
                    now=now,
                ),
                recent_window=window,
                recent_window_choices=_RECENT_WINDOW_CHOICES,
            ),
        )

    @application.get("/transactions", response_class=HTMLResponse)
    def transactions(
        request: Request,
        page: int = 1,
        date_from: str | None = None,
        date_to: str | None = None,
        merchant: str | None = None,
        sender: str | None = None,
        amount_min: int | None = None,
        amount_max: int | None = None,
        currency: str | None = None,
        transaction_type: str | None = None,
        confidence_min: str | None = None,
        review_status: str | None = None,
        sort: str = "transaction_at_desc",
    ) -> HTMLResponse:
        criteria = TransactionSearchCriteria(
            page=max(1, page),
            date_from=_optional_date(date_from),
            date_to=_optional_date(date_to),
            merchant=_clean_optional(merchant),
            sender=_clean_optional(sender),
            amount_min=amount_min,
            amount_max=amount_max,
            currency=_optional_currency(currency),
            transaction_type=_optional_enum(TransactionType, transaction_type),
            confidence_min=_optional_decimal(confidence_min),
            review_status=(
                review_status if review_status in _SAFE_REVIEW_STATUSES else None
            ),
            sort=sort if sort in _SAFE_SORTS else "transaction_at_desc",
        )
        result = dependencies.queries.search_transactions(criteria)
        template = (
            "partials/transaction_results.html"
            if request.headers.get("HX-Request") == "true"
            else "transactions.html"
        )
        return templates.TemplateResponse(
            request=request,
            name=template,
            context=context(request, result=result, criteria=criteria),
        )

    @application.post("/transactions/confirm")
    async def confirm_transactions(request: Request) -> Response:
        try:
            form = await _validated_form(request, settings)
            transaction_ids = tuple(
                int(value) for value in form.get("transaction_ids", ())
            )
            result = dependencies.confirm_transactions.execute(transaction_ids)
            return_to = _safe_return_target(_first(form, "return_to"))
        except (ValueError, ScribeError) as error:
            return HTMLResponse(f"無法確認交易: {error}", status_code=422)
        if request.state.access_mode is WebAccessMode.LOCAL_FULL and _checked(
            form, "continue_to_allocation"
        ):
            return _redirect(request, _confirmation_allocation_target(result))
        return _redirect(request, return_to)

    @application.get("/transactions/{transaction_id}", response_class=HTMLResponse)
    def transaction_detail(
        request: Request,
        transaction_id: int,
        return_to: str | None = None,
    ) -> HTMLResponse:
        detail = dependencies.queries.transaction_detail(transaction_id)
        if detail is None:
            return _not_found("找不到這筆交易")
        return templates.TemplateResponse(
            request=request,
            name="transaction_detail.html",
            context=context(
                request,
                detail=detail,
                return_to=_safe_return_target(return_to),
                transaction_types=tuple(TransactionType),
            ),
        )

    @application.post("/transactions/{transaction_id}/review")
    async def review_transaction(request: Request, transaction_id: int) -> Response:
        try:
            form = await _validated_form(request, settings)
            overrides = frozenset(form.get("override_fields", ()))
            manually_ignored = _checked(form, "manually_ignored")
            dependencies.review_transaction.execute(
                ReviewTransactionRequest(
                    transaction_id=transaction_id,
                    expected_updated_at=datetime.fromisoformat(
                        _required(form, "expected_updated_at")
                    ),
                    override_fields=overrides,
                    transaction_type=TransactionType(
                        _required(form, "transaction_type")
                    ),
                    merchant=_first(form, "merchant"),
                    amount_minor=_optional_display_amount(
                        _first(form, "amount_minor"), _first(form, "currency")
                    ),
                    currency=_first(form, "currency"),
                    transaction_at=_local_form_datetime(
                        _first(form, "transaction_at"), settings.local_timezone
                    ),
                    manually_confirmed=(
                        _checked(form, "manually_confirmed") and not manually_ignored
                    ),
                    manually_ignored=manually_ignored,
                    user_note=_first(form, "user_note"),
                )
            )
        except ConcurrentUpdateError as error:
            return HTMLResponse(str(error), status_code=409)
        except (ValueError, ScribeError) as error:
            return HTMLResponse(f"無法儲存: {error}", status_code=422)
        return _redirect(request, f"/transactions/{transaction_id}?saved=1")

    @application.post("/transactions/{transaction_id}/reparse")
    async def reparse_transaction(request: Request, transaction_id: int) -> Response:
        try:
            await _validated_form(request, settings)
            detail = dependencies.queries.transaction_detail(transaction_id)
            if detail is None:
                return _not_found("找不到這筆交易")
            dependencies.reparse_email.execute(detail.item.email_id)
        except (ValueError, ScribeError) as error:
            return HTMLResponse(f"無法重新解析: {error}", status_code=422)
        return _redirect(request, f"/transactions/{transaction_id}?reparsed=1")

    @application.post("/transactions/{transaction_id}/supplementary-card")
    async def set_supplementary_card(request: Request, transaction_id: int) -> Response:
        try:
            form = await _validated_form(request, settings)
            value = _required(form, "supplementary")
            if value not in {"0", "1"}:
                raise ValueError("invalid supplementary-card state")
            dependencies.set_supplementary_card.execute(
                SetSupplementaryCardRequest(
                    transaction_id=transaction_id,
                    supplementary=value == "1",
                )
            )
        except (ValueError, ScribeError) as error:
            return HTMLResponse(f"無法儲存副卡設定: {error}", status_code=422)
        return _redirect(request, f"/transactions/{transaction_id}?card_saved=1")

    @application.get("/cards", response_class=HTMLResponse)
    def cards(request: Request) -> HTMLResponse:
        setup = dependencies.manage_cards.setup_view()
        return templates.TemplateResponse(
            request=request,
            name="cards.html",
            context=context(
                request,
                cards=dependencies.money_review.cards(),
                accounts=dependencies.money_balances.account_balances(),
                holder_roles=tuple(CardHolderRole),
                gmail_accounts=setup.gmail_accounts,
                sender_rules=setup.sender_rules,
                candidates=setup.candidates,
                assignments=setup.assignments,
            ),
        )

    @application.post("/cards")
    async def create_card(request: Request) -> Response:
        try:
            form = await _validated_form(request, settings)
            dependencies.manage_cards.register(
                RegisterCardRequest(
                    gmail_account_id=int(_required(form, "gmail_account_id")),
                    issuer_code=_required(form, "issuer_code"),
                    display_name=_required(form, "display_name"),
                    last_four=_required(form, "last_four"),
                    holder_role=_card_holder_role(_first(form, "holder_role")),
                )
            )
        except (ValueError, ScribeError) as error:
            return HTMLResponse(f"無法新增卡片: {error}", status_code=422)
        return _redirect(request, "/cards?created=1")

    @application.post("/cards/{card_id}")
    async def configure_card(request: Request, card_id: int) -> Response:
        try:
            form = await _validated_form(request, settings)
            dependencies.manage_cards.configure(
                ConfigureCardRequest(
                    card_id=card_id,
                    expected_version=int(_required(form, "version")),
                    display_name=_required(form, "display_name"),
                    holder_role=_card_holder_role(_first(form, "holder_role")),
                    liability_account_id=_optional_int(
                        _first(form, "liability_account_id")
                    ),
                    settlement_account_id=_optional_int(
                        _first(form, "settlement_account_id")
                    ),
                    default_funding_wallet_id=_optional_int(
                        _first(form, "default_funding_wallet_id")
                    ),
                )
            )
        except ConcurrentUpdateError:
            return HTMLResponse("卡片設定已被更新: 請重新載入頁面", status_code=409)
        except (ValueError, ScribeError) as error:
            return HTMLResponse(f"無法儲存卡片設定: {error}", status_code=422)
        return _redirect(request, "/cards?saved=1")

    @application.post("/cards/import-candidates/{import_row_id}/assign")
    async def assign_card_import_candidate(
        request: Request, import_row_id: int
    ) -> Response:
        try:
            form = await _validated_form(request, settings)
            dependencies.manage_cards.assign_import_candidate(
                import_row_id=import_row_id,
                card_id=int(_required(form, "card_id")),
            )
        except (ValueError, ScribeError) as error:
            return HTMLResponse(f"無法承接匯入設定: {error}", status_code=422)
        return _redirect(request, "/cards?assigned=1")

    @application.get("/money/review", response_class=HTMLResponse)
    def money_review(request: Request, event_id: int | None = None) -> HTMLResponse:
        events = dependencies.money_review.events_needing_review()
        if event_id is not None:
            events = tuple(view for view in events if view.event.id == event_id)
        accounts = dependencies.money_balances.account_balances()
        dedicated_accounts = tuple(
            account
            for account in accounts
            if any(
                wallet.is_active and wallet.purpose is WalletPurpose.CLEARING_HUB
                for wallet in account.wallets
            )
        )
        categories = dependencies.manage_budget_plan.list_categories()
        return templates.TemplateResponse(
            request=request,
            name="money_review.html",
            context=context(
                request,
                events=events,
                selected_event_id=event_id,
                categories=categories,
                accounts=accounts,
                dedicated_accounts=dedicated_accounts,
                wallet_category_hints=_wallet_category_hints(accounts, categories),
                client_token=secrets.token_urlsafe(16),
            ),
        )

    @application.get("/budget", response_class=HTMLResponse)
    def budget_current(request: Request) -> Response:
        today = datetime.now(ZoneInfo(settings.local_timezone)).date()
        return _redirect(request, f"/budget/{today:%Y-%m}")

    @application.get("/budget/{label}", response_class=HTMLResponse)
    def budget_month(request: Request, label: str) -> Response:
        try:
            year, month = parse_period_label(label)
        except BudgetPeriodError as error:
            return HTMLResponse(f"無法顯示預算月份: {error}", status_code=404)
        currency = request.query_params.get("currency")
        return templates.TemplateResponse(
            request=request,
            name="budget.html",
            context=context(
                request,
                view=dependencies.budget.month(
                    year=year,
                    month=month,
                    currency=currency or settings.default_currency,
                ),
                close_preview=_close_preview(
                    dependencies, year, month, currency or settings.default_currency
                ),
                categories=dependencies.manage_budget_plan.list_categories(),
                accounts=dependencies.money_balances.account_balances(),
            ),
        )

    @application.post("/budget/categories")
    async def create_budget_category(request: Request) -> Response:
        try:
            form = await _validated_form(request, settings)
            dependencies.manage_budget_plan.add_category(
                AddCategoryRequest(
                    name=_required(form, "name"),
                    parent_id=_optional_int(_first(form, "parent_id")),
                )
            )
        except (ValueError, ScribeError) as error:
            return HTMLResponse(f"無法新增分類: {error}", status_code=422)
        return _redirect(request, _safe_return_target(_first(form, "return_to")))

    @application.post("/budget/categories/{category_id}")
    async def update_budget_category(request: Request, category_id: int) -> Response:
        try:
            form = await _validated_form(request, settings)
            dependencies.manage_budget_plan.update_category(
                UpdateCategoryRequest(
                    category_id=category_id,
                    expected_version=int(_required(form, "version")),
                    name=_required(form, "name"),
                    parent_id=_optional_int(_first(form, "parent_id")),
                    is_active=_checked(form, "is_active"),
                )
            )
        except ConcurrentUpdateError:
            return HTMLResponse("分類已被更新: 請重新載入頁面", status_code=409)
        except (ValueError, ScribeError) as error:
            return HTMLResponse(f"無法更新分類: {error}", status_code=422)
        return _redirect(request, _safe_return_target(_first(form, "return_to")))

    @application.post("/budget/{label}/targets")
    async def set_budget_target(request: Request, label: str) -> Response:
        try:
            year, month = parse_period_label(label)
            form = await _validated_form(request, settings)
            target_currency = _first(form, "currency") or settings.default_currency
            dependencies.manage_budget_plan.set_target(
                SetTargetRequest(
                    year=year,
                    month=month,
                    currency=target_currency,
                    budget_category_id=int(_required(form, "budget_category_id")),
                    target_minor=parse_display_amount(
                        _required(form, "target"), target_currency, allow_zero=True
                    ),
                    buffer_minor=(
                        parse_display_amount(
                            _first(form, "buffer"), target_currency, allow_zero=True
                        )
                        if (_first(form, "buffer") or "").strip()
                        else 0
                    ),
                    funding_wallet_id=int(_required(form, "funding_wallet_id")),
                    month_end_destination_wallet_id=_optional_int(
                        _first(form, "month_end_destination_wallet_id")
                    ),
                    expected_version=_optional_int(_first(form, "version")),
                )
            )
        except ConcurrentUpdateError:
            return HTMLResponse("預算目標已被更新: 請重新載入頁面", status_code=409)
        except (ValueError, ScribeError) as error:
            return HTMLResponse(f"無法儲存預算目標: {error}", status_code=422)
        return _redirect(request, f"/budget/{label}?saved=1")

    @application.post("/budget/{label}/close")
    async def close_budget_month(request: Request, label: str) -> Response:
        try:
            year, month = parse_period_label(label)
            form = await _validated_form(request, settings)
            dependencies.close_budget_period.execute(
                year=year,
                month=month,
                currency=_first(form, "currency") or settings.default_currency,
            )
        except (ValueError, ScribeError) as error:
            return HTMLResponse(f"無法結算月份: {error}", status_code=422)
        return _redirect(request, f"/budget/{label}?closed=1")

    @application.post("/money/review/{event_id}/allocate")
    async def allocate_event(request: Request, event_id: int) -> Response:
        try:
            form = await _validated_form(request, settings)
            dependencies.allocate_event.execute(
                AllocateFinancialEventRequest(
                    financial_event_id=event_id,
                    expected_version=int(_required(form, "version")),
                    client_token=_required(form, "client_token"),
                    lines=_allocation_lines(form, _required(form, "currency")),
                )
            )
        except (ValueError, ScribeError) as error:
            return HTMLResponse(f"無法分配此筆收支: {error}", status_code=422)
        return _redirect(request, "/money/review?allocated=1")

    # ----- B7: daily spending, accounts, matching, reconciliation ---------
    # Every route below is absent from the external read-only allow-list, so
    # the external hostname answers 404 for all of them (ADR-B068).

    @application.get("/money", response_class=HTMLResponse)
    def money_home(request: Request, days: int | None = None) -> HTMLResponse:
        return local_money_overview(request, _recent_window(days))

    @application.get("/more", response_class=HTMLResponse)
    def more_features(request: Request) -> HTMLResponse:
        """Keep secondary workflows reachable without crowding phone chrome."""
        return templates_response(request, "more.html", context(request))

    @application.get("/money/daily", response_class=HTMLResponse)
    def money_daily(
        request: Request,
        day: str | None = None,
        method: str | None = None,
        page: int = 1,
    ) -> Response:
        selected = _optional_date(day)
        if day and selected is None:
            return HTMLResponse("無法顯示這一天: 日期格式不正確", status_code=404)
        return templates_response(
            request,
            "money_daily.html",
            context(
                request,
                view=dependencies.spending.daily(
                    day=selected, payment_filter=method, page=max(1, page)
                ),
                cash=PaymentMethod.CASH,
                card=PaymentMethod.CREDIT_CARD,
            ),
        )

    def _expense_form(
        request: Request,
        *,
        mode: str,
        values: dict[str, str],
        error: str | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        """Render the expense form, keeping whatever was safely submitted.

        A rejected submission returns the form rather than a bare sentence, so
        a mistyped amount does not cost the user everything else they typed.
        Jinja autoescapes every value on the way back out.
        """
        page = templates_response(
            request,
            "money_expense_form.html",
            context(
                request,
                mode=mode,
                values=values,
                error=error,
                accounts=dependencies.spending.accounts(),
                cards=dependencies.money_review.cards(),
                categories=dependencies.manage_budget_plan.list_categories(),
                default_currency=settings.default_currency,
                now_local=datetime.now(ZoneInfo(settings.local_timezone)),
                client_token=values.get("client_token") or secrets.token_urlsafe(16),
            ),
        )
        page.status_code = status_code
        return page

    @application.get("/money/expenses/new", response_class=HTMLResponse)
    def new_manual_expense(request: Request, mode: str | None = None) -> HTMLResponse:
        return _expense_form(
            request, mode="card" if mode == "card" else "cash", values={}
        )

    @application.post("/money/expenses")
    async def create_manual_expense(request: Request) -> Response:
        form: dict[str, list[str]] = {}
        try:
            form = await _validated_form(request, settings)
        except ValueError as error:
            return HTMLResponse(f"無法記錄這筆支出: {error}", status_code=422)
        values = _expense_values(form)
        mode = "card" if values.get("payment_method") == "credit_card" else "cash"
        try:
            # The payment method is decoded strictly. Treating every unknown
            # value as cash would let a tampered POST post a card purchase as
            # cash, which moves a real wallet balance instead of a liability.
            method = _manual_payment_method(values.get("payment_method"))
            currency = _required(form, "currency")
            occurred_at = _local_form_datetime(
                _required(form, "occurred_at"), settings.local_timezone
            )
            if occurred_at is None:  # pragma: no cover - guarded by _required
                raise ValueError("occurred_at is required")
            dependencies.manual_expense.execute(
                ManualExpenseRequest(
                    payment_method=method,
                    occurred_at=occurred_at,
                    merchant=_required(form, "merchant"),
                    amount_minor=parse_display_amount(
                        _required(form, "amount"), currency
                    ),
                    currency=currency,
                    funding_wallet_id=int(_required(form, "funding_wallet_id")),
                    budget_category_id=int(_required(form, "budget_category_id")),
                    client_token=_required(form, "client_token"),
                    card_id=(
                        int(_required(form, "card_id"))
                        if method is PaymentMethod.CREDIT_CARD
                        else None
                    ),
                    note=_clean_optional(_first(form, "note")),
                    clearing_wallet_id=_dedicated_clearing_wallet_id(form),
                )
            )
        except ConcurrentUpdateError:
            return HTMLResponse("資料已被更新: 請重新載入頁面", status_code=409)
        except (ValueError, TypeError, ScribeError) as error:
            return _expense_form(
                request,
                mode=mode,
                values=values,
                error=f"無法記錄這筆支出: {error}",
                status_code=422,
            )
        return _redirect(request, "/money/daily?recorded=1")

    @application.get("/money/events/{event_id}", response_class=HTMLResponse)
    def money_event(request: Request, event_id: int) -> HTMLResponse:
        detail = dependencies.spending.event(event_id)
        if detail is None:
            return _not_found("找不到這筆收支")
        return templates_response(
            request,
            "money_event.html",
            context(
                request,
                detail=detail,
                note_value=detail.event.note or "",
                note_error=None,
            ),
        )

    @application.post("/money/events/{event_id}/note")
    async def update_money_event_note(request: Request, event_id: int) -> Response:
        form: dict[str, list[str]] = {}
        try:
            form = await _validated_form(request, settings)
            dependencies.update_event_note.execute(
                UpdateFinancialEventNoteRequest(
                    financial_event_id=event_id,
                    expected_version=int(_required(form, "version")),
                    note=_clean_optional(_first(form, "note")),
                )
            )
        except ConcurrentUpdateError:
            return HTMLResponse(
                "這筆收支已被更新\N{FULLWIDTH COMMA}請重新載入後再編輯備註",
                status_code=409,
            )
        except (ValueError, TypeError, ScribeError) as error:
            detail = dependencies.spending.event(event_id)
            if detail is None:
                return _not_found("找不到這筆收支")
            page = templates_response(
                request,
                "money_event.html",
                context(
                    request,
                    detail=detail,
                    note_value=_first(form, "note") or "",
                    note_error=f"無法儲存備註: {error}",
                ),
            )
            page.status_code = 422
            return page
        return _redirect(request, f"/money/events/{event_id}?note_saved=1")

    @application.post("/money/events/{event_id}/reverse")
    async def reverse_money_event(request: Request, event_id: int) -> Response:
        try:
            form = await _validated_form(request, settings)
            dependencies.reverse_event.execute(
                ReverseEventRequest(
                    financial_event_id=event_id,
                    expected_version=int(_required(form, "version")),
                )
            )
        except ConcurrentUpdateError:
            return HTMLResponse("這筆收支已被更新: 請重新載入頁面", status_code=409)
        except (ValueError, TypeError, ScribeError) as error:
            return HTMLResponse(f"無法沖銷這筆收支: {error}", status_code=422)
        return _redirect(request, f"/money/events/{event_id}?reversed=1")

    @application.post("/money/events/{event_id}/allocation-redo")
    async def redo_event_allocation(request: Request, event_id: int) -> Response:
        try:
            form = await _validated_form(request, settings)
            dependencies.redo_allocation.execute(
                RedoAllocationRequest(
                    financial_event_id=event_id,
                    expected_version=int(_required(form, "version")),
                )
            )
        except ConcurrentUpdateError:
            return HTMLResponse("這筆收支已被更新: 請重新載入頁面", status_code=409)
        except (ValueError, TypeError, ScribeError) as error:
            return HTMLResponse(f"無法重新分配這筆收支: {error}", status_code=422)
        return _redirect(request, f"/money/review?event_id={event_id}")

    @application.get("/money/accounts", response_class=HTMLResponse)
    def money_accounts(request: Request) -> HTMLResponse:
        return templates_response(
            request,
            "money_accounts.html",
            context(
                request,
                accounts=dependencies.spending.accounts(),
                client_token=secrets.token_urlsafe(16),
            ),
        )

    # Declared before "/money/accounts/{account_id}" so this literal path is
    # not captured as an account id.
    @application.get("/money/accounts/order", response_class=HTMLResponse)
    def arrange_accounts(request: Request) -> HTMLResponse:
        """Show the sheet's account columns so their order can be retyped."""
        return templates_response(
            request,
            "money_account_order.html",
            context(request, accounts=dependencies.spending.accounts()),
        )

    @application.get("/money/accounts/new", response_class=HTMLResponse)
    def new_money_account(request: Request) -> HTMLResponse:
        requested_type = request.query_params.get("type", "checking")
        try:
            account_type = FinancialAccountType(requested_type)
        except ValueError:
            account_type = FinancialAccountType.CHECKING
        return templates_response(
            request,
            "money_account_form.html",
            context(
                request,
                account_types=tuple(FinancialAccountType),
                selected_type=account_type,
                return_to=request.query_params.get("return_to", "accounts"),
                now_local=datetime.now(ZoneInfo(settings.local_timezone)),
            ),
        )

    @application.post("/money/accounts")
    async def create_money_account(request: Request) -> Response:
        try:
            form = await _validated_form(request, settings)
            currency = _required(form, "currency").upper()
            amount_text = (_first(form, "opening_balance") or "").strip()
            amount_minor = (
                parse_display_amount(amount_text, currency) if amount_text else 0
            )
            observed_at = _local_form_datetime(
                _first(form, "observed_at"), settings.local_timezone
            )
            dependencies.ledger_setup.open_account_with_opening(
                OpenAccountWithOpeningRequest(
                    account=OpenFinancialAccountRequest(
                        institution_code=_required(form, "institution_code"),
                        display_name=_required(form, "display_name"),
                        account_type=FinancialAccountType(
                            _required(form, "account_type")
                        ),
                        currency=currency,
                        masked_reference=(
                            (_first(form, "masked_reference") or "").strip() or None
                        ),
                    ),
                    opening_balance_minor=amount_minor,
                    observed_at=observed_at,
                )
            )
            return_to = _first(form, "return_to")
        except (ValueError, TypeError, ScribeError) as error:
            return HTMLResponse(f"無法新增帳戶: {error}", status_code=422)
        if return_to == "cards":
            return _redirect(request, "/cards?account_created=1")
        return _redirect(request, "/money/accounts?created=1")

    @application.get("/money/accounts/{account_id}", response_class=HTMLResponse)
    def money_account(request: Request, account_id: int) -> HTMLResponse:
        detail = dependencies.spending.account(account_id)
        if detail is None:
            return _not_found("找不到這個帳戶")
        return templates_response(
            request,
            "money_account.html",
            context(
                request,
                detail=detail,
                now_local=datetime.now(ZoneInfo(settings.local_timezone)),
                client_token=secrets.token_urlsafe(16),
            ),
        )

    @application.get("/money/bank-movements/new", response_class=HTMLResponse)
    def new_bank_movement(request: Request) -> HTMLResponse:
        return templates_response(
            request,
            "money_bank_movement_form.html",
            context(
                request,
                accounts=dependencies.spending.accounts(),
                categories=dependencies.manage_budget_plan.list_categories(),
                kinds=tuple(BankMovementKind),
                kind_labels=_BANK_MOVEMENT_KIND_LABELS,
                spending_kinds=tuple(kind.value for kind in SPENDING_BANK_MOVEMENTS),
                default_currency=settings.default_currency,
                now_local=datetime.now(ZoneInfo(settings.local_timezone)),
                client_token=secrets.token_urlsafe(16),
                transfer_token=secrets.token_urlsafe(16),
            ),
        )

    @application.post("/money/bank-movements")
    async def create_bank_movement(request: Request) -> Response:
        try:
            form = await _validated_form(request, settings)
            currency = _required(form, "currency")
            occurred_at = _local_form_datetime(
                _required(form, "occurred_at"), settings.local_timezone
            )
            if occurred_at is None:  # pragma: no cover - guarded by _required
                raise ValueError("occurred_at is required")
            dependencies.bank_movements.execute(
                BankMovementRequest(
                    kind=BankMovementKind(_required(form, "kind")),
                    occurred_at=occurred_at,
                    amount_minor=parse_display_amount(
                        _required(form, "amount"), currency
                    ),
                    currency=currency,
                    wallet_id=int(_required(form, "wallet_id")),
                    description=_required(form, "description"),
                    client_token=_required(form, "client_token"),
                    budget_category_id=_optional_int(
                        _first(form, "budget_category_id")
                    ),
                    note=_clean_optional(_first(form, "note")),
                )
            )
        except (ValueError, TypeError, ScribeError) as error:
            return HTMLResponse(f"無法記錄這筆銀行異動: {error}", status_code=422)
        return _redirect(request, "/money/accounts?recorded=1")

    @application.post("/money/account-transfers")
    async def create_account_transfer(request: Request) -> Response:
        try:
            form = await _validated_form(request, settings)
            currency = _required(form, "currency")
            occurred_at = _local_form_datetime(
                _required(form, "occurred_at"), settings.local_timezone
            )
            if occurred_at is None:  # pragma: no cover - guarded by _required
                raise ValueError("occurred_at is required")
            dependencies.bank_movements.transfer(
                AccountTransferRequest(
                    source_wallet_id=int(_required(form, "source_wallet_id")),
                    destination_wallet_id=int(_required(form, "destination_wallet_id")),
                    occurred_at=occurred_at,
                    amount_minor=parse_display_amount(
                        _required(form, "amount"), currency
                    ),
                    currency=currency,
                    description=_required(form, "description"),
                    client_token=_required(form, "client_token"),
                    note=_clean_optional(_first(form, "note")),
                )
            )
        except (ValueError, TypeError, ScribeError) as error:
            return HTMLResponse(f"無法記錄這筆轉帳: {error}", status_code=422)
        return _redirect(request, "/money/accounts?transferred=1")

    @application.post("/money/accounts/order")
    async def save_account_order(request: Request) -> Response:
        try:
            form = await _validated_form(request, settings)
            dependencies.ledger_setup.arrange_accounts(
                ArrangeAccountsRequest(positions=_submitted_positions(form))
            )
        except (ValueError, TypeError, ScribeError) as error:
            return HTMLResponse(f"無法儲存排序: {error}", status_code=422)
        return _redirect(request, "/money/accounts?arranged=accounts")

    @application.get("/money/accounts/{account_id}/order", response_class=HTMLResponse)
    def arrange_account_wallets(request: Request, account_id: int) -> HTMLResponse:
        """Show one account's wallets so their reading order can be retyped."""
        detail = dependencies.spending.account(account_id)
        if detail is None:
            return _not_found("找不到這個帳戶")
        return templates_response(
            request,
            "money_wallet_order.html",
            context(request, row=detail.row),
        )

    @application.post("/money/accounts/{account_id}/order")
    async def save_account_wallet_order(request: Request, account_id: int) -> Response:
        try:
            form = await _validated_form(request, settings)
            dependencies.ledger_setup.arrange_wallets(
                ArrangeWalletsRequest(
                    financial_account_id=account_id,
                    positions=_submitted_positions(form),
                )
            )
        except (ValueError, TypeError, ScribeError) as error:
            return HTMLResponse(f"無法儲存排序: {error}", status_code=422)
        return _redirect(request, "/money/accounts?arranged=wallets")

    @application.get(
        "/money/accounts/{account_id}/wallets/rename", response_class=HTMLResponse
    )
    def rename_account_wallets(request: Request, account_id: int) -> HTMLResponse:
        """Show one account's wallets so their names can be retyped."""
        detail = dependencies.spending.account(account_id)
        if detail is None:
            return _not_found("找不到這個帳戶")
        return templates_response(
            request,
            "money_wallet_rename.html",
            context(request, row=detail.row),
        )

    @application.post("/money/accounts/{account_id}/wallets/rename")
    async def save_account_wallet_names(request: Request, account_id: int) -> Response:
        try:
            form = await _validated_form(request, settings)
            # One command, one transaction: renaming them one at a time left
            # the earlier names stored when a later one was refused.
            dependencies.ledger_setup.rename_wallets(
                RenameWalletsRequest(
                    financial_account_id=account_id,
                    names=_submitted_wallet_names(form),
                    expected_versions=_submitted_wallet_versions(form),
                )
            )
        except ConcurrentUpdateError:
            return HTMLResponse("小錢包名稱已被更新: 請重新載入頁面", status_code=409)
        except (ValueError, TypeError, ScribeError) as error:
            return HTMLResponse(f"無法儲存名稱: {error}", status_code=422)
        return _redirect(request, f"/money/accounts/{account_id}?renamed=wallets")

    @application.post("/money/wallets/{wallet_id}/balance")
    async def reallocate_wallet(request: Request, wallet_id: int) -> Response:
        """Apply one edited sheet cell as a move inside the same account."""
        try:
            form = await _validated_form(request, settings)
            currency = _required(form, "currency")
            applied = dependencies.bank_movements.reallocate(
                ReallocateWalletRequest(
                    wallet_id=wallet_id,
                    counterpart_wallet_id=int(_required(form, "counterpart_wallet_id")),
                    expected_balance_minor=int(
                        _required(form, "expected_balance_minor")
                    ),
                    target_balance_minor=parse_display_amount(
                        _required(form, "target_balance"), currency, allow_signed=True
                    ),
                    currency=currency,
                    occurred_at=datetime.now(UTC),
                    client_token=_required(form, "client_token"),
                )
            )
        except (ValueError, TypeError, ScribeError) as error:
            return HTMLResponse(f"無法重分配這個小錢包: {error}", status_code=422)
        return _redirect(
            request, "/money/accounts?reallocated=1" if applied else "/money/accounts"
        )

    @application.post("/money/refunds")
    async def create_manual_refund(request: Request) -> Response:
        try:
            form = await _validated_form(request, settings)
            original_id = int(_required(form, "original_event_id"))
            original = dependencies.spending.event(original_id)
            if original is None:
                return _not_found("找不到要退款的原始收支")
            occurred_at = _local_form_datetime(
                _required(form, "occurred_at"), settings.local_timezone
            )
            if occurred_at is None:  # pragma: no cover - guarded by _required
                raise ValueError("occurred_at is required")
            dependencies.bank_movements.refund(
                ManualRefundRequest(
                    original_event_id=original_id,
                    occurred_at=occurred_at,
                    # The currency is read from the event being refunded, never
                    # from the form: a tampered code would rescale the amount.
                    amount_minor=parse_display_amount(
                        _required(form, "amount"), original.event.currency
                    ),
                    client_token=_required(form, "client_token"),
                    note=_clean_optional(_first(form, "note")),
                )
            )
        except (ValueError, TypeError, ScribeError) as error:
            return HTMLResponse(f"無法記錄這筆退款: {error}", status_code=422)
        return _redirect(request, f"/money/events/{original_id}?refunded=1")

    @application.get("/money/source-links", response_class=HTMLResponse)
    def money_source_links(request: Request) -> HTMLResponse:
        return templates_response(
            request,
            "money_source_links.html",
            context(request, view=dependencies.spending.source_links()),
        )

    @application.post("/money/source-links/{link_id}/confirm")
    async def confirm_source_link(request: Request, link_id: int) -> Response:
        try:
            await _validated_form(request, settings)
            dependencies.source_review.confirm(link_id)
        except ConcurrentUpdateError:
            return HTMLResponse("這筆比對已被處理: 請重新載入頁面", status_code=409)
        except (ValueError, ScribeError) as error:
            return HTMLResponse(f"無法確認這筆比對: {error}", status_code=422)
        return _redirect(request, "/money/source-links?confirmed=1")

    @application.post("/money/source-links/{link_id}/reject")
    async def reject_source_link(request: Request, link_id: int) -> Response:
        try:
            await _validated_form(request, settings)
            dependencies.source_review.reject(link_id)
        except ConcurrentUpdateError:
            return HTMLResponse("這筆比對已被處理: 請重新載入頁面", status_code=409)
        except (ValueError, ScribeError) as error:
            return HTMLResponse(f"無法排除這筆比對: {error}", status_code=422)
        return _redirect(request, "/money/source-links?rejected=1")

    @application.post("/money/source-transactions/{transaction_id}/separate")
    async def declare_source_separate(
        request: Request, transaction_id: int
    ) -> Response:
        try:
            await _validated_form(request, settings)
            dependencies.source_review.declare_separate(transaction_id)
        except ConcurrentUpdateError:
            return HTMLResponse("這筆比對已被處理: 請重新載入頁面", status_code=409)
        except (ValueError, ScribeError) as error:
            return HTMLResponse(f"無法另外記錄這筆通知: {error}", status_code=422)
        return _redirect(request, "/money/source-links?separated=1")

    @application.get("/money/reconcile", response_class=HTMLResponse)
    def money_reconcile(request: Request) -> HTMLResponse:
        return templates_response(
            request,
            "money_reconcile.html",
            context(
                request,
                view=dependencies.spending.reconciliation(),
                now_local=datetime.now(ZoneInfo(settings.local_timezone)),
                client_token=secrets.token_urlsafe(16),
                adjust_token=secrets.token_urlsafe(16),
            ),
        )

    @application.post("/money/reconcile/snapshots")
    async def create_balance_snapshot(request: Request) -> Response:
        try:
            form = await _validated_form(request, settings)
            account_id = int(_required(form, "financial_account_id"))
            currency = _account_currency(dependencies, account_id)
            observed_at = _local_form_datetime(
                _required(form, "observed_at"), settings.local_timezone
            )
            if observed_at is None:  # pragma: no cover - guarded by _required
                raise ValueError("observed_at is required")
            dependencies.balance_snapshots.execute(
                BalanceSnapshotRequest(
                    financial_account_id=account_id,
                    observed_balance_minor=parse_display_amount(
                        _required(form, "observed_balance"),
                        currency,
                        allow_signed=True,
                    ),
                    observed_at=observed_at,
                    client_token=_required(form, "client_token"),
                    note=_clean_optional(_first(form, "note")),
                )
            )
        except (ValueError, TypeError, ScribeError) as error:
            return HTMLResponse(f"無法記錄餘額觀測: {error}", status_code=422)
        return _redirect(request, "/money/reconcile?observed=1")

    @application.post("/money/reconcile/{snapshot_id}/adjust")
    async def adjust_reconciliation(request: Request, snapshot_id: int) -> Response:
        try:
            form = await _validated_form(request, settings)
            view = dependencies.balance_snapshots.view(snapshot_id)
            dependencies.reconciliation.execute(
                ReconciliationAdjustmentRequest(
                    snapshot_id=snapshot_id,
                    expected_difference_minor=parse_display_amount(
                        _required(form, "expected_difference"),
                        view.account.currency,
                        allow_signed=True,
                    ),
                    reason=_required(form, "reason"),
                    client_token=_required(form, "client_token"),
                )
            )
        except StaleReconciliationError as error:
            return HTMLResponse(f"{error}", status_code=409)
        except (ValueError, TypeError, ScribeError) as error:
            return HTMLResponse(f"無法調整差額: {error}", status_code=422)
        return _redirect(request, "/money/reconcile?adjusted=1")

    @application.get("/subscriptions", response_class=HTMLResponse)
    def subscriptions(request: Request) -> Response:
        label = request.query_params.get("period")
        today = datetime.now(ZoneInfo(settings.local_timezone)).date()
        try:
            year, month = (
                parse_period_label(label) if label else (today.year, today.month)
            )
        except BudgetPeriodError as error:
            return HTMLResponse(f"無法顯示訂閱月份: {error}", status_code=404)
        currencies = dependencies.subscription_requirement.currencies() or (
            settings.default_currency,
        )
        selected = _optional_currency(request.query_params.get("currency"))
        shown = (selected,) if selected in currencies else currencies
        return templates.TemplateResponse(
            request=request,
            name="subscriptions.html",
            context=context(
                request,
                label=f"{year:04d}-{month:02d}",
                requirements=tuple(
                    dependencies.subscription_requirement.execute(
                        year=year, month=month, currency=code
                    )
                    for code in shown
                ),
                currencies=currencies,
                selected_currency=selected if selected in currencies else None,
                register=dependencies.subscription_register.register(),
                default_wallet=(
                    dependencies.manage_subscriptions.default_funding_wallet()
                ),
                categories=dependencies.manage_budget_plan.list_categories(),
                accounts=dependencies.money_balances.account_balances(),
                cadences=tuple(SubscriptionCadence),
                reason_labels=_REASON_LABELS,
                cadence_labels=_CADENCE_LABELS,
                wallet_problem_labels=_WALLET_PROBLEM_LABELS,
            ),
        )

    @application.post("/subscriptions")
    async def create_subscription(request: Request) -> Response:
        try:
            form = await _validated_form(request, settings)
            dependencies.manage_subscriptions.add(
                AddSubscriptionRequest(
                    name=_required(form, "name"),
                    budget_category_id=int(_required(form, "budget_category_id")),
                    funding_wallet_id=int(_required(form, "funding_wallet_id")),
                    currency=_required(form, "currency"),
                    billing_cadence=_cadence(_first(form, "billing_cadence")),
                    cadence_months=int(_required(form, "cadence_months")),
                    billing_month=_optional_int(_first(form, "billing_month")),
                    billing_day=_optional_int(_first(form, "billing_day")),
                    active_from=_optional_month(_first(form, "active_from")),
                    active_to=_optional_month(_first(form, "active_to")),
                )
            )
        except (ValueError, TypeError, ScribeError) as error:
            return HTMLResponse(f"無法新增訂閱: {error}", status_code=422)
        return _redirect(request, _subscriptions_target(form, "created"))

    # Literal paths are declared before the parametrized one, so
    # `/subscriptions/default-wallet` is never read as a subscription ID.
    @application.post("/subscriptions/default-wallet")
    async def set_default_subscription_wallet(request: Request) -> Response:
        try:
            form = await _validated_form(request, settings)
            dependencies.manage_subscriptions.set_default_funding_wallet(
                int(_required(form, "funding_wallet_id"))
            )
        except (ValueError, ScribeError) as error:
            return HTMLResponse(f"無法設定預設錢包: {error}", status_code=422)
        return _redirect(request, _subscriptions_target(form, "wallet_saved"))

    @application.post("/subscriptions/prices/{price_period_id}")
    async def update_subscription_price(
        request: Request, price_period_id: int
    ) -> Response:
        try:
            form = await _validated_form(request, settings)
            dependencies.manage_subscriptions.update_price(
                UpdatePricePeriodRequest(
                    price_period_id=price_period_id,
                    expected_version=int(_required(form, "version")),
                    amount_minor=parse_display_amount(
                        _required(form, "amount"),
                        _required(form, "currency"),
                        allow_zero=True,
                    ),
                    effective_from=_required_month(form, "effective_from"),
                    effective_to=_optional_month(_first(form, "effective_to")),
                )
            )
        except ConcurrentUpdateError:
            return HTMLResponse("價格區間已被更新: 請重新載入頁面", status_code=409)
        except (ValueError, TypeError, ScribeError) as error:
            return HTMLResponse(f"無法更新價格區間: {error}", status_code=422)
        return _redirect(request, _subscriptions_target(form, "priced"))

    @application.post("/subscriptions/{subscription_id}")
    async def update_subscription(request: Request, subscription_id: int) -> Response:
        try:
            form = await _validated_form(request, settings)
            dependencies.manage_subscriptions.update(
                UpdateSubscriptionRequest(
                    subscription_id=subscription_id,
                    expected_version=int(_required(form, "version")),
                    name=_required(form, "name"),
                    budget_category_id=int(_required(form, "budget_category_id")),
                    funding_wallet_id=int(_required(form, "funding_wallet_id")),
                    currency=_required(form, "currency"),
                    billing_cadence=_cadence(_first(form, "billing_cadence")),
                    cadence_months=int(_required(form, "cadence_months")),
                    billing_month=_optional_int(_first(form, "billing_month")),
                    billing_day=_optional_int(_first(form, "billing_day")),
                    active_from=_optional_month(_first(form, "active_from")),
                    active_to=_optional_month(_first(form, "active_to")),
                )
            )
        except ConcurrentUpdateError:
            return HTMLResponse("訂閱已被更新: 請重新載入頁面", status_code=409)
        except (ValueError, TypeError, ScribeError) as error:
            return HTMLResponse(f"無法更新訂閱: {error}", status_code=422)
        return _redirect(request, _subscriptions_target(form, "saved"))

    @application.post("/subscriptions/{subscription_id}/deactivate")
    async def deactivate_subscription(
        request: Request, subscription_id: int
    ) -> Response:
        try:
            form = await _validated_form(request, settings)
            dependencies.manage_subscriptions.deactivate(
                DeactivateSubscriptionRequest(
                    subscription_id=subscription_id,
                    expected_version=int(_required(form, "version")),
                    active_to=_required_month(form, "active_to"),
                )
            )
        except ConcurrentUpdateError:
            return HTMLResponse("訂閱已被更新: 請重新載入頁面", status_code=409)
        except (ValueError, TypeError, ScribeError) as error:
            return HTMLResponse(f"無法停用訂閱: {error}", status_code=422)
        return _redirect(request, _subscriptions_target(form, "saved"))

    @application.post("/subscriptions/{subscription_id}/resume")
    async def resume_subscription(request: Request, subscription_id: int) -> Response:
        try:
            form = await _validated_form(request, settings)
            dependencies.manage_subscriptions.resume(
                ResumeSubscriptionRequest(
                    subscription_id=subscription_id,
                    active_from=_required_month(form, "active_from"),
                    active_to=_optional_month(_first(form, "active_to")),
                )
            )
        except (ValueError, TypeError, ScribeError) as error:
            return HTMLResponse(f"無法重新啟用訂閱: {error}", status_code=422)
        return _redirect(request, _subscriptions_target(form, "resumed"))

    @application.post("/subscriptions/{subscription_id}/prices")
    async def add_subscription_price(
        request: Request, subscription_id: int
    ) -> Response:
        try:
            form = await _validated_form(request, settings)
            dependencies.manage_subscriptions.add_price(
                AddPricePeriodRequest(
                    subscription_id=subscription_id,
                    amount_minor=parse_display_amount(
                        _required(form, "amount"),
                        _required(form, "currency"),
                        allow_zero=True,
                    ),
                    effective_from=_required_month(form, "effective_from"),
                    effective_to=_optional_month(_first(form, "effective_to")),
                )
            )
        except (ValueError, TypeError, ScribeError) as error:
            return HTMLResponse(f"無法新增價格區間: {error}", status_code=422)
        return _redirect(request, _subscriptions_target(form, "priced"))

    def salary_context(request: Request, **values: object) -> dict[str, object]:
        """Share the salary display vocabulary with both allocation pages."""
        return context(
            request,
            run_status_labels=_RUN_STATUS_LABELS,
            item_status_labels=_RUN_ITEM_STATUS_LABELS,
            fulfillment_labels=_FULFILLMENT_LABELS,
            calculation_labels=_CALCULATION_MODE_LABELS,
            reason_labels=_REASON_LABELS,
            calculation_modes=tuple(SalaryCalculationMode),
            **values,
        )

    # Literal segments are declared before `/allocations/{label}` so
    # `templates`, `runs`, and `items` are never read as a month.
    @application.get("/allocations/templates", response_class=HTMLResponse)
    def salary_templates(request: Request) -> HTMLResponse:
        templates = dependencies.salary.templates()
        currency = _first_currency(templates) or settings.default_currency
        return templates_response(
            request,
            "salary_templates.html",
            salary_context(
                request,
                rows=templates,
                accounts=dependencies.salary.account_options(),
                wallets=dependencies.salary.wallet_options(currency),
                wallets_by_currency=_wallets_by_currency(dependencies, templates),
                categories=dependencies.salary.category_options(),
                default_currency=settings.default_currency,
            ),
        )

    @application.post("/allocations/templates")
    async def create_salary_template(request: Request) -> Response:
        try:
            form = await _validated_form(request, settings)
            dependencies.manage_salary_templates.add_template(
                AddSalaryTemplateRequest(
                    name=_required(form, "name"),
                    currency=_required(form, "currency"),
                    source_account_id=int(_required(form, "source_account_id")),
                )
            )
        except (ValueError, TypeError, ScribeError) as error:
            return HTMLResponse(f"無法新增薪資範本: {error}", status_code=422)
        return _redirect(request, "/allocations/templates?created=1")

    @application.post("/allocations/templates/{template_id}/amounts")
    async def set_salary_amounts(request: Request, template_id: int) -> Response:
        """Apply the amounts one submission of the plan sheet changed."""
        try:
            form = await _validated_form(request, settings)
            currency = _required(form, "currency")
            edits = tuple(
                SalaryAmountEdit(
                    template_item_id=item_id,
                    expected_version=int(_required(form, f"version_{item_id}")),
                    fixed_amount_minor=parse_display_amount(
                        _required(form, f"amount_{item_id}"),
                        currency,
                        allow_zero=True,
                    ),
                )
                for item_id in sorted(
                    int(name.removeprefix("amount_"))
                    for name in form
                    if name.startswith("amount_")
                )
            )
            dependencies.manage_salary_templates.set_amounts(
                SetSalaryAmountsRequest(
                    template_id=template_id,
                    expected_template_version=int(_required(form, "template_version")),
                    edits=edits,
                )
            )
        except ConcurrentUpdateError:
            return HTMLResponse("薪資計畫已被更新: 請重新載入頁面", status_code=409)
        except (ValueError, TypeError, ScribeError) as error:
            return HTMLResponse(f"無法儲存金額: {error}", status_code=422)
        return _redirect(request, "/allocations/templates?saved=1")

    @application.post("/allocations/templates/items/{template_item_id}")
    async def update_salary_template_item(
        request: Request, template_item_id: int
    ) -> Response:
        try:
            form = await _validated_form(request, settings)
            dependencies.manage_salary_templates.update_item(
                UpdateSalaryTemplateItemRequest(
                    template_item_id=template_item_id,
                    expected_version=int(_required(form, "version")),
                    expected_template_version=_optional_int(
                        _first(form, "template_version")
                    ),
                    name=_required(form, "name"),
                    destination_wallet_id=int(_required(form, "destination_wallet_id")),
                    calculation_mode=_calculation_mode(
                        _first(form, "calculation_mode")
                    ),
                    budget_category_id=_optional_int(
                        _first(form, "budget_category_id")
                    ),
                    fixed_amount_minor=_optional_display_amount(
                        _first(form, "fixed_amount"), _first(form, "item_currency")
                    ),
                    display_order=int(_first(form, "display_order") or 0),
                    is_active=True,
                )
            )
        except ConcurrentUpdateError:
            return HTMLResponse("薪資項目已被更新: 請重新載入頁面", status_code=409)
        except (ValueError, TypeError, ScribeError) as error:
            return HTMLResponse(f"無法更新薪資項目: {error}", status_code=422)
        return _redirect(request, "/allocations/templates?saved=1")

    @application.post("/allocations/templates/items/{template_item_id}/deactivate")
    async def deactivate_salary_template_item(
        request: Request, template_item_id: int
    ) -> Response:
        try:
            form = await _validated_form(request, settings)
            dependencies.manage_salary_templates.deactivate_item(
                DeactivateSalaryTemplateItemRequest(
                    template_item_id=template_item_id,
                    expected_version=int(_required(form, "version")),
                    expected_template_version=_optional_int(
                        _first(form, "template_version")
                    ),
                )
            )
        except ConcurrentUpdateError:
            return HTMLResponse("薪資項目已被更新: 請重新載入頁面", status_code=409)
        except (ValueError, TypeError, ScribeError) as error:
            return HTMLResponse(f"無法停用薪資項目: {error}", status_code=422)
        return _redirect(request, "/allocations/templates?saved=1")

    @application.post("/allocations/templates/{template_id}/items")
    async def create_salary_template_item(
        request: Request, template_id: int
    ) -> Response:
        try:
            form = await _validated_form(request, settings)
            dependencies.manage_salary_templates.add_item(
                AddSalaryTemplateItemRequest(
                    template_id=template_id,
                    expected_template_version=_optional_int(
                        _first(form, "template_version")
                    ),
                    name=_required(form, "name"),
                    destination_wallet_id=int(_required(form, "destination_wallet_id")),
                    calculation_mode=_calculation_mode(
                        _first(form, "calculation_mode")
                    ),
                    budget_category_id=_optional_int(
                        _first(form, "budget_category_id")
                    ),
                    fixed_amount_minor=_optional_display_amount(
                        _first(form, "fixed_amount"), _first(form, "item_currency")
                    ),
                    display_order=int(_first(form, "display_order") or 0),
                )
            )
        except ConcurrentUpdateError:
            return HTMLResponse("薪資範本已被更新: 請重新載入頁面", status_code=409)
        except (ValueError, TypeError, ScribeError) as error:
            return HTMLResponse(f"無法新增薪資項目: {error}", status_code=422)
        return _redirect(request, "/allocations/templates?saved=1")

    @application.post("/allocations/templates/{template_id}")
    async def update_salary_template(request: Request, template_id: int) -> Response:
        try:
            form = await _validated_form(request, settings)
            dependencies.manage_salary_templates.update_template(
                UpdateSalaryTemplateRequest(
                    template_id=template_id,
                    expected_version=int(_required(form, "version")),
                    name=_required(form, "name"),
                    is_active=_checked(form, "is_active"),
                )
            )
        except ConcurrentUpdateError:
            return HTMLResponse("薪資範本已被更新: 請重新載入頁面", status_code=409)
        except (ValueError, TypeError, ScribeError) as error:
            return HTMLResponse(f"無法更新薪資範本: {error}", status_code=422)
        return _redirect(request, "/allocations/templates?saved=1")

    @application.post("/allocations/runs/{run_id}/regenerate")
    async def regenerate_salary_run(request: Request, run_id: int) -> Response:
        try:
            form = await _validated_form(request, settings)
            regenerate_currency = run_currency(run_id)
            dependencies.generate_salary_allocation.regenerate(
                RegenerateSalaryRunRequest(
                    run_id=run_id,
                    expected_version=int(_required(form, "version")),
                    manual_amounts=_manual_salary_amounts(form, regenerate_currency),
                )
            )
        except ConcurrentUpdateError:
            return HTMLResponse("這份草稿已被更新: 請重新載入頁面", status_code=409)
        except (ValueError, TypeError, ScribeError) as error:
            return HTMLResponse(f"無法重新產生分配: {error}", status_code=422)
        return _redirect(request, _allocation_target(form, "generated"))

    @application.post("/allocations/runs/{run_id}/approve")
    async def approve_salary_run(request: Request, run_id: int) -> Response:
        try:
            form = await _validated_form(request, settings)
            dependencies.approve_salary_allocation.execute(
                ApproveSalaryRunRequest(
                    run_id=run_id,
                    expected_version=int(_required(form, "version")),
                )
            )
        except ConcurrentUpdateError:
            return HTMLResponse("這份分配已被更新: 請重新載入頁面", status_code=409)
        except (ValueError, TypeError, ScribeError) as error:
            return HTMLResponse(f"無法核准分配: {error}", status_code=422)
        return _redirect(request, _allocation_target(form, "approved"))

    @application.post("/allocations/runs/{run_id}/cancel")
    async def cancel_salary_run(request: Request, run_id: int) -> Response:
        try:
            form = await _validated_form(request, settings)
            dependencies.generate_salary_allocation.cancel(
                CancelSalaryDraftRequest(
                    run_id=run_id,
                    expected_version=int(_required(form, "version")),
                )
            )
        except ConcurrentUpdateError:
            return HTMLResponse("這份草稿已被更新: 請重新載入頁面", status_code=409)
        except (ValueError, TypeError, ScribeError) as error:
            return HTMLResponse(f"無法取消草稿: {error}", status_code=422)
        return _redirect(request, _allocation_target(form, "cancelled"))

    @application.post("/allocations/items/{run_item_id}/mark-transferred")
    async def mark_salary_transferred(request: Request, run_item_id: int) -> Response:
        try:
            form = await _validated_form(request, settings)
            effective_at = _local_form_datetime(
                _required(form, "effective_at"), settings.local_timezone
            )
            if effective_at is None:  # pragma: no cover - guarded by _required
                raise ValueError("effective_at is required")
            dependencies.mark_salary_transferred.execute(
                MarkSalaryTransferredRequest(
                    run_item_id=run_item_id,
                    expected_version=int(_required(form, "version")),
                    effective_at=effective_at,
                )
            )
        except ConcurrentUpdateError:
            return HTMLResponse("這一列已被更新: 請重新載入頁面", status_code=409)
        except (ValueError, TypeError, ScribeError) as error:
            return HTMLResponse(f"無法記錄轉帳: {error}", status_code=422)
        return _redirect(request, _allocation_target(form, "transferred"))

    @application.get("/allocations", response_class=HTMLResponse)
    def salary_allocations_current(request: Request) -> Response:
        today = datetime.now(ZoneInfo(settings.local_timezone)).date()
        return _redirect(request, f"/allocations/{today:%Y-%m}")

    @application.get("/allocations/{label}", response_class=HTMLResponse)
    def salary_allocations(request: Request, label: str) -> Response:
        try:
            year, month = parse_period_label(label)
        except BudgetPeriodError as error:
            return HTMLResponse(f"無法顯示分配月份: {error}", status_code=404)
        template_id = _optional_int(request.query_params.get("template_id"))
        currency = (
            _optional_currency(request.query_params.get("currency"))
            or settings.default_currency
        )
        view = dependencies.salary.month(
            year=year, month=month, template_id=template_id, currency=currency
        )
        return templates_response(
            request,
            "salary_allocations.html",
            salary_context(
                request,
                view=view,
                client_token=secrets.token_urlsafe(16),
            ),
        )

    @application.post("/allocations/{label}/salary-receipt")
    async def create_salary_receipt(request: Request, label: str) -> Response:
        try:
            parse_period_label(label)
            form = await _validated_form(request, settings)
            occurred_at = _local_form_datetime(
                _required(form, "occurred_at"), settings.local_timezone
            )
            if occurred_at is None:  # pragma: no cover - guarded by _required
                raise ValueError("occurred_at is required")
            dependencies.record_salary_receipt.execute(
                SalaryReceiptRequest(
                    template_id=int(_required(form, "template_id")),
                    amount_minor=parse_display_amount(
                        _required(form, "amount"), _required(form, "currency")
                    ),
                    occurred_at=occurred_at,
                    client_token=_required(form, "client_token"),
                    note=_clean_optional(_first(form, "note")),
                )
            )
        except (ValueError, TypeError, ScribeError) as error:
            return HTMLResponse(f"無法記錄薪資入帳: {error}", status_code=422)
        return _redirect(request, _allocation_target(form, "received", label))

    @application.post("/allocations/{label}/generate")
    async def generate_salary_run(request: Request, label: str) -> Response:
        try:
            year, month = parse_period_label(label)
            form = await _validated_form(request, settings)
            salary_template_id = int(_required(form, "template_id"))
            dependencies.generate_salary_allocation.execute(
                GenerateSalaryRunRequest(
                    template_id=salary_template_id,
                    year=year,
                    month=month,
                    salary_event_id=int(_required(form, "salary_event_id")),
                    manual_amounts=_manual_salary_amounts(
                        form, template_currency(salary_template_id)
                    ),
                )
            )
        except (ValueError, TypeError, ScribeError) as error:
            return HTMLResponse(f"無法產生分配草稿: {error}", status_code=422)
        return _redirect(request, _allocation_target(form, "generated", label))

    @application.get("/clearing", response_class=HTMLResponse)
    def clearing(request: Request, page: int = 1, warn_page: int = 1) -> HTMLResponse:
        offset = max(0, (max(1, page) - 1) * _CLEARING_PAGE_SIZE)
        # The warning list is its own list with its own length, so paging the
        # history must not decide whether an over-covered commitment is
        # reachable (ADR-B060).
        warn_offset = max(0, (max(1, warn_page) - 1) * _CLEARING_PAGE_SIZE)
        return templates_response(
            request,
            "clearing.html",
            context(
                request,
                view=dependencies.clearing.view(
                    now=datetime.now(UTC),
                    limit=_CLEARING_PAGE_SIZE,
                    offset=offset,
                    warn_offset=warn_offset,
                ),
                coordinator=(
                    None
                    if dependencies.maintenance is None
                    else dependencies.maintenance.snapshot()
                ),
                default_currency=settings.default_currency,
                page=max(1, page),
                warn_page=max(1, warn_page),
                default_time=settings.scheduled_transfer_default_time,
                schedule_timezone=settings.scheduled_transfer_timezone,
                default_date=_next_local_day(settings.scheduled_transfer_timezone),
                client_token=secrets.token_urlsafe(16),
                status_labels=_SCHEDULE_STATUS_LABELS,
                purpose_labels=_SCHEDULE_PURPOSE_LABELS,
                warning_action_labels=_WARNING_ACTION_LABELS,
                warning_summary_labels=_WARNING_SUMMARY_LABELS,
                fulfillment_labels=_FULFILLMENT_LABELS,
            ),
        )

    def wallet_currency(wallet_id: int) -> str:
        """Return the currency the account holding this wallet is kept in.

        Never read from the form: a page that lists wallets in several
        currencies cannot state one of them in a hidden field, and trusting
        it would parse a JPY amount with TWD precision (ADR-B099).
        """
        for row in dependencies.spending.accounts():
            for wallet in row.wallets:
                if wallet.wallet_id == wallet_id:
                    return wallet.currency
        raise ValueError("funding wallet was not found")

    def run_currency(run_id: int) -> str:
        """Return the currency one frozen salary generation was written in."""
        view = dependencies.salary.run(run_id)
        if view is None:
            raise ValueError("salary run was not found")
        return view.run.currency

    def template_currency(template_id: int) -> str:
        """Return the currency a salary plan is fixed in (ADR-B099)."""
        for row in dependencies.salary.templates():
            if row.template.id == template_id:
                return row.template.currency
        raise ValueError("salary template was not found")

    @application.post("/clearing/schedules")
    async def create_clearing_schedule(request: Request) -> Response:
        try:
            form = await _validated_form(request, settings)
            source_wallet_id = int(_required(form, "source_wallet_id"))
            currency = wallet_currency(source_wallet_id)
            dependencies.schedule_transfers.schedule_clearing(
                ScheduleClearingTransferRequest(
                    source_account_id=int(_required(form, "source_account_id")),
                    source_wallet_id=source_wallet_id,
                    destination_account_id=int(
                        _required(form, "destination_account_id")
                    ),
                    destination_wallet_id=int(_required(form, "destination_wallet_id")),
                    amount_minor=parse_display_amount(
                        _required(form, "amount"), currency
                    ),
                    scheduled_local_date=_required_date(form, "scheduled_local_date"),
                    scheduled_local_time=_optional_wall_clock(
                        _first(form, "scheduled_local_time")
                    ),
                    client_token=_required(form, "client_token"),
                    lines=_commitment_lines(form, currency),
                    allow_overdue=_checked(form, "confirm_overdue"),
                )
            )
        except ScheduledTransferOverdueError as error:
            return HTMLResponse(f"{error}", status_code=422)
        except (ValueError, TypeError, ScribeError) as error:
            return HTMLResponse(f"無法建立排程: {error}", status_code=422)
        return _redirect(request, "/clearing?scheduled=1")

    @application.post("/clearing/autopay")
    async def create_card_autopay(request: Request) -> Response:
        try:
            form = await _validated_form(request, settings)
            autopay_wallet_id = int(_required(form, "source_wallet_id"))
            autopay_currency = wallet_currency(autopay_wallet_id)
            dependencies.schedule_transfers.schedule_card_autopay(
                ScheduleCardAutopayRequest(
                    card_id=int(_required(form, "card_id")),
                    source_wallet_id=autopay_wallet_id,
                    amount_minor=parse_display_amount(
                        _required(form, "amount"), autopay_currency
                    ),
                    scheduled_local_date=_required_date(form, "scheduled_local_date"),
                    scheduled_local_time=_optional_wall_clock(
                        _first(form, "scheduled_local_time")
                    ),
                    client_token=_required(form, "client_token"),
                    lines=_commitment_lines(form, autopay_currency),
                    allow_overdue=_checked(form, "confirm_overdue"),
                )
            )
        except ScheduledTransferOverdueError as error:
            return HTMLResponse(f"{error}", status_code=422)
        except (ValueError, TypeError, ScribeError) as error:
            return HTMLResponse(f"無法建立自動扣繳排程: {error}", status_code=422)
        return _redirect(request, "/clearing?autopay=1")

    @application.post("/clearing/schedules/{scheduled_transfer_id}/cancel")
    async def cancel_clearing_schedule(
        request: Request, scheduled_transfer_id: int
    ) -> Response:
        try:
            form = await _validated_form(request, settings)
            dependencies.schedule_transfers.cancel(
                ScheduledTransferAction(
                    scheduled_transfer_id=scheduled_transfer_id,
                    expected_version=int(_required(form, "version")),
                )
            )
        except ConcurrentUpdateError:
            return HTMLResponse("這筆排程已被更新: 請重新載入頁面", status_code=409)
        except (ValueError, TypeError, ScribeError) as error:
            return HTMLResponse(f"無法取消排程: {error}", status_code=422)
        return _redirect(request, "/clearing?cancelled=1")

    @application.post("/clearing/schedules/{scheduled_transfer_id}/retry")
    async def retry_clearing_schedule(
        request: Request, scheduled_transfer_id: int
    ) -> Response:
        try:
            form = await _validated_form(request, settings)
            dependencies.schedule_transfers.retry(
                ScheduledTransferAction(
                    scheduled_transfer_id=scheduled_transfer_id,
                    expected_version=int(_required(form, "version")),
                )
            )
        except ConcurrentUpdateError:
            return HTMLResponse("這筆排程已被更新: 請重新載入頁面", status_code=409)
        except (ValueError, TypeError, ScribeError) as error:
            return HTMLResponse(f"無法重試排程: {error}", status_code=422)
        return _redirect(request, "/clearing?retried=1")

    @application.post("/clearing/schedules/{scheduled_transfer_id}/reverse")
    async def reverse_clearing_schedule(
        request: Request, scheduled_transfer_id: int
    ) -> Response:
        try:
            form = await _validated_form(request, settings)
            dependencies.schedule_transfers.reverse(
                ScheduledTransferAction(
                    scheduled_transfer_id=scheduled_transfer_id,
                    expected_version=int(_required(form, "version")),
                )
            )
        except ConcurrentUpdateError:
            return HTMLResponse("這筆排程已被更新: 請重新載入頁面", status_code=409)
        except (ValueError, TypeError, ScribeError) as error:
            return HTMLResponse(f"無法沖銷排程: {error}", status_code=422)
        return _redirect(request, "/clearing?reversed=1")

    @application.post("/clearing/run-due")
    async def run_due_schedules(request: Request) -> Response:
        try:
            await _validated_form(request, settings)
            dependencies.schedule_transfers.run_due()
        except (ValueError, ScribeError) as error:
            return HTMLResponse(f"無法執行到期排程: {error}", status_code=422)
        return _redirect(request, "/clearing?ran=1")

    @application.get("/emails", response_class=HTMLResponse)
    def emails(
        request: Request,
        page: int = 1,
        query: str | None = None,
        classification: str | None = None,
        processing: str | None = None,
    ) -> HTMLResponse:
        criteria = EmailSearchCriteria(
            page=max(1, page),
            query=_clean_optional(query),
            classification=_optional_enum(ClassificationStatus, classification),
            processing=_optional_enum(ProcessingStatus, processing),
        )
        result = dependencies.queries.search_emails(criteria)
        template = (
            "partials/email_results.html"
            if request.headers.get("HX-Request") == "true"
            else "emails.html"
        )
        return templates.TemplateResponse(
            request=request,
            name=template,
            context=context(request, result=result, criteria=criteria),
        )

    @application.get("/emails/{email_id}", response_class=HTMLResponse)
    def email_detail(request: Request, email_id: int) -> HTMLResponse:
        detail = dependencies.queries.email_detail(email_id)
        if detail is None:
            return _not_found("找不到這封 Email")
        return templates.TemplateResponse(
            request=request,
            name="email_detail.html",
            context=context(request, detail=detail),
        )

    @application.post("/emails/{email_id}/reparse")
    async def reparse_email(request: Request, email_id: int) -> Response:
        try:
            await _validated_form(request, settings)
            dependencies.reparse_email.execute(email_id)
        except (ValueError, ScribeError) as error:
            return HTMLResponse(f"無法重新解析: {error}", status_code=422)
        return _redirect(request, f"/emails/{email_id}?reparsed=1")

    @application.get("/reports", response_class=HTMLResponse)
    def reports(request: Request, days: int = 90) -> HTMLResponse:
        snapshot = dependencies.statistics.report(
            now=datetime.now(ZoneInfo(settings.local_timezone)),
            daily_days=days,
        )
        return templates.TemplateResponse(
            request=request,
            name="reports.html",
            context=context(
                request,
                report=snapshot,
                chart_data=_statistics_chart_data(snapshot),
            ),
        )

    @application.get("/sync", response_class=HTMLResponse)
    def sync_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="sync.html",
            context=context(
                request,
                sync=dependencies.sync_queries.snapshot(),
                worker=dependencies.sync_control.worker_snapshot(),
            ),
        )

    @application.get("/sync/progress", response_class=HTMLResponse)
    def sync_progress(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="partials/sync_progress.html",
            context=context(
                request,
                sync=dependencies.sync_queries.snapshot(),
                worker=dependencies.sync_control.worker_snapshot(),
            ),
        )

    @application.post("/sync/start/incremental")
    async def start_incremental_sync(request: Request) -> Response:
        try:
            await _validated_form(request, settings)
            dependencies.sync_control.start_incremental()
        except (ValueError, ScribeError) as error:
            return HTMLResponse(f"無法啟動同步: {error}", status_code=409)
        return _redirect(request, "/sync?started=incremental")

    @application.post("/sync/start/full")
    async def start_full_sync(request: Request) -> Response:
        try:
            form = await _validated_form(request, settings)
            dependencies.sync_control.start_full(
                FullSyncRequest(
                    custom_query=_clean_optional(_first(form, "query")),
                    all_senders=_checked(form, "all_senders"),
                )
            )
        except (ValueError, ScribeError) as error:
            return HTMLResponse(f"無法啟動同步: {error}", status_code=409)
        return _redirect(request, "/sync?started=full")

    @application.post("/sync/{run_id}/cancel")
    async def cancel_sync(request: Request, run_id: str) -> Response:
        try:
            await _validated_form(request, settings)
            dependencies.sync_control.request_cancel(run_id)
        except (ValueError, ScribeError) as error:
            return HTMLResponse(f"無法取消同步: {error}", status_code=409)
        return _redirect(request, "/sync?cancel_requested=1")

    return application


async def _validated_form(
    request: Request, settings: AppSettings
) -> dict[str, list[str]]:
    """Decode bounded urlencoded form data and enforce same-origin CSRF."""
    content_type = request.headers.get("content-type", "")
    if not content_type.startswith("application/x-www-form-urlencoded"):
        raise ValueError("unsupported form encoding")
    body = await request.body()
    if len(body) > 32_768:
        raise ValueError("form is too large")
    form = parse_qs(body.decode("utf-8"), keep_blank_values=True)
    cookie = request.cookies.get("scribe_csrf")
    submitted = _first(form, "csrf_token")
    if not cookie or not submitted or not hmac.compare_digest(cookie, submitted):
        raise ValueError("CSRF validation failed")
    origin = request.headers.get("origin")
    allowed_hosts = local_full_hosts(settings)
    request_is_local = request.url.hostname is not None and (
        request.url.hostname in allowed_hosts
        or _is_loopback_address(request.url.hostname)
    )
    # Safari sends the literal string "null" instead of omitting the header
    # for some same-origin top-level form POSTs, especially over plain HTTP
    # on a LAN — and this app's own `no-referrer` policy for LOCAL_FULL means
    # Referer is never available as a fallback signal either. Treating "null"
    # as "no Origin" is safe there: the SameSite=Strict cookie check above
    # already rejects a cross-site attacker, including the sandboxed-iframe
    # technique that produces this same literal value on a genuinely opaque
    # origin. The externally reachable surface stays strict regardless,
    # since the whole internet can reach it rather than only a host
    # explicitly whitelisted at config time.
    if origin == "null" and request_is_local:
        origin = None
    if origin:
        parsed = urlparse(origin)
        local_origin = (
            parsed.scheme in {"http", "https"}
            and parsed.hostname is not None
            and (
                parsed.hostname in allowed_hosts
                or _is_loopback_address(parsed.hostname)
            )
        )
        browser_same_origin = (
            request.headers.get("sec-fetch-site", "").lower() == "same-origin"
            and request_is_local
        )
        external_origin = (
            settings.external_confirmation_enabled
            and settings.external_hostname is not None
            and parsed.scheme == "https"
            and parsed.hostname == settings.external_hostname
            and request.url.hostname == settings.external_hostname
        )
        if not local_origin and not browser_same_origin and not external_origin:
            raise ValueError("request origin is not allowed")
    return form


def _is_loopback_address(hostname: str) -> bool:
    """Recognize any numeric loopback address without trusting public hosts."""
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _redirect(request: Request, location: str) -> Response:
    if request.headers.get("HX-Request") == "true":
        return Response(status_code=204, headers={"HX-Redirect": location})
    return RedirectResponse(location, status_code=303)


def _request_target(request: Request) -> str:
    """Return the current local path and query without trusting the Host header."""
    query = request.url.query
    return f"{request.url.path}?{query}" if query else request.url.path


def _safe_return_target(value: str | None) -> str:
    """Allow redirects only to known read pages on this application."""
    fallback = "/transactions"
    if not value or any(character in value for character in "\r\n"):
        return fallback
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc or parsed.path.startswith("//"):
        return fallback
    allowed = (
        parsed.path == "/"
        or parsed.path == "/transactions"
        or parsed.path == "/reports"
        or parsed.path == "/money"
        or parsed.path == "/more"
        or parsed.path == "/money/review"
        or parsed.path == "/money/daily"
        or parsed.path == "/money/accounts"
        or parsed.path == "/money/source-links"
        or parsed.path == "/money/reconcile"
        or parsed.path.startswith("/money/accounts/")
        or parsed.path.startswith("/money/events/")
        or parsed.path == "/clearing"
        or parsed.path == "/subscriptions"
        or parsed.path.startswith("/budget/")
        or parsed.path.startswith("/emails/")
    )
    return value if allowed else fallback


#: Fields the expense form may echo back after a rejected submission. The
#: CSRF token is deliberately absent: it comes from the cookie, never from the
#: previous body.
_EXPENSE_FIELDS = (
    "payment_method",
    "merchant",
    "amount",
    "currency",
    "occurred_at",
    "funding_wallet_id",
    "budget_category_id",
    "card_id",
    "note",
    "client_token",
    "spending_treatment",
    "clearing_wallet_id",
)


def _expense_values(form: dict[str, list[str]]) -> dict[str, str]:
    """Return the submitted expense fields, bounded and ready to re-render."""
    values: dict[str, str] = {}
    for name in _EXPENSE_FIELDS:
        submitted = _first(form, name)
        if submitted is not None:
            values[name] = submitted[:200]
    return values


def _manual_payment_method(value: str | None) -> PaymentMethod:
    """Decode an explicit cash or credit-card choice; nothing here is guessed."""
    if value == PaymentMethod.CASH.value:
        return PaymentMethod.CASH
    if value == PaymentMethod.CREDIT_CARD.value:
        return PaymentMethod.CREDIT_CARD
    raise ValueError("payment_method must be cash or credit_card")


def _dedicated_clearing_wallet_id(
    form: dict[str, list[str]],
) -> int | None:
    """Decode the explicit spending treatment without guessing from names."""
    treatment = _first(form, "spending_treatment") or "monthly_budget"
    if treatment == "monthly_budget":
        if _clean_optional(_first(form, "clearing_wallet_id")) is not None:
            raise ValueError("monthly-budget spending cannot name a clearing wallet")
        return None
    if treatment == "dedicated_wallet":
        return int(_required(form, "clearing_wallet_id"))
    raise ValueError("spending_treatment is not recognized")


def _account_currency(dependencies: WebDependencies, account_id: int) -> str:
    """Return one account's currency from the ledger, never from the form.

    A submitted currency code would rescale the typed amount, so an account's
    own currency is the only one an observation may be recorded in.
    """
    for row in dependencies.spending.accounts():
        if row.account.id == account_id:
            return row.account.currency
    raise ValueError("financial account was not found")


def _confirmation_allocation_target(result: ConfirmationResult) -> str:
    """Continue a local confirmation into the one remaining money decision.

    Direct Gmail intake creates an event that needs allocation. A source that
    may already describe a hand-entered event must visit source review instead,
    while an exact match already points at posted money and needs no allocation.
    """
    intake = result.intake
    if intake is None:
        return "/money/review"
    if intake.pending:
        return "/money/source-links"
    event_ids = tuple(
        item.event_id for item in intake.staged if item.event_id is not None
    )
    if len(event_ids) == 1:
        return f"/money/review?event_id={event_ids[0]}"
    if intake.matched and len(intake.matched) == 1:
        return f"/money/events/{intake.matched[0].event_id}"
    return "/money/review"


def _close_preview(
    dependencies: WebDependencies, year: int, month: int, currency: str
) -> BudgetClosePreview | None:
    """Show what closing would move, or nothing before a month is planned."""
    try:
        return dependencies.close_budget_period.preview(
            year=year, month=month, currency=currency
        )
    except BudgetPeriodError:
        return None


def _allocation_lines(
    form: dict[str, list[str]], currency: str
) -> tuple[AllocationLine, ...]:
    """Read one or more split rows submitted by the allocation form.

    Amounts arrive the way they are written down rather than in minor units,
    so a TWD row reads 240 and not 24000 (ADR-B093).
    """
    amounts = form.get("amount", [])
    wallets = form.get("funding_wallet_id", [])
    categories = form.get("budget_category_id", [])
    if not amounts or len(amounts) != len(wallets) or len(amounts) != len(categories):
        raise ValueError("每一列分配都需要金額、資金來源與分類")
    clearing_wallet_id = _dedicated_clearing_wallet_id(form)
    return tuple(
        AllocationLine(
            amount_minor=parse_display_amount(amount, currency),
            funding_wallet_id=int(wallet),
            budget_category_id=_optional_int(category),
            clearing_wallet_id=clearing_wallet_id,
        )
        for amount, wallet, category in zip(amounts, wallets, categories, strict=True)
        if amount.strip()
    )


def _commitment_lines(
    form: dict[str, list[str]], currency: str
) -> tuple[ScheduleCommitmentLine, ...]:
    """Read the commitment shares one movement was asked to settle.

    Rows are paired positionally and an unchecked row is skipped, so a
    mismatched submission is refused rather than silently attaching an amount
    to the wrong commitment.
    """
    identifiers = form.get("commitment_id", [])
    amounts = form.get("commitment_amount", [])
    if len(identifiers) != len(amounts):
        raise ValueError("每一筆要涵蓋的預留都需要一個金額")
    selected = frozenset(form.get("cover_commitment_id", ()))
    return tuple(
        ScheduleCommitmentLine(
            funding_commitment_id=int(identifier),
            amount_minor=parse_display_amount(amount, currency, allow_zero=True),
        )
        for identifier, amount in zip(identifiers, amounts, strict=True)
        if identifier in selected and amount.strip()
    )


def _optional_display_amount(value: str | None, currency: str | None) -> int | None:
    """Return a typed amount in minor units, or None when the field was blank.

    A blank is not zero here: a calculated salary destination carries no
    amount at all, and storing 0 would claim it plans to fund nothing.
    """
    text = (value or "").strip()
    if not text:
        return None
    return parse_display_amount(text, currency, allow_zero=True)


def _submitted_positions(form: dict[str, list[str]]) -> dict[int, int]:
    """Return the `position_{id}` fields one arrangement form submitted.

    Both sheet axes — the account columns and one column's wallets — post the
    same field shape, so they translate it the same way. Which ids are allowed
    is not decided here: the application service checks the submission against
    the axis it actually belongs to.
    """
    return {
        int(name.removeprefix("position_")): int(_required(form, name))
        for name in form
        if name.startswith("position_")
    }


def _submitted_wallet_names(form: dict[str, list[str]]) -> dict[int, str]:
    """Return the `name_{id}` fields one wallet-rename form submitted."""
    return {
        int(name.removeprefix("name_")): _required(form, name)
        for name in form
        if name.startswith("name_")
    }


def _submitted_wallet_versions(form: dict[str, list[str]]) -> dict[int, int]:
    """Return the `version_{id}` fields the same form was rendered with."""
    return {
        int(name.removeprefix("version_")): int(_required(form, name))
        for name in form
        if name.startswith("version_")
    }


def _required_date(form: dict[str, list[str]], name: str) -> date:
    """Return one mandatory calendar date from a submitted form."""
    value = date.fromisoformat(_required(form, name))
    return value


def _optional_wall_clock(value: str | None) -> time | None:
    """Return a submitted ``HH:MM`` local policy time, or None for the default."""
    cleaned = _clean_optional(value)
    if cleaned is None:
        return None
    parsed = time.fromisoformat(cleaned)
    return time(parsed.hour, parsed.minute)


def _next_local_day(timezone: str) -> date:
    """Return tomorrow's local date, so a new schedule is not born overdue."""
    return datetime.now(ZoneInfo(timezone)).date() + timedelta(days=1)


def _calculation_mode(value: str | None) -> SalaryCalculationMode:
    """Decode an explicit calculation mode; nothing here is guessed."""
    mode = _optional_enum(SalaryCalculationMode, value)
    if mode is None:
        raise ValueError("calculation_mode is required")
    return mode


def _manual_salary_amounts(
    form: dict[str, list[str]], currency: str
) -> tuple[ManualSalaryAmount, ...]:
    """Read one typed amount per manual line submitted by the form.

    Rows are paired positionally, so a mismatched submission is refused rather
    than silently dropping the amount belonging to a destination.
    """
    identifiers = form.get("manual_item_id", [])
    amounts = form.get("manual_amount", [])
    if len(identifiers) != len(amounts):
        raise ValueError("每一個手動項目都需要一個金額")
    return tuple(
        ManualSalaryAmount(
            template_item_id=int(identifier),
            # A manual line may plan nothing this month, and the form ships
            # that zero as its default (ADR-B093).
            amount_minor=parse_display_amount(amount, currency, allow_zero=True),
        )
        for identifier, amount in zip(identifiers, amounts, strict=True)
    )


def _allocation_target(
    form: dict[str, list[str]], outcome: str, label: str | None = None
) -> str:
    """Return to the same month and template the form was submitted from."""
    period = _clean_optional(label or _first(form, "period"))
    template_id = _optional_int(_first(form, "template_id"))
    if period is None:
        return f"/allocations?{outcome}=1"
    try:
        year, month = parse_period_label(period)
    except BudgetPeriodError:
        return f"/allocations?{outcome}=1"
    query = [f"{outcome}=1"]
    if template_id is not None:
        query.append(f"template_id={template_id}")
    return f"/allocations/{year:04d}-{month:02d}?" + "&".join(query)


def _first_currency(rows: tuple[SalaryTemplateRow, ...]) -> str | None:
    """Return the currency of the first active plan, so a form can prefill."""
    return next((row.template.currency for row in rows if row.template.is_active), None)


def _wallets_by_currency(
    dependencies: WebDependencies, rows: tuple[SalaryTemplateRow, ...]
) -> dict[str, tuple[SalaryWalletOption, ...]]:
    """Return the destination choices each existing plan may pick from."""
    return {
        currency: dependencies.salary.wallet_options(currency)
        for currency in sorted({row.template.currency for row in rows})
    }


def _cadence(value: str | None) -> SubscriptionCadence:
    """Decode an explicit billing cadence; nothing here is guessed."""
    cadence = _optional_enum(SubscriptionCadence, value)
    if cadence is None:
        raise ValueError("billing_cadence is required")
    return cadence


def _optional_month(value: str | None) -> date | None:
    """Return the first day of a submitted ``yyyy-mm`` month, or None.

    Every subscription boundary is a whole month, so the form uses a month
    control and this never has to interpret a day.
    """
    cleaned = _clean_optional(value)
    if cleaned is None:
        return None
    year, month = parse_period_label(cleaned)
    return date(year, month, 1)


def _required_month(form: dict[str, list[str]], name: str) -> date:
    """Return one mandatory ``yyyy-mm`` month from a submitted form."""
    month = _optional_month(_first(form, name))
    if month is None:
        raise ValueError(f"{name} is required")
    return month


def _subscriptions_target(form: dict[str, list[str]], outcome: str) -> str:
    """Return to the same month and currency the form was submitted from."""
    period = _clean_optional(_first(form, "period"))
    currency = _optional_currency(_first(form, "currency_filter"))
    query = [f"{outcome}=1"]
    if period is not None:
        try:
            year, month = parse_period_label(period)
        except BudgetPeriodError:
            pass
        else:
            query.append(f"period={year:04d}-{month:02d}")
    if currency is not None:
        query.append(f"currency={currency}")
    return "/subscriptions?" + "&".join(query)


def _not_found(message: str) -> HTMLResponse:
    return HTMLResponse(f"<h1>{message}</h1>", status_code=404)


def _external_not_found() -> HTMLResponse:
    """Hide local-only route details from the external read-only surface."""
    return HTMLResponse("<h1>Not found</h1>", status_code=404)


def _external_forbidden() -> HTMLResponse:
    """Reject an unverified external mutation without leaking policy details."""
    return HTMLResponse("無法驗證 Cloudflare Access 身分", status_code=403)


def _first(form: dict[str, list[str]], name: str) -> str | None:
    values = form.get(name)
    return values[0] if values else None


def _required(form: dict[str, list[str]], name: str) -> str:
    value = _first(form, name)
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _checked(form: dict[str, list[str]], name: str) -> bool:
    return _first(form, name) in {"1", "true", "on"}


def _clean_optional(value: str | None) -> str | None:
    cleaned = value.strip() if value else ""
    return cleaned or None


def _optional_currency(value: str | None) -> str | None:
    cleaned = _clean_optional(value)
    if cleaned is None:
        return None
    normalized = cleaned.upper()
    if len(normalized) != 3 or not normalized.isalpha():
        return None
    return normalized


def _optional_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _optional_decimal(value: str | None) -> Decimal | None:
    try:
        result = Decimal(value) if value else None
    except InvalidOperation:
        return None
    if result is not None and not Decimal(0) <= result <= Decimal(1):
        return None
    return result


def _card_holder_role(value: str | None) -> CardHolderRole:
    """Decode an explicit holder role without inferring family relationships."""
    role = _optional_enum(CardHolderRole, value)
    return role if role is not None else CardHolderRole.UNKNOWN


def _optional_int(value: str | None) -> int | None:
    return int(value) if value else None


def _optional_enum[T: StrEnum](enum_type: type[T], value: str | None) -> T | None:
    try:
        return enum_type(value) if value else None
    except ValueError:
        return None


def _local_form_datetime(value: str | None, timezone: str) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone))
    return parsed.astimezone(UTC)


def _format_money(amount_minor: int | None, currency: str | None) -> str:
    if amount_minor is None:
        return "—"
    code = (currency or "").upper()
    precision = currency_precision(code)
    major = Decimal(amount_minor) / (Decimal(10) ** precision.minor_unit_exponent)
    return f"{code} {major:,.{precision.display_fraction_digits}f}".strip()


def _format_major(amount_minor: int | None, currency: str | None) -> str:
    """Render minor units as the plain major-unit amount a form accepts.

    No thousands separators and no currency code: this value goes straight
    back into `parse_display_amount`, so it has to round-trip exactly.
    """
    if amount_minor is None:
        return ""
    precision = currency_precision(currency)
    major = Decimal(amount_minor) / (Decimal(10) ** precision.minor_unit_exponent)
    return f"{major:.{precision.display_fraction_digits}f}"


def _format_month(value: date | None) -> str:
    """Render a calendar boundary as the month it names, never as a day."""
    return "—" if value is None else f"{value.year:04d}-{value.month:02d}"


def _format_datetime(value: datetime | None, timezone: str) -> str:
    if value is None:
        return "—"
    return value.astimezone(ZoneInfo(timezone)).strftime("%Y-%m-%d %H:%M")


def _wallet_category_hints(
    accounts: Sequence[FinancialAccountBalance],
    categories: Sequence[BudgetCategory],
) -> dict[int, int]:
    """Map each wallet to the category of the same name, where one exists.

    Someone who keeps an envelope per kind of spending names the two the
    same, so the category is derivable from the wallet they picked and does
    not need asking twice. It is only a suggestion: unlike the funding
    wallet, a category never moves money, so a wrong guess costs a report
    line and not a transfer the user has to make for real.
    """
    by_name = {
        category.name: category.id for category in categories if category.is_active
    }
    return {
        wallet.wallet_id: by_name[wallet.name]
        for account in accounts
        for wallet in account.wallets
        if wallet.is_active and wallet.name in by_name
    }


def _recent_window(value: int | None) -> int:
    """Return one of the offered windows, defaulting for anything else.

    The value arrives from a query string, so an unknown number is treated
    as an unset preference rather than an error: the page still renders, at
    the default width.
    """
    return value if value in _RECENT_WINDOW_CHOICES else _DEFAULT_RECENT_WINDOW


def _spending_totals(
    items: Sequence[TransactionListItem],
) -> tuple[MoneyTotal, ...]:
    """Sum consumption notifications by currency.

    Money movement and reimbursement are left out the way the dashboard
    queries leave them out, so the figure answers "what was spent" rather
    than "what moved"; an ignored notification is not spending at all.
    """
    sums: dict[str, int] = {}
    for item in items:
        if item.transaction_type not in SPENDING_TRANSACTION_TYPES:
            continue
        if item.manually_ignored:
            continue
        if item.amount_minor is None or item.currency is None:
            continue
        sums[item.currency] = sums.get(item.currency, 0) + item.amount_minor
    return tuple(
        MoneyTotal(currency=currency, amount_minor=amount)
        for currency, amount in sorted(sums.items())
    )


def _group_recent_transactions(
    items: tuple[TransactionListItem, ...],
    *,
    timezone: str,
    now: datetime,
) -> tuple[RecentTransactionDay, ...]:
    """Group recent items by local date with newest transactions first."""
    local_timezone = ZoneInfo(timezone)
    today = now.astimezone(local_timezone).date()
    grouped: dict[date | None, list[TransactionListItem]] = {}
    for item in items:
        local_date = (
            item.transaction_at.astimezone(local_timezone).date()
            if item.transaction_at is not None
            else None
        )
        grouped.setdefault(local_date, []).append(item)

    results = []
    for local_date, group_items in grouped.items():
        if local_date is None:
            key = "unknown"
            label = "日期未辨識"
        else:
            key = local_date.isoformat()
            if local_date == today:
                prefix = "今天"
            elif local_date == today - timedelta(days=1):
                prefix = "昨天"
            else:
                prefix = ""
            formatted = local_date.strftime("%Y/%m/%d")
            label = f"{prefix} · {formatted}" if prefix else formatted
        ordered_items = sorted(
            group_items,
            key=lambda item: (
                item.transaction_at or datetime.min.replace(tzinfo=UTC),
                item.id,
            ),
            reverse=True,
        )
        # A notification the user ignored is answered, so it is not what the
        # row is flagging; anything else unconfirmed still needs them.
        unconfirmed = tuple(
            item
            for item in ordered_items
            if not item.manually_confirmed and not item.manually_ignored
        )
        results.append(
            RecentTransactionDay(
                key=key,
                label=label,
                items=tuple(ordered_items),
                spending_totals=_spending_totals(ordered_items),
                unconfirmed_totals=_spending_totals(unconfirmed),
                unconfirmed_count=len(unconfirmed),
            )
        )
    return tuple(results)


def _statistics_chart_data(snapshot: StatisticsSnapshot) -> dict[str, object]:
    """Map integer report DTOs to inert JSON consumed by local Chart.js."""
    currencies = []
    for item in snapshot.currencies:
        currencies.append(
            {
                "currency": item.currency,
                "minor_digits": currency_precision(item.currency).minor_unit_exponent,
                "display_digits": currency_precision(
                    item.currency
                ).display_fraction_digits,
                "daily": {
                    "labels": [point.period for point in item.daily_trend],
                    "values": [point.amount_minor for point in item.daily_trend],
                },
                "monthly": {
                    "labels": [point.period for point in item.monthly_trend],
                    "values": [point.amount_minor for point in item.monthly_trend],
                },
                "merchants": {
                    "labels": [point.label for point in item.merchant_ranking],
                    "values": [point.amount_minor for point in item.merchant_ranking],
                },
                "cards": {
                    "labels": [point.label for point in item.card_ranking],
                    "values": [point.amount_minor for point in item.card_ranking],
                },
            }
        )
    return {
        "currencies": currencies,
        "types": {
            "labels": [
                item.transaction_type.value for item in snapshot.type_distribution
            ],
            "values": [item.count for item in snapshot.type_distribution],
        },
    }

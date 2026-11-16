"""Immutable dashboard criteria, view models, and read boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

from app.domain.budget_enums import FinancialEventStatus
from app.domain.enums import (
    ClassificationStatus,
    ProcessingStatus,
    TransactionType,
)
from app.domain.types import JsonValue


@dataclass(frozen=True)
class MoneyTotal:
    """One currency-safe aggregate."""

    currency: str
    amount_minor: int


@dataclass(frozen=True)
class SyncSummary:
    """Newest synchronization outcome shown without Gmail access."""

    status: str
    mode: str
    finished_at: datetime | None
    error_message: str | None
    history_id: str | None


@dataclass(frozen=True)
class TransactionListItem:
    """Transaction list projection with its source email."""

    id: int
    email_id: int
    transaction_at: datetime | None
    merchant: str | None
    amount_minor: int | None
    currency: str | None
    transaction_type: TransactionType
    card_last_four: str | None
    institution_key: str | None
    institution_name: str | None
    is_supplementary_card: bool
    confidence: Decimal
    parser_name: str
    parser_version: str
    manually_confirmed: bool
    manually_ignored: bool
    subject: str
    sender: str


@dataclass(frozen=True)
class DashboardSnapshot:
    """Home page summary; monetary values are always grouped by currency."""

    today_totals: tuple[MoneyTotal, ...]
    week_totals: tuple[MoneyTotal, ...]
    month_totals: tuple[MoneyTotal, ...]
    transaction_count: int
    unconfirmed_count: int
    #: Parsed transactions dated today that nobody has confirmed yet. They are
    #: the first of the two steps between a Gmail notification and a spending
    #: total, so an empty "today" with a non-zero count here is waiting for
    #: the user rather than broken (ADR-B092).
    unconfirmed_today_count: int
    #: What those same unconfirmed transactions add up to, shown as a
    #: provisional figure only — never folded into a confirmed total.
    unconfirmed_today_totals: tuple[MoneyTotal, ...]
    parser_failure_count: int
    recent_transactions: tuple[TransactionListItem, ...]
    last_sync: SyncSummary | None


@dataclass(frozen=True)
class TransactionSearchCriteria:
    """Validated, bounded transaction search inputs."""

    page: int = 1
    page_size: int = 25
    date_from: date | None = None
    date_to: date | None = None
    merchant: str | None = None
    sender: str | None = None
    amount_min: int | None = None
    amount_max: int | None = None
    currency: str | None = None
    transaction_type: TransactionType | None = None
    confidence_min: Decimal | None = None
    review_status: str | None = None
    sort: str = "transaction_at_desc"


@dataclass(frozen=True)
class TransactionPage:
    """One transaction result page."""

    items: tuple[TransactionListItem, ...]
    page: int
    page_size: int
    total: int

    @property
    def pages(self) -> int:
        """Return at least one page for stable navigation."""
        return max(1, (self.total + self.page_size - 1) // self.page_size)


@dataclass(frozen=True)
class TransactionOutcome:
    """What became of one confirmed notification.

    A confirmed notification is taken up as one canonical event, and that
    event still needs allocating before any money moves. Until it is
    allocated there is no wallet to show — only the queue that decides one.
    """

    event_id: int
    status: FinancialEventStatus
    #: Wallets the posted allocation draws from; empty until it is allocated.
    funding_wallets: tuple[str, ...]


@dataclass(frozen=True)
class TransactionDetail:
    """Full audit projection for one transaction and source email."""

    item: TransactionListItem
    updated_at: datetime
    source_key: str
    parsed_values: dict[str, JsonValue]
    effective_values: dict[str, JsonValue]
    manual_overrides: dict[str, JsonValue]
    original_merchant_text: str | None
    account_last_four: str | None
    posted_at: datetime | None
    authorization_code: str | None
    user_note: str | None
    source_status: str
    extraction_evidence: tuple[JsonValue, ...]
    parse_warnings: tuple[JsonValue, ...]
    email_received_at: datetime | None
    email_classification: ClassificationStatus
    classification_reason: str | None
    body_text: str | None
    body_summary: str | None
    #: None until a confirmed notification has been taken up as an event.
    outcome: TransactionOutcome | None = None


@dataclass(frozen=True)
class EmailSearchCriteria:
    """Bounded email list inputs."""

    page: int = 1
    page_size: int = 25
    query: str | None = None
    classification: ClassificationStatus | None = None
    processing: ProcessingStatus | None = None


@dataclass(frozen=True)
class EmailListItem:
    """Email list projection."""

    id: int
    received_at: datetime | None
    sender: str
    subject: str
    classification_status: ClassificationStatus
    processing_status: ProcessingStatus
    transaction_count: int
    parser_name: str | None
    confidence: Decimal | None
    source_status: str


@dataclass(frozen=True)
class EmailPage:
    """One email result page."""

    items: tuple[EmailListItem, ...]
    page: int
    page_size: int
    total: int

    @property
    def pages(self) -> int:
        return max(1, (self.total + self.page_size - 1) // self.page_size)


@dataclass(frozen=True)
class AttachmentDetail:
    """Safe attachment status without secret values."""

    filename: str
    mime_type: str
    size_bytes: int
    encrypted: bool
    extraction_status: str
    extractor_name: str | None
    error_message: str | None


@dataclass(frozen=True)
class EmailDetail:
    """Full locally retained email projection."""

    item: EmailListItem
    gmail_message_id: str
    gmail_thread_id: str
    recipients: tuple[str, ...]
    labels: tuple[str, ...]
    snippet: str
    body_text: str | None
    body_summary: str | None
    has_html_body: bool
    classification_reason: str | None
    parser_version: str | None
    attachments: tuple[AttachmentDetail, ...]
    transactions: tuple[TransactionListItem, ...]


class DashboardReadRepository(Protocol):
    """SQL-free read boundary used by web routes."""

    def dashboard(
        self, *, now: datetime, recent_days: int = 7
    ) -> DashboardSnapshot: ...

    def search_transactions(
        self, criteria: TransactionSearchCriteria
    ) -> TransactionPage: ...

    def transaction_detail(self, transaction_id: int) -> TransactionDetail | None: ...

    def search_emails(self, criteria: EmailSearchCriteria) -> EmailPage: ...

    def email_detail(self, email_id: int) -> EmailDetail | None: ...


class DashboardQueries:
    """Thin application query facade over a replaceable read repository."""

    def __init__(self, repository: DashboardReadRepository) -> None:
        self._repository = repository

    def dashboard(self, *, now: datetime, recent_days: int = 7) -> DashboardSnapshot:
        return self._repository.dashboard(now=now, recent_days=recent_days)

    def search_transactions(
        self, criteria: TransactionSearchCriteria
    ) -> TransactionPage:
        return self._repository.search_transactions(criteria)

    def transaction_detail(self, transaction_id: int) -> TransactionDetail | None:
        return self._repository.transaction_detail(transaction_id)

    def search_emails(self, criteria: EmailSearchCriteria) -> EmailPage:
        return self._repository.search_emails(criteria)

    def email_detail(self, email_id: int) -> EmailDetail | None:
        return self._repository.email_detail(email_id)

"""Read boundary for daily spending, accounts, matching, and reconciliation.

Every total here is derived from posted expense ledger lines rather than from
financial event magnitudes (ADR-B064). An event carries a positive magnitude
and a type; adding those up would count a refund as spending, count a reversal
twice, and collapse a two-category purchase into a single number.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from app.domain.budget_enums import (
    FinancialEventOrigin,
    FinancialEventStatus,
    FinancialEventType,
    JournalEntryType,
    PaymentMethod,
    SourceDiscrepancy,
    SourceLinkReason,
    SourceLinkStatus,
    WalletPurpose,
)
from app.domain.budget_models import (
    BalanceSnapshot,
    FinancialAccount,
    FinancialEvent,
    JournalEntry,
)
from app.domain.budget_rules import REFUND_EVENT_TYPES, local_day_bounds

#: How many days the mobile date strip shows around the selected one.
DATE_STRIP_DAYS = 7
#: The largest page the daily list will ever render or count.
MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 25

#: The two instruments the daily page is about. A bank-paid direct debit is
#: still spending, but it belongs to the account it left, not to the pocket
#: view; a transfer or a deposit is not spending at all (ADR-B064).
DAILY_PAYMENT_METHODS = (PaymentMethod.CASH, PaymentMethod.CREDIT_CARD)

_FILTER_METHODS: dict[str, tuple[PaymentMethod, ...]] = {
    "all": DAILY_PAYMENT_METHODS,
    "cash": (PaymentMethod.CASH,),
    "card": (PaymentMethod.CREDIT_CARD,),
}


def parse_payment_filter(value: str | None) -> str:
    """Return one of the three stable filter codes, defaulting to everything."""
    code = (value or "").strip().lower()
    return code if code in _FILTER_METHODS else "all"


def payment_methods_for(filter_code: str) -> tuple[PaymentMethod, ...]:
    """Return the payment methods one stable filter code selects."""
    return _FILTER_METHODS[parse_payment_filter(filter_code)]


@dataclass(frozen=True)
class DailyCategoryShare:
    """One category's share of a single day's row, in exact minor units."""

    budget_category_id: int | None
    name: str
    amount_minor: int


@dataclass(frozen=True)
class DailyActivityRow:
    """One posted spending entry as the daily page shows it.

    A reversal entry carries no financial event of its own, so the row it
    displays is resolved by following `reversal_of_id` back to the entry it
    cancels and reading that entry's event (ADR-B064).
    """

    journal_entry_id: int
    financial_event_id: int
    effective_at: datetime
    entry_type: JournalEntryType
    event_type: FinancialEventType
    event_status: FinancialEventStatus
    payment_method: PaymentMethod
    merchant: str | None
    note: str | None
    amount_minor: int
    currency: str
    card_display: str | None
    wallet_display: str | None
    categories: tuple[DailyCategoryShare, ...]

    @property
    def is_reversal(self) -> bool:
        """Return whether this row cancels an earlier entry."""
        return self.entry_type is JournalEntryType.REVERSAL

    @property
    def reduces_spending(self) -> bool:
        """Return whether this row gives money back rather than spends it."""
        return self.amount_minor < 0


@dataclass(frozen=True)
class DailyTotal:
    """One day's net spending in exactly one currency and one instrument."""

    payment_method: PaymentMethod
    currency: str
    amount_minor: int


@dataclass(frozen=True)
class LegacyDailyActivityRow:
    """One imported Sheet row shown as history without ledger side effects."""

    id: int
    activity_date: date
    sequence: int
    payment_method: PaymentMethod
    amount_minor: int
    currency: str
    note: str | None


def _daily_totals(
    rows: tuple[LegacyDailyActivityRow, ...],
) -> tuple[DailyTotal, ...]:
    grouped: dict[tuple[PaymentMethod, str], int] = {}
    for row in rows:
        key = (row.payment_method, row.currency)
        grouped[key] = grouped.get(key, 0) + row.amount_minor
    return tuple(
        DailyTotal(payment_method=method, currency=currency, amount_minor=amount)
        for (method, currency), amount in sorted(
            grouped.items(), key=lambda item: (item[0][0].value, item[0][1])
        )
    )


@dataclass(frozen=True)
class DailyActivityView:
    """One local day of cash and card spending, ready to render."""

    day: date
    days: tuple[date, ...]
    payment_filter: str
    rows: tuple[DailyActivityRow, ...]
    totals: tuple[DailyTotal, ...]
    legacy_rows: tuple[LegacyDailyActivityRow, ...]
    legacy_totals: tuple[DailyTotal, ...]
    total_rows: int
    page: int
    page_size: int

    @property
    def page_count(self) -> int:
        """Return how many pages this day's list has, never fewer than one."""
        if self.total_rows <= 0:
            return 1
        return (self.total_rows + self.page_size - 1) // self.page_size

    @property
    def has_more(self) -> bool:
        """Return whether a later page of this day exists."""
        return self.page < self.page_count

    def totals_for(self, payment_method: PaymentMethod) -> tuple[DailyTotal, ...]:
        """Return one instrument's totals, one entry per currency.

        Currencies are never added together: a day with TWD and JPY spending
        has two totals, because there is no exchange rate in this application
        and inventing one would silently misstate both.
        """
        return tuple(
            total for total in self.totals if total.payment_method is payment_method
        )

    def legacy_totals_for(
        self, payment_method: PaymentMethod
    ) -> tuple[DailyTotal, ...]:
        """Return imported pre-opening totals without mixing them into ledger totals."""
        return tuple(
            total
            for total in self.legacy_totals
            if total.payment_method is payment_method
        )


@dataclass(frozen=True)
class AccountSnapshotRow:
    """One observation of an account beside what the ledger then held."""

    snapshot: BalanceSnapshot
    ledger_balance_minor: int
    currency: str
    account_display_name: str

    @property
    def difference_minor(self) -> int:
        """Return observed minus ledger, signed, in exact minor units."""
        return self.snapshot.observed_balance_minor - self.ledger_balance_minor

    @property
    def is_reconciled(self) -> bool:
        """Return whether the bank and the ledger agreed at that moment."""
        return self.difference_minor == 0


@dataclass(frozen=True)
class DifferenceCandidate:
    """One entry worth looking at when an observation and the ledger disagree.

    Not an accusation: these are the entries whose shape could produce the
    gap, ranked by nothing. The bank statement decides which one it was.
    """

    journal_entry_id: int
    effective_at: datetime
    recorded_at: datetime
    amount_minor: int
    currency: str
    description: str
    financial_event_id: int | None
    merchant: str | None
    #: Stable code, so the wording lives in the presentation layer.
    reason: str


#: An entry whose own amount equals the gap, which one missing or one
#: duplicated posting would produce.
CANDIDATE_AMOUNT_MATCHES = "amount_matches_difference"
#: An entry effective before the observation but recorded after it. The
#: comparison is made as of the observed instant (ADR-B067), so an entry
#: like this changes a difference that was zero when it was written down.
CANDIDATE_RECORDED_LATE = "recorded_after_the_observation"


@dataclass(frozen=True)
class DifferenceExplanation:
    """What can be said about one account's newest unexplained difference."""

    snapshot: AccountSnapshotRow | None
    candidates: tuple[DifferenceCandidate, ...]

    @property
    def difference_minor(self) -> int:
        """Return the gap being explained, or zero when there is none."""
        return 0 if self.snapshot is None else self.snapshot.difference_minor


@dataclass(frozen=True)
class AccountWalletRow:
    """One wallet's three different numbers at the same time."""

    wallet_id: int
    name: str
    currency: str
    balance_minor: int
    reserved_minor: int
    purpose: WalletPurpose
    #: What the rename form has to submit back, so a page loaded before
    #: someone else renamed the same wallet is refused rather than winning.
    version: int = 1

    @property
    def available_minor(self) -> int:
        """Return what is left once every reservation is counted."""
        return self.balance_minor - self.reserved_minor


@dataclass(frozen=True)
class AccountRow:
    """One physical account, its wallets, and its latest observation."""

    account: FinancialAccount
    balance_minor: int
    wallets: tuple[AccountWalletRow, ...]
    latest_snapshot: AccountSnapshotRow | None

    @property
    def reserved_minor(self) -> int:
        """Return everything this account's wallets still hold back."""
        return sum(wallet.reserved_minor for wallet in self.wallets)

    @property
    def available_minor(self) -> int:
        """Return the account balance no reservation has claimed."""
        return self.balance_minor - self.reserved_minor


@dataclass(frozen=True)
class AccountActivityRow:
    """One posted entry that touched a physical account."""

    journal_entry_id: int
    effective_at: datetime
    entry_type: JournalEntryType
    description: str
    amount_minor: int
    currency: str
    wallet_display: str | None
    financial_event_id: int | None


@dataclass(frozen=True)
class AccountDetailView:
    """One account page: position, composition, evidence, and recent moves."""

    row: AccountRow
    snapshots: tuple[AccountSnapshotRow, ...]
    activity: tuple[AccountActivityRow, ...]


@dataclass(frozen=True)
class SourceLinkRow:
    """One proposed pairing with the evidence a person needs to decide.

    No Email body ever reaches this row. The merchant, amount, currency, time,
    and card identity are the same values the comparison itself used, plus the
    stable reason codes that explain the score.
    """

    link_id: int
    status: SourceLinkStatus
    score_bps: int
    reasons: tuple[SourceLinkReason, ...]
    discrepancies: tuple[SourceDiscrepancy, ...]
    reviewed_at: datetime | None
    transaction_id: int
    email_id: int
    source_merchant: str | None
    source_amount_minor: int | None
    source_currency: str | None
    source_occurred_at: datetime | None
    source_card_last_four: str | None
    event: FinancialEvent
    event_card_display: str | None

    @property
    def same_amount(self) -> bool:
        """Return whether both sides name the same amount and currency."""
        return (
            self.source_amount_minor == self.event.amount_minor
            and self.source_currency == self.event.currency
        )

    @property
    def time_distance_seconds(self) -> int | None:
        """Return how far apart the two observations are, in whole seconds."""
        if self.source_occurred_at is None:
            return None
        return int(
            abs((self.source_occurred_at - self.event.occurred_at).total_seconds())
        )


@dataclass(frozen=True)
class SourceLinkReviewView:
    """Every pairing still waiting for a decision, plus decided history."""

    pending: tuple[SourceLinkRow, ...]
    decided: tuple[SourceLinkRow, ...]
    pending_total: int


@dataclass(frozen=True)
class ReconcileAccountRow:
    """One account's newest observation and the gap it still shows."""

    account: FinancialAccount
    balance_minor: int
    latest: AccountSnapshotRow | None


@dataclass(frozen=True)
class ReconcileView:
    """The reconciliation page: what was observed against what was recorded."""

    accounts: tuple[ReconcileAccountRow, ...]
    snapshots: tuple[AccountSnapshotRow, ...]


@dataclass(frozen=True)
class EventHistoryRow:
    """One entry ever posted for this event, oldest first.

    An allocation may be redone (ADR-B103), which leaves the superseded
    entry and its reversal in place, so an event can have been posted,
    taken back, and posted again. The page says so rather than showing only
    the decision that happens to be current.
    """

    journal_entry_id: int
    entry_type: JournalEntryType
    #: When the money moved, which every entry of one event shares: a
    #: cancellation and its replacement are both effective at the original
    #: instant, because that is when the bank charged the card.
    effective_at: datetime
    #: When Scribe was told to do this, which is what distinguishes the
    #: three steps of a redone allocation from each other.
    recorded_at: datetime
    amount_minor: int
    currency: str
    #: Whether this is the entry the event's money currently rests on.
    is_in_force: bool

    @property
    def is_cancellation(self) -> bool:
        """Return whether this entry took an earlier one back."""
        return self.entry_type is JournalEntryType.REVERSAL


@dataclass(frozen=True)
class EventDetailView:
    """One canonical event with everything B7 can say about it."""

    event: FinancialEvent
    entry: JournalEntry | None
    reversal_entry: JournalEntry | None
    card_display: str | None
    categories: tuple[DailyCategoryShare, ...]
    wallet_display: str | None
    is_dedicated_wallet_spending: bool
    dedicated_source_wallet_display: str | None
    clearing_wallet_display: str | None
    source_links: tuple[SourceLinkRow, ...]
    history: tuple[EventHistoryRow, ...] = ()
    #: Whether this event holds a spending decision at all. A deposit,
    #: interest credit, transfer, or adjustment does not: its entry records
    #: money entering or leaving the bank (ADR-B103).
    has_allocation: bool = False

    @property
    def _is_posted_and_uncancelled(self) -> bool:
        return (
            self.entry is not None
            and self.reversal_entry is None
            and self.event.status is FinancialEventStatus.POSTED
        )

    @property
    def is_reversible(self) -> bool:
        """Return whether this page may offer to cancel the event.

        Origin is part of the answer. A bank's own notification is not
        Scribe's to deny, so offering the button on one only produced a
        refusal after the click (ADR-B103).
        """
        return (
            self._is_posted_and_uncancelled
            and self.event.origin is FinancialEventOrigin.MANUAL
        )

    @property
    def is_redoable(self) -> bool:
        """Return whether this page may offer to decide the allocation again.

        Origin does not matter: what gets redone is the user's own decision
        about wallet and category, never the event. What does matter is that
        such a decision exists. Offering this on a deposit would cancel a
        real bank movement, and on a refund would risk releasing the original
        purchase's reservation twice, so neither is offered (ADR-B103).
        """
        return (
            self._is_posted_and_uncancelled
            and self.has_allocation
            and self.event.event_type not in REFUND_EVENT_TYPES
        )


class SpendingReadRepository(Protocol):
    """Aggregate B7 read models without exposing SQLAlchemy to callers."""

    def daily_rows(
        self,
        *,
        start: datetime,
        end: datetime,
        payment_methods: tuple[PaymentMethod, ...],
        limit: int,
        offset: int,
    ) -> tuple[DailyActivityRow, ...]: ...

    def daily_count(
        self,
        *,
        start: datetime,
        end: datetime,
        payment_methods: tuple[PaymentMethod, ...],
    ) -> int: ...

    def daily_totals(
        self, *, start: datetime, end: datetime
    ) -> tuple[DailyTotal, ...]: ...

    def legacy_daily_rows(
        self,
        *,
        day: date,
        payment_methods: tuple[PaymentMethod, ...],
        limit: int,
    ) -> tuple[LegacyDailyActivityRow, ...]: ...

    def accounts(self) -> tuple[AccountRow, ...]: ...

    def account_detail(
        self, account_id: int, *, activity_limit: int, snapshot_limit: int
    ) -> AccountDetailView | None: ...

    def source_links(
        self, *, pending_limit: int, decided_limit: int
    ) -> SourceLinkReviewView: ...

    def reconciliation(self, *, snapshot_limit: int) -> ReconcileView: ...

    def event_detail(self, event_id: int) -> EventDetailView | None: ...

    def explain_difference(self, account_id: int) -> DifferenceExplanation: ...


class SpendingQueries:
    """Local read-only views backing the daily, account, and reconcile pages."""

    def __init__(
        self, read_repository: SpendingReadRepository, *, local_timezone: str
    ) -> None:
        self._read_repository = read_repository
        self._local_timezone = ZoneInfo(local_timezone)

    def today(self) -> date:
        """Return the current local calendar day, never a UTC one."""
        return datetime.now(self._local_timezone).date()

    def daily(
        self,
        *,
        day: date | None = None,
        payment_filter: str | None = None,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> DailyActivityView:
        """Return one local day of cash and card spending.

        The day's boundary is built in the user's timezone and then converted
        to UTC, so eight hours of a Taipei day never land in the wrong total.
        """
        selected = day or self.today()
        code = parse_payment_filter(payment_filter)
        bounded_size = max(1, min(page_size, MAX_PAGE_SIZE))
        start, end = local_day_bounds(selected, self._local_timezone)
        total_rows = self._read_repository.daily_count(
            start=start, end=end, payment_methods=payment_methods_for(code)
        )
        page_count = max(1, (total_rows + bounded_size - 1) // bounded_size)
        bounded_page = max(1, min(page, page_count))
        rows = self._read_repository.daily_rows(
            start=start,
            end=end,
            payment_methods=payment_methods_for(code),
            limit=bounded_size,
            offset=(bounded_page - 1) * bounded_size,
        )
        legacy_rows = self._read_repository.legacy_daily_rows(
            day=selected,
            payment_methods=payment_methods_for(code),
            limit=MAX_PAGE_SIZE,
        )
        return DailyActivityView(
            day=selected,
            days=_date_strip(selected),
            payment_filter=code,
            rows=rows,
            totals=self._read_repository.daily_totals(start=start, end=end),
            legacy_rows=legacy_rows,
            legacy_totals=_daily_totals(legacy_rows),
            total_rows=total_rows,
            page=bounded_page,
            page_size=bounded_size,
        )

    def accounts(self) -> tuple[AccountRow, ...]:
        """Return every active account with its wallets and latest evidence."""
        return self._read_repository.accounts()

    def account(
        self,
        account_id: int,
        *,
        activity_limit: int = DEFAULT_PAGE_SIZE,
        snapshot_limit: int = 10,
    ) -> AccountDetailView | None:
        """Return one account's position, composition, evidence, and moves."""
        return self._read_repository.account_detail(
            account_id,
            activity_limit=max(1, min(activity_limit, MAX_PAGE_SIZE)),
            snapshot_limit=max(1, min(snapshot_limit, MAX_PAGE_SIZE)),
        )

    def source_links(
        self, *, pending_limit: int = DEFAULT_PAGE_SIZE, decided_limit: int = 10
    ) -> SourceLinkReviewView:
        """Return the pairings waiting for a decision, and recent decisions."""
        return self._read_repository.source_links(
            pending_limit=max(1, min(pending_limit, MAX_PAGE_SIZE)),
            decided_limit=max(1, min(decided_limit, MAX_PAGE_SIZE)),
        )

    def reconciliation(
        self, *, snapshot_limit: int = DEFAULT_PAGE_SIZE
    ) -> ReconcileView:
        """Return each account's newest observation and its remaining gap."""
        return self._read_repository.reconciliation(
            snapshot_limit=max(1, min(snapshot_limit, MAX_PAGE_SIZE))
        )

    def event(self, event_id: int) -> EventDetailView | None:
        """Return one canonical event with its entry, split, and provenance."""
        return self._read_repository.event_detail(event_id)

    def explain_difference(self, account_id: int) -> DifferenceExplanation:
        """Narrow one account's newest difference down to a few entries.

        The reconcile view already reports the gap; what this adds is where
        to look, which otherwise means reading every entry since the last
        observation.
        """
        return self._read_repository.explain_difference(account_id)


def _date_strip(day: date) -> tuple[date, ...]:
    """Return the surrounding week the mobile date strip offers.

    The selected day sits at the end rather than the middle: a person looking
    at spending almost always wants the days that already happened, and
    offering four future days would waste half a small screen.
    """
    first = day - timedelta(days=DATE_STRIP_DAYS - 1)
    return tuple(first + timedelta(days=offset) for offset in range(DATE_STRIP_DAYS))

"""Read boundary for the clearing hub and its scheduled bank movements.

Nothing here decides money. Every amount is either a stored column or a sum of
stored columns, and every currency stays in its own group, so no route or
template ever has to add two currencies together or re-derive a reservation.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.domain.budget_enums import (
    CommitmentFulfillmentMode,
    ScheduledTransferPurpose,
    ScheduledTransferStatus,
)
from app.domain.budget_models import FundingCommitment, ScheduledTransfer

#: Default page size for the history lists. A clearing page must never try to
#: read every movement ever made.
DEFAULT_HISTORY_LIMIT = 25
MAX_HISTORY_LIMIT = 100

UPCOMING_STATUSES = frozenset(
    {ScheduledTransferStatus.SCHEDULED, ScheduledTransferStatus.PROCESSING}
)
FAILED_STATUSES = frozenset({ScheduledTransferStatus.FAILED})
EXECUTED_STATUSES = frozenset({ScheduledTransferStatus.ASSUMED_EXECUTED})
CLOSED_STATUSES = frozenset(
    {ScheduledTransferStatus.CANCELLED, ScheduledTransferStatus.REVERSED}
)
#: Statuses whose schedule can still be withdrawn, so the page can offer the
#: cancel-and-replace correction rather than only naming the problem.
_CANCELLABLE_STATUSES = frozenset(
    {ScheduledTransferStatus.SCHEDULED, ScheduledTransferStatus.FAILED}
)
#: Statuses whose schedule is still part of what reaches the bank. A cancelled
#: or reversed instruction moves nothing, so it can never be the reason a
#: commitment is over-covered and must not be shown as one.
_LIVE_ROW_STATUSES = frozenset(
    {
        ScheduledTransferStatus.SCHEDULED,
        ScheduledTransferStatus.PROCESSING,
        ScheduledTransferStatus.FAILED,
        ScheduledTransferStatus.ASSUMED_EXECUTED,
    }
)
#: What the user can actually do about a surplus this row is part of. The code
#: is decided here; only its wording lives in the template.
CANCEL_AND_REPLACE = "cancel_and_replace"
WAIT_FOR_RUN = "wait_for_run"
REVERSE = "reverse"
#: The order a summary lists the corrections in, from the one the user can do
#: right now to the one that needs a new entry. Fixed so the page never
#: reorders itself between two reads of the same data.
ACTION_ORDER = (CANCEL_AND_REPLACE, WAIT_FOR_RUN, REVERSE)
#: Which correction each live status allows. A cancelled or reversed schedule
#: is absent because it carries no money and offers nothing to correct.
ACTION_BY_STATUS = {
    ScheduledTransferStatus.SCHEDULED: CANCEL_AND_REPLACE,
    ScheduledTransferStatus.FAILED: CANCEL_AND_REPLACE,
    ScheduledTransferStatus.PROCESSING: WAIT_FOR_RUN,
    ScheduledTransferStatus.ASSUMED_EXECUTED: REVERSE,
}


def summarize_actions(statuses: Iterable[ScheduledTransferStatus]) -> tuple[str, ...]:
    """Return, in a fixed order, what the given live schedules really allow.

    A surplus carried only by a claimed schedule cannot be cancelled and one
    carried only by an executed schedule cannot either. Telling the user to
    cancel in those cases contradicts the per-row advice underneath and sends
    them after an action the page will refuse.
    """
    offered = {
        ACTION_BY_STATUS[status] for status in statuses if status in ACTION_BY_STATUS
    }
    return tuple(action for action in ACTION_ORDER if action in offered)


def paged_offset(*, total: int, limit: int, offset: int) -> int:
    """Return an offset that really has rows, given how many exist.

    A hand-typed `?warn_page=1000` must not produce an empty page that claims
    to be showing rows 0 to 24,975. Clamping to the last page that exists is
    the behaviour the user meant, and it keeps `shown_from`/`shown_to`
    truthful without a redirect.
    """
    if total <= 0 or limit <= 0:
        return 0
    if offset < 0:
        return 0
    if offset < total:
        return (offset // limit) * limit
    return ((total - 1) // limit) * limit


@dataclass(frozen=True)
class ClearingWalletPosition:
    """The three different numbers one wallet has at the same time."""

    wallet_id: int
    wallet_name: str
    financial_account_id: int
    account_name: str
    institution_code: str
    currency: str
    is_clearing_hub: bool
    balance_minor: int
    reserved_minor: int

    @property
    def available_minor(self) -> int:
        """Return what is left once every reservation is counted."""
        return self.balance_minor - self.reserved_minor


@dataclass(frozen=True)
class SchedulableCommitmentRow:
    """One reservation and how much of it no schedule has taken on."""

    commitment: FundingCommitment
    source_wallet_name: str
    source_account_id: int
    source_account_name: str
    settlement_account_id: int
    settlement_account_name: str
    card_id: int | None
    card_display_name: str | None
    active_covered_minor: int
    executed_covered_minor: int

    @property
    def required_minor(self) -> int:
        """Return what this commitment still asks for after any refund."""
        return self.commitment.amount_minor - self.commitment.released_minor

    @property
    def uncovered_minor(self) -> int:
        """Return the part no schedule has promised to move."""
        return max(
            0,
            self.required_minor
            - self.active_covered_minor
            - self.executed_covered_minor,
        )

    @property
    def needs_transfer(self) -> bool:
        """Return whether money has to leave this account at all."""
        return (
            self.commitment.fulfillment_mode
            is not CommitmentFulfillmentMode.RESERVE_ONLY
        )


@dataclass(frozen=True)
class SchedulableGroup:
    """Commitments that one single bank movement could settle together.

    The grouping key is the whole reason the link table exists: the same
    source account, the same destination, and the same currency (ADR-B052).
    """

    source_account_id: int
    source_account_name: str
    settlement_account_id: int
    settlement_account_name: str
    currency: str
    fulfillment_mode: CommitmentFulfillmentMode
    rows: tuple[SchedulableCommitmentRow, ...]

    @property
    def uncovered_minor(self) -> int:
        """Return what one movement would have to carry to settle this group."""
        return sum(row.uncovered_minor for row in self.rows)


@dataclass(frozen=True)
class ScheduleLinkRow:
    """One commitment share a schedule covers, and that commitment's coverage.

    ``live_coverage_minor`` is the whole commitment's coverage across *every*
    schedule that is `scheduled`, `processing`, `failed`, or
    `assumed_executed` — not this link's share. Over-coverage is a property of
    the commitment, so it can only be decided once all of them are added up.
    """

    funding_commitment_id: int
    amount_minor: int
    required_minor: int
    live_coverage_minor: int

    @property
    def commitment_excess_minor(self) -> int:
        """Return how much more this commitment has scheduled than it owes.

        Comparing one link with the requirement on its own is not enough: a
        commitment of 8,000 split across two schedules of 4,000 and then
        refunded by 4,000 leaves each link at exactly 4,000, so each one
        separately looks perfectly covered while 8,000 is still on its way to
        the bank. The aggregate is the only number that sees it (ADR-B060).
        """
        return max(0, self.live_coverage_minor - self.required_minor)


@dataclass(frozen=True)
class ScheduleRow:
    """One scheduled movement with every name its screen has to show."""

    schedule: ScheduledTransfer
    source_account_name: str
    source_wallet_name: str
    destination_account_name: str
    destination_wallet_name: str | None
    card_display_name: str | None
    links: tuple[ScheduleLinkRow, ...]

    @property
    def covered_minor(self) -> int:
        """Return how much of this movement is tied to a commitment."""
        return sum(link.amount_minor for link in self.links)

    @property
    def unlinked_minor(self) -> int:
        """Return the part of a card payment no commitment is behind."""
        return self.schedule.amount_minor - self.covered_minor

    @property
    def is_live(self) -> bool:
        """Return whether this movement is still part of what reaches the bank."""
        return self.schedule.status in _LIVE_ROW_STATUSES

    @property
    def excess_minor(self) -> int:
        """Return the over-coverage this movement is itself part of.

        One schedule links each commitment at most once, so summing across its
        own links never counts the same commitment twice. Two live schedules
        that share an over-covered commitment both report it, which is the
        point: either of them is a candidate for correction and the user
        decides which.

        A cancelled or reversed movement reports nothing. It carries no money
        any more, so blaming it for a surplus that its still-live sibling
        creates would point the user at the one row that is already fixed.
        """
        if not self.is_live:
            return 0
        return sum(link.commitment_excess_minor for link in self.links)

    @property
    def has_refund_warning(self) -> bool:
        """Return whether a commitment this live movement funds is over-covered."""
        return self.excess_minor > 0

    @property
    def warning_action(self) -> str | None:
        """Return what can still be done about this row's surplus, or None.

        The three cases are genuinely different: a scheduled or failed movement
        can be withdrawn, a claimed one has to finish its pass first and has
        moved nothing yet, and an executed one has already moved the money and
        can only be reversed.
        """
        if not self.has_refund_warning:
            return None
        if self.is_cancellable:
            return CANCEL_AND_REPLACE
        if self.schedule.status is ScheduledTransferStatus.PROCESSING:
            return WAIT_FOR_RUN
        return REVERSE

    @property
    def is_cancellable(self) -> bool:
        """Return whether cancelling this movement could still free money."""
        return self.schedule.status in _CANCELLABLE_STATUSES

    @property
    def is_autopay(self) -> bool:
        """Return whether this movement pays a card rather than an account."""
        return self.schedule.purpose is ScheduledTransferPurpose.CARD_AUTOPAY


@dataclass(frozen=True)
class SchedulePage:
    """One bounded page of schedules and how many there are in total."""

    rows: tuple[ScheduleRow, ...]
    total: int
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        """Return whether older movements exist beyond this page."""
        return self.offset + len(self.rows) < self.total

    @property
    def page_number(self) -> int:
        """Return which page this is, counting from one."""
        return self.offset // self.limit + 1 if self.limit else 1

    @property
    def page_count(self) -> int:
        """Return how many pages exist; an empty list is still one page."""
        if self.limit <= 0 or self.total <= 0:
            return 1
        return (self.total - 1) // self.limit + 1


@dataclass(frozen=True)
class ClearingAccountOption:
    """One asset account a movement could leave from or arrive in."""

    financial_account_id: int
    display_name: str
    institution_code: str
    currency: str


@dataclass(frozen=True)
class ClearingWalletOption:
    """One wallet a movement could leave from or arrive in."""

    wallet_id: int
    wallet_name: str
    financial_account_id: int
    account_name: str
    currency: str
    is_clearing_hub: bool


@dataclass(frozen=True)
class ClearingCardOption:
    """One card whose configuration is complete enough to be paid."""

    card_id: int
    display_name: str
    settlement_account_id: int
    settlement_account_name: str
    liability_account_id: int
    currency: str
    outstanding_minor: int


@dataclass(frozen=True)
class OverCoveredCommitment:
    """One commitment with more money scheduled at it than it still needs."""

    funding_commitment_id: int
    source_wallet_name: str
    settlement_account_name: str
    currency: str
    required_minor: int
    live_coverage_minor: int

    @property
    def excess_minor(self) -> int:
        """Return how much more is on its way than is still owed."""
        return max(0, self.live_coverage_minor - self.required_minor)


@dataclass(frozen=True)
class OverCoveredPage:
    """One bounded page of over-covered commitments, and how many exist.

    ``total`` is counted in the database rather than taken from ``rows``, so a
    page that shows the first 25 of 26 never lets the page claim there are 25.
    """

    rows: tuple[OverCoveredCommitment, ...]
    total: int
    limit: int
    offset: int
    #: What the live schedules behind *every* over-covered commitment allow,
    #: not only the ones on this page, so a truncated list still summarizes
    #: the whole problem correctly.
    actions: tuple[str, ...] = ()

    @property
    def has_more(self) -> bool:
        """Return whether more over-covered commitments exist beyond this page."""
        return self.offset + len(self.rows) < self.total

    @property
    def page_number(self) -> int:
        """Return which page this is, counting from one."""
        return self.offset // self.limit + 1 if self.limit else 1

    @property
    def page_count(self) -> int:
        """Return how many pages exist; an empty list is still one page."""
        if self.limit <= 0 or self.total <= 0:
            return 1
        return (self.total - 1) // self.limit + 1

    @property
    def shown_from(self) -> int:
        """Return the 1-based position of the first row on this page."""
        return self.offset + 1 if self.rows else 0

    @property
    def shown_to(self) -> int:
        """Return the 1-based position of the last row on this page."""
        return self.offset + len(self.rows)


@dataclass(frozen=True)
class ClearingView:
    """Everything the local clearing page shows, already decided."""

    now: datetime
    positions: tuple[ClearingWalletPosition, ...]
    groups: tuple[SchedulableGroup, ...]
    upcoming: SchedulePage
    failed: SchedulePage
    executed: SchedulePage
    closed: SchedulePage
    accounts: tuple[ClearingAccountOption, ...]
    wallets: tuple[ClearingWalletOption, ...]
    cards: tuple[ClearingCardOption, ...]
    due_now: int
    over_covered_commitments: OverCoveredPage

    @property
    def over_covered(self) -> int:
        """Return how many commitments have more scheduled than they owe.

        This is the database's exact count, not the length of the page being
        displayed, so a truncated list never understates the problem.
        """
        return self.over_covered_commitments.total

    @property
    def has_refund_warning(self) -> bool:
        """Return whether anything is over-covered anywhere.

        This deliberately does not read the paged schedule rows. A surplus
        whose schedules all sit on a later page would otherwise go
        unmentioned, and the whole point of the warning is that the user finds
        out.
        """
        return self.over_covered > 0


class ClearingReadRepository(Protocol):
    """Indexed reads the clearing page and the CLI share."""

    def positions(self) -> tuple[ClearingWalletPosition, ...]: ...

    def schedulable_groups(self) -> tuple[SchedulableGroup, ...]: ...

    def schedules(
        self,
        statuses: frozenset[ScheduledTransferStatus],
        *,
        limit: int,
        offset: int,
    ) -> SchedulePage: ...

    def schedule(self, scheduled_transfer_id: int) -> ScheduleRow | None: ...

    def over_covered_commitments(
        self, *, limit: int, offset: int
    ) -> OverCoveredPage: ...

    def account_options(self) -> tuple[ClearingAccountOption, ...]: ...

    def wallet_options(self) -> tuple[ClearingWalletOption, ...]: ...

    def card_options(self) -> tuple[ClearingCardOption, ...]: ...

    def due_now(self, now: datetime) -> int: ...


class ClearingQueries:
    """Assemble the clearing page from bounded, indexed reads."""

    def __init__(self, repository: ClearingReadRepository) -> None:
        self._repository = repository

    def view(
        self,
        *,
        now: datetime,
        limit: int = DEFAULT_HISTORY_LIMIT,
        offset: int = 0,
        warn_offset: int = 0,
    ) -> ClearingView:
        """Return one complete, bounded view of the clearing hub.

        The warning list has its own offset, because it is a different list
        with a different length; paging the history must not decide whether an
        over-covered commitment is reachable.
        """
        bounded = max(1, min(limit, MAX_HISTORY_LIMIT))
        page = max(0, offset)
        return ClearingView(
            now=now,
            positions=self._repository.positions(),
            groups=self._repository.schedulable_groups(),
            upcoming=self._repository.schedules(
                UPCOMING_STATUSES, limit=bounded, offset=page
            ),
            failed=self._repository.schedules(FAILED_STATUSES, limit=bounded, offset=0),
            executed=self._repository.schedules(
                EXECUTED_STATUSES, limit=bounded, offset=page
            ),
            closed=self._repository.schedules(
                CLOSED_STATUSES, limit=bounded, offset=page
            ),
            accounts=self._repository.account_options(),
            wallets=self._repository.wallet_options(),
            cards=self._repository.card_options(),
            due_now=self._repository.due_now(now),
            over_covered_commitments=self._repository.over_covered_commitments(
                limit=bounded, offset=max(0, warn_offset)
            ),
        )

    def schedule(self, scheduled_transfer_id: int) -> ScheduleRow | None:
        """Return one movement with its names and coverage, or None."""
        return self._repository.schedule(scheduled_transfer_id)

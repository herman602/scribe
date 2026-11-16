"""Pure rules behind the clearing hub and its assumed bank movements."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from app.domain.budget_enums import (
    CommitmentStatus,
    ScheduledExecutionPolicy,
    ScheduledTransferPurpose,
    ScheduledTransferStatus,
)
from app.domain.budget_models import (
    ScheduledTransferCommitmentDraft,
    ScheduledTransferDraft,
    validate_aware,
    validate_wall_clock,
)
from app.domain.budget_rules import (
    ScheduleCoverage,
    WalletCommitmentView,
    WalletScheduleDebitView,
    derive_commitment_status,
    ensure_clearing_coverage_exact,
    ensure_schedule_transition,
    resolve_due_at,
    retry_delay_seconds,
    schedule_wallet_debits,
    scheduled_transfer_create_key,
    scheduled_transfer_execute_key,
    scheduled_transfer_fingerprint,
    scheduled_transfer_reverse_key,
    wallet_reserved_minor,
)
from app.domain.errors import (
    ScheduledTransferShapeError,
    ScheduledTransferStateError,
    ScheduledTransferTimeError,
)

TAIPEI = "Asia/Taipei"
BERLIN = "Europe/Berlin"


def _draft(**overrides: Any) -> ScheduledTransferDraft:
    values: dict[str, Any] = {
        "purpose": ScheduledTransferPurpose.CLEARING_FUNDING,
        "source_account_id": 1,
        "source_wallet_id": 10,
        "destination_account_id": 2,
        "destination_wallet_id": 20,
        "amount_minor": 12_000,
        "currency": "twd",
        "scheduled_local_date": date(2026, 9, 1),
        "scheduled_local_time": time(6, 0),
        "timezone": TAIPEI,
        "due_at_utc": datetime(2026, 8, 31, 22, 0, tzinfo=UTC),
        "idempotency_key": "scheduled_transfer:create:token-1",
    }
    values.update(overrides)
    return ScheduledTransferDraft(**values)


class TestDueTimeResolution:
    """A local policy time must name exactly one instant."""

    def test_taipei_six_in_the_morning_is_twenty_two_utc_the_day_before(self) -> None:
        due = resolve_due_at(
            local_date=date(2026, 9, 1), local_time=time(6, 0), timezone=TAIPEI
        )
        assert due == datetime(2026, 8, 31, 22, 0, tzinfo=UTC)
        assert due.tzinfo is not None

    def test_result_round_trips_back_to_the_local_time_that_was_chosen(self) -> None:
        due = resolve_due_at(
            local_date=date(2026, 9, 1), local_time=time(6, 0), timezone=TAIPEI
        )
        local = due.astimezone(ZoneInfo(TAIPEI))
        assert (local.date(), local.hour, local.minute) == (date(2026, 9, 1), 6, 0)

    def test_a_nonexistent_local_time_is_refused_instead_of_shifted(self) -> None:
        # Berlin skips 02:30 on the last Sunday of March.
        with pytest.raises(ScheduledTransferTimeError, match="does not exist"):
            resolve_due_at(
                local_date=date(2026, 3, 29), local_time=time(2, 30), timezone=BERLIN
            )

    def test_an_ambiguous_local_time_is_refused_instead_of_guessed(self) -> None:
        # Berlin repeats 02:30 on the last Sunday of October.
        with pytest.raises(ScheduledTransferTimeError, match="happens twice"):
            resolve_due_at(
                local_date=date(2026, 10, 25), local_time=time(2, 30), timezone=BERLIN
            )

    def test_a_time_carrying_its_own_offset_is_refused(self) -> None:
        with pytest.raises(ValueError, match="carries no timezone"):
            resolve_due_at(
                local_date=date(2026, 9, 1),
                local_time=time(6, 0, tzinfo=UTC),
                timezone=TAIPEI,
            )

    def test_seconds_imply_a_precision_the_policy_does_not_have(self) -> None:
        with pytest.raises(ValueError, match="whole minute"):
            validate_wall_clock(time(6, 0, 30), label="scheduled time")

    def test_a_datetime_is_not_accepted_as_a_calendar_date(self) -> None:
        with pytest.raises(TypeError, match="calendar date"):
            resolve_due_at(
                local_date=datetime(2026, 9, 1, 6, 0),
                local_time=time(6, 0),
                timezone=TAIPEI,
            )


class TestScheduledTransferDraft:
    """A draft freezes an exact, self-consistent instruction."""

    @pytest.mark.parametrize("amount", [True, 1200.5, "1200", None])
    def test_inexact_amounts_are_refused(self, amount: object) -> None:
        with pytest.raises(TypeError):
            _draft(amount_minor=amount)

    @pytest.mark.parametrize("amount", [0, -1])
    def test_a_schedule_moves_a_positive_amount(self, amount: int) -> None:
        with pytest.raises(ValueError):
            _draft(amount_minor=amount)

    def test_currency_is_normalized(self) -> None:
        assert _draft().currency == "TWD"

    def test_a_naive_due_time_is_refused(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            _draft(due_at_utc=datetime(2026, 8, 31, 22, 0))

    def test_one_account_cannot_transfer_to_itself(self) -> None:
        with pytest.raises(ValueError, match="two different accounts"):
            _draft(destination_account_id=1)

    def test_a_clearing_transfer_names_no_card(self) -> None:
        with pytest.raises(ValueError, match="does not name a card"):
            _draft(card_id=5)

    def test_a_clearing_transfer_needs_a_destination_wallet(self) -> None:
        with pytest.raises(ValueError, match="destination wallet"):
            _draft(destination_wallet_id=None)

    def test_a_card_autopay_names_its_card(self) -> None:
        with pytest.raises(ValueError, match="names the card"):
            _draft(
                purpose=ScheduledTransferPurpose.CARD_AUTOPAY,
                destination_wallet_id=None,
                card_id=None,
            )

    def test_a_card_liability_holds_no_wallet(self) -> None:
        with pytest.raises(ValueError, match="holds no wallet"):
            _draft(purpose=ScheduledTransferPurpose.CARD_AUTOPAY, card_id=7)

    def test_a_valid_autopay_draft_is_accepted(self) -> None:
        draft = _draft(
            purpose=ScheduledTransferPurpose.CARD_AUTOPAY,
            destination_wallet_id=None,
            card_id=7,
        )
        assert draft.card_id == 7
        assert draft.execution_policy is ScheduledExecutionPolicy.ASSUME_AT_DUE_TIME

    @pytest.mark.parametrize("amount", [True, 1.5, 0, -3])
    def test_a_link_share_is_a_positive_integer(self, amount: object) -> None:
        with pytest.raises((TypeError, ValueError)):
            ScheduledTransferCommitmentDraft(
                scheduled_transfer_id=1,
                funding_commitment_id=2,
                amount_minor=amount,  # type: ignore[arg-type]
            )

    def test_a_naive_instant_is_never_accepted_as_aware(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            validate_aware(datetime(2026, 9, 1, 6, 0), label="due time")


class TestStateMachine:
    """Only the accepted lifecycle moves are possible."""

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            (ScheduledTransferStatus.SCHEDULED, ScheduledTransferStatus.PROCESSING),
            (ScheduledTransferStatus.SCHEDULED, ScheduledTransferStatus.CANCELLED),
            (
                ScheduledTransferStatus.PROCESSING,
                ScheduledTransferStatus.ASSUMED_EXECUTED,
            ),
            (ScheduledTransferStatus.PROCESSING, ScheduledTransferStatus.SCHEDULED),
            (ScheduledTransferStatus.PROCESSING, ScheduledTransferStatus.FAILED),
            (ScheduledTransferStatus.FAILED, ScheduledTransferStatus.SCHEDULED),
            (ScheduledTransferStatus.FAILED, ScheduledTransferStatus.CANCELLED),
            (
                ScheduledTransferStatus.ASSUMED_EXECUTED,
                ScheduledTransferStatus.REVERSED,
            ),
        ],
    )
    def test_accepted_transitions(
        self, current: ScheduledTransferStatus, target: ScheduledTransferStatus
    ) -> None:
        ensure_schedule_transition(current, target)

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            (ScheduledTransferStatus.PROCESSING, ScheduledTransferStatus.CANCELLED),
            (
                ScheduledTransferStatus.SCHEDULED,
                ScheduledTransferStatus.ASSUMED_EXECUTED,
            ),
            (ScheduledTransferStatus.CANCELLED, ScheduledTransferStatus.SCHEDULED),
            (ScheduledTransferStatus.REVERSED, ScheduledTransferStatus.SCHEDULED),
            (
                ScheduledTransferStatus.ASSUMED_EXECUTED,
                ScheduledTransferStatus.CANCELLED,
            ),
            (ScheduledTransferStatus.FAILED, ScheduledTransferStatus.ASSUMED_EXECUTED),
        ],
    )
    def test_refused_transitions(
        self, current: ScheduledTransferStatus, target: ScheduledTransferStatus
    ) -> None:
        with pytest.raises(ScheduledTransferStateError):
            ensure_schedule_transition(current, target)


class TestCommitmentStatus:
    """One function decides a commitment's status everywhere."""

    def test_a_fully_released_commitment_is_cancelled(self) -> None:
        assert (
            derive_commitment_status(required_minor=0, active_minor=0, executed_minor=0)
            is CommitmentStatus.CANCELLED
        )

    def test_executed_coverage_funds_the_commitment(self) -> None:
        assert (
            derive_commitment_status(
                required_minor=1000, active_minor=0, executed_minor=1000
            )
            is CommitmentStatus.FUNDED
        )

    def test_active_coverage_only_schedules_it(self) -> None:
        assert (
            derive_commitment_status(
                required_minor=1000, active_minor=1000, executed_minor=0
            )
            is CommitmentStatus.SCHEDULED
        )

    def test_partial_coverage_is_partially_scheduled(self) -> None:
        assert (
            derive_commitment_status(
                required_minor=1000, active_minor=400, executed_minor=0
            )
            is CommitmentStatus.PARTIALLY_SCHEDULED
        )

    def test_nothing_covered_is_open(self) -> None:
        assert (
            derive_commitment_status(
                required_minor=1000, active_minor=0, executed_minor=0
            )
            is CommitmentStatus.OPEN
        )

    def test_a_refund_that_shrinks_the_requirement_funds_it(self) -> None:
        assert (
            derive_commitment_status(
                required_minor=200, active_minor=0, executed_minor=1000
            )
            is CommitmentStatus.FUNDED
        )

    @pytest.mark.parametrize("value", [True, 1.5, -1])
    def test_inexact_coverage_is_refused(self, value: object) -> None:
        with pytest.raises((TypeError, ValueError)):
            derive_commitment_status(
                required_minor=1000,
                active_minor=value,  # type: ignore[arg-type]
                executed_minor=0,
            )


class TestWalletDebits:
    """Every part of one movement leaves the wallet that reserved it."""

    def test_an_unlinked_schedule_debits_only_its_own_wallet(self) -> None:
        assert schedule_wallet_debits(
            amount_minor=5000, source_wallet_id=10, coverage=()
        ) == ((10, 5000),)

    def test_covered_shares_leave_the_wallets_that_reserved_them(self) -> None:
        debits = schedule_wallet_debits(
            amount_minor=5000,
            source_wallet_id=10,
            coverage=(
                ScheduleCoverage(
                    funding_commitment_id=1, source_wallet_id=11, amount_minor=2000
                ),
                ScheduleCoverage(
                    funding_commitment_id=2, source_wallet_id=12, amount_minor=1500
                ),
            ),
        )
        assert debits == ((10, 1500), (11, 2000), (12, 1500))

    def test_shares_of_one_wallet_are_merged_into_one_line(self) -> None:
        debits = schedule_wallet_debits(
            amount_minor=3000,
            source_wallet_id=10,
            coverage=(
                ScheduleCoverage(
                    funding_commitment_id=1, source_wallet_id=10, amount_minor=1000
                ),
                ScheduleCoverage(
                    funding_commitment_id=2, source_wallet_id=10, amount_minor=500
                ),
            ),
        )
        assert debits == ((10, 3000),)

    def test_coverage_can_never_exceed_the_movement(self) -> None:
        with pytest.raises(ScheduledTransferShapeError, match="cover more"):
            schedule_wallet_debits(
                amount_minor=1000,
                source_wallet_id=10,
                coverage=(
                    ScheduleCoverage(
                        funding_commitment_id=1, source_wallet_id=11, amount_minor=1200
                    ),
                ),
            )

    def test_a_clearing_transfer_must_cover_its_amount_exactly(self) -> None:
        ensure_clearing_coverage_exact(amount_minor=1000, covered_minor=1000)
        with pytest.raises(ScheduledTransferShapeError):
            ensure_clearing_coverage_exact(amount_minor=1000, covered_minor=900)


class TestReservation:
    """A commitment and the schedule that funds it are one reservation."""

    def test_an_unscheduled_commitment_reserves_its_whole_requirement(self) -> None:
        assert (
            wallet_reserved_minor(
                salary_waiting_minor=0,
                commitments=(
                    WalletCommitmentView(
                        commitment_id=1,
                        required_minor=1000,
                        active_covered_minor=0,
                        executed_covered_minor=0,
                    ),
                ),
                schedule_debits=(),
            )
            == 1000
        )

    def test_a_commitment_and_its_schedule_are_not_counted_twice(self) -> None:
        assert (
            wallet_reserved_minor(
                salary_waiting_minor=0,
                commitments=(
                    WalletCommitmentView(
                        commitment_id=1,
                        required_minor=1000,
                        active_covered_minor=1000,
                        executed_covered_minor=0,
                    ),
                ),
                schedule_debits=(
                    WalletScheduleDebitView(scheduled_transfer_id=1, debit_minor=1000),
                ),
            )
            == 1000
        )

    def test_partial_scheduling_still_totals_the_requirement(self) -> None:
        assert (
            wallet_reserved_minor(
                salary_waiting_minor=0,
                commitments=(
                    WalletCommitmentView(
                        commitment_id=1,
                        required_minor=1000,
                        active_covered_minor=400,
                        executed_covered_minor=0,
                    ),
                ),
                schedule_debits=(
                    WalletScheduleDebitView(scheduled_transfer_id=1, debit_minor=400),
                ),
            )
            == 1000
        )

    def test_an_executed_schedule_reserves_nothing_and_covers_everything(self) -> None:
        assert (
            wallet_reserved_minor(
                salary_waiting_minor=0,
                commitments=(
                    WalletCommitmentView(
                        commitment_id=1,
                        required_minor=1000,
                        active_covered_minor=0,
                        executed_covered_minor=1000,
                    ),
                ),
                schedule_debits=(),
            )
            == 0
        )

    def test_a_refund_leaves_the_scheduled_movement_reserved(self) -> None:
        # The commitment now requires nothing, but the bank was already told to
        # move 1000, so the wallet still holds it back (ADR-B060).
        assert (
            wallet_reserved_minor(
                salary_waiting_minor=0,
                commitments=(
                    WalletCommitmentView(
                        commitment_id=1,
                        required_minor=0,
                        active_covered_minor=1000,
                        executed_covered_minor=0,
                    ),
                ),
                schedule_debits=(
                    WalletScheduleDebitView(scheduled_transfer_id=1, debit_minor=1000),
                ),
            )
            == 1000
        )

    def test_waiting_salary_is_added_exactly_once(self) -> None:
        assert (
            wallet_reserved_minor(
                salary_waiting_minor=7000,
                commitments=(
                    WalletCommitmentView(
                        commitment_id=1,
                        required_minor=1000,
                        active_covered_minor=0,
                        executed_covered_minor=0,
                    ),
                ),
                schedule_debits=(),
            )
            == 8000
        )


class TestRetryBackoff:
    """Retries grow, but never without bound."""

    @pytest.mark.parametrize(
        ("attempt", "expected"), [(1, 30), (2, 60), (3, 120), (4, 240)]
    )
    def test_backoff_doubles(self, attempt: int, expected: int) -> None:
        assert (
            retry_delay_seconds(attempt=attempt, base_seconds=30, max_seconds=1800)
            == expected
        )

    def test_backoff_is_capped(self) -> None:
        assert (
            retry_delay_seconds(attempt=40, base_seconds=30, max_seconds=1800) == 1800
        )

    def test_a_retry_follows_an_attempt(self) -> None:
        with pytest.raises(ValueError):
            retry_delay_seconds(attempt=0, base_seconds=30, max_seconds=1800)


class TestIdempotencyIdentity:
    """A token is a claim; the payload is the proof."""

    def test_keys_are_stable_and_distinct(self) -> None:
        assert scheduled_transfer_create_key(" token-1 ") == (
            "scheduled_transfer:create:token-1"
        )
        assert scheduled_transfer_execute_key(4) == "scheduled_transfer:execute:4"
        assert scheduled_transfer_reverse_key(4) == "scheduled_transfer:reverse:4"

    def test_an_empty_token_is_refused(self) -> None:
        with pytest.raises(ValueError):
            scheduled_transfer_create_key("   ")

    def test_link_order_does_not_change_the_fingerprint(self) -> None:
        first = scheduled_transfer_fingerprint(
            **_fingerprint_values(), links=((1, 400), (2, 600))
        )
        second = scheduled_transfer_fingerprint(
            **_fingerprint_values(), links=((2, 600), (1, 400))
        )
        assert first == second

    def test_a_different_amount_changes_the_fingerprint(self) -> None:
        values = _fingerprint_values()
        first = scheduled_transfer_fingerprint(**values, links=((1, 400),))
        values["amount_minor"] = 1100
        second = scheduled_transfer_fingerprint(**values, links=((1, 400),))
        assert first != second

    def test_a_different_due_time_changes_the_fingerprint(self) -> None:
        values = _fingerprint_values()
        first = scheduled_transfer_fingerprint(**values, links=())
        values["due_at_utc"] = datetime(2026, 8, 31, 23, 0, tzinfo=UTC)
        second = scheduled_transfer_fingerprint(**values, links=())
        assert first != second


def _fingerprint_values() -> dict[str, Any]:
    return {
        "purpose": ScheduledTransferPurpose.CLEARING_FUNDING,
        "source_account_id": 1,
        "source_wallet_id": 10,
        "destination_account_id": 2,
        "destination_wallet_id": 20,
        "card_id": None,
        "amount_minor": 1000,
        "currency": "TWD",
        "scheduled_local_date": date(2026, 9, 1),
        "scheduled_local_time": time(6, 0),
        "timezone": TAIPEI,
        "due_at_utc": datetime(2026, 8, 31, 22, 0, tzinfo=UTC),
        "execution_policy": ScheduledExecutionPolicy.ASSUME_AT_DUE_TIME,
    }

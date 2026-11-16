"""B5 domain rules: money shapes, statuses, fulfillment paths, and evidence."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

import pytest
from app.domain.budget_enums import (
    CommitmentFulfillmentMode,
    FinancialEventOrigin,
    FinancialEventStatus,
    FinancialEventType,
    JournalEntryType,
    LedgerAccountKind,
    PaymentMethod,
    RecognitionBasis,
    SalaryCalculationMode,
    SalaryRunItemStatus,
    SalaryRunStatus,
    SubscriptionCadence,
    SubscriptionRequirementReason,
)
from app.domain.budget_models import (
    SALARY_EVIDENCE_SCHEMA_VERSION,
    FixedSalaryItemEvidence,
    ManualSalaryAmount,
    ManualSalaryItemEvidence,
    SalaryAllocationRunDraft,
    SalaryAllocationRunItemDraft,
    SalaryPlanTemplateDraft,
    SalaryPlanTemplateItemDraft,
    SalarySubscriptionEvidence,
    SubscriptionRequirementItem,
    SubscriptionSalaryItemEvidence,
)
from app.domain.budget_rules import (
    SALARY_EVENT_ALREADY_USED,
    SALARY_EVENT_COUNTER_NOT_INCOME,
    SALARY_EVENT_NO_ENTRY,
    SALARY_EVENT_NO_MATCHING_DEPOSIT,
    SALARY_EVENT_NOT_POSTED,
    SALARY_EVENT_WRONG_CURRENCY,
    SALARY_EVENT_WRONG_ENTRY_TYPE,
    SALARY_EVENT_WRONG_TYPE,
    SALARY_RECEIPT_DIFFERENT_MONEY,
    SALARY_RECEIPT_NO_ENTRY,
    SALARY_RECEIPT_NO_INCOME_ACCOUNT,
    SALARY_RECEIPT_NOT_A_SALARY_EVENT,
    SALARY_RECEIPT_UNLINKED_ENTRY,
    SALARY_RECEIPT_WRONG_EFFECTIVE_AT,
    SALARY_RECEIPT_WRONG_ENTRY_TYPE,
    SALARY_RECEIPT_WRONG_POSTINGS,
    SalaryPostingView,
    approved_item_status,
    derive_run_status,
    deserialize_item_evidence,
    deserialize_subscription_evidence,
    ensure_run_item_transition,
    ensure_run_transition,
    salary_event_problem,
    salary_fulfillment_mode_for,
    salary_planned_total,
    salary_receipt_replay_problem,
    salary_regeneration_fingerprint,
    salary_remainder_minor,
    serialize_item_evidence,
    serialize_subscription_evidence,
)
from app.domain.errors import (
    SalaryAllocationError,
    SalaryEvidenceError,
)

_UTC_MONTH = date(2026, 8, 1)
_RECEIVED_AT = datetime(2026, 8, 5, 1, 0, tzinfo=UTC)


def _requirement_item(
    *,
    subscription_id: int = 1,
    wallet_id: int = 7,
    contribution_minor: int = 39000,
    reason: SubscriptionRequirementReason = (
        SubscriptionRequirementReason.MONTHLY_PRICE
    ),
    cycle_position: int | None = 0,
    price_period_id: int | None = 11,
    cycle_price_minor: int | None = 39000,
) -> SubscriptionRequirementItem:
    return SubscriptionRequirementItem(
        subscription_id=subscription_id,
        name="測試服務",
        currency="TWD",
        year=2026,
        month=8,
        funding_wallet_id=wallet_id,
        funding_wallet_name="測試訂閱錢包",
        budget_category_id=3,
        budget_category_name="測試分類",
        cadence=SubscriptionCadence.MONTHLY,
        cadence_months=1,
        cycle_position=cycle_position,
        price_period_id=price_period_id,
        cycle_price_minor=cycle_price_minor,
        contribution_minor=contribution_minor,
        is_active=True,
        active_from=_UTC_MONTH,
        active_to=None,
        effective_from=_UTC_MONTH,
        effective_to=None,
        reason=reason,
    )


def _posting(
    *,
    kind: LedgerAccountKind,
    amount_minor: int,
    account_id: int | None = 1,
    wallet_id: int | None = 2,
    currency: str = "TWD",
    ledger_account_id: int = 10,
) -> SalaryPostingView:
    return SalaryPostingView(
        ledger_account_id=ledger_account_id,
        ledger_kind=kind,
        financial_account_id=account_id,
        wallet_id=wallet_id,
        amount_minor=amount_minor,
        currency=currency,
    )


def _salary_postings(amount_minor: int = 90_000) -> tuple[SalaryPostingView, ...]:
    return (
        _posting(kind=LedgerAccountKind.ASSET, amount_minor=amount_minor),
        _posting(
            kind=LedgerAccountKind.INCOME,
            amount_minor=-amount_minor,
            account_id=None,
            wallet_id=None,
            ledger_account_id=99,
        ),
    )


def _problem(**overrides: Any) -> str | None:
    arguments: dict[str, Any] = {
        "event_type": FinancialEventType.DEPOSIT,
        "status": FinancialEventStatus.POSTED,
        "currency": "TWD",
        "amount_minor": 90_000,
        "expected_currency": "TWD",
        "source_account_id": 1,
        "source_wallet_id": 2,
        "entry_type": JournalEntryType.SALARY_RECEIPT,
        "postings": _salary_postings(),
        "already_used": False,
    }
    arguments.update(overrides)
    return salary_event_problem(**arguments)


@pytest.mark.parametrize("value", [True, 1.5, "1000", None])
def test_a_manual_amount_rejects_anything_that_is_not_integer_minor_units(
    value: object,
) -> None:
    with pytest.raises(TypeError):
        ManualSalaryAmount(template_item_id=1, amount_minor=value)  # type: ignore[arg-type]


def test_a_manual_amount_may_be_zero_but_never_negative() -> None:
    assert ManualSalaryAmount(template_item_id=1, amount_minor=0).amount_minor == 0
    with pytest.raises(ValueError, match="must not be negative"):
        ManualSalaryAmount(template_item_id=1, amount_minor=-1)


@pytest.mark.parametrize("value", [True, 2.5, "500"])
def test_a_fixed_template_item_rejects_inexact_money(value: object) -> None:
    with pytest.raises(TypeError):
        SalaryPlanTemplateItemDraft(
            template_id=1,
            name="房租",
            destination_wallet_id=2,
            calculation_mode=SalaryCalculationMode.FIXED,
            fixed_amount_minor=value,  # type: ignore[arg-type]
        )


def test_a_fixed_item_needs_its_amount_and_the_others_must_not_carry_one() -> None:
    with pytest.raises(ValueError, match="needs its amount"):
        SalaryPlanTemplateItemDraft(
            template_id=1,
            name="房租",
            destination_wallet_id=2,
            calculation_mode=SalaryCalculationMode.FIXED,
        )
    for mode in (
        SalaryCalculationMode.MANUAL,
        SalaryCalculationMode.SUBSCRIPTION_REQUIREMENT,
    ):
        with pytest.raises(ValueError, match="only a fixed salary item"):
            SalaryPlanTemplateItemDraft(
                template_id=1,
                name="訂閱",
                destination_wallet_id=2,
                calculation_mode=mode,
                fixed_amount_minor=1,
            )
    zero = SalaryPlanTemplateItemDraft(
        template_id=1,
        name="零元",
        destination_wallet_id=2,
        calculation_mode=SalaryCalculationMode.FIXED,
        fixed_amount_minor=0,
    )
    assert zero.fixed_amount_minor == 0


def test_a_template_normalizes_its_currency_and_rejects_an_unusable_one() -> None:
    assert (
        SalaryPlanTemplateDraft(
            name="月薪", currency=" twd ", source_account_id=1
        ).currency
        == "TWD"
    )
    with pytest.raises(ValueError, match="three-letter"):
        SalaryPlanTemplateDraft(name="月薪", currency="NTD$", source_account_id=1)


def test_planned_and_remainder_are_exact_and_never_lose_a_minor_unit() -> None:
    planned = salary_planned_total((33_333, 33_333, 33_334))
    assert planned == 100_000
    assert (
        salary_remainder_minor(
            salary_amount_minor=100_001, planned_amount_minor=planned
        )
        == 1
    )
    assert (
        salary_remainder_minor(
            salary_amount_minor=100_000, planned_amount_minor=planned
        )
        == 0
    )


def test_a_plan_larger_than_the_salary_is_refused_rather_than_clipped() -> None:
    with pytest.raises(SalaryAllocationError, match="larger than this salary"):
        salary_remainder_minor(
            salary_amount_minor=100_000, planned_amount_minor=100_001
        )


def test_a_run_draft_refuses_a_plan_that_does_not_add_up() -> None:
    with pytest.raises(ValueError, match="must equal the salary"):
        SalaryAllocationRunDraft(
            template_id=1,
            template_name_snapshot="月薪",
            template_version=1,
            budget_period_id=1,
            salary_event_id=1,
            source_account_id=1,
            source_wallet_id=2,
            currency="TWD",
            generation=1,
            salary_amount_minor=100_000,
            planned_amount_minor=60_000,
            remainder_minor=30_000,
            subscription_evidence_schema_version=1,
            subscription_evidence_json="{}",
            generated_at=_aware(),
        )


def _aware() -> Any:
    from datetime import UTC, datetime

    return datetime(2026, 8, 5, 1, 0, tzinfo=UTC)


def test_a_run_item_draft_refuses_a_negative_or_float_amount() -> None:
    for amount in (-1, 1.5, True):
        with pytest.raises((TypeError, ValueError)):
            SalaryAllocationRunItemDraft(
                run_id=1,
                template_item_id=1,
                template_item_version=1,
                name_snapshot="房租",
                destination_wallet_id=2,
                destination_account_id=1,
                destination_wallet_name_snapshot="房租",
                destination_account_name_snapshot="測試帳戶",
                budget_category_id=None,
                budget_category_name_snapshot=None,
                calculation_mode=SalaryCalculationMode.FIXED,
                amount_minor=amount,  # type: ignore[arg-type]
                currency="TWD",
                display_order=0,
                fulfillment_mode=CommitmentFulfillmentMode.RESERVE_ONLY,
                calculation_evidence_json="{}",
            )


@pytest.mark.parametrize(
    (
        "source_account",
        "destination_account",
        "source_bank",
        "destination_bank",
        "mode",
    ),
    [
        (1, 1, "bank_a", "bank_a", CommitmentFulfillmentMode.RESERVE_ONLY),
        # Two accounts at one bank are still two real accounts: the display
        # name is identical and the mode must not be reserve_only.
        (1, 2, "bank_a", "bank_a", CommitmentFulfillmentMode.ACCOUNT_TRANSFER),
        (1, 2, "bank_a", "bank_b", CommitmentFulfillmentMode.CROSS_BANK_TRANSFER),
        # The same account under two different display names is still one
        # account, so identity decides and the names do not.
        (5, 5, "bank_a", "bank_b", CommitmentFulfillmentMode.RESERVE_ONLY),
    ],
)
def test_the_fulfillment_path_is_decided_by_account_identity_not_bank_name(
    source_account: int,
    destination_account: int,
    source_bank: str,
    destination_bank: str,
    mode: CommitmentFulfillmentMode,
) -> None:
    assert (
        salary_fulfillment_mode_for(
            source_account_id=source_account,
            destination_account_id=destination_account,
            source_institution_code=source_bank,
            destination_institution_code=destination_bank,
        )
        is mode
    )


@pytest.mark.parametrize(
    ("amount_minor", "mode", "expected"),
    [
        (0, CommitmentFulfillmentMode.RESERVE_ONLY, SalaryRunItemStatus.NOT_REQUIRED),
        (
            0,
            CommitmentFulfillmentMode.CROSS_BANK_TRANSFER,
            SalaryRunItemStatus.NOT_REQUIRED,
        ),
        (1, CommitmentFulfillmentMode.RESERVE_ONLY, SalaryRunItemStatus.ALLOCATED),
        (
            1,
            CommitmentFulfillmentMode.ACCOUNT_TRANSFER,
            SalaryRunItemStatus.WAITING_TRANSFER,
        ),
        (
            1,
            CommitmentFulfillmentMode.CROSS_BANK_TRANSFER,
            SalaryRunItemStatus.WAITING_TRANSFER,
        ),
    ],
)
def test_approval_decides_each_line_from_its_amount_and_its_path(
    amount_minor: int, mode: CommitmentFulfillmentMode, expected: SalaryRunItemStatus
) -> None:
    assert approved_item_status(amount_minor=amount_minor, mode=mode) is expected


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ((), SalaryRunStatus.FUNDED),
        ((SalaryRunItemStatus.NOT_REQUIRED,), SalaryRunStatus.FUNDED),
        ((SalaryRunItemStatus.ALLOCATED,), SalaryRunStatus.FUNDED),
        ((SalaryRunItemStatus.TRANSFERRED,), SalaryRunStatus.FUNDED),
        (
            (SalaryRunItemStatus.ALLOCATED, SalaryRunItemStatus.NOT_REQUIRED),
            SalaryRunStatus.FUNDED,
        ),
        ((SalaryRunItemStatus.WAITING_TRANSFER,), SalaryRunStatus.APPROVED),
        (
            (SalaryRunItemStatus.WAITING_TRANSFER, SalaryRunItemStatus.NOT_REQUIRED),
            SalaryRunStatus.APPROVED,
        ),
        (
            (SalaryRunItemStatus.WAITING_TRANSFER, SalaryRunItemStatus.ALLOCATED),
            SalaryRunStatus.PARTIALLY_FUNDED,
        ),
        (
            (SalaryRunItemStatus.WAITING_TRANSFER, SalaryRunItemStatus.TRANSFERRED),
            SalaryRunStatus.PARTIALLY_FUNDED,
        ),
    ],
)
def test_a_runs_status_is_derived_from_what_its_lines_reached(
    statuses: tuple[SalaryRunItemStatus, ...], expected: SalaryRunStatus
) -> None:
    assert derive_run_status(statuses) is expected


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (SalaryRunStatus.APPROVED, SalaryRunStatus.DRAFT),
        (SalaryRunStatus.APPROVED, SalaryRunStatus.CANCELLED),
        (SalaryRunStatus.PARTIALLY_FUNDED, SalaryRunStatus.APPROVED),
        (SalaryRunStatus.FUNDED, SalaryRunStatus.PARTIALLY_FUNDED),
        (SalaryRunStatus.CANCELLED, SalaryRunStatus.DRAFT),
        (SalaryRunStatus.CANCELLED, SalaryRunStatus.APPROVED),
    ],
)
def test_an_invalid_run_transition_is_refused(
    current: SalaryRunStatus, target: SalaryRunStatus
) -> None:
    with pytest.raises(SalaryAllocationError, match="cannot become"):
        ensure_run_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (SalaryRunItemStatus.TRANSFERRED, SalaryRunItemStatus.WAITING_TRANSFER),
        (SalaryRunItemStatus.ALLOCATED, SalaryRunItemStatus.TRANSFERRED),
        (SalaryRunItemStatus.NOT_REQUIRED, SalaryRunItemStatus.ALLOCATED),
        (SalaryRunItemStatus.DRAFT, SalaryRunItemStatus.TRANSFERRED),
    ],
)
def test_an_invalid_run_item_transition_is_refused(
    current: SalaryRunItemStatus, target: SalaryRunItemStatus
) -> None:
    with pytest.raises(SalaryAllocationError, match="cannot become"):
        ensure_run_item_transition(current, target)


def test_valid_transitions_are_accepted() -> None:
    ensure_run_transition(SalaryRunStatus.DRAFT, SalaryRunStatus.APPROVED)
    ensure_run_transition(SalaryRunStatus.DRAFT, SalaryRunStatus.CANCELLED)
    ensure_run_transition(SalaryRunStatus.APPROVED, SalaryRunStatus.FUNDED)
    ensure_run_item_transition(
        SalaryRunItemStatus.WAITING_TRANSFER, SalaryRunItemStatus.TRANSFERRED
    )


def test_subscription_evidence_round_trips_and_keeps_every_amount_an_integer() -> None:
    evidence = SalarySubscriptionEvidence(
        schema_version=SALARY_EVIDENCE_SCHEMA_VERSION,
        year=2026,
        month=8,
        currency="TWD",
        items=(
            _requirement_item(subscription_id=2, contribution_minor=39000),
            _requirement_item(
                subscription_id=1,
                contribution_minor=0,
                reason=SubscriptionRequirementReason.NO_PRICE_FOR_MONTH,
                cycle_position=None,
                price_period_id=None,
                cycle_price_minor=None,
            ),
        ),
    )

    document = serialize_subscription_evidence(evidence)
    restored = deserialize_subscription_evidence(document)

    payload = json.loads(document)
    assert [item["subscription_id"] for item in payload["items"]] == [1, 2]
    assert document == serialize_subscription_evidence(restored)
    assert restored == SalarySubscriptionEvidence(
        schema_version=evidence.schema_version,
        year=evidence.year,
        month=evidence.month,
        currency=evidence.currency,
        items=tuple(sorted(evidence.items, key=lambda item: item.subscription_id)),
    )
    for item in restored.items:
        assert isinstance(item.contribution_minor, int)
        assert not isinstance(item.contribution_minor, bool)
    # A zero contribution keeps its reason instead of disappearing.
    assert restored.items[0].contribution_minor == 0
    assert restored.items[0].reason is SubscriptionRequirementReason.NO_PRICE_FOR_MONTH
    assert restored.total_minor == 39000
    assert restored.contributions_for_wallet(7) == 39000
    assert restored.contributions_for_wallet(8) == 0


def test_a_subscription_evidence_snapshot_refuses_two_currencies() -> None:
    with pytest.raises(ValueError, match="one currency"):
        SalarySubscriptionEvidence(
            schema_version=SALARY_EVIDENCE_SCHEMA_VERSION,
            year=2026,
            month=8,
            currency="TWD",
            items=(
                _requirement_item(),
                SubscriptionRequirementItem(
                    **{
                        **_requirement_item(subscription_id=2).__dict__,
                        "currency": "JPY",
                    }
                ),
            ),
        )


@pytest.mark.parametrize(
    "document",
    [
        "not json",
        "[]",
        json.dumps({"schema_version": 2, "year": 2026, "month": 8, "currency": "TWD"}),
        json.dumps({"schema_version": 1, "year": 2026, "month": 8, "currency": "TWD"}),
    ],
)
def test_a_malformed_subscription_snapshot_is_refused(document: str) -> None:
    with pytest.raises(SalaryEvidenceError):
        deserialize_subscription_evidence(document)


def test_a_snapshot_whose_amount_is_a_float_or_a_flag_is_refused() -> None:
    evidence = SalarySubscriptionEvidence(
        schema_version=SALARY_EVIDENCE_SCHEMA_VERSION,
        year=2026,
        month=8,
        currency="TWD",
        items=(_requirement_item(),),
    )
    payload = json.loads(serialize_subscription_evidence(evidence))
    for bad in (390.0, True, "39000"):
        payload["items"][0]["contribution_minor"] = bad
        with pytest.raises(SalaryEvidenceError, match="must be an integer"):
            deserialize_subscription_evidence(json.dumps(payload))


@pytest.mark.parametrize(
    "evidence",
    [
        FixedSalaryItemEvidence(
            schema_version=SALARY_EVIDENCE_SCHEMA_VERSION,
            template_item_id=4,
            template_item_version=2,
            fixed_amount_minor=25_000,
        ),
        ManualSalaryItemEvidence(
            schema_version=SALARY_EVIDENCE_SCHEMA_VERSION,
            template_item_id=5,
            template_item_version=1,
            entered_amount_minor=0,
        ),
        SubscriptionSalaryItemEvidence(
            schema_version=SALARY_EVIDENCE_SCHEMA_VERSION,
            template_item_id=6,
            template_item_version=3,
            year=2026,
            month=8,
            currency="TWD",
            destination_wallet_id=7,
            subscription_ids=(2, 1),
            contribution_total_minor=39_000,
        ),
    ],
)
def test_item_evidence_round_trips_through_its_canonical_document(
    evidence: object,
) -> None:
    document = serialize_item_evidence(evidence)  # type: ignore[arg-type]
    restored = deserialize_item_evidence(document)

    assert serialize_item_evidence(restored) == document
    payload = json.loads(document)
    assert isinstance(payload["schema_version"], int)
    if isinstance(evidence, SubscriptionSalaryItemEvidence):
        assert payload["subscription_ids"] == [1, 2]
        assert isinstance(restored, SubscriptionSalaryItemEvidence)
        assert restored.contribution_total_minor == 39_000


@pytest.mark.parametrize(
    "payload",
    [
        {
            "schema_version": 1,
            "kind": "unknown",
            "template_item_id": 1,
            "template_item_version": 1,
        },
        {
            "schema_version": 9,
            "kind": "fixed",
            "template_item_id": 1,
            "template_item_version": 1,
            "fixed_amount_minor": 1,
        },
        {
            "schema_version": 1,
            "kind": "fixed",
            "template_item_id": 1,
            "template_item_version": 1,
            "fixed_amount_minor": 1.5,
        },
        {
            "schema_version": 1,
            "kind": "subscription_requirement",
            "template_item_id": 1,
            "template_item_version": 1,
            "year": 2026,
            "month": 8,
            "currency": "TWD",
            "destination_wallet_id": 1,
            "subscription_ids": [True],
            "contribution_total_minor": 1,
        },
    ],
)
def test_malformed_item_evidence_is_refused(payload: dict[str, Any]) -> None:
    with pytest.raises(SalaryEvidenceError):
        deserialize_item_evidence(json.dumps(payload))


def test_a_correctly_shaped_salary_deposit_is_accepted() -> None:
    assert _problem() is None


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"already_used": True}, SALARY_EVENT_ALREADY_USED),
        ({"status": FinancialEventStatus.READY}, SALARY_EVENT_NOT_POSTED),
        ({"event_type": FinancialEventType.TRANSFER}, SALARY_EVENT_WRONG_TYPE),
        ({"currency": "JPY"}, SALARY_EVENT_WRONG_CURRENCY),
        ({"entry_type": None, "postings": ()}, SALARY_EVENT_NO_ENTRY),
        (
            {"entry_type": JournalEntryType.OPENING_BALANCE},
            SALARY_EVENT_WRONG_ENTRY_TYPE,
        ),
        (
            {"entry_type": JournalEntryType.INTERNAL_TRANSFER},
            SALARY_EVENT_WRONG_ENTRY_TYPE,
        ),
        ({"entry_type": JournalEntryType.ADJUSTMENT}, SALARY_EVENT_WRONG_ENTRY_TYPE),
    ],
)
def test_an_event_that_is_not_a_salary_names_the_reason(
    overrides: dict[str, Any], expected: str
) -> None:
    assert _problem(**overrides) == expected


def test_a_deposit_into_another_account_or_wallet_is_not_this_salary() -> None:
    assert (
        _problem(
            postings=(
                _posting(
                    kind=LedgerAccountKind.ASSET, amount_minor=90_000, account_id=9
                ),
                _posting(
                    kind=LedgerAccountKind.INCOME,
                    amount_minor=-90_000,
                    account_id=None,
                    wallet_id=None,
                ),
            )
        )
        == SALARY_EVENT_NO_MATCHING_DEPOSIT
    )
    assert (
        _problem(
            postings=(
                _posting(
                    kind=LedgerAccountKind.ASSET, amount_minor=90_000, wallet_id=8
                ),
                _posting(
                    kind=LedgerAccountKind.INCOME,
                    amount_minor=-90_000,
                    account_id=None,
                    wallet_id=None,
                ),
            )
        )
        == SALARY_EVENT_NO_MATCHING_DEPOSIT
    )


def test_a_partial_deposit_or_a_split_across_two_accounts_is_refused() -> None:
    assert (
        _problem(
            postings=(
                _posting(kind=LedgerAccountKind.ASSET, amount_minor=50_000),
                _posting(
                    kind=LedgerAccountKind.INCOME,
                    amount_minor=-50_000,
                    account_id=None,
                    wallet_id=None,
                ),
            )
        )
        == SALARY_EVENT_NO_MATCHING_DEPOSIT
    )
    assert (
        _problem(
            postings=(
                _posting(kind=LedgerAccountKind.ASSET, amount_minor=90_000),
                _posting(
                    kind=LedgerAccountKind.ASSET, amount_minor=10_000, account_id=3
                ),
                _posting(
                    kind=LedgerAccountKind.INCOME,
                    amount_minor=-100_000,
                    account_id=None,
                    wallet_id=None,
                ),
            )
        )
        == SALARY_EVENT_NO_MATCHING_DEPOSIT
    )


def test_an_opening_equity_credit_is_never_mistaken_for_income() -> None:
    assert (
        _problem(
            entry_type=JournalEntryType.SALARY_RECEIPT,
            postings=(
                _posting(kind=LedgerAccountKind.ASSET, amount_minor=90_000),
                _posting(
                    kind=LedgerAccountKind.EQUITY,
                    amount_minor=-90_000,
                    account_id=None,
                    wallet_id=None,
                ),
            ),
        )
        == SALARY_EVENT_COUNTER_NOT_INCOME
    )


def test_an_income_side_that_does_not_match_the_deposit_is_refused() -> None:
    assert (
        _problem(
            postings=(
                _posting(kind=LedgerAccountKind.ASSET, amount_minor=90_000),
                _posting(
                    kind=LedgerAccountKind.INCOME,
                    amount_minor=-80_000,
                    account_id=None,
                    wallet_id=None,
                ),
                _posting(
                    kind=LedgerAccountKind.INCOME,
                    amount_minor=-5_000,
                    account_id=None,
                    wallet_id=None,
                ),
            )
        )
        == SALARY_EVENT_COUNTER_NOT_INCOME
    )


def test_a_posting_in_another_currency_is_refused() -> None:
    assert (
        _problem(
            postings=(
                _posting(kind=LedgerAccountKind.ASSET, amount_minor=90_000),
                _posting(
                    kind=LedgerAccountKind.INCOME,
                    amount_minor=-90_000,
                    account_id=None,
                    wallet_id=None,
                    currency="JPY",
                ),
            )
        )
        == SALARY_EVENT_WRONG_CURRENCY
    )


# --- Salary receipt replay identity ----------------------------------------


def _receipt_postings(
    *,
    amount_minor: int = 90_000,
    source_ledger: int = 10,
    income_ledger: int = 99,
    wallet_id: int | None = 2,
    income_wallet: int | None = None,
    category: int | None = None,
    income_category: int | None = None,
    currency: str = "TWD",
) -> tuple[SalaryPostingView, ...]:
    return (
        SalaryPostingView(
            ledger_account_id=source_ledger,
            ledger_kind=LedgerAccountKind.ASSET,
            financial_account_id=1,
            wallet_id=wallet_id,
            amount_minor=amount_minor,
            currency=currency,
            budget_category_id=category,
        ),
        SalaryPostingView(
            ledger_account_id=income_ledger,
            ledger_kind=LedgerAccountKind.INCOME,
            financial_account_id=None,
            wallet_id=income_wallet,
            amount_minor=-amount_minor,
            currency=currency,
            budget_category_id=income_category,
        ),
    )


def _replay(**overrides: Any) -> str | None:
    arguments: dict[str, Any] = {
        "origin": FinancialEventOrigin.MANUAL,
        "event_type": FinancialEventType.DEPOSIT,
        "payment_method": PaymentMethod.BANK_ACCOUNT,
        "recognition_basis": RecognitionBasis.OBSERVED_MANUAL,
        "status": FinancialEventStatus.POSTED,
        "currency": "TWD",
        "amount_minor": 90_000,
        "occurred_at": _RECEIVED_AT,
        "event_id": 7,
        "expected_currency": "TWD",
        "expected_amount_minor": 90_000,
        "expected_occurred_at": _RECEIVED_AT,
        "source_ledger_account_id": 10,
        "source_wallet_id": 2,
        "income_ledger_account_id": 99,
        "entry_type": JournalEntryType.SALARY_RECEIPT,
        "entry_effective_at": _RECEIVED_AT,
        "entry_financial_event_id": 7,
        "postings": _receipt_postings(),
    }
    arguments.update(overrides)
    return salary_receipt_replay_problem(**arguments)


def test_a_genuine_salary_receipt_replays() -> None:
    assert _replay() is None
    # The two postings may arrive in either order.
    assert _replay(postings=tuple(reversed(_receipt_postings()))) is None


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"amount_minor": 90_001}, SALARY_RECEIPT_DIFFERENT_MONEY),
        ({"currency": "JPY"}, SALARY_RECEIPT_DIFFERENT_MONEY),
        (
            {"occurred_at": datetime(2026, 8, 5, 2, 0, tzinfo=UTC)},
            SALARY_RECEIPT_DIFFERENT_MONEY,
        ),
        (
            {"origin": FinancialEventOrigin.GMAIL_TRANSACTION},
            SALARY_RECEIPT_NOT_A_SALARY_EVENT,
        ),
        (
            {"event_type": FinancialEventType.PURCHASE},
            SALARY_RECEIPT_NOT_A_SALARY_EVENT,
        ),
        (
            {"payment_method": PaymentMethod.CREDIT_CARD},
            SALARY_RECEIPT_NOT_A_SALARY_EVENT,
        ),
        (
            {"recognition_basis": RecognitionBasis.GMAIL_NOTIFICATION},
            SALARY_RECEIPT_NOT_A_SALARY_EVENT,
        ),
        (
            {"status": FinancialEventStatus.READY},
            SALARY_RECEIPT_NOT_A_SALARY_EVENT,
        ),
        ({"entry_type": None, "postings": ()}, SALARY_RECEIPT_NO_ENTRY),
        (
            {"entry_type": JournalEntryType.ADJUSTMENT},
            SALARY_RECEIPT_WRONG_ENTRY_TYPE,
        ),
        ({"entry_financial_event_id": 8}, SALARY_RECEIPT_UNLINKED_ENTRY),
        (
            {"entry_effective_at": datetime(2026, 9, 5, 1, 0, tzinfo=UTC)},
            SALARY_RECEIPT_WRONG_EFFECTIVE_AT,
        ),
        ({"income_ledger_account_id": None}, SALARY_RECEIPT_NO_INCOME_ACCOUNT),
    ],
)
def test_a_stored_receipt_that_is_something_else_names_the_reason(
    overrides: dict[str, Any], expected: str
) -> None:
    assert _replay(**overrides) == expected


@pytest.mark.parametrize(
    "postings",
    [
        (),
        _receipt_postings()[:1],
        (*_receipt_postings(), _receipt_postings()[0]),
        _receipt_postings(source_ledger=11),
        _receipt_postings(income_ledger=98),
        _receipt_postings(wallet_id=3),
        _receipt_postings(wallet_id=None),
        _receipt_postings(income_wallet=2),
        _receipt_postings(category=4),
        _receipt_postings(income_category=4),
        _receipt_postings(amount_minor=80_000),
        _receipt_postings(currency="JPY"),
    ],
)
def test_a_receipt_whose_postings_differ_at_all_is_refused(
    postings: tuple[SalaryPostingView, ...],
) -> None:
    assert _replay(postings=postings) == SALARY_RECEIPT_WRONG_POSTINGS


def test_a_regeneration_fingerprint_is_canonical_and_order_independent() -> None:
    first = salary_regeneration_fingerprint(
        previous_run_id=3, expected_version=2, manual_amounts=((9, 100), (4, 250))
    )
    second = salary_regeneration_fingerprint(
        previous_run_id=3, expected_version=2, manual_amounts=((4, 250), (9, 100))
    )
    assert first == second
    assert json.loads(first)["manual"] == [[4, 250], [9, 100]]
    assert first != salary_regeneration_fingerprint(
        previous_run_id=3, expected_version=2, manual_amounts=((9, 101), (4, 250))
    )
    assert first != salary_regeneration_fingerprint(
        previous_run_id=3, expected_version=3, manual_amounts=((9, 100), (4, 250))
    )
    assert first != salary_regeneration_fingerprint(
        previous_run_id=4, expected_version=2, manual_amounts=((9, 100), (4, 250))
    )


@pytest.mark.parametrize("amount", [True, 1.5, "100"])
def test_a_fingerprint_refuses_an_amount_that_is_not_integer_minor_units(
    amount: object,
) -> None:
    with pytest.raises(TypeError):
        salary_regeneration_fingerprint(
            previous_run_id=1,
            expected_version=1,
            manual_amounts=((1, amount),),  # type: ignore[arg-type]
        )


def test_a_run_draft_requires_provenance_and_fingerprint_together() -> None:
    for provenance, fingerprint in ((3, None), (None, "{}")):
        with pytest.raises(ValueError, match="records both the draft"):
            SalaryAllocationRunDraft(
                template_id=1,
                template_name_snapshot="月薪",
                template_version=1,
                budget_period_id=1,
                salary_event_id=1,
                source_account_id=1,
                source_wallet_id=2,
                currency="TWD",
                generation=2,
                salary_amount_minor=100_000,
                planned_amount_minor=0,
                remainder_minor=100_000,
                subscription_evidence_schema_version=1,
                subscription_evidence_json="{}",
                generated_at=_aware(),
                regenerated_from_run_id=provenance,
                request_fingerprint=fingerprint,
            )

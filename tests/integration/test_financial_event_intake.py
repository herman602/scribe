"""Financial event intake, card mapping, and parser-rerun acceptance tests."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from app.application.services.cards import (
    ConfigureCardRequest,
    ManageCards,
    RegisterCardRequest,
)
from app.application.services.financial_event_intake import (
    FinancialEventIntake,
    IntakeResult,
    intake_idempotency_key,
)
from app.application.services.ledger import (
    LedgerSetupService,
    OpenFinancialAccountRequest,
)
from app.application.services.manual_money import (
    UpdateFinancialEventNote,
    UpdateFinancialEventNoteRequest,
)
from app.application.services.transaction_confirmation import ConfirmTransactions
from app.application.services.transaction_review import (
    ReviewTransaction,
    ReviewTransactionRequest,
)
from app.domain.budget_enums import (
    CardConfigurationStatus,
    CardHolderRole,
    FinancialAccountType,
    FinancialEventOrigin,
    FinancialEventStatus,
    FinancialEventType,
    IntakeSkipReason,
    PaymentMethod,
    RecognitionBasis,
    SourceDiscrepancy,
    SourceLinkReason,
    SourceLinkStatus,
)
from app.domain.budget_models import FinancialEventDraft
from app.domain.enums import SenderMatchType, TransactionType
from app.domain.errors import (
    CardConfigurationError,
    ConcurrentUpdateError,
    IdempotencyConflictError,
)
from app.domain.models import (
    EmailSnapshot,
    SenderRuleDraft,
    TransactionDraft,
    TransactionReviewDraft,
)
from app.domain.ports.repositories import UnitOfWork
from app.infrastructure.database.engine import Database
from app.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork

OCCURRED_AT = datetime(2026, 8, 7, 13, 30, tzinfo=UTC)
REVIEWED_AT = datetime(2026, 8, 8, 2, 0, tzinfo=UTC)

type UnitOfWorkFactory = Callable[[], UnitOfWork]


@pytest.fixture
def unit_of_work_factory(migrated_database: Database) -> UnitOfWorkFactory:
    """Return a unit-of-work factory bound to the migrated test database."""

    def factory() -> UnitOfWork:
        return SqlAlchemyUnitOfWork(migrated_database.session_factory)

    return factory


@pytest.fixture
def intake(unit_of_work_factory: UnitOfWorkFactory) -> FinancialEventIntake:
    """Return intake with a frozen review clock."""
    return FinancialEventIntake(unit_of_work_factory, clock=lambda: REVIEWED_AT)


@pytest.fixture
def cards(unit_of_work_factory: UnitOfWorkFactory) -> ManageCards:
    """Return the local card configuration service."""
    return ManageCards(unit_of_work_factory)


def _seed_transaction(
    factory: UnitOfWorkFactory,
    *,
    gmail_message_id: str = "gmail-b2-001",
    with_sender_rule: bool = True,
    card_last_four: str | None = "4321",
    transaction_type: TransactionType = TransactionType.PURCHASE,
    amount_minor: int | None = 128000,
    currency: str | None = "TWD",
    transaction_at: datetime | None = OCCURRED_AT,
    merchant: str | None = "測試咖啡",
) -> tuple[int, int, int]:
    """Return one (account_id, email_id, transaction_id) fixture triple."""
    with factory() as unit:
        account = unit.accounts.get_or_create("user@example.test")
        rule_id = None
        if with_sender_rule:
            rule = unit.sender_rules.upsert(
                SenderRuleDraft(
                    gmail_account_id=account.id,
                    institution_key="test_bank",
                    institution_name="測試銀行",
                    match_type=SenderMatchType.EXACT_ADDRESS,
                    match_value="notice@example-bank.test",
                )
            )
            rule_id = rule.id
        outcome = unit.emails.upsert_from_gmail(
            EmailSnapshot(
                gmail_account_id=account.id,
                gmail_message_id=gmail_message_id,
                gmail_thread_id="thread-shared-by-many-messages",
                gmail_history_id="500",
                sender="notice@example-bank.test",
                recipients=("user@example.test",),
                subject="消費通知",
                gmail_internal_date_ms=1_754_000_000_000,
                matched_sender_rule_id=rule_id,
            )
        )
        transaction = unit.transactions.add(
            TransactionDraft(
                email_id=outcome.email.id,
                source_key="row-1",
                transaction_type=transaction_type,
                confidence=Decimal("0.95"),
                parser_name="tests.fake",
                parser_version="1",
                merchant=merchant,
                amount_minor=amount_minor,
                currency=currency,
                card_last_four=card_last_four,
                transaction_at=transaction_at,
            )
        )
        unit.commit()
        return account.id, outcome.email.id, transaction.id


def _confirm(factory: UnitOfWorkFactory, transaction_id: int) -> None:
    with factory() as unit:
        unit.transactions.confirm_pending(frozenset({transaction_id}))
        unit.commit()


def _register_card(
    cards: ManageCards, account_id: int, *, last_four: str = "4321"
) -> int:
    card = cards.register(
        RegisterCardRequest(
            gmail_account_id=account_id,
            issuer_code="test_bank",
            display_name="測試卡",
            last_four=last_four,
        )
    )
    return card.id


def _stage(
    factory: UnitOfWorkFactory,
    intake: FinancialEventIntake,
    transaction_ids: tuple[int, ...],
) -> IntakeResult:
    with factory() as unit:
        result = intake.stage(unit, transaction_ids)
        unit.commit()
    return result


def test_one_transaction_creates_at_most_one_confirmed_link(
    unit_of_work_factory: UnitOfWorkFactory,
    intake: FinancialEventIntake,
    cards: ManageCards,
) -> None:
    account_id, _, transaction_id = _seed_transaction(unit_of_work_factory)
    card_id = _register_card(cards, account_id)
    _confirm(unit_of_work_factory, transaction_id)

    first = _stage(unit_of_work_factory, intake, (transaction_id,))
    second = _stage(unit_of_work_factory, intake, (transaction_id,))

    assert first.staged_count == 1
    assert second.staged_count == 0
    assert second.skipped[0].reason is IntakeSkipReason.ALREADY_STAGED
    with unit_of_work_factory() as unit:
        event = unit.financial_events.get_by_idempotency_key(
            intake_idempotency_key(transaction_id)
        )
        assert event is not None
        assert event.origin is FinancialEventOrigin.GMAIL_TRANSACTION
        assert event.recognition_basis is RecognitionBasis.GMAIL_NOTIFICATION
        assert event.status is FinancialEventStatus.NEEDS_ALLOCATION
        assert event.event_type is FinancialEventType.PURCHASE
        assert event.payment_method is PaymentMethod.CREDIT_CARD
        assert event.amount_minor == 128000
        assert event.currency == "TWD"
        assert event.card_id == card_id
        links = unit.source_links.list_for_event(event.id)
        assert len(links) == 1
        assert links[0].status is SourceLinkStatus.CONFIRMED
        assert links[0].reasons == (SourceLinkReason.DIRECT_GMAIL_INTAKE,)
        assert links[0].reviewed_at == REVIEWED_AT
        confirmed = unit.source_links.get_confirmed_for_transaction(transaction_id)
        assert confirmed is not None
        assert confirmed.id == links[0].id


def test_thread_identity_never_participates_in_event_uniqueness(
    unit_of_work_factory: UnitOfWorkFactory,
    intake: FinancialEventIntake,
    cards: ManageCards,
) -> None:
    account_id, _, first_id = _seed_transaction(unit_of_work_factory)
    _register_card(cards, account_id)
    _, _, second_id = _seed_transaction(
        unit_of_work_factory, gmail_message_id="gmail-b2-002"
    )
    _confirm(unit_of_work_factory, first_id)
    _confirm(unit_of_work_factory, second_id)

    result = _stage(unit_of_work_factory, intake, (first_id, second_id))

    assert result.staged_count == 2
    with unit_of_work_factory() as unit:
        keys = {
            unit.financial_events.get_by_idempotency_key(
                intake_idempotency_key(item)
            ).idempotency_key  # type: ignore[union-attr]
            for item in (first_id, second_id)
        }
    assert keys == {
        intake_idempotency_key(first_id),
        intake_idempotency_key(second_id),
    }


def test_unconfirmed_ignored_and_deleted_sources_are_never_staged(
    unit_of_work_factory: UnitOfWorkFactory, intake: FinancialEventIntake
) -> None:
    _, _, pending_id = _seed_transaction(unit_of_work_factory)
    _, ignored_email, ignored_id = _seed_transaction(
        unit_of_work_factory, gmail_message_id="gmail-b2-ignored"
    )
    account_id, _, deleted_id = _seed_transaction(
        unit_of_work_factory, gmail_message_id="gmail-b2-deleted"
    )
    with unit_of_work_factory() as unit:
        unit.transactions.apply_review(
            TransactionReviewDraft(
                transaction_id=ignored_id,
                expected_updated_at=unit.transactions.get(ignored_id).updated_at,  # type: ignore[union-attr]
                override_fields=frozenset(),
                transaction_type=TransactionType.PURCHASE,
                merchant=None,
                amount_minor=None,
                currency=None,
                transaction_at=None,
                manually_confirmed=False,
                manually_ignored=True,
                user_note=None,
            )
        )
        unit.transactions.confirm_pending(frozenset({deleted_id}))
        unit.emails.mark_source_deleted(
            account_id, "gmail-b2-deleted", datetime.now(UTC)
        )
        unit.commit()

    result = _stage(unit_of_work_factory, intake, (pending_id, ignored_id, deleted_id))

    assert result.staged_count == 0
    reasons = {item.transaction_id: item.reason for item in result.skipped}
    assert reasons[pending_id] is IntakeSkipReason.NOT_CONFIRMED
    assert reasons[ignored_id] is IntakeSkipReason.MANUALLY_IGNORED
    assert reasons[deleted_id] is IntakeSkipReason.SOURCE_DELETED
    assert ignored_email is not None


def test_undecidable_transactions_are_skipped_with_stable_reasons(
    unit_of_work_factory: UnitOfWorkFactory, intake: FinancialEventIntake
) -> None:
    cases = {
        IntakeSkipReason.UNSUPPORTED_TRANSACTION_TYPE: {
            "transaction_type": TransactionType.UNKNOWN
        },
        IntakeSkipReason.MISSING_AMOUNT: {"amount_minor": None},
        IntakeSkipReason.MISSING_CURRENCY: {"currency": None},
        IntakeSkipReason.MISSING_OCCURRED_AT: {"transaction_at": None},
    }
    identifiers: dict[IntakeSkipReason, int] = {}
    for index, (reason, overrides) in enumerate(cases.items()):
        _, _, transaction_id = _seed_transaction(
            unit_of_work_factory,
            gmail_message_id=f"gmail-b2-skip-{index}",
            **overrides,  # type: ignore[arg-type]
        )
        _confirm(unit_of_work_factory, transaction_id)
        identifiers[reason] = transaction_id

    result = _stage(unit_of_work_factory, intake, tuple(identifiers.values()))

    assert result.staged_count == 0
    observed = {item.transaction_id: item.reason for item in result.skipped}
    for reason, transaction_id in identifiers.items():
        assert observed[transaction_id] is reason
    missing = _stage(unit_of_work_factory, intake, (9_999,))
    assert missing.skipped[0].reason is IntakeSkipReason.NOT_FOUND


def test_a_missing_card_mapping_is_safe_and_user_readable(
    unit_of_work_factory: UnitOfWorkFactory,
    intake: FinancialEventIntake,
    cards: ManageCards,
) -> None:
    _, _, unconfigured_id = _seed_transaction(unit_of_work_factory)
    _confirm(unit_of_work_factory, unconfigured_id)
    account_id, _, unresolved_id = _seed_transaction(
        unit_of_work_factory,
        gmail_message_id="gmail-b2-no-rule",
        with_sender_rule=False,
    )
    _confirm(unit_of_work_factory, unresolved_id)

    result = _stage(unit_of_work_factory, intake, (unconfigured_id, unresolved_id))

    staged = {item.transaction_id: item for item in result.staged}
    assert staged[unconfigured_id].card_id is None
    assert staged[unconfigured_id].reasons == (SourceLinkReason.CARD_NOT_CONFIGURED,)
    assert staged[unresolved_id].reasons == (SourceLinkReason.CARD_UNRESOLVED,)

    _register_card(cards, account_id)
    _, _, ambiguous_id = _seed_transaction(
        unit_of_work_factory,
        gmail_message_id="gmail-b2-ambiguous",
        with_sender_rule=False,
    )
    _confirm(unit_of_work_factory, ambiguous_id)
    ambiguous = _stage(unit_of_work_factory, intake, (ambiguous_id,))
    assert ambiguous.staged[0].reasons == (SourceLinkReason.CARD_AMBIGUOUS,)
    with unit_of_work_factory() as unit:
        event = unit.financial_events.get_by_idempotency_key(
            intake_idempotency_key(unconfigured_id)
        )
        assert event is not None
        assert event.status is FinancialEventStatus.NEEDS_ALLOCATION
        links = unit.source_links.list_for_event(event.id)
        assert SourceLinkReason.CARD_NOT_CONFIGURED in links[0].reasons


def test_confirmation_and_intake_share_one_unit_of_work(
    unit_of_work_factory: UnitOfWorkFactory,
    intake: FinancialEventIntake,
    cards: ManageCards,
) -> None:
    account_id, _, transaction_id = _seed_transaction(unit_of_work_factory)
    _register_card(cards, account_id)

    result = ConfirmTransactions(unit_of_work_factory, intake=intake).execute(
        [transaction_id]
    )

    assert result.confirmed_count == 1
    assert result.intake is not None
    assert result.intake.staged_count == 1
    with unit_of_work_factory() as unit:
        assert (
            unit.financial_events.get_by_idempotency_key(
                intake_idempotency_key(transaction_id)
            )
            is not None
        )


def test_a_failed_intake_rolls_back_the_whole_confirmation(
    unit_of_work_factory: UnitOfWorkFactory,
) -> None:
    _, _, transaction_id = _seed_transaction(unit_of_work_factory)

    class _ExplodingIntake(FinancialEventIntake):
        def stage(self, unit, transaction_ids, *, dry_run=False):  # type: ignore[no-untyped-def]
            raise RuntimeError("intake failed")

    with pytest.raises(RuntimeError, match="intake failed"):
        ConfirmTransactions(
            unit_of_work_factory, intake=_ExplodingIntake(unit_of_work_factory)
        ).execute([transaction_id])

    with unit_of_work_factory() as unit:
        transaction = unit.transactions.get(transaction_id)
        assert transaction is not None
        assert not transaction.manually_confirmed
        assert (
            unit.financial_events.get_by_idempotency_key(
                intake_idempotency_key(transaction_id)
            )
            is None
        )


def test_detail_review_confirmation_stages_in_the_same_transaction(
    unit_of_work_factory: UnitOfWorkFactory,
    intake: FinancialEventIntake,
    cards: ManageCards,
) -> None:
    account_id, _, transaction_id = _seed_transaction(unit_of_work_factory)
    _register_card(cards, account_id)
    with unit_of_work_factory() as unit:
        expected = unit.transactions.get(transaction_id).updated_at  # type: ignore[union-attr]

    ReviewTransaction(unit_of_work_factory, intake=intake).execute(
        ReviewTransactionRequest(
            transaction_id=transaction_id,
            expected_updated_at=expected,
            override_fields=frozenset(),
            transaction_type=TransactionType.PURCHASE,
            merchant=None,
            amount_minor=None,
            currency=None,
            transaction_at=None,
            manually_confirmed=True,
            manually_ignored=False,
            user_note=None,
        )
    )

    with unit_of_work_factory() as unit:
        assert (
            unit.financial_events.get_by_idempotency_key(
                intake_idempotency_key(transaction_id)
            )
            is not None
        )


def test_a_reparse_refreshes_an_unposted_event_and_never_a_posted_one(
    unit_of_work_factory: UnitOfWorkFactory,
    intake: FinancialEventIntake,
    cards: ManageCards,
    migrated_database: Database,
) -> None:
    account_id, email_id, transaction_id = _seed_transaction(unit_of_work_factory)
    _register_card(cards, account_id)
    _confirm(unit_of_work_factory, transaction_id)
    _stage(unit_of_work_factory, intake, (transaction_id,))

    def _reparse(amount_minor: int, merchant: str) -> None:
        with unit_of_work_factory() as unit:
            unit.transactions.reconcile(
                email_id,
                (
                    TransactionDraft(
                        email_id=email_id,
                        source_key="row-1",
                        transaction_type=TransactionType.PURCHASE,
                        confidence=Decimal("0.95"),
                        parser_name="tests.fake",
                        parser_version="2",
                        merchant=merchant,
                        amount_minor=amount_minor,
                        currency="TWD",
                        card_last_four="4321",
                        transaction_at=OCCURRED_AT,
                    ),
                ),
            )
            intake.refresh_for_email(unit, email_id)
            unit.commit()

    _reparse(999000, "測試咖啡二號店")

    with unit_of_work_factory() as unit:
        event = unit.financial_events.get_by_idempotency_key(
            intake_idempotency_key(transaction_id)
        )
        assert event is not None
        assert event.amount_minor == 999000
        assert event.merchant == "測試咖啡二號店"
        assert event.version == 2
        link = unit.source_links.get_confirmed_for_transaction(transaction_id)
        assert link is not None
        assert link.discrepancies == ()
        posted_id = event.id

    with migrated_database.engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE financial_events SET status = 'posted' WHERE id = ?",
            (posted_id,),
        )

    _reparse(123000, "測試咖啡三號店")

    with unit_of_work_factory() as unit:
        event = unit.financial_events.get(posted_id)
        assert event is not None
        assert event.status is FinancialEventStatus.POSTED
        assert event.amount_minor == 999000
        assert event.merchant == "測試咖啡二號店"
        link = unit.source_links.get_confirmed_for_transaction(transaction_id)
        assert link is not None
        assert set(link.discrepancies) == {
            SourceDiscrepancy.AMOUNT,
            SourceDiscrepancy.MERCHANT,
        }
        assert SourceLinkReason.DIRECT_GMAIL_INTAKE in link.reasons


def test_a_source_that_stops_being_confirmed_records_a_discrepancy(
    unit_of_work_factory: UnitOfWorkFactory,
    intake: FinancialEventIntake,
    cards: ManageCards,
) -> None:
    account_id, email_id, transaction_id = _seed_transaction(unit_of_work_factory)
    _register_card(cards, account_id)
    _confirm(unit_of_work_factory, transaction_id)
    _stage(unit_of_work_factory, intake, (transaction_id,))

    with unit_of_work_factory() as unit:
        unit.transactions.apply_review(
            TransactionReviewDraft(
                transaction_id=transaction_id,
                expected_updated_at=unit.transactions.get(transaction_id).updated_at,  # type: ignore[union-attr]
                override_fields=frozenset(),
                transaction_type=TransactionType.PURCHASE,
                merchant=None,
                amount_minor=None,
                currency=None,
                transaction_at=None,
                manually_confirmed=False,
                manually_ignored=True,
                user_note=None,
            )
        )
        intake.refresh_for_email(unit, email_id)
        unit.commit()

    with unit_of_work_factory() as unit:
        link = unit.source_links.get_confirmed_for_transaction(transaction_id)
        assert link is not None
        assert SourceDiscrepancy.SOURCE_NO_LONGER_CONFIRMED in link.discrepancies


def test_supplementary_settings_backfill_is_idempotent_and_non_destructive(
    unit_of_work_factory: UnitOfWorkFactory, cards: ManageCards
) -> None:
    account_id, _, _ = _seed_transaction(unit_of_work_factory)
    with unit_of_work_factory() as unit:
        unit.supplementary_card_settings.mark(
            account_id=account_id, institution_key="test_bank", card_last_four="4321"
        )
        unit.commit()

    preview = cards.backfill_from_supplementary_settings(dry_run=True)
    first = cards.backfill_from_supplementary_settings()
    card = cards.list_cards()[0]
    edited = cards.configure(
        ConfigureCardRequest(
            card_id=card.id,
            expected_version=card.version,
            display_name="我改過的名稱",
            holder_role=CardHolderRole.PRIMARY,
            liability_account_id=None,
            settlement_account_id=None,
            default_funding_wallet_id=None,
        )
    )
    second = cards.backfill_from_supplementary_settings()

    assert preview.created_count == 1
    assert first.created_count == 1
    assert second.created_count == 0
    assert second.already_present_count == 1
    assert cards.list_cards() == (edited,)
    assert edited.display_name == "我改過的名稱"
    assert edited.holder_role is CardHolderRole.PRIMARY
    assert edited.issuer_code == "test_bank"
    assert edited.last_four == "4321"


def test_card_readiness_enforces_every_account_invariant(
    unit_of_work_factory: UnitOfWorkFactory, cards: ManageCards
) -> None:
    account_id, _, _ = _seed_transaction(unit_of_work_factory)
    setup = LedgerSetupService(unit_of_work_factory)
    liability = setup.open_account(
        OpenFinancialAccountRequest(
            institution_code="test_bank",
            display_name="測試信用卡負債",
            account_type=FinancialAccountType.CARD_LIABILITY,
            currency="TWD",
        )
    )
    settlement = setup.open_account(
        OpenFinancialAccountRequest(
            institution_code="test_bank",
            display_name="測試結帳帳戶",
            account_type=FinancialAccountType.CHECKING,
            currency="TWD",
        )
    )
    foreign = setup.open_account(
        OpenFinancialAccountRequest(
            institution_code="other_bank",
            display_name="日圓帳戶",
            account_type=FinancialAccountType.SAVINGS,
            currency="JPY",
        )
    )
    card_id = _register_card(cards, account_id)

    def _configure(**overrides: object) -> None:
        values: dict[str, object] = {
            "card_id": card_id,
            "expected_version": 1,
            "display_name": "測試卡",
            "holder_role": CardHolderRole.PRIMARY,
            "liability_account_id": liability.account.id,
            "settlement_account_id": settlement.account.id,
            "default_funding_wallet_id": None,
        }
        values.update(overrides)
        cards.configure(ConfigureCardRequest(**values))  # type: ignore[arg-type]

    with pytest.raises(CardConfigurationError, match="card_liability account"):
        _configure(liability_account_id=settlement.account.id)
    with pytest.raises(CardConfigurationError, match="asset account"):
        _configure(settlement_account_id=liability.account.id)
    with pytest.raises(CardConfigurationError, match="one currency"):
        _configure(settlement_account_id=foreign.account.id)
    with pytest.raises(CardConfigurationError, match="one currency"):
        _configure(
            default_funding_wallet_id=foreign.unallocated_wallet.id  # type: ignore[union-attr]
        )
    with pytest.raises(CardConfigurationError, match="liability account was not found"):
        _configure(liability_account_id=9_999)
    with pytest.raises(CardConfigurationError, match="wallet was not found"):
        _configure(default_funding_wallet_id=9_999)

    _configure(
        default_funding_wallet_id=settlement.unallocated_wallet.id  # type: ignore[union-attr]
    )
    ready = cards.list_cards()[0]
    assert ready.configuration_status is CardConfigurationStatus.READY
    assert ready.version == 2
    with pytest.raises(ConcurrentUpdateError):
        _configure()


def test_registering_a_duplicate_card_identity_is_rejected(
    unit_of_work_factory: UnitOfWorkFactory, cards: ManageCards
) -> None:
    account_id, _, _ = _seed_transaction(unit_of_work_factory)
    _register_card(cards, account_id)

    with pytest.raises(CardConfigurationError, match="already configured"):
        _register_card(cards, account_id)
    with pytest.raises(CardConfigurationError, match="Gmail account was not found"):
        _register_card(cards, 9_999)
    with pytest.raises(ValueError, match="exactly four digits"):
        cards.register(
            RegisterCardRequest(
                gmail_account_id=account_id,
                issuer_code="test_bank",
                display_name="壞卡",
                last_four="12345678901234",
            )
        )


def test_a_foreign_event_holding_the_intake_key_is_rejected(
    unit_of_work_factory: UnitOfWorkFactory, intake: FinancialEventIntake
) -> None:
    """Regression: a matching key alone never proves ownership."""
    _, _, transaction_id = _seed_transaction(unit_of_work_factory)
    _confirm(unit_of_work_factory, transaction_id)
    with unit_of_work_factory() as unit:
        unit.financial_events.add(
            FinancialEventDraft(
                origin=FinancialEventOrigin.MANUAL,
                event_type=FinancialEventType.PURCHASE,
                payment_method=PaymentMethod.CASH,
                recognition_basis=RecognitionBasis.OBSERVED_MANUAL,
                occurred_at=OCCURRED_AT,
                amount_minor=1,
                currency="TWD",
                idempotency_key=intake_idempotency_key(transaction_id),
            )
        )
        unit.commit()

    with pytest.raises(IdempotencyConflictError, match="different kind of event"):
        _stage(unit_of_work_factory, intake, (transaction_id,))

    with unit_of_work_factory() as unit:
        assert unit.source_links.get_confirmed_for_transaction(transaction_id) is None


def test_an_event_without_its_confirmed_link_is_rejected(
    unit_of_work_factory: UnitOfWorkFactory,
    intake: FinancialEventIntake,
    migrated_database: Database,
) -> None:
    """Regression: a Gmail event whose confirmed link vanished is a conflict."""
    _, _, transaction_id = _seed_transaction(unit_of_work_factory)
    _confirm(unit_of_work_factory, transaction_id)
    _stage(unit_of_work_factory, intake, (transaction_id,))
    with migrated_database.engine.begin() as connection:
        connection.exec_driver_sql("DELETE FROM financial_event_source_links")

    with pytest.raises(IdempotencyConflictError, match="not linked"):
        _stage(unit_of_work_factory, intake, (transaction_id,))


def test_editing_a_confirmed_amount_reconciles_instead_of_skipping(
    unit_of_work_factory: UnitOfWorkFactory,
    intake: FinancialEventIntake,
    cards: ManageCards,
) -> None:
    """Regression: a legitimate retry reconciles the existing event."""
    account_id, _, transaction_id = _seed_transaction(unit_of_work_factory)
    _register_card(cards, account_id)
    _confirm(unit_of_work_factory, transaction_id)
    _stage(unit_of_work_factory, intake, (transaction_id,))

    with unit_of_work_factory() as unit:
        unit.transactions.apply_review(
            TransactionReviewDraft(
                transaction_id=transaction_id,
                expected_updated_at=unit.transactions.get(transaction_id).updated_at,  # type: ignore[union-attr]
                override_fields=frozenset({"amount_minor"}),
                transaction_type=TransactionType.PURCHASE,
                merchant=None,
                amount_minor=200000,
                currency=None,
                transaction_at=None,
                manually_confirmed=True,
                manually_ignored=False,
                user_note=None,
            )
        )
        unit.commit()

    repeated = _stage(unit_of_work_factory, intake, (transaction_id,))

    assert repeated.skipped[0].reason is IntakeSkipReason.ALREADY_STAGED
    with unit_of_work_factory() as unit:
        event = unit.financial_events.get_by_idempotency_key(
            intake_idempotency_key(transaction_id)
        )
        assert event is not None
        assert event.amount_minor == 200000


def test_parser_reconciliation_never_overwrites_a_user_owned_event_note(
    unit_of_work_factory: UnitOfWorkFactory,
    intake: FinancialEventIntake,
    cards: ManageCards,
) -> None:
    """Parser-owned fields may refresh, but the ledger note belongs to the user."""
    account_id, _, transaction_id = _seed_transaction(unit_of_work_factory)
    _register_card(cards, account_id)
    _confirm(unit_of_work_factory, transaction_id)
    _stage(unit_of_work_factory, intake, (transaction_id,))
    with unit_of_work_factory() as unit:
        event = unit.financial_events.get_by_idempotency_key(
            intake_idempotency_key(transaction_id)
        )
    assert event is not None
    UpdateFinancialEventNote(unit_of_work_factory).execute(
        UpdateFinancialEventNoteRequest(
            financial_event_id=event.id,
            expected_version=event.version,
            note="我自己補的用途",
        )
    )

    with unit_of_work_factory() as unit:
        transaction = unit.transactions.get(transaction_id)
        assert transaction is not None
        unit.transactions.apply_review(
            TransactionReviewDraft(
                transaction_id=transaction_id,
                expected_updated_at=transaction.updated_at,
                override_fields=frozenset({"amount_minor"}),
                transaction_type=TransactionType.PURCHASE,
                merchant=None,
                amount_minor=200000,
                currency=None,
                transaction_at=None,
                manually_confirmed=True,
                manually_ignored=False,
                user_note=None,
            )
        )
        unit.commit()

    _stage(unit_of_work_factory, intake, (transaction_id,))

    with unit_of_work_factory() as unit:
        refreshed = unit.financial_events.get(event.id)
    assert refreshed is not None
    assert refreshed.amount_minor == 200000
    assert refreshed.note == "我自己補的用途"


def test_unconfirming_a_bridged_transaction_records_a_discrepancy(
    unit_of_work_factory: UnitOfWorkFactory,
    intake: FinancialEventIntake,
    cards: ManageCards,
) -> None:
    """Regression: every review reconciles, not only a confirmation."""
    account_id, _, transaction_id = _seed_transaction(unit_of_work_factory)
    _register_card(cards, account_id)
    review = ReviewTransaction(unit_of_work_factory, intake=intake)

    def _review(*, confirmed: bool, ignored: bool) -> None:
        with unit_of_work_factory() as unit:
            expected = unit.transactions.get(transaction_id).updated_at  # type: ignore[union-attr]
        review.execute(
            ReviewTransactionRequest(
                transaction_id=transaction_id,
                expected_updated_at=expected,
                override_fields=frozenset(),
                transaction_type=TransactionType.PURCHASE,
                merchant=None,
                amount_minor=None,
                currency=None,
                transaction_at=None,
                manually_confirmed=confirmed,
                manually_ignored=ignored,
                user_note=None,
            )
        )

    _review(confirmed=True, ignored=False)
    with unit_of_work_factory() as unit:
        link = unit.source_links.get_confirmed_for_transaction(transaction_id)
        assert link is not None
        assert link.discrepancies == ()

    _review(confirmed=False, ignored=True)

    with unit_of_work_factory() as unit:
        link = unit.source_links.get_confirmed_for_transaction(transaction_id)
        assert link is not None
        assert SourceDiscrepancy.SOURCE_NO_LONGER_CONFIRMED in link.discrepancies


def test_configuring_a_card_later_remaps_the_event_and_clears_the_reason(
    unit_of_work_factory: UnitOfWorkFactory,
    intake: FinancialEventIntake,
    cards: ManageCards,
) -> None:
    """Regression: a late card mapping must not leave contradictory state."""
    account_id, _, transaction_id = _seed_transaction(unit_of_work_factory)
    _confirm(unit_of_work_factory, transaction_id)
    _stage(unit_of_work_factory, intake, (transaction_id,))
    with unit_of_work_factory() as unit:
        link = unit.source_links.get_confirmed_for_transaction(transaction_id)
        assert link is not None
        assert SourceLinkReason.CARD_NOT_CONFIGURED in link.reasons

    card_id = _register_card(cards, account_id)
    repeated = _stage(unit_of_work_factory, intake, (transaction_id,))

    assert repeated.skipped[0].reason is IntakeSkipReason.ALREADY_STAGED
    with unit_of_work_factory() as unit:
        event = unit.financial_events.get_by_idempotency_key(
            intake_idempotency_key(transaction_id)
        )
        assert event is not None
        assert event.card_id == card_id
        link = unit.source_links.get_confirmed_for_transaction(transaction_id)
        assert link is not None
        assert SourceLinkReason.CARD_NOT_CONFIGURED not in link.reasons
        assert link.reasons == (SourceLinkReason.DIRECT_GMAIL_INTAKE,)


def test_backfill_persists_a_reconciliation_that_stages_nothing_new(
    unit_of_work_factory: UnitOfWorkFactory,
    intake: FinancialEventIntake,
    cards: ManageCards,
) -> None:
    """Regression: apply mode must commit reconciliation-only work.

    This drives the real ``backfill`` path rather than a test helper that
    always commits, because the defect was in that path's commit condition.
    """
    account_id, _, transaction_id = _seed_transaction(unit_of_work_factory)
    _confirm(unit_of_work_factory, transaction_id)

    first = intake.backfill(dry_run=False)
    card_id = _register_card(cards, account_id)
    second = intake.backfill(dry_run=False)

    assert first.staged_count == 1
    assert first.reconciled_count == 0
    assert second.staged_count == 0
    assert second.reconciled_count == 1
    assert second.skipped[0].reason is IntakeSkipReason.ALREADY_STAGED

    # A new Unit of Work proves the reconciliation survived the commit.
    with unit_of_work_factory() as unit:
        event = unit.financial_events.get_by_idempotency_key(
            intake_idempotency_key(transaction_id)
        )
        assert event is not None
        assert event.card_id == card_id
        link = unit.source_links.get_confirmed_for_transaction(transaction_id)
        assert link is not None
        assert link.reasons == (SourceLinkReason.DIRECT_GMAIL_INTAKE,)


def test_backfill_dry_run_never_persists_a_reconciliation(
    unit_of_work_factory: UnitOfWorkFactory,
    intake: FinancialEventIntake,
    cards: ManageCards,
) -> None:
    """A preview must leave the bridged event exactly as it was."""
    account_id, _, transaction_id = _seed_transaction(unit_of_work_factory)
    _confirm(unit_of_work_factory, transaction_id)
    intake.backfill(dry_run=False)
    _register_card(cards, account_id)

    preview = intake.backfill(dry_run=True)

    assert preview.reconciled_count == 0
    with unit_of_work_factory() as unit:
        event = unit.financial_events.get_by_idempotency_key(
            intake_idempotency_key(transaction_id)
        )
        assert event is not None
        assert event.card_id is None
        link = unit.source_links.get_confirmed_for_transaction(transaction_id)
        assert link is not None
        assert SourceLinkReason.CARD_NOT_CONFIGURED in link.reasons

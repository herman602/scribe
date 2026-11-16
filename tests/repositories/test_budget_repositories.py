"""File-backed ledger repository and database-constraint tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from app.domain.budget_enums import (
    CardConfigurationStatus,
    CardHolderRole,
    FinancialAccountType,
    FinancialEventOrigin,
    FinancialEventStatus,
    FinancialEventType,
    JournalEntryType,
    LedgerAccountKind,
    PaymentMethod,
    RecognitionBasis,
    SourceLinkReason,
    SourceLinkStatus,
    WalletPurpose,
)
from app.domain.budget_models import (
    CardConfigurationDraft,
    CardDraft,
    CardIdentity,
    FinancialAccountDraft,
    FinancialEventDraft,
    FinancialEventUpdate,
    JournalEntryDraft,
    LedgerAccountDraft,
    Money,
    PostingDraft,
    SourceLinkDraft,
    WalletDraft,
)
from app.domain.enums import TransactionType
from app.domain.errors import (
    CardConfigurationError,
    DatabaseError,
    FinancialEventError,
    SourceLinkError,
)
from app.domain.models import EmailSnapshot, TransactionDraft
from app.infrastructure.database.engine import Database
from app.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork
from sqlalchemy.exc import IntegrityError

EFFECTIVE_AT = datetime(2026, 8, 7, 13, 30, tzinfo=UTC)
RECORDED_AT = datetime(2026, 8, 8, 2, 0, tzinfo=UTC)


def _open_account(
    unit: SqlAlchemyUnitOfWork,
    *,
    display_name: str = "測試支票帳戶",
    account_type: FinancialAccountType = FinancialAccountType.CHECKING,
    currency: str = "TWD",
) -> tuple[int, int]:
    account = unit.financial_accounts.add(
        FinancialAccountDraft(
            institution_code="test_bank",
            display_name=display_name,
            account_type=account_type,
            currency=currency,
            masked_reference="****4321",
        )
    )
    kind = (
        LedgerAccountKind.LIABILITY
        if account_type is FinancialAccountType.CARD_LIABILITY
        else LedgerAccountKind.ASSET
    )
    ledger_account = unit.ledger_accounts.add(
        LedgerAccountDraft(
            code=f"{kind.value}:test_bank:{account.id}",
            name=display_name,
            kind=kind,
            currency=currency,
            financial_account_id=account.id,
        )
    )
    return account.id, ledger_account.id


def test_account_wallet_and_ledger_records_round_trip(
    migrated_database: Database,
) -> None:
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        account_id, ledger_account_id = _open_account(unit)
        unallocated = unit.wallets.add(
            WalletDraft(
                financial_account_id=account_id,
                name="尚未分配",
                purpose=WalletPurpose.UNALLOCATED,
            )
        )
        rent = unit.wallets.add(
            WalletDraft(financial_account_id=account_id, name=" 房租 ")
        )
        unit.commit()

    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        account = unit.financial_accounts.get(account_id)
        ledger_account = unit.ledger_accounts.get(ledger_account_id)
        assert account is not None
        assert ledger_account is not None
        assert account.account_type is FinancialAccountType.CHECKING
        assert account.masked_reference == "****4321"
        assert account.is_active
        assert account.version == 1
        assert ledger_account.kind is LedgerAccountKind.ASSET
        assert ledger_account.financial_account_id == account_id
        assert unit.ledger_accounts.get_by_code(ledger_account.code) == ledger_account
        assert (
            unit.ledger_accounts.get_for_financial_account(account_id) == ledger_account
        )
        assert unit.ledger_accounts.list_all() == (ledger_account,)
        assert unit.wallets.get(rent.id) is not None
        assert unit.wallets.get_by_name(account_id, "房租") is not None
        assert unit.wallets.get_unallocated(account_id) == unallocated
        assert unit.wallets.list_for_account(account_id) == (unallocated, rent)
        assert unit.financial_accounts.list_active() == (account,)
        assert (
            unit.financial_accounts.get_by_identity(
                institution_code="test_bank",
                display_name="測試支票帳戶",
                currency="twd",
            )
            == account
        )


def test_missing_ledger_records_return_none(migrated_database: Database) -> None:
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        assert unit.financial_accounts.get(999) is None
        assert unit.financial_accounts.list_active() == ()
        assert unit.wallets.get(999) is None
        assert unit.wallets.get_by_name(999, "尚未分配") is None
        assert unit.wallets.get_unallocated(999) is None
        assert unit.ledger_accounts.get(999) is None
        assert unit.ledger_accounts.get_by_code("missing") is None
        assert unit.ledger_accounts.get_for_financial_account(999) is None
        assert unit.journal.get(999) is None
        assert unit.journal.get_by_idempotency_key("missing") is None
        assert unit.journal.get_reversal_of(999) is None


def test_wallet_names_are_unique_within_one_account(
    migrated_database: Database,
) -> None:
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        account_id, _ = _open_account(unit)
        unit.wallets.add(WalletDraft(financial_account_id=account_id, name="房租"))
        unit.commit()

    with (
        SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit,
        pytest.raises(DatabaseError, match="database write failed"),
    ):
        unit.wallets.add(WalletDraft(financial_account_id=account_id, name="房租"))


def test_only_one_active_unallocated_wallet_per_account(
    migrated_database: Database,
) -> None:
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        account_id, _ = _open_account(unit)
        unit.wallets.add(
            WalletDraft(
                financial_account_id=account_id,
                name="尚未分配",
                purpose=WalletPurpose.UNALLOCATED,
            )
        )
        unit.commit()

    with (
        SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit,
        pytest.raises(DatabaseError, match="database write failed"),
    ):
        unit.wallets.add(
            WalletDraft(
                financial_account_id=account_id,
                name="尚未分配 2",
                purpose=WalletPurpose.UNALLOCATED,
            )
        )


def test_one_physical_ledger_account_per_financial_account(
    migrated_database: Database,
) -> None:
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        account_id, _ = _open_account(unit)
        unit.commit()

    with (
        SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit,
        pytest.raises(DatabaseError, match="database write failed"),
    ):
        unit.ledger_accounts.add(
            LedgerAccountDraft(
                code="asset:test_bank:duplicate",
                name="Duplicate",
                kind=LedgerAccountKind.ASSET,
                currency="TWD",
                financial_account_id=account_id,
            )
        )


def test_active_account_identity_is_unique_per_institution_and_currency(
    migrated_database: Database,
) -> None:
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        _open_account(unit)
        unit.commit()

    with (
        SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit,
        pytest.raises(DatabaseError, match="database write failed"),
    ):
        unit.financial_accounts.add(
            FinancialAccountDraft(
                institution_code="test_bank",
                display_name="測試支票帳戶",
                account_type=FinancialAccountType.CHECKING,
                currency="TWD",
            )
        )


def test_masked_reference_rejects_a_full_account_number(
    migrated_database: Database,
) -> None:
    with (
        SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit,
        pytest.raises(ValueError, match="full account number"),
    ):
        unit.financial_accounts.add(
            FinancialAccountDraft(
                institution_code="test_bank",
                display_name="測試帳戶",
                account_type=FinancialAccountType.SAVINGS,
                currency="TWD",
                masked_reference="00998877665544",
            )
        )


def test_journal_entry_and_postings_are_persisted_with_their_wallets(
    migrated_database: Database,
) -> None:
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        account_id, ledger_account_id = _open_account(unit)
        wallet = unit.wallets.add(
            WalletDraft(
                financial_account_id=account_id,
                name="尚未分配",
                purpose=WalletPurpose.UNALLOCATED,
            )
        )
        equity = unit.ledger_accounts.add(
            LedgerAccountDraft(
                code="equity:opening:TWD",
                name="Opening balance equity (TWD)",
                kind=LedgerAccountKind.EQUITY,
                currency="TWD",
            )
        )
        entry = unit.journal.add(
            JournalEntryDraft(
                entry_type=JournalEntryType.OPENING_BALANCE,
                effective_at=EFFECTIVE_AT,
                recorded_at=RECORDED_AT,
                idempotency_key="opening_balance:financial_account:1",
                description="Opening position",
                postings=(
                    PostingDraft(
                        ledger_account_id=ledger_account_id,
                        amount=Money(500000, "TWD"),
                        wallet_id=wallet.id,
                    ),
                    PostingDraft(
                        ledger_account_id=equity.id, amount=Money(-500000, "TWD")
                    ),
                ),
            )
        )
        unit.commit()

    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        stored = unit.journal.get(entry.id)
        assert stored is not None
        assert stored.effective_at == EFFECTIVE_AT
        assert stored.recorded_at == RECORDED_AT
        assert stored.entry_type is JournalEntryType.OPENING_BALANCE
        assert [posting.amount_minor for posting in stored.postings] == [
            500000,
            -500000,
        ]
        assert stored.postings[0].wallet_id == wallet.id
        assert stored.postings[1].wallet_id is None
        assert (
            unit.journal.get_by_idempotency_key("opening_balance:financial_account:1")
            == stored
        )


def test_journal_idempotency_key_is_unique(migrated_database: Database) -> None:
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        _, ledger_account_id = _open_account(
            unit, account_type=FinancialAccountType.CARD_LIABILITY
        )
        equity = unit.ledger_accounts.add(
            LedgerAccountDraft(
                code="equity:opening:TWD",
                name="Opening balance equity (TWD)",
                kind=LedgerAccountKind.EQUITY,
                currency="TWD",
            )
        )
        draft = JournalEntryDraft(
            entry_type=JournalEntryType.ADJUSTMENT,
            effective_at=EFFECTIVE_AT,
            recorded_at=RECORDED_AT,
            idempotency_key="duplicate-key",
            description="",
            postings=(
                PostingDraft(
                    ledger_account_id=ledger_account_id, amount=Money(-100, "TWD")
                ),
                PostingDraft(ledger_account_id=equity.id, amount=Money(100, "TWD")),
            ),
        )
        unit.journal.add(draft)
        with pytest.raises(DatabaseError, match="database write failed"):
            unit.journal.add(draft)


def test_journal_repository_rejects_unusable_idempotency_keys(
    migrated_database: Database,
) -> None:
    draft = JournalEntryDraft(
        entry_type=JournalEntryType.ADJUSTMENT,
        effective_at=EFFECTIVE_AT,
        recorded_at=RECORDED_AT,
        idempotency_key="   ",
        description="",
        postings=(),
    )
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        with pytest.raises(ValueError, match="must not be empty"):
            unit.journal.add(draft)
        with pytest.raises(ValueError, match="too long"):
            unit.journal.add(
                JournalEntryDraft(
                    entry_type=draft.entry_type,
                    effective_at=draft.effective_at,
                    recorded_at=draft.recorded_at,
                    idempotency_key="k" * 201,
                    description="",
                    postings=(),
                )
            )


def test_an_entry_can_be_reversed_only_once(migrated_database: Database) -> None:
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        _, ledger_account_id = _open_account(
            unit, account_type=FinancialAccountType.CARD_LIABILITY
        )
        equity = unit.ledger_accounts.add(
            LedgerAccountDraft(
                code="equity:opening:TWD",
                name="Opening balance equity (TWD)",
                kind=LedgerAccountKind.EQUITY,
                currency="TWD",
            )
        )
        original = unit.journal.add(
            JournalEntryDraft(
                entry_type=JournalEntryType.ADJUSTMENT,
                effective_at=EFFECTIVE_AT,
                recorded_at=RECORDED_AT,
                idempotency_key="original",
                description="",
                postings=(
                    PostingDraft(
                        ledger_account_id=ledger_account_id, amount=Money(-100, "TWD")
                    ),
                    PostingDraft(ledger_account_id=equity.id, amount=Money(100, "TWD")),
                ),
            )
        )
        unit.journal.add(
            JournalEntryDraft(
                entry_type=JournalEntryType.REVERSAL,
                effective_at=EFFECTIVE_AT,
                recorded_at=RECORDED_AT,
                idempotency_key="reversal-1",
                description="",
                reversal_of_id=original.id,
                postings=(
                    PostingDraft(
                        ledger_account_id=ledger_account_id, amount=Money(100, "TWD")
                    ),
                    PostingDraft(
                        ledger_account_id=equity.id, amount=Money(-100, "TWD")
                    ),
                ),
            )
        )
        unit.commit()
        assert unit.journal.get_reversal_of(original.id) is not None

    with (
        SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit,
        pytest.raises(DatabaseError, match="database write failed"),
    ):
        unit.journal.add(
            JournalEntryDraft(
                entry_type=JournalEntryType.REVERSAL,
                effective_at=EFFECTIVE_AT,
                recorded_at=RECORDED_AT,
                idempotency_key="reversal-2",
                description="",
                reversal_of_id=original.id,
                postings=(
                    PostingDraft(
                        ledger_account_id=ledger_account_id, amount=Money(100, "TWD")
                    ),
                    PostingDraft(
                        ledger_account_id=equity.id, amount=Money(-100, "TWD")
                    ),
                ),
            )
        )


def test_database_rejects_zero_amount_postings(migrated_database: Database) -> None:
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        _, ledger_account_id = _open_account(
            unit, account_type=FinancialAccountType.CARD_LIABILITY
        )
        with pytest.raises(DatabaseError, match="database write failed"):
            unit.journal.add(
                JournalEntryDraft(
                    entry_type=JournalEntryType.ADJUSTMENT,
                    effective_at=EFFECTIVE_AT,
                    recorded_at=RECORDED_AT,
                    idempotency_key="zero-amount",
                    description="",
                    postings=(
                        PostingDraft(
                            ledger_account_id=ledger_account_id,
                            amount=Money(0, "TWD"),
                        ),
                    ),
                )
            )


def test_ledger_history_uses_restrict_and_is_never_cascade_deleted(
    migrated_database: Database,
) -> None:
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        account_id, _ = _open_account(unit)
        unit.wallets.add(
            WalletDraft(
                financial_account_id=account_id,
                name="尚未分配",
                purpose=WalletPurpose.UNALLOCATED,
            )
        )
        unit.commit()

    with (
        migrated_database.engine.connect() as connection,
        pytest.raises(IntegrityError),
    ):
        connection.exec_driver_sql(
            "DELETE FROM financial_accounts WHERE id = ?", (account_id,)
        )


def _seed_card_source(
    unit: SqlAlchemyUnitOfWork, *, gmail_message_id: str = "gmail-b2-repo"
) -> tuple[int, int]:
    """Return one (gmail_account_id, transaction_id) pair for link tests."""
    account = unit.accounts.get_or_create("user@example.test")
    outcome = unit.emails.upsert_from_gmail(
        EmailSnapshot(
            gmail_account_id=account.id,
            gmail_message_id=gmail_message_id,
            gmail_thread_id="thread-repo",
            gmail_history_id="500",
            sender="notice@example-bank.test",
            recipients=("user@example.test",),
            subject="消費通知",
            gmail_internal_date_ms=1_754_000_000_000,
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
            amount_minor=128000,
            currency="TWD",
            transaction_at=EFFECTIVE_AT,
        )
    )
    return account.id, transaction.id


def _event_draft(**overrides: object) -> FinancialEventDraft:
    values: dict[str, object] = {
        "origin": FinancialEventOrigin.GMAIL_TRANSACTION,
        "event_type": FinancialEventType.PURCHASE,
        "payment_method": PaymentMethod.CREDIT_CARD,
        "recognition_basis": RecognitionBasis.GMAIL_NOTIFICATION,
        "occurred_at": EFFECTIVE_AT,
        "amount_minor": 128000,
        "currency": "TWD",
        "idempotency_key": "gmail_transaction:1",
    }
    values.update(overrides)
    return FinancialEventDraft(**values)  # type: ignore[arg-type]


def test_event_amounts_are_a_positive_integer_magnitude() -> None:
    for amount in (0, -1):
        with pytest.raises(ValueError, match="positive magnitude"):
            _event_draft(amount_minor=amount)
    with pytest.raises(TypeError, match="integer minor units"):
        _event_draft(amount_minor=1280.5)
    with pytest.raises(TypeError, match="integer minor units"):
        _event_draft(amount_minor=True)


def test_card_event_and_link_records_round_trip(
    migrated_database: Database,
) -> None:
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        account_id, transaction_id = _seed_card_source(unit)
        card = unit.cards.add(
            CardDraft(
                gmail_account_id=account_id,
                issuer_code=" test_bank ",
                display_name="測試卡",
                last_four="4321",
                holder_role=CardHolderRole.SUPPLEMENTARY,
            )
        )
        event = unit.financial_events.add(_event_draft(card_id=card.id))
        link = unit.source_links.add(
            SourceLinkDraft(
                financial_event_id=event.id,
                transaction_id=transaction_id,
                status=SourceLinkStatus.CONFIRMED,
                match_score_bps=10000,
                reasons=(SourceLinkReason.DIRECT_GMAIL_INTAKE,),
                reviewed_at=RECORDED_AT,
            )
        )
        unit.commit()

    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        stored = unit.cards.get(card.id)
        assert stored is not None
        assert stored.issuer_code == "test_bank"
        assert stored.configuration_status is CardConfigurationStatus.INCOMPLETE
        assert stored.version == 1
        assert unit.cards.list_all() == (stored,)
        assert (
            unit.cards.get_by_identity(
                CardIdentity(
                    gmail_account_id=account_id,
                    issuer_code="test_bank",
                    last_four="4321",
                )
            )
            == stored
        )
        assert unit.cards.list_active_by_last_four(
            gmail_account_id=account_id, last_four="4321"
        ) == (stored,)
        assert unit.cards.get(9_999) is None
        assert unit.financial_events.get(event.id) is not None
        assert unit.financial_events.list_by_status(
            frozenset({FinancialEventStatus.NEEDS_ALLOCATION})
        ) == (unit.financial_events.get(event.id),)
        assert unit.financial_events.list_by_status(frozenset()) == ()
        assert unit.financial_events.get(9_999) is None
        assert unit.source_links.get(link.id) is not None
        assert unit.source_links.get(9_999) is None
        assert unit.source_links.list_for_email(1) == (link,)
        assert unit.source_links.get_confirmed_for_transaction(9_999) is None


def test_only_one_confirmed_link_per_parsed_transaction(
    migrated_database: Database,
) -> None:
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        _, transaction_id = _seed_card_source(unit)
        first = unit.financial_events.add(_event_draft())
        second = unit.financial_events.add(
            _event_draft(idempotency_key="gmail_transaction:2")
        )
        unit.source_links.add(
            SourceLinkDraft(
                financial_event_id=first.id,
                transaction_id=transaction_id,
                status=SourceLinkStatus.CONFIRMED,
                match_score_bps=10000,
            )
        )
        unit.commit()

    with (
        SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit,
        pytest.raises(DatabaseError, match="database write failed"),
    ):
        unit.source_links.add(
            SourceLinkDraft(
                financial_event_id=second.id,
                transaction_id=transaction_id,
                status=SourceLinkStatus.CONFIRMED,
                match_score_bps=10000,
            )
        )

    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        candidate = unit.source_links.add(
            SourceLinkDraft(
                financial_event_id=second.id,
                transaction_id=transaction_id,
                status=SourceLinkStatus.CANDIDATE,
                match_score_bps=8500,
            )
        )
        unit.commit()
        assert candidate.status is SourceLinkStatus.CANDIDATE


def test_event_and_card_identities_are_unique(
    migrated_database: Database,
) -> None:
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        account_id, _ = _seed_card_source(unit)
        unit.cards.add(
            CardDraft(
                gmail_account_id=account_id,
                issuer_code="test_bank",
                display_name="測試卡",
                last_four="4321",
            )
        )
        unit.financial_events.add(_event_draft())
        unit.commit()

    with (
        SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit,
        pytest.raises(DatabaseError, match="database write failed"),
    ):
        unit.cards.add(
            CardDraft(
                gmail_account_id=account_id,
                issuer_code="test_bank",
                display_name="重複卡",
                last_four="4321",
            )
        )

    with (
        SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit,
        pytest.raises(DatabaseError, match="database write failed"),
    ):
        unit.financial_events.add(_event_draft())


def test_event_repository_rejects_unusable_keys_and_scores(
    migrated_database: Database,
) -> None:
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        _, transaction_id = _seed_card_source(unit)
        with pytest.raises(ValueError, match="must not be empty"):
            unit.financial_events.add(_event_draft(idempotency_key="   "))
        with pytest.raises(ValueError, match="too long"):
            unit.financial_events.add(_event_draft(idempotency_key="k" * 201))
        event = unit.financial_events.add(_event_draft())
        with pytest.raises(SourceLinkError, match="between 0 and 10000"):
            unit.source_links.add(
                SourceLinkDraft(
                    financial_event_id=event.id,
                    transaction_id=transaction_id,
                    status=SourceLinkStatus.CANDIDATE,
                    match_score_bps=10001,
                )
            )
        with pytest.raises(FinancialEventError, match="was not found"):
            unit.financial_events.refresh_source_values(
                9_999,
                FinancialEventUpdate(
                    event_type=FinancialEventType.PURCHASE,
                    payment_method=PaymentMethod.CREDIT_CARD,
                    occurred_at=EFFECTIVE_AT,
                    amount_minor=1,
                    currency="TWD",
                    merchant=None,
                    card_id=None,
                ),
            )
        with pytest.raises(SourceLinkError, match="was not found"):
            unit.source_links.record_discrepancies(9_999, ())
        with pytest.raises(CardConfigurationError, match="card was not found"):
            unit.cards.save_configuration(
                CardConfigurationDraft(
                    card_id=9_999,
                    expected_version=1,
                    display_name="缺失卡",
                    holder_role=CardHolderRole.UNKNOWN,
                    liability_account_id=None,
                    settlement_account_id=None,
                    default_funding_wallet_id=None,
                )
            )


def test_financial_history_cannot_be_cascade_deleted_by_email_removal(
    migrated_database: Database,
) -> None:
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        _, transaction_id = _seed_card_source(unit)
        event = unit.financial_events.add(_event_draft())
        unit.source_links.add(
            SourceLinkDraft(
                financial_event_id=event.id,
                transaction_id=transaction_id,
                status=SourceLinkStatus.CONFIRMED,
                match_score_bps=10000,
            )
        )
        unit.commit()

    with (
        migrated_database.engine.connect() as connection,
        pytest.raises(IntegrityError),
    ):
        connection.exec_driver_sql("DELETE FROM emails WHERE id = 1")

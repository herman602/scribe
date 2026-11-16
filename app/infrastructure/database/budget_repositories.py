"""SQLAlchemy ledger repositories and model/domain translation."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any, cast

from sqlalchemy import case, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, aliased

from app.domain.budget_enums import (
    AllocationSource,
    BalanceSnapshotSource,
    BudgetPeriodStatus,
    CardConfigurationStatus,
    CardHolderRole,
    CardImportAssignmentKind,
    CommitmentFulfillmentMode,
    CommitmentStatus,
    FinancialAccountType,
    FinancialEventOrigin,
    FinancialEventStatus,
    FinancialEventType,
    JournalEntryType,
    LedgerAccountKind,
    PaymentMethod,
    RecognitionBasis,
    SalaryRunItemStatus,
    SalaryRunStatus,
    SourceDiscrepancy,
    SourceLinkReason,
    SourceLinkStatus,
    WalletPurpose,
)
from app.domain.budget_matching import MAX_SCORE_BPS
from app.domain.budget_models import (
    BalanceSnapshot,
    BalanceSnapshotDraft,
    BudgetCategory,
    BudgetCategoryDraft,
    BudgetCategoryUpdate,
    BudgetPeriod,
    BudgetTarget,
    BudgetTargetDraft,
    Card,
    CardConfigurationDraft,
    CardDraft,
    CardIdentity,
    CardImportAssignment,
    CardImportAssignmentDraft,
    DedicatedWalletAllocation,
    DedicatedWalletAllocationDraft,
    EventAllocation,
    EventAllocationDraft,
    FinancialAccount,
    FinancialAccountDraft,
    FinancialEvent,
    FinancialEventDraft,
    FinancialEventNoteUpdate,
    FinancialEventSourceLink,
    FinancialEventUpdate,
    FundingCommitment,
    FundingCommitmentDraft,
    JournalEntry,
    JournalEntryDraft,
    JournalPosting,
    LedgerAccount,
    LedgerAccountDraft,
    SourceLinkDraft,
    Wallet,
    WalletDraft,
    normalize_currency,
    normalize_last_four,
    normalize_masked_reference,
    normalize_name,
    validate_minor_units,
)
from app.domain.budget_rules import (
    card_configuration_status,
    event_accepts_source_refresh,
)
from app.domain.errors import (
    BudgetPeriodError,
    CardConfigurationError,
    ConcurrentUpdateError,
    DatabaseError,
    FinancialEventError,
    LedgerConfigurationError,
    SourceLinkError,
)
from app.infrastructure.database.budget_models import (
    BalanceSnapshotModel,
    BudgetCategoryModel,
    BudgetPeriodModel,
    BudgetTargetModel,
    CardImportAssignmentModel,
    CardModel,
    DedicatedWalletAllocationModel,
    EventAllocationModel,
    FinancialAccountModel,
    FinancialEventModel,
    FinancialEventSourceLinkModel,
    FundingCommitmentModel,
    JournalEntryModel,
    JournalPostingModel,
    LedgerAccountModel,
    SalaryAllocationRunItemModel,
    SalaryAllocationRunModel,
    ScheduledTransferCommitmentModel,
    ScheduledTransferModel,
    WalletModel,
)
from app.infrastructure.database.models import TransactionModel
from app.infrastructure.database.scheduled_transfer_repositories import (
    ACTIVE_STATUS_VALUES,
    LIVE_STATUS_VALUES,
    refresh_commitment_status,
)

_MAX_IDEMPOTENCY_KEY_LENGTH = 200


def _flush_and_refresh(session: Session, model: Any) -> None:
    try:
        session.flush()
        session.refresh(model)
    except SQLAlchemyError as error:
        raise DatabaseError("database write failed") from error


def financial_account_record(model: FinancialAccountModel) -> FinancialAccount:
    return FinancialAccount(
        id=model.id,
        institution_code=model.institution_code,
        display_name=model.display_name,
        account_type=FinancialAccountType(model.account_type),
        currency=model.currency,
        masked_reference=model.masked_reference,
        is_active=model.is_active,
        version=model.version,
        created_at=model.created_at,
        updated_at=model.updated_at,
        display_order=model.display_order,
    )


def _wallet_record(model: WalletModel) -> Wallet:
    return Wallet(
        id=model.id,
        financial_account_id=model.financial_account_id,
        name=model.name,
        purpose=WalletPurpose(model.purpose),
        is_active=model.is_active,
        version=model.version,
        created_at=model.created_at,
        updated_at=model.updated_at,
        display_order=model.display_order,
    )


def _ledger_account_record(model: LedgerAccountModel) -> LedgerAccount:
    return LedgerAccount(
        id=model.id,
        code=model.code,
        name=model.name,
        kind=LedgerAccountKind(model.kind),
        currency=model.currency,
        financial_account_id=model.financial_account_id,
        budget_category_id=model.budget_category_id,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _posting_record(model: JournalPostingModel) -> JournalPosting:
    return JournalPosting(
        id=model.id,
        journal_entry_id=model.journal_entry_id,
        ledger_account_id=model.ledger_account_id,
        wallet_id=model.wallet_id,
        amount_minor=model.amount_minor,
        currency=model.currency,
        budget_category_id=model.budget_category_id,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def journal_entry_record(
    model: JournalEntryModel, postings: tuple[JournalPostingModel, ...]
) -> JournalEntry:
    return JournalEntry(
        id=model.id,
        entry_type=JournalEntryType(model.entry_type),
        effective_at=model.effective_at,
        recorded_at=model.recorded_at,
        idempotency_key=model.idempotency_key,
        reversal_of_id=model.reversal_of_id,
        description=model.description,
        financial_event_id=model.financial_event_id,
        postings=tuple(_posting_record(item) for item in postings),
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class SqlAlchemyFinancialAccountRepository:
    """SQLAlchemy repository for physical balance containers."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, draft: FinancialAccountDraft) -> FinancialAccount:
        """Persist one validated physical account at the end of the sheet."""
        last = self._session.scalar(
            select(func.max(FinancialAccountModel.display_order))
        )
        model = FinancialAccountModel(
            display_order=(last or 0) + 1,
            institution_code=normalize_name(
                draft.institution_code, label="institution code", max_length=100
            ),
            display_name=normalize_name(
                draft.display_name, label="account display name", max_length=200
            ),
            account_type=draft.account_type.value,
            currency=normalize_currency(draft.currency),
            masked_reference=normalize_masked_reference(draft.masked_reference),
        )
        self._session.add(model)
        _flush_and_refresh(self._session, model)
        return financial_account_record(model)

    def get(self, account_id: int) -> FinancialAccount | None:
        """Return one account by internal ID."""
        model = self._session.get(FinancialAccountModel, account_id)
        return None if model is None else financial_account_record(model)

    def get_by_identity(
        self, *, institution_code: str, display_name: str, currency: str
    ) -> FinancialAccount | None:
        """Return the active account matching one institution identity."""
        model = self._session.scalar(
            select(FinancialAccountModel).where(
                FinancialAccountModel.institution_code == institution_code.strip(),
                FinancialAccountModel.display_name == display_name.strip(),
                FinancialAccountModel.currency == normalize_currency(currency),
                FinancialAccountModel.is_active.is_(True),
            )
        )
        return None if model is None else financial_account_record(model)

    def list_active(self) -> tuple[FinancialAccount, ...]:
        """Return active accounts in the arrangement their owner chose."""
        models = self._session.scalars(
            select(FinancialAccountModel)
            .where(FinancialAccountModel.is_active.is_(True))
            .order_by(
                FinancialAccountModel.display_order,
                FinancialAccountModel.id,
            )
        ).all()
        return tuple(financial_account_record(model) for model in models)

    def set_display_order(self, account_id: int, position: int) -> None:
        """Place one account in the sheet without changing anything else.

        Its version is deliberately left alone: an arrangement is presentation,
        so reordering the sheet must not make a concurrent configuration edit
        look like a conflict.
        """
        self._session.execute(
            update(FinancialAccountModel)
            .where(FinancialAccountModel.id == account_id)
            .values(display_order=position)
        )


class SqlAlchemyWalletRepository:
    """SQLAlchemy repository for account-scoped wallets."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, draft: WalletDraft) -> Wallet:
        """Persist one wallet at the end of its account's sheet column."""
        last = self._session.scalar(
            select(func.max(WalletModel.display_order)).where(
                WalletModel.financial_account_id == draft.financial_account_id
            )
        )
        model = WalletModel(
            financial_account_id=draft.financial_account_id,
            name=normalize_name(draft.name, label="wallet name", max_length=200),
            purpose=draft.purpose.value,
            display_order=(last or 0) + 1,
        )
        self._session.add(model)
        _flush_and_refresh(self._session, model)
        return _wallet_record(model)

    def get(self, wallet_id: int) -> Wallet | None:
        """Return one wallet by internal ID."""
        model = self._session.get(WalletModel, wallet_id)
        return None if model is None else _wallet_record(model)

    def get_by_name(self, account_id: int, name: str) -> Wallet | None:
        """Return one wallet by its unique name inside an account."""
        model = self._session.scalar(
            select(WalletModel).where(
                WalletModel.financial_account_id == account_id,
                WalletModel.name == name.strip(),
            )
        )
        return None if model is None else _wallet_record(model)

    def get_unallocated(self, account_id: int) -> Wallet | None:
        """Return the account's single active Unallocated wallet."""
        model = self._session.scalar(
            select(WalletModel).where(
                WalletModel.financial_account_id == account_id,
                WalletModel.purpose == WalletPurpose.UNALLOCATED.value,
                WalletModel.is_active.is_(True),
            )
        )
        return None if model is None else _wallet_record(model)

    def list_for_account(self, account_id: int) -> tuple[Wallet, ...]:
        """Return one account's wallets in the arrangement its owner chose."""
        models = self._session.scalars(
            select(WalletModel)
            .where(WalletModel.financial_account_id == account_id)
            .order_by(WalletModel.display_order, WalletModel.id)
        ).all()
        return tuple(_wallet_record(model) for model in models)

    def set_display_order(self, wallet_id: int, position: int) -> None:
        """Place one wallet at a position in its account's sheet column.

        Nothing else about the wallet changes, and its version is deliberately
        left alone: an arrangement is presentation, so reordering a column must
        not make a concurrent configuration edit look like a conflict.
        """
        self._session.execute(
            update(WalletModel)
            .where(WalletModel.id == wallet_id)
            .values(display_order=position)
        )

    def rename(self, *, wallet_id: int, name: str, expected_version: int) -> Wallet:
        """Apply one optimistic rename; balance, purpose, and order untouched."""
        model = self._session.get(WalletModel, wallet_id)
        if model is None:
            raise LedgerConfigurationError("wallet was not found")
        if model.version != expected_version:
            raise ConcurrentUpdateError("wallet changed after this page was loaded")
        model.name = normalize_name(name, label="wallet name", max_length=200)
        model.version = model.version + 1
        _flush_and_refresh(self._session, model)
        return _wallet_record(model)


class SqlAlchemyLedgerAccountRepository:
    """SQLAlchemy repository for the double-entry chart of accounts."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, draft: LedgerAccountDraft) -> LedgerAccount:
        """Persist one ledger account identity."""
        model = LedgerAccountModel(
            code=normalize_name(draft.code, label="ledger code", max_length=200),
            name=normalize_name(draft.name, label="ledger name", max_length=200),
            kind=draft.kind.value,
            currency=normalize_currency(draft.currency),
            financial_account_id=draft.financial_account_id,
            budget_category_id=draft.budget_category_id,
        )
        self._session.add(model)
        _flush_and_refresh(self._session, model)
        return _ledger_account_record(model)

    def get(self, ledger_account_id: int) -> LedgerAccount | None:
        """Return one ledger account by internal ID."""
        model = self._session.get(LedgerAccountModel, ledger_account_id)
        return None if model is None else _ledger_account_record(model)

    def get_by_code(self, code: str) -> LedgerAccount | None:
        """Return one ledger account by its unique code."""
        model = self._session.scalar(
            select(LedgerAccountModel).where(LedgerAccountModel.code == code.strip())
        )
        return None if model is None else _ledger_account_record(model)

    def get_for_financial_account(self, account_id: int) -> LedgerAccount | None:
        """Return the single ledger account backing one physical account."""
        model = self._session.scalar(
            select(LedgerAccountModel).where(
                LedgerAccountModel.financial_account_id == account_id
            )
        )
        return None if model is None else _ledger_account_record(model)

    def get_for_budget_category(
        self, category_id: int, currency: str
    ) -> LedgerAccount | None:
        """Return the single expense identity of one category and currency."""
        model = self._session.scalar(
            select(LedgerAccountModel).where(
                LedgerAccountModel.budget_category_id == category_id,
                LedgerAccountModel.currency == normalize_currency(currency),
            )
        )
        return None if model is None else _ledger_account_record(model)

    def list_all(self) -> tuple[LedgerAccount, ...]:
        """Return the whole chart of accounts ordered by code."""
        models = self._session.scalars(
            select(LedgerAccountModel).order_by(LedgerAccountModel.code)
        ).all()
        return tuple(_ledger_account_record(model) for model in models)


class SqlAlchemyJournalRepository:
    """SQLAlchemy repository for immutable balanced journal entries."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, draft: JournalEntryDraft) -> JournalEntry:
        """Insert one entry and its postings; posted rows are never updated."""
        key = draft.idempotency_key.strip()
        if not key:
            raise ValueError("journal idempotency key must not be empty")
        if len(key) > _MAX_IDEMPOTENCY_KEY_LENGTH:
            raise ValueError("journal idempotency key is too long")
        entry = JournalEntryModel(
            entry_type=draft.entry_type.value,
            effective_at=draft.effective_at,
            recorded_at=draft.recorded_at,
            idempotency_key=key,
            reversal_of_id=draft.reversal_of_id,
            description=draft.description.strip(),
            financial_event_id=draft.financial_event_id,
        )
        self._session.add(entry)
        _flush_and_refresh(self._session, entry)
        postings = tuple(
            JournalPostingModel(
                journal_entry_id=entry.id,
                ledger_account_id=posting.ledger_account_id,
                wallet_id=posting.wallet_id,
                amount_minor=posting.amount.amount_minor,
                currency=posting.amount.currency,
                budget_category_id=posting.budget_category_id,
            )
            for posting in draft.postings
        )
        self._session.add_all(postings)
        for posting_model in postings:
            _flush_and_refresh(self._session, posting_model)
        return journal_entry_record(entry, postings)

    def get(self, entry_id: int) -> JournalEntry | None:
        """Return one entry with its postings."""
        model = self._session.get(JournalEntryModel, entry_id)
        return None if model is None else self._with_postings(model)

    def get_by_idempotency_key(self, key: str) -> JournalEntry | None:
        """Return the entry already posted under one stable key."""
        model = self._session.scalar(
            select(JournalEntryModel).where(
                JournalEntryModel.idempotency_key == key.strip()
            )
        )
        return None if model is None else self._with_postings(model)

    def get_reversal_of(self, entry_id: int) -> JournalEntry | None:
        """Return the entry that already reverses one posted entry."""
        model = self._session.scalar(
            select(JournalEntryModel).where(
                JournalEntryModel.reversal_of_id == entry_id
            )
        )
        return None if model is None else self._with_postings(model)

    def get_by_financial_event(self, event_id: int) -> JournalEntry | None:
        """Return the entry currently in force for one financial event.

        An allocation may be redone (ADR-B103), which leaves the superseded
        entry and the reversal that cancelled it both in place, so an event
        can carry more than one primary entry over its life. The one in force
        is the newest that has not itself been reversed; where an event has
        only ever been posted once, that is the entry it always was.
        """
        cancelled = select(JournalEntryModel.reversal_of_id).where(
            JournalEntryModel.reversal_of_id.is_not(None)
        )
        model = self._session.scalar(
            select(JournalEntryModel)
            .where(
                JournalEntryModel.financial_event_id == event_id,
                JournalEntryModel.id.not_in(cancelled),
            )
            .order_by(JournalEntryModel.id.desc())
        )
        return None if model is None else self._with_postings(model)

    def latest_by_financial_event(self, event_id: int) -> JournalEntry | None:
        """Return one event's newest primary entry, cancelled or not."""
        model = self._session.scalar(
            select(JournalEntryModel)
            .where(JournalEntryModel.financial_event_id == event_id)
            .order_by(JournalEntryModel.id.desc())
        )
        return None if model is None else self._with_postings(model)

    def _with_postings(self, model: JournalEntryModel) -> JournalEntry:
        postings = self._session.scalars(
            select(JournalPostingModel)
            .where(JournalPostingModel.journal_entry_id == model.id)
            .order_by(JournalPostingModel.id)
        ).all()
        return journal_entry_record(model, tuple(postings))


_REASON_SCHEMA_VERSION = 1


def _reason_payload(
    reasons: tuple[SourceLinkReason, ...],
    discrepancies: tuple[SourceDiscrepancy, ...],
) -> dict[str, Any]:
    """Return the bounded, non-sensitive reason document stored per link."""
    return {
        "schema_version": _REASON_SCHEMA_VERSION,
        "reasons": sorted({item.value for item in reasons}),
        "discrepancies": sorted({item.value for item in discrepancies}),
    }


def _decode_codes[T: StrEnum](
    payload: Any, key: str, enum_type: type[T]
) -> tuple[T, ...]:
    """Decode stored codes, ignoring values a later revision no longer knows."""
    if not isinstance(payload, dict):
        return ()
    raw = payload.get(key)
    if not isinstance(raw, list):
        return ()
    decoded: list[T] = []
    for item in raw:
        try:
            decoded.append(enum_type(item))
        except ValueError:
            continue
    return tuple(decoded)


def card_record(model: CardModel) -> Card:
    return Card(
        id=model.id,
        gmail_account_id=model.gmail_account_id,
        issuer_code=model.issuer_code,
        display_name=model.display_name,
        last_four=model.last_four,
        holder_role=CardHolderRole(model.holder_role),
        configuration_status=CardConfigurationStatus(model.configuration_status),
        liability_account_id=model.liability_account_id,
        settlement_account_id=model.settlement_account_id,
        default_funding_wallet_id=model.default_funding_wallet_id,
        active_from=model.active_from,
        active_to=model.active_to,
        version=model.version,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def card_import_assignment_record(
    model: CardImportAssignmentModel,
) -> CardImportAssignment:
    """Translate one explicit imported-hint assignment."""
    return CardImportAssignment(
        id=model.id,
        import_row_id=model.import_row_id,
        card_id=model.card_id,
        assignment_kind=CardImportAssignmentKind(model.assignment_kind),
        statement_day=model.statement_day,
        due_day=model.due_day,
        autopay_day=model.autopay_day,
        version=model.version,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def financial_event_record(model: FinancialEventModel) -> FinancialEvent:
    return FinancialEvent(
        id=model.id,
        origin=FinancialEventOrigin(model.origin),
        source_import_key=model.source_import_key,
        event_type=FinancialEventType(model.event_type),
        payment_method=PaymentMethod(model.payment_method),
        recognition_basis=RecognitionBasis(model.recognition_basis),
        occurred_at=model.occurred_at,
        merchant=model.merchant,
        amount_minor=model.amount_minor,
        currency=model.currency,
        card_id=model.card_id,
        status=FinancialEventStatus(model.status),
        note=model.note,
        version=model.version,
        idempotency_key=model.idempotency_key,
        refund_of_event_id=model.refund_of_event_id,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def source_link_record(
    model: FinancialEventSourceLinkModel,
) -> FinancialEventSourceLink:
    return FinancialEventSourceLink(
        id=model.id,
        financial_event_id=model.financial_event_id,
        transaction_id=model.transaction_id,
        status=SourceLinkStatus(model.status),
        match_score_bps=model.match_score_bps,
        reasons=_decode_codes(model.reason_json, "reasons", SourceLinkReason),
        discrepancies=_decode_codes(
            model.reason_json, "discrepancies", SourceDiscrepancy
        ),
        reviewed_at=model.reviewed_at,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class SqlAlchemyCardRepository:
    """SQLAlchemy repository for local card configuration."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, draft: CardDraft) -> Card:
        """Register one card identified only by institution and last four."""
        model = CardModel(
            gmail_account_id=draft.gmail_account_id,
            issuer_code=normalize_name(
                draft.issuer_code, label="issuer code", max_length=100
            ),
            display_name=normalize_name(
                draft.display_name, label="card display name", max_length=200
            ),
            last_four=normalize_last_four(draft.last_four),
            holder_role=draft.holder_role.value,
            configuration_status=CardConfigurationStatus.INCOMPLETE.value,
        )
        self._session.add(model)
        _flush_and_refresh(self._session, model)
        return card_record(model)

    def get(self, card_id: int) -> Card | None:
        """Return one card by internal ID."""
        model = self._session.get(CardModel, card_id)
        return None if model is None else card_record(model)

    def get_by_identity(self, identity: CardIdentity) -> Card | None:
        """Return the active card matching one issuer and last-four pair."""
        model = self._session.scalar(
            select(CardModel).where(
                CardModel.gmail_account_id == identity.gmail_account_id,
                CardModel.issuer_code == identity.issuer_code.strip(),
                CardModel.last_four == identity.last_four.strip(),
                CardModel.active_to.is_(None),
            )
        )
        return None if model is None else card_record(model)

    def list_all(self) -> tuple[Card, ...]:
        """Return every configured card ordered by issuer and last four."""
        models = self._session.scalars(
            select(CardModel).order_by(
                CardModel.issuer_code, CardModel.last_four, CardModel.id
            )
        ).all()
        return tuple(card_record(model) for model in models)

    def list_active_by_last_four(
        self, *, gmail_account_id: int, last_four: str
    ) -> tuple[Card, ...]:
        """Return every active card sharing one last-four across issuers."""
        return _card_list_active_by_last_four(
            self._session, gmail_account_id=gmail_account_id, last_four=last_four
        )

    def save_configuration(self, draft: CardConfigurationDraft) -> Card:
        """Apply one optimistic settlement configuration change."""
        model = self._session.get(CardModel, draft.card_id)
        if model is None:
            raise CardConfigurationError("card was not found")
        if model.version != draft.expected_version:
            raise ConcurrentUpdateError("card changed after this page was loaded")
        model.display_name = normalize_name(
            draft.display_name, label="card display name", max_length=200
        )
        model.holder_role = draft.holder_role.value
        model.liability_account_id = draft.liability_account_id
        model.settlement_account_id = draft.settlement_account_id
        model.default_funding_wallet_id = draft.default_funding_wallet_id
        model.configuration_status = card_configuration_status(
            liability_account_id=draft.liability_account_id,
            settlement_account_id=draft.settlement_account_id,
        ).value
        model.version = model.version + 1
        _flush_and_refresh(self._session, model)
        return card_record(model)


class SqlAlchemyCardImportAssignmentRepository:
    """SQLAlchemy persistence for user-approved cutover hint mappings."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, draft: CardImportAssignmentDraft) -> CardImportAssignment:
        """Attach an unassigned immutable source row to one card."""
        model = CardImportAssignmentModel(
            import_row_id=draft.import_row_id,
            card_id=draft.card_id,
            assignment_kind=draft.assignment_kind.value,
            statement_day=draft.statement_day,
            due_day=draft.due_day,
            autopay_day=draft.autopay_day,
        )
        self._session.add(model)
        _flush_and_refresh(self._session, model)
        return card_import_assignment_record(model)

    def get_for_import_row(self, import_row_id: int) -> CardImportAssignment | None:
        """Return the assignment for one immutable import row."""
        model = self._session.scalar(
            select(CardImportAssignmentModel).where(
                CardImportAssignmentModel.import_row_id == import_row_id
            )
        )
        return None if model is None else card_import_assignment_record(model)

    def list_all(self) -> tuple[CardImportAssignment, ...]:
        """Return every assignment in source-row order."""
        models = self._session.scalars(
            select(CardImportAssignmentModel).order_by(
                CardImportAssignmentModel.import_row_id,
                CardImportAssignmentModel.id,
            )
        ).all()
        return tuple(card_import_assignment_record(model) for model in models)


class SqlAlchemyFinancialEventRepository:
    """SQLAlchemy repository for canonical economic events."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, draft: FinancialEventDraft) -> FinancialEvent:
        """Persist one canonical event; posting belongs to a later milestone."""
        key = draft.idempotency_key.strip()
        if not key:
            raise ValueError("financial event idempotency key must not be empty")
        if len(key) > _MAX_IDEMPOTENCY_KEY_LENGTH:
            raise ValueError("financial event idempotency key is too long")
        model = FinancialEventModel(
            origin=draft.origin.value,
            source_import_key=draft.source_import_key,
            event_type=draft.event_type.value,
            payment_method=draft.payment_method.value,
            recognition_basis=draft.recognition_basis.value,
            occurred_at=draft.occurred_at,
            merchant=draft.merchant,
            amount_minor=draft.amount_minor,
            currency=draft.currency,
            card_id=draft.card_id,
            status=draft.status.value,
            note=draft.note,
            idempotency_key=key,
            refund_of_event_id=draft.refund_of_event_id,
        )
        self._session.add(model)
        _flush_and_refresh(self._session, model)
        return financial_event_record(model)

    def get(self, event_id: int) -> FinancialEvent | None:
        """Return one event by internal ID."""
        model = self._session.get(FinancialEventModel, event_id)
        return None if model is None else financial_event_record(model)

    def get_by_idempotency_key(self, key: str) -> FinancialEvent | None:
        """Return the event already staged under one stable key."""
        model = self._session.scalar(
            select(FinancialEventModel).where(
                FinancialEventModel.idempotency_key == key.strip()
            )
        )
        return None if model is None else financial_event_record(model)

    def list_by_status(
        self, statuses: frozenset[FinancialEventStatus]
    ) -> tuple[FinancialEvent, ...]:
        """Return events in the requested review states, newest first."""
        if not statuses:
            return ()
        models = self._session.scalars(
            select(FinancialEventModel)
            .where(
                FinancialEventModel.status.in_(
                    sorted(status.value for status in statuses)
                )
            )
            .order_by(
                FinancialEventModel.occurred_at.desc(), FinancialEventModel.id.desc()
            )
        ).all()
        return tuple(financial_event_record(model) for model in models)

    def mark_posted(self, event_id: int, expected_version: int) -> FinancialEvent:
        """Move one event to posted exactly once, under optimistic control."""
        result = cast(
            CursorResult[Any],
            self._session.execute(
                update(FinancialEventModel)
                .where(
                    FinancialEventModel.id == event_id,
                    FinancialEventModel.version == expected_version,
                    FinancialEventModel.status.in_(
                        (
                            FinancialEventStatus.DRAFT.value,
                            FinancialEventStatus.NEEDS_ALLOCATION.value,
                            FinancialEventStatus.READY.value,
                        )
                    ),
                )
                .values(
                    status=FinancialEventStatus.POSTED.value,
                    version=FinancialEventModel.version + 1,
                )
            ),
        )
        if result.rowcount != 1:
            model = self._session.get(FinancialEventModel, event_id)
            if model is None:
                raise FinancialEventError("financial event was not found")
            if model.status == FinancialEventStatus.POSTED.value:
                raise FinancialEventError("this event is already posted")
            raise ConcurrentUpdateError("event changed after this page was loaded")
        model = self._session.get(FinancialEventModel, event_id)
        if model is None:  # pragma: no cover - just updated
            raise FinancialEventError("financial event disappeared")
        self._session.refresh(model)
        return financial_event_record(model)

    def mark_needs_allocation(
        self, event_id: int, expected_version: int
    ) -> FinancialEvent:
        """Send one posted event back to the allocation queue, conditionally.

        Status and version are both part of the statement, so two concurrent
        redo attempts cannot each read ``posted`` and each stage a reversal.
        """
        result = cast(
            CursorResult[Any],
            self._session.execute(
                update(FinancialEventModel)
                .where(
                    FinancialEventModel.id == event_id,
                    FinancialEventModel.version == expected_version,
                    FinancialEventModel.status == FinancialEventStatus.POSTED.value,
                )
                .values(
                    status=FinancialEventStatus.NEEDS_ALLOCATION.value,
                    version=FinancialEventModel.version + 1,
                )
            ),
        )
        if result.rowcount != 1:
            model = self._session.get(FinancialEventModel, event_id)
            if model is None:
                raise FinancialEventError("financial event was not found")
            if model.status != FinancialEventStatus.POSTED.value:
                raise FinancialEventError(
                    f"a {model.status} event has no allocation to redo"
                )
            raise ConcurrentUpdateError("event changed after this page was loaded")
        model = self._session.get(FinancialEventModel, event_id)
        if model is None:  # pragma: no cover - just updated
            raise FinancialEventError("financial event disappeared")
        self._session.refresh(model)
        return financial_event_record(model)

    def mark_reversed(self, event_id: int, expected_version: int) -> FinancialEvent:
        """Move one posted event to reversed exactly once, conditionally.

        The status and version are both part of the statement, so two
        concurrent reversals cannot each read ``posted`` and each proceed to
        stage an entry.
        """
        result = cast(
            CursorResult[Any],
            self._session.execute(
                update(FinancialEventModel)
                .where(
                    FinancialEventModel.id == event_id,
                    FinancialEventModel.version == expected_version,
                    FinancialEventModel.status == FinancialEventStatus.POSTED.value,
                )
                .values(
                    status=FinancialEventStatus.REVERSED.value,
                    version=FinancialEventModel.version + 1,
                )
            ),
        )
        if result.rowcount != 1:
            model = self._session.get(FinancialEventModel, event_id)
            if model is None:
                raise FinancialEventError("financial event was not found")
            if model.status == FinancialEventStatus.REVERSED.value:
                raise FinancialEventError("this event is already reversed")
            if model.status != FinancialEventStatus.POSTED.value:
                raise FinancialEventError(
                    f"a {model.status} event has nothing posted to reverse"
                )
            raise ConcurrentUpdateError("event changed after this page was loaded")
        model = self._session.get(FinancialEventModel, event_id)
        if model is None:  # pragma: no cover - just updated
            raise FinancialEventError("financial event disappeared")
        self._session.refresh(model)
        return financial_event_record(model)

    def save_note(self, change: FinancialEventNoteUpdate) -> FinancialEvent:
        """Conditionally edit only the user-owned note, never accounting facts."""
        result = cast(
            CursorResult[Any],
            self._session.execute(
                update(FinancialEventModel)
                .where(
                    FinancialEventModel.id == change.event_id,
                    FinancialEventModel.version == change.expected_version,
                )
                .values(
                    note=change.note,
                    version=FinancialEventModel.version + 1,
                )
            ),
        )
        if result.rowcount != 1:
            if self._session.get(FinancialEventModel, change.event_id) is None:
                raise FinancialEventError("financial event was not found")
            raise ConcurrentUpdateError("event changed after this page was loaded")
        model = self._session.get(FinancialEventModel, change.event_id)
        if model is None:  # pragma: no cover - just updated
            raise FinancialEventError("financial event disappeared")
        self._session.refresh(model)
        return financial_event_record(model)

    def list_match_candidates(
        self,
        *,
        origin: FinancialEventOrigin,
        payment_method: PaymentMethod,
        status: FinancialEventStatus,
        currency: str,
        amount_minor: int,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple[FinancialEvent, ...]:
        """Return events that could describe the same money as one source.

        Amount and currency are exact gates rather than ranges, and the time
        window is bounded, so this can never widen into a fuzzy search however
        it is called (ADR-B062).
        """
        models = self._session.scalars(
            select(FinancialEventModel)
            .where(
                FinancialEventModel.origin == origin.value,
                FinancialEventModel.payment_method == payment_method.value,
                FinancialEventModel.status == status.value,
                FinancialEventModel.currency == normalize_currency(currency),
                FinancialEventModel.amount_minor == amount_minor,
                FinancialEventModel.occurred_at >= window_start,
                FinancialEventModel.occurred_at <= window_end,
            )
            .order_by(FinancialEventModel.occurred_at, FinancialEventModel.id)
        ).all()
        return tuple(financial_event_record(model) for model in models)

    def list_posted_refunds(self, event_id: int) -> tuple[FinancialEvent, ...]:
        """Return the posted refunds already recorded against one event.

        Read inside the caller's transaction so a cumulative cap is decided
        against the same snapshot the new refund is written into.
        """
        models = self._session.scalars(
            select(FinancialEventModel)
            .where(
                FinancialEventModel.refund_of_event_id == event_id,
                FinancialEventModel.status == FinancialEventStatus.POSTED.value,
            )
            .order_by(FinancialEventModel.id)
        ).all()
        return tuple(financial_event_record(model) for model in models)

    def refresh_source_values(
        self, event_id: int, update: FinancialEventUpdate
    ) -> FinancialEvent:
        """Refresh parser-owned values on an event that is not yet posted."""
        model = self._session.get(FinancialEventModel, event_id)
        if model is None:
            raise FinancialEventError("financial event was not found")
        if not event_accepts_source_refresh(FinancialEventStatus(model.status)):
            raise FinancialEventError(
                "a posted, reversed, or ignored event is never refreshed"
            )
        model.event_type = update.event_type.value
        model.payment_method = update.payment_method.value
        model.occurred_at = update.occurred_at
        model.amount_minor = update.amount_minor
        model.currency = update.currency
        model.merchant = update.merchant
        model.card_id = update.card_id
        model.version = model.version + 1
        _flush_and_refresh(self._session, model)
        return financial_event_record(model)


class SqlAlchemyFinancialEventSourceLinkRepository:
    """SQLAlchemy repository for event-to-transaction provenance."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, draft: SourceLinkDraft) -> FinancialEventSourceLink:
        """Persist one provenance row; it never creates a journal entry."""
        if not 0 <= draft.match_score_bps <= MAX_SCORE_BPS:
            raise SourceLinkError("match score must be between 0 and 10000 bps")
        model = FinancialEventSourceLinkModel(
            financial_event_id=draft.financial_event_id,
            transaction_id=draft.transaction_id,
            status=draft.status.value,
            match_score_bps=draft.match_score_bps,
            reason_json=_reason_payload(draft.reasons, ()),
            reviewed_at=draft.reviewed_at,
        )
        self._session.add(model)
        _flush_and_refresh(self._session, model)
        return source_link_record(model)

    def get(self, link_id: int) -> FinancialEventSourceLink | None:
        """Return one source link by internal ID."""
        model = self._session.get(FinancialEventSourceLinkModel, link_id)
        return None if model is None else source_link_record(model)

    def get_confirmed_for_transaction(
        self, transaction_id: int
    ) -> FinancialEventSourceLink | None:
        """Return the single confirmed link owning one parsed transaction."""
        model = self._session.scalar(
            select(FinancialEventSourceLinkModel).where(
                FinancialEventSourceLinkModel.transaction_id == transaction_id,
                FinancialEventSourceLinkModel.status
                == SourceLinkStatus.CONFIRMED.value,
            )
        )
        return None if model is None else source_link_record(model)

    def list_for_event(self, event_id: int) -> tuple[FinancialEventSourceLink, ...]:
        """Return every link belonging to one event."""
        models = self._session.scalars(
            select(FinancialEventSourceLinkModel)
            .where(FinancialEventSourceLinkModel.financial_event_id == event_id)
            .order_by(FinancialEventSourceLinkModel.id)
        ).all()
        return tuple(source_link_record(model) for model in models)

    def list_for_transaction(
        self, transaction_id: int
    ) -> tuple[FinancialEventSourceLink, ...]:
        """Return every link claiming one parsed transaction, decided or not."""
        models = self._session.scalars(
            select(FinancialEventSourceLinkModel)
            .where(FinancialEventSourceLinkModel.transaction_id == transaction_id)
            .order_by(FinancialEventSourceLinkModel.id)
        ).all()
        return tuple(source_link_record(model) for model in models)

    def list_by_status(
        self, statuses: frozenset[SourceLinkStatus], *, limit: int, offset: int = 0
    ) -> tuple[FinancialEventSourceLink, ...]:
        """Return links awaiting or holding one review decision, newest first."""
        if not statuses:
            return ()
        models = self._session.scalars(
            select(FinancialEventSourceLinkModel)
            .where(
                FinancialEventSourceLinkModel.status.in_(
                    sorted(status.value for status in statuses)
                )
            )
            .order_by(FinancialEventSourceLinkModel.id.desc())
            .limit(max(0, limit))
            .offset(max(0, offset))
        ).all()
        return tuple(source_link_record(model) for model in models)

    def count_by_status(self, statuses: frozenset[SourceLinkStatus]) -> int:
        """Return how many links hold one of the requested review states."""
        if not statuses:
            return 0
        total = self._session.execute(
            select(func.count(FinancialEventSourceLinkModel.id)).where(
                FinancialEventSourceLinkModel.status.in_(
                    sorted(status.value for status in statuses)
                )
            )
        ).scalar_one()
        return int(total)

    def upsert_candidate(self, draft: SourceLinkDraft) -> FinancialEventSourceLink:
        """Record one proposed pairing without ever reopening a decision.

        A retry of the same intake must not create a second candidate row, and
        it must not resurrect a pairing a person already confirmed or rejected.
        An existing decided link is therefore returned unchanged, and only a
        still-undecided candidate has its score and reasons refreshed.
        """
        if draft.status is not SourceLinkStatus.CANDIDATE:
            raise SourceLinkError("only a candidate pairing is upserted")
        existing = self._session.scalar(
            select(FinancialEventSourceLinkModel).where(
                FinancialEventSourceLinkModel.financial_event_id
                == draft.financial_event_id,
                FinancialEventSourceLinkModel.transaction_id == draft.transaction_id,
            )
        )
        if existing is None:
            return self.add(draft)
        if existing.status != SourceLinkStatus.CANDIDATE.value:
            return source_link_record(existing)
        current = source_link_record(existing)
        existing.match_score_bps = draft.match_score_bps
        existing.reason_json = _reason_payload(draft.reasons, current.discrepancies)
        _flush_and_refresh(self._session, existing)
        return source_link_record(existing)

    def confirm(
        self,
        link_id: int,
        *,
        reviewed_at: datetime,
        reasons: tuple[SourceLinkReason, ...],
    ) -> FinancialEventSourceLink:
        """Promote one still-undecided candidate to confirmed, conditionally.

        The current status is part of the statement, so two reviewers cannot
        both read ``candidate`` and both confirm; the second one is told the
        decision was already made.
        """
        return self._decide(
            link_id,
            target=SourceLinkStatus.CONFIRMED,
            reviewed_at=reviewed_at,
            reasons=reasons,
        )

    def reject(
        self,
        link_id: int,
        *,
        reviewed_at: datetime,
        reasons: tuple[SourceLinkReason, ...],
    ) -> FinancialEventSourceLink:
        """Record that one candidate pairing is not the same money."""
        return self._decide(
            link_id,
            target=SourceLinkStatus.REJECTED,
            reviewed_at=reviewed_at,
            reasons=reasons,
        )

    def _decide(
        self,
        link_id: int,
        *,
        target: SourceLinkStatus,
        reviewed_at: datetime,
        reasons: tuple[SourceLinkReason, ...],
    ) -> FinancialEventSourceLink:
        model = self._session.get(FinancialEventSourceLinkModel, link_id)
        if model is None:
            raise SourceLinkError("source link was not found")
        current = source_link_record(model)
        if current.status is target:
            return current
        payload = _reason_payload(current.reasons + reasons, current.discrepancies)
        result = cast(
            CursorResult[Any],
            self._session.execute(
                update(FinancialEventSourceLinkModel)
                .where(
                    FinancialEventSourceLinkModel.id == link_id,
                    FinancialEventSourceLinkModel.status
                    == SourceLinkStatus.CANDIDATE.value,
                )
                .values(
                    status=target.value,
                    reviewed_at=reviewed_at,
                    reason_json=payload,
                )
            ),
        )
        if result.rowcount != 1:
            raise ConcurrentUpdateError(
                "this source link was already decided; reload before deciding"
            )
        self._session.expire(model)
        refreshed = self._session.get(FinancialEventSourceLinkModel, link_id)
        if refreshed is None:  # pragma: no cover - just updated
            raise SourceLinkError("source link disappeared")
        return source_link_record(refreshed)

    def list_for_email(self, email_id: int) -> tuple[FinancialEventSourceLink, ...]:
        """Return every link whose parsed transaction belongs to one email."""
        models = self._session.scalars(
            select(FinancialEventSourceLinkModel)
            .join(
                TransactionModel,
                TransactionModel.id == FinancialEventSourceLinkModel.transaction_id,
            )
            .where(TransactionModel.email_id == email_id)
            .order_by(FinancialEventSourceLinkModel.id)
        ).all()
        return tuple(source_link_record(model) for model in models)

    def replace_reasons(
        self, link_id: int, reasons: tuple[SourceLinkReason, ...]
    ) -> FinancialEventSourceLink:
        """Replace current mapping reasons without discarding discrepancies."""
        model = self._session.get(FinancialEventSourceLinkModel, link_id)
        if model is None:
            raise SourceLinkError("source link was not found")
        existing = source_link_record(model)
        if existing.reasons == reasons:
            return existing
        model.reason_json = _reason_payload(reasons, existing.discrepancies)
        _flush_and_refresh(self._session, model)
        return source_link_record(model)

    def record_discrepancies(
        self, link_id: int, discrepancies: tuple[SourceDiscrepancy, ...]
    ) -> FinancialEventSourceLink:
        """Merge stable discrepancy codes without discarding match reasons."""
        model = self._session.get(FinancialEventSourceLinkModel, link_id)
        if model is None:
            raise SourceLinkError("source link was not found")
        existing = source_link_record(model)
        model.reason_json = _reason_payload(
            existing.reasons, existing.discrepancies + discrepancies
        )
        _flush_and_refresh(self._session, model)
        return source_link_record(model)


def _card_list_active_by_last_four(
    session: Session, *, gmail_account_id: int, last_four: str
) -> tuple[Card, ...]:
    models = session.scalars(
        select(CardModel)
        .where(
            CardModel.gmail_account_id == gmail_account_id,
            CardModel.last_four == last_four.strip(),
            CardModel.active_to.is_(None),
        )
        .order_by(CardModel.issuer_code, CardModel.id)
    ).all()
    return tuple(card_record(model) for model in models)


def budget_category_record(model: BudgetCategoryModel) -> BudgetCategory:
    """Translate one persisted category into its domain record."""
    return BudgetCategory(
        id=model.id,
        name=model.name,
        parent_id=model.parent_id,
        is_active=model.is_active,
        version=model.version,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def budget_period_record(model: BudgetPeriodModel) -> BudgetPeriod:
    """Translate one persisted budget month into its domain record."""
    return BudgetPeriod(
        id=model.id,
        year=model.year,
        month=model.month,
        currency=model.currency,
        status=BudgetPeriodStatus(model.status),
        closed_at=model.closed_at,
        version=model.version,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def budget_target_record(model: BudgetTargetModel) -> BudgetTarget:
    """Translate one persisted target into its domain record."""
    return BudgetTarget(
        id=model.id,
        budget_period_id=model.budget_period_id,
        budget_category_id=model.budget_category_id,
        target_minor=model.target_minor,
        buffer_minor=model.buffer_minor,
        funding_wallet_id=model.funding_wallet_id,
        month_end_destination_wallet_id=model.month_end_destination_wallet_id,
        version=model.version,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def event_allocation_record(model: EventAllocationModel) -> EventAllocation:
    """Translate one persisted allocation into its domain record."""
    return EventAllocation(
        id=model.id,
        financial_event_id=model.financial_event_id,
        budget_category_id=model.budget_category_id,
        funding_wallet_id=model.funding_wallet_id,
        amount_minor=model.amount_minor,
        currency=model.currency,
        allocation_source=AllocationSource(model.allocation_source),
        created_at=model.created_at,
        updated_at=model.updated_at,
        journal_entry_id=model.journal_entry_id,
    )


def dedicated_wallet_allocation_record(
    model: DedicatedWalletAllocationModel,
) -> DedicatedWalletAllocation:
    """Translate one immutable dedicated-wallet routing decision."""
    return DedicatedWalletAllocation(
        id=model.id,
        event_allocation_id=model.event_allocation_id,
        clearing_wallet_id=model.clearing_wallet_id,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def funding_commitment_record(model: FundingCommitmentModel) -> FundingCommitment:
    """Translate one persisted commitment into its domain record."""
    return FundingCommitment(
        id=model.id,
        financial_event_id=model.financial_event_id,
        event_allocation_id=model.event_allocation_id,
        source_wallet_id=model.source_wallet_id,
        settlement_account_id=model.settlement_account_id,
        amount_minor=model.amount_minor,
        released_minor=model.released_minor,
        currency=model.currency,
        fulfillment_mode=CommitmentFulfillmentMode(model.fulfillment_mode),
        status=CommitmentStatus(model.status),
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class SqlAlchemyBudgetCategoryRepository:
    """SQLAlchemy repository for hierarchical spending purposes."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, draft: BudgetCategoryDraft) -> BudgetCategory:
        """Create one category under an optional parent."""
        model = BudgetCategoryModel(
            name=normalize_name(draft.name, label="category name", max_length=200),
            parent_id=draft.parent_id,
        )
        self._session.add(model)
        _flush_and_refresh(self._session, model)
        return budget_category_record(model)

    def get(self, category_id: int) -> BudgetCategory | None:
        """Return one category by internal ID."""
        model = self._session.get(BudgetCategoryModel, category_id)
        return None if model is None else budget_category_record(model)

    def list_all(self) -> tuple[BudgetCategory, ...]:
        """Return every category ordered by parent and name."""
        models = self._session.scalars(
            select(BudgetCategoryModel).order_by(
                BudgetCategoryModel.parent_id, BudgetCategoryModel.name
            )
        ).all()
        return tuple(budget_category_record(model) for model in models)

    def save(self, change: BudgetCategoryUpdate) -> BudgetCategory:
        """Apply one optimistic rename, re-parent, or activation change.

        The version check is part of the statement, so an interleaved writer
        cannot slip between the read and the write.
        """
        name = normalize_name(change.name, label="category name", max_length=200)
        result = cast(
            CursorResult[Any],
            self._session.execute(
                update(BudgetCategoryModel)
                .where(
                    BudgetCategoryModel.id == change.category_id,
                    BudgetCategoryModel.version == change.expected_version,
                )
                .values(
                    name=name,
                    parent_id=change.parent_id,
                    is_active=change.is_active,
                    version=BudgetCategoryModel.version + 1,
                )
            ),
        )
        if result.rowcount != 1:
            if self._session.get(BudgetCategoryModel, change.category_id) is None:
                raise BudgetPeriodError("budget category was not found")
            raise ConcurrentUpdateError("category changed after this page was loaded")
        model = self._session.get(BudgetCategoryModel, change.category_id)
        if model is None:  # pragma: no cover - just updated
            raise BudgetPeriodError("budget category disappeared")
        self._session.refresh(model)
        return budget_category_record(model)

    def is_referenced(self, category_id: int) -> bool:
        """Return whether any target, allocation, or posting still uses it."""
        for statement in (
            select(BudgetTargetModel.id).where(
                BudgetTargetModel.budget_category_id == category_id
            ),
            select(EventAllocationModel.id).where(
                EventAllocationModel.budget_category_id == category_id
            ),
            select(JournalPostingModel.id).where(
                JournalPostingModel.budget_category_id == category_id
            ),
            select(BudgetCategoryModel.id).where(
                BudgetCategoryModel.parent_id == category_id
            ),
        ):
            if self._session.scalar(statement) is not None:
                return True
        return False


class SqlAlchemyBudgetPeriodRepository:
    """SQLAlchemy repository for calendar-month budget periods."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_or_create(self, *, year: int, month: int, currency: str) -> BudgetPeriod:
        """Return the month, creating it open when it does not exist yet."""
        code = normalize_currency(currency)
        model = self._session.scalar(
            select(BudgetPeriodModel).where(
                BudgetPeriodModel.year == year,
                BudgetPeriodModel.month == month,
                BudgetPeriodModel.currency == code,
            )
        )
        if model is None:
            model = BudgetPeriodModel(
                year=year,
                month=month,
                currency=code,
                status=BudgetPeriodStatus.OPEN.value,
            )
            self._session.add(model)
            _flush_and_refresh(self._session, model)
        return budget_period_record(model)

    def get(self, period_id: int) -> BudgetPeriod | None:
        """Return one period by internal ID."""
        model = self._session.get(BudgetPeriodModel, period_id)
        return None if model is None else budget_period_record(model)

    def find(self, *, year: int, month: int, currency: str) -> BudgetPeriod | None:
        """Return one period without creating it."""
        model = self._session.scalar(
            select(BudgetPeriodModel).where(
                BudgetPeriodModel.year == year,
                BudgetPeriodModel.month == month,
                BudgetPeriodModel.currency == normalize_currency(currency),
            )
        )
        return None if model is None else budget_period_record(model)

    def close(self, period_id: int, *, closed_at: datetime) -> BudgetPeriod:
        """Mark one open period closed exactly once."""
        model = self._session.get(BudgetPeriodModel, period_id)
        if model is None:
            raise BudgetPeriodError("budget period was not found")
        if model.status == BudgetPeriodStatus.CLOSED.value:
            raise BudgetPeriodError("this budget period is already closed")
        model.status = BudgetPeriodStatus.CLOSED.value
        model.closed_at = closed_at
        model.version = model.version + 1
        _flush_and_refresh(self._session, model)
        return budget_period_record(model)


class SqlAlchemyBudgetTargetRepository:
    """SQLAlchemy repository for monthly category targets."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert(self, draft: BudgetTargetDraft) -> BudgetTarget:
        """Create or optimistically update one category's monthly target.

        Optimistic locking is never optional: creating requires no version,
        updating requires the exact current one, and each mismatched
        combination is a conflict rather than a silent overwrite. The update
        itself is a conditional statement, so a concurrent writer cannot slip
        between the read and the write.
        """
        existing = self._session.scalar(
            select(BudgetTargetModel).where(
                BudgetTargetModel.budget_period_id == draft.budget_period_id,
                BudgetTargetModel.budget_category_id == draft.budget_category_id,
            )
        )
        if existing is None:
            if draft.expected_version is not None:
                raise ConcurrentUpdateError(
                    "this target no longer exists; reload before saving"
                )
            model = BudgetTargetModel(
                budget_period_id=draft.budget_period_id,
                budget_category_id=draft.budget_category_id,
                target_minor=draft.target_minor,
                buffer_minor=draft.buffer_minor,
                funding_wallet_id=draft.funding_wallet_id,
                month_end_destination_wallet_id=(draft.month_end_destination_wallet_id),
            )
            self._session.add(model)
            _flush_and_refresh(self._session, model)
            return budget_target_record(model)
        if draft.expected_version is None:
            raise ConcurrentUpdateError(
                "this target already exists; reload before saving"
            )
        result = cast(
            CursorResult[Any],
            self._session.execute(
                update(BudgetTargetModel)
                .where(
                    BudgetTargetModel.id == existing.id,
                    BudgetTargetModel.version == draft.expected_version,
                )
                .values(
                    target_minor=draft.target_minor,
                    buffer_minor=draft.buffer_minor,
                    funding_wallet_id=draft.funding_wallet_id,
                    month_end_destination_wallet_id=(
                        draft.month_end_destination_wallet_id
                    ),
                    version=BudgetTargetModel.version + 1,
                )
            ),
        )
        if result.rowcount != 1:
            raise ConcurrentUpdateError("target changed after this page was loaded")
        self._session.expire(existing)
        refreshed = self._session.get(BudgetTargetModel, existing.id)
        if refreshed is None:  # pragma: no cover - just updated
            raise BudgetPeriodError("budget target disappeared")
        return budget_target_record(refreshed)

    def get(self, target_id: int) -> BudgetTarget | None:
        """Return one target by internal ID."""
        model = self._session.get(BudgetTargetModel, target_id)
        return None if model is None else budget_target_record(model)

    def list_for_period(self, period_id: int) -> tuple[BudgetTarget, ...]:
        """Return every target belonging to one budget month."""
        models = self._session.scalars(
            select(BudgetTargetModel)
            .where(BudgetTargetModel.budget_period_id == period_id)
            .order_by(BudgetTargetModel.budget_category_id)
        ).all()
        return tuple(budget_target_record(model) for model in models)


class SqlAlchemyEventAllocationRepository:
    """SQLAlchemy repository for event allocations."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, draft: EventAllocationDraft) -> EventAllocation:
        """Persist one share of a financial event."""
        model = EventAllocationModel(
            financial_event_id=draft.financial_event_id,
            budget_category_id=draft.budget_category_id,
            funding_wallet_id=draft.funding_wallet_id,
            amount_minor=draft.amount_minor,
            currency=draft.currency,
            allocation_source=draft.allocation_source.value,
        )
        self._session.add(model)
        _flush_and_refresh(self._session, model)
        return event_allocation_record(model)

    def list_for_event(self, event_id: int) -> tuple[EventAllocation, ...]:
        """Return every allocation an event ever had, superseded ones included.

        This is the history. A caller that means "which wallet funds this
        event now" asks `list_for_entry` with the entry in force, because a
        redone allocation leaves the decision it replaced in place
        (ADR-B103).
        """
        models = self._session.scalars(
            select(EventAllocationModel)
            .where(EventAllocationModel.financial_event_id == event_id)
            .order_by(EventAllocationModel.id)
        ).all()
        return tuple(event_allocation_record(model) for model in models)

    def list_for_entry(self, entry_id: int) -> tuple[EventAllocation, ...]:
        """Return the allocations one journal entry was built from."""
        models = self._session.scalars(
            select(EventAllocationModel)
            .where(EventAllocationModel.journal_entry_id == entry_id)
            .order_by(EventAllocationModel.id)
        ).all()
        return tuple(event_allocation_record(model) for model in models)

    def attach_entry(
        self, allocation_ids: tuple[int, ...], entry_id: int
    ) -> tuple[EventAllocation, ...]:
        """Name the entry these allocations were posted with.

        Written once, in the transaction that staged the entry: the postings
        are built from the rows, so the entry cannot exist when they are
        inserted. Re-pointing a row that already names another entry is
        refused, because that would silently move a posted decision onto a
        different entry.
        """
        if not allocation_ids:
            return ()
        models = self._session.scalars(
            select(EventAllocationModel)
            .where(EventAllocationModel.id.in_(allocation_ids))
            .order_by(EventAllocationModel.id)
        ).all()
        if len(models) != len(set(allocation_ids)):
            raise FinancialEventError("an allocation disappeared before it was posted")
        for model in models:
            if model.journal_entry_id not in (None, entry_id):
                raise FinancialEventError(
                    "this allocation was already posted with another entry"
                )
            model.journal_entry_id = entry_id
        self._session.flush()
        for model in models:
            self._session.refresh(model)
        return tuple(event_allocation_record(model) for model in models)

    def add_dedicated(
        self, draft: DedicatedWalletAllocationDraft
    ) -> DedicatedWalletAllocation:
        """Persist the clearing destination of one dedicated-wallet share."""
        model = DedicatedWalletAllocationModel(
            event_allocation_id=draft.event_allocation_id,
            clearing_wallet_id=draft.clearing_wallet_id,
        )
        self._session.add(model)
        _flush_and_refresh(self._session, model)
        return dedicated_wallet_allocation_record(model)

    def get_dedicated(
        self, event_allocation_id: int
    ) -> DedicatedWalletAllocation | None:
        """Return one allocation's dedicated route, if it has one."""
        model = self._session.scalar(
            select(DedicatedWalletAllocationModel).where(
                DedicatedWalletAllocationModel.event_allocation_id
                == event_allocation_id
            )
        )
        return None if model is None else dedicated_wallet_allocation_record(model)

    def list_dedicated_for_event(
        self, event_id: int
    ) -> tuple[DedicatedWalletAllocation, ...]:
        """Return dedicated routes in their allocation's stable order."""
        models = self._session.scalars(
            select(DedicatedWalletAllocationModel)
            .join(
                EventAllocationModel,
                EventAllocationModel.id
                == DedicatedWalletAllocationModel.event_allocation_id,
            )
            .where(EventAllocationModel.financial_event_id == event_id)
            .order_by(EventAllocationModel.id)
        ).all()
        return tuple(dedicated_wallet_allocation_record(model) for model in models)


class SqlAlchemyBudgetLedgerRepository:
    """Aggregate reads over posted journal lines, inside the open session."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def net_spend_by_category(
        self, *, start: datetime, end: datetime, currency: str
    ) -> Mapping[int, int]:
        """Return spend minus refunds per category for one local month.

        Only category ledger lines are counted, and only in one currency, so
        the result is never a sum across currencies. A refund posts a credit
        against the same category, so it nets out here by construction.
        """
        original = aliased(JournalEntryModel)
        event_id = func.coalesce(
            JournalEntryModel.financial_event_id, original.financial_event_id
        )
        not_dedicated = (
            ~select(DedicatedWalletAllocationModel.id)
            .join(
                EventAllocationModel,
                EventAllocationModel.id
                == DedicatedWalletAllocationModel.event_allocation_id,
            )
            .where(EventAllocationModel.financial_event_id == event_id)
            .exists()
        )
        rows = self._session.execute(
            select(
                JournalPostingModel.budget_category_id,
                func.sum(JournalPostingModel.amount_minor),
            )
            .join(
                JournalEntryModel,
                JournalEntryModel.id == JournalPostingModel.journal_entry_id,
            )
            .join(
                original,
                original.id == JournalEntryModel.reversal_of_id,
                isouter=True,
            )
            .where(
                JournalPostingModel.budget_category_id.is_not(None),
                JournalPostingModel.currency == normalize_currency(currency),
                JournalEntryModel.effective_at >= start,
                JournalEntryModel.effective_at < end,
                not_dedicated,
            )
            .group_by(JournalPostingModel.budget_category_id)
        ).all()
        return {int(row[0]): int(row[1] or 0) for row in rows}

    def wallet_balance(self, wallet_id: int) -> int:
        """Return the posted balance held in one wallet."""
        total = self._session.execute(
            select(func.coalesce(func.sum(JournalPostingModel.amount_minor), 0)).where(
                JournalPostingModel.wallet_id == wallet_id
            )
        ).scalar_one()
        return int(total)

    def reserved_minor(self, wallet_id: int) -> int:
        """Return everything still held back in one wallet, counted once.

        Three disjoint parts add up (ADR-B059), mirroring the pure
        ``wallet_reserved_minor`` rule the repository tests compare against:

        1. B5 waiting salary lines, which are their own commitment (ADR-B043);
        2. the part of each funding commitment's requirement that no active or
           executed schedule covers;
        3. what each active schedule will really take out of this wallet.

        Counting a commitment and the schedule that funds it separately would
        reserve the same money twice; counting only one of them would release
        money the moment a schedule is cancelled or a refund shrinks a
        requirement. Nothing here reads a commitment's ``status`` column, so a
        stale status cannot change an available balance.
        """
        return (
            self._waiting_salary_minor(wallet_id)
            + self._uncovered_commitment_minor(wallet_id)
            + self._active_schedule_debit_minor(wallet_id)
        )

    def _waiting_salary_minor(self, wallet_id: int) -> int:
        """Return what B5 lines awaiting a manual transfer still hold back."""
        total = self._session.execute(
            select(
                func.coalesce(func.sum(SalaryAllocationRunItemModel.amount_minor), 0)
            )
            .join(
                SalaryAllocationRunModel,
                SalaryAllocationRunModel.id == SalaryAllocationRunItemModel.run_id,
            )
            .where(
                SalaryAllocationRunModel.source_wallet_id == wallet_id,
                SalaryAllocationRunModel.status.in_(_RESERVING_RUN_STATUSES),
                SalaryAllocationRunItemModel.status
                == SalaryRunItemStatus.WAITING_TRANSFER.value,
            )
        ).scalar_one()
        return int(total)

    def _uncovered_commitment_minor(self, wallet_id: int) -> int:
        """Return the requirement no schedule has taken responsibility for."""
        covered = (
            select(
                ScheduledTransferCommitmentModel.funding_commitment_id.label(
                    "commitment_id"
                ),
                func.sum(ScheduledTransferCommitmentModel.amount_minor).label(
                    "covered_minor"
                ),
            )
            .join(
                ScheduledTransferModel,
                ScheduledTransferModel.id
                == ScheduledTransferCommitmentModel.scheduled_transfer_id,
            )
            .where(ScheduledTransferModel.status.in_(LIVE_STATUS_VALUES))
            .group_by(ScheduledTransferCommitmentModel.funding_commitment_id)
            .subquery()
        )
        total = self._session.execute(
            select(
                func.coalesce(
                    func.sum(
                        func.max(
                            0,
                            FundingCommitmentModel.amount_minor
                            - FundingCommitmentModel.released_minor
                            - func.coalesce(covered.c.covered_minor, 0),
                        )
                    ),
                    0,
                )
            )
            .select_from(FundingCommitmentModel)
            .join(
                covered,
                covered.c.commitment_id == FundingCommitmentModel.id,
                isouter=True,
            )
            .where(FundingCommitmentModel.source_wallet_id == wallet_id)
        ).scalar_one()
        return int(total)

    def _active_schedule_debit_minor(self, wallet_id: int) -> int:
        """Return what schedules that have not moved yet will take from here.

        A covered share leaves the wallet that reserved it, so it is counted
        against that wallet's commitments; whatever an autopay does not cover
        leaves the schedule's own source wallet.
        """
        linked = self._session.execute(
            select(
                func.coalesce(
                    func.sum(ScheduledTransferCommitmentModel.amount_minor), 0
                )
            )
            .join(
                ScheduledTransferModel,
                ScheduledTransferModel.id
                == ScheduledTransferCommitmentModel.scheduled_transfer_id,
            )
            .join(
                FundingCommitmentModel,
                FundingCommitmentModel.id
                == ScheduledTransferCommitmentModel.funding_commitment_id,
            )
            .where(
                FundingCommitmentModel.source_wallet_id == wallet_id,
                ScheduledTransferModel.status.in_(ACTIVE_STATUS_VALUES),
            )
        ).scalar_one()
        link_totals = (
            select(
                ScheduledTransferCommitmentModel.scheduled_transfer_id.label(
                    "schedule_id"
                ),
                func.sum(ScheduledTransferCommitmentModel.amount_minor).label(
                    "linked_minor"
                ),
            )
            .group_by(ScheduledTransferCommitmentModel.scheduled_transfer_id)
            .subquery()
        )
        residual = self._session.execute(
            select(
                func.coalesce(
                    func.sum(
                        ScheduledTransferModel.amount_minor
                        - func.coalesce(link_totals.c.linked_minor, 0)
                    ),
                    0,
                )
            )
            .select_from(ScheduledTransferModel)
            .join(
                link_totals,
                link_totals.c.schedule_id == ScheduledTransferModel.id,
                isouter=True,
            )
            .where(
                ScheduledTransferModel.source_wallet_id == wallet_id,
                ScheduledTransferModel.status.in_(ACTIVE_STATUS_VALUES),
            )
        ).scalar_one()
        return int(linked) + int(residual)


def balance_snapshot_record(model: BalanceSnapshotModel) -> BalanceSnapshot:
    """Translate one persisted observation into its domain record."""
    return BalanceSnapshot(
        id=model.id,
        financial_account_id=model.financial_account_id,
        observed_balance_minor=model.observed_balance_minor,
        observed_at=model.observed_at,
        source=BalanceSnapshotSource(model.source),
        note=model.note,
        idempotency_key=model.idempotency_key,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class SqlAlchemyBalanceSnapshotRepository:
    """SQLAlchemy repository for immutable observed bank balances.

    Only ``add`` and reads exist. Revision 0014 also refuses UPDATE and DELETE
    at the row level, so the absence of a mutating method here is a design
    statement rather than the only defence (ADR-B066).
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, draft: BalanceSnapshotDraft) -> BalanceSnapshot:
        """Persist one observation; it posts nothing and moves no balance."""
        key = draft.idempotency_key.strip()
        if len(key) > _MAX_IDEMPOTENCY_KEY_LENGTH:
            raise ValueError("balance snapshot idempotency key is too long")
        model = BalanceSnapshotModel(
            financial_account_id=draft.financial_account_id,
            observed_balance_minor=draft.observed_balance_minor,
            observed_at=draft.observed_at,
            source=draft.source.value,
            note=draft.note,
            idempotency_key=key,
        )
        self._session.add(model)
        _flush_and_refresh(self._session, model)
        return balance_snapshot_record(model)

    def get(self, snapshot_id: int) -> BalanceSnapshot | None:
        """Return one observation by internal ID."""
        model = self._session.get(BalanceSnapshotModel, snapshot_id)
        return None if model is None else balance_snapshot_record(model)

    def get_by_idempotency_key(self, key: str) -> BalanceSnapshot | None:
        """Return the observation already recorded under one stable key."""
        model = self._session.scalar(
            select(BalanceSnapshotModel).where(
                BalanceSnapshotModel.idempotency_key == key.strip()
            )
        )
        return None if model is None else balance_snapshot_record(model)

    def list_for_account(
        self, account_id: int, *, limit: int
    ) -> tuple[BalanceSnapshot, ...]:
        """Return one account's observations, most recently observed first."""
        models = self._session.scalars(
            select(BalanceSnapshotModel)
            .where(BalanceSnapshotModel.financial_account_id == account_id)
            .order_by(
                BalanceSnapshotModel.observed_at.desc(),
                BalanceSnapshotModel.id.desc(),
            )
            .limit(max(0, limit))
        ).all()
        return tuple(balance_snapshot_record(model) for model in models)

    def latest_for_account(self, account_id: int) -> BalanceSnapshot | None:
        """Return the most recent observation of one account, if any."""
        rows = self.list_for_account(account_id, limit=1)
        return rows[0] if rows else None


class SqlAlchemyReconciliationLedgerRepository:
    """Read one account's ledger balance as of an observed instant."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def physical_balance_as_of(
        self, *, financial_account_id: int, as_of: datetime
    ) -> int:
        """Return the account's balance from entries effective at or before then.

        The current balance is deliberately not used (ADR-B067): comparing an
        observation made yesterday against today's balance would blame today's
        spending for yesterday's gap. Every posting of the account's own
        physical ledger account counts, wallet-assigned or not, because the
        bank knows nothing about wallets.
        """
        total = self._session.execute(
            select(func.coalesce(func.sum(JournalPostingModel.amount_minor), 0))
            .select_from(JournalPostingModel)
            .join(
                JournalEntryModel,
                JournalEntryModel.id == JournalPostingModel.journal_entry_id,
            )
            .join(
                LedgerAccountModel,
                LedgerAccountModel.id == JournalPostingModel.ledger_account_id,
            )
            .where(
                LedgerAccountModel.financial_account_id == financial_account_id,
                JournalEntryModel.effective_at <= as_of,
            )
        ).scalar_one()
        return int(total)


#: Salary runs whose waiting lines still reserve money. A draft has reserved
#: nothing yet and a cancelled generation has released everything it held.
_RESERVING_RUN_STATUSES = (
    SalaryRunStatus.APPROVED.value,
    SalaryRunStatus.PARTIALLY_FUNDED.value,
)


class SqlAlchemyFundingCommitmentRepository:
    """SQLAlchemy repository for funding reservations."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, draft: FundingCommitmentDraft) -> FundingCommitment:
        """Reserve funding for one allocation without moving any money."""
        model = FundingCommitmentModel(
            financial_event_id=draft.financial_event_id,
            event_allocation_id=draft.event_allocation_id,
            source_wallet_id=draft.source_wallet_id,
            settlement_account_id=draft.settlement_account_id,
            amount_minor=draft.amount_minor,
            currency=normalize_currency(draft.currency),
            fulfillment_mode=draft.fulfillment_mode.value,
            status=draft.status.value,
        )
        self._session.add(model)
        _flush_and_refresh(self._session, model)
        return funding_commitment_record(model)

    def get(self, commitment_id: int) -> FundingCommitment | None:
        """Return one commitment by internal ID."""
        model = self._session.get(FundingCommitmentModel, commitment_id)
        return None if model is None else funding_commitment_record(model)

    def list_for_event(self, event_id: int) -> tuple[FundingCommitment, ...]:
        """Return every commitment created for one event."""
        models = self._session.scalars(
            select(FundingCommitmentModel)
            .where(FundingCommitmentModel.financial_event_id == event_id)
            .order_by(FundingCommitmentModel.id)
        ).all()
        return tuple(funding_commitment_record(model) for model in models)

    def list_open_for_wallet(self, wallet_id: int) -> tuple[FundingCommitment, ...]:
        """Return commitments still reserving money in one wallet."""
        models = self._session.scalars(
            select(FundingCommitmentModel)
            .where(
                FundingCommitmentModel.source_wallet_id == wallet_id,
                FundingCommitmentModel.status.in_(
                    (
                        CommitmentStatus.OPEN.value,
                        CommitmentStatus.PARTIALLY_SCHEDULED.value,
                        CommitmentStatus.SCHEDULED.value,
                    )
                ),
            )
            .order_by(FundingCommitmentModel.id)
        ).all()
        return tuple(funding_commitment_record(model) for model in models)

    def release(self, commitment_id: int, amount_minor: int) -> FundingCommitment:
        """Release part of a reservation without ever releasing it twice.

        The bound is part of the statement, so two concurrent refunds cannot
        both read the same outstanding amount and each release it.
        """
        released = validate_minor_units(
            amount_minor, label="released amount", minimum=1
        )
        result = cast(
            CursorResult[Any],
            self._session.execute(
                update(FundingCommitmentModel)
                .where(
                    FundingCommitmentModel.id == commitment_id,
                    FundingCommitmentModel.released_minor + released
                    <= FundingCommitmentModel.amount_minor,
                )
                .values(
                    released_minor=FundingCommitmentModel.released_minor + released,
                    status=case(
                        (
                            FundingCommitmentModel.released_minor + released
                            == FundingCommitmentModel.amount_minor,
                            CommitmentStatus.CANCELLED.value,
                        ),
                        else_=FundingCommitmentModel.status,
                    ),
                )
            ),
        )
        if result.rowcount != 1:
            if self._session.get(FundingCommitmentModel, commitment_id) is None:
                raise BudgetPeriodError("funding commitment was not found")
            raise BudgetPeriodError(
                "cannot release more than the commitment still reserves"
            )
        model = self._session.get(FundingCommitmentModel, commitment_id)
        if model is None:  # pragma: no cover - just updated
            raise BudgetPeriodError("funding commitment disappeared")
        self._session.refresh(model)
        return funding_commitment_record(model)

    def refresh_status(self, commitment_id: int) -> FundingCommitment:
        """Re-derive one commitment's status from what actually covers it.

        Scheduling, cancelling, executing, reversing, and refunding all end
        here, so the status column can never disagree with the coverage it is
        supposed to describe (ADR-B059).
        """
        refresh_commitment_status(self._session, commitment_id)
        model = self._session.get(FundingCommitmentModel, commitment_id)
        if model is None:  # pragma: no cover - refresh raises first
            raise BudgetPeriodError("funding commitment was not found")
        return funding_commitment_record(model)

    def list_open_for_settlement_account(
        self, settlement_account_id: int
    ) -> tuple[FundingCommitment, ...]:
        """Return commitments that still need money to reach one account."""
        models = self._session.scalars(
            select(FundingCommitmentModel)
            .where(
                FundingCommitmentModel.settlement_account_id == settlement_account_id,
                FundingCommitmentModel.amount_minor
                > FundingCommitmentModel.released_minor,
            )
            .order_by(FundingCommitmentModel.id)
        ).all()
        return tuple(funding_commitment_record(model) for model in models)

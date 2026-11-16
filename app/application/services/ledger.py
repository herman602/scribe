"""Ledger setup and immutable double-entry posting use cases."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from app.domain.budget_enums import (
    FinancialAccountType,
    JournalEntryType,
    LedgerAccountKind,
    WalletPurpose,
)
from app.domain.budget_models import (
    FinancialAccount,
    FinancialAccountDraft,
    FinancialEvent,
    JournalEntry,
    JournalEntryDraft,
    LedgerAccount,
    LedgerAccountDraft,
    Money,
    OpeningWalletSplit,
    PostingDraft,
    Wallet,
    WalletDraft,
    normalize_currency,
    validate_minor_units,
)
from app.domain.budget_rules import (
    OPENING_EQUITY_NAME,
    UNALLOCATED_WALLET_NAME,
    draft_signatures,
    ensure_balanced,
    ensure_exact_reversal,
    ensure_reversal_shape,
    holds_wallets,
    is_wallet_managed,
    ledger_kind_for_account_type,
    negated_postings,
    opening_equity_code,
    opening_posting_amount,
    physical_ledger_account_code,
    posted_signatures,
    resolve_display_arrangement,
)
from app.domain.errors import (
    CurrencyMismatchError,
    IdempotencyConflictError,
    LedgerConfigurationError,
    OpeningPositionError,
    PreOpeningEventError,
    WalletAssignmentError,
)
from app.domain.ports.repositories import UnitOfWork

type UnitOfWorkFactory = Callable[[], UnitOfWork]
type Clock = Callable[[], datetime]


def ensure_event_after_opening_cutoff(
    unit: UnitOfWork,
    event: FinancialEvent,
    *,
    effective_at: datetime,
) -> None:
    """Reject a new event effect already represented by opening balances."""
    batch = unit.import_batches.latest_applied()
    if batch is not None and (
        event.occurred_at <= batch.cutoff_at or effective_at <= batch.cutoff_at
    ):
        raise PreOpeningEventError(
            "this event is at or before the applied opening cutoff; "
            "it remains historical evidence and cannot be posted again"
        )


@dataclass(frozen=True)
class OpenFinancialAccountRequest:
    """User intent to register one physical balance container."""

    institution_code: str
    display_name: str
    account_type: FinancialAccountType
    currency: str
    masked_reference: str | None = None


@dataclass(frozen=True)
class OpenFinancialAccountResult:
    """The created account, its ledger identity, and its default wallet."""

    account: FinancialAccount
    ledger_account: LedgerAccount
    unallocated_wallet: Wallet | None


@dataclass(frozen=True)
class AddWalletRequest:
    """User intent to add one envelope to an existing asset account."""

    financial_account_id: int
    name: str
    purpose: WalletPurpose = WalletPurpose.GENERAL


@dataclass(frozen=True)
class RenameWalletRequest:
    """User intent to rename one wallet; nothing else about it changes."""

    wallet_id: int
    name: str
    expected_version: int


@dataclass(frozen=True)
class RenameWalletsRequest:
    """User intent to retype the names of one account's wallets at once.

    `names` maps a wallet to the name typed for it and `expected_versions` to
    the version the form was rendered with, so a page loaded before someone
    else renamed the same wallet is refused rather than silently winning.
    Every wallet named must belong to `financial_account_id`: the form is
    scoped to one account, and a submission that reaches past it is a
    mistake, however it was produced.
    """

    financial_account_id: int
    names: Mapping[int, str]
    expected_versions: Mapping[int, int]


@dataclass(frozen=True)
class ArrangeWalletsRequest:
    """User intent to fix the order one account's wallets read in.

    `positions` maps every active wallet of the account to the place its owner
    typed for it. The values are only a sort key; what gets stored is their
    resolved sequence.
    """

    financial_account_id: int
    positions: Mapping[int, int]


@dataclass(frozen=True)
class ArrangeAccountsRequest:
    """User intent to fix the order the sheet's account columns read in.

    `positions` maps every active account to the place its owner typed for it.
    The values are only a sort key; what gets stored is their resolved
    sequence.
    """

    positions: Mapping[int, int]


@dataclass(frozen=True)
class OpeningPositionRequest:
    """A reconciled opening balance and its explicit wallet split."""

    financial_account_id: int
    balance_minor: int
    observed_at: datetime
    splits: tuple[OpeningWalletSplit, ...] = ()
    description: str = "Opening position"


@dataclass(frozen=True)
class OpenAccountWithOpeningRequest:
    """Register one account and its optional current opening in one commit."""

    account: OpenFinancialAccountRequest
    opening_balance_minor: int = 0
    observed_at: datetime | None = None


@dataclass(frozen=True)
class OpenAccountWithOpeningResult:
    """The setup result plus the opening entry when a non-zero value was given."""

    setup: OpenFinancialAccountResult
    opening_entry: JournalEntry | None


class LedgerSetupService:
    """Create accounts, their ledger identity, and their wallets."""

    def __init__(self, unit_of_work_factory: UnitOfWorkFactory) -> None:
        self._unit_of_work_factory = unit_of_work_factory

    def open_account(
        self, request: OpenFinancialAccountRequest
    ) -> OpenFinancialAccountResult:
        """Create one account plus its physical ledger account atomically."""
        with self._unit_of_work_factory() as unit:
            result = self.stage_open_account(unit, request)
            unit.commit()
        return result

    def open_account_with_opening(
        self, request: OpenAccountWithOpeningRequest
    ) -> OpenAccountWithOpeningResult:
        """Create account and opening position atomically for the Web setup flow."""
        amount = validate_minor_units(
            request.opening_balance_minor,
            label="opening balance",
        )
        if amount and request.observed_at is None:
            raise OpeningPositionError(
                "a non-zero opening balance requires an observation time"
            )
        with self._unit_of_work_factory() as unit:
            setup = self.stage_open_account(unit, request.account)
            entry: JournalEntry | None = None
            if amount:
                splits = (
                    ()
                    if setup.unallocated_wallet is None
                    else (
                        OpeningWalletSplit(
                            wallet_name=setup.unallocated_wallet.name,
                            amount_minor=amount,
                        ),
                    )
                )
                assert request.observed_at is not None
                entry = JournalService(
                    self._unit_of_work_factory
                ).stage_opening_position(
                    unit,
                    OpeningPositionRequest(
                        financial_account_id=setup.account.id,
                        balance_minor=amount,
                        observed_at=request.observed_at,
                        splits=splits,
                        description="Web account opening",
                    ),
                )
            unit.commit()
        return OpenAccountWithOpeningResult(setup=setup, opening_entry=entry)

    @staticmethod
    def stage_open_account(
        unit: UnitOfWork, request: OpenFinancialAccountRequest
    ) -> OpenFinancialAccountResult:
        """Stage account, ledger identity, and default wallet without committing."""
        currency = normalize_currency(request.currency)
        kind = ledger_kind_for_account_type(request.account_type)
        existing = unit.financial_accounts.get_by_identity(
            institution_code=request.institution_code,
            display_name=request.display_name,
            currency=currency,
        )
        if existing is not None:
            raise LedgerConfigurationError(
                "an active account with this institution, name, and currency "
                "already exists"
            )
        account = unit.financial_accounts.add(
            FinancialAccountDraft(
                institution_code=request.institution_code,
                display_name=request.display_name,
                account_type=request.account_type,
                currency=currency,
                masked_reference=request.masked_reference,
            )
        )
        ledger_account = unit.ledger_accounts.add(
            LedgerAccountDraft(
                code=physical_ledger_account_code(
                    kind, account.institution_code, account.id
                ),
                name=account.display_name,
                kind=kind,
                currency=currency,
                financial_account_id=account.id,
            )
        )
        wallet: Wallet | None = None
        if holds_wallets(account.account_type):
            wallet = unit.wallets.add(
                WalletDraft(
                    financial_account_id=account.id,
                    name=UNALLOCATED_WALLET_NAME,
                    purpose=WalletPurpose.UNALLOCATED,
                )
            )
        return OpenFinancialAccountResult(
            account=account,
            ledger_account=ledger_account,
            unallocated_wallet=wallet,
        )

    def get_wallet(self, wallet_id: int) -> Wallet | None:
        """Return one wallet for a form to prefill; no other read belongs here."""
        with self._unit_of_work_factory() as unit:
            return unit.wallets.get(wallet_id)

    def add_wallet(self, request: AddWalletRequest) -> Wallet:
        """Create one additional envelope inside a wallet-managed account."""
        if request.purpose is WalletPurpose.UNALLOCATED:
            raise LedgerConfigurationError(
                "the Unallocated wallet is created with its account"
            )
        with self._unit_of_work_factory() as unit:
            account = unit.financial_accounts.get(request.financial_account_id)
            if account is None:
                raise LedgerConfigurationError("financial account was not found")
            if not holds_wallets(account.account_type):
                raise LedgerConfigurationError(
                    "only asset accounts are divided into wallets"
                )
            existing = unit.wallets.get_by_name(account.id, request.name)
            if existing is not None:
                raise LedgerConfigurationError(
                    "this account already has a wallet with that name"
                )
            wallet = unit.wallets.add(
                WalletDraft(
                    financial_account_id=account.id,
                    name=request.name,
                    purpose=request.purpose,
                )
            )
            unit.commit()
        return wallet

    def rename_wallet(self, request: RenameWalletRequest) -> Wallet:
        """Rename one wallet optimistically; no balance or purpose changes."""
        with self._unit_of_work_factory() as unit:
            current = unit.wallets.get(request.wallet_id)
            if current is None:
                raise LedgerConfigurationError("wallet was not found")
            existing = unit.wallets.get_by_name(
                current.financial_account_id, request.name
            )
            if existing is not None and existing.id != current.id:
                raise LedgerConfigurationError(
                    "this account already has a wallet with that name"
                )
            wallet = unit.wallets.rename(
                wallet_id=request.wallet_id,
                name=request.name,
                expected_version=request.expected_version,
            )
            unit.commit()
        return wallet

    def rename_wallets(self, request: RenameWalletsRequest) -> tuple[Wallet, ...]:
        """Retype one account's wallet names as a single change.

        One wallet at a time was one transaction each, so a name colliding
        halfway through left the earlier ones renamed and the rest not, with
        the page reporting only the failure. Here either every name is stored
        or none is.

        The version comes from the form, not from a read taken a moment
        earlier: reading it here would make every submission win, which is
        the opposite of what an expected version is for.

        Two wallets cannot trade names in one submission. The account's names
        are unique in the database and SQLite checks that as each row is
        written, not at the end, so the swap is refused by name here with an
        explanation rather than by an integrity error halfway through.
        """
        with self._unit_of_work_factory() as unit:
            account = unit.financial_accounts.get(request.financial_account_id)
            if account is None:
                raise LedgerConfigurationError("financial account was not found")
            owned = {
                wallet.id: wallet
                for wallet in unit.wallets.list_for_account(account.id)
            }
            for wallet_id in request.names:
                if wallet_id not in owned:
                    raise LedgerConfigurationError(
                        "this wallet does not belong to this account"
                    )
                if wallet_id not in request.expected_versions:
                    raise LedgerConfigurationError(
                        "this wallet was submitted without the version it was "
                        "shown with"
                    )
            # Every refusal is decided before the first row is written, so a
            # rejected submission cannot leave half of it applied.
            held = {wallet.name: wallet.id for wallet in owned.values()}
            requested: dict[str, int] = {}
            for wallet_id, name in request.names.items():
                holder = held.get(name) or requested.get(name)
                if holder is not None and holder != wallet_id:
                    raise LedgerConfigurationError(
                        "this account already has a wallet with that name; rename "
                        "that one first if the two are trading names"
                    )
                requested[name] = wallet_id
            renamed = tuple(
                unit.wallets.rename(
                    wallet_id=wallet_id,
                    name=name,
                    expected_version=request.expected_versions[wallet_id],
                )
                for wallet_id, name in request.names.items()
                if owned[wallet_id].name != name
            )
            unit.commit()
        return renamed

    def arrange_accounts(
        self, request: ArrangeAccountsRequest
    ) -> tuple[FinancialAccount, ...]:
        """Store the order the sheet's account columns read in.

        Only presentation changes here, exactly as for a column's wallets: no
        balance moves and nothing is posted.
        """
        with self._unit_of_work_factory() as unit:
            accounts = unit.financial_accounts.list_active()
            arranged = resolve_display_arrangement(
                item_ids=[account.id for account in accounts],
                requested_positions=request.positions,
            )
            for position, account_id in enumerate(arranged, start=1):
                unit.financial_accounts.set_display_order(account_id, position)
            unit.commit()
            by_id = {account.id: account for account in accounts}
        return tuple(by_id[account_id] for account_id in arranged)

    def arrange_wallets(self, request: ArrangeWalletsRequest) -> tuple[Wallet, ...]:
        """Store the order one account's wallets read in on the sheet.

        Only presentation changes here. No balance moves, nothing is posted,
        and the whole column is renumbered from the resolved sequence so that
        repeated or skipped positions never accumulate in the stored values.
        """
        with self._unit_of_work_factory() as unit:
            account = unit.financial_accounts.get(request.financial_account_id)
            if account is None:
                raise LedgerConfigurationError("financial account was not found")
            wallets = tuple(
                wallet
                for wallet in unit.wallets.list_for_account(account.id)
                if wallet.is_active
            )
            arranged = resolve_display_arrangement(
                item_ids=[wallet.id for wallet in wallets],
                requested_positions=request.positions,
            )
            for position, wallet_id in enumerate(arranged, start=1):
                unit.wallets.set_display_order(wallet_id, position)
            unit.commit()
            by_id = {wallet.id: wallet for wallet in wallets}
        return tuple(by_id[wallet_id] for wallet_id in arranged)


class JournalService:
    """Post, reverse, and open balanced immutable accounting entries."""

    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        *,
        clock: Clock = lambda: datetime.now(UTC),
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._clock = clock

    def post_balanced(
        self,
        *,
        entry_type: JournalEntryType,
        effective_at: datetime,
        idempotency_key: str,
        description: str,
        postings: tuple[PostingDraft, ...],
    ) -> JournalEntry:
        """Validate and commit one balanced entry, or return the existing one.

        A reversal is never posted through this method. Only `reverse` may link
        an entry to the entry it cancels, so a caller cannot record a
        differently shaped entry that merely claims to reverse another one.

        This method also cannot link a financial event. Posting an event has to
        commit its allocations, commitments, and new status in the same
        transaction, so that path goes through `stage_balanced` and the calling
        command's Unit of Work.
        """
        with self._unit_of_work_factory() as unit:
            entry, created = self._post(
                unit,
                entry_type=entry_type,
                effective_at=effective_at,
                idempotency_key=idempotency_key,
                description=description,
                postings=postings,
                reversal_of_id=None,
                financial_event_id=None,
            )
            if created:
                unit.commit()
        return entry

    def stage_balanced(
        self,
        unit: UnitOfWork,
        *,
        entry_type: JournalEntryType,
        effective_at: datetime,
        idempotency_key: str,
        description: str,
        postings: tuple[PostingDraft, ...],
        financial_event_id: int | None = None,
    ) -> JournalEntry:
        """Validate and stage one entry inside a caller's transaction.

        The caller owns the commit, so an allocation, its commitments, the
        journal entry, and the event's new status become one atomic change.
        Every journal invariant is still checked here: a caller must never
        reach the journal repository directly.
        """
        entry, _ = self._post(
            unit,
            entry_type=entry_type,
            effective_at=effective_at,
            idempotency_key=idempotency_key,
            description=description,
            postings=postings,
            reversal_of_id=None,
            financial_event_id=financial_event_id,
        )
        return entry

    def reverse(
        self,
        entry_id: int,
        *,
        description: str = "Reversal",
        effective_at: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> JournalEntry:
        """Create the single opposite entry that cancels a posted entry."""
        with self._unit_of_work_factory() as unit:
            entry, created = self._reverse(
                unit,
                entry_id,
                description=description,
                effective_at=effective_at,
                idempotency_key=idempotency_key,
            )
            if created:
                unit.commit()
        return entry

    def stage_reversal(
        self,
        unit: UnitOfWork,
        entry_id: int,
        *,
        description: str = "Reversal",
        effective_at: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> JournalEntry:
        """Stage the opposite entry inside a caller's transaction.

        A caller that has to move its own row in step with the reversal — a
        schedule becoming `reversed`, and every commitment it covered being
        recomputed — needs all of it in one commit. Every reversal invariant is
        still checked here, exactly as in `reverse`: the caller must never
        reach the journal repository itself.
        """
        entry, _ = self._reverse(
            unit,
            entry_id,
            description=description,
            effective_at=effective_at,
            idempotency_key=idempotency_key,
        )
        return entry

    def _reverse(
        self,
        unit: UnitOfWork,
        entry_id: int,
        *,
        description: str,
        effective_at: datetime | None,
        idempotency_key: str | None,
    ) -> tuple[JournalEntry, bool]:
        """Validate and stage one exact reversal without committing."""
        original = unit.journal.get(entry_id)
        if original is None:
            raise LedgerConfigurationError("journal entry was not found")
        existing = unit.journal.get_reversal_of(entry_id)
        if existing is not None:
            return existing, False
        postings = negated_postings(
            PostingDraft(
                ledger_account_id=posting.ledger_account_id,
                amount=Money(posting.amount_minor, posting.currency),
                wallet_id=posting.wallet_id,
                budget_category_id=posting.budget_category_id,
            )
            for posting in original.postings
        )
        return self._post(
            unit,
            entry_type=JournalEntryType.REVERSAL,
            effective_at=effective_at or original.effective_at,
            idempotency_key=(idempotency_key or f"reversal:journal_entry:{entry_id}"),
            description=description,
            postings=postings,
            reversal_of_id=entry_id,
        )

    def record_opening_position(self, request: OpeningPositionRequest) -> JournalEntry:
        """Post one reconciled opening balance against opening equity."""
        with self._unit_of_work_factory() as unit:
            entry = self.stage_opening_position(unit, request)
            unit.commit()
        return entry

    def stage_opening_position(
        self, unit: UnitOfWork, request: OpeningPositionRequest
    ) -> JournalEntry:
        """Stage an opening position inside a caller-owned atomic workflow."""
        account = unit.financial_accounts.get(request.financial_account_id)
        if account is None:
            raise LedgerConfigurationError("financial account was not found")
        ledger_account = unit.ledger_accounts.get_for_financial_account(account.id)
        if ledger_account is None:
            raise LedgerConfigurationError(
                "financial account has no physical ledger account"
            )
        balance = Money(request.balance_minor, account.currency)
        physical_amount, equity_amount = opening_posting_amount(
            ledger_account.kind, balance
        )
        equity = self._opening_equity_account(unit, account.currency)
        postings = (
            *self._opening_physical_postings(
                unit,
                account_id=account.id,
                ledger_account=ledger_account,
                physical_amount=physical_amount,
                splits=request.splits,
            ),
            PostingDraft(ledger_account_id=equity.id, amount=equity_amount),
        )
        entry, _ = self._post(
            unit,
            entry_type=JournalEntryType.OPENING_BALANCE,
            effective_at=request.observed_at,
            idempotency_key=f"opening_balance:financial_account:{account.id}",
            description=request.description,
            postings=postings,
            reversal_of_id=None,
        )
        return entry

    def _post(
        self,
        unit: UnitOfWork,
        *,
        entry_type: JournalEntryType,
        effective_at: datetime,
        idempotency_key: str,
        description: str,
        postings: tuple[PostingDraft, ...],
        reversal_of_id: int | None,
        financial_event_id: int | None = None,
    ) -> tuple[JournalEntry, bool]:
        """Validate one draft inside an open transaction without committing."""
        ensure_balanced(postings)
        ensure_reversal_shape(entry_type, reversal_of_id)
        effective = _require_aware(effective_at)
        existing = unit.journal.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            _ensure_same_operation(
                existing,
                entry_type=entry_type,
                effective_at=effective,
                postings=postings,
                reversal_of_id=reversal_of_id,
                financial_event_id=financial_event_id,
            )
            return existing, False
        self._validate_postings(unit, postings)
        if reversal_of_id is not None:
            self._validate_reversal_target(unit, reversal_of_id, postings)
        if financial_event_id is not None:
            self._validate_event_link(unit, financial_event_id, effective)
        entry = unit.journal.add(
            JournalEntryDraft(
                entry_type=entry_type,
                effective_at=effective,
                recorded_at=self._clock(),
                idempotency_key=idempotency_key,
                description=description,
                postings=postings,
                reversal_of_id=reversal_of_id,
                financial_event_id=financial_event_id,
            )
        )
        return entry, True

    @staticmethod
    def _validate_event_link(
        unit: UnitOfWork,
        financial_event_id: int,
        effective_at: datetime,
    ) -> None:
        """Keep event entries unique and outside an applied opening boundary."""
        event = unit.financial_events.get(financial_event_id)
        if event is None:
            raise LedgerConfigurationError("financial event was not found")
        if unit.journal.get_by_financial_event(financial_event_id) is not None:
            raise LedgerConfigurationError(
                "this financial event already has a journal entry"
            )
        ensure_event_after_opening_cutoff(
            unit,
            event,
            effective_at=effective_at,
        )

    def _opening_equity_account(self, unit: UnitOfWork, currency: str) -> LedgerAccount:
        code = opening_equity_code(currency)
        equity = unit.ledger_accounts.get_by_code(code)
        if equity is not None:
            return equity
        return unit.ledger_accounts.add(
            LedgerAccountDraft(
                code=code,
                name=f"{OPENING_EQUITY_NAME} ({currency})",
                kind=LedgerAccountKind.EQUITY,
                currency=currency,
            )
        )

    def _opening_physical_postings(
        self,
        unit: UnitOfWork,
        *,
        account_id: int,
        ledger_account: LedgerAccount,
        physical_amount: Money,
        splits: tuple[OpeningWalletSplit, ...],
    ) -> tuple[PostingDraft, ...]:
        if not is_wallet_managed(ledger_account):
            if splits:
                raise OpeningPositionError(
                    "only wallet-managed asset accounts accept an opening split"
                )
            return (
                PostingDraft(
                    ledger_account_id=ledger_account.id, amount=physical_amount
                ),
            )
        if not splits:
            raise OpeningPositionError(
                "an asset opening position requires an explicit wallet split"
            )
        total = sum(split.amount_minor for split in splits)
        if total != physical_amount.amount_minor:
            raise OpeningPositionError(
                "the opening wallet split must equal the account balance"
            )
        postings: list[PostingDraft] = []
        seen: set[int] = set()
        for split in splits:
            wallet = unit.wallets.get_by_name(account_id, split.wallet_name)
            if wallet is None:
                raise LedgerConfigurationError(
                    f"wallet '{split.wallet_name}' was not found in this account"
                )
            if wallet.id in seen:
                raise OpeningPositionError(
                    "each wallet may appear only once in an opening split"
                )
            seen.add(wallet.id)
            if split.amount_minor == 0:
                # A zero wallet is valid setup evidence but cannot become a
                # journal posting: every persisted posting must be nonzero.
                continue
            postings.append(
                PostingDraft(
                    ledger_account_id=ledger_account.id,
                    amount=Money(split.amount_minor, physical_amount.currency),
                    wallet_id=wallet.id,
                )
            )
        return tuple(postings)

    @staticmethod
    def _validate_reversal_target(
        unit: UnitOfWork, entry_id: int, postings: tuple[PostingDraft, ...]
    ) -> None:
        original = unit.journal.get(entry_id)
        if original is None:
            raise LedgerConfigurationError("the reversed journal entry was not found")
        if original.reversal_of_id is not None:
            raise LedgerConfigurationError("a reversal cannot itself be reversed")
        if unit.journal.get_reversal_of(entry_id) is not None:
            raise LedgerConfigurationError(
                "this journal entry has already been reversed"
            )
        ensure_exact_reversal(original.postings, postings)

    @staticmethod
    def _validate_postings(
        unit: UnitOfWork, postings: tuple[PostingDraft, ...]
    ) -> None:
        for posting in postings:
            ledger_account = unit.ledger_accounts.get(posting.ledger_account_id)
            if ledger_account is None:
                raise LedgerConfigurationError("ledger account was not found")
            if ledger_account.currency != posting.amount.currency:
                raise CurrencyMismatchError(
                    "posting currency must match its ledger account currency"
                )
            if is_wallet_managed(ledger_account):
                if posting.wallet_id is None:
                    raise WalletAssignmentError(
                        "postings to a wallet-managed account require a wallet"
                    )
                wallet = unit.wallets.get(posting.wallet_id)
                if wallet is None:
                    raise WalletAssignmentError("wallet was not found")
                if wallet.financial_account_id != ledger_account.financial_account_id:
                    raise WalletAssignmentError(
                        "wallet belongs to a different financial account"
                    )
                if not wallet.is_active:
                    raise WalletAssignmentError("wallet is inactive")
            elif posting.wallet_id is not None:
                raise WalletAssignmentError(
                    "only wallet-managed asset accounts accept a wallet"
                )
            # Equality in both directions: a category ledger needs that exact
            # category, and a ledger without one must not be given a category,
            # which would otherwise attach spending to an equity or asset line.
            if posting.budget_category_id != ledger_account.budget_category_id:
                raise LedgerConfigurationError(
                    "a posting must carry exactly its ledger account's budget category"
                )


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("ledger timestamps must be timezone-aware")
    return value


def _ensure_same_operation(
    existing: JournalEntry,
    *,
    entry_type: JournalEntryType,
    effective_at: datetime,
    postings: tuple[PostingDraft, ...],
    reversal_of_id: int | None,
    financial_event_id: int | None = None,
) -> None:
    """Reject a retry that reuses one key for a different accounting operation.

    A matching key alone never proves that two requests are the same work. The
    description is deliberately excluded, because it is display text and does
    not change the accounting effect of the entry.
    """
    same = (
        existing.entry_type is entry_type
        and existing.reversal_of_id == reversal_of_id
        and existing.financial_event_id == financial_event_id
        and existing.effective_at == effective_at
        and posted_signatures(existing.postings) == draft_signatures(postings)
    )
    if not same:
        raise IdempotencyConflictError(
            "this idempotency key already records a different journal entry"
        )

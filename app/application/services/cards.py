"""Local card configuration: identity, settlement accounts, and backfill.

A card is only ever identified by institution plus last four. This service
configures where a card settles; it never creates a manual expense or a
financial event.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import cast

from app.domain.budget_enums import (
    CardConfigurationStatus,
    CardHolderRole,
    CardImportAssignmentKind,
    FinancialAccountType,
)
from app.domain.budget_models import (
    Card,
    CardConfigurationDraft,
    CardDraft,
    CardIdentity,
    CardImportAssignment,
    CardImportAssignmentDraft,
    normalize_last_four,
)
from app.domain.budget_rules import ensure_single_card_currency, holds_wallets
from app.domain.cutover import ImportRowEvidence, ImportRowStatus
from app.domain.errors import CardConfigurationError
from app.domain.models import GmailAccount, SenderRule
from app.domain.ports.repositories import UnitOfWork

type UnitOfWorkFactory = Callable[[], UnitOfWork]


@dataclass(frozen=True)
class RegisterCardRequest:
    """User intent to record one card identity for later configuration."""

    gmail_account_id: int
    issuer_code: str
    display_name: str
    last_four: str
    holder_role: CardHolderRole = CardHolderRole.UNKNOWN


@dataclass(frozen=True)
class ConfigureCardRequest:
    """User intent to set where one card settles and how it is funded."""

    card_id: int
    expected_version: int
    display_name: str
    holder_role: CardHolderRole
    liability_account_id: int | None
    settlement_account_id: int | None
    default_funding_wallet_id: int | None


@dataclass(frozen=True)
class CardBackfillResult:
    """Outcome of one idempotent supplementary-card backfill run."""

    created: tuple[int, ...]
    already_present: tuple[int, ...]

    @property
    def created_count(self) -> int:
        """Return how many card identities this run created."""
        return len(self.created)

    @property
    def already_present_count(self) -> int:
        """Return how many identities already existed and were left alone."""
        return len(self.already_present)


@dataclass(frozen=True)
class CardImportCandidate:
    """A safe presentation of one immutable cutover row awaiting a card."""

    import_row_id: int
    kind: CardImportAssignmentKind
    source_label: str
    statement_day: int | None = None
    due_day: int | None = None
    autopay_day: int | None = None
    scheduled_for: date | None = None
    amount_minor: int | None = None
    currency: str | None = None


@dataclass(frozen=True)
class CardSetupView:
    """All local configuration inputs needed by the card page."""

    gmail_accounts: tuple[GmailAccount, ...]
    sender_rules: tuple[SenderRule, ...]
    candidates: tuple[CardImportCandidate, ...]
    assignments: tuple[CardImportAssignment, ...]


class ManageCards:
    """Create and configure local cards without inferring any mapping."""

    def __init__(self, unit_of_work_factory: UnitOfWorkFactory) -> None:
        self._unit_of_work_factory = unit_of_work_factory

    def list_cards(self) -> tuple[Card, ...]:
        """Return every configured card."""
        with self._unit_of_work_factory() as unit:
            return unit.cards.list_all()

    def setup_view(self) -> CardSetupView:
        """Return account choices and reviewed import hints for the Web page."""
        with self._unit_of_work_factory() as unit:
            accounts = unit.accounts.list_all()
            rules = tuple(
                rule
                for account in accounts
                for rule in unit.sender_rules.list_enabled(account.id)
            )
            assigned_ids = {
                assignment.import_row_id
                for assignment in unit.card_import_assignments.list_all()
            }
            candidates = tuple(
                self._candidate(row)
                for row in unit.import_rows.list_pending_card_candidates()
                if row.id not in assigned_ids
            )
            return CardSetupView(
                gmail_accounts=accounts,
                sender_rules=rules,
                candidates=candidates,
                assignments=unit.card_import_assignments.list_all(),
            )

    def assign_import_candidate(
        self, *, import_row_id: int, card_id: int
    ) -> CardImportAssignment:
        """Map an immutable card hint without scheduling or moving money."""
        with self._unit_of_work_factory() as unit:
            if unit.cards.get(card_id) is None:
                raise CardConfigurationError("card was not found")
            row = unit.import_rows.get(import_row_id)
            if row is None:
                raise CardConfigurationError("import candidate was not found")
            if unit.card_import_assignments.get_for_import_row(import_row_id):
                raise CardConfigurationError(
                    "this import candidate is already assigned"
                )
            candidate = self._candidate(row)
            if candidate.kind is CardImportAssignmentKind.CARD_CYCLE and any(
                item.card_id == card_id
                and item.assignment_kind is CardImportAssignmentKind.CARD_CYCLE
                for item in unit.card_import_assignments.list_all()
            ):
                raise CardConfigurationError("this card already has a billing cycle")
            assignment = unit.card_import_assignments.add(
                CardImportAssignmentDraft(
                    import_row_id=row.id,
                    card_id=card_id,
                    assignment_kind=candidate.kind,
                    statement_day=candidate.statement_day,
                    due_day=candidate.due_day,
                    autopay_day=candidate.autopay_day,
                )
            )
            unit.commit()
        return assignment

    def register(self, request: RegisterCardRequest) -> Card:
        """Record one card identity; settlement is configured separately."""
        last_four = normalize_last_four(request.last_four)
        with self._unit_of_work_factory() as unit:
            if unit.accounts.get(request.gmail_account_id) is None:
                raise CardConfigurationError("Gmail account was not found")
            identity = CardIdentity(
                gmail_account_id=request.gmail_account_id,
                issuer_code=request.issuer_code.strip(),
                last_four=last_four,
            )
            if unit.cards.get_by_identity(identity) is not None:
                raise CardConfigurationError(
                    "this issuer and last-four pair is already configured"
                )
            card = unit.cards.add(
                CardDraft(
                    gmail_account_id=request.gmail_account_id,
                    issuer_code=request.issuer_code,
                    display_name=request.display_name,
                    last_four=last_four,
                    holder_role=request.holder_role,
                )
            )
            unit.commit()
        return card

    def configure(self, request: ConfigureCardRequest) -> Card:
        """Validate and store one card's settlement configuration."""
        with self._unit_of_work_factory() as unit:
            card = unit.cards.get(request.card_id)
            if card is None:
                raise CardConfigurationError("card was not found")
            currencies = self._validated_currencies(unit, request)
            ensure_single_card_currency(currencies)
            updated = unit.cards.save_configuration(
                CardConfigurationDraft(
                    card_id=request.card_id,
                    expected_version=request.expected_version,
                    display_name=request.display_name,
                    holder_role=request.holder_role,
                    liability_account_id=request.liability_account_id,
                    settlement_account_id=request.settlement_account_id,
                    default_funding_wallet_id=request.default_funding_wallet_id,
                )
            )
            unit.commit()
        return updated

    def backfill_from_supplementary_settings(
        self, *, dry_run: bool = False
    ) -> CardBackfillResult:
        """Create card identities from explicit supplementary-card decisions.

        Only issuer, last four, and the supplementary holder role migrate.
        Settlement and funding accounts are never guessed. Running this again
        leaves every existing card, including user edits, untouched.
        """
        created: list[int] = []
        already_present: list[int] = []
        with self._unit_of_work_factory() as unit:
            for setting in unit.supplementary_card_settings.list_all():
                identity = CardIdentity(
                    gmail_account_id=setting.gmail_account_id,
                    issuer_code=setting.institution_key,
                    last_four=setting.card_last_four,
                )
                existing = unit.cards.get_by_identity(identity)
                if existing is not None:
                    already_present.append(existing.id)
                    continue
                if dry_run:
                    created.append(0)
                    continue
                card = unit.cards.add(
                    CardDraft(
                        gmail_account_id=setting.gmail_account_id,
                        issuer_code=setting.institution_key,
                        display_name=(
                            f"{setting.institution_key} ****{setting.card_last_four}"
                        ),
                        last_four=setting.card_last_four,
                        holder_role=CardHolderRole.SUPPLEMENTARY,
                    )
                )
                created.append(card.id)
            if not dry_run and created:
                unit.commit()
        return CardBackfillResult(
            created=tuple(created), already_present=tuple(already_present)
        )

    @staticmethod
    def _validated_currencies(
        unit: UnitOfWork, request: ConfigureCardRequest
    ) -> tuple[str, ...]:
        """Enforce every card readiness invariant before anything is stored."""
        currencies: list[str] = []
        if request.liability_account_id is not None:
            liability = unit.financial_accounts.get(request.liability_account_id)
            if liability is None or not liability.is_active:
                raise CardConfigurationError("liability account was not found")
            if liability.account_type is not FinancialAccountType.CARD_LIABILITY:
                raise CardConfigurationError(
                    "a card's liability account must be a card_liability account"
                )
            currencies.append(liability.currency)
        if request.settlement_account_id is not None:
            settlement = unit.financial_accounts.get(request.settlement_account_id)
            if settlement is None or not settlement.is_active:
                raise CardConfigurationError("settlement account was not found")
            if not holds_wallets(settlement.account_type):
                raise CardConfigurationError(
                    "a card's settlement account must be an asset account"
                )
            currencies.append(settlement.currency)
        if request.default_funding_wallet_id is not None:
            wallet = unit.wallets.get(request.default_funding_wallet_id)
            if wallet is None or not wallet.is_active:
                raise CardConfigurationError("default funding wallet was not found")
            account = unit.financial_accounts.get(wallet.financial_account_id)
            if account is None or not account.is_active:  # pragma: no cover
                raise CardConfigurationError(
                    "the funding wallet's account is not active"
                )
            if not holds_wallets(account.account_type):  # pragma: no cover
                raise CardConfigurationError(
                    "a funding wallet must belong to an asset account"
                )
            currencies.append(account.currency)
        return tuple(currencies)

    @staticmethod
    def _candidate(row: object) -> CardImportCandidate:
        """Strictly decode only the two B9 shapes this workflow understands."""
        if not isinstance(row, ImportRowEvidence):  # pragma: no cover
            raise CardConfigurationError("invalid import candidate")
        if (
            row.status is not ImportRowStatus.PENDING
            or row.reason_code != "card_mapping_required"
        ):
            raise CardConfigurationError("this import row is not awaiting card mapping")
        try:
            payload = json.loads(row.normalized_payload_json)
        except (json.JSONDecodeError, TypeError) as error:
            raise CardConfigurationError(
                "the import candidate payload is invalid"
            ) from error
        if not isinstance(payload, dict):
            raise CardConfigurationError("the import candidate payload is invalid")
        if row.entity_kind == "card_cycle":
            bank = payload.get("bank")
            days = tuple(
                payload.get(key) for key in ("statement_day", "due_day", "autopay_day")
            )
            if any(
                isinstance(value, bool) or not isinstance(value, int) for value in days
            ):
                raise CardConfigurationError("the imported card cycle is invalid")
            statement, due, autopay = cast(tuple[int, int, int], days)
            if any(value < 1 or value > 31 for value in (statement, due, autopay)):
                raise CardConfigurationError("the imported card cycle is invalid")
            return CardImportCandidate(
                import_row_id=row.id,
                kind=CardImportAssignmentKind.CARD_CYCLE,
                source_label=(
                    bank.strip()
                    if isinstance(bank, str) and bank.strip()
                    else row.source_row_key
                ),
                statement_day=statement,
                due_day=due,
                autopay_day=autopay,
            )
        if row.entity_kind == "scheduled_transfer_candidate":
            amount = payload.get("amount_minor")
            # Version 1 cutover documents are TWD-only and named this field
            # ``date``. Accept the explicit future spelling without rewriting
            # the immutable source row.
            currency = payload.get("currency", "TWD")
            scheduled = payload.get("scheduled_for", payload.get("date"))
            destination = payload.get("destination")
            if (
                isinstance(amount, bool)
                or not isinstance(amount, int)
                or amount <= 0
                or not isinstance(currency, str)
                or len(currency) != 3
                or not isinstance(scheduled, str)
                or not isinstance(destination, str)
                or not destination.strip()
            ):
                raise CardConfigurationError("the imported transfer hint is invalid")
            try:
                scheduled_for = date.fromisoformat(scheduled)
            except ValueError as error:
                raise CardConfigurationError(
                    "the imported transfer hint is invalid"
                ) from error
            return CardImportCandidate(
                import_row_id=row.id,
                kind=CardImportAssignmentKind.SCHEDULED_TRANSFER_REFERENCE,
                source_label=destination.strip(),
                scheduled_for=scheduled_for,
                amount_minor=amount,
                currency=currency.upper(),
            )
        raise CardConfigurationError("this import row is not a supported card hint")


def card_is_ready(card: Card) -> bool:
    """Return whether a card may fund a posted expense in a later milestone."""
    return card.configuration_status is CardConfigurationStatus.READY

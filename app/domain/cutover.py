"""Versioned, normalized inputs and audit records for a local cutover."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from app.domain.budget_enums import PaymentMethod, SalaryCalculationMode, WalletPurpose
from app.domain.budget_models import normalize_currency, validate_signed_minor_units


class ImportBatchStatus(StrEnum):
    """Lifecycle of one reviewed source snapshot."""

    PREVIEWED = "previewed"
    APPLYING = "applying"
    APPLIED = "applied"
    FAILED = "failed"


class ImportRowStatus(StrEnum):
    """How one normalized source row was handled."""

    APPLIED = "applied"
    HISTORICAL = "historical"
    PENDING = "pending"
    SKIPPED = "skipped"
    WARNING = "warning"


@dataclass(frozen=True)
class CutoverWallet:
    """One signed opening envelope in a physical account."""

    name: str
    balance_minor: int
    purpose: WalletPurpose = WalletPurpose.GENERAL

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", self.name.strip())
        if not self.name:
            raise ValueError("a cutover wallet needs a name")
        object.__setattr__(
            self,
            "balance_minor",
            validate_signed_minor_units(
                self.balance_minor, label="cutover wallet balance"
            ),
        )


@dataclass(frozen=True)
class CutoverAccount:
    """One observed bank balance and its complete signed wallet split."""

    source_key: str
    institution_code: str
    display_name: str
    currency: str
    masked_reference: str | None
    balance_minor: int
    wallets: tuple[CutoverWallet, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "currency", normalize_currency(self.currency))
        object.__setattr__(
            self,
            "balance_minor",
            validate_signed_minor_units(
                self.balance_minor, label="cutover account balance"
            ),
        )
        if not self.source_key.strip() or not self.display_name.strip():
            raise ValueError("a cutover account needs a source key and display name")
        names = [wallet.name for wallet in self.wallets]
        if len(names) != len(set(names)):
            raise ValueError("wallet names must be unique inside one account")
        if sum(wallet.balance_minor for wallet in self.wallets) != self.balance_minor:
            raise ValueError("the signed wallet split must equal the account balance")


@dataclass(frozen=True)
class CutoverCategory:
    """One root budget category imported as current configuration."""

    source_key: str
    name: str


@dataclass(frozen=True)
class CutoverTarget:
    """One calendar-month target in minor units."""

    source_key: str
    year: int
    month: int
    currency: str
    category_key: str
    target_minor: int
    buffer_minor: int
    funding_account_key: str
    funding_wallet_name: str


@dataclass(frozen=True)
class CutoverSubscription:
    """One active monthly subscription and its first effective price."""

    source_key: str
    name: str
    amount_minor: int
    category_key: str
    funding_account_key: str
    funding_wallet_name: str
    effective_from: date


@dataclass(frozen=True)
class CutoverSalaryItem:
    """One destination in the current salary allocation template."""

    source_key: str
    name: str
    destination_account_key: str
    destination_wallet_name: str
    calculation_mode: SalaryCalculationMode
    fixed_amount_minor: int | None
    category_key: str | None = None


@dataclass(frozen=True)
class CutoverSalaryTemplate:
    """The current salary source and its ordered destinations."""

    source_key: str
    name: str
    currency: str
    source_account_key: str
    items: tuple[CutoverSalaryItem, ...]


@dataclass(frozen=True)
class LegacyDailyActivity:
    """One pre-opening Sheet row retained without posting it again."""

    source_key: str
    activity_date: date
    sequence: int
    amount_minor: int
    currency: str
    payment_method: PaymentMethod
    note: str | None

    def __post_init__(self) -> None:
        if self.amount_minor <= 0:
            raise ValueError("legacy daily activity must have a positive amount")
        if self.payment_method not in {PaymentMethod.CASH, PaymentMethod.CREDIT_CARD}:
            raise ValueError("legacy daily activity is cash or credit card only")
        object.__setattr__(self, "currency", normalize_currency(self.currency))


@dataclass(frozen=True)
class PendingSourceEvidence:
    """A source fact deliberately retained for later human configuration."""

    source_key: str
    entity_kind: str
    reason_code: str
    payload: dict[str, object]


@dataclass(frozen=True)
class CutoverDocument:
    """The complete normalized file approved for one local import."""

    format_version: int
    source_kind: str
    source_locator_hash: str
    observed_at: datetime
    cutoff_at: datetime
    accounts: tuple[CutoverAccount, ...]
    categories: tuple[CutoverCategory, ...]
    targets: tuple[CutoverTarget, ...]
    subscriptions: tuple[CutoverSubscription, ...]
    salary_template: CutoverSalaryTemplate | None
    legacy_daily: tuple[LegacyDailyActivity, ...]
    pending: tuple[PendingSourceEvidence, ...]

    def __post_init__(self) -> None:
        if self.format_version != 1:
            raise ValueError("unsupported cutover format version")
        for value in (self.observed_at, self.cutoff_at):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("cutover timestamps must be timezone-aware")
        if self.cutoff_at > self.observed_at:
            raise ValueError("the historical cutoff cannot follow the observation")
        keys = [account.source_key for account in self.accounts]
        if len(keys) != len(set(keys)):
            raise ValueError("cutover account keys must be unique")


@dataclass(frozen=True)
class ImportBatch:
    """Persisted identity and outcome of one source snapshot."""

    id: int
    content_sha256: str
    observed_at: datetime
    cutoff_at: datetime
    status: ImportBatchStatus
    row_count: int
    warning_count: int
    applied_at: datetime | None


@dataclass(frozen=True)
class ImportRowEvidence:
    """One immutable normalized source row available to later setup workflows."""

    id: int
    batch_id: int
    entity_kind: str
    source_row_key: str
    normalized_payload_json: str
    status: ImportRowStatus
    reason_code: str | None


@dataclass(frozen=True)
class ImportPreview:
    """Human-reviewable totals for a normalized file."""

    content_sha256: str
    account_count: int
    wallet_count: int
    opening_totals: tuple[ImportCurrencyTotal, ...]
    category_count: int
    subscription_count: int
    salary_item_count: int
    legacy_daily_count: int
    legacy_daily_totals: tuple[ImportCurrencyTotal, ...]
    pending_count: int


@dataclass(frozen=True)
class ImportCurrencyTotal:
    """One import preview total in exactly one currency."""

    currency: str
    amount_minor: int


@dataclass(frozen=True)
class ImportApplyResult:
    """Applied batch summary, replayed unchanged for the same file."""

    batch: ImportBatch
    created: bool
    preview: ImportPreview

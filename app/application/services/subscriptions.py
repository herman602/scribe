"""Manage the subscription register and calculate its monthly requirement.

The register is the one canonical source of subscription funding. Nothing in
this module posts a journal entry, creates a funding commitment, or moves a
balance: a requirement is a calculation, and freezing it into a salary run
belongs to a later milestone.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from app.application.queries.subscriptions import (
    DefaultFundingWalletView,
    SubscriptionReadRepository,
    SubscriptionRequirementInput,
)
from app.domain.budget_enums import (
    SubscriptionCadence,
    SubscriptionRequirementReason,
)
from app.domain.budget_models import (
    Subscription,
    SubscriptionDraft,
    SubscriptionPricePeriod,
    SubscriptionPricePeriodDraft,
    SubscriptionPricePeriodUpdate,
    SubscriptionRequirement,
    SubscriptionRequirementItem,
    SubscriptionUpdate,
    normalize_currency,
    validate_month_start,
)
from app.domain.budget_rules import (
    activity_ranges_overlap,
    cycle_position,
    cycle_share_minor,
    frozen_configuration_changes,
    holds_wallets,
    month_activity_reason,
    month_start,
)
from app.domain.errors import (
    LedgerConfigurationError,
    SubscriptionError,
    SubscriptionOverlapError,
)
from app.domain.ports.repositories import UnitOfWork

type UnitOfWorkFactory = Callable[[], UnitOfWork]

#: Stable, non-secret setting key. The Subscription wallet is real bank
#: configuration, so it is stored as data rather than written into code.
DEFAULT_FUNDING_WALLET_SETTING = "budget.default_subscription_funding_wallet_id"

WALLET_MISSING = "wallet_missing"
WALLET_INACTIVE = "wallet_inactive"
WALLET_ACCOUNT_MISSING = "wallet_account_missing"
WALLET_ACCOUNT_INACTIVE = "wallet_account_inactive"
WALLET_NOT_ASSET = "wallet_not_asset"


def _month_label(value: date | None, *, fallback: str) -> str:
    return fallback if value is None else f"{value.year:04d}-{value.month:02d}"


def _range_label(subscription: Subscription) -> str:
    """Describe one interval's months, so a refusal names what it clashed with."""
    start = _month_label(subscription.active_from, fallback="the beginning")
    end = _month_label(subscription.active_to, fallback="no end month")
    return f"{start} to {end}"


@dataclass(frozen=True)
class AddSubscriptionRequest:
    """One new recurring service and the money that will fund it."""

    name: str
    budget_category_id: int
    funding_wallet_id: int
    currency: str
    billing_cadence: SubscriptionCadence
    cadence_months: int
    billing_month: int | None = None
    billing_day: int | None = None
    active_from: date | None = None
    active_to: date | None = None


@dataclass(frozen=True)
class UpdateSubscriptionRequest:
    """One optimistic correction of an existing subscription.

    Activation is deliberately absent: a subscription is stopped through
    ``deactivate`` and resumed as a new interval through ``resume``, so an
    edit can never quietly restart something that already ended.
    """

    subscription_id: int
    expected_version: int
    name: str
    budget_category_id: int
    funding_wallet_id: int
    currency: str
    billing_cadence: SubscriptionCadence
    cadence_months: int
    billing_month: int | None
    billing_day: int | None
    active_from: date | None
    active_to: date | None


@dataclass(frozen=True)
class DeactivateSubscriptionRequest:
    """End one subscription from an explicit month."""

    subscription_id: int
    expected_version: int
    active_to: date


@dataclass(frozen=True)
class ResumeSubscriptionRequest:
    """Start a new interval of a subscription that had been stopped.

    Resuming never clears the old row's end month. The stop stays in the
    record and the new interval begins where the user says it begins, so
    use, stop, and use again are three facts the register can all show.
    """

    subscription_id: int
    active_from: date
    active_to: date | None = None


@dataclass(frozen=True)
class AddPricePeriodRequest:
    """One effective-dated price for an existing subscription."""

    subscription_id: int
    amount_minor: int
    effective_from: date
    effective_to: date | None = None


@dataclass(frozen=True)
class UpdatePricePeriodRequest:
    """One optimistic correction of an existing effective-dated price."""

    price_period_id: int
    expected_version: int
    amount_minor: int
    effective_from: date
    effective_to: date | None


class ManageSubscriptions:
    """Create and correct the register, and name its default wallet."""

    def __init__(self, unit_of_work_factory: UnitOfWorkFactory) -> None:
        self._unit_of_work_factory = unit_of_work_factory

    def add(self, request: AddSubscriptionRequest) -> Subscription:
        """Register one subscription after validating its funding path."""
        draft = SubscriptionDraft(
            name=request.name,
            budget_category_id=request.budget_category_id,
            funding_wallet_id=request.funding_wallet_id,
            currency=request.currency,
            billing_cadence=request.billing_cadence,
            cadence_months=request.cadence_months,
            billing_month=request.billing_month,
            billing_day=request.billing_day,
            active_from=request.active_from,
            active_to=request.active_to,
        )
        with self._unit_of_work_factory() as unit:
            self._require_no_interval_overlap(
                unit,
                name=draft.name,
                active_from=draft.active_from,
                active_to=draft.active_to,
            )
            self._require_no_running_namesake(unit, name=draft.name)
            self._require_category(unit, draft.budget_category_id, must_be_active=True)
            self._require_funding_wallet(
                unit, draft.funding_wallet_id, currency=draft.currency
            )
            subscription = unit.subscriptions.add(draft)
            unit.commit()
        return subscription

    def update(self, request: UpdateSubscriptionRequest) -> Subscription:
        """Correct one subscription under an exact optimistic version.

        Once a price period exists, the configuration that price is read
        through is fixed. A price row stores an amount and a range only, so
        editing the currency, cadence, anchor, category, or funding wallet
        would reinterpret every month that was already calculated.
        """
        with self._unit_of_work_factory() as unit:
            current = unit.subscriptions.get(request.subscription_id)
            if current is None:
                raise SubscriptionError("subscription was not found")
            change = SubscriptionUpdate(
                subscription_id=request.subscription_id,
                expected_version=request.expected_version,
                name=request.name,
                budget_category_id=request.budget_category_id,
                funding_wallet_id=request.funding_wallet_id,
                currency=request.currency,
                billing_cadence=request.billing_cadence,
                cadence_months=request.cadence_months,
                billing_month=request.billing_month,
                billing_day=request.billing_day,
                active_from=request.active_from,
                active_to=request.active_to,
                is_active=current.is_active,
            )
            self._require_configuration_unchanged(unit, current, change)
            # An already chosen category may since have been deactivated;
            # only a change has to point at a category still in use.
            self._require_category(
                unit,
                change.budget_category_id,
                must_be_active=change.budget_category_id != current.budget_category_id,
            )
            self._require_funding_wallet(
                unit, change.funding_wallet_id, currency=change.currency
            )
            # Renaming and re-dating both move an interval inside its
            # service's timeline, so either can create an overlap.
            self._require_no_interval_overlap(
                unit,
                name=change.name,
                active_from=change.active_from,
                active_to=change.active_to,
                exclude_id=current.id,
            )
            if current.is_active:
                self._require_no_running_namesake(
                    unit, name=change.name, exclude_id=current.id
                )
            subscription = unit.subscriptions.save(change)
            unit.commit()
        return subscription

    def deactivate(self, request: DeactivateSubscriptionRequest) -> Subscription:
        """End one subscription from an explicit month.

        Deactivating records the month the contribution stopped instead of
        hiding the row, so every earlier month keeps the requirement it had.
        """
        active_to = validate_month_start(request.active_to, label="active to")
        if active_to is None:
            raise SubscriptionError(
                "deactivating a subscription needs the month it stopped"
            )
        with self._unit_of_work_factory() as unit:
            current = unit.subscriptions.get(request.subscription_id)
            if current is None:
                raise SubscriptionError("subscription was not found")
            if not current.is_active:
                raise SubscriptionError("this subscription has already stopped")
            self._require_no_interval_overlap(
                unit,
                name=current.name,
                active_from=current.active_from,
                active_to=active_to,
                exclude_id=current.id,
            )
            subscription = unit.subscriptions.save(
                SubscriptionUpdate(
                    subscription_id=current.id,
                    expected_version=request.expected_version,
                    name=current.name,
                    budget_category_id=current.budget_category_id,
                    funding_wallet_id=current.funding_wallet_id,
                    currency=current.currency,
                    billing_cadence=current.billing_cadence,
                    cadence_months=current.cadence_months,
                    billing_month=current.billing_month,
                    billing_day=current.billing_day,
                    active_from=current.active_from,
                    active_to=active_to,
                    is_active=False,
                )
            )
            unit.commit()
        return subscription

    def resume(self, request: ResumeSubscriptionRequest) -> Subscription:
        """Start a new interval of a subscription that had been stopped.

        A single ``[active_from, active_to)`` range cannot say "used, then
        stopped, then used again". Clearing the old row's end month would
        instead delete the stop, and every month between would silently start
        contributing again. Resuming therefore copies the configuration into a
        new subscription that begins on or after the month the old one ended,
        and leaves the old row exactly as it was.
        """
        active_from = validate_month_start(request.active_from, label="active from")
        if active_from is None:
            raise SubscriptionError("resuming a subscription needs its restart month")
        with self._unit_of_work_factory() as unit:
            current = unit.subscriptions.get(request.subscription_id)
            if current is None:
                raise SubscriptionError("subscription was not found")
            if current.is_active or current.active_to is None:
                raise SubscriptionError(
                    "only a stopped subscription starts a new interval"
                )
            if active_from < current.active_to:
                raise SubscriptionError(
                    "a new interval starts no earlier than the month the "
                    "previous one ended"
                )
            # Compared against every interval this service has held, not only
            # the one being resumed from: an older interval, or one added
            # separately under the same name, could still cover these months.
            self._require_no_interval_overlap(
                unit,
                name=current.name,
                active_from=active_from,
                active_to=validate_month_start(request.active_to, label="active to"),
                exclude_id=current.id,
            )
            self._require_no_running_namesake(
                unit, name=current.name, exclude_id=current.id
            )
            self._require_category(
                unit, current.budget_category_id, must_be_active=True
            )
            self._require_funding_wallet(
                unit, current.funding_wallet_id, currency=current.currency
            )
            resumed = unit.subscriptions.add(
                SubscriptionDraft(
                    name=current.name,
                    budget_category_id=current.budget_category_id,
                    funding_wallet_id=current.funding_wallet_id,
                    currency=current.currency,
                    billing_cadence=current.billing_cadence,
                    cadence_months=current.cadence_months,
                    billing_month=current.billing_month,
                    billing_day=current.billing_day,
                    active_from=active_from,
                    active_to=validate_month_start(
                        request.active_to, label="active to"
                    ),
                )
            )
            unit.commit()
        return resumed

    def get(self, subscription_id: int) -> Subscription | None:
        """Return one subscription, so a caller can name its version."""
        with self._unit_of_work_factory() as unit:
            return unit.subscriptions.get(subscription_id)

    def list_all(self) -> tuple[Subscription, ...]:
        """Return every subscription, deactivated ones included."""
        with self._unit_of_work_factory() as unit:
            return unit.subscriptions.list_all()

    def list_prices(self, subscription_id: int) -> tuple[SubscriptionPricePeriod, ...]:
        """Return one subscription's price history, oldest range first."""
        with self._unit_of_work_factory() as unit:
            return unit.subscription_prices.list_for_subscription(subscription_id)

    def get_price(self, price_period_id: int) -> SubscriptionPricePeriod | None:
        """Return one price period, so a caller can name its version."""
        with self._unit_of_work_factory() as unit:
            return unit.subscription_prices.get(price_period_id)

    def add_price(self, request: AddPricePeriodRequest) -> SubscriptionPricePeriod:
        """Add one non-overlapping effective-dated price.

        The overlap check and the insert share one transaction, so a refused
        price leaves no half-written period behind.
        """
        draft = SubscriptionPricePeriodDraft(
            subscription_id=request.subscription_id,
            amount_minor=request.amount_minor,
            effective_from=request.effective_from,
            effective_to=request.effective_to,
        )
        with self._unit_of_work_factory() as unit:
            if unit.subscriptions.get(draft.subscription_id) is None:
                raise SubscriptionError("subscription was not found")
            self._require_no_overlap(
                unit,
                subscription_id=draft.subscription_id,
                effective_from=draft.effective_from,
                effective_to=draft.effective_to,
            )
            price = unit.subscription_prices.add(draft)
            unit.commit()
        return price

    def update_price(
        self, request: UpdatePricePeriodRequest
    ) -> SubscriptionPricePeriod:
        """Correct one price period under an exact optimistic version."""
        change = SubscriptionPricePeriodUpdate(
            price_period_id=request.price_period_id,
            expected_version=request.expected_version,
            amount_minor=request.amount_minor,
            effective_from=request.effective_from,
            effective_to=request.effective_to,
        )
        with self._unit_of_work_factory() as unit:
            current = unit.subscription_prices.get(change.price_period_id)
            if current is None:
                raise SubscriptionError("subscription price period was not found")
            self._require_no_overlap(
                unit,
                subscription_id=current.subscription_id,
                effective_from=change.effective_from,
                effective_to=change.effective_to,
                exclude_id=current.id,
            )
            price = unit.subscription_prices.save(change)
            unit.commit()
        return price

    def set_default_funding_wallet(self, wallet_id: int) -> DefaultFundingWalletView:
        """Name the wallet new subscriptions are prefilled from.

        Choosing a default moves no money and creates no commitment; it only
        records which envelope the register should offer first.
        """
        with self._unit_of_work_factory() as unit:
            view = self._describe_wallet(unit, wallet_id)
            if view.problem is not None:
                raise LedgerConfigurationError(
                    "the default subscription wallet must be an active wallet "
                    "in an active asset account"
                )
            unit.app_settings.set(DEFAULT_FUNDING_WALLET_SETTING, wallet_id)
            unit.commit()
        return view

    def default_funding_wallet(self) -> DefaultFundingWalletView:
        """Return the configured default wallet and whether it is still usable."""
        with self._unit_of_work_factory() as unit:
            setting = unit.app_settings.get(DEFAULT_FUNDING_WALLET_SETTING)
            if setting is None or not isinstance(setting.value, int):
                return DefaultFundingWalletView(
                    wallet_id=None,
                    wallet_name=None,
                    financial_account_name=None,
                    currency=None,
                )
            return self._describe_wallet(unit, setting.value)

    @staticmethod
    def _describe_wallet(unit: UnitOfWork, wallet_id: int) -> DefaultFundingWalletView:
        """Describe one wallet and name the first reason it is unusable."""
        wallet = unit.wallets.get(wallet_id)
        if wallet is None:
            return DefaultFundingWalletView(
                wallet_id=wallet_id,
                wallet_name=None,
                financial_account_name=None,
                currency=None,
                problem=WALLET_MISSING,
            )
        account = unit.financial_accounts.get(wallet.financial_account_id)
        if account is None:
            return DefaultFundingWalletView(
                wallet_id=wallet_id,
                wallet_name=wallet.name,
                financial_account_name=None,
                currency=None,
                problem=WALLET_ACCOUNT_MISSING,
            )
        problem = None
        if not wallet.is_active:
            problem = WALLET_INACTIVE
        elif not account.is_active:
            problem = WALLET_ACCOUNT_INACTIVE
        elif not holds_wallets(account.account_type):
            problem = WALLET_NOT_ASSET
        return DefaultFundingWalletView(
            wallet_id=wallet_id,
            wallet_name=wallet.name,
            financial_account_name=account.display_name,
            currency=account.currency,
            problem=problem,
        )

    @staticmethod
    def _require_configuration_unchanged(
        unit: UnitOfWork, current: Subscription, change: SubscriptionUpdate
    ) -> None:
        """Freeze the configuration a stored price is already read through."""
        if not unit.subscription_prices.list_for_subscription(current.id):
            return
        changed = frozen_configuration_changes(
            current,
            currency=change.currency,
            billing_cadence=change.billing_cadence,
            cadence_months=change.cadence_months,
            billing_month=change.billing_month,
            budget_category_id=change.budget_category_id,
            funding_wallet_id=change.funding_wallet_id,
            active_from=change.active_from,
        )
        if changed:
            raise SubscriptionError(
                "a subscription that already has a price cannot change "
                + ", ".join(changed)
                + "; stop this subscription from a month and add a new one "
                "instead, so past months keep the amounts they were given"
            )

    @staticmethod
    def _require_no_running_namesake(
        unit: UnitOfWork, *, name: str, exclude_id: int | None = None
    ) -> None:
        """One service has at most one running interval at a time.

        This is the second half of the register's identity rule and the one
        the database holds as a unique index. It is checked only for a row
        that is itself running: a stopped interval may keep the name of the
        interval that succeeded it, which is what makes history editable.
        """
        for existing in unit.subscriptions.list_by_name(name):
            if existing.id == exclude_id or not existing.is_active:
                continue
            raise SubscriptionError(
                "an active subscription already uses this name; stop it from "
                "a month first, then start the next interval"
            )

    @staticmethod
    def _require_no_interval_overlap(
        unit: UnitOfWork,
        *,
        name: str,
        active_from: date | None,
        active_to: date | None,
        exclude_id: int | None = None,
    ) -> None:
        """No two intervals of one service may claim the same month.

        The name identifies the service, so every interval it has ever held is
        compared, not only the active ones and not only the interval a resume
        happens to start from. Two overlapping intervals would each emit their
        own requirement item for the shared months, and the month's total
        would count one service twice.
        """
        for existing in unit.subscriptions.list_by_name(name):
            if existing.id == exclude_id:
                continue
            if activity_ranges_overlap(
                first_from=existing.active_from,
                first_to=existing.active_to,
                second_from=active_from,
                second_to=active_to,
            ):
                raise SubscriptionOverlapError(
                    "subscription intervals must not overlap; "
                    f"this service already covers {_range_label(existing)}"
                )

    @staticmethod
    def _require_category(
        unit: UnitOfWork, category_id: int, *, must_be_active: bool
    ) -> None:
        category = unit.budget_categories.get(category_id)
        if category is None:
            raise LedgerConfigurationError("budget category was not found")
        if must_be_active and not category.is_active:
            raise LedgerConfigurationError(
                "an inactive budget category cannot receive a subscription"
            )

    @staticmethod
    def _require_funding_wallet(
        unit: UnitOfWork, wallet_id: int, *, currency: str
    ) -> None:
        """A subscription is funded from a wallet holding its own currency."""
        wallet = unit.wallets.get(wallet_id)
        if wallet is None:
            raise LedgerConfigurationError("funding wallet was not found")
        if not wallet.is_active:
            raise LedgerConfigurationError("funding wallet is not active")
        account = unit.financial_accounts.get(wallet.financial_account_id)
        if account is None:
            raise LedgerConfigurationError("funding account was not found")
        if not account.is_active or not holds_wallets(account.account_type):
            raise LedgerConfigurationError(
                "a subscription is funded from an active asset account"
            )
        if account.currency != currency:
            raise LedgerConfigurationError(
                "a subscription must be funded from a wallet in its own currency"
            )

    @staticmethod
    def _require_no_overlap(
        unit: UnitOfWork,
        *,
        subscription_id: int,
        effective_from: date,
        effective_to: date | None,
        exclude_id: int | None = None,
    ) -> None:
        """Two prices of one subscription may not claim the same month."""
        clashes = unit.subscription_prices.list_overlapping(
            subscription_id=subscription_id,
            effective_from=effective_from,
            effective_to=effective_to,
            exclude_id=exclude_id,
        )
        if clashes:
            raise SubscriptionOverlapError(
                "subscription price periods must not overlap"
            )


def _resolve_contribution(
    entry: SubscriptionRequirementInput,
    *,
    year: int,
    month: int,
    selected: date,
) -> tuple[SubscriptionRequirementReason, int | None, int]:
    """Decide one month's reason code, cycle position, and contribution.

    Every path returns a reason, including the zero-contribution ones, so a
    subscription never appears without an explanation for what it did.
    """
    subscription = entry.subscription
    outside = month_activity_reason(
        active_from=subscription.active_from,
        active_to=subscription.active_to,
        selected_month=selected,
    )
    if outside is not None:
        return outside, None, 0
    price = entry.price
    if price is None:
        return SubscriptionRequirementReason.NO_PRICE_FOR_MONTH, None, 0
    position = cycle_position(
        year=year,
        month=month,
        cadence=subscription.billing_cadence,
        cadence_months=subscription.cadence_months,
        billing_month=subscription.billing_month,
        active_from=subscription.active_from,
    )
    contribution = cycle_share_minor(
        amount_minor=price.amount_minor,
        cadence_months=subscription.cadence_months,
        position=position,
    )
    if price.amount_minor == 0:
        return SubscriptionRequirementReason.ZERO_PRICE, position, contribution
    if subscription.billing_cadence is SubscriptionCadence.MONTHLY:
        return SubscriptionRequirementReason.MONTHLY_PRICE, position, contribution
    return SubscriptionRequirementReason.CYCLE_SHARE, position, contribution


def requirement_item(
    entry: SubscriptionRequirementInput,
    *,
    year: int,
    month: int,
    selected: date,
) -> SubscriptionRequirementItem:
    """Return one subscription's evidence for one month, contributing or not."""
    subscription = entry.subscription
    reason, position, contribution = _resolve_contribution(
        entry, year=year, month=month, selected=selected
    )
    return SubscriptionRequirementItem(
        subscription_id=subscription.id,
        name=subscription.name,
        currency=subscription.currency,
        year=year,
        month=month,
        funding_wallet_id=subscription.funding_wallet_id,
        funding_wallet_name=entry.funding_wallet_name,
        budget_category_id=subscription.budget_category_id,
        budget_category_name=entry.budget_category_name,
        cadence=subscription.billing_cadence,
        cadence_months=subscription.cadence_months,
        cycle_position=position,
        price_period_id=None if entry.price is None else entry.price.id,
        cycle_price_minor=(None if entry.price is None else entry.price.amount_minor),
        contribution_minor=contribution,
        is_active=subscription.is_active,
        active_from=subscription.active_from,
        active_to=subscription.active_to,
        effective_from=None if entry.price is None else entry.price.effective_from,
        effective_to=None if entry.price is None else entry.price.effective_to,
        reason=reason,
    )


def calculate_requirement(
    inputs: tuple[SubscriptionRequirementInput, ...],
    *,
    year: int,
    month: int,
    currency: str,
) -> SubscriptionRequirement:
    """Turn already-read register rows into one month of requirement.

    This is the whole calculation, and it takes its inputs rather than
    fetching them. The read-only preview passes rows from a short session; a
    salary run passes rows read inside the transaction that freezes them, so
    the amount a run stores is the amount the same snapshot produced
    (ADR-B045). Neither path owns a second copy of the cadence or rounding
    rules.
    """
    code = normalize_currency(currency)
    selected = month_start(year, month)
    return SubscriptionRequirement(
        year=year,
        month=month,
        currency=code,
        items=tuple(
            requirement_item(entry, year=year, month=month, selected=selected)
            for entry in inputs
        ),
    )


class CalculateSubscriptionRequirement:
    """Calculate one calendar month of funding requirement, read-only.

    The calculation writes nothing. It reads the price that was effective in
    the selected month, places that month inside its own billing cycle, and
    returns per-item evidence complete enough for a salary run to freeze.
    """

    def __init__(self, read_repository: SubscriptionReadRepository) -> None:
        self._read_repository = read_repository

    def execute(
        self, *, year: int, month: int, currency: str
    ) -> SubscriptionRequirement:
        """Return one month of requirement in exactly one currency."""
        code = normalize_currency(currency)
        return calculate_requirement(
            self._read_repository.requirement_inputs(
                currency=code, selected_month=month_start(year, month)
            ),
            year=year,
            month=month,
            currency=code,
        )

    def currencies(self) -> tuple[str, ...]:
        """Return each currency present, each calculated on its own."""
        return self._read_repository.currencies()

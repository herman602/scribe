"""Preview and atomically apply one normalized, read-only Sheet cutover."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from app.application.services.ledger import JournalService, OpeningPositionRequest
from app.application.services.subscriptions import DEFAULT_FUNDING_WALLET_SETTING
from app.domain.budget_enums import (
    FinancialAccountType,
    SubscriptionCadence,
    WalletPurpose,
)
from app.domain.budget_models import (
    BudgetCategoryDraft,
    BudgetTargetDraft,
    FinancialAccountDraft,
    LedgerAccountDraft,
    OpeningWalletSplit,
    SalaryPlanTemplateDraft,
    SalaryPlanTemplateItemDraft,
    SubscriptionDraft,
    SubscriptionPricePeriodDraft,
    WalletDraft,
)
from app.domain.budget_rules import (
    UNALLOCATED_WALLET_NAME,
    ledger_kind_for_account_type,
    physical_ledger_account_code,
)
from app.domain.cutover import (
    CutoverDocument,
    ImportApplyResult,
    ImportBatchStatus,
    ImportCurrencyTotal,
    ImportPreview,
    ImportRowStatus,
)
from app.domain.errors import CutoverImportError
from app.domain.ports.repositories import UnitOfWork
from app.infrastructure.cutover.local_document import load_cutover_document

type UnitOfWorkFactory = Callable[[], UnitOfWork]
type Clock = Callable[[], datetime]


def _json(payload: object) -> str:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


class BudgetCutoverImport:
    """Translate reviewed configuration into one all-or-nothing local write."""

    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        journal: JournalService,
        *,
        clock: Clock = lambda: datetime.now(UTC),
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._journal = journal
        self._clock = clock

    def preview(self, path: Path) -> ImportPreview:
        """Validate a local file and return exact counts without a DB write."""
        document, content_hash = load_cutover_document(path)
        self._validate_references(document)
        return self._preview(document, content_hash)

    def apply(self, path: Path, *, approved_sha256: str) -> ImportApplyResult:
        """Apply exactly the previewed content in one transaction, or replay it."""
        document, content_hash = load_cutover_document(path)
        if approved_sha256.strip().lower() != content_hash:
            raise CutoverImportError(
                "the file changed after preview; preview it again and approve "
                "its new hash"
            )
        self._validate_references(document)
        preview = self._preview(document, content_hash)
        with self._unit_of_work_factory() as unit:
            existing = unit.import_batches.get_by_hash(content_hash)
            if existing is not None:
                if existing.status is not ImportBatchStatus.APPLIED:
                    raise CutoverImportError(
                        "this import hash exists without a completed apply"
                    )
                return ImportApplyResult(batch=existing, created=False, preview=preview)
            if unit.financial_accounts.list_active():
                raise CutoverImportError(
                    "cutover requires an empty financial-account ledger; "
                    "existing accounts were not changed"
                )
            batch = unit.import_batches.add(
                source_kind=document.source_kind,
                format_version=document.format_version,
                content_sha256=content_hash,
                source_locator_hash=document.source_locator_hash,
                observed_at=document.observed_at,
                cutoff_at=document.cutoff_at,
                row_count=self._row_count(document),
                warning_count=len(document.pending),
                applied_at=self._clock(),
            )
            accounts, wallets = self._accounts(unit, batch.id, document)
            categories = self._categories(unit, batch.id, document)
            self._targets(unit, batch.id, document, wallets, categories)
            self._subscriptions(unit, batch.id, document, wallets, categories)
            self._salary(unit, batch.id, document, accounts, wallets, categories)
            self._legacy(unit, batch.id, document)
            self._pending(unit, batch.id, document)
            unit.commit()
        return ImportApplyResult(batch=batch, created=True, preview=preview)

    @staticmethod
    def _row_count(document: CutoverDocument) -> int:
        salary = (
            0
            if document.salary_template is None
            else 1 + len(document.salary_template.items)
        )
        return (
            len(document.accounts)
            + len(document.accounts)  # one opening-position evidence row each
            + sum(len(account.wallets) for account in document.accounts)
            + len(document.categories)
            + len(document.targets)
            + len(document.subscriptions)
            + salary
            + len(document.legacy_daily)
            + len(document.pending)
        )

    @staticmethod
    def _preview(document: CutoverDocument, content_hash: str) -> ImportPreview:
        opening: dict[str, int] = {}
        legacy: dict[str, int] = {}
        for account in document.accounts:
            opening[account.currency] = (
                opening.get(account.currency, 0) + account.balance_minor
            )
        for row in document.legacy_daily:
            legacy[row.currency] = legacy.get(row.currency, 0) + row.amount_minor
        return ImportPreview(
            content_sha256=content_hash,
            account_count=len(document.accounts),
            wallet_count=sum(len(account.wallets) for account in document.accounts),
            opening_totals=tuple(
                ImportCurrencyTotal(currency=currency, amount_minor=amount)
                for currency, amount in sorted(opening.items())
            ),
            category_count=len(document.categories),
            subscription_count=len(document.subscriptions),
            salary_item_count=(
                0
                if document.salary_template is None
                else len(document.salary_template.items)
            ),
            legacy_daily_count=len(document.legacy_daily),
            legacy_daily_totals=tuple(
                ImportCurrencyTotal(currency=currency, amount_minor=amount)
                for currency, amount in sorted(legacy.items())
            ),
            pending_count=len(document.pending),
        )

    @staticmethod
    def _validate_references(document: CutoverDocument) -> None:
        accounts = {account.source_key: account for account in document.accounts}
        categories = {category.source_key for category in document.categories}
        wallets = {
            (account.source_key, wallet.name)
            for account in document.accounts
            for wallet in account.wallets
        }
        wallets.update(
            (account.source_key, UNALLOCATED_WALLET_NAME)
            for account in document.accounts
        )
        keys: list[str] = []
        keys.extend(account.source_key for account in document.accounts)
        keys.extend(category.source_key for category in document.categories)
        keys.extend(target.source_key for target in document.targets)
        keys.extend(item.source_key for item in document.subscriptions)
        keys.extend(item.source_key for item in document.legacy_daily)
        keys.extend(item.source_key for item in document.pending)
        if document.salary_template is not None:
            keys.append(document.salary_template.source_key)
            keys.extend(item.source_key for item in document.salary_template.items)
        if len(keys) != len(set(keys)):
            raise CutoverImportError("every normalized source row needs a unique key")
        for target in document.targets:
            if target.category_key not in categories:
                raise CutoverImportError("a target references an unknown category")
            if (
                target.funding_account_key,
                target.funding_wallet_name,
            ) not in wallets:
                raise CutoverImportError(
                    "a target references an unknown funding wallet"
                )
        for subscription in document.subscriptions:
            if subscription.category_key not in categories:
                raise CutoverImportError(
                    "a subscription references an unknown category"
                )
            if (
                subscription.funding_account_key,
                subscription.funding_wallet_name,
            ) not in wallets:
                raise CutoverImportError(
                    "a subscription references an unknown funding wallet"
                )
        if document.salary_template is not None:
            if document.salary_template.source_account_key not in accounts:
                raise CutoverImportError(
                    "the salary template references an unknown source account"
                )
            for item in document.salary_template.items:
                if (
                    item.destination_account_key,
                    item.destination_wallet_name,
                ) not in wallets:
                    raise CutoverImportError(
                        "a salary item references an unknown wallet"
                    )
                if (
                    item.category_key is not None
                    and item.category_key not in categories
                ):
                    raise CutoverImportError(
                        "a salary item references an unknown category"
                    )

    def _accounts(
        self, unit: UnitOfWork, batch_id: int, document: CutoverDocument
    ) -> tuple[dict[str, int], dict[tuple[str, str], int]]:
        account_ids: dict[str, int] = {}
        wallet_ids: dict[tuple[str, str], int] = {}
        for source in document.accounts:
            account = unit.financial_accounts.add(
                FinancialAccountDraft(
                    institution_code=source.institution_code,
                    display_name=source.display_name,
                    account_type=FinancialAccountType.SAVINGS,
                    currency=source.currency,
                    masked_reference=source.masked_reference,
                )
            )
            account_ids[source.source_key] = account.id
            kind = ledger_kind_for_account_type(account.account_type)
            unit.ledger_accounts.add(
                LedgerAccountDraft(
                    code=physical_ledger_account_code(
                        kind, account.institution_code, account.id
                    ),
                    name=account.display_name,
                    kind=kind,
                    currency=account.currency,
                    financial_account_id=account.id,
                )
            )
            unallocated = unit.wallets.add(
                WalletDraft(
                    financial_account_id=account.id,
                    name=UNALLOCATED_WALLET_NAME,
                    purpose=WalletPurpose.UNALLOCATED,
                )
            )
            wallet_ids[(source.source_key, UNALLOCATED_WALLET_NAME)] = unallocated.id
            split_names = {wallet.name for wallet in source.wallets}
            for wallet in source.wallets:
                if wallet.purpose is WalletPurpose.UNALLOCATED:
                    continue
                created = unit.wallets.add(
                    WalletDraft(
                        financial_account_id=account.id,
                        name=wallet.name,
                        purpose=wallet.purpose,
                    )
                )
                wallet_ids[(source.source_key, wallet.name)] = created.id
                self._row(
                    unit,
                    batch_id=batch_id,
                    key=f"{source.source_key}:wallet:{wallet.name}",
                    kind="wallet",
                    payload={
                        "account": source.source_key,
                        "name": wallet.name,
                        "balance_minor": wallet.balance_minor,
                        "purpose": wallet.purpose.value,
                    },
                    entity_type="wallet",
                    entity_id=created.id,
                )
            splits = tuple(
                OpeningWalletSplit(
                    wallet_name=wallet.name, amount_minor=wallet.balance_minor
                )
                for wallet in source.wallets
            )
            if UNALLOCATED_WALLET_NAME not in split_names:
                splits = (*splits, OpeningWalletSplit(UNALLOCATED_WALLET_NAME, 0))
            entry = self._journal.stage_opening_position(
                unit,
                OpeningPositionRequest(
                    financial_account_id=account.id,
                    balance_minor=source.balance_minor,
                    observed_at=document.observed_at,
                    splits=splits,
                    description="Sheet cutover opening position",
                ),
            )
            self._row(
                unit,
                batch_id=batch_id,
                key=source.source_key,
                kind="financial_account",
                payload={
                    "display_name": source.display_name,
                    "currency": source.currency,
                    "balance_minor": source.balance_minor,
                    "masked_reference": source.masked_reference,
                },
                entity_type="financial_account",
                entity_id=account.id,
            )
            self._row(
                unit,
                batch_id=batch_id,
                key=f"{source.source_key}:opening",
                kind="opening_position",
                payload={
                    "observed_at": document.observed_at.isoformat(),
                    "balance_minor": source.balance_minor,
                },
                entity_type="journal_entry",
                entity_id=entry.id,
            )
        return account_ids, wallet_ids

    def _categories(
        self, unit: UnitOfWork, batch_id: int, document: CutoverDocument
    ) -> dict[str, int]:
        result: dict[str, int] = {}
        for source in document.categories:
            category = unit.budget_categories.add(BudgetCategoryDraft(name=source.name))
            result[source.source_key] = category.id
            self._row(
                unit,
                batch_id=batch_id,
                key=source.source_key,
                kind="budget_category",
                payload={"name": source.name},
                entity_type="budget_category",
                entity_id=category.id,
            )
        return result

    def _targets(
        self,
        unit: UnitOfWork,
        batch_id: int,
        document: CutoverDocument,
        wallets: dict[tuple[str, str], int],
        categories: dict[str, int],
    ) -> None:
        for source in document.targets:
            period = unit.budget_periods.get_or_create(
                year=source.year, month=source.month, currency=source.currency
            )
            target = unit.budget_targets.upsert(
                BudgetTargetDraft(
                    budget_period_id=period.id,
                    budget_category_id=categories[source.category_key],
                    target_minor=source.target_minor,
                    buffer_minor=source.buffer_minor,
                    funding_wallet_id=wallets[
                        (source.funding_account_key, source.funding_wallet_name)
                    ],
                )
            )
            self._row(
                unit,
                batch_id=batch_id,
                key=source.source_key,
                kind="budget_target",
                payload=asdict(source),
                entity_type="budget_target",
                entity_id=target.id,
            )

    def _subscriptions(
        self,
        unit: UnitOfWork,
        batch_id: int,
        document: CutoverDocument,
        wallets: dict[tuple[str, str], int],
        categories: dict[str, int],
    ) -> None:
        default_wallet_id: int | None = None
        for source in document.subscriptions:
            wallet_id = wallets[
                (source.funding_account_key, source.funding_wallet_name)
            ]
            default_wallet_id = wallet_id
            wallet = unit.wallets.get(wallet_id)
            if wallet is None:  # pragma: no cover - reference validation resolved it
                raise CutoverImportError("a subscription funding wallet disappeared")
            account = unit.financial_accounts.get(wallet.financial_account_id)
            if account is None:  # pragma: no cover - foreign key invariant
                raise CutoverImportError("a subscription funding account disappeared")
            subscription = unit.subscriptions.add(
                SubscriptionDraft(
                    name=source.name,
                    budget_category_id=categories[source.category_key],
                    funding_wallet_id=wallet_id,
                    currency=account.currency,
                    billing_cadence=SubscriptionCadence.MONTHLY,
                    cadence_months=1,
                    active_from=source.effective_from,
                )
            )
            unit.subscription_prices.add(
                SubscriptionPricePeriodDraft(
                    subscription_id=subscription.id,
                    amount_minor=source.amount_minor,
                    effective_from=source.effective_from,
                )
            )
            self._row(
                unit,
                batch_id=batch_id,
                key=source.source_key,
                kind="subscription",
                payload=asdict(source),
                entity_type="subscription",
                entity_id=subscription.id,
            )
        if default_wallet_id is not None:
            unit.app_settings.set(DEFAULT_FUNDING_WALLET_SETTING, default_wallet_id)

    def _salary(
        self,
        unit: UnitOfWork,
        batch_id: int,
        document: CutoverDocument,
        accounts: dict[str, int],
        wallets: dict[tuple[str, str], int],
        categories: dict[str, int],
    ) -> None:
        source = document.salary_template
        if source is None:
            return
        template = unit.salary_templates.add(
            SalaryPlanTemplateDraft(
                name=source.name,
                currency=source.currency,
                source_account_id=accounts[source.source_account_key],
            )
        )
        self._row(
            unit,
            batch_id=batch_id,
            key=source.source_key,
            kind="salary_template",
            payload={"name": source.name, "currency": source.currency},
            entity_type="salary_template",
            entity_id=template.id,
        )
        for order, item in enumerate(source.items):
            created = unit.salary_template_items.add(
                SalaryPlanTemplateItemDraft(
                    template_id=template.id,
                    name=item.name,
                    destination_wallet_id=wallets[
                        (item.destination_account_key, item.destination_wallet_name)
                    ],
                    calculation_mode=item.calculation_mode,
                    budget_category_id=(
                        None
                        if item.category_key is None
                        else categories[item.category_key]
                    ),
                    fixed_amount_minor=item.fixed_amount_minor,
                    display_order=order,
                )
            )
            self._row(
                unit,
                batch_id=batch_id,
                key=item.source_key,
                kind="salary_template_item",
                payload=asdict(item),
                entity_type="salary_template_item",
                entity_id=created.id,
            )

    def _legacy(
        self, unit: UnitOfWork, batch_id: int, document: CutoverDocument
    ) -> None:
        for source in document.legacy_daily:
            row_id = self._row(
                unit,
                batch_id=batch_id,
                key=source.source_key,
                kind="legacy_daily_activity",
                payload={
                    "activity_date": source.activity_date.isoformat(),
                    "sequence": source.sequence,
                    "amount_minor": source.amount_minor,
                    "currency": source.currency,
                    "payment_method": source.payment_method.value,
                    "note": source.note,
                },
                status=ImportRowStatus.HISTORICAL,
            )
            unit.legacy_daily_activities.add(
                import_row_id=row_id,
                activity_date=source.activity_date,
                sequence=source.sequence,
                amount_minor=source.amount_minor,
                currency=source.currency,
                payment_method=source.payment_method,
                note=source.note,
            )

    def _pending(
        self, unit: UnitOfWork, batch_id: int, document: CutoverDocument
    ) -> None:
        for source in document.pending:
            self._row(
                unit,
                batch_id=batch_id,
                key=source.source_key,
                kind=source.entity_kind,
                payload=source.payload,
                status=ImportRowStatus.PENDING,
                reason_code=source.reason_code,
            )

    @staticmethod
    def _row(
        unit: UnitOfWork,
        *,
        batch_id: int,
        key: str,
        kind: str,
        payload: object,
        status: ImportRowStatus = ImportRowStatus.APPLIED,
        reason_code: str | None = None,
        entity_type: str | None = None,
        entity_id: int | None = None,
    ) -> int:
        return unit.import_rows.add(
            batch_id=batch_id,
            entity_kind=kind,
            source_row_key=key,
            normalized_payload_json=_json(payload),
            status=status.value,
            reason_code=reason_code,
            created_entity_type=entity_type,
            created_entity_id=entity_id,
        )

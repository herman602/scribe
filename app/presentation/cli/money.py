"""Diagnostic and setup commands for the local accounting ledger."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

import typer
from pydantic import ValidationError

from app.application.services.bank_movements import (
    AccountTransferRequest,
    BankMovementRequest,
    ManualRefundRequest,
    ReallocateWalletRequest,
)
from app.application.services.budget_planning import (
    AddCategoryRequest,
    SetTargetRequest,
    UpdateCategoryRequest,
)
from app.application.services.cards import (
    ConfigureCardRequest,
    RegisterCardRequest,
)
from app.application.services.event_allocation import (
    AllocateFinancialEventRequest,
    AllocationLine,
    RedoAllocationRequest,
)
from app.application.services.ledger import (
    AddWalletRequest,
    ArrangeAccountsRequest,
    ArrangeWalletsRequest,
    OpenFinancialAccountRequest,
    OpeningPositionRequest,
    RenameWalletRequest,
)
from app.application.services.manual_money import (
    ManualExpenseRequest,
    ReverseEventRequest,
    UpdateFinancialEventNoteRequest,
)
from app.application.services.reconciliation import (
    BalanceSnapshotRequest,
    ReconciliationAdjustmentRequest,
)
from app.application.services.salary_allocation import (
    AddSalaryTemplateItemRequest,
    AddSalaryTemplateRequest,
    ApproveSalaryRunRequest,
    CancelSalaryDraftRequest,
    DeactivateSalaryTemplateItemRequest,
    GenerateSalaryRunRequest,
    MarkSalaryTransferredRequest,
    RegenerateSalaryRunRequest,
    SalaryReceiptRequest,
    UpdateSalaryTemplateItemRequest,
    UpdateSalaryTemplateRequest,
)
from app.application.services.scheduled_transfers import (
    ScheduleCardAutopayRequest,
    ScheduleClearingTransferRequest,
    ScheduleCommitmentLine,
    ScheduledTransferAction,
)
from app.application.services.subscriptions import (
    AddPricePeriodRequest,
    AddSubscriptionRequest,
    DeactivateSubscriptionRequest,
    ResumeSubscriptionRequest,
    UpdatePricePeriodRequest,
    UpdateSubscriptionRequest,
)
from app.application.services.supplementary_card_settings import (
    SetSupplementaryCardRequest,
)
from app.application.services.transaction_review import ReviewTransactionRequest
from app.bootstrap import MoneyRuntime, build_money_runtime
from app.domain.budget_enums import (
    BankMovementKind,
    CardHolderRole,
    FinancialAccountType,
    PaymentMethod,
    SalaryCalculationMode,
    SubscriptionCadence,
    WalletPurpose,
)
from app.domain.budget_models import (
    ManualSalaryAmount,
    OpeningWalletSplit,
    SalaryAllocationRun,
    SalaryPlanTemplateItem,
    ScheduledTransfer,
)
from app.domain.budget_rules import parse_period_label
from app.domain.enums import TransactionType
from app.domain.errors import (
    ConcurrentUpdateError,
    MigrationError,
    ScheduledTransferOverdueError,
    ScribeError,
)
from app.domain.money import currency_precision, parse_display_amount
from app.infrastructure.config.settings import AppSettings
from app.infrastructure.database.migrations import inspect_migration_status
from app.presentation.cli.render import emit_json

money_cli = typer.Typer(
    help="Inspect and set up the local accounts, wallets, and ledger.",
    no_args_is_help=True,
)


@contextmanager
def _money_runtime(settings: AppSettings) -> Iterator[MoneyRuntime]:
    """Own one ledger runtime and always release its SQLite engine."""
    runtime = build_money_runtime(settings)
    try:
        yield runtime
    finally:
        runtime.close()


def _load_settings() -> AppSettings:
    try:
        settings = AppSettings()
    except ValidationError as error:
        typer.echo(f"Configuration error:\n{error}", err=True)
        raise typer.Exit(code=2) from error
    try:
        migration = inspect_migration_status(settings)
    except MigrationError as error:
        typer.echo(f"Database is unavailable: {error}", err=True)
        raise typer.Exit(code=1) from error
    if not migration.is_current:
        typer.echo("Database is not at the current migration.", err=True)
        raise typer.Exit(code=1)
    return settings


def _format_money(amount_minor: int, currency: str) -> str:
    precision = currency_precision(currency)
    major = Decimal(amount_minor) / (Decimal(10) ** precision.minor_unit_exponent)
    return f"{currency} {major:,.{precision.display_fraction_digits}f}"


def _echo_cutover_preview(preview: object) -> None:
    """Render the stable fields shared by preview and apply."""
    from app.domain.cutover import ImportPreview

    assert isinstance(preview, ImportPreview)
    typer.echo(f"sha256={preview.content_sha256}")
    typer.echo(
        f"accounts={preview.account_count} wallets={preview.wallet_count} "
        "opening="
        + ", ".join(
            _format_money(total.amount_minor, total.currency)
            for total in preview.opening_totals
        )
    )
    typer.echo(
        f"categories={preview.category_count} "
        f"subscriptions={preview.subscription_count} "
        f"salary_items={preview.salary_item_count}"
    )
    typer.echo(
        f"legacy_daily={preview.legacy_daily_count} "
        "legacy_total="
        + ", ".join(
            _format_money(total.amount_minor, total.currency)
            for total in preview.legacy_daily_totals
        )
        + " "
        f"pending={preview.pending_count}"
    )


@money_cli.command("import-preview")
def import_preview(
    file: Annotated[Path, typer.Option(exists=True, dir_okay=False, readable=True)],
) -> None:
    """Validate a private normalized Sheet snapshot without writing SQLite."""
    with _money_runtime(_load_settings()) as runtime:
        try:
            preview = runtime.cutover.preview(file)
        except (ScribeError, ValueError, OSError) as error:
            typer.echo(f"Could not preview the cutover: {error}", err=True)
            raise typer.Exit(code=1) from error
    _echo_cutover_preview(preview)
    typer.echo("No database rows were written. Use this exact hash with import-apply.")


@money_cli.command("import-apply")
def import_apply(
    file: Annotated[Path, typer.Option(exists=True, dir_okay=False, readable=True)],
    approve_sha256: Annotated[
        str,
        typer.Option(
            "--approve-sha256",
            help="Exact hash printed by import-preview; refuses changed content.",
        ),
    ],
) -> None:
    """Atomically apply the exact locally previewed cutover document."""
    with _money_runtime(_load_settings()) as runtime:
        try:
            result = runtime.cutover.apply(file, approved_sha256=approve_sha256)
        except (ScribeError, ValueError, OSError) as error:
            typer.echo(f"Could not apply the cutover: {error}", err=True)
            raise typer.Exit(code=1) from error
    _echo_cutover_preview(result.preview)
    typer.echo(
        f"batch_id={result.batch.id} status={result.batch.status.value} "
        f"created={str(result.created).lower()}"
    )


def _parse_local_instant(value: str, timezone: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("observed time must be an ISO 8601 date-time") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone))
    return parsed.astimezone(UTC)


def _parse_month(value: str | None) -> date | None:
    """Return the first day of one ``yyyy-mm`` month, or None when omitted."""
    if value is None or not value.strip():
        return None
    year, month = parse_period_label(value.strip())
    return date(year, month, 1)


def _require_month(value: str, *, label: str) -> date:
    """Return one mandatory ``yyyy-mm`` month named on the command line."""
    parsed = _parse_month(value)
    if parsed is None:
        raise ValueError(f"{label} must be written as yyyy-mm")
    return parsed


def _format_month(value: date | None) -> str:
    """Render a boundary as the month it names, never as a day."""
    return "-" if value is None else f"{value.year:04d}-{value.month:02d}"


def _parse_manual_amounts(values: list[str]) -> tuple[ManualSalaryAmount, ...]:
    """Read repeatable ``--manual ITEM_ID=AMOUNT_MINOR`` pairs.

    Amounts are always integer minor units on the command line, so a decimal
    point is a mistake rather than a convenience and is refused.
    """
    amounts: list[ManualSalaryAmount] = []
    for item in values:
        identifier, separator, amount = item.partition("=")
        if not separator or not identifier.strip():
            raise ValueError("each --manual must use the form 'ITEM_ID=123456'")
        try:
            amounts.append(
                ManualSalaryAmount(
                    template_item_id=int(identifier.strip()),
                    amount_minor=int(amount.strip()),
                )
            )
        except ValueError as error:
            raise ValueError(
                "each --manual value must be an integer minor-unit amount"
            ) from error
    return tuple(amounts)


def _require_template_item(
    runtime: MoneyRuntime, template_item_id: int
) -> SalaryPlanTemplateItem:
    """Return one template item so an update can keep its untouched values."""
    for row in runtime.salary.templates():
        for entry in row.items:
            if entry.item.id == template_item_id:
                return entry.item
    typer.echo("Salary template item was not found.", err=True)
    raise typer.Exit(code=1)


def _echo_run(run: SalaryAllocationRun, *, created: bool) -> None:
    """Print one run's identity, money, and status, in minor units too."""
    typer.echo(
        f"run_id={run.id} generation={run.generation} status={run.status.value} "
        f"template_version={run.template_version} version={run.version} "
        f"created={created}"
    )
    typer.echo(
        f"salary={_format_money(run.salary_amount_minor, run.currency)} "
        f"salary_minor={run.salary_amount_minor} "
        f"planned={_format_money(run.planned_amount_minor, run.currency)} "
        f"planned_minor={run.planned_amount_minor} "
        f"remainder={_format_money(run.remainder_minor, run.currency)} "
        f"remainder_minor={run.remainder_minor}"
    )


def _parse_splits(values: list[str]) -> tuple[OpeningWalletSplit, ...]:
    splits: list[OpeningWalletSplit] = []
    for item in values:
        name, separator, amount = item.rpartition("=")
        if not separator or not name.strip():
            raise ValueError("each --split must use the form 'Wallet name=123456'")
        try:
            minor = int(amount.strip())
        except ValueError as error:
            raise ValueError(
                "each --split amount must be an integer minor-unit value"
            ) from error
        splits.append(OpeningWalletSplit(wallet_name=name.strip(), amount_minor=minor))
    return tuple(splits)


@money_cli.command("account-add")
def account_add(
    institution: Annotated[
        str, typer.Option(help="Stable bank key such as first_bank or cathay.")
    ],
    name: Annotated[str, typer.Option(help="Local display name for this account.")],
    account_type: Annotated[
        FinancialAccountType,
        typer.Option("--type", help="checking, savings, cash, card_liability, other."),
    ],
    currency: Annotated[str, typer.Option(help="ISO 4217 code such as TWD.")],
    masked_reference: Annotated[
        str | None,
        typer.Option(
            help="Optional masked hint such as ****1234. Never a full number.",
        ),
    ] = None,
) -> None:
    """Register one physical account and its ledger identity."""
    with _money_runtime(_load_settings()) as runtime:
        try:
            result = runtime.setup.open_account(
                OpenFinancialAccountRequest(
                    institution_code=institution,
                    display_name=name,
                    account_type=account_type,
                    currency=currency,
                    masked_reference=masked_reference,
                )
            )
        except (ScribeError, ValueError) as error:
            typer.echo(f"Could not create the account: {error}", err=True)
            raise typer.Exit(code=1) from error
    typer.echo(
        f"account_id={result.account.id} {result.account.institution_code} "
        f"{result.account.display_name} {result.account.currency} "
        f"ledger={result.ledger_account.code}"
    )
    if result.unallocated_wallet is not None:
        typer.echo(
            f"wallet_id={result.unallocated_wallet.id} "
            f"{result.unallocated_wallet.name} (unallocated)"
        )


@money_cli.command("wallet-add")
def wallet_add(
    account_id: Annotated[int, typer.Option(min=1, help="Financial account ID.")],
    name: Annotated[str, typer.Option(help="Wallet name unique within the account.")],
    purpose: Annotated[
        WalletPurpose,
        typer.Option(help="general, budget, or clearing_hub."),
    ] = WalletPurpose.GENERAL,
) -> None:
    """Add one envelope to an existing asset account."""
    with _money_runtime(_load_settings()) as runtime:
        try:
            wallet = runtime.setup.add_wallet(
                AddWalletRequest(
                    financial_account_id=account_id,
                    name=name,
                    purpose=purpose,
                )
            )
        except (ScribeError, ValueError) as error:
            typer.echo(f"Could not create the wallet: {error}", err=True)
            raise typer.Exit(code=1) from error
    typer.echo(
        f"wallet_id={wallet.id} account_id={wallet.financial_account_id} "
        f"{wallet.name} ({wallet.purpose.value})"
    )


@money_cli.command("wallet-rename")
def wallet_rename(
    wallet_id: Annotated[int, typer.Argument(min=1)],
    name: Annotated[str, typer.Option(help="New wallet name.")],
    expected_version: Annotated[int, typer.Option("--expected-version")],
) -> None:
    """Rename one wallet; balance, purpose, and order are untouched."""
    with _money_runtime(_load_settings()) as runtime:
        try:
            wallet = runtime.setup.rename_wallet(
                RenameWalletRequest(
                    wallet_id=wallet_id,
                    name=name,
                    expected_version=expected_version,
                )
            )
        except ConcurrentUpdateError as error:
            typer.echo(f"Wallet changed since it was read: {error}", err=True)
            raise typer.Exit(code=1) from error
        except (ScribeError, ValueError) as error:
            typer.echo(f"Could not rename the wallet: {error}", err=True)
            raise typer.Exit(code=1) from error
    typer.echo(f"wallet_id={wallet.id} name={wallet.name} version={wallet.version}")


@money_cli.command("opening-balance")
def opening_balance(
    account_id: Annotated[int, typer.Option(min=1, help="Financial account ID.")],
    amount_minor: Annotated[
        int,
        typer.Option(help="Reconciled balance in integer minor units."),
    ],
    observed_at: Annotated[
        str,
        typer.Option(
            help="Exact observation time, for example 2026-08-07T21:30.",
        ),
    ],
    split: Annotated[
        list[str] | None,
        typer.Option(
            help=(
                "Repeatable signed wallet share, for example "
                "--split 'Unallocated=700000' --split '生活費=-200000'."
            ),
        ),
    ] = None,
) -> None:
    """Post one reconciled opening position against opening equity."""
    settings = _load_settings()
    with _money_runtime(settings) as runtime:
        try:
            entry = runtime.journal.record_opening_position(
                OpeningPositionRequest(
                    financial_account_id=account_id,
                    balance_minor=amount_minor,
                    observed_at=_parse_local_instant(
                        observed_at, settings.local_timezone
                    ),
                    splits=_parse_splits(split or []),
                )
            )
        except (ScribeError, ValueError) as error:
            typer.echo(f"Could not record the opening position: {error}", err=True)
            raise typer.Exit(code=1) from error
    typer.echo(
        f"entry_id={entry.id} type={entry.entry_type.value} "
        f"postings={len(entry.postings)} key={entry.idempotency_key}"
    )


@money_cli.command("intake-confirmed")
def intake_confirmed(
    apply: Annotated[
        bool,
        typer.Option(
            "--apply/--dry-run",
            help="Preview by default; --apply stages the financial events.",
        ),
    ] = False,
) -> None:
    """Stage confirmed Gmail transactions as canonical financial events."""
    with _money_runtime(_load_settings()) as runtime:
        try:
            result = runtime.intake.backfill(dry_run=not apply)
        except (ScribeError, ValueError) as error:
            typer.echo(f"Could not stage financial events: {error}", err=True)
            raise typer.Exit(code=1) from error
    mode = "apply" if apply else "dry-run"
    typer.echo(
        f"mode={mode} staged={result.staged_count} "
        f"reconciled={result.reconciled_count} skipped={result.skipped_count}"
    )
    for staged in result.staged:
        reasons = ",".join(reason.value for reason in staged.reasons) or "-"
        typer.echo(
            f"  stage transaction_id={staged.transaction_id} "
            f"card_id={staged.card_id if staged.card_id is not None else '-'} "
            f"reasons={reasons}"
        )
    for skipped in result.skipped:
        typer.echo(
            f"  skip  transaction_id={skipped.transaction_id} "
            f"reason={skipped.reason.value}"
        )
    if not apply:
        typer.echo("No financial event was created. Rerun with --apply to stage.")


@money_cli.command("cards-backfill")
def cards_backfill(
    apply: Annotated[
        bool,
        typer.Option(
            "--apply/--dry-run",
            help="Preview by default; --apply creates the card identities.",
        ),
    ] = False,
) -> None:
    """Create card identities from explicit supplementary-card decisions."""
    with _money_runtime(_load_settings()) as runtime:
        try:
            result = runtime.cards.backfill_from_supplementary_settings(
                dry_run=not apply
            )
        except (ScribeError, ValueError) as error:
            typer.echo(f"Could not backfill cards: {error}", err=True)
            raise typer.Exit(code=1) from error
    mode = "apply" if apply else "dry-run"
    typer.echo(
        f"mode={mode} created={result.created_count} "
        f"already_present={result.already_present_count}"
    )
    typer.echo(
        "Only issuer, last four, and the supplementary role are migrated; "
        "settlement and funding accounts are never guessed."
    )


@money_cli.command("transaction-confirm")
def transaction_confirm(
    transaction_ids: Annotated[
        list[int],
        typer.Argument(help="Parsed transaction IDs to confirm."),
    ],
    apply: Annotated[
        bool,
        typer.Option(
            "--apply/--dry-run",
            help="Preview by default; --apply confirms and stages the events.",
        ),
    ] = False,
) -> None:
    """Confirm parsed transactions, the first step of the daily loop.

    Confirmation and financial-event intake share one transaction, so either
    both commit or neither does. Nothing is posted here: a staged event still
    waits for an allocation (ADR-B096).
    """
    with _money_runtime(_load_settings()) as runtime:
        try:
            if not apply:
                # Echo the interpretation back before anything is written: the
                # dry-run's job here is to show which rows the caller actually
                # named, not to predict what confirmation will find.
                requested = sorted(set(transaction_ids))
                typer.echo(f"mode=dry-run requested={len(requested)}")
                for item in requested:
                    typer.echo(f"  transaction_id={item}")
                typer.echo("Nothing changed. Rerun with --apply to confirm.")
                return
            result = runtime.confirm_transactions.execute(transaction_ids)
        except (ScribeError, ValueError) as error:
            typer.echo(f"Could not confirm: {error}", err=True)
            raise typer.Exit(code=1) from error
    typer.echo(
        f"mode=apply confirmed={result.confirmed_count} "
        f"staged={result.intake.staged_count if result.intake else 0}"
    )


@money_cli.command("allocate")
def allocate(
    event_id: Annotated[int, typer.Argument(help="Financial event to allocate.")],
    expected_version: Annotated[
        int, typer.Option("--expected-version", help="Version the caller read.")
    ],
    client_token: Annotated[
        str, typer.Option("--client-token", help="Idempotency token for this attempt.")
    ],
    line: Annotated[
        list[str],
        typer.Option(
            "--line",
            help="amount:wallet_id[:category_id], repeatable; amounts are written "
            "the way they are shown, not in minor units.",
        ),
    ],
    apply: Annotated[
        bool,
        typer.Option(
            "--apply/--dry-run",
            help="Preview by default; --apply posts the balanced entry.",
        ),
    ] = False,
) -> None:
    """Categorize, fund, and post one reviewed event.

    This is the step that actually moves the ledger, so it previews by default
    (ADR-B096).
    """
    with _money_runtime(_load_settings()) as runtime:
        try:
            event = runtime.spending.event(event_id)
            if event is None:
                typer.echo(f"No financial event {event_id}.", err=True)
                raise typer.Exit(code=1)
            currency = event.event.currency
            lines = tuple(_allocation_line(item, currency) for item in line)
            total = sum(item.amount_minor for item in lines)
            if not apply:
                typer.echo(
                    f"mode=dry-run event={event_id} lines={len(lines)} "
                    f"total={_format_money(total, currency)} "
                    f"event_amount={_format_money(event.event.amount_minor, currency)}"
                )
                for item in lines:
                    typer.echo(
                        f"  {_format_money(item.amount_minor, currency)} "
                        f"wallet={item.funding_wallet_id} "
                        f"category={item.budget_category_id or '-'}"
                    )
                typer.echo("Nothing was posted. Rerun with --apply.")
                return
            result = runtime.allocate.execute(
                AllocateFinancialEventRequest(
                    financial_event_id=event_id,
                    expected_version=expected_version,
                    client_token=client_token,
                    lines=lines,
                )
            )
        except (ScribeError, ValueError) as error:
            typer.echo(f"Could not allocate: {error}", err=True)
            raise typer.Exit(code=1) from error
    typer.echo(
        f"mode=apply event={result.event.id} status={result.event.status.value} "
        f"entry={result.entry.id}"
    )


def _allocation_line(value: str, currency: str) -> AllocationLine:
    """Decode one ``amount:wallet_id[:category_id]`` option."""
    parts = value.split(":")
    if len(parts) not in (2, 3):
        raise ValueError(
            "a line is written amount:wallet_id or amount:wallet_id:category_id"
        )
    return AllocationLine(
        amount_minor=parse_display_amount(parts[0], currency),
        funding_wallet_id=int(parts[1]),
        budget_category_id=int(parts[2]) if len(parts) == 3 and parts[2] else None,
    )


@money_cli.command("card-register")
def card_register(
    gmail_account_id: Annotated[int, typer.Option("--gmail-account-id")],
    issuer: Annotated[str, typer.Option("--issuer", help="Institution key.")],
    name: Annotated[str, typer.Option("--name", help="Display name.")],
    last_four: Annotated[str, typer.Option("--last-four", help="Exactly four digits.")],
    holder_role: Annotated[
        str, typer.Option("--holder-role", help="primary, supplementary, or unknown.")
    ] = "unknown",
) -> None:
    """Record one card identity; configuration is a separate, explicit step."""
    with _money_runtime(_load_settings()) as runtime:
        try:
            card = runtime.cards.register(
                RegisterCardRequest(
                    gmail_account_id=gmail_account_id,
                    issuer_code=issuer,
                    display_name=name,
                    last_four=last_four,
                    holder_role=CardHolderRole(holder_role),
                )
            )
        except (ScribeError, ValueError) as error:
            typer.echo(f"Could not register the card: {error}", err=True)
            raise typer.Exit(code=1) from error
    typer.echo(f"card_id={card.id} status={card.configuration_status.value}")


@money_cli.command("card-configure")
def card_configure(
    card_id: Annotated[int, typer.Argument(help="Card to configure.")],
    expected_version: Annotated[int, typer.Option("--expected-version")],
    name: Annotated[str, typer.Option("--name")],
    holder_role: Annotated[str, typer.Option("--holder-role")],
    liability_account_id: Annotated[
        int | None, typer.Option("--liability-account-id")
    ] = None,
    settlement_account_id: Annotated[
        int | None, typer.Option("--settlement-account-id")
    ] = None,
    funding_wallet_id: Annotated[
        int | None, typer.Option("--funding-wallet-id")
    ] = None,
) -> None:
    """Say where a card settles. Both accounts are needed before it is ready."""
    with _money_runtime(_load_settings()) as runtime:
        try:
            card = runtime.cards.configure(
                ConfigureCardRequest(
                    card_id=card_id,
                    expected_version=expected_version,
                    display_name=name,
                    holder_role=CardHolderRole(holder_role),
                    liability_account_id=liability_account_id,
                    settlement_account_id=settlement_account_id,
                    default_funding_wallet_id=funding_wallet_id,
                )
            )
        except ConcurrentUpdateError as error:
            typer.echo(f"Card changed since it was read: {error}", err=True)
            raise typer.Exit(code=1) from error
        except (ScribeError, ValueError) as error:
            typer.echo(f"Could not configure the card: {error}", err=True)
            raise typer.Exit(code=1) from error
    typer.echo(f"card_id={card.id} status={card.configuration_status.value}")


@money_cli.command("card-assign-import")
def card_assign_import(
    import_row_id: Annotated[int, typer.Argument(help="Immutable import row.")],
    card_id: Annotated[int, typer.Option("--card-id")],
) -> None:
    """Attach one reviewed cutover hint to a card, creating no schedule."""
    with _money_runtime(_load_settings()) as runtime:
        try:
            assignment = runtime.cards.assign_import_candidate(
                import_row_id=import_row_id, card_id=card_id
            )
        except (ScribeError, ValueError) as error:
            typer.echo(f"Could not assign the hint: {error}", err=True)
            raise typer.Exit(code=1) from error
    typer.echo(
        f"assignment_id={assignment.id} kind={assignment.assignment_kind.value} "
        "nothing was scheduled and no money moved"
    )


@money_cli.command("category-update")
def category_update(
    category_id: Annotated[int, typer.Argument(help="Category to correct.")],
    expected_version: Annotated[int, typer.Option("--expected-version")],
    name: Annotated[str, typer.Option("--name")],
    parent_id: Annotated[int | None, typer.Option("--parent-id")] = None,
    active: Annotated[
        bool,
        typer.Option(
            "--active/--inactive", help="A referenced category is never deleted."
        ),
    ] = True,
) -> None:
    """Rename, re-parent, or deactivate one spending purpose."""
    with _money_runtime(_load_settings()) as runtime:
        try:
            category = runtime.plan.update_category(
                UpdateCategoryRequest(
                    category_id=category_id,
                    expected_version=expected_version,
                    name=name,
                    parent_id=parent_id,
                    is_active=active,
                )
            )
        except ConcurrentUpdateError as error:
            typer.echo(f"Category changed since it was read: {error}", err=True)
            raise typer.Exit(code=1) from error
        except (ScribeError, ValueError) as error:
            typer.echo(f"Could not update the category: {error}", err=True)
            raise typer.Exit(code=1) from error
    typer.echo(f"category_id={category.id} active={category.is_active}")


@money_cli.command("event-note")
def event_note(
    event_id: Annotated[int, typer.Argument(help="Financial event to annotate.")],
    expected_version: Annotated[int, typer.Option("--expected-version")],
    note: Annotated[
        str | None, typer.Option("--note", help="Omit to clear the note.")
    ] = None,
) -> None:
    """Replace or clear one event's note; the posting is never touched."""
    with _money_runtime(_load_settings()) as runtime:
        try:
            event = runtime.update_event_note.execute(
                UpdateFinancialEventNoteRequest(
                    financial_event_id=event_id,
                    expected_version=expected_version,
                    note=note,
                )
            )
        except ConcurrentUpdateError as error:
            typer.echo(f"Event changed since it was read: {error}", err=True)
            raise typer.Exit(code=1) from error
        except (ScribeError, ValueError) as error:
            typer.echo(f"Could not save the note: {error}", err=True)
            raise typer.Exit(code=1) from error
    typer.echo(f"event_id={event.id} version={event.version}")


@money_cli.command("wallet-reallocate")
def wallet_reallocate(
    wallet_id: Annotated[int, typer.Argument(help="Wallet whose amount changed.")],
    counterpart_wallet_id: Annotated[
        int, typer.Option("--counterpart", help="Wallet in the same account.")
    ],
    expected_minor: Annotated[
        int, typer.Option("--expected-minor", help="Balance the caller read.")
    ],
    target: Annotated[
        str, typer.Option("--target", help="Intended result, written normally.")
    ],
    currency: Annotated[str, typer.Option("--currency")],
    client_token: Annotated[str, typer.Option("--client-token")],
    apply: Annotated[
        bool,
        typer.Option("--apply/--dry-run", help="Preview by default; --apply posts."),
    ] = False,
) -> None:
    """Re-divide one account between two of its wallets; the total never moves."""
    # A wallet can be overdrawn, so this mirrors the Web form exactly
    # (ADR-B075): signed, which already admits zero.
    target_minor = parse_display_amount(target, currency, allow_signed=True)
    if not apply:
        typer.echo(
            f"mode=dry-run wallet={wallet_id} counterpart={counterpart_wallet_id} "
            f"expected={_format_money(expected_minor, currency)} "
            f"target={_format_money(target_minor, currency)} "
            f"move={_format_money(target_minor - expected_minor, currency)}"
        )
        typer.echo("Nothing was posted. Rerun with --apply.")
        return
    with _money_runtime(_load_settings()) as runtime:
        try:
            result = runtime.bank_movements.reallocate(
                ReallocateWalletRequest(
                    wallet_id=wallet_id,
                    counterpart_wallet_id=counterpart_wallet_id,
                    expected_balance_minor=expected_minor,
                    target_balance_minor=target_minor,
                    currency=currency,
                    occurred_at=datetime.now(UTC),
                    client_token=client_token,
                )
            )
        except (ScribeError, ValueError) as error:
            typer.echo(f"Could not reallocate: {error}", err=True)
            raise typer.Exit(code=1) from error
    if result is None:
        typer.echo("mode=apply unchanged=1 (the amount already matched)")
        return
    typer.echo(f"mode=apply event={result.event.id} entry={result.entry.id}")


@money_cli.command("account-order")
def account_order(
    position: Annotated[
        list[str],
        typer.Option("--position", help="account_id:place, repeatable; name them all."),
    ],
) -> None:
    """Store the order the sheet's account columns read in. Moves no money."""
    with _money_runtime(_load_settings()) as runtime:
        try:
            runtime.setup.arrange_accounts(
                ArrangeAccountsRequest(positions=_positions(position))
            )
        except (ScribeError, ValueError) as error:
            typer.echo(f"Could not store the order: {error}", err=True)
            raise typer.Exit(code=1) from error
    typer.echo("Stored. Nothing was posted.")


@money_cli.command("wallet-order")
def wallet_order(
    account_id: Annotated[int, typer.Argument(help="Account whose column to order.")],
    position: Annotated[
        list[str],
        typer.Option("--position", help="wallet_id:place, repeatable; name them all."),
    ],
) -> None:
    """Store the order one account's wallets read in. Moves no money."""
    with _money_runtime(_load_settings()) as runtime:
        try:
            runtime.setup.arrange_wallets(
                ArrangeWalletsRequest(
                    financial_account_id=account_id, positions=_positions(position)
                )
            )
        except (ScribeError, ValueError) as error:
            typer.echo(f"Could not store the order: {error}", err=True)
            raise typer.Exit(code=1) from error
    typer.echo("Stored. Nothing was posted.")


def _positions(values: list[str]) -> dict[int, int]:
    """Decode repeated ``id:place`` options into one arrangement."""
    positions: dict[int, int] = {}
    for value in values:
        identifier, _, place = value.partition(":")
        if not place:
            raise ValueError("a position is written id:place")
        positions[int(identifier)] = int(place)
    return positions


@money_cli.command("supplementary-card-set")
def supplementary_card_set(
    transaction_id: Annotated[int, typer.Argument(help="Transaction naming the card.")],
    supplementary: Annotated[
        bool,
        typer.Option("--supplementary/--primary", help="Mark or unmark the card."),
    ] = True,
) -> None:
    """Set the holder role of the card a transaction names.

    The role is stored on the card itself, so this reports which card it
    landed on and whether that card identity had to be created.
    """
    with _money_runtime(_load_settings()) as runtime:
        try:
            result = runtime.set_supplementary_card.execute(
                SetSupplementaryCardRequest(
                    transaction_id=transaction_id, supplementary=supplementary
                )
            )
        except (ScribeError, ValueError) as error:
            typer.echo(f"Could not change the setting: {error}", err=True)
            raise typer.Exit(code=1) from error
    typer.echo(
        f"institution={result.institution_key} supplementary={supplementary} "
        f"card_id={result.card_id} "
        f"card={'created' if result.card_created else 'updated'}"
    )


@money_cli.command("email-reparse")
def email_reparse(
    email_id: Annotated[int, typer.Argument(help="Stored email to parse again.")],
) -> None:
    """Rerun the parser over one stored email, preserving manual overrides.

    Offline by definition: it reads retained text, never Gmail.
    """
    with _money_runtime(_load_settings()) as runtime:
        try:
            result = runtime.reparse_email.execute(email_id)
        except (ScribeError, ValueError) as error:
            typer.echo(f"Could not reparse: {error}", err=True)
            raise typer.Exit(code=1) from error
    typer.echo(
        f"email_id={email_id} candidates={len(result.candidates)} "
        f"classification={result.classification.value} parser={result.parser_name}"
    )


@money_cli.command("sync-cancel")
def sync_cancel(
    run_id: Annotated[str, typer.Argument(help="Sync run to stop.")],
) -> None:
    """Ask whichever process owns a sync run to stop at its next safe boundary.

    Cancellation is persisted, so this works against a run the Web process
    started; every committed checkpoint is preserved.
    """
    with _money_runtime(_load_settings()) as runtime:
        try:
            runtime.sync_control.request_cancel(run_id)
        except (ScribeError, ValueError) as error:
            typer.echo(f"Could not cancel: {error}", err=True)
            raise typer.Exit(code=1) from error
    typer.echo(f"Cancellation requested for {run_id}.")


@money_cli.command("transaction-review")
def transaction_review(
    transaction_id: Annotated[int, typer.Argument(help="Parsed transaction.")],
    expected_updated_at: Annotated[
        str, typer.Option("--expected-updated-at", help="ISO timestamp last read.")
    ],
    transaction_type: Annotated[str, typer.Option("--type")],
    override: Annotated[
        list[str] | None,
        typer.Option(
            "--override",
            help="Field to take manually: merchant, amount_minor, currency, "
            "transaction_at, transaction_type. Repeatable.",
        ),
    ] = None,
    merchant: Annotated[str | None, typer.Option("--merchant")] = None,
    amount: Annotated[
        str | None, typer.Option("--amount", help="Written normally, not minor units.")
    ] = None,
    currency: Annotated[str | None, typer.Option("--currency")] = None,
    transaction_at: Annotated[
        str | None, typer.Option("--transaction-at", help="ISO timestamp.")
    ] = None,
    confirmed: Annotated[bool, typer.Option("--confirmed/--unconfirmed")] = False,
    ignored: Annotated[bool, typer.Option("--ignored/--not-ignored")] = False,
    note: Annotated[str | None, typer.Option("--note")] = None,
) -> None:
    """Record manual review of one parsed transaction.

    Only the fields named by --override are taken manually; everything else
    keeps whatever the parser derived, so a rerun can still correct them.
    """
    with _money_runtime(_load_settings()) as runtime:
        try:
            runtime.review_transaction.execute(
                ReviewTransactionRequest(
                    transaction_id=transaction_id,
                    expected_updated_at=datetime.fromisoformat(expected_updated_at),
                    override_fields=frozenset(override or ()),
                    transaction_type=TransactionType(transaction_type),
                    merchant=merchant,
                    amount_minor=(
                        None
                        if amount is None
                        else parse_display_amount(amount, currency)
                    ),
                    currency=currency,
                    transaction_at=(
                        None
                        if transaction_at is None
                        else datetime.fromisoformat(transaction_at)
                    ),
                    manually_confirmed=confirmed,
                    manually_ignored=ignored,
                    user_note=note,
                )
            )
        except ConcurrentUpdateError as error:
            typer.echo(f"Transaction changed since it was read: {error}", err=True)
            raise typer.Exit(code=1) from error
        except (ScribeError, ValueError) as error:
            typer.echo(f"Could not save the review: {error}", err=True)
            raise typer.Exit(code=1) from error
    typer.echo(
        f"transaction_id={transaction_id} overrides={sorted(override or ())} "
        f"confirmed={confirmed} ignored={ignored}"
    )


@money_cli.command("event-show")
def event_show(
    event_id: Annotated[int, typer.Argument(help="Financial event to read.")],
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit the event as one JSON document.")
    ] = False,
) -> None:
    """Read one event, including the version a correction has to quote.

    Every write against an event is optimistic, so the version is part of what
    reading it is for (ADR-B097).
    """
    with _money_runtime(_load_settings()) as runtime:
        detail = runtime.spending.event(event_id)
    if detail is None:
        typer.echo(f"No financial event {event_id}.", err=True)
        raise typer.Exit(code=1)
    event = detail.event
    if as_json:
        emit_json(
            {
                "id": event.id,
                "version": event.version,
                "status": event.status.value,
                "event_type": event.event_type.value,
                "payment_method": event.payment_method.value,
                "occurred_at": event.occurred_at,
                "amount_minor": event.amount_minor,
                "currency": event.currency,
                "merchant": event.merchant,
                "note": event.note,
                "card_id": event.card_id,
                "card_display": detail.card_display,
                "entry_id": None if detail.entry is None else detail.entry.id,
            }
        )
        return
    typer.echo(
        f"event_id={event.id} version={event.version} status={event.status.value} "
        f"{_format_money(event.amount_minor, event.currency)} "
        f"{event.merchant or '-'}"
    )


@money_cli.command("transaction-show")
def transaction_show(
    transaction_id: Annotated[int, typer.Argument(help="Parsed transaction.")],
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit the transaction as JSON.")
    ] = False,
) -> None:
    """Read one parsed transaction, including what a review has to quote.

    `updated_at` is the optimistic token `transaction-review` demands, and
    `email_id` is what `email-reparse` needs, so both are part of this read.
    """
    with _money_runtime(_load_settings()) as runtime:
        detail = runtime.queries.transaction_detail(transaction_id)
    if detail is None:
        typer.echo(f"No transaction {transaction_id}.", err=True)
        raise typer.Exit(code=1)
    item = detail.item
    if as_json:
        emit_json(
            {
                "id": item.id,
                "updated_at": detail.updated_at,
                "email_id": item.email_id,
                "merchant": item.merchant,
                "amount_minor": item.amount_minor,
                "currency": item.currency,
                "transaction_type": item.transaction_type.value,
                "card_last_four": item.card_last_four,
                "manually_confirmed": item.manually_confirmed,
                "manual_overrides": sorted(detail.manual_overrides),
            }
        )
        return
    typer.echo(
        f"transaction_id={item.id} "
        f"updated_at={detail.updated_at.isoformat()} email_id={item.email_id} "
        f"confirmed={item.manually_confirmed} "
        f"{_format_money(item.amount_minor or 0, item.currency or '')} "
        f"{item.merchant or '-'}"
    )


@money_cli.command("status")
def status(
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Emit the snapshot as one JSON document."),
    ] = False,
) -> None:
    """Summarise local state in one read: accounts, cards, and what is waiting.

    Written for an operator who has just arrived and needs the shape of things
    before deciding anything (ADR-B095). It reads only; nothing here posts,
    confirms, or schedules.
    """
    settings = _load_settings()
    today = datetime.now(ZoneInfo(settings.local_timezone)).date()
    with _money_runtime(settings) as runtime:
        try:
            accounts = runtime.spending.accounts()
            cards = runtime.cards.list_cards()
            month = runtime.budget.month(
                year=today.year,
                month=today.month,
                currency=settings.default_currency,
            )
        except ScribeError as error:
            typer.echo(f"Could not read local state: {error}", err=True)
            raise typer.Exit(code=1) from error

    snapshot = {
        "local_date": today.isoformat(),
        "migration_revision": inspect_migration_status(settings).current_revision,
        "accounts": [
            {
                "id": row.account.id,
                "display_name": row.account.display_name,
                "account_type": row.account.account_type.value,
                "currency": row.account.currency,
                "balance_minor": row.balance_minor,
                "reserved_minor": row.reserved_minor,
                "available_minor": row.available_minor,
                "wallet_count": len(row.wallets),
            }
            for row in accounts
        ],
        "cards": [
            {
                "id": card.id,
                "display_name": card.display_name,
                "issuer_code": card.issuer_code,
                "last_four": card.last_four,
                "holder_role": card.holder_role.value,
                "is_ready": card.is_ready,
            }
            for card in cards
        ],
        "waiting": {
            "events_needing_allocation": month.events_needing_allocation,
        },
        "budget_month": {
            "label": month.label,
            "currency": month.currency,
            "target_minor": month.total_target_minor,
            "spend_minor": month.total_spend_minor,
        },
    }
    if as_json:
        emit_json(snapshot)
        return
    typer.echo(
        f"revision={snapshot['migration_revision']} "
        f"accounts={len(accounts)} "
        f"cards={len(cards)} "
        f"cards_ready={sum(1 for card in cards if card.is_ready)} "
        f"needing_allocation={month.events_needing_allocation}"
    )
    for row in accounts:
        typer.echo(
            f"  {row.account.display_name:<16} "
            f"{_format_money(row.balance_minor, row.account.currency)}"
        )


@money_cli.command("balances")
def balances(
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Emit the same balances as one JSON document."),
    ] = False,
) -> None:
    """Show calculated account balances and their wallet composition."""
    with _money_runtime(_load_settings()) as runtime:
        try:
            accounts = runtime.balances.account_balances()
        except ScribeError as error:
            typer.echo(f"Could not read balances: {error}", err=True)
            raise typer.Exit(code=1) from error
    if as_json:
        emit_json({"accounts": list(accounts)})
        return
    if not accounts:
        typer.echo("No financial accounts are configured.")
        return
    for account in accounts:
        typer.echo(
            f"{account.financial_account_id:<4} {account.institution_code:<14} "
            f"{account.display_name:<20} "
            f"{_format_money(account.balance_minor, account.currency)}"
        )
        for wallet in account.wallets:
            state = "" if wallet.is_active else " (inactive)"
            typer.echo(
                f"       └─ {wallet.name:<20} "
                f"{_format_money(wallet.balance_minor, wallet.currency)}{state}"
            )


@money_cli.command("category-add")
def category_add(
    name: Annotated[str, typer.Argument(help="Display name of the new category.")],
    parent_id: Annotated[
        int | None,
        typer.Option("--parent-id", help="Nest this category under an existing one."),
    ] = None,
) -> None:
    """Create one currency-neutral spending purpose."""
    with _money_runtime(_load_settings()) as runtime:
        try:
            category = runtime.plan.add_category(
                AddCategoryRequest(name=name, parent_id=parent_id)
            )
        except (ScribeError, ValueError) as error:
            typer.echo(f"Could not add the category: {error}", err=True)
            raise typer.Exit(code=1) from error
    typer.echo(f"Created category {category.id}: {category.name}")


@money_cli.command("category-list")
def category_list() -> None:
    """Show every budget category, including deactivated ones."""
    with _money_runtime(_load_settings()) as runtime:
        try:
            categories = runtime.plan.list_categories()
        except ScribeError as error:
            typer.echo(f"Could not read categories: {error}", err=True)
            raise typer.Exit(code=1) from error
    if not categories:
        typer.echo("No budget categories are configured.")
        return
    for category in categories:
        parent = "" if category.parent_id is None else f" parent={category.parent_id}"
        state = "" if category.is_active else " (inactive)"
        # The version is printed because correcting a category demands it, and
        # an optimistic token nobody can read is a token nobody can use
        # (ADR-B097).
        typer.echo(
            f"{category.id:<4} version={category.version} "
            f"{category.name}{parent}{state}"
        )


@money_cli.command("target-set")
def target_set(
    period: Annotated[str, typer.Argument(help="Budget month written as yyyy-mm.")],
    category_id: Annotated[int, typer.Option("--category-id")],
    target_minor: Annotated[
        int, typer.Option("--target-minor", help="Spendable target in minor units.")
    ],
    funding_wallet_id: Annotated[int, typer.Option("--funding-wallet-id")],
    buffer_minor: Annotated[
        int,
        typer.Option(
            "--buffer-minor",
            help="Cushion held beside the target; never part of it.",
        ),
    ] = 0,
    destination_wallet_id: Annotated[
        int | None,
        typer.Option(
            "--destination-wallet-id",
            help="Where unspent target money is swept at month end.",
        ),
    ] = None,
    expected_version: Annotated[
        int | None,
        typer.Option(
            "--expected-version",
            help="Required to correct an existing target; see budget-show.",
        ),
    ] = None,
    currency: Annotated[
        str | None,
        typer.Option("--currency", help="Defaults to the configured currency."),
    ] = None,
) -> None:
    """Set one category's target and buffer for one month."""
    settings = _load_settings()
    try:
        year, month = parse_period_label(period)
    except ScribeError as error:
        typer.echo(f"Could not read the period: {error}", err=True)
        raise typer.Exit(code=1) from error
    code = currency or settings.default_currency
    with _money_runtime(settings) as runtime:
        try:
            existing = runtime.plan.find_target(
                year=year,
                month=month,
                currency=code,
                budget_category_id=category_id,
            )
        except ScribeError as error:
            typer.echo(f"Could not read the target: {error}", err=True)
            raise typer.Exit(code=1) from error
        if existing is not None and expected_version is None:
            # Overwriting silently would discard whatever another window just
            # saved, so the correction has to name the version it saw.
            typer.echo(
                f"This target already exists at version {existing.version}. "
                f"Re-run with --expected-version {existing.version} to correct it.",
                err=True,
            )
            raise typer.Exit(code=1)
        try:
            saved = runtime.plan.set_target(
                SetTargetRequest(
                    year=year,
                    month=month,
                    currency=code,
                    budget_category_id=category_id,
                    target_minor=target_minor,
                    buffer_minor=buffer_minor,
                    funding_wallet_id=funding_wallet_id,
                    month_end_destination_wallet_id=destination_wallet_id,
                    expected_version=expected_version,
                )
            )
        except (ScribeError, ValueError, TypeError) as error:
            typer.echo(f"Could not set the target: {error}", err=True)
            raise typer.Exit(code=1) from error
    typer.echo(
        f"{period} category={saved.budget_category_id} "
        f"target={_format_money(saved.target_minor, code)} "
        f"buffer={_format_money(saved.buffer_minor, code)} "
        f"version={saved.version}"
    )


@money_cli.command("budget-show")
def budget_show(
    period: Annotated[str, typer.Argument(help="Budget month written as yyyy-mm.")],
    currency: Annotated[str | None, typer.Option("--currency")] = None,
) -> None:
    """Show one month's targets, spending, and remaining amounts."""
    settings = _load_settings()
    try:
        year, month = parse_period_label(period)
    except ScribeError as error:
        typer.echo(f"Could not read the period: {error}", err=True)
        raise typer.Exit(code=1) from error
    with _money_runtime(settings) as runtime:
        try:
            view = runtime.budget.month(
                year=year, month=month, currency=currency or settings.default_currency
            )
        except ScribeError as error:
            typer.echo(f"Could not read the budget: {error}", err=True)
            raise typer.Exit(code=1) from error
    typer.echo(
        f"{view.label} target={_format_money(view.total_target_minor, view.currency)} "
        f"spent={_format_money(view.total_spend_minor, view.currency)} "
        f"remaining={_format_money(view.total_remaining_minor, view.currency)} "
        f"buffer={_format_money(view.total_buffer_minor, view.currency)} "
        f"buffer_left="
        f"{_format_money(view.total_buffer_remaining_minor, view.currency)}"
    )
    for item in view.categories:
        over = (
            ""
            if not item.overspend_minor
            else f" over={_format_money(item.overspend_minor, item.currency)}"
        )
        planned = f" version={item.target_version}" if item.is_planned else " unplanned"
        typer.echo(
            f"  {item.name:<20} "
            f"{_format_money(item.net_spend_minor, item.currency)} / "
            f"{_format_money(item.target_minor, item.currency)}{over}{planned}"
        )
    if view.unplanned_spend_minor:
        typer.echo(
            "unplanned="
            f"{_format_money(view.unplanned_spend_minor, view.currency)} "
            "(spending with no target for this month, still counted above)"
        )
    if view.events_needing_allocation:
        typer.echo(f"{view.events_needing_allocation} events still need allocation.")


@money_cli.command("budget-close")
def budget_close(
    period: Annotated[str, typer.Argument(help="Budget month written as yyyy-mm.")],
    apply: Annotated[
        bool,
        typer.Option(
            "--apply/--dry-run",
            help="Preview by default; --apply commits the sweep and closes.",
        ),
    ] = False,
    currency: Annotated[str | None, typer.Option("--currency")] = None,
) -> None:
    """Preview, then optionally commit, the month-end sweep."""
    settings = _load_settings()
    try:
        year, month = parse_period_label(period)
    except ScribeError as error:
        typer.echo(f"Could not read the period: {error}", err=True)
        raise typer.Exit(code=1) from error
    code = currency or settings.default_currency
    with _money_runtime(settings) as runtime:
        try:
            preview = (
                runtime.close_period.execute(
                    year=year, month=month, currency=code
                ).preview
                if apply
                else runtime.close_period.preview(year=year, month=month, currency=code)
            )
        except (ScribeError, ValueError) as error:
            typer.echo(f"Could not close the month: {error}", err=True)
            raise typer.Exit(code=1) from error
    mode = "apply" if apply else "dry-run"
    typer.echo(
        f"mode={mode} period={preview.period.label} "
        f"sweep={_format_money(preview.total_sweep_minor, code)} "
        f"already_closed={preview.already_closed}"
    )
    for line in preview.lines:
        note = (
            " requires-manual-transfer"
            if line.requires_manual_transfer
            else (" limited-by-available" if line.limited_by_available else "")
        )
        typer.echo(
            f"  {line.category_name:<20} "
            f"unspent={_format_money(line.unspent_minor, code)} "
            f"sweep={_format_money(line.sweep_minor, code)}{note}"
        )
    if preview.manual_transfers:
        typer.echo(
            "A destination wallet in another account needs a real bank transfer; "
            "nothing was posted for those lines."
        )


@money_cli.command("subscription-list")
def subscription_list() -> None:
    """Show every subscription, deactivated ones included."""
    with _money_runtime(_load_settings()) as runtime:
        try:
            rows = runtime.subscription_register.register()
        except ScribeError as error:
            typer.echo(f"Could not read subscriptions: {error}", err=True)
            raise typer.Exit(code=1) from error
    if not rows:
        typer.echo("No subscriptions are configured.")
        return
    for row in rows:
        item = row.subscription
        state = "" if item.is_active else " (inactive)"
        typer.echo(
            f"{item.id:<4} {item.name:<24} {item.currency} "
            f"{item.billing_cadence.value}/{item.cadence_months} "
            f"anchor_month={item.billing_month or '-'} "
            f"active={_format_month(item.active_from)}~"
            f"{_format_month(item.active_to)} "
            f"wallet={row.funding_wallet_name} "
            f"category={row.budget_category_name} "
            f"version={item.version}{state}"
        )
        for price in row.prices:
            typer.echo(
                f"       price_id={price.id} "
                f"{_format_money(price.amount_minor, item.currency)} "
                f"{_format_month(price.effective_from)}~"
                f"{_format_month(price.effective_to)} "
                f"version={price.version}"
            )


@money_cli.command("subscription-add")
def subscription_add(
    name: Annotated[str, typer.Argument(help="Display name of the subscription.")],
    category_id: Annotated[int, typer.Option("--category-id", min=1)],
    funding_wallet_id: Annotated[int, typer.Option("--funding-wallet-id", min=1)],
    cadence: Annotated[
        SubscriptionCadence,
        typer.Option("--cadence", help="monthly, yearly, or custom."),
    ] = SubscriptionCadence.MONTHLY,
    cadence_months: Annotated[
        int | None,
        typer.Option(
            "--cadence-months",
            help="Fixed month interval; defaults to 1 monthly and 12 yearly.",
        ),
    ] = None,
    billing_month: Annotated[
        int | None,
        typer.Option("--billing-month", help="Cycle anchor month for a yearly plan."),
    ] = None,
    billing_day: Annotated[
        int | None,
        typer.Option("--billing-day", help="Recorded only; never used to prorate."),
    ] = None,
    active_from: Annotated[
        str | None,
        typer.Option("--active-from", help="yyyy-mm; required for a custom cadence."),
    ] = None,
    active_to: Annotated[
        str | None,
        typer.Option("--active-to", help="yyyy-mm; the month it stops counting."),
    ] = None,
    currency: Annotated[
        str | None,
        typer.Option("--currency", help="Defaults to the configured currency."),
    ] = None,
) -> None:
    """Register one subscription funded from a wallet in its own currency."""
    settings = _load_settings()
    default_months = {
        SubscriptionCadence.MONTHLY: 1,
        SubscriptionCadence.YEARLY: 12,
    }
    with _money_runtime(settings) as runtime:
        try:
            subscription = runtime.subscriptions.add(
                AddSubscriptionRequest(
                    name=name,
                    budget_category_id=category_id,
                    funding_wallet_id=funding_wallet_id,
                    currency=currency or settings.default_currency,
                    billing_cadence=cadence,
                    cadence_months=(
                        cadence_months
                        if cadence_months is not None
                        else default_months.get(cadence, 0)
                    ),
                    billing_month=billing_month,
                    billing_day=billing_day,
                    active_from=_parse_month(active_from),
                    active_to=_parse_month(active_to),
                )
            )
        except (ScribeError, ValueError, TypeError) as error:
            typer.echo(f"Could not add the subscription: {error}", err=True)
            raise typer.Exit(code=1) from error
    typer.echo(
        f"subscription_id={subscription.id} {subscription.name} "
        f"{subscription.currency} {subscription.billing_cadence.value}/"
        f"{subscription.cadence_months} version={subscription.version}"
    )
    typer.echo("No price is set yet, so this subscription still requires 0 per month.")


@money_cli.command("subscription-update")
def subscription_update(
    subscription_id: Annotated[int, typer.Argument(min=1)],
    expected_version: Annotated[
        int,
        typer.Option(
            "--expected-version",
            help="Required; see subscription-list. A blind overwrite is refused.",
        ),
    ],
    name: Annotated[str | None, typer.Option("--name")] = None,
    category_id: Annotated[int | None, typer.Option("--category-id")] = None,
    funding_wallet_id: Annotated[
        int | None, typer.Option("--funding-wallet-id")
    ] = None,
    cadence: Annotated[SubscriptionCadence | None, typer.Option("--cadence")] = None,
    cadence_months: Annotated[int | None, typer.Option("--cadence-months")] = None,
    billing_month: Annotated[int | None, typer.Option("--billing-month")] = None,
    billing_day: Annotated[int | None, typer.Option("--billing-day")] = None,
    active_from: Annotated[str | None, typer.Option("--active-from")] = None,
    active_to: Annotated[str | None, typer.Option("--active-to")] = None,
    currency: Annotated[str | None, typer.Option("--currency")] = None,
) -> None:
    """Correct one subscription; every omitted option keeps its current value."""
    with _money_runtime(_load_settings()) as runtime:
        try:
            current = runtime.subscriptions.get(subscription_id)
            if current is None:
                typer.echo("Subscription was not found.", err=True)
                raise typer.Exit(code=1)
            updated = runtime.subscriptions.update(
                UpdateSubscriptionRequest(
                    subscription_id=subscription_id,
                    expected_version=expected_version,
                    name=name if name is not None else current.name,
                    budget_category_id=(
                        category_id
                        if category_id is not None
                        else current.budget_category_id
                    ),
                    funding_wallet_id=(
                        funding_wallet_id
                        if funding_wallet_id is not None
                        else current.funding_wallet_id
                    ),
                    currency=currency if currency is not None else current.currency,
                    billing_cadence=(
                        cadence if cadence is not None else current.billing_cadence
                    ),
                    cadence_months=(
                        cadence_months
                        if cadence_months is not None
                        else current.cadence_months
                    ),
                    billing_month=(
                        billing_month
                        if billing_month is not None
                        else current.billing_month
                    ),
                    billing_day=(
                        billing_day if billing_day is not None else current.billing_day
                    ),
                    active_from=(
                        _parse_month(active_from)
                        if active_from is not None
                        else current.active_from
                    ),
                    active_to=(
                        _parse_month(active_to)
                        if active_to is not None
                        else current.active_to
                    ),
                )
            )
        except (ScribeError, ValueError, TypeError) as error:
            typer.echo(f"Could not update the subscription: {error}", err=True)
            raise typer.Exit(code=1) from error
    typer.echo(
        f"subscription_id={updated.id} {updated.name} {updated.currency} "
        f"{updated.billing_cadence.value}/{updated.cadence_months} "
        f"version={updated.version}"
    )


@money_cli.command("subscription-deactivate")
def subscription_deactivate(
    subscription_id: Annotated[int, typer.Argument(min=1)],
    expected_version: Annotated[int, typer.Option("--expected-version")],
    effective_from: Annotated[
        str,
        typer.Option(
            "--effective-from",
            help="yyyy-mm; the first month that no longer counts.",
        ),
    ],
) -> None:
    """Stop one subscription from a named month without deleting history."""
    with _money_runtime(_load_settings()) as runtime:
        try:
            subscription = runtime.subscriptions.deactivate(
                DeactivateSubscriptionRequest(
                    subscription_id=subscription_id,
                    expected_version=expected_version,
                    active_to=_require_month(effective_from, label="--effective-from"),
                )
            )
        except (ScribeError, ValueError, TypeError) as error:
            typer.echo(f"Could not deactivate the subscription: {error}", err=True)
            raise typer.Exit(code=1) from error
    typer.echo(
        f"subscription_id={subscription.id} active={subscription.is_active} "
        f"active_to={_format_month(subscription.active_to)} "
        f"version={subscription.version}"
    )
    typer.echo("Months before that stay exactly as they were calculated.")


@money_cli.command("subscription-resume")
def subscription_resume(
    subscription_id: Annotated[int, typer.Argument(min=1)],
    active_from: Annotated[
        str,
        typer.Option(
            "--active-from",
            help="yyyy-mm; the first month the new interval counts again.",
        ),
    ],
    active_to: Annotated[
        str | None,
        typer.Option("--active-to", help="yyyy-mm; leave empty for no end month."),
    ] = None,
) -> None:
    """Start a new interval of a stopped subscription, keeping the old one."""
    with _money_runtime(_load_settings()) as runtime:
        try:
            resumed = runtime.subscriptions.resume(
                ResumeSubscriptionRequest(
                    subscription_id=subscription_id,
                    active_from=_require_month(active_from, label="--active-from"),
                    active_to=_parse_month(active_to),
                )
            )
        except (ScribeError, ValueError, TypeError) as error:
            typer.echo(f"Could not resume the subscription: {error}", err=True)
            raise typer.Exit(code=1) from error
    typer.echo(
        f"subscription_id={resumed.id} {resumed.name} "
        f"active_from={_format_month(resumed.active_from)} "
        f"version={resumed.version}"
    )
    typer.echo(
        f"Subscription {subscription_id} keeps the month it stopped. "
        "Add a price period for the new interval."
    )


@money_cli.command("subscription-price-add")
def subscription_price_add(
    subscription_id: Annotated[int, typer.Argument(min=1)],
    amount_minor: Annotated[
        int,
        typer.Option("--amount-minor", help="Cycle price in minor units; 0 allowed."),
    ],
    effective_from: Annotated[str, typer.Option("--effective-from", help="yyyy-mm.")],
    effective_to: Annotated[
        str | None,
        typer.Option("--effective-to", help="yyyy-mm, excluded; empty means open."),
    ] = None,
) -> None:
    """Add one effective-dated price that overlaps no existing range."""
    with _money_runtime(_load_settings()) as runtime:
        try:
            price = runtime.subscriptions.add_price(
                AddPricePeriodRequest(
                    subscription_id=subscription_id,
                    amount_minor=amount_minor,
                    effective_from=_require_month(
                        effective_from, label="--effective-from"
                    ),
                    effective_to=_parse_month(effective_to),
                )
            )
        except (ScribeError, ValueError, TypeError) as error:
            typer.echo(f"Could not add the price period: {error}", err=True)
            raise typer.Exit(code=1) from error
    typer.echo(
        f"price_id={price.id} subscription_id={price.subscription_id} "
        f"amount_minor={price.amount_minor} "
        f"{_format_month(price.effective_from)}~"
        f"{_format_month(price.effective_to)} version={price.version}"
    )


@money_cli.command("subscription-price-update")
def subscription_price_update(
    price_id: Annotated[int, typer.Argument(min=1)],
    expected_version: Annotated[int, typer.Option("--expected-version")],
    amount_minor: Annotated[int | None, typer.Option("--amount-minor")] = None,
    effective_from: Annotated[str | None, typer.Option("--effective-from")] = None,
    effective_to: Annotated[str | None, typer.Option("--effective-to")] = None,
) -> None:
    """Correct one price period; history is versioned, never deleted."""
    with _money_runtime(_load_settings()) as runtime:
        try:
            current = runtime.subscriptions.get_price(price_id)
            if current is None:
                typer.echo("Subscription price period was not found.", err=True)
                raise typer.Exit(code=1)
            price = runtime.subscriptions.update_price(
                UpdatePricePeriodRequest(
                    price_period_id=price_id,
                    expected_version=expected_version,
                    amount_minor=(
                        amount_minor
                        if amount_minor is not None
                        else current.amount_minor
                    ),
                    effective_from=(
                        _require_month(effective_from, label="--effective-from")
                        if effective_from is not None
                        else current.effective_from
                    ),
                    effective_to=(
                        _parse_month(effective_to)
                        if effective_to is not None
                        else current.effective_to
                    ),
                )
            )
        except (ScribeError, ValueError, TypeError) as error:
            typer.echo(f"Could not update the price period: {error}", err=True)
            raise typer.Exit(code=1) from error
    typer.echo(
        f"price_id={price.id} amount_minor={price.amount_minor} "
        f"{_format_month(price.effective_from)}~"
        f"{_format_month(price.effective_to)} version={price.version}"
    )


@money_cli.command("subscription-default-wallet-show")
def subscription_default_wallet_show() -> None:
    """Show the configured default subscription funding wallet."""
    with _money_runtime(_load_settings()) as runtime:
        try:
            view = runtime.subscriptions.default_funding_wallet()
        except ScribeError as error:
            typer.echo(f"Could not read the default wallet: {error}", err=True)
            raise typer.Exit(code=1) from error
    if not view.is_configured:
        typer.echo("No default subscription funding wallet is configured.")
        return
    typer.echo(
        f"wallet_id={view.wallet_id} {view.financial_account_name} · "
        f"{view.wallet_name} {view.currency} "
        f"usable={view.is_usable}"
        + ("" if view.problem is None else f" problem={view.problem}")
    )


@money_cli.command("subscription-default-wallet-set")
def subscription_default_wallet_set(
    wallet_id: Annotated[int, typer.Option("--wallet-id", min=1)],
) -> None:
    """Name the wallet new subscriptions are prefilled from."""
    with _money_runtime(_load_settings()) as runtime:
        try:
            view = runtime.subscriptions.set_default_funding_wallet(wallet_id)
        except (ScribeError, ValueError) as error:
            typer.echo(f"Could not set the default wallet: {error}", err=True)
            raise typer.Exit(code=1) from error
    typer.echo(
        f"wallet_id={view.wallet_id} {view.financial_account_name} · "
        f"{view.wallet_name} {view.currency}"
    )
    typer.echo("This is a default only. No money moved and nothing was reserved.")


@money_cli.command("subscription-requirement")
def subscription_requirement(
    period: Annotated[
        str, typer.Option("--period", help="Preview month written as yyyy-mm.")
    ],
    currency: Annotated[
        str | None,
        typer.Option("--currency", help="One currency; totals are never combined."),
    ] = None,
) -> None:
    """Preview one month of subscription funding requirement."""
    settings = _load_settings()
    try:
        year, month = parse_period_label(period)
    except ScribeError as error:
        typer.echo(f"Could not read the period: {error}", err=True)
        raise typer.Exit(code=1) from error
    with _money_runtime(settings) as runtime:
        try:
            codes = (
                (currency,)
                if currency is not None
                else (
                    runtime.subscription_requirement.currencies()
                    or (settings.default_currency,)
                )
            )
            requirements = tuple(
                runtime.subscription_requirement.execute(
                    year=year, month=month, currency=code
                )
                for code in codes
            )
        except (ScribeError, ValueError) as error:
            typer.echo(f"Could not calculate the requirement: {error}", err=True)
            raise typer.Exit(code=1) from error
    for requirement in requirements:
        typer.echo(
            f"{requirement.label} {requirement.currency} "
            f"total={_format_money(requirement.total_minor, requirement.currency)}"
        )
        for item in requirement.items:
            position = (
                "-"
                if item.cycle_position is None
                else f"{item.cycle_position + 1}/{item.cadence_months}"
            )
            cycle_price = (
                "-" if item.cycle_price_minor is None else item.cycle_price_minor
            )
            # The exact minor units are printed beside the formatted amount:
            # TWD and JPY display without decimals, so the formatted value
            # alone would hide the share this month actually contributes.
            typer.echo(
                f"  {item.name:<24} "
                f"{_format_money(item.contribution_minor, item.currency)} "
                f"contribution_minor={item.contribution_minor} "
                f"cadence={item.cadence.value} cycle={position} "
                f"price_id={item.price_period_id or '-'} "
                f"cycle_price_minor={cycle_price} "
                f"wallet={item.funding_wallet_name} "
                f"category={item.budget_category_name} "
                f"reason={item.reason.value}"
            )


@money_cli.command("salary-template-list")
def salary_template_list() -> None:
    """Show every salary template and the destinations it distributes into."""
    with _money_runtime(_load_settings()) as runtime:
        try:
            rows = runtime.salary.templates()
        except ScribeError as error:
            typer.echo(f"Could not read salary templates: {error}", err=True)
            raise typer.Exit(code=1) from error
    if not rows:
        typer.echo("No salary templates are configured.")
        return
    for row in rows:
        template = row.template
        state = "" if template.is_active else " (inactive)"
        typer.echo(
            f"{template.id:<4} {template.name:<24} {template.currency} "
            f"account={row.source_account_name} "
            f"wallet={row.source_wallet_name or '-'} "
            f"version={template.version}{state}"
        )
        for entry in row.items:
            item = entry.item
            fixed = (
                "-"
                if item.fixed_amount_minor is None
                else _format_money(item.fixed_amount_minor, template.currency)
            )
            fixed_minor = (
                "-" if item.fixed_amount_minor is None else item.fixed_amount_minor
            )
            inactive = "" if item.is_active else " (inactive)"
            typer.echo(
                f"       item_id={item.id} {item.name:<20} "
                f"mode={item.calculation_mode.value} fixed={fixed} "
                f"fixed_minor={fixed_minor} "
                f"destination={entry.destination_account_name}/"
                f"{entry.destination_wallet_name} "
                f"category={entry.budget_category_name or '-'} "
                f"order={item.display_order} version={item.version}{inactive}"
            )


@money_cli.command("salary-template-add")
def salary_template_add(
    name: Annotated[str, typer.Argument(help="Display name of the template.")],
    source_account_id: Annotated[
        int,
        typer.Option("--source-account-id", min=1, help="The salary account."),
    ],
    currency: Annotated[
        str | None,
        typer.Option("--currency", help="Defaults to the configured currency."),
    ] = None,
) -> None:
    """Register one salary distribution template."""
    settings = _load_settings()
    with _money_runtime(settings) as runtime:
        try:
            template = runtime.salary_templates.add_template(
                AddSalaryTemplateRequest(
                    name=name,
                    currency=currency or settings.default_currency,
                    source_account_id=source_account_id,
                )
            )
        except (ScribeError, ValueError, TypeError) as error:
            typer.echo(f"Could not add the salary template: {error}", err=True)
            raise typer.Exit(code=1) from error
    typer.echo(
        f"template_id={template.id} {template.name} {template.currency} "
        f"version={template.version}"
    )
    typer.echo("Currency and salary account are fixed; add items next.")


@money_cli.command("salary-template-update")
def salary_template_update(
    template_id: Annotated[int, typer.Argument(min=1)],
    expected_version: Annotated[
        int,
        typer.Option(
            "--expected-version",
            help="Required; see salary-template-list. A blind overwrite is refused.",
        ),
    ],
    name: Annotated[str | None, typer.Option("--name")] = None,
    active: Annotated[
        bool | None,
        typer.Option("--active/--inactive", help="Whether the template is in use."),
    ] = None,
) -> None:
    """Rename or deactivate one template; every omitted option is kept."""
    with _money_runtime(_load_settings()) as runtime:
        try:
            current = runtime.salary.template(template_id)
            if current is None:
                typer.echo("Salary template was not found.", err=True)
                raise typer.Exit(code=1)
            template = runtime.salary_templates.update_template(
                UpdateSalaryTemplateRequest(
                    template_id=template_id,
                    expected_version=expected_version,
                    name=name if name is not None else current.template.name,
                    is_active=(
                        active if active is not None else current.template.is_active
                    ),
                )
            )
        except (ScribeError, ValueError, TypeError) as error:
            typer.echo(f"Could not update the salary template: {error}", err=True)
            raise typer.Exit(code=1) from error
    typer.echo(
        f"template_id={template.id} {template.name} active={template.is_active} "
        f"version={template.version}"
    )


@money_cli.command("salary-template-item-add")
def salary_template_item_add(
    template_id: Annotated[int, typer.Argument(min=1)],
    name: Annotated[str, typer.Option("--name", help="Display name of the item.")],
    destination_wallet_id: Annotated[
        int, typer.Option("--destination-wallet-id", min=1)
    ],
    mode: Annotated[
        SalaryCalculationMode,
        typer.Option("--mode", help="fixed, subscription_requirement, or manual."),
    ] = SalaryCalculationMode.FIXED,
    fixed_amount_minor: Annotated[
        int | None,
        typer.Option(
            "--fixed-amount-minor",
            help="Integer minor units; only for --mode fixed.",
        ),
    ] = None,
    category_id: Annotated[int | None, typer.Option("--category-id")] = None,
    display_order: Annotated[int, typer.Option("--display-order")] = 0,
) -> None:
    """Add one destination to a template and bump the template version."""
    with _money_runtime(_load_settings()) as runtime:
        try:
            item = runtime.salary_templates.add_item(
                AddSalaryTemplateItemRequest(
                    template_id=template_id,
                    name=name,
                    destination_wallet_id=destination_wallet_id,
                    calculation_mode=mode,
                    budget_category_id=category_id,
                    fixed_amount_minor=fixed_amount_minor,
                    display_order=display_order,
                )
            )
        except (ScribeError, ValueError, TypeError) as error:
            typer.echo(f"Could not add the salary item: {error}", err=True)
            raise typer.Exit(code=1) from error
    typer.echo(
        f"item_id={item.id} {item.name} mode={item.calculation_mode.value} "
        f"destination_wallet_id={item.destination_wallet_id} version={item.version}"
    )


@money_cli.command("salary-template-item-update")
def salary_template_item_update(
    item_id: Annotated[int, typer.Argument(min=1)],
    expected_version: Annotated[int, typer.Option("--expected-version")],
    name: Annotated[str | None, typer.Option("--name")] = None,
    destination_wallet_id: Annotated[
        int | None, typer.Option("--destination-wallet-id")
    ] = None,
    mode: Annotated[SalaryCalculationMode | None, typer.Option("--mode")] = None,
    fixed_amount_minor: Annotated[
        int | None,
        typer.Option("--fixed-amount-minor", help="Integer minor units."),
    ] = None,
    category_id: Annotated[int | None, typer.Option("--category-id")] = None,
    display_order: Annotated[int | None, typer.Option("--display-order")] = None,
) -> None:
    """Correct one destination; every omitted option keeps its current value."""
    with _money_runtime(_load_settings()) as runtime:
        try:
            current = _require_template_item(runtime, item_id)
            chosen_mode = mode if mode is not None else current.calculation_mode
            item = runtime.salary_templates.update_item(
                UpdateSalaryTemplateItemRequest(
                    template_item_id=item_id,
                    expected_version=expected_version,
                    name=name if name is not None else current.name,
                    destination_wallet_id=(
                        destination_wallet_id
                        if destination_wallet_id is not None
                        else current.destination_wallet_id
                    ),
                    calculation_mode=chosen_mode,
                    budget_category_id=(
                        category_id
                        if category_id is not None
                        else current.budget_category_id
                    ),
                    fixed_amount_minor=(
                        fixed_amount_minor
                        if fixed_amount_minor is not None
                        else (
                            current.fixed_amount_minor
                            if chosen_mode is SalaryCalculationMode.FIXED
                            else None
                        )
                    ),
                    display_order=(
                        display_order
                        if display_order is not None
                        else current.display_order
                    ),
                    is_active=current.is_active,
                )
            )
        except (ScribeError, ValueError, TypeError) as error:
            typer.echo(f"Could not update the salary item: {error}", err=True)
            raise typer.Exit(code=1) from error
    typer.echo(
        f"item_id={item.id} {item.name} mode={item.calculation_mode.value} "
        f"version={item.version}"
    )


@money_cli.command("salary-template-item-deactivate")
def salary_template_item_deactivate(
    item_id: Annotated[int, typer.Argument(min=1)],
    expected_version: Annotated[int, typer.Option("--expected-version")],
) -> None:
    """Stop using one destination without deleting the months that used it."""
    with _money_runtime(_load_settings()) as runtime:
        try:
            item = runtime.salary_templates.deactivate_item(
                DeactivateSalaryTemplateItemRequest(
                    template_item_id=item_id,
                    expected_version=expected_version,
                )
            )
        except (ScribeError, ValueError, TypeError) as error:
            typer.echo(f"Could not deactivate the salary item: {error}", err=True)
            raise typer.Exit(code=1) from error
    typer.echo(f"item_id={item.id} active={item.is_active} version={item.version}")
    typer.echo("Months already generated keep this item exactly as it was frozen.")


@money_cli.command("salary-receipt")
def salary_receipt(
    template_id: Annotated[int, typer.Option("--template-id", min=1)],
    amount_minor: Annotated[
        int,
        typer.Option("--amount-minor", help="Salary in integer minor units."),
    ],
    occurred_at: Annotated[
        str,
        typer.Option("--occurred-at", help="For example 2026-08-05T09:00."),
    ],
    token: Annotated[
        str,
        typer.Option(
            "--token",
            help="Required idempotency token; the same token records once only.",
        ),
    ],
    note: Annotated[str | None, typer.Option("--note")] = None,
) -> None:
    """Record the salary that actually arrived in the salary account."""
    settings = _load_settings()
    with _money_runtime(settings) as runtime:
        try:
            result = runtime.salary_receipt.execute(
                SalaryReceiptRequest(
                    template_id=template_id,
                    amount_minor=amount_minor,
                    occurred_at=_parse_local_instant(
                        occurred_at, settings.local_timezone
                    ),
                    client_token=token,
                    note=note,
                )
            )
        except (ScribeError, ValueError, TypeError) as error:
            typer.echo(f"Could not record the salary receipt: {error}", err=True)
            raise typer.Exit(code=1) from error
    typer.echo(
        f"event_id={result.event.id} entry_id={result.entry.id} "
        f"amount_minor={result.event.amount_minor} "
        f"{_format_money(result.event.amount_minor, result.event.currency)} "
        f"wallet_id={result.wallet.id} created={result.created}"
    )


@money_cli.command("salary-events")
def salary_events(
    template_id: Annotated[int, typer.Option("--template-id", min=1)],
) -> None:
    """Show the posted deposits this template could actually distribute."""
    with _money_runtime(_load_settings()) as runtime:
        try:
            rows = runtime.salary.eligible_salary_events(template_id)
        except ScribeError as error:
            typer.echo(f"Could not read salary events: {error}", err=True)
            raise typer.Exit(code=1) from error
    if not rows:
        typer.echo("No eligible salary events are available for this template.")
        return
    for row in rows:
        typer.echo(
            f"event_id={row.event.id} entry_id={row.entry_id} "
            f"amount_minor={row.event.amount_minor} "
            f"{_format_money(row.event.amount_minor, row.event.currency)} "
            f"occurred_at={row.event.occurred_at.isoformat()}"
        )


@money_cli.command("salary-generate")
def salary_generate(
    period: Annotated[str, typer.Argument(help="Allocation month written as yyyy-mm.")],
    template_id: Annotated[int, typer.Option("--template-id", min=1)],
    salary_event_id: Annotated[int, typer.Option("--salary-event-id", min=1)],
    manual: Annotated[
        list[str] | None,
        typer.Option(
            "--manual",
            help="Repeatable ITEM_ID=AMOUNT_MINOR in integer minor units.",
        ),
    ] = None,
) -> None:
    """Freeze one month's distribution of one salary."""
    try:
        year, month = parse_period_label(period)
        amounts = _parse_manual_amounts(manual or [])
    except (ScribeError, ValueError, TypeError) as error:
        typer.echo(f"Could not read the request: {error}", err=True)
        raise typer.Exit(code=1) from error
    with _money_runtime(_load_settings()) as runtime:
        try:
            result = runtime.salary_allocation.execute(
                GenerateSalaryRunRequest(
                    template_id=template_id,
                    year=year,
                    month=month,
                    salary_event_id=salary_event_id,
                    manual_amounts=amounts,
                )
            )
        except (ScribeError, ValueError, TypeError) as error:
            typer.echo(f"Could not generate the allocation: {error}", err=True)
            raise typer.Exit(code=1) from error
    _echo_run(result.run, created=result.created)


@money_cli.command("salary-regenerate")
def salary_regenerate(
    run_id: Annotated[int, typer.Argument(min=1)],
    expected_version: Annotated[int, typer.Option("--expected-version")],
    manual: Annotated[
        list[str] | None,
        typer.Option("--manual", help="Repeatable ITEM_ID=AMOUNT_MINOR."),
    ] = None,
) -> None:
    """Replace one draft with the next generation of the same month."""
    try:
        amounts = _parse_manual_amounts(manual or [])
    except (ValueError, TypeError) as error:
        typer.echo(f"Could not read the request: {error}", err=True)
        raise typer.Exit(code=1) from error
    with _money_runtime(_load_settings()) as runtime:
        try:
            result = runtime.salary_allocation.regenerate(
                RegenerateSalaryRunRequest(
                    run_id=run_id,
                    expected_version=expected_version,
                    manual_amounts=amounts,
                )
            )
        except (ScribeError, ValueError, TypeError) as error:
            typer.echo(f"Could not regenerate the allocation: {error}", err=True)
            raise typer.Exit(code=1) from error
    _echo_run(result.run, created=result.created)


@money_cli.command("salary-show")
def salary_show(
    run_id: Annotated[int, typer.Argument(min=1)],
) -> None:
    """Show one generation exactly as it was frozen."""
    with _money_runtime(_load_settings()) as runtime:
        try:
            view = runtime.salary.run(run_id)
        except ScribeError as error:
            typer.echo(f"Could not read the allocation: {error}", err=True)
            raise typer.Exit(code=1) from error
        if view is None:
            typer.echo("Salary allocation run was not found.", err=True)
            raise typer.Exit(code=1)
    run = view.run
    typer.echo(
        f"run_id={run.id} {view.label} generation={run.generation} "
        f"status={run.status.value} template_version={run.template_version} "
        f"version={run.version}"
    )
    typer.echo(
        f"salary={_format_money(run.salary_amount_minor, run.currency)} "
        f"planned={_format_money(run.planned_amount_minor, run.currency)} "
        f"remainder={_format_money(run.remainder_minor, run.currency)}"
    )
    typer.echo(
        f"source {view.source.account_name} · {view.source.wallet_name} "
        f"balance={_format_money(view.source.balance_minor, run.currency)} "
        f"reserved={_format_money(view.source.reserved_minor, run.currency)} "
        f"available={_format_money(view.source.available_minor, run.currency)}"
    )
    for entry in view.items:
        item = entry.item
        typer.echo(
            f"  item_id={item.id} {item.name_snapshot:<20} "
            f"{_format_money(item.amount_minor, item.currency)} "
            f"amount_minor={item.amount_minor} "
            f"mode={item.calculation_mode.value} "
            f"path={item.fulfillment_mode.value} "
            f"status={item.status.value} "
            f"destination={item.destination_account_name_snapshot}/"
            f"{item.destination_wallet_name_snapshot} "
            f"version={item.version}"
        )
    typer.echo(
        f"frozen subscription evidence: {len(view.evidence.items)} item(s), "
        f"total={_format_money(view.evidence.total_minor, run.currency)}"
    )
    for line in view.evidence.items:
        typer.echo(
            f"  subscription_id={line.subscription_id} {line.name:<20} "
            f"contribution_minor={line.contribution_minor} "
            f"wallet={line.funding_wallet_name} reason={line.reason.value}"
        )


@money_cli.command("salary-approve")
def salary_approve(
    run_id: Annotated[int, typer.Argument(min=1)],
    expected_version: Annotated[int, typer.Option("--expected-version")],
) -> None:
    """Approve one draft, reclassifying wallets and reserving transfers."""
    with _money_runtime(_load_settings()) as runtime:
        try:
            result = runtime.salary_approval.execute(
                ApproveSalaryRunRequest(
                    run_id=run_id, expected_version=expected_version
                )
            )
        except (ScribeError, ValueError, TypeError) as error:
            typer.echo(f"Could not approve the allocation: {error}", err=True)
            raise typer.Exit(code=1) from error
    entry_id = "-" if result.entry is None else result.entry.id
    _echo_run(result.run, created=result.created)
    typer.echo(f"reclassification_entry={entry_id}")
    typer.echo(
        "Nothing left the bank for a cross-account line; press 已轉帳 "
        "(salary-mark-transferred) after the real transfer."
    )


@money_cli.command("salary-cancel")
def salary_cancel(
    run_id: Annotated[int, typer.Argument(min=1)],
    expected_version: Annotated[int, typer.Option("--expected-version")],
) -> None:
    """Cancel one draft; its snapshot and every frozen line are kept."""
    with _money_runtime(_load_settings()) as runtime:
        try:
            run = runtime.salary_allocation.cancel(
                CancelSalaryDraftRequest(
                    run_id=run_id, expected_version=expected_version
                )
            )
        except (ScribeError, ValueError, TypeError) as error:
            typer.echo(f"Could not cancel the draft: {error}", err=True)
            raise typer.Exit(code=1) from error
    _echo_run(run, created=False)
    typer.echo("Nothing was posted or released; a draft reserves nothing.")


@money_cli.command("salary-mark-transferred")
def salary_mark_transferred(
    item_id: Annotated[int, typer.Argument(min=1)],
    expected_version: Annotated[int, typer.Option("--expected-version")],
    effective_at: Annotated[
        str,
        typer.Option(
            "--effective-at",
            help="When the transfer really happened, for example 2026-08-06T10:30.",
        ),
    ],
) -> None:
    """Record that one cross-account salary transfer really happened."""
    settings = _load_settings()
    with _money_runtime(settings) as runtime:
        try:
            result = runtime.salary_transfer.execute(
                MarkSalaryTransferredRequest(
                    run_item_id=item_id,
                    expected_version=expected_version,
                    effective_at=_parse_local_instant(
                        effective_at, settings.local_timezone
                    ),
                )
            )
        except (ScribeError, ValueError, TypeError) as error:
            typer.echo(f"Could not record the transfer: {error}", err=True)
            raise typer.Exit(code=1) from error
    typer.echo(
        f"item_id={result.item.id} status={result.item.status.value} "
        f"entry_id={result.entry.id} "
        f"amount_minor={result.item.amount_minor} "
        f"{_format_money(result.item.amount_minor, result.item.currency)} "
        f"created={result.created}"
    )
    _echo_run(result.run, created=False)


def _optional_instant(value: datetime | None) -> str:
    """Return an ISO instant, or a dash when nothing has happened yet."""
    return "-" if value is None else value.isoformat()


def _parse_local_date(value: str) -> date:
    """Return one ``yyyy-mm-dd`` calendar date, never a datetime."""
    parsed = date.fromisoformat(value.strip())
    return parsed


def _parse_wall_clock(value: str | None) -> time | None:
    """Return one ``HH:MM`` local policy time, or None for the configured one."""
    if value is None or not value.strip():
        return None
    parsed = time.fromisoformat(value.strip())
    return time(parsed.hour, parsed.minute)


def _parse_coverage(values: list[str]) -> tuple[ScheduleCommitmentLine, ...]:
    """Return ``commitment_id:amount_minor`` pairs the movement will settle."""
    lines: list[ScheduleCommitmentLine] = []
    for raw in values:
        identifier, separator, amount = raw.partition(":")
        if not separator:
            raise ValueError("coverage is written as commitment_id:amount_minor")
        lines.append(
            ScheduleCommitmentLine(
                funding_commitment_id=int(identifier.strip()),
                amount_minor=int(amount.strip()),
            )
        )
    return tuple(lines)


def _echo_schedule(schedule: ScheduledTransfer, *, created: bool) -> None:
    """Print one schedule without naming a bank confirmation."""
    typer.echo(
        f"schedule_id={schedule.id} purpose={schedule.purpose.value} "
        f"status={schedule.status.value} version={schedule.version} "
        f"created={created}"
    )
    typer.echo(
        f"amount_minor={schedule.amount_minor} "
        f"{_format_money(schedule.amount_minor, schedule.currency)} "
        f"due_at={schedule.due_at_utc.isoformat()} "
        f"local={schedule.scheduled_local_date.isoformat()} "
        f"{schedule.scheduled_local_time.isoformat(timespec='minutes')} "
        f"{schedule.timezone}"
    )
    typer.echo(
        f"processed_at={_optional_instant(schedule.processed_at)} "
        f"executed_entry={schedule.executed_entry_id or '-'} "
        f"reversal_entry={schedule.reversal_entry_id or '-'} "
        f"attempts={schedule.attempt_count}"
    )


@money_cli.command("clearing-show")
def clearing_show(
    limit: Annotated[int, typer.Option("--limit", min=1, max=100)] = 25,
) -> None:
    """Show clearing balances, schedulable reservations, and every schedule."""
    with _money_runtime(_load_settings()) as runtime:
        try:
            view = runtime.clearing.view(now=datetime.now(UTC), limit=limit)
        except ScribeError as error:
            typer.echo(f"Could not read the clearing hub: {error}", err=True)
            raise typer.Exit(code=1) from error
    typer.echo(f"due_now={view.due_now}")
    typer.echo("wallets:")
    for position in view.positions:
        typer.echo(
            f"  wallet_id={position.wallet_id} {position.account_name} · "
            f"{position.wallet_name} "
            f"actual={_format_money(position.balance_minor, position.currency)} "
            f"reserved={_format_money(position.reserved_minor, position.currency)} "
            f"available={_format_money(position.available_minor, position.currency)}"
        )
    typer.echo("schedulable reservations:")
    for group in view.groups:
        typer.echo(
            f"  {group.source_account_name} -> {group.settlement_account_name} "
            f"({group.currency}) mode={group.fulfillment_mode.value} "
            f"uncovered={_format_money(group.uncovered_minor, group.currency)}"
        )
        for row in group.rows:
            typer.echo(
                f"    commitment_id={row.commitment.id} "
                f"wallet={row.source_wallet_name} "
                f"required_minor={row.required_minor} "
                f"uncovered_minor={row.uncovered_minor}"
            )
    for caption, page in (
        ("upcoming", view.upcoming),
        ("failed", view.failed),
        ("assumed executed", view.executed),
        ("cancelled or reversed", view.closed),
    ):
        typer.echo(f"{caption} ({len(page.rows)} of {page.total}):")
        for entry in page.rows:
            warning = (
                f" over_covered_minor={entry.excess_minor}"
                if entry.has_refund_warning
                else ""
            )
            typer.echo(
                f"  schedule_id={entry.schedule.id} "
                f"purpose={entry.schedule.purpose.value} "
                f"status={entry.schedule.status.value} "
                f"amount_minor={entry.schedule.amount_minor} "
                f"due_at={entry.schedule.due_at_utc.isoformat()} "
                f"processed_at={_optional_instant(entry.schedule.processed_at)} "
                f"version={entry.schedule.version}{warning}"
            )
    typer.echo(
        "Executed schedules are assumed from the local policy, not confirmed by a bank."
    )


@money_cli.command("clearing-schedule")
def clearing_schedule(
    source_account_id: Annotated[int, typer.Option("--source-account-id", min=1)],
    source_wallet_id: Annotated[int, typer.Option("--source-wallet-id", min=1)],
    destination_account_id: Annotated[
        int, typer.Option("--destination-account-id", min=1)
    ],
    destination_wallet_id: Annotated[
        int, typer.Option("--destination-wallet-id", min=1)
    ],
    amount_minor: Annotated[int, typer.Option("--amount-minor", min=1)],
    scheduled_date: Annotated[str, typer.Option("--date", help="yyyy-mm-dd")],
    client_token: Annotated[str, typer.Option("--client-token")],
    cover: Annotated[
        list[str] | None,
        typer.Option("--cover", help="commitment_id:amount_minor, repeatable"),
    ] = None,
    scheduled_time: Annotated[
        str | None, typer.Option("--time", help="HH:MM in the schedule timezone")
    ] = None,
    allow_overdue: Annotated[bool, typer.Option("--allow-overdue")] = False,
) -> None:
    """Schedule one funding transfer into a clearing hub or another bank."""
    with _money_runtime(_load_settings()) as runtime:
        try:
            result = runtime.schedule_transfers.schedule_clearing(
                ScheduleClearingTransferRequest(
                    source_account_id=source_account_id,
                    source_wallet_id=source_wallet_id,
                    destination_account_id=destination_account_id,
                    destination_wallet_id=destination_wallet_id,
                    amount_minor=amount_minor,
                    scheduled_local_date=_parse_local_date(scheduled_date),
                    scheduled_local_time=_parse_wall_clock(scheduled_time),
                    client_token=client_token,
                    lines=_parse_coverage(cover or []),
                    allow_overdue=allow_overdue,
                )
            )
        except ScheduledTransferOverdueError as error:
            typer.echo(f"{error} Pass --allow-overdue to continue.", err=True)
            raise typer.Exit(code=1) from error
        except (ScribeError, ValueError, TypeError) as error:
            typer.echo(f"Could not schedule the transfer: {error}", err=True)
            raise typer.Exit(code=1) from error
    _echo_schedule(result.schedule, created=result.created)
    typer.echo("Nothing has moved yet; only the reserved amount changed.")


@money_cli.command("autopay-schedule")
def autopay_schedule(
    card_id: Annotated[int, typer.Option("--card-id", min=1)],
    source_wallet_id: Annotated[int, typer.Option("--source-wallet-id", min=1)],
    amount_minor: Annotated[int, typer.Option("--amount-minor", min=1)],
    scheduled_date: Annotated[str, typer.Option("--date", help="yyyy-mm-dd")],
    client_token: Annotated[str, typer.Option("--client-token")],
    cover: Annotated[
        list[str] | None,
        typer.Option("--cover", help="commitment_id:amount_minor, repeatable"),
    ] = None,
    scheduled_time: Annotated[
        str | None, typer.Option("--time", help="HH:MM in the schedule timezone")
    ] = None,
    allow_overdue: Annotated[bool, typer.Option("--allow-overdue")] = False,
) -> None:
    """Schedule one card payment out of that card's settlement account."""
    with _money_runtime(_load_settings()) as runtime:
        try:
            result = runtime.schedule_transfers.schedule_card_autopay(
                ScheduleCardAutopayRequest(
                    card_id=card_id,
                    source_wallet_id=source_wallet_id,
                    amount_minor=amount_minor,
                    scheduled_local_date=_parse_local_date(scheduled_date),
                    scheduled_local_time=_parse_wall_clock(scheduled_time),
                    client_token=client_token,
                    lines=_parse_coverage(cover or []),
                    allow_overdue=allow_overdue,
                )
            )
        except ScheduledTransferOverdueError as error:
            typer.echo(f"{error} Pass --allow-overdue to continue.", err=True)
            raise typer.Exit(code=1) from error
        except (ScribeError, ValueError, TypeError) as error:
            typer.echo(f"Could not schedule the card payment: {error}", err=True)
            raise typer.Exit(code=1) from error
    _echo_schedule(result.schedule, created=result.created)
    typer.echo("Nothing has moved yet; only the reserved amount changed.")


@money_cli.command("schedule-cancel")
def schedule_cancel(
    scheduled_transfer_id: Annotated[int, typer.Argument(min=1)],
    expected_version: Annotated[int, typer.Option("--expected-version")],
) -> None:
    """Withdraw one schedule that has not been executed."""
    with _money_runtime(_load_settings()) as runtime:
        try:
            schedule = runtime.schedule_transfers.cancel(
                ScheduledTransferAction(
                    scheduled_transfer_id=scheduled_transfer_id,
                    expected_version=expected_version,
                )
            )
        except (ScribeError, ValueError, TypeError) as error:
            typer.echo(f"Could not cancel the schedule: {error}", err=True)
            raise typer.Exit(code=1) from error
    _echo_schedule(schedule, created=False)
    typer.echo("The reservation this schedule held has been released.")


@money_cli.command("schedule-retry")
def schedule_retry(
    scheduled_transfer_id: Annotated[int, typer.Argument(min=1)],
    expected_version: Annotated[int, typer.Option("--expected-version")],
) -> None:
    """Put one failed schedule back in the queue immediately."""
    with _money_runtime(_load_settings()) as runtime:
        try:
            schedule = runtime.schedule_transfers.retry(
                ScheduledTransferAction(
                    scheduled_transfer_id=scheduled_transfer_id,
                    expected_version=expected_version,
                )
            )
        except (ScribeError, ValueError, TypeError) as error:
            typer.echo(f"Could not retry the schedule: {error}", err=True)
            raise typer.Exit(code=1) from error
    _echo_schedule(schedule, created=False)


@money_cli.command("schedule-reverse")
def schedule_reverse(
    scheduled_transfer_id: Annotated[int, typer.Argument(min=1)],
    expected_version: Annotated[int, typer.Option("--expected-version")],
) -> None:
    """Cancel an executed movement with its exact opposite entry."""
    with _money_runtime(_load_settings()) as runtime:
        try:
            result = runtime.schedule_transfers.reverse(
                ScheduledTransferAction(
                    scheduled_transfer_id=scheduled_transfer_id,
                    expected_version=expected_version,
                )
            )
        except (ScribeError, ValueError, TypeError) as error:
            typer.echo(f"Could not reverse the schedule: {error}", err=True)
            raise typer.Exit(code=1) from error
    _echo_schedule(result.schedule, created=False)
    entry_id = "-" if result.entry is None else result.entry.id
    typer.echo(f"reversal_entry={entry_id}")


@money_cli.command("run-due")
def run_due(
    apply: Annotated[
        bool, typer.Option("--apply", help="Execute instead of only listing.")
    ] = False,
    limit: Annotated[int, typer.Option("--limit", min=1, max=200)] = 20,
) -> None:
    """List, or with --apply execute, every scheduled movement now due."""
    with _money_runtime(_load_settings()) as runtime:
        try:
            result = (
                runtime.schedule_transfers.run_due(limit=limit)
                if apply
                else runtime.schedule_transfers.preview_due(limit=limit)
            )
        except (ScribeError, ValueError, TypeError) as error:
            typer.echo(f"Could not run due schedules: {error}", err=True)
            raise typer.Exit(code=1) from error
    mode = "apply" if apply else "dry-run"
    typer.echo(
        f"mode={mode} claimed={result.claimed} executed={result.executed} "
        f"retried={result.retried} failed={result.failed}"
    )
    for outcome in result.outcomes:
        typer.echo(
            f"  schedule_id={outcome.scheduled_transfer_id} "
            f"status={outcome.status.value} attempt={outcome.attempt} "
            f"entry_id={outcome.entry_id or '-'} "
            f"error_code={outcome.error_code or '-'}"
        )
    if not apply:
        typer.echo("Dry run: nothing was claimed, posted, or changed.")
    else:
        typer.echo(
            "Executed schedules are assumed from the local policy, not "
            "confirmed by a bank."
        )


def _parse_local_day(value: str | None, timezone: str) -> date | None:
    """Return one local calendar day named on the command line, or None."""
    if value is None or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError as error:
        raise ValueError("a day is written as yyyy-mm-dd") from error


@money_cli.command("daily")
def daily(
    day: Annotated[
        str | None,
        typer.Option("--day", help="A local calendar day, for example 2026-08-09."),
    ] = None,
    method: Annotated[
        str, typer.Option("--method", help="all, cash, or card.")
    ] = "all",
    page: Annotated[int, typer.Option("--page", min=1)] = 1,
) -> None:
    """Show one local day of cash and card spending, and its net totals."""
    settings = _load_settings()
    with _money_runtime(settings) as runtime:
        try:
            view = runtime.spending.daily(
                day=_parse_local_day(day, settings.local_timezone),
                payment_filter=method,
                page=page,
            )
        except (ScribeError, ValueError, TypeError) as error:
            typer.echo(f"Could not read the day: {error}", err=True)
            raise typer.Exit(code=1) from error
    typer.echo(
        f"day={view.day.isoformat()} filter={view.payment_filter} "
        f"rows={view.total_rows} page={view.page}/{view.page_count}"
    )
    for total in view.totals:
        typer.echo(
            f"total method={total.payment_method.value} "
            f"currency={total.currency} "
            f"net={_format_money(total.amount_minor, total.currency)} "
            f"net_minor={total.amount_minor}"
        )
    for row in view.rows:
        categories = ",".join(share.name for share in row.categories) or "-"
        typer.echo(
            f"  entry_id={row.journal_entry_id} event_id={row.financial_event_id} "
            f"at={row.effective_at.isoformat()} "
            f"method={row.payment_method.value} "
            f"amount={_format_money(row.amount_minor, row.currency)} "
            f"amount_minor={row.amount_minor} "
            f"merchant={row.merchant or '-'} categories={categories} "
            f"identity={row.card_display or row.wallet_display or '-'} "
            f"status={row.event_status.value}"
        )
    if not view.rows:
        typer.echo("No cash or card spending on this day.")


@money_cli.command("expense-add")
def expense_add(
    merchant: Annotated[str, typer.Option("--merchant")],
    amount_minor: Annotated[int, typer.Option("--amount-minor", min=1)],
    currency: Annotated[str, typer.Option("--currency")],
    funding_wallet_id: Annotated[int, typer.Option("--funding-wallet-id", min=1)],
    budget_category_id: Annotated[int, typer.Option("--budget-category-id", min=1)],
    client_token: Annotated[str, typer.Option("--client-token")],
    occurred_at: Annotated[
        str, typer.Option("--occurred-at", help="For example 2026-08-09T19:30.")
    ],
    payment_method: Annotated[
        str, typer.Option("--payment-method", help="cash or credit_card.")
    ] = "cash",
    card_id: Annotated[int | None, typer.Option("--card-id")] = None,
    note: Annotated[str | None, typer.Option("--note")] = None,
    clearing_wallet_id: Annotated[
        int | None,
        typer.Option(
            "--clearing-wallet-id",
            help=(
                "For dedicated card spending, move the source envelope into "
                "this clearing-hub wallet and exclude it from monthly spending."
            ),
        ),
    ] = None,
) -> None:
    """Record one cash or credit-card expense and post it in one commit."""
    settings = _load_settings()
    with _money_runtime(settings) as runtime:
        try:
            result = runtime.manual_expense.execute(
                ManualExpenseRequest(
                    payment_method=PaymentMethod(payment_method),
                    occurred_at=_parse_local_instant(
                        occurred_at, settings.local_timezone
                    ),
                    merchant=merchant,
                    amount_minor=amount_minor,
                    currency=currency,
                    funding_wallet_id=funding_wallet_id,
                    client_token=client_token,
                    budget_category_id=budget_category_id,
                    card_id=card_id,
                    note=note,
                    clearing_wallet_id=clearing_wallet_id,
                )
            )
        except (ScribeError, ValueError, TypeError) as error:
            typer.echo(f"Could not record the expense: {error}", err=True)
            raise typer.Exit(code=1) from error
    typer.echo(
        f"event_id={result.event.id} status={result.event.status.value} "
        f"entry_id={result.entry.id} allocations={len(result.allocations)} "
        f"commitments={len(result.commitments)}"
    )


@money_cli.command("bank-movement-add")
def bank_movement_add(
    kind: Annotated[str, typer.Option("--kind")],
    description: Annotated[str, typer.Option("--description")],
    amount_minor: Annotated[int, typer.Option("--amount-minor", min=1)],
    currency: Annotated[str, typer.Option("--currency")],
    wallet_id: Annotated[int, typer.Option("--wallet-id", min=1)],
    client_token: Annotated[str, typer.Option("--client-token")],
    occurred_at: Annotated[str, typer.Option("--occurred-at")],
    budget_category_id: Annotated[
        int | None, typer.Option("--budget-category-id")
    ] = None,
    note: Annotated[str | None, typer.Option("--note")] = None,
) -> None:
    """Record one bank movement that already happened, with its real shape."""
    settings = _load_settings()
    with _money_runtime(settings) as runtime:
        try:
            result = runtime.bank_movements.execute(
                BankMovementRequest(
                    kind=BankMovementKind(kind),
                    occurred_at=_parse_local_instant(
                        occurred_at, settings.local_timezone
                    ),
                    amount_minor=amount_minor,
                    currency=currency,
                    wallet_id=wallet_id,
                    description=description,
                    client_token=client_token,
                    budget_category_id=budget_category_id,
                    note=note,
                )
            )
        except (ScribeError, ValueError, TypeError) as error:
            typer.echo(f"Could not record the movement: {error}", err=True)
            raise typer.Exit(code=1) from error
    typer.echo(
        f"event_id={result.event.id} type={result.event.event_type.value} "
        f"status={result.event.status.value} entry_id={result.entry.id} "
        f"entry_type={result.entry.entry_type.value}"
    )


@money_cli.command("account-transfer")
def account_transfer(
    source_wallet_id: Annotated[int, typer.Option("--source-wallet-id", min=1)],
    destination_wallet_id: Annotated[
        int, typer.Option("--destination-wallet-id", min=1)
    ],
    amount_minor: Annotated[int, typer.Option("--amount-minor", min=1)],
    currency: Annotated[str, typer.Option("--currency")],
    description: Annotated[str, typer.Option("--description")],
    client_token: Annotated[str, typer.Option("--client-token")],
    occurred_at: Annotated[str, typer.Option("--occurred-at")],
    note: Annotated[str | None, typer.Option("--note")] = None,
) -> None:
    """Move money between two tracked wallets; this is never spending."""
    settings = _load_settings()
    with _money_runtime(settings) as runtime:
        try:
            result = runtime.bank_movements.transfer(
                AccountTransferRequest(
                    source_wallet_id=source_wallet_id,
                    destination_wallet_id=destination_wallet_id,
                    occurred_at=_parse_local_instant(
                        occurred_at, settings.local_timezone
                    ),
                    amount_minor=amount_minor,
                    currency=currency,
                    description=description,
                    client_token=client_token,
                    note=note,
                )
            )
        except (ScribeError, ValueError, TypeError) as error:
            typer.echo(f"Could not record the transfer: {error}", err=True)
            raise typer.Exit(code=1) from error
    typer.echo(
        f"event_id={result.event.id} entry_id={result.entry.id} "
        f"entry_type={result.entry.entry_type.value} "
        f"amount={_format_money(result.event.amount_minor, result.event.currency)}"
    )


@money_cli.command("refund-add")
def refund_add(
    original_event_id: Annotated[int, typer.Option("--original-event-id", min=1)],
    amount_minor: Annotated[int, typer.Option("--amount-minor", min=1)],
    client_token: Annotated[str, typer.Option("--client-token")],
    occurred_at: Annotated[str, typer.Option("--occurred-at")],
    note: Annotated[str | None, typer.Option("--note")] = None,
) -> None:
    """Record one refund of a posted expense, keeping every B3 refund rule."""
    settings = _load_settings()
    with _money_runtime(settings) as runtime:
        try:
            result = runtime.bank_movements.refund(
                ManualRefundRequest(
                    original_event_id=original_event_id,
                    occurred_at=_parse_local_instant(
                        occurred_at, settings.local_timezone
                    ),
                    amount_minor=amount_minor,
                    client_token=client_token,
                    note=note,
                )
            )
        except (ScribeError, ValueError, TypeError) as error:
            typer.echo(f"Could not record the refund: {error}", err=True)
            raise typer.Exit(code=1) from error
    typer.echo(
        f"event_id={result.event.id} entry_id={result.entry.id} "
        f"allocations={len(result.allocations)}"
    )


@money_cli.command("event-reverse")
def event_reverse(
    event_id: Annotated[int, typer.Argument(min=1)],
    expected_version: Annotated[int, typer.Option("--expected-version")],
) -> None:
    """Cancel one posted manual event without rewriting any evidence."""
    with _money_runtime(_load_settings()) as runtime:
        try:
            result = runtime.reverse_event.execute(
                ReverseEventRequest(
                    financial_event_id=event_id, expected_version=expected_version
                )
            )
        except (ScribeError, ValueError, TypeError) as error:
            typer.echo(f"Could not reverse the event: {error}", err=True)
            raise typer.Exit(code=1) from error
    typer.echo(
        f"event_id={result.event.id} status={result.event.status.value} "
        f"original_entry={result.original_entry.id} "
        f"reversal_entry={result.reversal_entry.id} "
        f"commitments={len(result.commitments)}"
    )


@money_cli.command("allocation-redo")
def allocation_redo(
    event_id: Annotated[int, typer.Argument(min=1)],
    expected_version: Annotated[int, typer.Option("--expected-version")],
) -> None:
    """Decide one posted event's wallet and category again.

    Reverses the entry that carried the previous decision, gives back what it
    reserved, and returns the event to the allocation queue. The event's own
    values are never touched, so this works for a Gmail-origin event where
    cancelling the event does not (ADR-B103).
    """
    with _money_runtime(_load_settings()) as runtime:
        try:
            result = runtime.redo_allocation.execute(
                RedoAllocationRequest(
                    financial_event_id=event_id, expected_version=expected_version
                )
            )
        except (ScribeError, ValueError, TypeError) as error:
            typer.echo(f"Could not redo the allocation: {error}", err=True)
            raise typer.Exit(code=1) from error
    typer.echo(
        f"event_id={result.event.id} status={result.event.status.value} "
        f"reversed_entry={result.reversed_entry.id} "
        f"reversal_entry={result.reversal_entry.id} "
        f"superseded_allocations={result.superseded_allocations} "
        f"released_commitments={len(result.released_commitments)}"
    )


@money_cli.command("source-links")
def source_links(
    limit: Annotated[int, typer.Option("--limit", min=1, max=100)] = 25,
) -> None:
    """List the source pairings still waiting for a decision."""
    with _money_runtime(_load_settings()) as runtime:
        view = runtime.spending.source_links(pending_limit=limit)
    typer.echo(f"pending={view.pending_total}")
    for row in view.pending:
        typer.echo(
            f"  link_id={row.link_id} score_bps={row.score_bps} "
            f"event_id={row.event.id} transaction_id={row.transaction_id} "
            f"amount={_format_money(row.event.amount_minor, row.event.currency)} "
            f"reasons={','.join(reason.value for reason in row.reasons) or '-'}"
        )
    if not view.pending:
        typer.echo("No pairing is waiting for a decision.")


@money_cli.command("source-link-confirm")
def source_link_confirm(link_id: Annotated[int, typer.Argument(min=1)]) -> None:
    """Accept one pairing after re-proving every mandatory gate."""
    with _money_runtime(_load_settings()) as runtime:
        try:
            link = runtime.source_review.confirm(link_id)
        except (ScribeError, ValueError, TypeError) as error:
            typer.echo(f"Could not confirm the pairing: {error}", err=True)
            raise typer.Exit(code=1) from error
    typer.echo(
        f"link_id={link.id} status={link.status.value} "
        f"event_id={link.financial_event_id} "
        f"transaction_id={link.transaction_id}"
    )
    typer.echo("Matching records provenance only; nothing was posted.")


@money_cli.command("source-link-reject")
def source_link_reject(link_id: Annotated[int, typer.Argument(min=1)]) -> None:
    """Record that one pairing is not the same money."""
    with _money_runtime(_load_settings()) as runtime:
        try:
            link = runtime.source_review.reject(link_id)
        except (ScribeError, ValueError, TypeError) as error:
            typer.echo(f"Could not reject the pairing: {error}", err=True)
            raise typer.Exit(code=1) from error
    typer.echo(f"link_id={link.id} status={link.status.value}")


@money_cli.command("source-link-separate")
def source_link_separate(
    transaction_id: Annotated[int, typer.Argument(min=1)],
) -> None:
    """Record one notification as its own event rather than as a duplicate."""
    with _money_runtime(_load_settings()) as runtime:
        try:
            result = runtime.source_review.declare_separate(transaction_id)
        except (ScribeError, ValueError, TypeError) as error:
            typer.echo(f"Could not record it separately: {error}", err=True)
            raise typer.Exit(code=1) from error
    typer.echo(
        f"transaction_id={result.staged.transaction_id} "
        f"event_id={result.staged.event_id} "
        f"rejected_links={len(result.rejected_link_ids)}"
    )
    typer.echo("The new event still needs allocation; nothing was posted.")


@money_cli.command("snapshot-add")
def snapshot_add(
    account_id: Annotated[int, typer.Option("--account-id", min=1)],
    observed_balance_minor: Annotated[
        int,
        typer.Option(
            "--observed-balance-minor",
            help="Signed: a real account can be empty or overdrawn.",
        ),
    ],
    observed_at: Annotated[str, typer.Option("--observed-at")],
    client_token: Annotated[str, typer.Option("--client-token")],
    note: Annotated[str | None, typer.Option("--note")] = None,
) -> None:
    """Write down what a bank account really held, without posting anything."""
    settings = _load_settings()
    with _money_runtime(settings) as runtime:
        try:
            snapshot = runtime.balance_snapshots.execute(
                BalanceSnapshotRequest(
                    financial_account_id=account_id,
                    observed_balance_minor=observed_balance_minor,
                    observed_at=_parse_local_instant(
                        observed_at, settings.local_timezone
                    ),
                    client_token=client_token,
                    note=note,
                )
            )
        except (ScribeError, ValueError, TypeError) as error:
            typer.echo(f"Could not record the observation: {error}", err=True)
            raise typer.Exit(code=1) from error
    typer.echo(
        f"snapshot_id={snapshot.id} account_id={snapshot.financial_account_id} "
        f"observed_minor={snapshot.observed_balance_minor} "
        f"observed_at={snapshot.observed_at.isoformat()} "
        f"source={snapshot.source.value}"
    )
    typer.echo("A snapshot is evidence only; no balance moved.")


@money_cli.command("reconcile-explain")
def reconcile_explain(
    account_id: Annotated[int, typer.Argument(min=1)],
) -> None:
    """Narrow one account's newest difference down to a few entries.

    Read-only. It reports the gap and the entries whose shape could produce
    it; the bank statement decides which one it actually was.
    """
    with _money_runtime(_load_settings()) as runtime:
        explanation = runtime.spending.explain_difference(account_id)
    snapshot = explanation.snapshot
    if snapshot is None:
        typer.echo(f"account_id={account_id} observed=- (no observation recorded yet)")
        return
    typer.echo(
        f"account_id={account_id} name={snapshot.account_display_name} "
        f"observed_at={snapshot.snapshot.observed_at.isoformat()} "
        f"observed_minor={snapshot.snapshot.observed_balance_minor} "
        f"ledger_as_of_minor={snapshot.ledger_balance_minor} "
        f"difference_minor={snapshot.difference_minor}"
    )
    if snapshot.difference_minor == 0:
        typer.echo("The ledger agreed with the bank at that instant.")
        return
    if not explanation.candidates:
        typer.echo(
            "No entry matches the gap on its own, and none was recorded after "
            "the observation. Compare the statement line by line from "
            f"{snapshot.snapshot.observed_at.date().isoformat()}."
        )
        return
    for candidate in explanation.candidates:
        typer.echo(
            f"  reason={candidate.reason} entry={candidate.journal_entry_id} "
            f"amount_minor={candidate.amount_minor} "
            f"effective_at={candidate.effective_at.isoformat()} "
            f"recorded_at={candidate.recorded_at.isoformat()} "
            f"event={candidate.financial_event_id or '-'} "
            f"merchant={candidate.merchant or '-'}"
        )


@money_cli.command("reconcile-show")
def reconcile_show(
    limit: Annotated[int, typer.Option("--limit", min=1, max=100)] = 25,
) -> None:
    """Show each account's newest observation and the gap it still has."""
    with _money_runtime(_load_settings()) as runtime:
        view = runtime.spending.reconciliation(snapshot_limit=limit)
    for row in view.accounts:
        latest = row.latest
        if latest is None:
            typer.echo(
                f"account_id={row.account.id} name={row.account.display_name} "
                f"balance_minor={row.balance_minor} observed=-"
            )
            continue
        typer.echo(
            f"account_id={row.account.id} name={row.account.display_name} "
            f"snapshot_id={latest.snapshot.id} "
            f"observed_minor={latest.snapshot.observed_balance_minor} "
            f"ledger_as_of_minor={latest.ledger_balance_minor} "
            f"difference_minor={latest.difference_minor}"
        )
    for observation in view.snapshots:
        typer.echo(
            f"  snapshot_id={observation.snapshot.id} "
            f"observed_at={observation.snapshot.observed_at.isoformat()} "
            f"difference_minor={observation.difference_minor}"
        )


@money_cli.command("reconciliation-adjust")
def reconciliation_adjust(
    snapshot_id: Annotated[int, typer.Argument(min=1)],
    expected_difference_minor: Annotated[
        int, typer.Option("--expected-difference-minor")
    ],
    reason: Annotated[str, typer.Option("--reason")],
    client_token: Annotated[str, typer.Option("--client-token")],
) -> None:
    """Accept one reasoned difference, recomputed as of the observation."""
    with _money_runtime(_load_settings()) as runtime:
        try:
            result = runtime.reconciliation.execute(
                ReconciliationAdjustmentRequest(
                    snapshot_id=snapshot_id,
                    expected_difference_minor=expected_difference_minor,
                    reason=reason,
                    client_token=client_token,
                )
            )
        except (ScribeError, ValueError, TypeError) as error:
            typer.echo(f"Could not adjust the difference: {error}", err=True)
            raise typer.Exit(code=1) from error
    typer.echo(
        f"snapshot_id={result.snapshot.id} event_id={result.event.id} "
        f"entry_id={result.entry.id} "
        f"difference_minor={result.difference_minor} "
        f"effective_at={result.entry.effective_at.isoformat()}"
    )

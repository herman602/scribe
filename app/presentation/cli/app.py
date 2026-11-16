"""Scribe command-line application."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
import uvicorn
from pydantic import ValidationError

from app.application.services.gmail_full_sync import FullSyncRequest, FullSyncResult
from app.application.services.gmail_incremental_sync import IncrementalSyncResult
from app.bootstrap import (
    build_full_sync_service,
    build_gmail_authentication_service,
    build_incremental_sync_service,
    build_processed_label_service,
    build_sender_rule_service,
    build_web_application,
)
from app.domain.enums import RunStatus, SenderMatchType
from app.domain.errors import DatabaseError, MigrationError, ScribeError
from app.domain.parsing import SecretValue
from app.infrastructure.config.doctor import CheckStatus, run_foundation_checks
from app.infrastructure.config.initializer import initialize_local_environment
from app.infrastructure.config.settings import AppSettings
from app.infrastructure.database.backup import (
    MANIFEST_SUFFIX,
    create_backup,
    list_backups,
    read_facts,
    restore_backup,
)
from app.infrastructure.database.base import Base
from app.infrastructure.database.locking import DatabaseInUseError
from app.infrastructure.database.migrations import (
    inspect_migration_status,
    upgrade_database,
)
from app.presentation.cli.money import money_cli
from app.presentation.cli.render import emit_json
from app.presentation.cli.services import service_cli

cli = typer.Typer(
    name="scribe",
    help="Local-first Gmail transaction notification tracker.",
    no_args_is_help=True,
    add_completion=False,
)
sender_rules_cli = typer.Typer(
    help="Manage flexible, account-scoped Gmail sender filters.",
    no_args_is_help=True,
)
cli.add_typer(sender_rules_cli, name="sender-rules")
cli.add_typer(money_cli, name="money")
cli.add_typer(service_cli, name="service")


def _load_settings(*, host: str | None = None, port: int | None = None) -> AppSettings:
    try:
        if host is not None and port is not None:
            return AppSettings(host=host, port=port)
        if host is not None:
            return AppSettings(host=host)
        if port is not None:
            return AppSettings(port=port)
        return AppSettings()
    except ValidationError as error:
        typer.echo(f"Configuration error:\n{error}", err=True)
        raise typer.Exit(code=2) from error


@cli.command("init")
def initialize() -> None:
    """Create private local files and migrate the SQLite database."""
    settings = _load_settings()
    result = initialize_local_environment(settings)
    for path in result.created_paths:
        typer.echo(f"created  {path}")
    for path in result.existing_paths:
        typer.echo(f"exists   {path}")
    try:
        migration = upgrade_database(settings)
    except MigrationError as error:
        typer.echo(f"Database initialization failed: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"database {settings.database_path} revision={migration.head_revision}")
    typer.echo("Local foundation initialized. Existing files were not overwritten.")


@cli.command()
def auth() -> None:
    """Authorize the exact Gmail scope required by local configuration."""
    settings = _load_settings()
    try:
        migration = inspect_migration_status(settings)
        if not migration.is_current:
            raise MigrationError("database is not at the current migration")
        result = build_gmail_authentication_service(settings).execute()
    except ScribeError as error:
        typer.echo(f"Gmail authorization failed: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Authorized Gmail account: {result.profile.email_address}")
    typer.echo(
        f"Mailbox profile: {result.profile.messages_total} messages, "
        f"{result.profile.threads_total} threads"
    )
    scope_name = "gmail.modify" if settings.gmail_labeling_enabled else "gmail.readonly"
    typer.echo(f"OAuth token stored locally with {scope_name} scope.")


@cli.command("label-processed")
def label_processed() -> None:
    """Apply the configured Gmail Label to locally processed messages."""
    settings = _load_settings()
    try:
        migration = inspect_migration_status(settings)
        if not migration.is_current:
            raise MigrationError("database is not at the current migration")
        result = build_processed_label_service(settings).backfill()
    except (ScribeError, ValueError) as error:
        typer.echo(f"Could not label processed Gmail messages: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(
        f"label={settings.gmail_processed_label} queued={result.queued_count} "
        f"succeeded={result.succeeded_count} pending={result.pending_count} "
        f"failed={result.failed_count}"
    )
    if result.error_message is not None:
        typer.echo(f"Gmail Label delivery paused: {result.error_message}", err=True)
        raise typer.Exit(code=1)


@sender_rules_cli.command("list")
def list_sender_rules(
    account: Annotated[
        str | None,
        typer.Option(help="Authorized Gmail address when more than one exists."),
    ] = None,
) -> None:
    """List sender rules without contacting Gmail."""
    settings = _load_settings()
    try:
        rules = build_sender_rule_service(settings).list_rules(account)
    except ScribeError as error:
        typer.echo(f"Could not list sender rules: {error}", err=True)
        raise typer.Exit(code=1) from error
    if not rules:
        typer.echo("No sender rules configured.")
        return
    for rule in rules:
        typer.echo(
            f"{rule.id:<4} {rule.institution_name:<12} "
            f"{rule.match_type.value:<14} {rule.match_value_normalized:<30} "
            f"enabled={str(rule.enabled).lower()} "
            f"gmail_query={str(rule.gmail_query_enabled).lower()} "
            f"pdf_passwords={len(rule.pdf_password_refs)}"
        )


@sender_rules_cli.command("add")
def add_sender_rule(
    institution_key: Annotated[
        str, typer.Option(help="Stable key such as fubon or cathay.")
    ],
    institution_name: Annotated[
        str, typer.Option(help="Display name such as 富邦 or 國泰.")
    ],
    match_value: Annotated[
        str,
        typer.Option(
            "--address",
            help="Exact sender address. Use --match-type domain_suffix for a domain.",
        ),
    ],
    match_type: Annotated[
        SenderMatchType,
        typer.Option(help="exact_address (preferred) or domain_suffix."),
    ] = SenderMatchType.EXACT_ADDRESS,
    account: Annotated[
        str | None,
        typer.Option(help="Authorized Gmail address when more than one exists."),
    ] = None,
    parser_name: Annotated[
        str | None,
        typer.Option(help="Optional future parser binding."),
    ] = None,
    priority: Annotated[
        int, typer.Option(min=0, help="Lower values match first.")
    ] = 100,
    gmail_query_enabled: Annotated[
        bool,
        typer.Option(
            "--gmail-query/--local-only",
            help="Use this rule for Gmail discovery or only local thread gating.",
        ),
    ] = True,
) -> None:
    """Create or update one exact-address/domain sender rule."""
    settings = _load_settings()
    try:
        rule = build_sender_rule_service(settings).add_or_update(
            institution_key=institution_key,
            institution_name=institution_name,
            match_type=match_type,
            match_value=match_value,
            account_email=account,
            parser_name=parser_name,
            priority=priority,
            gmail_query_enabled=gmail_query_enabled,
        )
    except (ScribeError, ValueError) as error:
        typer.echo(f"Could not save sender rule: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(
        f"Sender rule {rule.id} saved: {rule.institution_name} "
        f"{rule.match_type.value}={rule.match_value_normalized}"
    )


@sender_rules_cli.command("disable")
def disable_sender_rule(
    rule_id: Annotated[int, typer.Argument(min=1)],
    account: Annotated[
        str | None,
        typer.Option(help="Authorized Gmail address when more than one exists."),
    ] = None,
) -> None:
    """Disable one rule while retaining its local audit identity."""
    settings = _load_settings()
    try:
        rule = build_sender_rule_service(settings).disable(
            rule_id, account_email=account
        )
    except ScribeError as error:
        typer.echo(f"Could not disable sender rule: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Sender rule {rule.id} disabled.")


@sender_rules_cli.command("add-pdf-password")
def add_pdf_password(
    rule_id: Annotated[int, typer.Argument(min=1)],
    account: Annotated[
        str | None,
        typer.Option(help="Authorized Gmail address when more than one exists."),
    ] = None,
) -> None:
    """Prompt privately and add one PDF password to the OS credential store."""
    password = typer.prompt(
        "PDF password",
        hide_input=True,
        confirmation_prompt=True,
    )
    try:
        rule = build_sender_rule_service(_load_settings()).add_pdf_password(
            rule_id,
            SecretValue(password),
            account_email=account,
        )
    except (ScribeError, ValueError) as error:
        typer.echo(f"Could not save PDF password: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(
        f"PDF password stored in the OS credential store for sender rule {rule.id}."
    )


@sender_rules_cli.command("clear-pdf-passwords")
def clear_pdf_passwords(
    rule_id: Annotated[int, typer.Argument(min=1)],
    account: Annotated[
        str | None,
        typer.Option(help="Authorized Gmail address when more than one exists."),
    ] = None,
) -> None:
    """Delete every locally stored PDF password for one sender rule."""
    try:
        rule = build_sender_rule_service(_load_settings()).clear_pdf_passwords(
            rule_id, account_email=account
        )
    except ScribeError as error:
        typer.echo(f"Could not clear PDF passwords: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"PDF passwords cleared for sender rule {rule.id}.")


@cli.command()
def sync(
    full: Annotated[
        bool,
        typer.Option("--full", help="Run resumable full/backfill synchronization."),
    ] = False,
    query: Annotated[
        str | None,
        typer.Option(help="Additional Gmail query, for example newer_than:2y."),
    ] = None,
    all_senders: Annotated[
        bool,
        typer.Option(
            "--all-senders",
            help="Explicitly scan the whole query scope without sender filtering.",
        ),
    ] = False,
    include_spam_trash: Annotated[
        bool,
        typer.Option(help="Include Spam and Trash in Gmail discovery."),
    ] = False,
    restart: Annotated[
        bool,
        typer.Option(
            "--restart",
            help="Explicitly supersede an unfinished run and start listing again.",
        ),
    ] = False,
) -> None:
    """Synchronize Gmail incrementally, or run an explicit full backfill."""
    if not full and any((query, all_senders, include_spam_trash, restart)):
        typer.echo(
            "--query, --all-senders, --include-spam-trash, and --restart "
            "apply only to sync --full.",
            err=True,
        )
        raise typer.Exit(code=2)
    settings = _load_settings()
    result: FullSyncResult | IncrementalSyncResult
    try:
        migration = inspect_migration_status(settings)
        if not migration.is_current:
            raise MigrationError("database is not at the current migration")
        if full:
            effective_query = (
                query if query is not None else settings.default_gmail_query
            )
            result = build_full_sync_service(settings).execute(
                FullSyncRequest(
                    custom_query=effective_query,
                    all_senders=all_senders,
                    include_spam_trash=include_spam_trash,
                    restart=restart,
                )
            )
        else:
            result = build_incremental_sync_service(settings).execute()
    except KeyboardInterrupt as error:
        typer.echo(
            "Sync interrupted. Page and message checkpoints were kept; rerun "
            "the same command to resume.",
            err=True,
        )
        raise typer.Exit(code=130) from error
    except ScribeError as error:
        typer.echo(f"Gmail sync failed: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(
        f"run_id={result.run_id} status={result.status.value} mode={result.mode.value}"
    )
    typer.echo(
        f"pages={result.pages_processed} discovered={result.discovered_count} "
        f"new={result.new_count} updated={result.updated_count} "
        f"deleted={result.deleted_count} "
        f"failed={result.failed_count} "
        f"parser_failed={result.parser_failed_count}"
    )
    if full:
        typer.echo(
            "coverage="
            + (
                "message-level whole query scope"
                if all_senders
                else "sender-filtered threads expanded message by message"
            )
        )
    else:
        typer.echo(
            "coverage=mailbox history events processed by Gmail message ID; "
            "unknown senders metadata-gated"
        )
    if result.status is RunStatus.PARTIAL:
        raise typer.Exit(code=1)


@cli.command()
def doctor(
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Emit the same checks as one JSON document."),
    ] = False,
) -> None:
    """Check local configuration, filesystem, and database health."""
    settings = _load_settings()
    checks = run_foundation_checks(settings)
    if as_json:
        emit_json({"checks": list(checks)})
    else:
        for check in checks:
            typer.echo(f"{check.status.value:<4}  {check.name}: {check.detail}")
    if any(check.status is CheckStatus.FAIL for check in checks):
        raise typer.Exit(code=1)


@cli.command()
def schema(
    table: Annotated[
        str | None,
        typer.Option("--table", help="Describe one table instead of all of them."),
    ] = None,
) -> None:
    """Describe the local tables and columns, as JSON.

    Read from the mapped metadata rather than from a database file, so it
    answers before anything is migrated and always describes the code that is
    actually running (ADR-B095).
    """
    tables = {
        name: {
            "columns": [
                {
                    "name": column.name,
                    "type": str(column.type),
                    "nullable": column.nullable,
                    "primary_key": column.primary_key,
                }
                for column in mapped.columns
            ],
        }
        for name, mapped in sorted(Base.metadata.tables.items())
        if table is None or name == table
    }
    if table is not None and not tables:
        typer.echo(f"No table named {table}.", err=True)
        raise typer.Exit(code=1)
    emit_json({"tables": tables})


@cli.command()
def backup(
    directory: Annotated[
        Path | None,
        typer.Option(
            "--directory", help="Where to write it; defaults to data/backups."
        ),
    ] = None,
) -> None:
    """Take a verified backup of the local database, with a manifest.

    Uses SQLite's online backup API, so it is safe while the service is
    serving and never omits the write-ahead log (ADR-B098).
    """
    settings = _load_settings()
    target = directory or (settings.data_dir / "backups")
    try:
        result = create_backup(settings.database_path, target)
    except (DatabaseError, OSError) as error:
        typer.echo(f"Could not back up: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"backup={result.path}")
    typer.echo(f"manifest={result.manifest_path}")
    typer.echo(
        f"revision={result.copy.revision} integrity={result.copy.integrity} "
        f"foreign_key_violations={result.copy.foreign_key_violations} "
        f"verified={result.verified}"
    )
    if not result.verified:
        typer.echo(
            "The copy does not match the live database. Do not rely on it.",
            err=True,
        )
        raise typer.Exit(code=1)


@cli.command("backup-list")
def backup_list(
    directory: Annotated[Path | None, typer.Option("--directory")] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List local backups, newest first."""
    settings = _load_settings()
    target = directory or (settings.data_dir / "backups")
    items = [
        {
            "path": str(item),
            "bytes": item.stat().st_size,
            "modified_at": datetime.fromtimestamp(
                item.stat().st_mtime, tz=UTC
            ).isoformat(),
            "has_manifest": item.with_suffix(MANIFEST_SUFFIX).is_file(),
        }
        for item in list_backups(target)
    ]
    if as_json:
        emit_json({"directory": str(target), "backups": items})
        return
    if not items:
        typer.echo(f"No backups in {target}.")
        return
    for item in items:
        manifest = "" if item["has_manifest"] else "  (no manifest)"
        typer.echo(f"{item['modified_at']}  {item['path']}{manifest}")


@cli.command()
def restore(
    backup_path: Annotated[Path, typer.Argument(help="Backup file to restore.")],
    apply: Annotated[
        bool,
        typer.Option("--apply/--dry-run", help="Preview by default."),
    ] = False,
    confirm: Annotated[
        str | None,
        typer.Option(
            "--confirm",
            help="Type the word REPLACE to overwrite the live database.",
        ),
    ] = None,
) -> None:
    """Replace the live database with a backup, keeping a rollback copy.

    This is the only command that discards the current database, so a flag
    alone is not enough: it previews unless `--apply` is given, and overwrites
    only when `--confirm REPLACE` is typed as well (ADR-B098). Stop the local
    service first; it holds the file open.
    """
    settings = _load_settings()
    try:
        incoming = read_facts(backup_path)
        current = (
            read_facts(settings.database_path)
            if settings.database_path.is_file()
            else None
        )
    except DatabaseError as error:
        typer.echo(f"Could not read: {error}", err=True)
        raise typer.Exit(code=1) from error

    typer.echo(f"backup   revision={incoming.revision} integrity={incoming.integrity}")
    if current is not None:
        typer.echo(
            f"current  revision={current.revision} integrity={current.integrity}"
        )
        for table, count in sorted(incoming.counts.items()):
            live_count = current.counts.get(table, 0)
            if live_count != count:
                typer.echo(f"  {table}: {live_count} -> {count}")
    if not apply:
        typer.echo("Nothing was replaced. Rerun with --apply --confirm REPLACE.")
        return
    if confirm != "REPLACE":
        typer.echo(
            "Refusing to overwrite the live database without --confirm REPLACE.",
            err=True,
        )
        raise typer.Exit(code=1)
    try:
        rollback = restore_backup(
            backup_path,
            settings.database_path,
            settings.data_dir / "backups",
            expected_revision=inspect_migration_status(settings).head_revision,
        )
    except DatabaseInUseError as error:
        typer.echo(f"{error}", err=True)
        raise typer.Exit(code=1) from error
    except (DatabaseError, OSError) as error:
        typer.echo(f"Could not restore: {error}", err=True)
        raise typer.Exit(code=1) from error
    restored = read_facts(settings.database_path)
    typer.echo(f"restored revision={restored.revision} integrity={restored.integrity}")
    typer.echo(f"rollback_copy={rollback}")
    if not (restored.is_sound and restored.journal_balances):
        typer.echo("The restored database failed its own checks.", err=True)
        raise typer.Exit(code=1)


@cli.command()
def serve(
    host: Annotated[
        str | None,
        typer.Option(help="Validated bind host override for this invocation."),
    ] = None,
    port: Annotated[
        int | None,
        typer.Option(min=1, max=65535, help="TCP port override."),
    ] = None,
) -> None:
    """Start the dashboard on its validated local or LAN listener."""
    settings = _load_settings(host=host, port=port)
    application = build_web_application(settings)
    uvicorn.run(
        application,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


def main() -> None:
    """Run the Typer CLI."""
    cli()

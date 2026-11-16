"""Resumable, sender-filtered Gmail full synchronization."""

from __future__ import annotations

import hashlib
import json
import logging
import traceback
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from app.application.services.email_processing import PrepareEmailProcessing
from app.application.services.financial_event_intake import FinancialEventIntake
from app.application.services.gmail_labeling import ManageProcessedGmailLabels
from app.application.services.gmail_message_persistence import PersistGmailMessage
from app.application.services.sync_lease import SyncRunLease
from app.domain.enums import (
    BodyRetentionPolicy,
    FullSyncStatus,
    RunStatus,
    SyncItemOperation,
    SyncMode,
)
from app.domain.errors import (
    EmailDecodeError,
    GmailMessageFetchError,
    ScribeError,
    SyncAlreadyRunningError,
    SyncCancelledError,
    SyncInterruptedError,
)
from app.domain.gmail import GmailFullSyncGateway, MimeDecoder
from app.domain.models import SenderRule, SyncRun
from app.domain.ports.repositories import UnitOfWork
from app.domain.sender_rules import compile_gmail_query, match_sender
from app.infrastructure.observability.logging import (
    LOGGER_NAME,
    log_context,
)

type UnitOfWorkFactory = Callable[[], UnitOfWork]
type Clock = Callable[[], datetime]

_QUERY_SEMANTICS_VERSION = 1
_SYNC_SCOPE_SETTING_PREFIX = "sync_scope."
_LOGGER = logging.getLogger(LOGGER_NAME)


@dataclass(frozen=True)
class FullSyncRequest:
    """User-selected scope for one full or targeted backfill sync."""

    custom_query: str | None = None
    all_senders: bool = False
    include_spam_trash: bool = False
    restart: bool = False
    mode: SyncMode = SyncMode.FULL


@dataclass(frozen=True)
class FullSyncResult:
    """Safe completion summary for CLI and later Dashboard use."""

    run_id: str
    status: RunStatus
    query: str | None
    thread_expansion_enabled: bool
    pages_processed: int
    discovered_count: int
    new_count: int
    updated_count: int
    failed_count: int
    baseline_history_id: str
    parser_failed_count: int = 0
    deleted_count: int = 0
    mode: SyncMode = SyncMode.FULL


class FullGmailSync:
    """List, expand, decode, and persist Gmail messages idempotently."""

    def __init__(
        self,
        gateway: GmailFullSyncGateway,
        mime_decoder: MimeDecoder,
        email_processing: PrepareEmailProcessing,
        unit_of_work_factory: UnitOfWorkFactory,
        *,
        max_results: int,
        work_batch_size: int,
        body_retention_policy: BodyRetentionPolicy,
        body_summary_chars: int = 1000,
        lease_seconds: int = 300,
        clock: Clock = lambda: datetime.now(UTC),
        processed_label_name: str | None = None,
        label_manager: ManageProcessedGmailLabels | None = None,
        intake: FinancialEventIntake | None = None,
    ) -> None:
        if not 1 <= max_results <= 500:
            raise ValueError("Gmail page size must be between 1 and 500")
        if work_batch_size < 1 or body_summary_chars < 1:
            raise ValueError("sync batch and summary sizes must be positive")
        self._gateway = gateway
        self._mime_decoder = mime_decoder
        self._unit_of_work_factory = unit_of_work_factory
        self._max_results = max_results
        self._work_batch_size = work_batch_size
        self._lease_seconds = lease_seconds
        self._clock = clock
        self._label_manager = label_manager
        self._message_persistence = PersistGmailMessage(
            email_processing,
            unit_of_work_factory,
            body_retention_policy=body_retention_policy,
            body_summary_chars=body_summary_chars,
            processed_label_name=processed_label_name,
            intake=intake,
        )

    def execute(self, request: FullSyncRequest) -> FullSyncResult:
        """Run or resume an exact-scope full synchronization."""
        profile = self._gateway.get_profile()
        run, rules, fingerprint = self._prepare_run(
            profile.email_address,
            profile.history_id,
            request,
        )
        lease = SyncRunLease(
            self._unit_of_work_factory,
            run.id,
            lease_seconds=self._lease_seconds,
            clock=self._clock,
        )
        lease.acquire()
        with log_context(sync_run_id=run.id):
            _LOGGER.info(
                "Full sync started account=%s mode=%s thread_expansion=%s",
                profile.email_address,
                request.mode.value,
                not request.all_senders,
            )
            try:
                lease.checkpoint()
                run = self._discover_messages(run, rules, request, lease)
                self._process_messages(run, rules, request, lease)
                result = self._complete(run, fingerprint, request)
            except SyncCancelledError as error:
                result = self._cancel(run, request, error)
            except SyncAlreadyRunningError:
                raise
            except ScribeError as error:
                self._record_run_error(run.id, error)
                raise
            except KeyboardInterrupt:
                self._record_run_error(
                    run.id,
                    SyncInterruptedError(
                        "Sync was interrupted; rerun the same scope to resume"
                    ),
                    include_trace=False,
                )
                raise
            finally:
                lease.release()
            if self._label_manager is not None:
                label_result = self._label_manager.flush(run.gmail_account_id)
                if label_result.error_message is not None:
                    _LOGGER.warning(
                        "Sync completed but processed Gmail Labels remain pending"
                    )
            _LOGGER.info(
                "Full sync completed status=%s pages=%d discovered=%d "
                "new=%d updated=%d failed=%d parser_failed=%d",
                result.status.value,
                result.pages_processed,
                result.discovered_count,
                result.new_count,
                result.updated_count,
                result.failed_count,
                result.parser_failed_count,
            )
            return result

    def _record_run_error(
        self,
        run_id: str,
        error: ScribeError,
        *,
        include_trace: bool = True,
    ) -> None:
        with self._unit_of_work_factory() as unit:
            unit.sync_runs.record_error(
                run_id,
                error_code=type(error).__name__,
                error_message=str(error),
                debug_trace=traceback.format_exc(limit=20) if include_trace else None,
            )
            unit.commit()
        _LOGGER.error(
            "Full sync paused with resumable error error_code=%s",
            type(error).__name__,
        )

    def _prepare_run(
        self,
        profile_email: str,
        baseline_history_id: str,
        request: FullSyncRequest,
    ) -> tuple[SyncRun, tuple[SenderRule, ...], str]:
        with self._unit_of_work_factory() as unit:
            account = unit.accounts.get_or_create(profile_email)
            rules = unit.sender_rules.list_enabled(account.id)
            query = compile_gmail_query(
                rules,
                custom_query=request.custom_query,
                all_senders=request.all_senders,
            )
            fingerprint = _query_fingerprint(query, rules, request)
            state = unit.sync_states.get_or_create(account.id)
            active = unit.sync_runs.find_any_running(account.id)
            if active is not None and request.restart:
                unit.sync_runs.finish(
                    active.id,
                    status=RunStatus.FAILED,
                    end_history_id=None,
                    error_code="user_restarted",
                    error_message="Run was superseded by an explicit restart",
                )
                active = None
            if active is not None:
                if (
                    state.query_fingerprint != fingerprint
                    or active.mode is not request.mode
                    or active.query != query
                ):
                    raise SyncAlreadyRunningError(
                        "An unfinished sync exists with a different scope. "
                        "Resume it with the original options or pass --restart "
                        "to supersede its checkpoint."
                    )
                run = active
            else:
                run = unit.sync_runs.create(
                    account_id=account.id,
                    mode=request.mode,
                    query=query,
                    start_history_id=baseline_history_id,
                )
                unit.sync_states.save(
                    replace(
                        state,
                        baseline_history_id=baseline_history_id,
                        initial_query=query,
                        query_fingerprint=fingerprint,
                        query_semantics_version=_QUERY_SEMANTICS_VERSION,
                        full_sync_next_page_token=None,
                        full_sync_status=FullSyncStatus.RUNNING,
                        reconciliation_required=False,
                    ),
                    expected_version=state.version,
                )
            unit.commit()
        return run, rules, fingerprint

    def _discover_messages(
        self,
        run: SyncRun,
        rules: tuple[SenderRule, ...],
        request: FullSyncRequest,
        lease: SyncRunLease,
    ) -> SyncRun:
        expanded_threads: set[str] = set()
        checkpoint_restarted = False
        while run.pages_processed == 0 or run.next_page_token is not None:
            lease.checkpoint()
            try:
                page = self._gateway.list_messages(
                    query=run.query,
                    page_token=run.next_page_token,
                    max_results=self._max_results,
                    include_spam_trash=request.include_spam_trash,
                )
            except GmailMessageFetchError as error:
                if (
                    run.next_page_token is None
                    or error.retryable
                    or checkpoint_restarted
                ):
                    raise
                with self._unit_of_work_factory() as unit:
                    run = unit.sync_runs.restart_listing(run.id)
                    state = unit.sync_states.get_or_create(run.gmail_account_id)
                    unit.sync_states.save(
                        replace(state, full_sync_next_page_token=None),
                        expected_version=state.version,
                    )
                    unit.commit()
                checkpoint_restarted = True
                expanded_threads.clear()
                _LOGGER.warning(
                    "Saved Gmail page token was rejected; restarting listing "
                    "from page one with message-ID deduplication"
                )
                continue
            message_ids = {item.message_id for item in page.messages}
            if not request.all_senders:
                for thread_id in {
                    item.thread_id for item in page.messages
                } - expanded_threads:
                    try:
                        thread = self._gateway.get_thread(thread_id)
                    except GmailMessageFetchError as error:
                        if error.retryable:
                            raise
                        expanded_threads.add(thread_id)
                        _LOGGER.warning(
                            "Relevant thread disappeared before expansion thread_id=%s",
                            thread_id,
                        )
                        continue
                    expanded_threads.add(thread_id)
                    message_ids.update(
                        item.message_id
                        for item in thread.messages
                        if match_sender(item.sender_header, rules) is not None
                    )
            with self._unit_of_work_factory() as unit:
                inserted = unit.sync_runs.enqueue_message_ids(
                    run.id, tuple(message_ids)
                )
                run = unit.sync_runs.checkpoint_page(
                    run.id,
                    next_page_token=page.next_page_token,
                )
                state = unit.sync_states.get_or_create(run.gmail_account_id)
                unit.sync_states.save(
                    replace(
                        state,
                        full_sync_next_page_token=page.next_page_token,
                        full_sync_status=FullSyncStatus.RUNNING,
                    ),
                    expected_version=state.version,
                )
                unit.commit()
            _LOGGER.info(
                "Gmail list page checkpointed page=%d listed=%d enqueued=%d "
                "has_next=%s",
                run.pages_processed,
                len(page.messages),
                inserted,
                page.next_page_token is not None,
            )
        if request.mode is SyncMode.RECONCILIATION:
            lease.checkpoint()
            self._enqueue_reconciliation_deletions(run, rules, request)
            with self._unit_of_work_factory() as unit:
                refreshed = unit.sync_runs.get(run.id)
            if refreshed is None:
                raise RuntimeError("reconciliation run disappeared")
            run = refreshed
        return run

    def _enqueue_reconciliation_deletions(
        self,
        run: SyncRun,
        rules: tuple[SenderRule, ...],
        request: FullSyncRequest,
    ) -> None:
        with self._unit_of_work_factory() as unit:
            tracked = set(
                unit.emails.list_active_gmail_ids(
                    run.gmail_account_id,
                    sender_rule_ids=(
                        None
                        if request.all_senders
                        else tuple(rule.id for rule in rules)
                    ),
                )
            )
            discovered = set(unit.sync_runs.list_message_ids(run.id))
            missing = tracked - discovered
            unit.sync_runs.enqueue_changes(
                run.id,
                tuple(
                    (message_id, SyncItemOperation.DELETE)
                    for message_id in sorted(missing)
                ),
            )
            unit.commit()
        if missing:
            _LOGGER.info(
                "Reconciliation queued missing local sources count=%d",
                len(missing),
            )

    def _process_messages(
        self,
        run: SyncRun,
        rules: tuple[SenderRule, ...],
        request: FullSyncRequest,
        lease: SyncRunLease,
    ) -> None:
        while True:
            lease.checkpoint()
            with self._unit_of_work_factory() as unit:
                items = unit.sync_runs.list_processable(
                    run.id, limit=self._work_batch_size
                )
            if not items:
                return
            for item in items:
                lease.checkpoint()
                with log_context(email_message_id=item.gmail_message_id):
                    if item.operation is SyncItemOperation.DELETE:
                        with self._unit_of_work_factory() as unit:
                            deleted = unit.emails.mark_source_deleted(
                                run.gmail_account_id,
                                item.gmail_message_id,
                                self._clock(),
                            )
                            if deleted:
                                unit.sync_runs.mark_succeeded(
                                    item.id,
                                    created=False,
                                    deleted=True,
                                )
                            else:
                                unit.sync_runs.mark_skipped(item.id)
                            unit.commit()
                        continue
                    try:
                        raw = self._gateway.get_message(item.gmail_message_id)
                        decoded = self._mime_decoder.decode(
                            raw, self._gateway.get_attachment
                        )
                        rule = match_sender(decoded.sender, rules)
                        if not request.all_senders and rule is None:
                            with self._unit_of_work_factory() as unit:
                                unit.sync_runs.mark_skipped(item.id)
                                unit.commit()
                            _LOGGER.info("Message skipped by local sender verification")
                            continue
                        self._message_persistence.execute(
                            run,
                            item.id,
                            decoded,
                            rule,
                        )
                    except GmailMessageFetchError as error:
                        if error.retryable:
                            raise
                        with self._unit_of_work_factory() as unit:
                            unit.sync_runs.mark_failed(
                                item.id,
                                error_code=type(error).__name__,
                                error_message=str(error),
                                retryable=False,
                            )
                            unit.commit()
                        _LOGGER.warning(
                            "Message processing failed error_code=%s retryable=false",
                            type(error).__name__,
                        )
                    except EmailDecodeError as error:
                        with self._unit_of_work_factory() as unit:
                            unit.sync_runs.mark_failed(
                                item.id,
                                error_code=type(error).__name__,
                                error_message=str(error),
                                retryable=False,
                            )
                            unit.commit()
                        _LOGGER.warning(
                            "Message processing failed error_code=%s retryable=false",
                            type(error).__name__,
                        )

    def _cancel(
        self,
        run: SyncRun,
        request: FullSyncRequest,
        error: SyncCancelledError,
    ) -> FullSyncResult:
        with self._unit_of_work_factory() as unit:
            current = unit.sync_runs.get(run.id)
            if current is None:
                raise RuntimeError("sync run disappeared during cancellation")
            failed = unit.sync_runs.failed_count(run.id)
            finished = unit.sync_runs.finish(
                run.id,
                status=RunStatus.CANCELLED,
                end_history_id=current.end_history_id,
                error_code=type(error).__name__,
                error_message=str(error),
            )
            state = unit.sync_states.get_or_create(run.gmail_account_id)
            unit.sync_states.save(
                replace(
                    state,
                    full_sync_status=FullSyncStatus.FAILED,
                ),
                expected_version=state.version,
            )
            unit.commit()
        if finished.start_history_id is None:
            raise RuntimeError("cancelled full sync has no baseline history ID")
        _LOGGER.info("Full sync cancelled at a safe checkpoint")
        return FullSyncResult(
            run_id=finished.id,
            status=finished.status,
            query=finished.query,
            thread_expansion_enabled=not request.all_senders,
            pages_processed=finished.pages_processed,
            discovered_count=finished.discovered_count,
            new_count=finished.new_count,
            updated_count=finished.updated_count,
            failed_count=failed,
            baseline_history_id=finished.start_history_id,
            parser_failed_count=finished.parser_failed_count,
            deleted_count=finished.deleted_count,
            mode=finished.mode,
        )

    def _complete(
        self, run: SyncRun, fingerprint: str, request: FullSyncRequest
    ) -> FullSyncResult:
        with self._unit_of_work_factory() as unit:
            failed = unit.sync_runs.failed_count(run.id)
            current = unit.sync_runs.get(run.id)
            if current is None:
                raise RuntimeError("sync run disappeared before completion")
            parser_failed = current.parser_failed_count
            status = (
                RunStatus.SUCCEEDED
                if failed == 0 and parser_failed == 0
                else RunStatus.PARTIAL
            )
            finished = unit.sync_runs.finish(
                run.id,
                status=status,
                end_history_id=current.start_history_id if failed == 0 else None,
                error_code=(
                    "item_failures"
                    if failed
                    else "parser_failures"
                    if parser_failed
                    else None
                ),
                error_message=(
                    f"{failed} message(s) failed; local data was preserved"
                    if failed
                    else (
                        f"{parser_failed} parser run(s) failed; "
                        "source email was preserved"
                        if parser_failed
                        else None
                    )
                ),
            )
            state = unit.sync_states.get_or_create(run.gmail_account_id)
            if state.query_fingerprint != fingerprint:
                raise SyncAlreadyRunningError(
                    "Sync scope changed while the run was active"
                )
            unit.sync_states.save(
                replace(
                    state,
                    committed_history_id=(
                        finished.start_history_id
                        if failed == 0
                        else state.committed_history_id
                    ),
                    full_sync_next_page_token=None,
                    full_sync_status=(
                        FullSyncStatus.SUCCEEDED
                        if failed == 0
                        else FullSyncStatus.FAILED
                    ),
                    reconciliation_required=failed > 0,
                    last_successful_sync_at=(
                        self._clock() if failed == 0 else state.last_successful_sync_at
                    ),
                ),
                expected_version=state.version,
            )
            unit.app_settings.set(
                f"{_SYNC_SCOPE_SETTING_PREFIX}{run.gmail_account_id}",
                {"all_senders": request.all_senders},
            )
            unit.commit()
        if finished.start_history_id is None:
            raise RuntimeError("full sync has no baseline history ID")
        return FullSyncResult(
            run_id=finished.id,
            status=finished.status,
            query=finished.query,
            thread_expansion_enabled=not request.all_senders,
            pages_processed=finished.pages_processed,
            discovered_count=finished.discovered_count,
            new_count=finished.new_count,
            updated_count=finished.updated_count,
            failed_count=failed,
            parser_failed_count=finished.parser_failed_count,
            deleted_count=finished.deleted_count,
            baseline_history_id=finished.start_history_id,
            mode=finished.mode,
        )


def _query_fingerprint(
    query: str | None,
    rules: tuple[SenderRule, ...],
    request: FullSyncRequest,
) -> str:
    payload = {
        "semantics": _QUERY_SEMANTICS_VERSION,
        "query": query,
        "all_senders": request.all_senders,
        "include_spam_trash": request.include_spam_trash,
        "rules": [
            {
                "id": rule.id,
                "version": rule.version,
                "enabled": rule.enabled,
                "query": rule.gmail_query_enabled,
                "type": rule.match_type.value,
                "value": rule.match_value_normalized,
            }
            for rule in rules
        ],
    }
    serialized = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

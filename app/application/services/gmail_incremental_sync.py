"""Durable Gmail history synchronization with safe reconciliation fallback."""

from __future__ import annotations

import logging
import traceback
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from app.application.services.gmail_full_sync import (
    FullGmailSync,
    FullSyncRequest,
    FullSyncResult,
)
from app.application.services.gmail_labeling import ManageProcessedGmailLabels
from app.application.services.gmail_message_persistence import PersistGmailMessage
from app.application.services.sync_lease import SyncRunLease
from app.domain.enums import RunStatus, SyncItemOperation, SyncMode
from app.domain.errors import (
    EmailDecodeError,
    GmailHistoryExpiredError,
    GmailMessageFetchError,
    GmailMessageNotFoundError,
    InitialSyncRequiredError,
    ScribeError,
    SyncAlreadyRunningError,
    SyncCancelledError,
)
from app.domain.gmail import GmailGateway, GmailHistoryChange, MimeDecoder
from app.domain.models import SenderRule, SyncRun, SyncState
from app.domain.ports.repositories import UnitOfWork
from app.domain.sender_rules import match_sender
from app.infrastructure.observability.logging import LOGGER_NAME, log_context

type UnitOfWorkFactory = Callable[[], UnitOfWork]
type Clock = Callable[[], datetime]

_LOGGER = logging.getLogger(LOGGER_NAME)


@dataclass(frozen=True)
class IncrementalSyncResult:
    """Safe completion summary for CLI and the future Sync dashboard."""

    run_id: str
    status: RunStatus
    mode: SyncMode
    start_history_id: str | None
    end_history_id: str
    pages_processed: int
    discovered_count: int
    new_count: int
    updated_count: int
    deleted_count: int
    failed_count: int
    parser_failed_count: int
    reconciliation_fallback: bool = False


@dataclass(frozen=True)
class _PreparedRun:
    run: SyncRun | None
    rules: tuple[SenderRule, ...]
    state: SyncState
    all_senders: bool
    ignored_label_ids: frozenset[str]


class IncrementalGmailSync:
    """Apply mailbox history by Gmail message ID and advance the cursor last."""

    def __init__(
        self,
        gateway: GmailGateway,
        mime_decoder: MimeDecoder,
        message_persistence: PersistGmailMessage,
        reconciliation_service: FullGmailSync,
        unit_of_work_factory: UnitOfWorkFactory,
        *,
        max_results: int,
        work_batch_size: int,
        lease_seconds: int = 300,
        clock: Clock = lambda: datetime.now(UTC),
        label_manager: ManageProcessedGmailLabels | None = None,
    ) -> None:
        if not 1 <= max_results <= 500:
            raise ValueError("Gmail history page size must be between 1 and 500")
        if work_batch_size < 1:
            raise ValueError("sync work batch size must be positive")
        self._gateway = gateway
        self._mime_decoder = mime_decoder
        self._message_persistence = message_persistence
        self._reconciliation_service = reconciliation_service
        self._unit_of_work_factory = unit_of_work_factory
        self._max_results = max_results
        self._work_batch_size = work_batch_size
        self._lease_seconds = lease_seconds
        self._clock = clock
        self._label_manager = label_manager

    def execute(self) -> IncrementalSyncResult:
        """Run or resume history sync, falling back safely on an expired cursor."""
        profile = self._gateway.get_profile()
        prepared = self._prepare_run(profile.email_address)
        if prepared.run is None:
            return self._reconcile(
                start_history_id=prepared.state.committed_history_id,
                all_senders=prepared.all_senders,
            )
        run = prepared.run
        lease = SyncRunLease(
            self._unit_of_work_factory,
            run.id,
            lease_seconds=self._lease_seconds,
            clock=self._clock,
        )
        lease.acquire()
        with log_context(sync_run_id=run.id):
            _LOGGER.info(
                "Incremental sync started account=%s start_history_id=%s",
                profile.email_address,
                run.start_history_id,
            )
            try:
                lease.checkpoint()
                run = self._discover_changes(run, lease, prepared.ignored_label_ids)
                self._process_changes(run, prepared.rules, lease)
                result = self._complete(run)
            except GmailHistoryExpiredError as error:
                self._finish_expired_run(run, error)
                return self._reconcile(
                    start_history_id=run.start_history_id,
                    all_senders=prepared.all_senders,
                )
            except SyncCancelledError as error:
                result = self._cancel(run, error)
            except SyncAlreadyRunningError:
                raise
            except ScribeError as error:
                self._record_run_error(run.id, error)
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
                "Incremental sync completed status=%s pages=%d discovered=%d "
                "new=%d updated=%d deleted=%d failed=%d parser_failed=%d",
                result.status.value,
                result.pages_processed,
                result.discovered_count,
                result.new_count,
                result.updated_count,
                result.deleted_count,
                result.failed_count,
                result.parser_failed_count,
            )
            return result

    def _prepare_run(self, profile_email: str) -> _PreparedRun:
        with self._unit_of_work_factory() as unit:
            account = unit.accounts.get_or_create(profile_email)
            state = unit.sync_states.get_or_create(account.id)
            rules = unit.sender_rules.list_enabled(account.id)
            scope = unit.app_settings.get(f"sync_scope.{account.id}")
            processed_label = unit.app_settings.get(
                f"gmail_processed_label.{account.id}"
            )
            ignored_label_ids = _configured_label_ids(
                processed_label.value if processed_label is not None else None
            )
            all_senders = (
                bool(scope.value.get("all_senders", False))
                if scope is not None and isinstance(scope.value, dict)
                else state.initial_query is None
            )
            active = unit.sync_runs.find_any_running(account.id)
            if active is not None and active.mode is SyncMode.FULL:
                raise SyncAlreadyRunningError(
                    "A full sync is already running; resume it with sync --full"
                )
            if state.committed_history_id is None:
                raise InitialSyncRequiredError(
                    "No committed Gmail history cursor exists; run sync --full first"
                )
            if active is not None and active.mode is SyncMode.INCREMENTAL:
                run = active
            elif (
                active is not None and active.mode is SyncMode.RECONCILIATION
            ) or state.reconciliation_required:
                run = None
            else:
                run = unit.sync_runs.create(
                    account_id=account.id,
                    mode=SyncMode.INCREMENTAL,
                    query=None,
                    start_history_id=state.committed_history_id,
                )
            unit.commit()
        return _PreparedRun(
            run=run,
            rules=rules,
            state=state,
            all_senders=all_senders,
            ignored_label_ids=ignored_label_ids,
        )

    def _discover_changes(
        self,
        run: SyncRun,
        lease: SyncRunLease,
        ignored_label_ids: frozenset[str],
    ) -> SyncRun:
        if run.start_history_id is None:
            raise InitialSyncRequiredError("Incremental run has no history cursor")
        checkpoint_restarted = False
        while run.pages_processed == 0 or run.next_page_token is not None:
            lease.checkpoint()
            try:
                page = self._gateway.list_history(
                    start_history_id=run.start_history_id,
                    page_token=run.next_page_token,
                    max_results=self._max_results,
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
                    unit.commit()
                checkpoint_restarted = True
                _LOGGER.warning(
                    "Saved Gmail history page token was rejected; restarting "
                    "from the committed cursor with item deduplication"
                )
                continue
            changes = _collapse_changes(page.changes, ignored_label_ids)
            with self._unit_of_work_factory() as unit:
                inserted = unit.sync_runs.enqueue_changes(run.id, changes)
                run = unit.sync_runs.checkpoint_page(
                    run.id,
                    next_page_token=page.next_page_token,
                    end_history_id=page.latest_history_id,
                )
                unit.commit()
            _LOGGER.info(
                "Gmail history page checkpointed page=%d events=%d enqueued=%d "
                "has_next=%s",
                run.pages_processed,
                len(page.changes),
                inserted,
                page.next_page_token is not None,
            )
        return run

    def _process_changes(
        self,
        run: SyncRun,
        rules: tuple[SenderRule, ...],
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
                        self._apply_delete(run, item.id, item.gmail_message_id)
                        continue
                    try:
                        metadata = self._gateway.get_message_metadata(
                            item.gmail_message_id
                        )
                        configured_rule = match_sender(
                            metadata.sender_header,
                            rules,
                        )
                        with self._unit_of_work_factory() as unit:
                            existing = unit.emails.get_by_gmail_id(
                                run.gmail_account_id,
                                item.gmail_message_id,
                            )
                            prior_rule = (
                                unit.sender_rules.get(existing.matched_sender_rule_id)
                                if existing is not None
                                and existing.matched_sender_rule_id is not None
                                else None
                            )
                        if configured_rule is None and existing is None:
                            with self._unit_of_work_factory() as unit:
                                unit.sync_runs.mark_skipped(item.id)
                                unit.commit()
                            continue
                        raw = self._gateway.get_message(item.gmail_message_id)
                        decoded = self._mime_decoder.decode(
                            raw,
                            self._gateway.get_attachment,
                        )
                        verified_rule = (
                            match_sender(decoded.sender, rules) or prior_rule
                        )
                        if verified_rule is None and existing is None:
                            with self._unit_of_work_factory() as unit:
                                unit.sync_runs.mark_skipped(item.id)
                                unit.commit()
                            continue
                        self._message_persistence.execute(
                            run,
                            item.id,
                            decoded,
                            verified_rule,
                        )
                    except GmailMessageNotFoundError:
                        self._apply_delete(run, item.id, item.gmail_message_id)
                    except GmailMessageFetchError as error:
                        if error.retryable:
                            raise
                        self._mark_item_failed(item.id, error)
                    except EmailDecodeError as error:
                        self._mark_item_failed(item.id, error)

    def _apply_delete(
        self,
        run: SyncRun,
        item_id: int,
        gmail_message_id: str,
    ) -> None:
        with self._unit_of_work_factory() as unit:
            deleted = unit.emails.mark_source_deleted(
                run.gmail_account_id,
                gmail_message_id,
                self._clock(),
            )
            if deleted:
                unit.sync_runs.mark_succeeded(
                    item_id,
                    created=False,
                    deleted=True,
                )
            else:
                unit.sync_runs.mark_skipped(item_id)
            unit.commit()

    def _mark_item_failed(self, item_id: int, error: ScribeError) -> None:
        with self._unit_of_work_factory() as unit:
            unit.sync_runs.mark_failed(
                item_id,
                error_code=type(error).__name__,
                error_message=str(error),
                retryable=error.retryable,
            )
            unit.commit()

    def _complete(self, run: SyncRun) -> IncrementalSyncResult:
        with self._unit_of_work_factory() as unit:
            current = unit.sync_runs.get(run.id)
            if current is None or current.end_history_id is None:
                raise RuntimeError("incremental run has no target history ID")
            failed = unit.sync_runs.failed_count(run.id)
            status = (
                RunStatus.SUCCEEDED
                if failed == 0 and current.parser_failed_count == 0
                else RunStatus.PARTIAL
            )
            finished = unit.sync_runs.finish(
                run.id,
                status=status,
                end_history_id=current.end_history_id,
                error_code=(
                    "item_failures"
                    if failed
                    else "parser_failures"
                    if current.parser_failed_count
                    else None
                ),
                error_message=(
                    f"{failed} message change(s) failed; history cursor was retained"
                    if failed
                    else (
                        f"{current.parser_failed_count} parser run(s) failed"
                        if current.parser_failed_count
                        else None
                    )
                ),
            )
            state = unit.sync_states.get_or_create(run.gmail_account_id)
            unit.sync_states.save(
                replace(
                    state,
                    committed_history_id=(
                        finished.end_history_id
                        if failed == 0
                        else state.committed_history_id
                    ),
                    reconciliation_required=failed > 0,
                    last_successful_sync_at=(
                        self._clock() if failed == 0 else state.last_successful_sync_at
                    ),
                ),
                expected_version=state.version,
            )
            unit.commit()
        return self._result(finished, failed=failed)

    def _finish_expired_run(
        self,
        run: SyncRun,
        error: GmailHistoryExpiredError,
    ) -> None:
        with self._unit_of_work_factory() as unit:
            unit.sync_runs.finish(
                run.id,
                status=RunStatus.FAILED,
                end_history_id=None,
                error_code=type(error).__name__,
                error_message=str(error),
            )
            unit.commit()
        _LOGGER.warning(
            "Gmail history cursor expired; preserving local data and starting "
            "complete scope reconciliation"
        )

    def _cancel(
        self,
        run: SyncRun,
        error: SyncCancelledError,
    ) -> IncrementalSyncResult:
        with self._unit_of_work_factory() as unit:
            current = unit.sync_runs.get(run.id)
            if current is None:
                raise RuntimeError("sync run disappeared during cancellation")
            failed = unit.sync_runs.failed_count(run.id)
            end_history_id = current.end_history_id or current.start_history_id
            if end_history_id is None:
                raise RuntimeError("cancelled incremental sync has no history cursor")
            finished = unit.sync_runs.finish(
                run.id,
                status=RunStatus.CANCELLED,
                end_history_id=end_history_id,
                error_code=type(error).__name__,
                error_message=str(error),
            )
            state = unit.sync_states.get_or_create(run.gmail_account_id)
            unit.sync_states.save(
                replace(state, reconciliation_required=True),
                expected_version=state.version,
            )
            unit.commit()
        _LOGGER.info("Incremental sync cancelled at a safe checkpoint")
        return self._result(finished, failed=failed)

    def _reconcile(
        self,
        *,
        start_history_id: str | None,
        all_senders: bool,
    ) -> IncrementalSyncResult:
        result = self._reconciliation_service.execute(
            FullSyncRequest(
                all_senders=all_senders,
                mode=SyncMode.RECONCILIATION,
            )
        )
        return _result_from_reconciliation(
            result,
            start_history_id=start_history_id,
        )

    def _record_run_error(self, run_id: str, error: ScribeError) -> None:
        with self._unit_of_work_factory() as unit:
            unit.sync_runs.record_error(
                run_id,
                error_code=type(error).__name__,
                error_message=str(error),
                debug_trace=traceback.format_exc(limit=20),
            )
            unit.commit()

    @staticmethod
    def _result(finished: SyncRun, *, failed: int) -> IncrementalSyncResult:
        if finished.end_history_id is None:
            raise RuntimeError("completed incremental run has no ending history ID")
        return IncrementalSyncResult(
            run_id=finished.id,
            status=finished.status,
            mode=finished.mode,
            start_history_id=finished.start_history_id,
            end_history_id=finished.end_history_id,
            pages_processed=finished.pages_processed,
            discovered_count=finished.discovered_count,
            new_count=finished.new_count,
            updated_count=finished.updated_count,
            deleted_count=finished.deleted_count,
            failed_count=failed,
            parser_failed_count=finished.parser_failed_count,
        )


def _collapse_changes(
    changes: tuple[GmailHistoryChange, ...],
    ignored_label_ids: frozenset[str] = frozenset(),
) -> tuple[tuple[str, SyncItemOperation], ...]:
    precedence = {
        SyncItemOperation.UPDATE: 1,
        SyncItemOperation.FETCH: 2,
        SyncItemOperation.DELETE: 3,
    }
    collapsed: dict[str, SyncItemOperation] = {}
    for change in changes:
        changed_labels = frozenset(change.labels_added + change.labels_removed)
        if (
            not change.message_added
            and not change.message_deleted
            and changed_labels
            and changed_labels <= ignored_label_ids
        ):
            continue
        if change.message_deleted:
            operation = SyncItemOperation.DELETE
        elif change.message_added:
            operation = SyncItemOperation.FETCH
        else:
            operation = SyncItemOperation.UPDATE
        current = collapsed.get(change.message_id)
        if current is None or precedence[operation] > precedence[current]:
            collapsed[change.message_id] = operation
    return tuple(sorted(collapsed.items()))


def _configured_label_ids(value: object) -> frozenset[str]:
    if not isinstance(value, dict):
        return frozenset()
    label_id = value.get("id")
    if not isinstance(label_id, str) or not label_id:
        return frozenset()
    return frozenset({label_id})


def _result_from_reconciliation(
    result: FullSyncResult,
    *,
    start_history_id: str | None,
) -> IncrementalSyncResult:
    return IncrementalSyncResult(
        run_id=result.run_id,
        status=result.status,
        mode=SyncMode.RECONCILIATION,
        start_history_id=start_history_id,
        end_history_id=result.baseline_history_id,
        pages_processed=result.pages_processed,
        discovered_count=result.discovered_count,
        new_count=result.new_count,
        updated_count=result.updated_count,
        deleted_count=result.deleted_count,
        failed_count=result.failed_count,
        parser_failed_count=result.parser_failed_count,
        reconciliation_fallback=True,
    )

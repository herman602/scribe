"""Durable application service for labeling processed Gmail messages."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from app.domain.errors import ScribeError
from app.domain.gmail import GmailAccountProfile
from app.domain.ports.repositories import UnitOfWork
from app.infrastructure.observability.logging import LOGGER_NAME

type UnitOfWorkFactory = Callable[[], UnitOfWork]

_SETTING_PREFIX = "gmail_processed_label."
_LOGGER = logging.getLogger(LOGGER_NAME)


class ProcessedLabelGateway(Protocol):
    """Minimal Gmail boundary needed by the processed-Label workflow."""

    def get_profile(self) -> GmailAccountProfile: ...

    def ensure_label(self, label_name: str) -> str: ...

    def add_label_to_messages(
        self, message_ids: tuple[str, ...], label_id: str
    ) -> None: ...


@dataclass(frozen=True)
class GmailLabelingResult:
    """Non-sensitive backfill or flush summary."""

    account_id: int
    queued_count: int
    succeeded_count: int
    pending_count: int
    failed_count: int
    error_message: str | None = None


class ManageProcessedGmailLabels:
    """Queue and deliver idempotent message-level processed Labels."""

    def __init__(
        self,
        gateway: ProcessedLabelGateway,
        unit_of_work_factory: UnitOfWorkFactory,
        *,
        label_name: str,
        batch_size: int = 1000,
    ) -> None:
        if not 1 <= batch_size <= 1000:
            raise ValueError("Gmail Label batch size must be between 1 and 1000")
        self._gateway = gateway
        self._unit_of_work_factory = unit_of_work_factory
        self._label_name = label_name
        self._batch_size = batch_size

    def backfill(self) -> GmailLabelingResult:
        """Queue all locally processed messages, then deliver pending work."""
        profile = self._gateway.get_profile()
        with self._unit_of_work_factory() as unit:
            account = unit.accounts.get_or_create(profile.email_address)
            queued = unit.gmail_label_operations.enqueue_processed(
                account_id=account.id, label_name=self._label_name
            )
            unit.commit()
        return self.flush(account.id, queued_count=queued)

    def flush(self, account_id: int, *, queued_count: int = 0) -> GmailLabelingResult:
        """Deliver pending operations in bounded Gmail batchModify calls."""
        try:
            label_id = self._gateway.ensure_label(self._label_name)
        except ScribeError as error:
            return self._result(account_id, queued_count, str(error))

        with self._unit_of_work_factory() as unit:
            unit.app_settings.set(
                f"{_SETTING_PREFIX}{account_id}",
                {"name": self._label_name, "id": label_id},
            )
            unit.commit()

        while True:
            with self._unit_of_work_factory() as unit:
                operations = unit.gmail_label_operations.list_processable(
                    account_id=account_id,
                    label_name=self._label_name,
                    limit=self._batch_size,
                )
            if not operations:
                return self._result(account_id, queued_count, None)
            operation_ids = tuple(item.id for item in operations)
            try:
                self._gateway.add_label_to_messages(
                    tuple(item.gmail_message_id for item in operations), label_id
                )
            except ScribeError as error:
                with self._unit_of_work_factory() as unit:
                    unit.gmail_label_operations.mark_failed(
                        operation_ids,
                        error_code=type(error).__name__,
                        error_message=str(error),
                        retryable=error.retryable,
                    )
                    unit.commit()
                _LOGGER.warning(
                    "Processed Gmail Label batch failed error_code=%s retryable=%s",
                    type(error).__name__,
                    error.retryable,
                )
                return self._result(account_id, queued_count, str(error))
            with self._unit_of_work_factory() as unit:
                unit.gmail_label_operations.mark_succeeded(operation_ids)
                unit.commit()

    def _result(
        self, account_id: int, queued_count: int, error_message: str | None
    ) -> GmailLabelingResult:
        with self._unit_of_work_factory() as unit:
            counts = unit.gmail_label_operations.counts(
                account_id=account_id, label_name=self._label_name
            )
        return GmailLabelingResult(
            account_id=account_id,
            queued_count=queued_count,
            succeeded_count=counts.get("succeeded", 0),
            pending_count=counts.get("pending", 0),
            failed_count=counts.get("failed", 0),
            error_message=error_message,
        )

"""Atomic persistence for one decoded Gmail message and parser result."""

from __future__ import annotations

import logging
from collections.abc import Callable

from app.application.services.email_processing import PrepareEmailProcessing
from app.application.services.financial_event_intake import FinancialEventIntake
from app.domain.enums import (
    BodyRetentionPolicy,
    ClassificationStatus,
    ParserTrigger,
    ProcessingStatus,
)
from app.domain.gmail import DecodedEmail
from app.domain.models import EmailSnapshot, SenderRule, SyncRun, TransactionDraft
from app.domain.ports.repositories import UnitOfWork
from app.infrastructure.observability.logging import LOGGER_NAME

type UnitOfWorkFactory = Callable[[], UnitOfWork]

_LOGGER = logging.getLogger(LOGGER_NAME)


class PersistGmailMessage:
    """Persist Gmail-owned fields, parser audit, and candidates atomically."""

    def __init__(
        self,
        email_processing: PrepareEmailProcessing,
        unit_of_work_factory: UnitOfWorkFactory,
        *,
        body_retention_policy: BodyRetentionPolicy,
        body_summary_chars: int,
        processed_label_name: str | None = None,
        intake: FinancialEventIntake | None = None,
    ) -> None:
        if body_summary_chars < 1:
            raise ValueError("body summary size must be positive")
        self._email_processing = email_processing
        self._unit_of_work_factory = unit_of_work_factory
        self._body_retention_policy = body_retention_policy
        self._body_summary_chars = body_summary_chars
        self._processed_label_name = processed_label_name
        self._intake = intake

    def execute(
        self,
        run: SyncRun,
        item_id: int,
        decoded: DecodedEmail,
        matched_rule: SenderRule | None,
    ) -> None:
        """Process and commit one source message without partial parser writes."""
        prepared = self._email_processing.execute(decoded, matched_rule)
        body_text, body_summary = self._retained_body(decoded.text_body)
        snapshot = EmailSnapshot(
            gmail_account_id=run.gmail_account_id,
            gmail_message_id=decoded.message_id,
            gmail_thread_id=decoded.thread_id,
            gmail_history_id=decoded.history_id,
            sender=decoded.sender,
            recipients=decoded.recipients,
            subject=decoded.subject,
            gmail_internal_date_ms=decoded.internal_date_ms,
            snippet=decoded.snippet,
            received_at=decoded.received_at,
            body_text=body_text,
            body_summary=body_summary,
            has_html_body=decoded.has_html_body,
            labels=decoded.label_ids,
            matched_sender_rule_id=matched_rule.id if matched_rule else None,
            body_policy_applied=self._body_retention_policy,
        )
        with self._unit_of_work_factory() as unit:
            outcome = unit.emails.upsert_from_gmail(snapshot)
            unit.attachments.replace_processed_for_email(
                outcome.email.id, prepared.attachments
            )
            parser_run_id = unit.parser_runs.start(
                email_id=outcome.email.id,
                sync_run_id=run.id,
                trigger=ParserTrigger.SYNC,
            )
            result = prepared.pipeline_result
            if result is None:
                unit.parser_runs.fail(
                    parser_run_id,
                    error_code=prepared.error_code or "ParserError",
                    error_message=prepared.error_message or "Parser failed",
                    debug_trace=prepared.debug_trace or "",
                )
                unit.emails.apply_processing_result(
                    outcome.email.id,
                    status=ProcessingStatus.FAILED,
                    classification=ClassificationStatus.UNCERTAIN,
                    confidence=None,
                    reason="Local transaction processing failed",
                    parser_name=None,
                    parser_version=None,
                    purge_body=False,
                )
            else:
                unit.classification_results.add_many(
                    email_id=outcome.email.id,
                    parser_run_id=parser_run_id,
                    stages=result.stages,
                )
                unit.transactions.reconcile(
                    outcome.email.id,
                    tuple(
                        TransactionDraft(
                            email_id=outcome.email.id,
                            source_key=candidate.source_key,
                            transaction_type=candidate.transaction_type,
                            confidence=candidate.confidence,
                            parser_name=result.parser_name,
                            parser_version=result.parser_version,
                            merchant=candidate.merchant,
                            original_merchant_text=candidate.original_merchant_text,
                            amount_minor=candidate.amount_minor,
                            currency=candidate.currency,
                            card_last_four=candidate.card_last_four,
                            account_last_four=candidate.account_last_four,
                            transaction_at=candidate.transaction_at,
                            posted_at=candidate.posted_at,
                            authorization_code=candidate.authorization_code,
                            extraction_evidence=tuple(
                                item.as_json() for item in candidate.evidence
                            ),
                            parse_warnings=tuple(
                                item.as_json() for item in candidate.warnings
                            ),
                        )
                        for candidate in result.candidates
                    ),
                )
                if self._intake is not None:
                    self._intake.refresh_for_email(unit, outcome.email.id)
                if self._processed_label_name is not None:
                    unit.gmail_label_operations.enqueue(
                        email_id=outcome.email.id,
                        label_name=self._processed_label_name,
                    )
                unit.parser_runs.succeed(parser_run_id, result)
                unit.emails.apply_processing_result(
                    outcome.email.id,
                    status=ProcessingStatus.PROCESSED,
                    classification=result.classification,
                    confidence=result.confidence,
                    reason=result.reason,
                    parser_name=result.parser_name,
                    parser_version=result.parser_version,
                    purge_body=(
                        self._body_retention_policy
                        is BodyRetentionPolicy.PURGE_AFTER_PARSE
                    ),
                )
            unit.sync_runs.mark_succeeded(
                item_id,
                created=outcome.created,
                parsed=result is not None,
                parser_failed=result is None,
            )
            unit.commit()
        if decoded.warnings:
            _LOGGER.warning(
                "Message decoded with warnings warning_codes=%s",
                ",".join(item.code for item in decoded.warnings),
            )
        _LOGGER.info(
            "Message persisted created=%s attachments=%d",
            outcome.created,
            len(prepared.attachments),
        )

    def _retained_body(self, text: str) -> tuple[str | None, str | None]:
        summary = text[: self._body_summary_chars] if text else None
        if self._body_retention_policy is BodyRetentionPolicy.SUMMARY_ONLY:
            return None, summary
        return text, summary

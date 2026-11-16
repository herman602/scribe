"""Offline parser rerun over already retained email and attachment text."""

from __future__ import annotations

import traceback
from collections.abc import Callable
from datetime import UTC, datetime

from app.application.services.financial_event_intake import FinancialEventIntake
from app.domain.enums import (
    ClassificationStatus,
    ParserTrigger,
    ProcessingStatus,
)
from app.domain.errors import ParserError
from app.domain.models import TransactionDraft
from app.domain.parsing import (
    ClassificationEngine,
    ParserAttachment,
    ParserEmail,
    PipelineResult,
)
from app.domain.ports.repositories import UnitOfWork

type UnitOfWorkFactory = Callable[[], UnitOfWork]


class ReparseStoredEmail:
    """Rerun deterministic parsers without Gmail or external network access."""

    def __init__(
        self,
        pipeline: ClassificationEngine,
        unit_of_work_factory: UnitOfWorkFactory,
        *,
        processed_label_name: str | None = None,
        intake: FinancialEventIntake | None = None,
    ) -> None:
        self._pipeline = pipeline
        self._unit_of_work_factory = unit_of_work_factory
        self._processed_label_name = processed_label_name
        self._intake = intake

    def execute(self, email_id: int) -> PipelineResult:
        """Persist one manual parser run while preserving transaction overrides."""
        with self._unit_of_work_factory() as unit:
            email = unit.emails.get(email_id)
            if email is None:
                raise ValueError("email was not found")
            rule = (
                unit.sender_rules.get(email.matched_sender_rule_id)
                if email.matched_sender_rule_id is not None
                else None
            )
            attachments = unit.attachments.list_for_email(email_id)
            parser_email = ParserEmail(
                email_id=email.id,
                gmail_message_id=email.gmail_message_id,
                sender=email.sender,
                subject=email.subject,
                received_at=email.received_at,
                internal_date=datetime.fromtimestamp(
                    email.gmail_internal_date_ms / 1000, tz=UTC
                ),
                text_body=email.body_text or email.body_summary or "",
                has_html_body=email.has_html_body,
                labels=frozenset(email.labels),
                institution_key=rule.institution_key if rule else None,
                parser_hint=rule.parser_name if rule else None,
                attachments=tuple(
                    ParserAttachment(
                        attachment_id=item.id,
                        mime_part_id=item.mime_part_id,
                        filename=item.filename,
                        mime_type=item.mime_type,
                        sha256=item.sha256,
                        encrypted=item.is_encrypted,
                        extraction_status=item.extraction_status,
                        extracted_text=item.extracted_text,
                        extractor_name=item.extractor_name,
                        extractor_version=item.extractor_version,
                    )
                    for item in attachments
                ),
            )
            parser_run_id = unit.parser_runs.start(
                email_id=email_id,
                sync_run_id=None,
                trigger=ParserTrigger.MANUAL,
            )
            try:
                result = self._pipeline.evaluate(parser_email)
            except Exception as error:
                safe_error = ParserError("Stored email reparse failed")
                unit.parser_runs.fail(
                    parser_run_id,
                    error_code=type(safe_error).__name__,
                    error_message=str(safe_error),
                    debug_trace=traceback.format_exc(limit=20),
                )
                unit.emails.apply_processing_result(
                    email_id,
                    status=ProcessingStatus.FAILED,
                    classification=ClassificationStatus.UNCERTAIN,
                    confidence=None,
                    reason="Local transaction processing failed",
                    parser_name=None,
                    parser_version=None,
                    purge_body=False,
                )
                unit.commit()
                raise safe_error from error
            unit.classification_results.add_many(
                email_id=email_id,
                parser_run_id=parser_run_id,
                stages=result.stages,
            )
            unit.transactions.reconcile(
                email_id,
                tuple(
                    TransactionDraft(
                        email_id=email_id,
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
                self._intake.refresh_for_email(unit, email_id)
            unit.parser_runs.succeed(parser_run_id, result)
            unit.emails.apply_processing_result(
                email_id,
                status=ProcessingStatus.PROCESSED,
                classification=result.classification,
                confidence=result.confidence,
                reason=result.reason,
                parser_name=result.parser_name,
                parser_version=result.parser_version,
                purge_body=False,
            )
            if self._processed_label_name is not None:
                unit.gmail_label_operations.enqueue(
                    email_id=email_id,
                    label_name=self._processed_label_name,
                )
            unit.commit()
            return result

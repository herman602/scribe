"""SQLAlchemy repository implementations and model/domain translation."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.domain.enums import (
    AttachmentStatus,
    BodyRetentionPolicy,
    ClassificationStatus,
    EmailSourceStatus,
    FullSyncStatus,
    GmailLabelOperationStatus,
    ParserRunStatus,
    ParserTrigger,
    ProcessingStatus,
    RunStatus,
    SenderMatchType,
    SyncItemOperation,
    SyncItemStatus,
    SyncMode,
    TransactionSourceStatus,
    TransactionType,
)
from app.domain.errors import (
    ConcurrentUpdateError,
    DatabaseError,
    SyncAlreadyRunningError,
)
from app.domain.models import (
    AppSetting,
    AttachmentSnapshot,
    EmailAttachment,
    EmailMessage,
    EmailSnapshot,
    EmailUpsertOutcome,
    GmailAccount,
    GmailLabelOperation,
    ProcessedAttachmentSnapshot,
    SenderRule,
    SenderRuleDraft,
    SupplementaryCardSetting,
    SyncRun,
    SyncRunItem,
    SyncState,
    Transaction,
    TransactionDraft,
    TransactionReviewDraft,
)
from app.domain.parsing import PipelineResult, StageResult
from app.domain.sender_rules import normalize_sender_match, validate_institution_key
from app.domain.types import JsonValue
from app.infrastructure.database.models import (
    AppSettingModel,
    ClassificationResultModel,
    EmailAttachmentModel,
    EmailModel,
    GmailAccountModel,
    GmailLabelOperationModel,
    ParserRunModel,
    SenderRuleModel,
    SupplementaryCardSettingModel,
    SyncRunItemModel,
    SyncRunModel,
    SyncStateModel,
    TransactionModel,
)

_SECRET_SETTING_KEY = re.compile(
    r"(?:^|[._-])(token|password|secret|credential)(?:$|[._-])",
    re.IGNORECASE,
)


def _account_record(model: GmailAccountModel) -> GmailAccount:
    return GmailAccount(
        id=model.id,
        profile_email=model.profile_email,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _sender_rule_record(model: SenderRuleModel) -> SenderRule:
    return SenderRule(
        id=model.id,
        gmail_account_id=model.gmail_account_id,
        institution_key=model.institution_key,
        institution_name=model.institution_name,
        match_type=SenderMatchType(model.match_type),
        match_value_normalized=model.match_value_normalized,
        parser_name=model.parser_name,
        priority=model.priority,
        enabled=model.enabled,
        gmail_query_enabled=model.gmail_query_enabled,
        pdf_password_refs=tuple(cast(list[str], model.pdf_password_refs_json)),
        version=model.version,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _supplementary_card_setting_record(
    model: SupplementaryCardSettingModel,
) -> SupplementaryCardSetting:
    return SupplementaryCardSetting(
        id=model.id,
        gmail_account_id=model.gmail_account_id,
        institution_key=model.institution_key,
        card_last_four=model.card_last_four,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _attachment_record(model: EmailAttachmentModel) -> EmailAttachment:
    return EmailAttachment(
        id=model.id,
        email_id=model.email_id,
        gmail_attachment_id=model.gmail_attachment_id,
        mime_part_id=model.mime_part_id,
        filename=model.filename,
        mime_type=model.mime_type,
        size_bytes=model.size_bytes,
        sha256=model.sha256,
        is_pdf=model.is_pdf,
        is_encrypted=model.is_encrypted,
        extraction_status=AttachmentStatus(model.extraction_status),
        extracted_text=model.extracted_text,
        raw_encrypted_content=model.raw_encrypted_content,
        extractor_name=model.extractor_name,
        extractor_version=model.extractor_version,
        warnings=tuple(cast(list[JsonValue], model.warnings_json)),
        error_code=model.error_code,
        error_message=model.error_message,
        password_ref_used=model.password_ref_used,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _sync_run_record(model: SyncRunModel) -> SyncRun:
    return SyncRun(
        id=model.id,
        gmail_account_id=model.gmail_account_id,
        mode=SyncMode(model.mode),
        status=RunStatus(model.status),
        query=model.query,
        start_history_id=model.start_history_id,
        end_history_id=model.end_history_id,
        next_page_token=model.next_page_token,
        pages_processed=model.pages_processed,
        discovered_count=model.discovered_count,
        new_count=model.new_count,
        updated_count=model.updated_count,
        deleted_count=model.deleted_count,
        parsed_count=model.parsed_count,
        parser_failed_count=model.parser_failed_count,
        retried_count=model.retried_count,
        cancel_requested=model.cancel_requested,
        worker_id=model.worker_id,
        lease_expires_at=model.lease_expires_at,
        error_code=model.error_code,
        error_message=model.error_message,
        started_at=model.started_at,
        heartbeat_at=model.heartbeat_at,
        finished_at=model.finished_at,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _sync_item_record(model: SyncRunItemModel) -> SyncRunItem:
    return SyncRunItem(
        id=model.id,
        sync_run_id=model.sync_run_id,
        gmail_message_id=model.gmail_message_id,
        operation=SyncItemOperation(model.operation),
        status=SyncItemStatus(model.status),
        attempt_count=model.attempt_count,
        error_code=model.error_code,
        error_message=model.error_message,
        retryable=model.retryable,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _email_record(model: EmailModel) -> EmailMessage:
    return EmailMessage(
        id=model.id,
        gmail_account_id=model.gmail_account_id,
        gmail_message_id=model.gmail_message_id,
        gmail_thread_id=model.gmail_thread_id,
        gmail_history_id=model.gmail_history_id,
        sender=model.sender,
        recipients=tuple(model.recipients_json),
        subject=model.subject,
        received_at=model.received_at,
        gmail_internal_date_ms=model.gmail_internal_date_ms,
        snippet=model.snippet,
        body_text=model.body_text,
        body_summary=model.body_summary,
        has_html_body=model.has_html_body,
        labels=tuple(model.labels_json),
        matched_sender_rule_id=model.matched_sender_rule_id,
        body_policy_applied=BodyRetentionPolicy(model.body_policy_applied),
        classification_status=ClassificationStatus(model.classification_status),
        classification_confidence=model.classification_confidence,
        classification_reason=model.classification_reason,
        parser_name=model.parser_name,
        parser_version=model.parser_version,
        processing_status=ProcessingStatus(model.processing_status),
        source_status=EmailSourceStatus(model.source_status),
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _gmail_label_operation_record(
    model: GmailLabelOperationModel,
) -> GmailLabelOperation:
    return GmailLabelOperation(
        id=model.id,
        gmail_account_id=model.gmail_account_id,
        email_id=model.email_id,
        gmail_message_id=model.gmail_message_id,
        label_name=model.label_name,
        status=GmailLabelOperationStatus(model.status),
        attempt_count=model.attempt_count,
        error_code=model.error_code,
        error_message=model.error_message,
        last_attempt_at=model.last_attempt_at,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _transaction_record(model: TransactionModel) -> Transaction:
    return Transaction(
        id=model.id,
        email_id=model.email_id,
        source_key=model.source_key,
        transaction_type=TransactionType(model.transaction_type),
        merchant=model.merchant,
        amount_minor=model.amount_minor,
        currency=model.currency,
        card_last_four=model.card_last_four,
        account_last_four=model.account_last_four,
        transaction_at=model.transaction_at,
        posted_at=model.posted_at,
        confidence=model.confidence,
        parser_name=model.parser_name,
        parser_version=model.parser_version,
        manually_confirmed=model.manually_confirmed,
        manually_ignored=model.manually_ignored,
        user_note=model.user_note,
        source_status=TransactionSourceStatus(model.source_status),
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _sync_state_record(model: SyncStateModel) -> SyncState:
    return SyncState(
        gmail_account_id=model.gmail_account_id,
        committed_history_id=model.committed_history_id,
        baseline_history_id=model.baseline_history_id,
        initial_query=model.initial_query,
        query_fingerprint=model.query_fingerprint,
        query_semantics_version=model.query_semantics_version,
        full_sync_next_page_token=model.full_sync_next_page_token,
        full_sync_status=FullSyncStatus(model.full_sync_status),
        reconciliation_required=model.reconciliation_required,
        last_successful_sync_at=model.last_successful_sync_at,
        updated_at=model.updated_at,
        version=model.version,
    )


def _flush_and_refresh(session: Session, model: Any) -> None:
    try:
        session.flush()
        session.refresh(model)
    except SQLAlchemyError as error:
        raise DatabaseError("database write failed") from error


class SqlAlchemyGmailAccountRepository:
    """SQLAlchemy Gmail account repository."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_or_create(self, profile_email: str) -> GmailAccount:
        """Return a normalized account, creating it when absent."""
        normalized = profile_email.strip().lower()
        if not normalized:
            raise ValueError("profile email cannot be empty")
        model = self._session.scalar(
            select(GmailAccountModel).where(
                GmailAccountModel.profile_email == normalized
            )
        )
        if model is None:
            model = GmailAccountModel(profile_email=normalized)
            self._session.add(model)
            _flush_and_refresh(self._session, model)
        return _account_record(model)

    def get(self, account_id: int) -> GmailAccount | None:
        """Return an account by internal ID."""
        model = self._session.get(GmailAccountModel, account_id)
        return None if model is None else _account_record(model)

    def list_all(self) -> tuple[GmailAccount, ...]:
        """Return all configured accounts in stable ID order."""
        models = self._session.scalars(
            select(GmailAccountModel).order_by(GmailAccountModel.id)
        )
        return tuple(_account_record(model) for model in models)

    def get_by_profile_email(self, profile_email: str) -> GmailAccount | None:
        """Return an account by normalized Gmail profile address."""
        normalized = profile_email.strip().lower()
        model = self._session.scalar(
            select(GmailAccountModel).where(
                GmailAccountModel.profile_email == normalized
            )
        )
        return None if model is None else _account_record(model)


class SqlAlchemySenderRuleRepository:
    """SQLAlchemy sender-rule repository with safe normalization."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_all(self, account_id: int) -> tuple[SenderRule, ...]:
        """Return account rules by priority then ID."""
        models = self._session.scalars(
            select(SenderRuleModel)
            .where(SenderRuleModel.gmail_account_id == account_id)
            .order_by(SenderRuleModel.priority, SenderRuleModel.id)
        )
        return tuple(_sender_rule_record(model) for model in models)

    def list_enabled(self, account_id: int) -> tuple[SenderRule, ...]:
        """Return only enabled account rules."""
        return tuple(rule for rule in self.list_all(account_id) if rule.enabled)

    def get(self, rule_id: int) -> SenderRule | None:
        """Return one sender rule by its stable local ID."""
        model = self._session.get(SenderRuleModel, rule_id)
        return None if model is None else _sender_rule_record(model)

    def upsert(self, draft: SenderRuleDraft) -> SenderRule:
        """Create or update the unique account/match rule."""
        institution_key = validate_institution_key(draft.institution_key)
        institution_name = draft.institution_name.strip()
        if not institution_name:
            raise ValueError("institution name cannot be empty")
        if draft.priority < 0:
            raise ValueError("priority must be non-negative")
        normalized = normalize_sender_match(draft.match_type, draft.match_value)
        model = self._session.scalar(
            select(SenderRuleModel).where(
                SenderRuleModel.gmail_account_id == draft.gmail_account_id,
                SenderRuleModel.match_type == draft.match_type.value,
                SenderRuleModel.match_value_normalized == normalized,
            )
        )
        if model is None:
            model = SenderRuleModel(
                gmail_account_id=draft.gmail_account_id,
                institution_key=institution_key,
                institution_name=institution_name,
                match_type=draft.match_type.value,
                match_value_normalized=normalized,
                parser_name=draft.parser_name,
                priority=draft.priority,
                enabled=True,
                gmail_query_enabled=draft.gmail_query_enabled,
            )
            self._session.add(model)
        else:
            model.institution_key = institution_key
            model.institution_name = institution_name
            model.parser_name = draft.parser_name
            model.priority = draft.priority
            model.enabled = True
            model.gmail_query_enabled = draft.gmail_query_enabled
            model.version += 1
        _flush_and_refresh(self._session, model)
        return _sender_rule_record(model)

    def disable(self, rule_id: int) -> SenderRule | None:
        """Disable a rule without deleting its audit identity."""
        model = self._session.get(SenderRuleModel, rule_id)
        if model is None:
            return None
        if model.enabled:
            model.enabled = False
            model.version += 1
            _flush_and_refresh(self._session, model)
        return _sender_rule_record(model)

    def set_pdf_password_refs(
        self, rule_id: int, references: tuple[str, ...]
    ) -> SenderRule | None:
        """Store only opaque credential-store references on a sender rule."""
        if any(not reference.strip() for reference in references):
            raise ValueError("PDF password references cannot be blank")
        if len(set(references)) != len(references):
            raise ValueError("PDF password references must be unique")
        model = self._session.get(SenderRuleModel, rule_id)
        if model is None:
            return None
        model.pdf_password_refs_json = list(references)
        model.version += 1
        _flush_and_refresh(self._session, model)
        return _sender_rule_record(model)


class SqlAlchemySupplementaryCardSettingRepository:
    """SQLAlchemy repository for explicit supplementary-card mappings."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def mark(
        self,
        *,
        account_id: int,
        institution_key: str,
        card_last_four: str,
    ) -> SupplementaryCardSetting:
        """Idempotently mark one bank and suffix pair as supplementary."""
        key, suffix = self._validated_identity(institution_key, card_last_four)
        model = self._session.scalar(
            select(SupplementaryCardSettingModel).where(
                SupplementaryCardSettingModel.gmail_account_id == account_id,
                SupplementaryCardSettingModel.institution_key == key,
                SupplementaryCardSettingModel.card_last_four == suffix,
            )
        )
        if model is None:
            model = SupplementaryCardSettingModel(
                gmail_account_id=account_id,
                institution_key=key,
                card_last_four=suffix,
            )
            self._session.add(model)
            _flush_and_refresh(self._session, model)
        return _supplementary_card_setting_record(model)

    def unmark(
        self,
        *,
        account_id: int,
        institution_key: str,
        card_last_four: str,
    ) -> bool:
        """Remove one supplementary-card mapping when present."""
        key, suffix = self._validated_identity(institution_key, card_last_four)
        model = self._session.scalar(
            select(SupplementaryCardSettingModel).where(
                SupplementaryCardSettingModel.gmail_account_id == account_id,
                SupplementaryCardSettingModel.institution_key == key,
                SupplementaryCardSettingModel.card_last_four == suffix,
            )
        )
        if model is None:
            return False
        self._session.delete(model)
        self._session.flush()
        return True

    def is_marked(
        self,
        *,
        account_id: int,
        institution_key: str,
        card_last_four: str,
    ) -> bool:
        """Return whether the exact account, bank, and suffix is marked."""
        key, suffix = self._validated_identity(institution_key, card_last_four)
        return (
            self._session.scalar(
                select(SupplementaryCardSettingModel.id).where(
                    SupplementaryCardSettingModel.gmail_account_id == account_id,
                    SupplementaryCardSettingModel.institution_key == key,
                    SupplementaryCardSettingModel.card_last_four == suffix,
                )
            )
            is not None
        )

    def list_all(self) -> tuple[SupplementaryCardSetting, ...]:
        """Return every explicit supplementary-card decision, oldest first."""
        models = self._session.scalars(
            select(SupplementaryCardSettingModel).order_by(
                SupplementaryCardSettingModel.id
            )
        ).all()
        return tuple(_supplementary_card_setting_record(model) for model in models)

    @staticmethod
    def _validated_identity(
        institution_key: str, card_last_four: str
    ) -> tuple[str, str]:
        key = validate_institution_key(institution_key)
        suffix = card_last_four.strip()
        if re.fullmatch(r"[0-9]{4}", suffix) is None:
            raise ValueError("card last four must contain exactly four digits")
        return key, suffix


class SqlAlchemyEmailRepository:
    """SQLAlchemy email repository with Gmail-ID deduplication."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, email_id: int) -> EmailMessage | None:
        """Return an email by local ID."""
        model = self._session.get(EmailModel, email_id)
        return None if model is None else _email_record(model)

    def get_by_gmail_id(
        self, account_id: int, gmail_message_id: str
    ) -> EmailMessage | None:
        """Return an email by account-scoped Gmail message ID."""
        model = self._session.scalar(
            select(EmailModel).where(
                EmailModel.gmail_account_id == account_id,
                EmailModel.gmail_message_id == gmail_message_id,
            )
        )
        return None if model is None else _email_record(model)

    def upsert_from_gmail(self, snapshot: EmailSnapshot) -> EmailUpsertOutcome:
        """Insert or refresh Gmail-owned fields without replacing review state."""
        model = self._session.scalar(
            select(EmailModel).where(
                EmailModel.gmail_account_id == snapshot.gmail_account_id,
                EmailModel.gmail_message_id == snapshot.gmail_message_id,
            )
        )
        created = model is None
        values: dict[str, Any] = {
            "gmail_thread_id": snapshot.gmail_thread_id,
            "gmail_history_id": snapshot.gmail_history_id,
            "sender": snapshot.sender,
            "recipients_json": sorted(set(snapshot.recipients)),
            "subject": snapshot.subject,
            "matched_sender_rule_id": snapshot.matched_sender_rule_id,
            "received_at": snapshot.received_at,
            "gmail_internal_date_ms": snapshot.gmail_internal_date_ms,
            "snippet": snapshot.snippet,
            "body_text": snapshot.body_text,
            "body_summary": snapshot.body_summary,
            "has_html_body": snapshot.has_html_body,
            "labels_json": sorted(set(snapshot.labels)),
            "body_policy_applied": snapshot.body_policy_applied.value,
            "source_status": EmailSourceStatus.ACTIVE.value,
            "source_deleted_at": None,
        }
        if model is None:
            model = EmailModel(
                gmail_account_id=snapshot.gmail_account_id,
                gmail_message_id=snapshot.gmail_message_id,
                **values,
            )
            self._session.add(model)
        else:
            for field, value in values.items():
                setattr(model, field, value)
        _flush_and_refresh(self._session, model)
        return EmailUpsertOutcome(email=_email_record(model), created=created)

    def mark_source_deleted(
        self, account_id: int, gmail_message_id: str, deleted_at: datetime
    ) -> bool:
        """Soft-delete a Gmail source while preserving local evidence."""
        result = cast(
            CursorResult[Any],
            self._session.execute(
                update(EmailModel)
                .where(
                    EmailModel.gmail_account_id == account_id,
                    EmailModel.gmail_message_id == gmail_message_id,
                    EmailModel.source_status != EmailSourceStatus.DELETED.value,
                )
                .values(
                    source_status=EmailSourceStatus.DELETED.value,
                    source_deleted_at=deleted_at,
                )
            ),
        )
        return bool(result.rowcount)

    def list_active_gmail_ids(
        self,
        account_id: int,
        *,
        sender_rule_ids: tuple[int, ...] | None,
    ) -> tuple[str, ...]:
        """List active source identities in a complete reconciliation scope."""
        statement = select(EmailModel.gmail_message_id).where(
            EmailModel.gmail_account_id == account_id,
            EmailModel.source_status == EmailSourceStatus.ACTIVE.value,
        )
        if sender_rule_ids is not None:
            if not sender_rule_ids:
                return ()
            statement = statement.where(
                EmailModel.matched_sender_rule_id.in_(sender_rule_ids)
            )
        return tuple(sorted(self._session.scalars(statement)))

    def apply_processing_result(
        self,
        email_id: int,
        *,
        status: ProcessingStatus,
        classification: ClassificationStatus,
        confidence: Decimal | None,
        reason: str | None,
        parser_name: str | None,
        parser_version: str | None,
        purge_body: bool,
    ) -> EmailMessage:
        """Apply parser summary without replacing Gmail-owned metadata."""
        model = self._session.get(EmailModel, email_id)
        if model is None:
            raise DatabaseError(f"email {email_id} does not exist")
        model.processing_status = status.value
        model.classification_status = classification.value
        model.classification_confidence = confidence
        model.classification_reason = reason
        model.parser_name = parser_name
        model.parser_version = parser_version
        if purge_body:
            model.body_text = None
            model.body_purged_at = datetime.now(UTC)
        _flush_and_refresh(self._session, model)
        return _email_record(model)


class SqlAlchemyGmailLabelOperationRepository:
    """Durable, idempotent outbox for message-level Gmail Label writes."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def enqueue(self, email_id: int, label_name: str) -> GmailLabelOperation:
        """Queue a Label mutation in the caller's database transaction."""
        normalized = _validated_label_name(label_name)
        email = self._session.get(EmailModel, email_id)
        if email is None:
            raise DatabaseError(f"email {email_id} does not exist")
        model = self._session.scalar(
            select(GmailLabelOperationModel).where(
                GmailLabelOperationModel.email_id == email_id,
                GmailLabelOperationModel.label_name == normalized,
            )
        )
        if model is None:
            model = GmailLabelOperationModel(
                gmail_account_id=email.gmail_account_id,
                email_id=email.id,
                gmail_message_id=email.gmail_message_id,
                label_name=normalized,
            )
            self._session.add(model)
        elif model.status == GmailLabelOperationStatus.FAILED.value:
            model.status = GmailLabelOperationStatus.PENDING.value
            model.error_code = None
            model.error_message = None
        _flush_and_refresh(self._session, model)
        return _gmail_label_operation_record(model)

    def enqueue_processed(self, account_id: int, label_name: str) -> int:
        """Queue active, successfully processed messages not already succeeded."""
        normalized = _validated_label_name(label_name)
        emails = tuple(
            self._session.scalars(
                select(EmailModel).where(
                    EmailModel.gmail_account_id == account_id,
                    EmailModel.processing_status == ProcessingStatus.PROCESSED.value,
                    EmailModel.source_status == EmailSourceStatus.ACTIVE.value,
                )
            )
        )
        existing = {
            model.email_id: model
            for model in self._session.scalars(
                select(GmailLabelOperationModel).where(
                    GmailLabelOperationModel.gmail_account_id == account_id,
                    GmailLabelOperationModel.label_name == normalized,
                )
            )
        }
        queued = 0
        for email in emails:
            model = existing.get(email.id)
            if model is None:
                self._session.add(
                    GmailLabelOperationModel(
                        gmail_account_id=account_id,
                        email_id=email.id,
                        gmail_message_id=email.gmail_message_id,
                        label_name=normalized,
                    )
                )
                queued += 1
            elif model.status == GmailLabelOperationStatus.FAILED.value:
                model.status = GmailLabelOperationStatus.PENDING.value
                model.error_code = None
                model.error_message = None
                queued += 1
        try:
            self._session.flush()
        except SQLAlchemyError as error:
            raise DatabaseError("Gmail Label outbox write failed") from error
        return queued

    def list_processable(
        self, account_id: int, label_name: str, limit: int
    ) -> tuple[GmailLabelOperation, ...]:
        """Return pending operations in stable order for a bounded API batch."""
        if not 1 <= limit <= 1000:
            raise ValueError("Gmail Label batch size must be between 1 and 1000")
        models = self._session.scalars(
            select(GmailLabelOperationModel)
            .where(
                GmailLabelOperationModel.gmail_account_id == account_id,
                GmailLabelOperationModel.label_name
                == _validated_label_name(label_name),
                GmailLabelOperationModel.status
                == GmailLabelOperationStatus.PENDING.value,
            )
            .order_by(GmailLabelOperationModel.id)
            .limit(limit)
        )
        return tuple(_gmail_label_operation_record(model) for model in models)

    def mark_succeeded(self, operation_ids: tuple[int, ...]) -> None:
        """Mark one successfully applied API batch."""
        self._mark(operation_ids, status=GmailLabelOperationStatus.SUCCEEDED)

    def mark_failed(
        self,
        operation_ids: tuple[int, ...],
        *,
        error_code: str,
        error_message: str,
        retryable: bool,
    ) -> None:
        """Persist a safe API failure and retain transient work as pending."""
        self._mark(
            operation_ids,
            status=(
                GmailLabelOperationStatus.PENDING
                if retryable
                else GmailLabelOperationStatus.FAILED
            ),
            error_code=error_code[:100],
            error_message=error_message[:2000],
        )

    def counts(self, account_id: int, label_name: str) -> dict[str, int]:
        """Return status counts without exposing message data."""
        rows = self._session.execute(
            select(
                GmailLabelOperationModel.status,
                func.count(GmailLabelOperationModel.id),
            )
            .where(
                GmailLabelOperationModel.gmail_account_id == account_id,
                GmailLabelOperationModel.label_name
                == _validated_label_name(label_name),
            )
            .group_by(GmailLabelOperationModel.status)
        )
        return {str(status): int(count) for status, count in rows}

    def _mark(
        self,
        operation_ids: tuple[int, ...],
        *,
        status: GmailLabelOperationStatus,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        if not operation_ids:
            return
        now = datetime.now(UTC)
        self._session.execute(
            update(GmailLabelOperationModel)
            .where(GmailLabelOperationModel.id.in_(operation_ids))
            .values(
                status=status.value,
                attempt_count=GmailLabelOperationModel.attempt_count + 1,
                error_code=error_code,
                error_message=error_message,
                last_attempt_at=now,
                updated_at=now,
            )
        )


def _validated_label_name(label_name: str) -> str:
    normalized = label_name.strip()
    if not normalized or len(normalized) > 200:
        raise ValueError("Gmail Label name must contain 1 to 200 characters")
    if any(ord(character) < 32 for character in normalized):
        raise ValueError("Gmail Label name cannot contain control characters")
    return normalized


class SqlAlchemyAttachmentRepository:
    """SQLAlchemy attachment metadata repository."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_for_email(self, email_id: int) -> tuple[EmailAttachment, ...]:
        """Return attachment metadata in MIME part order."""
        models = self._session.scalars(
            select(EmailAttachmentModel)
            .where(EmailAttachmentModel.email_id == email_id)
            .order_by(EmailAttachmentModel.id)
        )
        return tuple(_attachment_record(model) for model in models)

    def replace_for_email(
        self, email_id: int, snapshots: tuple[AttachmentSnapshot, ...]
    ) -> tuple[EmailAttachment, ...]:
        """Reconcile current MIME parts while preserving unchanged extraction."""
        if len({item.mime_part_id for item in snapshots}) != len(snapshots):
            raise ValueError("attachment MIME part IDs must be unique")
        existing = {
            model.mime_part_id: model
            for model in self._session.scalars(
                select(EmailAttachmentModel).where(
                    EmailAttachmentModel.email_id == email_id
                )
            )
        }
        retained: list[EmailAttachmentModel] = []
        for snapshot in snapshots:
            if snapshot.size_bytes < 0:
                raise ValueError("attachment size must be non-negative")
            model = existing.pop(snapshot.mime_part_id, None)
            if model is None:
                model = EmailAttachmentModel(
                    email_id=email_id,
                    mime_part_id=snapshot.mime_part_id,
                    filename=snapshot.filename,
                    mime_type=snapshot.mime_type,
                    size_bytes=snapshot.size_bytes,
                    sha256=snapshot.sha256,
                    is_pdf=snapshot.is_pdf,
                    is_encrypted=False,
                    gmail_attachment_id=snapshot.gmail_attachment_id,
                )
                self._session.add(model)
            else:
                content_changed = model.sha256 != snapshot.sha256
                model.filename = snapshot.filename
                model.mime_type = snapshot.mime_type
                model.size_bytes = snapshot.size_bytes
                model.sha256 = snapshot.sha256
                model.is_pdf = snapshot.is_pdf
                model.gmail_attachment_id = snapshot.gmail_attachment_id
                if content_changed:
                    model.is_encrypted = False
                    model.raw_encrypted_content = None
                    model.extracted_text = None
                    model.extraction_status = AttachmentStatus.PENDING.value
                    model.extractor_name = None
                    model.extractor_version = None
                    model.warnings_json = []
                    model.error_code = None
                    model.error_message = None
                    model.password_ref_used = None
                    model.extracted_at = None
                    model.purged_at = None
            retained.append(model)
        for stale in existing.values():
            self._session.delete(stale)
        try:
            self._session.flush()
            for model in retained:
                self._session.refresh(model)
        except SQLAlchemyError as error:
            raise DatabaseError("attachment metadata write failed") from error
        return tuple(_attachment_record(model) for model in retained)

    def replace_processed_for_email(
        self,
        email_id: int,
        snapshots: tuple[ProcessedAttachmentSnapshot, ...],
    ) -> tuple[EmailAttachment, ...]:
        """Reconcile metadata and atomically apply local extraction output."""
        self.replace_for_email(email_id, tuple(item.attachment for item in snapshots))
        by_part = {item.attachment.mime_part_id: item for item in snapshots}
        models = tuple(
            self._session.scalars(
                select(EmailAttachmentModel).where(
                    EmailAttachmentModel.email_id == email_id
                )
            )
        )
        for model in models:
            snapshot = by_part[model.mime_part_id]
            if (
                model.extraction_status == AttachmentStatus.EXTRACTED.value
                and snapshot.extraction_status is not AttachmentStatus.EXTRACTED
            ):
                # ``replace_for_email`` resets changed content to pending. An
                # extracted state here therefore proves the SHA is unchanged;
                # never downgrade good local evidence after a transient failure.
                continue
            if snapshot.raw_encrypted_content is not None and not snapshot.is_encrypted:
                raise ValueError("only encrypted attachment bytes may be retained")
            model.is_encrypted = snapshot.is_encrypted
            model.raw_encrypted_content = snapshot.raw_encrypted_content
            model.extracted_text = snapshot.extracted_text
            model.extraction_status = snapshot.extraction_status.value
            model.extractor_name = snapshot.extractor_name
            model.extractor_version = snapshot.extractor_version
            model.warnings_json = list(snapshot.warnings)
            model.error_code = snapshot.error_code
            model.error_message = snapshot.error_message
            model.password_ref_used = snapshot.password_ref_used
            model.extracted_at = (
                datetime.now(UTC)
                if snapshot.extraction_status is not AttachmentStatus.PENDING
                else None
            )
        try:
            self._session.flush()
            for model in models:
                self._session.refresh(model)
        except SQLAlchemyError as error:
            raise DatabaseError("attachment extraction write failed") from error
        return tuple(_attachment_record(model) for model in models)


class SqlAlchemyTransactionRepository:
    """SQLAlchemy transaction repository."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, draft: TransactionDraft) -> Transaction:
        """Persist a parser draft with identical parsed/effective values."""
        _validate_transaction_draft(draft)
        transaction_type = draft.transaction_type.value
        evidence = list(draft.extraction_evidence)
        warnings = list(draft.parse_warnings)
        model = TransactionModel(
            email_id=draft.email_id,
            source_key=draft.source_key,
            parsed_transaction_type=transaction_type,
            parsed_merchant=draft.merchant,
            parsed_original_merchant_text=draft.original_merchant_text,
            parsed_amount_minor=draft.amount_minor,
            parsed_currency=draft.currency,
            parsed_card_last_four=draft.card_last_four,
            parsed_account_last_four=draft.account_last_four,
            parsed_transaction_at=draft.transaction_at,
            parsed_posted_at=draft.posted_at,
            parsed_authorization_code=draft.authorization_code,
            transaction_type=transaction_type,
            merchant=draft.merchant,
            original_merchant_text=draft.original_merchant_text,
            amount_minor=draft.amount_minor,
            currency=draft.currency,
            card_last_four=draft.card_last_four,
            account_last_four=draft.account_last_four,
            transaction_at=draft.transaction_at,
            posted_at=draft.posted_at,
            authorization_code=draft.authorization_code,
            confidence=draft.confidence,
            parser_name=draft.parser_name,
            parser_version=draft.parser_version,
            extraction_evidence_json=evidence,
            parse_warnings_json=warnings,
        )
        self._session.add(model)
        _flush_and_refresh(self._session, model)
        return _transaction_record(model)

    def get(self, transaction_id: int) -> Transaction | None:
        """Return a transaction by ID."""
        model = self._session.get(TransactionModel, transaction_id)
        return None if model is None else _transaction_record(model)

    def list_for_email(self, email_id: int) -> tuple[Transaction, ...]:
        """Return transactions for an email in stable insertion order."""
        models = self._session.scalars(
            select(TransactionModel)
            .where(TransactionModel.email_id == email_id)
            .order_by(TransactionModel.id)
        )
        return tuple(_transaction_record(model) for model in models)

    def reconcile(
        self, email_id: int, drafts: tuple[TransactionDraft, ...]
    ) -> tuple[Transaction, ...]:
        """Refresh parsed fields while preserving explicit effective overrides."""
        keys = [draft.source_key for draft in drafts]
        if len(keys) != len(set(keys)):
            raise ValueError("transaction source keys must be unique per email")
        existing = {
            model.source_key: model
            for model in self._session.scalars(
                select(TransactionModel).where(TransactionModel.email_id == email_id)
            )
        }
        retained: list[TransactionModel] = []
        for draft in drafts:
            if draft.email_id != email_id:
                raise ValueError("transaction draft email IDs must match")
            _validate_transaction_draft(draft)
            model = existing.pop(draft.source_key, None)
            if model is None:
                self.add(draft)
                model = self._session.scalar(
                    select(TransactionModel).where(
                        TransactionModel.email_id == email_id,
                        TransactionModel.source_key == draft.source_key,
                    )
                )
                if model is None:  # pragma: no cover
                    raise DatabaseError("new transaction disappeared")
            else:
                for field, value in _parsed_transaction_values(draft).items():
                    setattr(model, f"parsed_{field}", value)
                    if field not in model.manual_overrides_json:
                        setattr(model, field, value)
                model.confidence = draft.confidence
                model.parser_name = draft.parser_name
                model.parser_version = draft.parser_version
                model.extraction_evidence_json = list(draft.extraction_evidence)
                model.parse_warnings_json = list(draft.parse_warnings)
                model.source_status = TransactionSourceStatus.ACTIVE.value
            retained.append(model)
        for stale in existing.values():
            stale.source_status = TransactionSourceStatus.SUPERSEDED.value
        try:
            self._session.flush()
            for model in retained:
                self._session.refresh(model)
        except SQLAlchemyError as error:
            raise DatabaseError("transaction reconciliation failed") from error
        return tuple(_transaction_record(model) for model in retained)

    def apply_review(self, draft: TransactionReviewDraft) -> Transaction:
        """Atomically replace explicit overrides and review flags."""
        allowed = {
            "transaction_type",
            "merchant",
            "amount_minor",
            "currency",
            "transaction_at",
        }
        if not draft.override_fields <= allowed:
            raise ValueError("unsupported transaction override field")
        if draft.amount_minor is not None and draft.amount_minor < 0:
            raise ValueError("amount_minor must be non-negative")
        if draft.currency is not None and (
            len(draft.currency) != 3
            or not draft.currency.isalpha()
            or draft.currency != draft.currency.upper()
        ):
            raise ValueError("currency must be a three-letter uppercase code")
        if draft.manually_confirmed and draft.manually_ignored:
            raise ValueError("a transaction cannot be confirmed and ignored")

        model = self._session.get(TransactionModel, draft.transaction_id)
        if model is None:
            raise DatabaseError("transaction was not found")
        if model.updated_at != draft.expected_updated_at:
            raise ConcurrentUpdateError(
                "transaction changed after this page was loaded"
            )

        values: dict[str, Any] = {
            "transaction_type": draft.transaction_type.value,
            "merchant": draft.merchant,
            "amount_minor": draft.amount_minor,
            "currency": draft.currency,
            "transaction_at": draft.transaction_at,
        }
        overrides: dict[str, JsonValue] = {}
        for field, value in values.items():
            if field in draft.override_fields:
                setattr(model, field, value)
                overrides[field] = (
                    value.isoformat() if isinstance(value, datetime) else value
                )
            else:
                setattr(model, field, getattr(model, f"parsed_{field}"))
        reviewed_at = datetime.now(UTC)
        model.manual_overrides_json = overrides
        model.overridden_at = reviewed_at if overrides else None
        model.manually_confirmed = draft.manually_confirmed
        model.manually_ignored = draft.manually_ignored
        model.user_note = draft.user_note
        model.updated_at = reviewed_at
        _flush_and_refresh(self._session, model)
        return _transaction_record(model)

    def confirm_pending(self, transaction_ids: frozenset[int]) -> int:
        """Confirm selected pending active rows without changing transaction values."""
        if not transaction_ids:
            return 0
        reviewed_at = datetime.now(UTC)
        try:
            result = cast(
                CursorResult[Any],
                self._session.execute(
                    update(TransactionModel)
                    .where(
                        TransactionModel.id.in_(transaction_ids),
                        TransactionModel.source_status
                        == TransactionSourceStatus.ACTIVE.value,
                        TransactionModel.manually_confirmed.is_(False),
                        TransactionModel.manually_ignored.is_(False),
                    )
                    .values(
                        manually_confirmed=True,
                        updated_at=reviewed_at,
                    )
                ),
            )
            self._session.flush()
        except SQLAlchemyError as error:
            raise DatabaseError("transaction confirmation failed") from error
        return result.rowcount

    def list_confirmed_ids(self) -> tuple[int, ...]:
        """Return every active, confirmed, non-ignored transaction ID."""
        rows = self._session.scalars(
            select(TransactionModel.id)
            .where(
                TransactionModel.source_status == TransactionSourceStatus.ACTIVE.value,
                TransactionModel.manually_confirmed.is_(True),
                TransactionModel.manually_ignored.is_(False),
            )
            .order_by(TransactionModel.id)
        ).all()
        return tuple(rows)


def _parsed_transaction_values(draft: TransactionDraft) -> dict[str, Any]:
    return {
        "transaction_type": draft.transaction_type.value,
        "merchant": draft.merchant,
        "original_merchant_text": draft.original_merchant_text,
        "amount_minor": draft.amount_minor,
        "currency": draft.currency,
        "card_last_four": draft.card_last_four,
        "account_last_four": draft.account_last_four,
        "transaction_at": draft.transaction_at,
        "posted_at": draft.posted_at,
        "authorization_code": draft.authorization_code,
    }


class SqlAlchemyParserRunRepository:
    """SQLAlchemy parser-attempt repository."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def start(
        self,
        *,
        email_id: int,
        sync_run_id: str | None,
        trigger: ParserTrigger,
    ) -> int:
        """Start one parser run in the current transaction."""
        model = ParserRunModel(
            email_id=email_id,
            sync_run_id=sync_run_id,
            trigger=trigger.value,
            status=ParserRunStatus.RUNNING.value,
        )
        self._session.add(model)
        _flush_and_refresh(self._session, model)
        return model.id

    def succeed(self, parser_run_id: int, result: PipelineResult) -> None:
        """Finalize one successful parser run."""
        model = self._require(parser_run_id)
        model.status = ParserRunStatus.SUCCEEDED.value
        model.parser_name = result.parser_name
        model.parser_version = result.parser_version
        model.candidate_count = len(result.candidates)
        model.warnings_json = [warning.as_json() for warning in result.warnings]
        model.finished_at = datetime.now(UTC)
        _flush_and_refresh(self._session, model)

    def fail(
        self,
        parser_run_id: int,
        *,
        error_code: str,
        error_message: str,
        debug_trace: str,
    ) -> None:
        """Finalize one failed parser run with safe diagnostics."""
        model = self._require(parser_run_id)
        model.status = ParserRunStatus.FAILED.value
        model.error_code = error_code
        model.error_message = error_message
        model.debug_trace = debug_trace
        model.finished_at = datetime.now(UTC)
        _flush_and_refresh(self._session, model)

    def _require(self, parser_run_id: int) -> ParserRunModel:
        model = self._session.get(ParserRunModel, parser_run_id)
        if model is None:
            raise DatabaseError(f"parser run {parser_run_id} does not exist")
        return model


class SqlAlchemyClassificationResultRepository:
    """SQLAlchemy immutable classification-stage repository."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add_many(
        self,
        *,
        email_id: int,
        parser_run_id: int,
        stages: tuple[StageResult, ...],
    ) -> None:
        """Append every stage from one pipeline evaluation."""
        for stage in stages:
            self._session.add(
                ClassificationResultModel(
                    email_id=email_id,
                    parser_run_id=parser_run_id,
                    stage_name=stage.stage_name,
                    stage_version=stage.stage_version,
                    verdict=stage.verdict.value,
                    confidence=stage.confidence,
                    reason_code=stage.reason_code,
                    reason=stage.reason,
                    extracted_fields_json=dict(stage.extracted_fields),
                    evidence_json=[item.as_json() for item in stage.evidence],
                )
            )
        try:
            self._session.flush()
        except SQLAlchemyError as error:
            raise DatabaseError("classification stage write failed") from error


def _validate_transaction_draft(draft: TransactionDraft) -> None:
    if draft.amount_minor is not None and draft.amount_minor < 0:
        raise ValueError("amount_minor must be non-negative")
    if draft.currency is not None and (
        len(draft.currency) != 3
        or not draft.currency.isalpha()
        or draft.currency != draft.currency.upper()
    ):
        raise ValueError("currency must be a three-letter uppercase code")
    for name, value in (
        ("card_last_four", draft.card_last_four),
        ("account_last_four", draft.account_last_four),
    ):
        if value is not None and (len(value) != 4 or not value.isalnum()):
            raise ValueError(f"{name} must contain exactly four letters or digits")
    if not Decimal(0) <= draft.confidence <= Decimal(1):
        raise ValueError("confidence must be between zero and one")


class SqlAlchemySyncStateRepository:
    """SQLAlchemy sync-state repository with optimistic concurrency."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, account_id: int) -> SyncState | None:
        """Return an account cursor without creating one during a read."""
        model = self._session.get(SyncStateModel, account_id)
        return None if model is None else _sync_state_record(model)

    def get_or_create(self, account_id: int) -> SyncState:
        """Return the account checkpoint, creating its initial state if absent."""
        model = self._session.get(SyncStateModel, account_id)
        if model is None:
            model = SyncStateModel(gmail_account_id=account_id)
            self._session.add(model)
            _flush_and_refresh(self._session, model)
        return _sync_state_record(model)

    def save(self, state: SyncState, *, expected_version: int) -> SyncState:
        """Persist a checkpoint only if its expected version remains current."""
        next_version = expected_version + 1
        result = cast(
            CursorResult[Any],
            self._session.execute(
                update(SyncStateModel)
                .where(
                    SyncStateModel.gmail_account_id == state.gmail_account_id,
                    SyncStateModel.version == expected_version,
                )
                .values(
                    committed_history_id=state.committed_history_id,
                    baseline_history_id=state.baseline_history_id,
                    initial_query=state.initial_query,
                    query_fingerprint=state.query_fingerprint,
                    query_semantics_version=state.query_semantics_version,
                    full_sync_next_page_token=state.full_sync_next_page_token,
                    full_sync_status=state.full_sync_status.value,
                    reconciliation_required=state.reconciliation_required,
                    last_successful_sync_at=state.last_successful_sync_at,
                    version=next_version,
                )
            ),
        )
        if result.rowcount != 1:
            raise ConcurrentUpdateError(
                f"sync state for account {state.gmail_account_id} changed"
            )
        try:
            self._session.flush()
        except SQLAlchemyError as error:
            raise DatabaseError("sync state write failed") from error
        model = self._session.get(
            SyncStateModel,
            state.gmail_account_id,
            populate_existing=True,
        )
        if model is None:
            raise ConcurrentUpdateError(
                f"sync state for account {state.gmail_account_id} disappeared"
            )
        return _sync_state_record(model)


class SqlAlchemySyncRunRepository:
    """SQLAlchemy run queue and progress repository."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        account_id: int,
        mode: SyncMode,
        query: str | None,
        start_history_id: str,
    ) -> SyncRun:
        """Create a running sync with a durable baseline history ID."""
        now = datetime.now(UTC)
        model = SyncRunModel(
            id=str(uuid4()),
            gmail_account_id=account_id,
            mode=mode.value,
            status=RunStatus.RUNNING.value,
            query=query,
            start_history_id=start_history_id,
            started_at=now,
            heartbeat_at=now,
        )
        try:
            with self._session.begin_nested():
                self._session.add(model)
                self._session.flush()
        except IntegrityError as error:
            raise SyncAlreadyRunningError(
                "Another running sync already exists for this Gmail account"
            ) from error
        self._session.refresh(model)
        return _sync_run_record(model)

    def find_any_running(self, account_id: int) -> SyncRun | None:
        """Return the newest unfinished run for concurrency/scope checks."""
        model = self._session.scalar(
            select(SyncRunModel)
            .where(
                SyncRunModel.gmail_account_id == account_id,
                SyncRunModel.status == RunStatus.RUNNING.value,
            )
            .order_by(SyncRunModel.created_at.desc())
            .limit(1)
        )
        return None if model is None else _sync_run_record(model)

    def get(self, run_id: str) -> SyncRun | None:
        """Return a run by UUID."""
        model = self._session.get(SyncRunModel, run_id)
        return None if model is None else _sync_run_record(model)

    def list_recent(self, *, limit: int) -> tuple[SyncRun, ...]:
        """Return recent runs for local progress and audit views."""
        if limit < 1:
            raise ValueError("sync run limit must be positive")
        models = self._session.scalars(
            select(SyncRunModel).order_by(SyncRunModel.created_at.desc()).limit(limit)
        )
        return tuple(_sync_run_record(model) for model in models)

    def acquire_lease(
        self,
        run_id: str,
        *,
        worker_id: str,
        now: datetime,
        expires_at: datetime,
    ) -> bool:
        """Atomically claim an unowned, re-entrant, or expired running lease."""
        if not worker_id.strip() or expires_at <= now:
            raise ValueError("worker lease identity and expiry must be valid")
        result = cast(
            CursorResult[Any],
            self._session.execute(
                update(SyncRunModel)
                .where(
                    SyncRunModel.id == run_id,
                    SyncRunModel.status == RunStatus.RUNNING.value,
                    or_(
                        SyncRunModel.worker_id.is_(None),
                        SyncRunModel.worker_id == worker_id,
                        SyncRunModel.lease_expires_at.is_(None),
                        SyncRunModel.lease_expires_at <= now,
                    ),
                )
                .values(
                    worker_id=worker_id,
                    heartbeat_at=now,
                    lease_expires_at=expires_at,
                )
            ),
        )
        return bool(result.rowcount)

    def renew_lease(
        self,
        run_id: str,
        *,
        worker_id: str,
        now: datetime,
        expires_at: datetime,
    ) -> bool:
        """Extend a lease only while the same worker still owns it."""
        if expires_at <= now:
            raise ValueError("worker lease expiry must be after heartbeat")
        result = cast(
            CursorResult[Any],
            self._session.execute(
                update(SyncRunModel)
                .where(
                    SyncRunModel.id == run_id,
                    SyncRunModel.status == RunStatus.RUNNING.value,
                    SyncRunModel.worker_id == worker_id,
                )
                .values(heartbeat_at=now, lease_expires_at=expires_at)
            ),
        )
        return bool(result.rowcount)

    def release_lease(self, run_id: str, *, worker_id: str) -> None:
        """Release a lease without disturbing another worker's ownership."""
        self._session.execute(
            update(SyncRunModel)
            .where(
                SyncRunModel.id == run_id,
                SyncRunModel.worker_id == worker_id,
            )
            .values(worker_id=None, lease_expires_at=None)
        )

    def request_cancel(self, run_id: str) -> bool:
        """Request cooperative cancellation for a currently running sync."""
        result = cast(
            CursorResult[Any],
            self._session.execute(
                update(SyncRunModel)
                .where(
                    SyncRunModel.id == run_id,
                    SyncRunModel.status == RunStatus.RUNNING.value,
                )
                .values(cancel_requested=True)
            ),
        )
        return bool(result.rowcount)

    def cancellation_requested(self, run_id: str) -> bool:
        """Return whether a running worker should stop at its next boundary."""
        requested = self._session.scalar(
            select(SyncRunModel.cancel_requested).where(SyncRunModel.id == run_id)
        )
        return bool(requested)

    def enqueue_message_ids(self, run_id: str, message_ids: tuple[str, ...]) -> int:
        """Insert unseen message IDs as fetch items and return the new count."""
        return self.enqueue_changes(
            run_id,
            tuple((message_id, SyncItemOperation.FETCH) for message_id in message_ids),
        )

    def enqueue_changes(
        self,
        run_id: str,
        changes: tuple[tuple[str, SyncItemOperation], ...],
    ) -> int:
        """Merge history changes to one durable item per Gmail message."""
        precedence = {
            SyncItemOperation.UPDATE: 1,
            SyncItemOperation.FETCH: 2,
            SyncItemOperation.DELETE: 3,
        }
        merged: dict[str, SyncItemOperation] = {}
        for message_id, operation in changes:
            if not message_id:
                raise ValueError("Gmail message ID cannot be empty")
            current = merged.get(message_id)
            if current is None or precedence[operation] > precedence[current]:
                merged[message_id] = operation
        if not merged:
            return 0
        existing = {
            model.gmail_message_id: model
            for model in self._session.scalars(
                select(SyncRunItemModel).where(
                    SyncRunItemModel.sync_run_id == run_id,
                    SyncRunItemModel.gmail_message_id.in_(tuple(merged)),
                )
            )
        }
        inserted = 0
        for message_id, operation in sorted(merged.items()):
            model = existing.get(message_id)
            if model is None:
                self._session.add(
                    SyncRunItemModel(
                        sync_run_id=run_id,
                        gmail_message_id=message_id,
                        operation=operation.value,
                    )
                )
                inserted += 1
                continue
            current_operation = SyncItemOperation(model.operation)
            if precedence[operation] > precedence[current_operation]:
                model.operation = operation.value
        run = self._require_run(run_id)
        run.discovered_count += inserted
        run.heartbeat_at = datetime.now(UTC)
        try:
            self._session.flush()
        except SQLAlchemyError as error:
            raise DatabaseError("sync items could not be enqueued") from error
        return inserted

    def checkpoint_page(
        self,
        run_id: str,
        *,
        next_page_token: str | None,
        end_history_id: str | None = None,
    ) -> SyncRun:
        """Advance one durable list/history-page checkpoint."""
        model = self._require_run(run_id)
        model.next_page_token = next_page_token
        if end_history_id is not None:
            model.end_history_id = end_history_id
        model.pages_processed += 1
        model.heartbeat_at = datetime.now(UTC)
        _flush_and_refresh(self._session, model)
        return _sync_run_record(model)

    def restart_listing(self, run_id: str) -> SyncRun:
        """Discard a rejected page token while retaining deduplicated items."""
        model = self._require_run(run_id)
        model.next_page_token = None
        model.end_history_id = None
        model.pages_processed = 0
        model.retried_count += 1
        model.heartbeat_at = datetime.now(UTC)
        _flush_and_refresh(self._session, model)
        return _sync_run_record(model)

    def list_processable(self, run_id: str, *, limit: int) -> tuple[SyncRunItem, ...]:
        """Return pending items in stable order."""
        if limit < 1:
            raise ValueError("sync work-item limit must be positive")
        models = self._session.scalars(
            select(SyncRunItemModel)
            .where(
                SyncRunItemModel.sync_run_id == run_id,
                SyncRunItemModel.status == SyncItemStatus.PENDING.value,
            )
            .order_by(SyncRunItemModel.id)
            .limit(limit)
        )
        return tuple(_sync_item_record(model) for model in models)

    def list_message_ids(self, run_id: str) -> tuple[str, ...]:
        """Return every deduplicated source identity discovered for a run."""
        return tuple(
            self._session.scalars(
                select(SyncRunItemModel.gmail_message_id)
                .where(SyncRunItemModel.sync_run_id == run_id)
                .order_by(SyncRunItemModel.gmail_message_id)
            )
        )

    def mark_succeeded(
        self,
        item_id: int,
        *,
        created: bool,
        parsed: bool = False,
        parser_failed: bool = False,
        deleted: bool = False,
    ) -> None:
        """Complete one item and update new/updated counters once."""
        item = self._require_item(item_id)
        if item.status == SyncItemStatus.SUCCEEDED.value:
            return
        item.status = SyncItemStatus.SUCCEEDED.value
        item.attempt_count += 1
        item.error_code = None
        item.error_message = None
        item.retryable = False
        item.completed_at = datetime.now(UTC)
        run = self._require_run(item.sync_run_id)
        if deleted:
            run.deleted_count += 1
        elif created:
            run.new_count += 1
        else:
            run.updated_count += 1
        if parsed:
            run.parsed_count += 1
        if parser_failed:
            run.parser_failed_count += 1
        run.heartbeat_at = datetime.now(UTC)

    def mark_skipped(self, item_id: int) -> None:
        """Complete an item rejected by the local sender gate."""
        item = self._require_item(item_id)
        item.status = SyncItemStatus.SKIPPED.value
        item.attempt_count += 1
        item.retryable = False
        item.completed_at = datetime.now(UTC)

    def mark_failed(
        self,
        item_id: int,
        *,
        error_code: str,
        error_message: str,
        retryable: bool,
    ) -> None:
        """Persist a bounded, non-sensitive per-message failure."""
        item = self._require_item(item_id)
        item.status = SyncItemStatus.FAILED.value
        item.attempt_count += 1
        item.error_code = error_code[:100]
        item.error_message = error_message[:2000]
        item.retryable = retryable
        item.completed_at = datetime.now(UTC)
        self._require_run(item.sync_run_id).heartbeat_at = datetime.now(UTC)

    def failed_count(self, run_id: str) -> int:
        """Return all failed items, including non-retryable failures."""
        count = self._session.scalar(
            select(func.count())
            .select_from(SyncRunItemModel)
            .where(
                SyncRunItemModel.sync_run_id == run_id,
                SyncRunItemModel.status == SyncItemStatus.FAILED.value,
            )
        )
        return int(count or 0)

    def record_error(
        self,
        run_id: str,
        *,
        error_code: str,
        error_message: str,
        debug_trace: str | None,
    ) -> None:
        """Record a resumable run error without discarding checkpoints."""
        model = self._require_run(run_id)
        model.error_code = error_code[:100]
        model.error_message = error_message[:2000]
        model.debug_trace = debug_trace[:20_000] if debug_trace else None
        model.heartbeat_at = datetime.now(UTC)

    def finish(
        self,
        run_id: str,
        *,
        status: RunStatus,
        end_history_id: str | None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> SyncRun:
        """Finish an auditable run after all immediately processable work."""
        if status not in {
            RunStatus.SUCCEEDED,
            RunStatus.PARTIAL,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }:
            raise ValueError("run can only finish in a terminal status")
        model = self._require_run(run_id)
        model.status = status.value
        model.end_history_id = end_history_id
        model.error_code = error_code
        model.error_message = error_message
        model.finished_at = datetime.now(UTC)
        model.heartbeat_at = model.finished_at
        model.worker_id = None
        model.lease_expires_at = None
        _flush_and_refresh(self._session, model)
        return _sync_run_record(model)

    def _require_run(self, run_id: str) -> SyncRunModel:
        model = self._session.get(SyncRunModel, run_id)
        if model is None:
            raise DatabaseError(f"sync run {run_id} does not exist")
        return model

    def _require_item(self, item_id: int) -> SyncRunItemModel:
        model = self._session.get(SyncRunItemModel, item_id)
        if model is None:
            raise DatabaseError(f"sync item {item_id} does not exist")
        return model


class SqlAlchemyAppSettingRepository:
    """SQLAlchemy non-secret setting repository."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, key: str) -> AppSetting | None:
        """Return a preference by key."""
        model = self._session.get(AppSettingModel, key)
        return None if model is None else self._record(model)

    def set(self, key: str, value: JsonValue, *, schema_version: int = 1) -> AppSetting:
        """Insert or replace a non-secret preference."""
        if not key.strip():
            raise ValueError("setting key cannot be empty")
        if _SECRET_SETTING_KEY.search(key):
            raise ValueError("secrets cannot be stored in app_settings")
        if schema_version < 1:
            raise ValueError("schema_version must be positive")
        model = self._session.get(AppSettingModel, key)
        if model is None:
            model = AppSettingModel(
                key=key,
                value_json=value,
                schema_version=schema_version,
            )
            self._session.add(model)
        else:
            model.value_json = value
            model.schema_version = schema_version
        _flush_and_refresh(self._session, model)
        return self._record(model)

    @staticmethod
    def _record(model: AppSettingModel) -> AppSetting:
        return AppSetting(
            key=model.key,
            value=cast(JsonValue, model.value_json),
            schema_version=model.schema_version,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )

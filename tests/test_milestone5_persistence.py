"""Milestone 5 application and persistence integration tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from app.application.services.email_processing import PrepareEmailProcessing
from app.application.services.sender_rule_management import ManageSenderRules
from app.domain.enums import (
    AttachmentStatus,
    BodyRetentionPolicy,
    ParserTrigger,
    ProcessingStatus,
    SenderMatchType,
    TransactionSourceStatus,
    TransactionType,
)
from app.domain.errors import SecretStoreError
from app.domain.gmail import DecodedAttachment, DecodedEmail
from app.domain.models import (
    AttachmentSnapshot,
    EmailSnapshot,
    ProcessedAttachmentSnapshot,
    SenderRule,
    SenderRuleDraft,
    TransactionDraft,
)
from app.domain.parsing import ExtractionLimits, SecretValue
from app.infrastructure.attachments.pdf import LocalPdfExtractor
from app.infrastructure.database.engine import Database
from app.infrastructure.database.models import (
    ClassificationResultModel,
    ParserRunModel,
    TransactionModel,
)
from app.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork
from app.infrastructure.parsers import (
    ClassificationPipeline,
    DefaultParserRegistry,
    FubonTransactionParser,
    GenericTransactionParser,
)
from sqlalchemy import func, select

PDF_FIXTURE = (
    Path(__file__).parent / "fixtures" / "pdf" / "fubon_purchase_encrypted.pdf"
)


class _MemorySecrets:
    def __init__(self) -> None:
        self.values: dict[str, SecretValue] = {}

    def get(self, reference: str) -> SecretValue | None:
        return self.values.get(reference)

    def set(self, reference: str, value: SecretValue) -> None:
        self.values[reference] = value

    def delete(self, reference: str) -> None:
        self.values.pop(reference, None)


def _processor(secrets: _MemorySecrets) -> PrepareEmailProcessing:
    return PrepareEmailProcessing(
        pipeline=ClassificationPipeline(
            DefaultParserRegistry(
                (FubonTransactionParser(), GenericTransactionParser())
            ),
            confirmed_threshold=Decimal("0.90"),
        ),
        attachment_extractor=LocalPdfExtractor(),
        secret_store=secrets,
        extraction_limits=ExtractionLimits(
            max_bytes=1024 * 1024,
            max_pages=10,
            max_text_chars=100_000,
            max_password_attempts=3,
            max_seconds=5,
        ),
    )


def _decoded_pdf() -> DecodedEmail:
    return DecodedEmail(
        message_id="pdf-message",
        thread_id="pdf-thread",
        history_id="700",
        internal_date_ms=1_753_400_000_000,
        sender="fixture@bank.invalid",
        recipients=("user@example.invalid",),
        subject="Encrypted fixture notice",
        received_at=datetime(2026, 7, 25, tzinfo=UTC),
        snippet="Encrypted attachment",
        text_body="Please see the attached purchase notification.",
        has_html_body=False,
        label_ids=("INBOX",),
        attachments=(
            DecodedAttachment(
                mime_part_id="1",
                filename="notice.pdf",
                mime_type="application/pdf",
                gmail_attachment_id="gmail-attachment",
                content=PDF_FIXTURE.read_bytes(),
            ),
        ),
        warnings=(),
    )


def _account_and_rule(database: Database) -> tuple[int, SenderRule]:
    with SqlAlchemyUnitOfWork(database.session_factory) as unit:
        account = unit.accounts.get_or_create("user@example.invalid")
        rule = unit.sender_rules.upsert(
            SenderRuleDraft(
                gmail_account_id=account.id,
                institution_key="fubon",
                institution_name="匿名測試銀行",
                match_type=SenderMatchType.EXACT_ADDRESS,
                match_value="fixture@bank.invalid",
                parser_name="banks.fubon",
            )
        )
        unit.commit()
        return account.id, rule


def test_encrypted_pdf_processing_uses_rule_scoped_secret(
    migrated_database: Database,
) -> None:
    _, rule_value = _account_and_rule(migrated_database)
    rule = rule_value
    secrets = _MemorySecrets()
    secrets.set("fixture-ref", SecretValue("fixture-only-password"))
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        updated = unit.sender_rules.set_pdf_password_refs(rule.id, ("fixture-ref",))
        assert updated is not None
        unit.commit()

    prepared = _processor(secrets).execute(_decoded_pdf(), updated)

    assert prepared.succeeded
    assert prepared.pipeline_result is not None
    assert prepared.pipeline_result.parser_name == "banks.fubon"
    assert prepared.pipeline_result.candidates[0].amount_minor == 128_000
    attachment = prepared.attachments[0]
    assert attachment.extraction_status is AttachmentStatus.EXTRACTED
    assert attachment.is_encrypted
    assert attachment.raw_encrypted_content == PDF_FIXTURE.read_bytes()
    assert attachment.password_ref_used == "fixture-ref"


def test_processing_translates_secret_store_failure_to_safe_result(
    migrated_database: Database,
) -> None:
    _, rule_value = _account_and_rule(migrated_database)

    class _BrokenSecrets(_MemorySecrets):
        def get(self, reference: str) -> SecretValue | None:
            del reference
            raise SecretStoreError("fixture keyring unavailable")

    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        updated = unit.sender_rules.set_pdf_password_refs(
            rule_value.id, ("fixture-ref",)
        )
        assert updated is not None
        unit.commit()

    prepared = _processor(_BrokenSecrets()).execute(_decoded_pdf(), updated)

    assert not prepared.succeeded
    assert prepared.error_code == "SecretStoreError"
    assert prepared.error_message == "fixture keyring unavailable"
    assert (
        prepared.attachments[0].extraction_status is AttachmentStatus.PASSWORD_REQUIRED
    )
    assert prepared.attachments[0].raw_encrypted_content == PDF_FIXTURE.read_bytes()


def test_repository_persists_audit_stages_and_preserves_manual_override(
    migrated_database: Database,
) -> None:
    account_id, rule_value = _account_and_rule(migrated_database)
    secrets = _MemorySecrets()
    secrets.set("fixture-ref", SecretValue("fixture-only-password"))
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        rule = unit.sender_rules.set_pdf_password_refs(rule_value.id, ("fixture-ref",))
        assert rule is not None
        unit.commit()
    prepared = _processor(secrets).execute(_decoded_pdf(), rule)
    result = prepared.pipeline_result
    assert result is not None

    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        outcome = unit.emails.upsert_from_gmail(
            EmailSnapshot(
                gmail_account_id=account_id,
                gmail_message_id="pdf-message",
                gmail_thread_id="pdf-thread",
                gmail_history_id="700",
                sender="fixture@bank.invalid",
                recipients=("user@example.invalid",),
                subject="Encrypted fixture notice",
                gmail_internal_date_ms=1_753_400_000_000,
                body_text="temporary source body",
                matched_sender_rule_id=rule.id,
                body_policy_applied=BodyRetentionPolicy.PURGE_AFTER_PARSE,
            )
        )
        email_id = outcome.email.id
        unit.attachments.replace_processed_for_email(email_id, prepared.attachments)
        run_id = unit.parser_runs.start(
            email_id=email_id,
            sync_run_id=None,
            trigger=ParserTrigger.MANUAL,
        )
        unit.classification_results.add_many(
            email_id=email_id,
            parser_run_id=run_id,
            stages=result.stages,
        )
        candidate = result.candidates[0]
        original = TransactionDraft(
            email_id=email_id,
            source_key=candidate.source_key,
            transaction_type=candidate.transaction_type,
            confidence=candidate.confidence,
            parser_name=result.parser_name,
            parser_version=result.parser_version,
            merchant=candidate.merchant,
            amount_minor=candidate.amount_minor,
            currency=candidate.currency,
            transaction_at=candidate.transaction_at,
        )
        transactions = unit.transactions.reconcile(email_id, (original,))
        unit.parser_runs.succeed(run_id, result)
        processed = unit.emails.apply_processing_result(
            email_id,
            status=ProcessingStatus.PROCESSED,
            classification=result.classification,
            confidence=result.confidence,
            reason=result.reason,
            parser_name=result.parser_name,
            parser_version=result.parser_version,
            purge_body=True,
        )
        assert processed.body_text is None
        unit.commit()

    with migrated_database.session_factory() as session:
        model = session.get(TransactionModel, transactions[0].id)
        assert model is not None
        model.merchant = "MANUAL MERCHANT"
        model.manual_overrides_json = {"merchant": "MANUAL MERCHANT"}
        session.commit()

    updated = TransactionDraft(
        **{
            **original.__dict__,
            "merchant": "NEW PARSED MERCHANT",
            "amount_minor": 999_900,
        }
    )
    stale = TransactionDraft(
        email_id=email_id,
        source_key="temporary-second-candidate",
        transaction_type=TransactionType.PURCHASE,
        confidence=Decimal("0.50"),
        parser_name="generic",
        parser_version="1",
        amount_minor=100,
        currency="TWD",
    )
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        unit.transactions.add(stale)
        unit.transactions.reconcile(email_id, (updated,))
        unit.commit()

    with migrated_database.session_factory() as session:
        model = session.get(TransactionModel, transactions[0].id)
        assert model is not None
        assert model.parsed_merchant == "NEW PARSED MERCHANT"
        assert model.merchant == "MANUAL MERCHANT"
        assert model.parsed_amount_minor == 999_900
        assert model.amount_minor == 999_900
        superseded = session.scalar(
            select(TransactionModel).where(
                TransactionModel.source_key == "temporary-second-candidate"
            )
        )
        assert superseded is not None
        assert superseded.source_status == TransactionSourceStatus.SUPERSEDED.value
        assert session.scalar(select(func.count()).select_from(ParserRunModel)) == 1
        assert session.scalar(
            select(func.count()).select_from(ClassificationResultModel)
        ) == len(result.stages)


def test_processed_attachment_rejects_plaintext_raw_bytes(
    migrated_database: Database,
) -> None:
    account_id, _ = _account_and_rule(migrated_database)
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        email = unit.emails.upsert_from_gmail(
            EmailSnapshot(
                gmail_account_id=account_id,
                gmail_message_id="plain",
                gmail_thread_id="thread",
                gmail_history_id="1",
                sender="fixture@bank.invalid",
                recipients=(),
                subject="fixture",
                gmail_internal_date_ms=1,
            )
        ).email
        snapshot = ProcessedAttachmentSnapshot(
            attachment=AttachmentSnapshot(
                mime_part_id="1",
                filename="plain.pdf",
                mime_type="application/pdf",
                size_bytes=4,
                sha256="fixture",
                is_pdf=True,
            ),
            is_encrypted=False,
            extraction_status=AttachmentStatus.EXTRACTED,
            raw_encrypted_content=b"plain",
        )
        with pytest.raises(ValueError, match="encrypted"):
            unit.attachments.replace_processed_for_email(email.id, (snapshot,))


def test_unchanged_attachment_extraction_is_not_downgraded(
    migrated_database: Database,
) -> None:
    account_id, _ = _account_and_rule(migrated_database)
    metadata = AttachmentSnapshot(
        mime_part_id="1",
        filename="notice.pdf",
        mime_type="application/pdf",
        size_bytes=100,
        sha256="unchanged-sha",
        is_pdf=True,
    )
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        email = unit.emails.upsert_from_gmail(
            EmailSnapshot(
                gmail_account_id=account_id,
                gmail_message_id="unchanged",
                gmail_thread_id="thread",
                gmail_history_id="1",
                sender="fixture@bank.invalid",
                recipients=(),
                subject="fixture",
                gmail_internal_date_ms=1,
            )
        ).email
        unit.attachments.replace_processed_for_email(
            email.id,
            (
                ProcessedAttachmentSnapshot(
                    attachment=metadata,
                    is_encrypted=True,
                    extraction_status=AttachmentStatus.EXTRACTED,
                    extracted_text="trusted extracted fixture",
                    raw_encrypted_content=b"encrypted fixture",
                ),
            ),
        )
        unit.commit()

    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        retained = unit.attachments.replace_processed_for_email(
            email.id,
            (
                ProcessedAttachmentSnapshot(
                    attachment=metadata,
                    is_encrypted=True,
                    extraction_status=AttachmentStatus.PASSWORD_REQUIRED,
                    raw_encrypted_content=b"encrypted fixture",
                    error_code="credential_store_locked",
                ),
            ),
        )
        unit.commit()

    assert retained[0].extraction_status is AttachmentStatus.EXTRACTED
    assert retained[0].extracted_text == "trusted extracted fixture"


def test_sender_rule_password_management_keeps_secrets_out_of_database(
    migrated_database: Database,
) -> None:
    _, rule_value = _account_and_rule(migrated_database)
    secrets = _MemorySecrets()
    service = ManageSenderRules(
        lambda: SqlAlchemyUnitOfWork(migrated_database.session_factory),
        secrets,
    )

    updated = service.add_pdf_password(rule_value.id, SecretValue("local-only-fixture"))

    assert len(updated.pdf_password_refs) == 1
    reference = updated.pdf_password_refs[0]
    assert secrets.get(reference) is not None
    assert "local-only-fixture" not in reference
    cleared = service.clear_pdf_passwords(rule_value.id)
    assert cleared.pdf_password_refs == ()
    assert secrets.get(reference) is None

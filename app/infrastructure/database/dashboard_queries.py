"""Indexed SQLAlchemy read models for the server-rendered dashboard."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.application.queries.dashboard import (
    AttachmentDetail,
    DashboardSnapshot,
    EmailDetail,
    EmailListItem,
    EmailPage,
    EmailSearchCriteria,
    MoneyTotal,
    SyncSummary,
    TransactionDetail,
    TransactionListItem,
    TransactionOutcome,
    TransactionPage,
    TransactionSearchCriteria,
)
from app.domain.budget_enums import (
    CardHolderRole,
    FinancialEventStatus,
    SourceLinkStatus,
)
from app.domain.enums import (
    ClassificationStatus,
    ProcessingStatus,
    TransactionSourceStatus,
    TransactionType,
)
from app.domain.transactions import SPENDING_TRANSACTION_TYPES
from app.domain.types import JsonValue
from app.infrastructure.database.budget_models import (
    CardModel,
    EventAllocationModel,
    FinancialEventModel,
    FinancialEventSourceLinkModel,
    WalletModel,
)
from app.infrastructure.database.models import (
    EmailAttachmentModel,
    EmailModel,
    SenderRuleModel,
    SyncRunModel,
    SyncStateModel,
    TransactionModel,
)


class SqlAlchemyDashboardReadRepository:
    """Short-session dashboard reads with no dependency on Gmail."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        local_timezone: str,
    ) -> None:
        self._session_factory = session_factory
        self._timezone = ZoneInfo(local_timezone)

    def dashboard(self, *, now: datetime, recent_days: int = 7) -> DashboardSnapshot:
        """Build home totals using local calendar boundaries and UTC storage.

        ``recent_days`` counts local calendar days ending today, so the
        default of seven means today plus the six days before it. Only the
        recent-transaction list widens with it; every total keeps its own
        fixed boundary.
        """
        if recent_days < 1:
            raise ValueError("recent_days must be at least one day")
        local_now = now.astimezone(self._timezone)
        today = local_now.date()
        week_start = today - timedelta(days=today.weekday())
        month_start = today.replace(day=1)
        recent_start = datetime.combine(
            today - timedelta(days=recent_days - 1),
            time.min,
            self._timezone,
        ).astimezone(UTC)
        recent_end = datetime.combine(
            today + timedelta(days=1),
            time.min,
            self._timezone,
        ).astimezone(UTC)
        with self._session_factory() as session:
            recent_models = session.execute(
                select(
                    TransactionModel,
                    EmailModel,
                    SenderRuleModel,
                    CardModel.id,
                )
                .join(EmailModel, EmailModel.id == TransactionModel.email_id)
                .outerjoin(
                    SenderRuleModel,
                    SenderRuleModel.id == EmailModel.matched_sender_rule_id,
                )
                .outerjoin(
                    CardModel,
                    and_(
                        CardModel.gmail_account_id == EmailModel.gmail_account_id,
                        CardModel.issuer_code == SenderRuleModel.institution_key,
                        CardModel.last_four == TransactionModel.card_last_four,
                        # Being supplementary is part of the match, so a join
                        # hit means exactly that and the projection below stays
                        # a presence test.
                        CardModel.holder_role == CardHolderRole.SUPPLEMENTARY.value,
                    ),
                )
                .where(
                    TransactionModel.source_status
                    == TransactionSourceStatus.ACTIVE.value,
                    TransactionModel.manually_ignored.is_(False),
                    TransactionModel.transaction_at >= recent_start,
                    TransactionModel.transaction_at < recent_end,
                )
                .order_by(
                    TransactionModel.transaction_at.desc(),
                    TransactionModel.id.desc(),
                )
            ).all()
            last_sync_model = session.scalar(
                select(SyncRunModel).order_by(SyncRunModel.created_at.desc()).limit(1)
            )
            history_id = session.scalar(
                select(SyncStateModel.committed_history_id)
                .order_by(SyncStateModel.updated_at.desc())
                .limit(1)
            )
            return DashboardSnapshot(
                today_totals=self._totals(session, today, today + timedelta(days=1)),
                week_totals=self._totals(
                    session, week_start, today + timedelta(days=1)
                ),
                month_totals=self._totals(
                    session, month_start, today + timedelta(days=1)
                ),
                transaction_count=self._transaction_count(session),
                unconfirmed_count=self._unconfirmed_count(session),
                unconfirmed_today_count=self._unconfirmed_between(
                    session, today, today + timedelta(days=1)
                ),
                unconfirmed_today_totals=self._unconfirmed_totals(
                    session, today, today + timedelta(days=1)
                ),
                parser_failure_count=int(
                    session.scalar(
                        select(func.count(EmailModel.id)).where(
                            EmailModel.processing_status
                            == ProcessingStatus.FAILED.value
                        )
                    )
                    or 0
                ),
                recent_transactions=tuple(
                    self._transaction_item(
                        transaction, email, sender_rule, supplementary_card_id
                    )
                    for (
                        transaction,
                        email,
                        sender_rule,
                        supplementary_card_id,
                    ) in recent_models
                ),
                last_sync=(
                    None
                    if last_sync_model is None
                    else SyncSummary(
                        status=last_sync_model.status,
                        mode=last_sync_model.mode,
                        finished_at=last_sync_model.finished_at,
                        error_message=last_sync_model.error_message,
                        history_id=history_id,
                    )
                ),
            )

    def search_transactions(
        self, criteria: TransactionSearchCriteria
    ) -> TransactionPage:
        conditions = self._transaction_conditions(criteria)
        order: tuple[Any, ...] = {
            "transaction_at_asc": (
                TransactionModel.transaction_at.asc(),
                TransactionModel.id.asc(),
            ),
            "amount_desc": (
                TransactionModel.amount_minor.desc(),
                TransactionModel.id.desc(),
            ),
            "amount_asc": (
                TransactionModel.amount_minor.asc(),
                TransactionModel.id.asc(),
            ),
        }.get(
            criteria.sort,
            (
                TransactionModel.transaction_at.desc(),
                TransactionModel.id.desc(),
            ),
        )
        offset = (criteria.page - 1) * criteria.page_size
        with self._session_factory() as session:
            total = int(
                session.scalar(
                    select(func.count(TransactionModel.id))
                    .join(EmailModel, EmailModel.id == TransactionModel.email_id)
                    .where(*conditions)
                )
                or 0
            )
            rows = session.execute(
                select(
                    TransactionModel,
                    EmailModel,
                    SenderRuleModel,
                    CardModel.id,
                )
                .join(EmailModel, EmailModel.id == TransactionModel.email_id)
                .outerjoin(
                    SenderRuleModel,
                    SenderRuleModel.id == EmailModel.matched_sender_rule_id,
                )
                .outerjoin(
                    CardModel,
                    and_(
                        CardModel.gmail_account_id == EmailModel.gmail_account_id,
                        CardModel.issuer_code == SenderRuleModel.institution_key,
                        CardModel.last_four == TransactionModel.card_last_four,
                        # Being supplementary is part of the match, so a join
                        # hit means exactly that and the projection below stays
                        # a presence test.
                        CardModel.holder_role == CardHolderRole.SUPPLEMENTARY.value,
                    ),
                )
                .where(*conditions)
                .order_by(*order)
                .offset(offset)
                .limit(criteria.page_size)
            ).all()
        return TransactionPage(
            items=tuple(
                self._transaction_item(tx, email, sender_rule, supplementary_id)
                for tx, email, sender_rule, supplementary_id in rows
            ),
            page=criteria.page,
            page_size=criteria.page_size,
            total=total,
        )

    def _transaction_outcome(
        self, session: Session, transaction_id: int
    ) -> TransactionOutcome | None:
        """Report the event one confirmed notification became, if any.

        Only a confirmed pairing counts: a candidate is still a proposal and a
        rejected one was answered by saying this is different money.
        """
        row = session.execute(
            select(FinancialEventModel.id, FinancialEventModel.status)
            .join(
                FinancialEventSourceLinkModel,
                FinancialEventSourceLinkModel.financial_event_id
                == FinancialEventModel.id,
            )
            .where(
                FinancialEventSourceLinkModel.transaction_id == transaction_id,
                FinancialEventSourceLinkModel.status
                == SourceLinkStatus.CONFIRMED.value,
            )
            .limit(1)
        ).first()
        if row is None:
            return None
        event_id, event_status = row
        wallets = (
            session.execute(
                select(WalletModel.name)
                .join(
                    EventAllocationModel,
                    EventAllocationModel.funding_wallet_id == WalletModel.id,
                )
                .where(EventAllocationModel.financial_event_id == event_id)
                .order_by(WalletModel.name)
            )
            .scalars()
            .all()
        )
        return TransactionOutcome(
            event_id=int(event_id),
            status=FinancialEventStatus(event_status),
            # One event can draw on the same wallet in several rows; the
            # reader only needs to know which wallets, once each.
            funding_wallets=tuple(dict.fromkeys(wallets)),
        )

    def transaction_detail(self, transaction_id: int) -> TransactionDetail | None:
        with self._session_factory() as session:
            row = session.execute(
                select(
                    TransactionModel,
                    EmailModel,
                    SenderRuleModel,
                    CardModel.id,
                )
                .join(EmailModel, EmailModel.id == TransactionModel.email_id)
                .outerjoin(
                    SenderRuleModel,
                    SenderRuleModel.id == EmailModel.matched_sender_rule_id,
                )
                .outerjoin(
                    CardModel,
                    and_(
                        CardModel.gmail_account_id == EmailModel.gmail_account_id,
                        CardModel.issuer_code == SenderRuleModel.institution_key,
                        CardModel.last_four == TransactionModel.card_last_four,
                        # Being supplementary is part of the match, so a join
                        # hit means exactly that and the projection below stays
                        # a presence test.
                        CardModel.holder_role == CardHolderRole.SUPPLEMENTARY.value,
                    ),
                )
                .where(TransactionModel.id == transaction_id)
            ).one_or_none()
            if row is None:
                return None
            transaction, email, sender_rule, supplementary_card_id = row
            parsed = {
                "transaction_type": transaction.parsed_transaction_type,
                "merchant": transaction.parsed_merchant,
                "amount_minor": transaction.parsed_amount_minor,
                "currency": transaction.parsed_currency,
                "transaction_at": self._json_datetime(
                    transaction.parsed_transaction_at
                ),
            }
            effective = {
                "transaction_type": transaction.transaction_type,
                "merchant": transaction.merchant,
                "amount_minor": transaction.amount_minor,
                "currency": transaction.currency,
                "transaction_at": self._json_datetime(transaction.transaction_at),
            }
            return TransactionDetail(
                outcome=self._transaction_outcome(session, transaction_id),
                item=self._transaction_item(
                    transaction,
                    email,
                    sender_rule,
                    supplementary_card_id,
                ),
                updated_at=transaction.updated_at,
                source_key=transaction.source_key,
                parsed_values=parsed,
                effective_values=effective,
                manual_overrides=cast(
                    dict[str, JsonValue], transaction.manual_overrides_json
                ),
                original_merchant_text=transaction.original_merchant_text,
                account_last_four=transaction.account_last_four,
                posted_at=transaction.posted_at,
                authorization_code=transaction.authorization_code,
                user_note=transaction.user_note,
                source_status=transaction.source_status,
                extraction_evidence=tuple(
                    cast(list[JsonValue], transaction.extraction_evidence_json)
                ),
                parse_warnings=tuple(
                    cast(list[JsonValue], transaction.parse_warnings_json)
                ),
                email_received_at=email.received_at,
                email_classification=ClassificationStatus(email.classification_status),
                classification_reason=email.classification_reason,
                body_text=email.body_text,
                body_summary=email.body_summary,
            )

    def search_emails(self, criteria: EmailSearchCriteria) -> EmailPage:
        conditions: list[Any] = []
        if criteria.query:
            value = criteria.query.strip()
            conditions.append(
                or_(
                    EmailModel.sender.contains(value),
                    EmailModel.subject.contains(value),
                )
            )
        if criteria.classification:
            conditions.append(
                EmailModel.classification_status == criteria.classification.value
            )
        if criteria.processing:
            conditions.append(EmailModel.processing_status == criteria.processing.value)
        count_transactions = func.count(TransactionModel.id)
        with self._session_factory() as session:
            total = int(
                session.scalar(select(func.count(EmailModel.id)).where(*conditions))
                or 0
            )
            rows = session.execute(
                select(EmailModel, count_transactions)
                .outerjoin(
                    TransactionModel,
                    and_(
                        TransactionModel.email_id == EmailModel.id,
                        TransactionModel.source_status
                        == TransactionSourceStatus.ACTIVE.value,
                    ),
                )
                .where(*conditions)
                .group_by(EmailModel.id)
                .order_by(EmailModel.received_at.desc(), EmailModel.id.desc())
                .offset((criteria.page - 1) * criteria.page_size)
                .limit(criteria.page_size)
            ).all()
        return EmailPage(
            items=tuple(
                self._email_item(email, int(transaction_count))
                for email, transaction_count in rows
            ),
            page=criteria.page,
            page_size=criteria.page_size,
            total=total,
        )

    def email_detail(self, email_id: int) -> EmailDetail | None:
        with self._session_factory() as session:
            email = session.get(EmailModel, email_id)
            if email is None:
                return None
            sender_rule = (
                session.get(SenderRuleModel, email.matched_sender_rule_id)
                if email.matched_sender_rule_id is not None
                else None
            )
            transaction_rows = session.execute(
                select(
                    TransactionModel,
                    CardModel.id,
                )
                .outerjoin(
                    CardModel,
                    and_(
                        CardModel.gmail_account_id == email.gmail_account_id,
                        CardModel.issuer_code
                        == (
                            sender_rule.institution_key
                            if sender_rule is not None
                            else None
                        ),
                        CardModel.last_four == TransactionModel.card_last_four,
                        CardModel.holder_role == CardHolderRole.SUPPLEMENTARY.value,
                    ),
                )
                .where(
                    TransactionModel.email_id == email_id,
                    TransactionModel.source_status
                    == TransactionSourceStatus.ACTIVE.value,
                )
                .order_by(TransactionModel.id)
            ).all()
            attachments = session.scalars(
                select(EmailAttachmentModel)
                .where(EmailAttachmentModel.email_id == email_id)
                .order_by(EmailAttachmentModel.id)
            ).all()
            return EmailDetail(
                item=self._email_item(email, len(transaction_rows)),
                gmail_message_id=email.gmail_message_id,
                gmail_thread_id=email.gmail_thread_id,
                recipients=tuple(email.recipients_json),
                labels=tuple(email.labels_json),
                snippet=email.snippet,
                body_text=email.body_text,
                body_summary=email.body_summary,
                has_html_body=email.has_html_body,
                classification_reason=email.classification_reason,
                parser_version=email.parser_version,
                attachments=tuple(
                    AttachmentDetail(
                        filename=item.filename,
                        mime_type=item.mime_type,
                        size_bytes=item.size_bytes,
                        encrypted=item.is_encrypted,
                        extraction_status=item.extraction_status,
                        extractor_name=item.extractor_name,
                        error_message=item.error_message,
                    )
                    for item in attachments
                ),
                transactions=tuple(
                    self._transaction_item(
                        transaction,
                        email,
                        sender_rule,
                        supplementary_card_id,
                    )
                    for (
                        transaction,
                        supplementary_card_id,
                    ) in transaction_rows
                ),
            )

    def _totals(
        self, session: Session, date_from: date, date_to: date
    ) -> tuple[MoneyTotal, ...]:
        start = datetime.combine(date_from, time.min, self._timezone).astimezone(UTC)
        end = datetime.combine(date_to, time.min, self._timezone).astimezone(UTC)
        rows = session.execute(
            select(
                TransactionModel.currency,
                func.sum(TransactionModel.amount_minor),
            )
            .where(
                TransactionModel.source_status == TransactionSourceStatus.ACTIVE.value,
                TransactionModel.manually_ignored.is_(False),
                TransactionModel.currency.is_not(None),
                TransactionModel.amount_minor.is_not(None),
                TransactionModel.transaction_at >= start,
                TransactionModel.transaction_at < end,
                TransactionModel.transaction_type.in_(
                    item.value for item in SPENDING_TRANSACTION_TYPES
                ),
            )
            .group_by(TransactionModel.currency)
            .order_by(TransactionModel.currency)
        ).all()
        return tuple(
            MoneyTotal(currency=currency, amount_minor=int(amount))
            for currency, amount in rows
            if currency is not None and amount is not None
        )

    @staticmethod
    def _transaction_count(session: Session) -> int:
        return int(
            session.scalar(
                select(func.count(TransactionModel.id)).where(
                    TransactionModel.source_status
                    == TransactionSourceStatus.ACTIVE.value,
                    TransactionModel.manually_ignored.is_(False),
                )
            )
            or 0
        )

    def _unconfirmed_totals(
        self, session: Session, date_from: date, date_to: date
    ) -> tuple[MoneyTotal, ...]:
        """Sum one local day's parsed spending still awaiting confirmation.

        Provisional only: a misparsed amount or a duplicate has not been
        ruled out yet, so this never substitutes for `_totals` (ADR-B092).
        """
        start = datetime.combine(date_from, time.min, self._timezone).astimezone(UTC)
        end = datetime.combine(date_to, time.min, self._timezone).astimezone(UTC)
        rows = session.execute(
            select(
                TransactionModel.currency,
                func.sum(TransactionModel.amount_minor),
            )
            .where(
                TransactionModel.source_status == TransactionSourceStatus.ACTIVE.value,
                TransactionModel.manually_confirmed.is_(False),
                TransactionModel.manually_ignored.is_(False),
                TransactionModel.currency.is_not(None),
                TransactionModel.amount_minor.is_not(None),
                TransactionModel.transaction_at >= start,
                TransactionModel.transaction_at < end,
                TransactionModel.transaction_type.in_(
                    item.value for item in SPENDING_TRANSACTION_TYPES
                ),
            )
            .group_by(TransactionModel.currency)
            .order_by(TransactionModel.currency)
        ).all()
        return tuple(
            MoneyTotal(currency=currency, amount_minor=int(amount))
            for currency, amount in rows
            if currency is not None and amount is not None
        )

    def _unconfirmed_between(
        self, session: Session, date_from: date, date_to: date
    ) -> int:
        """Count one local day's parsed transactions still awaiting confirmation."""
        start = datetime.combine(date_from, time.min, self._timezone).astimezone(UTC)
        end = datetime.combine(date_to, time.min, self._timezone).astimezone(UTC)
        return int(
            session.scalar(
                select(func.count(TransactionModel.id)).where(
                    TransactionModel.source_status
                    == TransactionSourceStatus.ACTIVE.value,
                    TransactionModel.manually_confirmed.is_(False),
                    TransactionModel.manually_ignored.is_(False),
                    TransactionModel.transaction_at >= start,
                    TransactionModel.transaction_at < end,
                )
            )
            or 0
        )

    @staticmethod
    def _unconfirmed_count(session: Session) -> int:
        return int(
            session.scalar(
                select(func.count(TransactionModel.id)).where(
                    TransactionModel.source_status
                    == TransactionSourceStatus.ACTIVE.value,
                    TransactionModel.manually_confirmed.is_(False),
                    TransactionModel.manually_ignored.is_(False),
                )
            )
            or 0
        )

    def _transaction_conditions(self, criteria: TransactionSearchCriteria) -> list[Any]:
        conditions: list[Any] = [
            TransactionModel.source_status == TransactionSourceStatus.ACTIVE.value
        ]
        if criteria.date_from:
            conditions.append(
                TransactionModel.transaction_at
                >= datetime.combine(
                    criteria.date_from, time.min, self._timezone
                ).astimezone(UTC)
            )
        if criteria.date_to:
            conditions.append(
                TransactionModel.transaction_at
                < datetime.combine(
                    criteria.date_to + timedelta(days=1),
                    time.min,
                    self._timezone,
                ).astimezone(UTC)
            )
        if criteria.merchant:
            conditions.append(
                TransactionModel.merchant.contains(criteria.merchant.strip())
            )
        if criteria.sender:
            conditions.append(EmailModel.sender.contains(criteria.sender.strip()))
        if criteria.amount_min is not None:
            conditions.append(TransactionModel.amount_minor >= criteria.amount_min)
        if criteria.amount_max is not None:
            conditions.append(TransactionModel.amount_minor <= criteria.amount_max)
        if criteria.currency:
            conditions.append(TransactionModel.currency == criteria.currency)
        if criteria.transaction_type:
            conditions.append(
                TransactionModel.transaction_type == criteria.transaction_type.value
            )
        if criteria.confidence_min is not None:
            conditions.append(TransactionModel.confidence >= criteria.confidence_min)
        if criteria.review_status == "confirmed":
            conditions.append(TransactionModel.manually_confirmed.is_(True))
        elif criteria.review_status == "ignored":
            conditions.append(TransactionModel.manually_ignored.is_(True))
        elif criteria.review_status == "unconfirmed":
            conditions.extend(
                (
                    TransactionModel.manually_confirmed.is_(False),
                    TransactionModel.manually_ignored.is_(False),
                )
            )
        return conditions

    @staticmethod
    def _transaction_item(
        transaction: TransactionModel,
        email: EmailModel,
        sender_rule: SenderRuleModel | None,
        supplementary_card_id: int | None,
    ) -> TransactionListItem:
        return TransactionListItem(
            id=transaction.id,
            email_id=email.id,
            transaction_at=transaction.transaction_at,
            merchant=transaction.merchant,
            amount_minor=transaction.amount_minor,
            currency=transaction.currency,
            transaction_type=TransactionType(transaction.transaction_type),
            card_last_four=transaction.card_last_four,
            institution_key=(
                sender_rule.institution_key if sender_rule is not None else None
            ),
            institution_name=(
                sender_rule.institution_name if sender_rule is not None else None
            ),
            is_supplementary_card=supplementary_card_id is not None,
            confidence=transaction.confidence,
            parser_name=transaction.parser_name,
            parser_version=transaction.parser_version,
            manually_confirmed=transaction.manually_confirmed,
            manually_ignored=transaction.manually_ignored,
            subject=email.subject,
            sender=email.sender,
        )

    @staticmethod
    def _email_item(email: EmailModel, transaction_count: int) -> EmailListItem:
        return EmailListItem(
            id=email.id,
            received_at=email.received_at,
            sender=email.sender,
            subject=email.subject,
            classification_status=ClassificationStatus(email.classification_status),
            processing_status=ProcessingStatus(email.processing_status),
            transaction_count=transaction_count,
            parser_name=email.parser_name,
            confidence=email.classification_confidence,
            source_status=email.source_status,
        )

    @staticmethod
    def _json_datetime(value: datetime | None) -> JsonValue:
        return value.isoformat() if value else None

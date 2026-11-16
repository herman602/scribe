"""Indexed SQLAlchemy read models for the clearing hub.

Every list is bounded and every aggregate stays inside one currency. The
reservation shown here comes from the same repository the services use, so a
page can never display an availability the ledger disagrees with.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql import Subquery

from app.application.queries.clearing import (
    ClearingAccountOption,
    ClearingCardOption,
    ClearingWalletOption,
    ClearingWalletPosition,
    OverCoveredCommitment,
    OverCoveredPage,
    SchedulableCommitmentRow,
    SchedulableGroup,
    ScheduleLinkRow,
    SchedulePage,
    ScheduleRow,
    paged_offset,
    summarize_actions,
)
from app.domain.budget_enums import (
    CardConfigurationStatus,
    CommitmentFulfillmentMode,
    FinancialAccountType,
    LedgerAccountKind,
    ScheduledTransferStatus,
    WalletPurpose,
)
from app.domain.budget_rules import holds_wallets
from app.infrastructure.database.budget_models import (
    CardModel,
    FinancialAccountModel,
    FinancialEventModel,
    FundingCommitmentModel,
    JournalPostingModel,
    LedgerAccountModel,
    ScheduledTransferCommitmentModel,
    ScheduledTransferModel,
    WalletModel,
)
from app.infrastructure.database.budget_repositories import (
    SqlAlchemyBudgetLedgerRepository,
    funding_commitment_record,
)
from app.infrastructure.database.scheduled_transfer_repositories import (
    ACTIVE_STATUS_VALUES,
    EXECUTED_STATUS_VALUES,
    LIVE_STATUS_VALUES,
    claimable_condition,
    scheduled_transfer_record,
)


class SqlAlchemyClearingReadRepository:
    """Short-session reads for the clearing page and the money CLI."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def positions(self) -> tuple[ClearingWalletPosition, ...]:
        """Return every active wallet's actual, reserved, and available money."""
        with self._session_factory() as session:
            rows = session.execute(
                select(WalletModel, FinancialAccountModel)
                .join(
                    FinancialAccountModel,
                    FinancialAccountModel.id == WalletModel.financial_account_id,
                )
                .where(
                    WalletModel.is_active.is_(True),
                    FinancialAccountModel.is_active.is_(True),
                )
                .order_by(
                    FinancialAccountModel.institution_code,
                    FinancialAccountModel.display_name,
                    WalletModel.name,
                    WalletModel.id,
                )
            ).all()
            ledger = SqlAlchemyBudgetLedgerRepository(session)
            return tuple(
                ClearingWalletPosition(
                    wallet_id=wallet.id,
                    wallet_name=wallet.name,
                    financial_account_id=account.id,
                    account_name=account.display_name,
                    institution_code=account.institution_code,
                    currency=account.currency,
                    is_clearing_hub=wallet.purpose == WalletPurpose.CLEARING_HUB.value,
                    balance_minor=ledger.wallet_balance(wallet.id),
                    reserved_minor=ledger.reserved_minor(wallet.id),
                )
                for wallet, account in rows
                if holds_wallets(FinancialAccountType(account.account_type))
            )

    def schedulable_groups(self) -> tuple[SchedulableGroup, ...]:
        """Return commitments one movement each could settle, already grouped."""
        with self._session_factory() as session:
            rows = session.execute(
                select(
                    FundingCommitmentModel,
                    WalletModel,
                    FinancialAccountModel,
                    FinancialEventModel,
                )
                .join(
                    WalletModel,
                    WalletModel.id == FundingCommitmentModel.source_wallet_id,
                )
                .join(
                    FinancialAccountModel,
                    FinancialAccountModel.id == WalletModel.financial_account_id,
                )
                .join(
                    FinancialEventModel,
                    FinancialEventModel.id == FundingCommitmentModel.financial_event_id,
                )
                .where(
                    FundingCommitmentModel.amount_minor
                    > FundingCommitmentModel.released_minor
                )
                .order_by(FundingCommitmentModel.id)
            ).all()
            settlement_names = _account_names(session)
            card_names = _card_names(session)
            grouped: dict[
                tuple[int, int, str, str], list[SchedulableCommitmentRow]
            ] = {}
            for commitment, wallet, account, event in rows:
                active, executed = _coverage(session, commitment.id)
                row = SchedulableCommitmentRow(
                    commitment=funding_commitment_record(commitment),
                    source_wallet_name=wallet.name,
                    source_account_id=account.id,
                    source_account_name=account.display_name,
                    settlement_account_id=commitment.settlement_account_id,
                    settlement_account_name=settlement_names.get(
                        commitment.settlement_account_id, ""
                    ),
                    card_id=event.card_id,
                    card_display_name=card_names.get(event.card_id),
                    active_covered_minor=active,
                    executed_covered_minor=executed,
                )
                if row.uncovered_minor <= 0:
                    continue
                key = (
                    account.id,
                    commitment.settlement_account_id,
                    commitment.currency,
                    commitment.fulfillment_mode,
                )
                grouped.setdefault(key, []).append(row)
        return tuple(
            SchedulableGroup(
                source_account_id=source_account_id,
                source_account_name=rows[0].source_account_name,
                settlement_account_id=settlement_account_id,
                settlement_account_name=rows[0].settlement_account_name,
                currency=currency,
                fulfillment_mode=CommitmentFulfillmentMode(mode),
                rows=tuple(rows),
            )
            for (
                source_account_id,
                settlement_account_id,
                currency,
                mode,
            ), rows in sorted(grouped.items())
        )

    def schedules(
        self,
        statuses: frozenset[ScheduledTransferStatus],
        *,
        limit: int,
        offset: int,
    ) -> SchedulePage:
        """Return one bounded page of movements, newest due time first."""
        values = sorted(item.value for item in statuses)
        with self._session_factory() as session:
            total = int(
                session.execute(
                    select(func.count())
                    .select_from(ScheduledTransferModel)
                    .where(ScheduledTransferModel.status.in_(values))
                ).scalar_one()
            )
            page = paged_offset(total=total, limit=limit, offset=offset)
            models = session.scalars(
                select(ScheduledTransferModel)
                .where(ScheduledTransferModel.status.in_(values))
                .order_by(
                    ScheduledTransferModel.due_at_utc.desc(),
                    ScheduledTransferModel.id.desc(),
                )
                .limit(limit)
                .offset(page)
            ).all()
            rows = tuple(self._row(session, model) for model in models)
        return SchedulePage(rows=rows, total=total, limit=limit, offset=page)

    def schedule(self, scheduled_transfer_id: int) -> ScheduleRow | None:
        """Return one movement with its names and coverage, or None."""
        with self._session_factory() as session:
            model = session.get(ScheduledTransferModel, scheduled_transfer_id)
            return None if model is None else self._row(session, model)

    def account_options(self) -> tuple[ClearingAccountOption, ...]:
        """Return active asset accounts a movement could use."""
        with self._session_factory() as session:
            accounts = session.scalars(
                select(FinancialAccountModel)
                .where(FinancialAccountModel.is_active.is_(True))
                .order_by(
                    FinancialAccountModel.institution_code,
                    FinancialAccountModel.display_name,
                    FinancialAccountModel.id,
                )
            ).all()
        return tuple(
            ClearingAccountOption(
                financial_account_id=account.id,
                display_name=account.display_name,
                institution_code=account.institution_code,
                currency=account.currency,
            )
            for account in accounts
            if holds_wallets(FinancialAccountType(account.account_type))
        )

    def wallet_options(self) -> tuple[ClearingWalletOption, ...]:
        """Return active wallets a movement could leave from or arrive in."""
        with self._session_factory() as session:
            rows = session.execute(
                select(WalletModel, FinancialAccountModel)
                .join(
                    FinancialAccountModel,
                    FinancialAccountModel.id == WalletModel.financial_account_id,
                )
                .where(
                    WalletModel.is_active.is_(True),
                    FinancialAccountModel.is_active.is_(True),
                )
                .order_by(
                    FinancialAccountModel.institution_code,
                    FinancialAccountModel.display_name,
                    WalletModel.name,
                    WalletModel.id,
                )
            ).all()
        return tuple(
            ClearingWalletOption(
                wallet_id=wallet.id,
                wallet_name=wallet.name,
                financial_account_id=account.id,
                account_name=account.display_name,
                currency=account.currency,
                is_clearing_hub=wallet.purpose == WalletPurpose.CLEARING_HUB.value,
            )
            for wallet, account in rows
            if holds_wallets(FinancialAccountType(account.account_type))
        )

    def card_options(self) -> tuple[ClearingCardOption, ...]:
        """Return ready cards, with the liability each of them still owes."""
        with self._session_factory() as session:
            cards = session.scalars(
                select(CardModel)
                .where(
                    CardModel.configuration_status
                    == CardConfigurationStatus.READY.value
                )
                .order_by(CardModel.display_name, CardModel.id)
            ).all()
            names = _account_names(session)
            options: list[ClearingCardOption] = []
            for card in cards:
                if card.settlement_account_id is None:  # pragma: no cover - ready
                    continue
                if card.liability_account_id is None:  # pragma: no cover - ready
                    continue
                settlement = session.get(
                    FinancialAccountModel, card.settlement_account_id
                )
                if settlement is None:  # pragma: no cover - guarded by FK
                    continue
                options.append(
                    ClearingCardOption(
                        card_id=card.id,
                        display_name=card.display_name,
                        settlement_account_id=card.settlement_account_id,
                        settlement_account_name=names.get(
                            card.settlement_account_id, ""
                        ),
                        liability_account_id=card.liability_account_id,
                        currency=settlement.currency,
                        outstanding_minor=_liability_minor(
                            session, card.liability_account_id
                        ),
                    )
                )
        return tuple(options)

    def over_covered_commitments(self, *, limit: int, offset: int) -> OverCoveredPage:
        """Return commitments with more scheduled at them than they still owe.

        The comparison is per commitment and covers every live schedule at
        once, so a surplus split across two movements is found even when
        neither movement looks wrong on its own (ADR-B060). The total is
        counted in the database rather than inferred from the page, because a
        page that shows 25 of 26 must not report 25.
        """
        live = _live_coverage_subquery()
        condition = (
            live.c.live_minor
            > FundingCommitmentModel.amount_minor
            - FundingCommitmentModel.released_minor
        )
        with self._session_factory() as session:
            total = int(
                session.execute(
                    select(func.count())
                    .select_from(FundingCommitmentModel)
                    .join(live, live.c.commitment_id == FundingCommitmentModel.id)
                    .where(condition)
                ).scalar_one()
            )
            page = paged_offset(total=total, limit=limit, offset=offset)
            rows = session.execute(
                select(
                    FundingCommitmentModel.id,
                    FundingCommitmentModel.currency,
                    FundingCommitmentModel.amount_minor
                    - FundingCommitmentModel.released_minor,
                    live.c.live_minor,
                    WalletModel.name,
                    FinancialAccountModel.display_name,
                )
                .join(live, live.c.commitment_id == FundingCommitmentModel.id)
                .join(
                    WalletModel,
                    WalletModel.id == FundingCommitmentModel.source_wallet_id,
                )
                .join(
                    FinancialAccountModel,
                    FinancialAccountModel.id
                    == FundingCommitmentModel.settlement_account_id,
                )
                .where(condition)
                .order_by(FundingCommitmentModel.id)
                .limit(limit)
                .offset(page)
            ).all()
            # Which corrections are possible is decided over every over-covered
            # commitment, not over the page being shown, so a truncated list
            # still summarizes the whole problem.
            statuses = session.execute(
                select(ScheduledTransferModel.status)
                .distinct()
                .join(
                    ScheduledTransferCommitmentModel,
                    ScheduledTransferCommitmentModel.scheduled_transfer_id
                    == ScheduledTransferModel.id,
                )
                .join(
                    FundingCommitmentModel,
                    FundingCommitmentModel.id
                    == ScheduledTransferCommitmentModel.funding_commitment_id,
                )
                .join(live, live.c.commitment_id == FundingCommitmentModel.id)
                .where(
                    condition,
                    ScheduledTransferModel.status.in_(LIVE_STATUS_VALUES),
                )
            ).all()
        return OverCoveredPage(
            rows=tuple(
                OverCoveredCommitment(
                    funding_commitment_id=int(commitment_id),
                    source_wallet_name=str(wallet_name),
                    settlement_account_name=str(account_name),
                    currency=str(currency),
                    required_minor=int(required_minor),
                    live_coverage_minor=int(live_minor),
                )
                for (
                    commitment_id,
                    currency,
                    required_minor,
                    live_minor,
                    wallet_name,
                    account_name,
                ) in rows
            ),
            total=total,
            limit=limit,
            offset=page,
            actions=summarize_actions(
                ScheduledTransferStatus(row[0]) for row in statuses
            ),
        )

    def due_now(self, now: datetime) -> int:
        """Return how many movements a maintenance pass would claim right now."""
        with self._session_factory() as session:
            total = session.execute(
                select(func.count())
                .select_from(ScheduledTransferModel)
                .where(claimable_condition(now))
            ).scalar_one()
        return int(total)

    @staticmethod
    def _row(session: Session, model: ScheduledTransferModel) -> ScheduleRow:
        """Attach the names and the coverage one movement's screen needs."""
        names = _account_names(session)
        wallets = _wallet_names(session)
        card_names = _card_names(session)
        live = _live_coverage_subquery()
        links = session.execute(
            select(
                ScheduledTransferCommitmentModel.funding_commitment_id,
                ScheduledTransferCommitmentModel.amount_minor,
                FundingCommitmentModel.amount_minor
                - FundingCommitmentModel.released_minor,
                func.coalesce(live.c.live_minor, 0),
            )
            .join(
                FundingCommitmentModel,
                FundingCommitmentModel.id
                == ScheduledTransferCommitmentModel.funding_commitment_id,
            )
            .join(
                live,
                live.c.commitment_id
                == ScheduledTransferCommitmentModel.funding_commitment_id,
                isouter=True,
            )
            .where(ScheduledTransferCommitmentModel.scheduled_transfer_id == model.id)
            .order_by(ScheduledTransferCommitmentModel.funding_commitment_id)
        ).all()
        return ScheduleRow(
            schedule=scheduled_transfer_record(model),
            source_account_name=names.get(model.source_account_id, ""),
            source_wallet_name=wallets.get(model.source_wallet_id, ""),
            destination_account_name=names.get(model.destination_account_id, ""),
            destination_wallet_name=(
                None
                if model.destination_wallet_id is None
                else wallets.get(model.destination_wallet_id)
            ),
            card_display_name=card_names.get(model.card_id),
            links=tuple(
                ScheduleLinkRow(
                    funding_commitment_id=int(commitment_id),
                    amount_minor=int(amount_minor),
                    required_minor=int(required_minor),
                    live_coverage_minor=int(live_minor),
                )
                for commitment_id, amount_minor, required_minor, live_minor in links
            ),
        )


def _live_coverage_subquery() -> Subquery:
    """Return each commitment's coverage across every schedule still standing.

    `cancelled` and `reversed` schedules move nothing, so they are excluded;
    `assumed_executed` is included because that money really did leave.
    """
    return (
        select(
            ScheduledTransferCommitmentModel.funding_commitment_id.label(
                "commitment_id"
            ),
            func.sum(ScheduledTransferCommitmentModel.amount_minor).label("live_minor"),
        )
        .join(
            ScheduledTransferModel,
            ScheduledTransferModel.id
            == ScheduledTransferCommitmentModel.scheduled_transfer_id,
        )
        .where(ScheduledTransferModel.status.in_(LIVE_STATUS_VALUES))
        .group_by(ScheduledTransferCommitmentModel.funding_commitment_id)
        .subquery()
    )


def _coverage(session: Session, commitment_id: int) -> tuple[int, int]:
    """Return one commitment's active and executed coverage, in that order."""
    return (
        _coverage_for(session, commitment_id, ACTIVE_STATUS_VALUES),
        _coverage_for(session, commitment_id, EXECUTED_STATUS_VALUES),
    )


def _coverage_for(
    session: Session, commitment_id: int, statuses: tuple[str, ...]
) -> int:
    total = session.execute(
        select(
            func.coalesce(func.sum(ScheduledTransferCommitmentModel.amount_minor), 0)
        )
        .join(
            ScheduledTransferModel,
            ScheduledTransferModel.id
            == ScheduledTransferCommitmentModel.scheduled_transfer_id,
        )
        .where(
            ScheduledTransferCommitmentModel.funding_commitment_id == commitment_id,
            ScheduledTransferModel.status.in_(statuses),
        )
    ).scalar_one()
    return int(total)


def _liability_minor(session: Session, liability_account_id: int) -> int:
    """Return what one card liability account still owes, as a magnitude."""
    total = session.execute(
        select(func.coalesce(func.sum(JournalPostingModel.amount_minor), 0))
        .join(
            LedgerAccountModel,
            LedgerAccountModel.id == JournalPostingModel.ledger_account_id,
        )
        .where(
            LedgerAccountModel.financial_account_id == liability_account_id,
            LedgerAccountModel.kind == LedgerAccountKind.LIABILITY.value,
        )
    ).scalar_one()
    return -int(total)


def _account_names(session: Session) -> dict[int, str]:
    return {
        int(row[0]): str(row[1])
        for row in session.execute(
            select(FinancialAccountModel.id, FinancialAccountModel.display_name)
        ).all()
    }


def _wallet_names(session: Session) -> dict[int, str]:
    return {
        int(row[0]): str(row[1])
        for row in session.execute(select(WalletModel.id, WalletModel.name)).all()
    }


def _card_names(session: Session) -> dict[int | None, str | None]:
    return {
        int(row[0]): str(row[1])
        for row in session.execute(select(CardModel.id, CardModel.display_name)).all()
    }

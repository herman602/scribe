"""add immutable dedicated-wallet spending routes

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-14 12:00:00.000000

One row says that an event allocation was funded from a dedicated envelope and
was reclassified into a specific clearing-hub wallet. Absence means the normal
calendar-month spending treatment. The decision is stored at allocation time;
it is never inferred later from a category or wallet name.

The table is deliberately append-only. Its row changes the journal posting
shape, daily visibility, monthly budget actuals, and commitment source, so an
UPDATE or DELETE would rewrite all four interpretations without a reversal.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | Sequence[str] | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "dedicated_wallet_allocations"
_NO_UPDATE_TRIGGER = "dedicated_wallet_allocation_no_update"
_NO_DELETE_TRIGGER = "dedicated_wallet_allocation_no_delete"
_VALID_ROUTE_TRIGGER = "dedicated_wallet_allocation_valid_route"
_IMMUTABLE_MESSAGE = "a dedicated-wallet allocation is immutable"
_INVALID_ROUTE_MESSAGE = "a dedicated-wallet allocation has an invalid route"


class DedicatedWalletHistoryPresentError(RuntimeError):
    """A downgrade would delete a posted spending-treatment decision."""


def upgrade() -> None:
    """Create immutable routing evidence without rebuilding a financial table."""
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_allocation_id", sa.Integer(), nullable=False),
        sa.Column("clearing_wallet_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["event_allocation_id"],
            ["event_allocations.id"],
            name=op.f(
                "fk_dedicated_wallet_allocations_event_allocation_id_event_allocations"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["clearing_wallet_id"],
            ["wallets.id"],
            name=op.f("fk_dedicated_wallet_allocations_clearing_wallet_id_wallets"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dedicated_wallet_allocations")),
        sa.UniqueConstraint(
            "event_allocation_id", name="dedicated_wallet_event_allocation"
        ),
    )
    op.create_index(
        "ix_dedicated_wallet_allocations_clearing_wallet_id",
        _TABLE,
        ["clearing_wallet_id"],
    )
    op.execute(
        f"CREATE TRIGGER {_VALID_ROUTE_TRIGGER} BEFORE INSERT ON {_TABLE} "
        "FOR EACH ROW WHEN NOT EXISTS ("
        "SELECT 1 FROM event_allocations AS allocation "
        "JOIN financial_events AS event "
        "ON event.id = allocation.financial_event_id "
        "JOIN wallets AS source ON source.id = allocation.funding_wallet_id "
        "JOIN wallets AS clearing ON clearing.id = NEW.clearing_wallet_id "
        "WHERE allocation.id = NEW.event_allocation_id "
        "AND event.payment_method = 'credit_card' "
        "AND clearing.purpose = 'clearing_hub' "
        "AND clearing.is_active = 1 "
        "AND source.financial_account_id = clearing.financial_account_id "
        "AND source.id <> clearing.id"
        ") BEGIN "
        f"SELECT RAISE(ABORT, '{_INVALID_ROUTE_MESSAGE}'); END"
    )
    op.execute(
        f"CREATE TRIGGER {_NO_UPDATE_TRIGGER} BEFORE UPDATE ON {_TABLE} "
        "FOR EACH ROW BEGIN "
        f"SELECT RAISE(ABORT, '{_IMMUTABLE_MESSAGE}'); END"
    )
    op.execute(
        f"CREATE TRIGGER {_NO_DELETE_TRIGGER} BEFORE DELETE ON {_TABLE} "
        "FOR EACH ROW BEGIN "
        f"SELECT RAISE(ABORT, '{_IMMUTABLE_MESSAGE}'); END"
    )


def downgrade() -> None:
    """Remove the capability only when no historical decision would be lost."""
    count = int(
        op.get_bind().exec_driver_sql(f"SELECT COUNT(*) FROM {_TABLE}").scalar_one()
    )
    if count:
        raise DedicatedWalletHistoryPresentError(
            "revision 0015 refuses to downgrade because dedicated-wallet "
            f"history exists (rows={count}). Retain that audit history at 0015 "
            "or move it through a deliberate data migration; nothing was changed."
        )
    op.execute(f"DROP TRIGGER {_NO_DELETE_TRIGGER}")
    op.execute(f"DROP TRIGGER {_NO_UPDATE_TRIGGER}")
    op.execute(f"DROP TRIGGER {_VALID_ROUTE_TRIGGER}")
    op.drop_index(
        "ix_dedicated_wallet_allocations_clearing_wallet_id", table_name=_TABLE
    )
    op.drop_table(_TABLE)

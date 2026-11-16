"""add the subscription requirement register

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-09 15:00:00.000000

This revision only creates new tables, indexes, and triggers. It rebuilds no
existing table, so the ADR-B027 protocol in ``env.py`` applies unchanged but
has nothing to copy: foreign keys are disabled outside the transaction, the
whole migration runs under ``BEGIN IMMEDIATE``, and a complete
``PRAGMA foreign_key_check`` must pass before it commits.

SQLite cannot express "two price periods of one subscription may not claim the
same month" as a check constraint, because that is a cross-row rule. The
service validates it inside the same transaction that inserts the row, and the
BEFORE INSERT and BEFORE UPDATE triggers below are the database's own line of
defence for anything that reaches the table another way.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CURRENCY_CHECK = "length({0}) = 3 AND {0} GLOB '[A-Z][A-Z][A-Z]'"
_MONTH_START_CHECK = "{0} IS strftime('%Y-%m-01', {0})"
_MAX_CADENCE_MONTHS = 120
_CADENCE_SHAPE_CHECK = (
    "(billing_cadence = 'monthly' AND cadence_months = 1) OR "
    "(billing_cadence = 'yearly' AND cadence_months = 12) OR "
    f"(billing_cadence = 'custom' AND cadence_months BETWEEN 2 AND {_MAX_CADENCE_MONTHS})"
)
_OVERLAP_OPERATIONS = ("insert", "update")
_OVERLAP_TRIGGER = (
    "CREATE TRIGGER subscription_price_no_overlap_{operation} "
    "BEFORE {keyword} ON subscription_price_periods FOR EACH ROW BEGIN "
    "SELECT RAISE(ABORT, 'subscription price periods must not overlap') WHERE EXISTS ("
    "SELECT 1 FROM subscription_price_periods AS other "
    "WHERE other.subscription_id = NEW.subscription_id AND other.id <> NEW.id "
    "AND (NEW.effective_to IS NULL OR other.effective_from < NEW.effective_to) "
    "AND (other.effective_to IS NULL OR NEW.effective_from < other.effective_to)); END"
)


def upgrade() -> None:
    """Create the subscription register and its non-overlap triggers."""
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("budget_category_id", sa.Integer(), nullable=False),
        sa.Column("funding_wallet_id", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("billing_cadence", sa.String(length=32), nullable=False),
        sa.Column("cadence_months", sa.Integer(), nullable=False),
        sa.Column("billing_month", sa.Integer(), nullable=True),
        sa.Column("billing_day", sa.Integer(), nullable=True),
        sa.Column("active_from", sa.Date(), nullable=True),
        sa.Column("active_to", sa.Date(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
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
        sa.CheckConstraint(
            "active_from IS NULL OR " + _MONTH_START_CHECK.format("active_from"),
            name=op.f("ck_subscriptions_subscription_active_from_month_start"),
        ),
        sa.CheckConstraint(
            "active_to IS NULL OR " + _MONTH_START_CHECK.format("active_to"),
            name=op.f("ck_subscriptions_subscription_active_to_month_start"),
        ),
        sa.CheckConstraint(
            "active_to IS NULL OR active_from IS NULL OR active_to > active_from",
            name=op.f("ck_subscriptions_subscription_active_range"),
        ),
        sa.CheckConstraint(
            "is_active IN (0, 1)",
            name=op.f("ck_subscriptions_subscription_active_boolean"),
        ),
        sa.CheckConstraint(
            "billing_cadence IN ('monthly', 'yearly', 'custom')",
            name=op.f("ck_subscriptions_subscription_cadence"),
        ),
        sa.CheckConstraint(
            _CADENCE_SHAPE_CHECK,
            name=op.f("ck_subscriptions_subscription_cadence_shape"),
        ),
        sa.CheckConstraint(
            "billing_day IS NULL OR (billing_day >= 1 AND billing_day <= 31)",
            name=op.f("ck_subscriptions_subscription_billing_day_range"),
        ),
        sa.CheckConstraint(
            "billing_month IS NULL OR (billing_month >= 1 AND billing_month <= 12)",
            name=op.f("ck_subscriptions_subscription_billing_month_range"),
        ),
        sa.CheckConstraint(
            "billing_cadence <> 'custom' OR active_from IS NOT NULL",
            name=op.f("ck_subscriptions_subscription_custom_anchor"),
        ),
        sa.CheckConstraint(
            "billing_cadence <> 'yearly' OR billing_month IS NOT NULL",
            name=op.f("ck_subscriptions_subscription_yearly_anchor"),
        ),
        sa.CheckConstraint(
            "is_active = 1 OR active_to IS NOT NULL",
            name=op.f("ck_subscriptions_subscription_inactive_needs_end"),
        ),
        sa.CheckConstraint(
            _CURRENCY_CHECK.format("currency"),
            name=op.f("ck_subscriptions_subscription_currency"),
        ),
        sa.CheckConstraint(
            "version >= 1",
            name=op.f("ck_subscriptions_subscription_version_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["budget_category_id"],
            ["budget_categories.id"],
            name=op.f("fk_subscriptions_budget_category_id_budget_categories"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["funding_wallet_id"],
            ["wallets.id"],
            name=op.f("fk_subscriptions_funding_wallet_id_wallets"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_subscriptions")),
    )
    op.create_index(
        "ix_subscriptions_budget_category_id",
        "subscriptions",
        ["budget_category_id"],
        unique=False,
    )
    op.create_index(
        "ix_subscriptions_currency_active",
        "subscriptions",
        ["currency", "is_active"],
        unique=False,
    )
    op.create_index(
        "ix_subscriptions_funding_wallet_id",
        "subscriptions",
        ["funding_wallet_id"],
        unique=False,
    )
    op.create_index(
        "uq_subscriptions_active_name",
        "subscriptions",
        ["name"],
        unique=True,
        sqlite_where=sa.text("is_active = 1"),
    )

    op.create_table(
        "subscription_price_periods",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("subscription_id", sa.Integer(), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
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
        sa.CheckConstraint(
            "amount_minor >= 0",
            name=op.f("ck_subscription_price_periods_subscription_price_nonnegative"),
        ),
        sa.CheckConstraint(
            _MONTH_START_CHECK.format("effective_from"),
            name=op.f(
                "ck_subscription_price_periods_subscription_price_from_month_start"
            ),
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR " + _MONTH_START_CHECK.format("effective_to"),
            name=op.f(
                "ck_subscription_price_periods_subscription_price_to_month_start"
            ),
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name=op.f("ck_subscription_price_periods_subscription_price_range"),
        ),
        sa.CheckConstraint(
            "version >= 1",
            name=op.f(
                "ck_subscription_price_periods_subscription_price_version_positive"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["subscriptions.id"],
            name=op.f("fk_subscription_price_periods_subscription_id_subscriptions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_subscription_price_periods")),
        sa.UniqueConstraint(
            "subscription_id", "effective_from", name="subscription_price_identity"
        ),
    )
    op.create_index(
        "ix_subscription_price_periods_lookup",
        "subscription_price_periods",
        ["subscription_id", "effective_from", "effective_to"],
        unique=False,
    )

    for operation in _OVERLAP_OPERATIONS:
        op.execute(
            _OVERLAP_TRIGGER.format(operation=operation, keyword=operation.upper())
        )


def downgrade() -> None:
    """Remove the subscription register, leaving every B1-B3 table intact."""
    for operation in _OVERLAP_OPERATIONS:
        op.execute(f"DROP TRIGGER subscription_price_no_overlap_{operation}")
    op.drop_index(
        "ix_subscription_price_periods_lookup",
        table_name="subscription_price_periods",
    )
    op.drop_table("subscription_price_periods")
    op.drop_index("uq_subscriptions_active_name", table_name="subscriptions")
    op.drop_index("ix_subscriptions_funding_wallet_id", table_name="subscriptions")
    op.drop_index("ix_subscriptions_currency_active", table_name="subscriptions")
    op.drop_index("ix_subscriptions_budget_category_id", table_name="subscriptions")
    op.drop_table("subscriptions")

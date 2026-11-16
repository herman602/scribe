"""add the monthly salary allocation plan and its frozen runs

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-09 20:00:00.000000

This revision only creates new tables, indexes, and triggers, and inserts two
rows into the ``journal_entry_types`` registry. It rebuilds no existing table,
so the ADR-B027 protocol in ``env.py`` applies unchanged but has nothing to
copy: foreign keys are disabled outside the transaction, the whole migration
runs under ``BEGIN IMMEDIATE``, and a complete ``PRAGMA foreign_key_check``
must pass before it commits.

``salary_receipt`` and ``funding_transfer`` are added by inserting one row each
(ADR-B028 and ADR-B044). ``financial_events`` is deliberately not rebuilt: a
salary is an ordinary ``deposit`` recognized manually, so no new event type is
needed and the most sensitive existing table stays untouched.

A salary run is a snapshot. SQLite cannot express "these columns are writable
and those are not" as a check constraint, so the service writes them once and
BEFORE UPDATE triggers refuse any statement that changes one of them, whichever
path reaches the table.

The downgrade removes an accounting capability, not just a shape. It therefore
refuses, before any DDL, when salary rows exist or when the journal already
records a ``salary_receipt`` or ``funding_transfer`` entry, because dropping
those tables and registry rows would delete financial history or orphan a
posted entry's type. A refusal changes nothing: no schema, no data, and no
revision.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CURRENCY_CHECK = "length({0}) = 3 AND {0} GLOB '[A-Z][A-Z][A-Z]'"
_B5_ENTRY_TYPES = ("salary_receipt", "funding_transfer")
_B5_TABLES = (
    "salary_allocation_run_items",
    "salary_allocation_runs",
    "salary_plan_template_items",
    "salary_plan_templates",
)
_ITEM_SHAPE_CHECK = (
    "(calculation_mode = 'fixed' AND fixed_amount_minor IS NOT NULL "
    "AND fixed_amount_minor >= 0) OR "
    "(calculation_mode IN ('subscription_requirement', 'manual') "
    "AND fixed_amount_minor IS NULL)"
)
_RUN_TIMESTAMP_CHECK = (
    "(status = 'draft' AND approved_at IS NULL AND cancelled_at IS NULL) OR "
    "(status = 'cancelled' AND cancelled_at IS NOT NULL AND approved_at IS NULL) OR "
    "(status IN ('approved', 'partially_funded', 'funded') "
    "AND approved_at IS NOT NULL AND cancelled_at IS NULL)"
)
_ITEM_TRANSFER_CHECK = (
    "(status = 'transferred' AND transfer_entry_id IS NOT NULL "
    "AND transferred_at IS NOT NULL) OR "
    "(status <> 'transferred' AND transfer_entry_id IS NULL "
    "AND transferred_at IS NULL)"
)

#: Written once when the month is generated. ``IS NOT`` rather than ``<>`` so a
#: nullable column compares null-safely instead of yielding an unknown result
#: that would let the change through.
_RUN_FROZEN_COLUMNS = (
    "template_id",
    "template_name_snapshot",
    "template_version",
    "budget_period_id",
    "salary_event_id",
    "source_account_id",
    "source_wallet_id",
    "currency",
    "generation",
    "salary_amount_minor",
    "planned_amount_minor",
    "remainder_minor",
    "subscription_evidence_schema_version",
    "subscription_evidence_json",
    "generated_at",
    "created_at",
)
_RUN_ITEM_FROZEN_COLUMNS = (
    "run_id",
    "template_item_id",
    "template_item_version",
    "name_snapshot",
    "destination_wallet_id",
    "destination_account_id",
    "destination_wallet_name_snapshot",
    "destination_account_name_snapshot",
    "budget_category_id",
    "budget_category_name_snapshot",
    "calculation_mode",
    "amount_minor",
    "currency",
    "display_order",
    "fulfillment_mode",
    "calculation_evidence_json",
    "created_at",
)
_FROZEN_TRIGGERS = (
    (
        "salary_run_snapshot_is_immutable",
        "salary_allocation_runs",
        _RUN_FROZEN_COLUMNS,
        "a salary allocation run snapshot cannot be changed",
    ),
    (
        "salary_run_item_snapshot_is_immutable",
        "salary_allocation_run_items",
        _RUN_ITEM_FROZEN_COLUMNS,
        "a salary allocation run item snapshot cannot be changed",
    ),
)


class SalaryHistoryPresentError(RuntimeError):
    """A downgrade would have deleted salary history or a posted entry type."""


def _frozen_trigger(
    name: str, table: str, columns: tuple[str, ...], message: str
) -> str:
    condition = " OR ".join(f"NEW.{column} IS NOT OLD.{column}" for column in columns)
    return (
        f"CREATE TRIGGER {name} BEFORE UPDATE ON {table} FOR EACH ROW "
        f"WHEN {condition} BEGIN SELECT RAISE(ABORT, '{message}'); END"
    )


def upgrade() -> None:
    """Create the salary plan, its frozen runs, and the two entry types."""
    op.create_table(
        "salary_plan_templates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("source_account_id", sa.Integer(), nullable=False),
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
            _CURRENCY_CHECK.format("currency"),
            name=op.f("ck_salary_plan_templates_salary_template_currency"),
        ),
        sa.CheckConstraint(
            "is_active IN (0, 1)",
            name=op.f("ck_salary_plan_templates_salary_template_active_boolean"),
        ),
        sa.CheckConstraint(
            "version >= 1",
            name=op.f("ck_salary_plan_templates_salary_template_version_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["source_account_id"],
            ["financial_accounts.id"],
            name=op.f("fk_salary_plan_templates_source_account_id_financial_accounts"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_salary_plan_templates")),
    )
    op.create_index(
        "ix_salary_plan_templates_currency_active",
        "salary_plan_templates",
        ["currency", "is_active"],
        unique=False,
    )
    op.create_index(
        "ix_salary_plan_templates_source_account_id",
        "salary_plan_templates",
        ["source_account_id"],
        unique=False,
    )
    op.create_index(
        "uq_salary_plan_templates_active_identity",
        "salary_plan_templates",
        ["name", "currency"],
        unique=True,
        sqlite_where=sa.text("is_active = 1"),
    )

    op.create_table(
        "salary_plan_template_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("template_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("destination_wallet_id", sa.Integer(), nullable=False),
        sa.Column("budget_category_id", sa.Integer(), nullable=True),
        sa.Column("calculation_mode", sa.String(length=32), nullable=False),
        sa.Column("fixed_amount_minor", sa.BigInteger(), nullable=True),
        sa.Column("display_order", sa.Integer(), nullable=False),
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
            "calculation_mode IN ('fixed', 'subscription_requirement', 'manual')",
            name=op.f("ck_salary_plan_template_items_salary_item_calculation_mode"),
        ),
        sa.CheckConstraint(
            _ITEM_SHAPE_CHECK,
            name=op.f("ck_salary_plan_template_items_salary_item_amount_shape"),
        ),
        sa.CheckConstraint(
            "display_order >= 0",
            name=op.f("ck_salary_plan_template_items_salary_item_display_order"),
        ),
        sa.CheckConstraint(
            "is_active IN (0, 1)",
            name=op.f("ck_salary_plan_template_items_salary_item_active_boolean"),
        ),
        sa.CheckConstraint(
            "version >= 1",
            name=op.f("ck_salary_plan_template_items_salary_item_version_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["budget_category_id"],
            ["budget_categories.id"],
            name=op.f(
                "fk_salary_plan_template_items_budget_category_id_budget_categories"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["destination_wallet_id"],
            ["wallets.id"],
            name=op.f("fk_salary_plan_template_items_destination_wallet_id_wallets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["salary_plan_templates.id"],
            name=op.f(
                "fk_salary_plan_template_items_template_id_salary_plan_templates"
            ),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_salary_plan_template_items")),
    )
    op.create_index(
        "ix_salary_plan_template_items_budget_category_id",
        "salary_plan_template_items",
        ["budget_category_id"],
        unique=False,
    )
    op.create_index(
        "ix_salary_plan_template_items_destination_wallet_id",
        "salary_plan_template_items",
        ["destination_wallet_id"],
        unique=False,
    )
    op.create_index(
        "ix_salary_plan_template_items_template_id",
        "salary_plan_template_items",
        ["template_id"],
        unique=False,
    )
    op.create_index(
        "uq_salary_template_items_active_destination",
        "salary_plan_template_items",
        ["template_id", "destination_wallet_id"],
        unique=True,
        sqlite_where=sa.text("is_active = 1"),
    )
    op.create_index(
        "uq_salary_template_items_active_name",
        "salary_plan_template_items",
        ["template_id", "name"],
        unique=True,
        sqlite_where=sa.text("is_active = 1"),
    )

    op.create_table(
        "salary_allocation_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("template_id", sa.Integer(), nullable=False),
        sa.Column("template_name_snapshot", sa.String(length=200), nullable=False),
        sa.Column("template_version", sa.Integer(), nullable=False),
        sa.Column("budget_period_id", sa.Integer(), nullable=False),
        sa.Column("salary_event_id", sa.Integer(), nullable=False),
        sa.Column("source_account_id", sa.Integer(), nullable=False),
        sa.Column("source_wallet_id", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("salary_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("planned_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("remainder_minor", sa.BigInteger(), nullable=False),
        sa.Column("subscription_evidence_schema_version", sa.Integer(), nullable=False),
        sa.Column("subscription_evidence_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("approval_entry_id", sa.Integer(), nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
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
            "status IN ('draft', 'approved', 'partially_funded', 'funded', 'cancelled')",
            name=op.f("ck_salary_allocation_runs_salary_run_status"),
        ),
        sa.CheckConstraint(
            _CURRENCY_CHECK.format("currency"),
            name=op.f("ck_salary_allocation_runs_salary_run_currency"),
        ),
        sa.CheckConstraint(
            "salary_amount_minor > 0",
            name=op.f("ck_salary_allocation_runs_salary_run_amount_positive"),
        ),
        sa.CheckConstraint(
            "planned_amount_minor >= 0",
            name=op.f("ck_salary_allocation_runs_salary_run_planned_bounded"),
        ),
        sa.CheckConstraint(
            "remainder_minor >= 0",
            name=op.f("ck_salary_allocation_runs_salary_run_remainder_bounded"),
        ),
        sa.CheckConstraint(
            "planned_amount_minor + remainder_minor = salary_amount_minor",
            name=op.f("ck_salary_allocation_runs_salary_run_plan_adds_up"),
        ),
        sa.CheckConstraint(
            "generation >= 1",
            name=op.f("ck_salary_allocation_runs_salary_run_generation_positive"),
        ),
        sa.CheckConstraint(
            "version >= 1",
            name=op.f("ck_salary_allocation_runs_salary_run_version_positive"),
        ),
        sa.CheckConstraint(
            "subscription_evidence_schema_version >= 1",
            name=op.f("ck_salary_allocation_runs_salary_run_evidence_version_positive"),
        ),
        sa.CheckConstraint(
            _RUN_TIMESTAMP_CHECK,
            name=op.f("ck_salary_allocation_runs_salary_run_timestamp_shape"),
        ),
        sa.ForeignKeyConstraint(
            ["approval_entry_id"],
            ["journal_entries.id"],
            name=op.f("fk_salary_allocation_runs_approval_entry_id_journal_entries"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["budget_period_id"],
            ["budget_periods.id"],
            name=op.f("fk_salary_allocation_runs_budget_period_id_budget_periods"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["salary_event_id"],
            ["financial_events.id"],
            name=op.f("fk_salary_allocation_runs_salary_event_id_financial_events"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_account_id"],
            ["financial_accounts.id"],
            name=op.f("fk_salary_allocation_runs_source_account_id_financial_accounts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_wallet_id"],
            ["wallets.id"],
            name=op.f("fk_salary_allocation_runs_source_wallet_id_wallets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["salary_plan_templates.id"],
            name=op.f("fk_salary_allocation_runs_template_id_salary_plan_templates"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_salary_allocation_runs")),
        sa.UniqueConstraint(
            "template_id",
            "budget_period_id",
            "generation",
            name="salary_run_generation_identity",
        ),
    )
    op.create_index(
        "ix_salary_allocation_runs_budget_period_id",
        "salary_allocation_runs",
        ["budget_period_id"],
        unique=False,
    )
    op.create_index(
        "ix_salary_allocation_runs_source_wallet_id",
        "salary_allocation_runs",
        ["source_wallet_id"],
        unique=False,
    )
    op.create_index(
        "ix_salary_allocation_runs_status",
        "salary_allocation_runs",
        ["status"],
        unique=False,
    )
    op.create_index(
        "uq_salary_runs_approval_entry",
        "salary_allocation_runs",
        ["approval_entry_id"],
        unique=True,
        sqlite_where=sa.text("approval_entry_id IS NOT NULL"),
    )
    op.create_index(
        "uq_salary_runs_live_period",
        "salary_allocation_runs",
        ["template_id", "budget_period_id"],
        unique=True,
        sqlite_where=sa.text("status <> 'cancelled'"),
    )
    op.create_index(
        "uq_salary_runs_live_salary_event",
        "salary_allocation_runs",
        ["salary_event_id"],
        unique=True,
        sqlite_where=sa.text("status <> 'cancelled'"),
    )

    op.create_table(
        "salary_allocation_run_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("template_item_id", sa.Integer(), nullable=False),
        sa.Column("template_item_version", sa.Integer(), nullable=False),
        sa.Column("name_snapshot", sa.String(length=200), nullable=False),
        sa.Column("destination_wallet_id", sa.Integer(), nullable=False),
        sa.Column("destination_account_id", sa.Integer(), nullable=False),
        sa.Column(
            "destination_wallet_name_snapshot", sa.String(length=200), nullable=False
        ),
        sa.Column(
            "destination_account_name_snapshot", sa.String(length=200), nullable=False
        ),
        sa.Column("budget_category_id", sa.Integer(), nullable=True),
        sa.Column(
            "budget_category_name_snapshot", sa.String(length=200), nullable=True
        ),
        sa.Column("calculation_mode", sa.String(length=32), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.Column("fulfillment_mode", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("calculation_evidence_json", sa.Text(), nullable=False),
        sa.Column("transfer_entry_id", sa.Integer(), nullable=True),
        sa.Column("transferred_at", sa.DateTime(timezone=True), nullable=True),
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
            "calculation_mode IN ('fixed', 'subscription_requirement', 'manual')",
            name=op.f(
                "ck_salary_allocation_run_items_salary_run_item_calculation_mode"
            ),
        ),
        sa.CheckConstraint(
            "fulfillment_mode IN "
            "('reserve_only', 'account_transfer', 'cross_bank_transfer')",
            name=op.f(
                "ck_salary_allocation_run_items_salary_run_item_fulfillment_mode"
            ),
        ),
        sa.CheckConstraint(
            "status IN "
            "('draft', 'not_required', 'allocated', 'waiting_transfer', 'transferred')",
            name=op.f("ck_salary_allocation_run_items_salary_run_item_status"),
        ),
        sa.CheckConstraint(
            _CURRENCY_CHECK.format("currency"),
            name=op.f("ck_salary_allocation_run_items_salary_run_item_currency"),
        ),
        sa.CheckConstraint(
            "amount_minor >= 0",
            name=op.f("ck_salary_allocation_run_items_salary_run_item_amount_bounded"),
        ),
        sa.CheckConstraint(
            "display_order >= 0",
            name=op.f("ck_salary_allocation_run_items_salary_run_item_display_order"),
        ),
        sa.CheckConstraint(
            "version >= 1",
            name=op.f(
                "ck_salary_allocation_run_items_salary_run_item_version_positive"
            ),
        ),
        sa.CheckConstraint(
            _ITEM_TRANSFER_CHECK,
            name=op.f("ck_salary_allocation_run_items_salary_run_item_transfer_shape"),
        ),
        sa.ForeignKeyConstraint(
            ["budget_category_id"],
            ["budget_categories.id"],
            name=op.f(
                "fk_salary_allocation_run_items_budget_category_id_budget_categories"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["destination_account_id"],
            ["financial_accounts.id"],
            name=op.f(
                "fk_salary_allocation_run_items_destination_account_id_"
                "financial_accounts"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["destination_wallet_id"],
            ["wallets.id"],
            name=op.f("fk_salary_allocation_run_items_destination_wallet_id_wallets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["salary_allocation_runs.id"],
            name=op.f("fk_salary_allocation_run_items_run_id_salary_allocation_runs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["template_item_id"],
            ["salary_plan_template_items.id"],
            name=op.f(
                "fk_salary_allocation_run_items_template_item_id_"
                "salary_plan_template_items"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["transfer_entry_id"],
            ["journal_entries.id"],
            name=op.f(
                "fk_salary_allocation_run_items_transfer_entry_id_journal_entries"
            ),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_salary_allocation_run_items")),
        sa.UniqueConstraint(
            "run_id", "template_item_id", name="salary_run_item_identity"
        ),
    )
    op.create_index(
        "ix_salary_allocation_run_items_destination_wallet_id",
        "salary_allocation_run_items",
        ["destination_wallet_id"],
        unique=False,
    )
    op.create_index(
        "ix_salary_allocation_run_items_run_id",
        "salary_allocation_run_items",
        ["run_id"],
        unique=False,
    )
    op.create_index(
        "ix_salary_allocation_run_items_status",
        "salary_allocation_run_items",
        ["status"],
        unique=False,
    )
    op.create_index(
        "uq_salary_run_items_transfer_entry",
        "salary_allocation_run_items",
        ["transfer_entry_id"],
        unique=True,
        sqlite_where=sa.text("transfer_entry_id IS NOT NULL"),
    )

    op.bulk_insert(
        sa.table(
            "journal_entry_types",
            sa.column("code", sa.String),
            sa.column("description", sa.Text),
        ),
        [
            {"code": code, "description": code.replace("_", " ")}
            for code in _B5_ENTRY_TYPES
        ],
    )

    for name, table, columns, message in _FROZEN_TRIGGERS:
        op.execute(_frozen_trigger(name, table, columns, message))


def _reject_when_history_exists() -> None:
    """Refuse before any DDL when a downgrade would destroy real history.

    Only counts are reported. A template name, a destination, and an amount are
    all user data and never belong in a migration error message.
    """
    connection = op.get_bind()
    counts = {
        table: int(
            connection.exec_driver_sql(f"SELECT COUNT(*) FROM {table}").scalar_one()
        )
        for table in _B5_TABLES
    }
    placeholders = ", ".join("?" for _ in _B5_ENTRY_TYPES)
    counts["journal_entries"] = int(
        connection.exec_driver_sql(
            "SELECT COUNT(*) FROM journal_entries WHERE entry_type IN "
            f"({placeholders})",
            _B5_ENTRY_TYPES,
        ).scalar_one()
    )
    present = {table: count for table, count in counts.items() if count}
    if not present:
        return
    detail = ", ".join(f"{table}={count}" for table, count in sorted(present.items()))
    raise SalaryHistoryPresentError(
        "revision 0011 refuses to downgrade because salary history already "
        f"exists ({detail}). Dropping these tables would delete a recorded "
        "salary plan, and removing the journal entry types would orphan a "
        "posted entry. Reverse or remove that history at 0011 first. Nothing "
        "was changed."
    )


def downgrade() -> None:
    """Remove the salary plan, but only from a database that never used it."""
    _reject_when_history_exists()
    for name, _table, _columns, _message in _FROZEN_TRIGGERS:
        op.execute(f"DROP TRIGGER {name}")
    op.execute(
        "DELETE FROM journal_entry_types WHERE code IN ('salary_receipt', "
        "'funding_transfer')"
    )
    op.drop_index(
        "uq_salary_run_items_transfer_entry", table_name="salary_allocation_run_items"
    )
    op.drop_index(
        "ix_salary_allocation_run_items_status",
        table_name="salary_allocation_run_items",
    )
    op.drop_index(
        "ix_salary_allocation_run_items_run_id",
        table_name="salary_allocation_run_items",
    )
    op.drop_index(
        "ix_salary_allocation_run_items_destination_wallet_id",
        table_name="salary_allocation_run_items",
    )
    op.drop_table("salary_allocation_run_items")
    op.drop_index(
        "uq_salary_runs_live_salary_event", table_name="salary_allocation_runs"
    )
    op.drop_index("uq_salary_runs_live_period", table_name="salary_allocation_runs")
    op.drop_index("uq_salary_runs_approval_entry", table_name="salary_allocation_runs")
    op.drop_index(
        "ix_salary_allocation_runs_status", table_name="salary_allocation_runs"
    )
    op.drop_index(
        "ix_salary_allocation_runs_source_wallet_id",
        table_name="salary_allocation_runs",
    )
    op.drop_index(
        "ix_salary_allocation_runs_budget_period_id",
        table_name="salary_allocation_runs",
    )
    op.drop_table("salary_allocation_runs")
    op.drop_index(
        "uq_salary_template_items_active_name", table_name="salary_plan_template_items"
    )
    op.drop_index(
        "uq_salary_template_items_active_destination",
        table_name="salary_plan_template_items",
    )
    op.drop_index(
        "ix_salary_plan_template_items_template_id",
        table_name="salary_plan_template_items",
    )
    op.drop_index(
        "ix_salary_plan_template_items_destination_wallet_id",
        table_name="salary_plan_template_items",
    )
    op.drop_index(
        "ix_salary_plan_template_items_budget_category_id",
        table_name="salary_plan_template_items",
    )
    op.drop_table("salary_plan_template_items")
    op.drop_index(
        "uq_salary_plan_templates_active_identity", table_name="salary_plan_templates"
    )
    op.drop_index(
        "ix_salary_plan_templates_source_account_id", table_name="salary_plan_templates"
    )
    op.drop_index(
        "ix_salary_plan_templates_currency_active", table_name="salary_plan_templates"
    )
    op.drop_table("salary_plan_templates")

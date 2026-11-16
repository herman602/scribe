"""add reviewed cutover batches and pre-opening daily evidence

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-14 23:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | Sequence[str] | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_IMMUTABLE_TABLES = ("import_rows", "legacy_daily_activities")


class ImportHistoryPresentError(RuntimeError):
    """A downgrade would erase applied cutover evidence."""


def upgrade() -> None:
    """Create the audit boundary without modifying an existing finance table."""
    op.create_table(
        "import_batches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_kind", sa.String(50), nullable=False),
        sa.Column("format_version", sa.Integer(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("source_locator_hash", sa.String(64), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cutoff_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("warning_count", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(100)),
        sa.Column("applied_at", sa.DateTime(timezone=True)),
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
            "status IN ('previewed', 'applying', 'applied', 'failed')",
            name=op.f("ck_import_batches_import_batch_status"),
        ),
        sa.CheckConstraint(
            "row_count >= 0",
            name=op.f("ck_import_batches_import_batch_row_count_nonnegative"),
        ),
        sa.CheckConstraint(
            "warning_count >= 0",
            name=op.f("ck_import_batches_import_batch_warning_count_nonnegative"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_batches")),
        sa.UniqueConstraint("content_sha256", name="import_batch_content_sha256"),
    )
    op.create_index("ix_import_batches_observed_at", "import_batches", ["observed_at"])
    op.create_index("ix_import_batches_status", "import_batches", ["status"])
    op.create_table(
        "import_rows",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("batch_id", sa.Integer(), nullable=False),
        sa.Column("entity_kind", sa.String(50), nullable=False),
        sa.Column("source_row_key", sa.String(200), nullable=False),
        sa.Column("normalized_payload_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("reason_code", sa.String(100)),
        sa.Column("created_entity_type", sa.String(50)),
        sa.Column("created_entity_id", sa.Integer()),
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
            "status IN ('applied', 'historical', 'pending', 'skipped', 'warning')",
            name=op.f("ck_import_rows_import_row_status"),
        ),
        sa.CheckConstraint(
            "(created_entity_type IS NULL) = (created_entity_id IS NULL)",
            name=op.f("ck_import_rows_import_row_created_entity_pairing"),
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["import_batches.id"],
            name=op.f("fk_import_rows_batch_id_import_batches"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_rows")),
        sa.UniqueConstraint("batch_id", "source_row_key", name="import_row_source"),
    )
    op.create_index(
        "ix_import_rows_batch_id_entity_kind",
        "import_rows",
        ["batch_id", "entity_kind"],
    )
    op.create_index("ix_import_rows_status", "import_rows", ["status"])
    op.create_table(
        "legacy_daily_activities",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("import_row_id", sa.Integer(), nullable=False),
        sa.Column("activity_date", sa.Date(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("amount_minor", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("payment_method", sa.String(20), nullable=False),
        sa.Column("note", sa.String(500)),
        sa.Column("inclusion_mode", sa.String(30), nullable=False),
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
            "amount_minor > 0",
            name=op.f("ck_legacy_daily_activities_legacy_daily_amount_positive"),
        ),
        sa.CheckConstraint(
            "length(currency) = 3 AND currency GLOB '[A-Z][A-Z][A-Z]'",
            name=op.f("ck_legacy_daily_activities_legacy_daily_currency"),
        ),
        sa.CheckConstraint(
            "payment_method IN ('cash', 'credit_card')",
            name=op.f("ck_legacy_daily_activities_legacy_daily_payment_method"),
        ),
        sa.CheckConstraint(
            "inclusion_mode = 'pre_opening_history'",
            name=op.f("ck_legacy_daily_activities_legacy_daily_inclusion_mode"),
        ),
        sa.ForeignKeyConstraint(
            ["import_row_id"],
            ["import_rows.id"],
            name=op.f("fk_legacy_daily_activities_import_row_id_import_rows"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_legacy_daily_activities")),
        sa.UniqueConstraint("import_row_id", name="legacy_daily_import_row"),
    )
    op.create_index(
        "ix_legacy_daily_activities_activity_date_sequence",
        "legacy_daily_activities",
        ["activity_date", "sequence"],
    )
    for table in _IMMUTABLE_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{table} is immutable'); END"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{table} is immutable'); END"
        )


def downgrade() -> None:
    """Refuse to erase an applied import or its historical daily evidence."""
    bind = op.get_bind()
    counts = {
        table: int(bind.exec_driver_sql(f"SELECT COUNT(*) FROM {table}").scalar_one())
        for table in ("import_batches", "import_rows", "legacy_daily_activities")
    }
    if any(counts.values()):
        raise ImportHistoryPresentError(
            "revision 0016 refuses to downgrade because import history exists "
            f"(batches={counts['import_batches']}, rows={counts['import_rows']}, "
            f"daily={counts['legacy_daily_activities']}); nothing was changed"
        )
    for table in reversed(_IMMUTABLE_TABLES):
        op.execute(f"DROP TRIGGER {table}_no_delete")
        op.execute(f"DROP TRIGGER {table}_no_update")
    op.drop_index(
        "ix_legacy_daily_activities_activity_date_sequence",
        table_name="legacy_daily_activities",
    )
    op.drop_table("legacy_daily_activities")
    op.drop_index("ix_import_rows_status", table_name="import_rows")
    op.drop_index("ix_import_rows_batch_id_entity_kind", table_name="import_rows")
    op.drop_table("import_rows")
    op.drop_index("ix_import_batches_status", table_name="import_batches")
    op.drop_index("ix_import_batches_observed_at", table_name="import_batches")
    op.drop_table("import_batches")

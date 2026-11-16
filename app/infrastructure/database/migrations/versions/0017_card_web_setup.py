"""add explicit card mappings for imported operational hints

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-15 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | Sequence[str] | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


class CardImportAssignmentsPresentError(RuntimeError):
    """A downgrade would erase explicit user decisions."""


def upgrade() -> None:
    """Create mappings without changing immutable import evidence."""
    op.create_table(
        "card_import_assignments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("import_row_id", sa.Integer(), nullable=False),
        sa.Column("card_id", sa.Integer(), nullable=False),
        sa.Column("assignment_kind", sa.String(40), nullable=False),
        sa.Column("statement_day", sa.Integer()),
        sa.Column("due_day", sa.Integer()),
        sa.Column("autopay_day", sa.Integer()),
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
            "assignment_kind IN ('card_cycle', 'scheduled_transfer_reference')",
            name=op.f("ck_card_import_assignments_card_import_assignment_kind"),
        ),
        sa.CheckConstraint(
            "(assignment_kind = 'card_cycle' AND statement_day IS NOT NULL "
            "AND due_day IS NOT NULL AND autopay_day IS NOT NULL) OR "
            "(assignment_kind = 'scheduled_transfer_reference' "
            "AND statement_day IS NULL AND due_day IS NULL AND autopay_day IS NULL)",
            name=op.f("ck_card_import_assignments_card_import_assignment_shape"),
        ),
        sa.CheckConstraint(
            "statement_day IS NULL OR statement_day BETWEEN 1 AND 31",
            name=op.f(
                "ck_card_import_assignments_card_import_assignment_statement_day"
            ),
        ),
        sa.CheckConstraint(
            "due_day IS NULL OR due_day BETWEEN 1 AND 31",
            name=op.f("ck_card_import_assignments_card_import_assignment_due_day"),
        ),
        sa.CheckConstraint(
            "autopay_day IS NULL OR autopay_day BETWEEN 1 AND 31",
            name=op.f("ck_card_import_assignments_card_import_assignment_autopay_day"),
        ),
        sa.CheckConstraint(
            "version >= 1",
            name=op.f("ck_card_import_assignments_card_import_assignment_version"),
        ),
        sa.ForeignKeyConstraint(
            ["import_row_id"],
            ["import_rows.id"],
            name=op.f("fk_card_import_assignments_import_row_id_import_rows"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["card_id"],
            ["cards.id"],
            name=op.f("fk_card_import_assignments_card_id_cards"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_card_import_assignments")),
        sa.UniqueConstraint("import_row_id", name="card_import_assignment_import_row"),
    )
    op.create_index(
        "ix_card_import_assignments_card_id",
        "card_import_assignments",
        ["card_id"],
    )
    op.create_index(
        "uq_card_import_assignment_cycle_card",
        "card_import_assignments",
        ["card_id"],
        unique=True,
        sqlite_where=sa.text("assignment_kind = 'card_cycle'"),
    )


def downgrade() -> None:
    """Refuse to erase reviewed mappings."""
    count = int(
        op.get_bind()
        .exec_driver_sql("SELECT COUNT(*) FROM card_import_assignments")
        .scalar_one()
    )
    if count:
        raise CardImportAssignmentsPresentError(
            "revision 0017 refuses to erase reviewed card mappings "
            f"(assignments={count}); nothing was changed"
        )
    op.drop_index(
        "uq_card_import_assignment_cycle_card",
        table_name="card_import_assignments",
    )
    op.drop_index(
        "ix_card_import_assignments_card_id",
        table_name="card_import_assignments",
    )
    op.drop_table("card_import_assignments")

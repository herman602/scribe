"""add durable Gmail Label outbox

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-06 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create message-level, idempotent Gmail Label operations."""
    op.create_table(
        "gmail_label_operations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("gmail_account_id", sa.Integer(), nullable=False),
        sa.Column("email_id", sa.Integer(), nullable=False),
        sa.Column("gmail_message_id", sa.String(length=100), nullable=False),
        sa.Column("label_name", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
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
            "attempt_count >= 0",
            name=op.f(
                "ck_gmail_label_operations_gmail_label_operation_attempt_nonnegative"
            ),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'succeeded', 'failed')",
            name=op.f("ck_gmail_label_operations_gmail_label_operation_status"),
        ),
        sa.ForeignKeyConstraint(
            ["email_id"],
            ["emails.id"],
            name=op.f("fk_gmail_label_operations_email_id_emails"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["gmail_account_id"],
            ["gmail_accounts.id"],
            name=op.f("fk_gmail_label_operations_gmail_account_id_gmail_accounts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_gmail_label_operations")),
        sa.UniqueConstraint("email_id", "label_name", name="email_gmail_label"),
    )
    op.create_index(
        "ix_gmail_label_operations_processable",
        "gmail_label_operations",
        ["gmail_account_id", "label_name", "status", "id"],
        unique=False,
    )


def downgrade() -> None:
    """Remove Gmail Label operations."""
    op.drop_index(
        "ix_gmail_label_operations_processable",
        table_name="gmail_label_operations",
    )
    op.drop_table("gmail_label_operations")

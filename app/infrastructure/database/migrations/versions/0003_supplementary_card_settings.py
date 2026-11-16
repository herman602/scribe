"""add local supplementary-card mappings

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-28 22:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the account-scoped bank and last-four mapping table."""
    op.create_table(
        "supplementary_card_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("gmail_account_id", sa.Integer(), nullable=False),
        sa.Column("institution_key", sa.String(length=100), nullable=False),
        sa.Column("card_last_four", sa.String(length=4), nullable=False),
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
            "length(card_last_four) = 4 AND card_last_four GLOB '[0-9][0-9][0-9][0-9]'",
            name=op.f("ck_supplementary_card_settings_supplementary_card_last_four"),
        ),
        sa.ForeignKeyConstraint(
            ["gmail_account_id"],
            ["gmail_accounts.id"],
            name=op.f("fk_supplementary_card_settings_gmail_account_id_gmail_accounts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_supplementary_card_settings")),
        sa.UniqueConstraint(
            "gmail_account_id",
            "institution_key",
            "card_last_four",
            name="supplementary_card_identity",
        ),
    )
    op.create_index(
        "ix_supplementary_card_settings_lookup",
        "supplementary_card_settings",
        ["gmail_account_id", "institution_key", "card_last_four"],
        unique=False,
    )


def downgrade() -> None:
    """Remove local supplementary-card mappings."""
    op.drop_index(
        "ix_supplementary_card_settings_lookup",
        table_name="supplementary_card_settings",
    )
    op.drop_table("supplementary_card_settings")

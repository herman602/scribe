"""enforce one running sync per Gmail account

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-28 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Prevent concurrent running runs for one Gmail account."""
    op.create_index(
        "uq_sync_runs_one_running_per_account",
        "sync_runs",
        ["gmail_account_id"],
        unique=True,
        sqlite_where=sa.text("status = 'running'"),
    )


def downgrade() -> None:
    """Remove the single-running-sync database invariant."""
    op.drop_index(
        "uq_sync_runs_one_running_per_account",
        table_name="sync_runs",
    )

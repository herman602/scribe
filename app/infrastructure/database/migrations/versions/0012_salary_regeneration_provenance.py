"""record which draft a salary generation replaced, and on whose request

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-09 19:30:00.000000

Revision `0011` shipped without any record of *why* a generation exists, so a
regenerate replay had to guess: it looked for a later live generation of the
same template and month and assumed that was its own result. It was not
necessarily. Cancelling generation 1 by hand, generating generation 2 through
the ordinary path, and then resubmitting the stale regenerate for generation 1
returned generation 2 and reported success, while the plan the caller actually
asked for had never been written.

Two columns fix that, and `0011` is left exactly as published, because a
database that already ran it — including this user's — would otherwise have no
migration path to the corrected schema. A defect in a released revision is
repaired by the next revision, never by editing it in place; this is the same
rule ADR-B041 established for `0009`.

* ``regenerated_from_run_id`` names the draft this generation replaced. It is a
  self-referencing foreign key with ON DELETE RESTRICT, unique when present so
  at most one generation can claim to replace a given draft, and it can never
  point at its own row.
* ``request_fingerprint`` stores the canonical fingerprint of the request that
  produced it — the previous run, the version it expected, and the sorted
  manual amounts. Provenance alone says which draft was replaced; only the
  fingerprint says whether *this* request is the one that replaced it.

Both are nullable, because an ordinary first generation replaces nothing, and a
check constraint keeps them either both set or both absent.

The table is rebuilt under the ADR-B027 protocol rather than extended with
``ALTER TABLE ADD COLUMN``. SQLite accepts a nullable added column carrying an
inline ``REFERENCES`` clause, but it stores it as a column-level constraint
whose ``ON DELETE`` SQLAlchemy's reflection does not recover, so `alembic
check` would report a permanent phantom difference against the model. A
table-level foreign key is the only shape that reflects back identically.

The frozen-snapshot trigger is recreated on both paths to match the columns the
table then has, because the two new values are decided once when the generation
is written and must not be editable afterwards either.
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RUNS = "salary_allocation_runs"
_TRIGGER = "salary_run_snapshot_is_immutable"
_FROZEN_MESSAGE = "a salary allocation run snapshot cannot be changed"
_PROVENANCE_COLUMNS = ("regenerated_from_run_id", "request_fingerprint")
_FK_NAME = "fk_salary_allocation_runs_regenerated_from_run_id_salary_allocation_runs"
#: Batch operations re-apply the metadata naming convention, so they take the
#: bare name; the rebuilt table definition and every drop take the final one.
_NOT_SELF_BASE = "salary_run_regeneration_not_self"
_PAIRING_BASE = "salary_run_regeneration_pairing"
_NOT_SELF = f"ck_{_RUNS}_{_NOT_SELF_BASE}"
_PAIRING = f"ck_{_RUNS}_{_PAIRING_BASE}"
_NOT_SELF_CHECK = "regenerated_from_run_id IS NULL OR regenerated_from_run_id <> id"
_PAIRING_CHECK = (
    "(regenerated_from_run_id IS NULL AND request_fingerprint IS NULL) OR "
    "(regenerated_from_run_id IS NOT NULL AND request_fingerprint IS NOT NULL)"
)

_CURRENCY_CHECK = "length({0}) = 3 AND {0} GLOB '[A-Z][A-Z][A-Z]'"
_RUN_TIMESTAMP_CHECK = (
    "(status = 'draft' AND approved_at IS NULL AND cancelled_at IS NULL) OR "
    "(status = 'cancelled' AND cancelled_at IS NOT NULL AND approved_at IS NULL) OR "
    "(status IN ('approved', 'partially_funded', 'funded') "
    "AND approved_at IS NOT NULL AND cancelled_at IS NULL)"
)

#: The columns `0011` froze. ``IS NOT`` rather than ``<>`` so a nullable column
#: compares null-safely instead of yielding an unknown result that would let
#: the change through.
_FROZEN_COLUMNS = (
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


def _runs_table(*, with_provenance: bool) -> sa.Table:
    """Describe the runs table exactly, so a rebuild loses nothing.

    SQLite reflection does not recover every check constraint, so the shape is
    stated here rather than guessed, which is what `0007` established for the
    journal rebuild.
    """
    metadata = sa.MetaData()
    columns: list[sa.Column[Any]] = [
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
    ]
    constraints: list[sa.schema.SchemaItem] = [
        sa.CheckConstraint(
            "status IN ('draft', 'approved', 'partially_funded', 'funded', 'cancelled')",
            name="ck_salary_allocation_runs_salary_run_status",
        ),
        sa.CheckConstraint(
            _CURRENCY_CHECK.format("currency"),
            name="ck_salary_allocation_runs_salary_run_currency",
        ),
        sa.CheckConstraint(
            "salary_amount_minor > 0",
            name="ck_salary_allocation_runs_salary_run_amount_positive",
        ),
        sa.CheckConstraint(
            "planned_amount_minor >= 0",
            name="ck_salary_allocation_runs_salary_run_planned_bounded",
        ),
        sa.CheckConstraint(
            "remainder_minor >= 0",
            name="ck_salary_allocation_runs_salary_run_remainder_bounded",
        ),
        sa.CheckConstraint(
            "planned_amount_minor + remainder_minor = salary_amount_minor",
            name="ck_salary_allocation_runs_salary_run_plan_adds_up",
        ),
        sa.CheckConstraint(
            "generation >= 1",
            name="ck_salary_allocation_runs_salary_run_generation_positive",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_salary_allocation_runs_salary_run_version_positive",
        ),
        sa.CheckConstraint(
            "subscription_evidence_schema_version >= 1",
            name="ck_salary_allocation_runs_salary_run_evidence_version_positive",
        ),
        sa.CheckConstraint(
            _RUN_TIMESTAMP_CHECK,
            name="ck_salary_allocation_runs_salary_run_timestamp_shape",
        ),
        sa.ForeignKeyConstraint(
            ["approval_entry_id"],
            ["journal_entries.id"],
            name="fk_salary_allocation_runs_approval_entry_id_journal_entries",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["budget_period_id"],
            ["budget_periods.id"],
            name="fk_salary_allocation_runs_budget_period_id_budget_periods",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["salary_event_id"],
            ["financial_events.id"],
            name="fk_salary_allocation_runs_salary_event_id_financial_events",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_account_id"],
            ["financial_accounts.id"],
            name="fk_salary_allocation_runs_source_account_id_financial_accounts",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_wallet_id"],
            ["wallets.id"],
            name="fk_salary_allocation_runs_source_wallet_id_wallets",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["salary_plan_templates.id"],
            name="fk_salary_allocation_runs_template_id_salary_plan_templates",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_salary_allocation_runs"),
        sa.UniqueConstraint(
            "template_id",
            "budget_period_id",
            "generation",
            name="salary_run_generation_identity",
        ),
    ]
    if with_provenance:
        columns.extend(
            [
                sa.Column("regenerated_from_run_id", sa.Integer(), nullable=True),
                sa.Column("request_fingerprint", sa.String(length=200), nullable=True),
            ]
        )
        constraints.extend(
            [
                sa.CheckConstraint(_NOT_SELF_CHECK, name=_NOT_SELF),
                sa.CheckConstraint(_PAIRING_CHECK, name=_PAIRING),
                sa.ForeignKeyConstraint(
                    ["regenerated_from_run_id"],
                    [f"{_RUNS}.id"],
                    name=_FK_NAME,
                    ondelete="RESTRICT",
                ),
            ]
        )
    return sa.Table(_RUNS, metadata, *columns, *constraints)


_RECREATED_INDEXES = (
    ("ix_salary_allocation_runs_budget_period_id", ["budget_period_id"], False, None),
    ("ix_salary_allocation_runs_source_wallet_id", ["source_wallet_id"], False, None),
    ("ix_salary_allocation_runs_status", ["status"], False, None),
    (
        "uq_salary_runs_approval_entry",
        ["approval_entry_id"],
        True,
        "approval_entry_id IS NOT NULL",
    ),
    (
        "uq_salary_runs_live_period",
        ["template_id", "budget_period_id"],
        True,
        "status <> 'cancelled'",
    ),
    (
        "uq_salary_runs_live_salary_event",
        ["salary_event_id"],
        True,
        "status <> 'cancelled'",
    ),
)


class SalaryRegenerationHistoryPresentError(RuntimeError):
    """A downgrade would have discarded recorded regeneration provenance."""


def _frozen_trigger(columns: tuple[str, ...]) -> str:
    condition = " OR ".join(f"NEW.{column} IS NOT OLD.{column}" for column in columns)
    return (
        f"CREATE TRIGGER {_TRIGGER} BEFORE UPDATE ON {_RUNS} FOR EACH ROW "
        f"WHEN {condition} BEGIN SELECT RAISE(ABORT, '{_FROZEN_MESSAGE}'); END"
    )


def _recreate_indexes() -> None:
    """Recreate every index the rebuild dropped along with the old table."""
    for name, columns, unique, where in _RECREATED_INDEXES:
        op.create_index(
            name,
            _RUNS,
            columns,
            unique=unique,
            sqlite_where=None if where is None else sa.text(where),
        )


def upgrade() -> None:
    """Rebuild the runs table with its two provenance columns."""
    op.execute(f"DROP TRIGGER {_TRIGGER}")
    with op.batch_alter_table(
        _RUNS, copy_from=_runs_table(with_provenance=False)
    ) as batch_op:
        batch_op.add_column(
            sa.Column("regenerated_from_run_id", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("request_fingerprint", sa.String(length=200), nullable=True)
        )
        batch_op.create_foreign_key(
            _FK_NAME, _RUNS, ["regenerated_from_run_id"], ["id"], ondelete="RESTRICT"
        )
        batch_op.create_check_constraint(_NOT_SELF_BASE, _NOT_SELF_CHECK)
        batch_op.create_check_constraint(_PAIRING_BASE, _PAIRING_CHECK)
    _recreate_indexes()
    op.create_index(
        "uq_salary_runs_regenerated_from",
        _RUNS,
        ["regenerated_from_run_id"],
        unique=True,
        sqlite_where=sa.text("regenerated_from_run_id IS NOT NULL"),
    )
    op.execute(_frozen_trigger((*_FROZEN_COLUMNS, *_PROVENANCE_COLUMNS)))


def _reject_when_provenance_exists() -> None:
    """Refuse before any DDL when a downgrade would lose recorded history.

    Only a count is reported. Which draft a generation replaced is a fact about
    the user's money, and no name or amount belongs in a migration error.
    """
    recorded = int(
        op.get_bind()
        .exec_driver_sql(
            f"SELECT COUNT(*) FROM {_RUNS} WHERE regenerated_from_run_id IS NOT NULL"
        )
        .scalar_one()
    )
    if recorded:
        raise SalaryRegenerationHistoryPresentError(
            "revision 0012 refuses to downgrade because "
            f"{recorded} salary generation(s) record which draft they replaced. "
            "Dropping these columns would delete that link and make a resubmitted "
            "regeneration unrecognizable again. Nothing was changed."
        )


def downgrade() -> None:
    """Return to the schema revision 0011 was published with."""
    _reject_when_provenance_exists()
    op.execute(f"DROP TRIGGER {_TRIGGER}")
    op.drop_index("uq_salary_runs_regenerated_from", table_name=_RUNS)
    with op.batch_alter_table(
        _RUNS, copy_from=_runs_table(with_provenance=True)
    ) as batch_op:
        batch_op.drop_constraint(op.f(_PAIRING), type_="check")
        batch_op.drop_constraint(op.f(_NOT_SELF), type_="check")
        batch_op.drop_constraint(op.f(_FK_NAME), type_="foreignkey")
        for column in reversed(_PROVENANCE_COLUMNS):
            batch_op.drop_column(column)
    _recreate_indexes()
    op.execute(_frozen_trigger(_FROZEN_COLUMNS))

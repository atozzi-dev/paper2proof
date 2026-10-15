"""ledger entries

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-15 09:27:05.693070
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:

    op.create_table(
        "ledger_entries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(length=28), nullable=True),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("ledger_entries", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_ledger_entries_day"), ["day"], unique=False)
        batch_op.create_index(batch_op.f("ix_ledger_entries_run_id"), ["run_id"], unique=False)


def downgrade() -> None:

    with op.batch_alter_table("ledger_entries", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_ledger_entries_run_id"))
        batch_op.drop_index(batch_op.f("ix_ledger_entries_day"))

    op.drop_table("ledger_entries")

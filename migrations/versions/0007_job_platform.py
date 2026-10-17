"""where each job runs: platform and preset

The reaper charges a job nobody watched at the on-demand rate of its own platform and preset,
so an exec job moved to the fallback GPU platform is not charged at the primary's rate. Rows
recorded before this revision keep NULL and are charged at the rate of their kind.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-17 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("platform", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("preset", sa.String(length=32), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.drop_column("preset")
        batch_op.drop_column("platform")

"""run approval

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-15 11:12:05.027200
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:

    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("approval", sa.String(length=16), nullable=True))


def downgrade() -> None:

    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_column("approval")

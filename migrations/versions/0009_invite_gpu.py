"""a daily GPU quota per invite, and the invite each run was created with

GPU when needed (A-47): a run that would be cut on CPU gets a GPU plan only while its invite
still has GPU runs today. Invites from before this revision allow none (CPU only), and runs
from before it have no invite recorded, so they never get a GPU.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-16 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("invites", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("gpu_runs_per_day", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("gpu_used_today", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("gpu_used_on", sa.Date(), nullable=True))
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("invite_hash", sa.String(length=64), nullable=True))
        batch_op.add_column(
            sa.Column("gpu_granted", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_column("gpu_granted")
        batch_op.drop_column("invite_hash")
    with op.batch_alter_table("invites", schema=None) as batch_op:
        batch_op.drop_column("gpu_used_on")
        batch_op.drop_column("gpu_used_today")
        batch_op.drop_column("gpu_runs_per_day")

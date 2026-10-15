"""trusted overlay digests

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-15 15:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "overlay_digests",
        sa.Column("cache_key", sa.String(length=64), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("cache_key"),
    )
    op.create_index("ix_overlay_digests_created_at", "overlay_digests", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_overlay_digests_created_at", table_name="overlay_digests")
    op.drop_table("overlay_digests")

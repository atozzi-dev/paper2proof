"""discovery cache

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-15 12:53:28.725977
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:

    op.create_table(
        "discovery_cache",
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("response", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("provider", "key"),
    )


def downgrade() -> None:

    op.drop_table("discovery_cache")

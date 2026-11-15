"""the paper's title on each run

The report pages show the paper's title instead of its arXiv ID. INGESTING stores it, already
cleaned to plain text (app/ingestion/title.py). Runs ingested before this revision keep NULL
and the pages fall back to the arXiv ID.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-15 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("title", sa.String(length=300), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_column("title")

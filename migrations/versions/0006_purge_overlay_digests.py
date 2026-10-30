"""drop overlay digests recorded by the single provision job

Until the provisioning split (A-06), the job that wrote p2p-cache and reported these digests also
parsed untrusted datasets and cloned untrusted repositories. None of them is trusted any more:
each cache key is rebuilt once by a provision job that touches only PyPI wheels.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-16 12:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DELETE FROM overlay_digests")


def downgrade() -> None:
    pass  # the dropped digests are never restored

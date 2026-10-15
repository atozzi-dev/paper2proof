"""initial

Revision ID: 0001
Revises: (none)
Create Date: 2026-10-15 09:22:52.582190
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:

    op.create_table(
        "invites",
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("quota_per_day", sa.Integer(), nullable=False),
        sa.Column("used_today", sa.Integer(), nullable=False),
        sa.Column("used_on", sa.Date(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("code_hash"),
    )
    op.create_table(
        "ledger_daily",
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("inference_usd", sa.Float(), nullable=False),
        sa.Column("cloud_usd", sa.Float(), nullable=False),
        sa.Column("kill_switch", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("date"),
    )
    op.create_table(
        "runs",
        sa.Column("id", sa.String(length=28), nullable=False),
        sa.Column("arxiv_id", sa.String(length=20), nullable=False),
        sa.Column("paper_sha256", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("error_code", sa.String(length=32), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column("claim_hint", sa.Text(), nullable=True),
        sa.Column("budget_json", sa.JSON(), nullable=False),
        sa.Column("listed", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("verdict_summary", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_runs_arxiv_id"), ["arxiv_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_runs_status"), ["status"], unique=False)

    op.create_table(
        "claims",
        sa.Column("run_id", sa.String(length=28), nullable=False),
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("claim_type", sa.String(length=16), nullable=False),
        sa.Column("metric", sa.String(length=64), nullable=False),
        sa.Column("higher_is_better", sa.Boolean(), nullable=False),
        sa.Column("dataset", sa.Text(), nullable=False),
        sa.Column("setting", sa.Text(), nullable=False),
        sa.Column("reported_value", sa.Float(), nullable=True),
        sa.Column("reported_std", sa.Float(), nullable=True),
        sa.Column("baseline_name", sa.Text(), nullable=True),
        sa.Column("claimed_delta", sa.Float(), nullable=True),
        sa.Column("source_ref", sa.Text(), nullable=False),
        sa.Column("selected", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id", "id"),
    )
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(length=28), nullable=False),
        sa.Column("ts", sa.DateTime(), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_events_run_id"), ["run_id"], unique=False)

    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(length=28), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("nebius_job_id", sa.String(length=64), nullable=True),
        sa.Column("bucket", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("gpu_seconds", sa.Float(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("deadline", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_jobs_created_at"), ["created_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_jobs_run_id"), ["run_id"], unique=False)

    op.create_table(
        "model_calls",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(length=28), nullable=True),
        sa.Column("step", sa.String(length=32), nullable=True),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("model_id", sa.String(length=128), nullable=False),
        sa.Column("prompt_version", sa.String(length=32), nullable=True),
        sa.Column("tokens_in", sa.Integer(), nullable=False),
        sa.Column("tokens_out", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("ratelimit_remaining", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("model_calls", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_model_calls_created_at"), ["created_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_model_calls_run_id"), ["run_id"], unique=False)

    op.create_table(
        "steps",
        sa.Column("run_id", sa.String(length=28), nullable=False),
        sa.Column("name", sa.String(length=32), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("error_code", sa.String(length=32), nullable=True),
        sa.Column("output_ref", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id", "name", "attempt"),
    )
    op.create_table(
        "verdicts",
        sa.Column("run_id", sa.String(length=28), nullable=False),
        sa.Column("claim_id", sa.String(length=64), nullable=False),
        sa.Column("verdict", sa.String(length=32), nullable=False),
        sa.Column("reason_code", sa.String(length=32), nullable=True),
        sa.Column("scale", sa.String(length=8), nullable=False),
        sa.Column("measured_mean", sa.Float(), nullable=True),
        sa.Column("measured_std", sa.Float(), nullable=True),
        sa.Column("ci_low", sa.Float(), nullable=True),
        sa.Column("ci_high", sa.Float(), nullable=True),
        sa.Column("n_seeds", sa.Integer(), nullable=False),
        sa.Column("tolerance_json", sa.JSON(), nullable=False),
        sa.Column("config_sha256", sa.String(length=64), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id", "claim_id"),
    )


def downgrade() -> None:

    op.drop_table("verdicts")
    op.drop_table("steps")
    with op.batch_alter_table("model_calls", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_model_calls_run_id"))
        batch_op.drop_index(batch_op.f("ix_model_calls_created_at"))

    op.drop_table("model_calls")
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_jobs_run_id"))
        batch_op.drop_index(batch_op.f("ix_jobs_created_at"))

    op.drop_table("jobs")
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_events_run_id"))

    op.drop_table("events")
    op.drop_table("claims")
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_runs_status"))
        batch_op.drop_index(batch_op.f("ix_runs_arxiv_id"))

    op.drop_table("runs")
    op.drop_table("ledger_daily")
    op.drop_table("invites")

"""SQLAlchemy tables (architecture §10.2, v0.2 columns). Migrations live in migrations/versions."""

from datetime import UTC, date, datetime
from typing import Any, ClassVar

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class UTCDateTime(TypeDecorator[datetime]):
    """Store UTC, always return timezone-aware UTC (SQLite drops the offset)."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetimes are not allowed; use UTC")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        dict[str, Any]: JSON,
        datetime: UTCDateTime,
        date: Date,
    }


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(28), primary_key=True)
    arxiv_id: Mapped[str] = mapped_column(String(20), index=True)
    paper_sha256: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), index=True)
    error_code: Mapped[str | None] = mapped_column(String(32))
    created_by: Mapped[str | None] = mapped_column(String(64))  # sha256 of the invite code
    claim_hint: Mapped[str | None] = mapped_column(Text)
    budget_json: Mapped[dict[str, Any]]
    listed: Mapped[bool] = mapped_column(Boolean, default=False)  # public gallery (admin)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None]
    verdict_summary: Mapped[str | None] = mapped_column(String(64))


class ClaimRow(Base):
    __tablename__ = "claims"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    claim_type: Mapped[str] = mapped_column(String(16))
    metric: Mapped[str] = mapped_column(String(64))
    higher_is_better: Mapped[bool]
    dataset: Mapped[str] = mapped_column(Text)
    setting: Mapped[str] = mapped_column(Text)
    reported_value: Mapped[float | None] = mapped_column(Float)
    reported_std: Mapped[float | None] = mapped_column(Float)
    baseline_name: Mapped[str | None] = mapped_column(Text)
    claimed_delta: Mapped[float | None] = mapped_column(Float)
    source_ref: Mapped[str] = mapped_column(Text)
    selected: Mapped[bool] = mapped_column(Boolean, default=False)


class StepRow(Base):
    __tablename__ = "steps"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    attempt: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None]
    error_code: Mapped[str | None] = mapped_column(String(32))
    output_ref: Mapped[str | None] = mapped_column(Text)


class EventRow(Base):
    __tablename__ = "events"
    # ids are never reused (AUTOINCREMENT): clients resume SSE with Last-Event-ID
    __table_args__ = ({"sqlite_autoincrement": True},)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime] = mapped_column(default=utcnow)
    type: Mapped[str] = mapped_column(String(32))
    payload_json: Mapped[dict[str, Any]]


class ModelCallRow(Base):
    __tablename__ = "model_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), index=True
    )
    step: Mapped[str | None] = mapped_column(String(32))
    role: Mapped[str] = mapped_column(String(16))
    model_id: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    tokens_in: Mapped[int]
    tokens_out: Mapped[int]
    latency_ms: Mapped[int]
    cost_usd: Mapped[float | None] = mapped_column(Float)  # None: model not priced
    ratelimit_remaining: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)


class JobRow(Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(8))
    name: Mapped[str] = mapped_column(String(64))  # deterministic: p2p-<kind>-<slug>-<attempt>
    nebius_job_id: Mapped[str | None] = mapped_column(String(64))
    bucket: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    gpu_seconds: Mapped[float | None] = mapped_column(Float)
    cost_usd: Mapped[float | None] = mapped_column(Float)
    deadline: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)


class VerdictRow(Base):
    __tablename__ = "verdicts"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    claim_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    verdict: Mapped[str] = mapped_column(String(32))
    reason_code: Mapped[str | None] = mapped_column(String(32))
    scale: Mapped[str] = mapped_column(String(8))
    measured_mean: Mapped[float | None] = mapped_column(Float)
    measured_std: Mapped[float | None] = mapped_column(Float)
    ci_low: Mapped[float | None] = mapped_column(Float)
    ci_high: Mapped[float | None] = mapped_column(Float)
    n_seeds: Mapped[int]
    tolerance_json: Mapped[dict[str, Any]]
    config_sha256: Mapped[str] = mapped_column(String(64))
    explanation: Mapped[str] = mapped_column(Text)


class InviteRow(Base):
    __tablename__ = "invites"

    code_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    quota_per_day: Mapped[int]
    used_today: Mapped[int] = mapped_column(Integer, default=0)
    used_on: Mapped[date | None]
    expires_at: Mapped[datetime | None]


class LedgerDaily(Base):
    __tablename__ = "ledger_daily"

    day: Mapped[date] = mapped_column("date", primary_key=True)  # UTC date
    inference_usd: Mapped[float] = mapped_column(Float, default=0.0)
    cloud_usd: Mapped[float] = mapped_column(Float, default=0.0)
    kill_switch: Mapped[bool] = mapped_column(Boolean, default=False)

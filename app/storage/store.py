"""Persistence API used by the orchestrator, API and reaper. One writer process (the endpoint)."""

from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.domain.events import PAYLOAD_MODELS, EventEnvelope, EventType
from app.domain.models import (
    Claim,
    ErrorCode,
    ReasonCode,
    RunStatus,
    Scale,
    StepName,
    Verdict,
    VerdictKind,
)
from app.storage.tables import (
    ClaimRow,
    EventRow,
    JobRow,
    ModelCallRow,
    Run,
    StepRow,
    VerdictRow,
    utcnow,
)

TERMINAL_STEP_STATUSES = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True)


class RunRecord(_Record):
    id: str
    arxiv_id: str
    status: RunStatus
    error_code: ErrorCode | None
    created_by: str | None
    claim_hint: str | None
    budget: dict[str, Any]
    listed: bool
    created_at: datetime
    finished_at: datetime | None


class StepRecord(_Record):
    name: StepName
    attempt: int
    status: str
    started_at: datetime
    finished_at: datetime | None
    error_code: ErrorCode | None
    output_ref: str | None


class DailySpend(_Record):
    inference_usd: float
    cloud_usd: float


class Store:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    def _session(self) -> AsyncSession:
        return self._sessions()

    # --- runs ------------------------------------------------------------------------

    async def create_run(
        self,
        run_id: str,
        arxiv_id: str,
        *,
        created_by: str | None,
        claim_hint: str | None,
        budget: dict[str, Any],
    ) -> None:
        async with self._session() as s, s.begin():
            s.add(
                Run(
                    id=run_id,
                    arxiv_id=arxiv_id,
                    status=RunStatus.QUEUED.value,
                    created_by=created_by,
                    claim_hint=claim_hint,
                    budget_json=budget,
                )
            )

    async def get_run(self, run_id: str) -> RunRecord | None:
        async with self._session() as s:
            row = await s.get(Run, run_id)
            if row is None:
                return None
            return RunRecord(
                id=row.id,
                arxiv_id=row.arxiv_id,
                status=RunStatus(row.status),
                error_code=ErrorCode(row.error_code) if row.error_code else None,
                created_by=row.created_by,
                claim_hint=row.claim_hint,
                budget=row.budget_json,
                listed=row.listed,
                created_at=row.created_at,
                finished_at=row.finished_at,
            )

    async def set_run_status(
        self, run_id: str, status: RunStatus, error_code: ErrorCode | None = None
    ) -> None:
        async with self._session() as s, s.begin():
            row = await s.get(Run, run_id)
            if row is None:
                raise KeyError(run_id)
            row.status = status.value
            row.error_code = error_code.value if error_code else None
            if status.is_terminal and row.finished_at is None:
                row.finished_at = utcnow()

    # --- steps -----------------------------------------------------------------------

    async def upsert_step(
        self,
        run_id: str,
        name: StepName,
        attempt: int,
        status: str,
        *,
        error_code: ErrorCode | None = None,
        output_ref: str | None = None,
    ) -> None:
        """Idempotent on (run_id, step, attempt): the FSM's idempotency key."""
        async with self._session() as s, s.begin():
            row = await s.get(StepRow, (run_id, name.value, attempt))
            if row is None:
                row = StepRow(run_id=run_id, name=name.value, attempt=attempt, status=status)
                s.add(row)
            row.status = status
            row.error_code = error_code.value if error_code else None
            if output_ref is not None:
                row.output_ref = output_ref
            if status in TERMINAL_STEP_STATUSES:
                row.finished_at = utcnow()

    async def get_steps(self, run_id: str) -> list[StepRecord]:
        async with self._session() as s:
            rows = await s.scalars(
                select(StepRow)
                .where(StepRow.run_id == run_id)
                .order_by(StepRow.started_at, StepRow.name, StepRow.attempt)
            )
            return [
                StepRecord(
                    name=StepName(r.name),
                    attempt=r.attempt,
                    status=r.status,
                    started_at=r.started_at,
                    finished_at=r.finished_at,
                    error_code=ErrorCode(r.error_code) if r.error_code else None,
                    output_ref=r.output_ref,
                )
                for r in rows
            ]

    # --- events ----------------------------------------------------------------------

    async def append_event(
        self, run_id: str, type: EventType, payload: BaseModel | dict[str, Any]
    ) -> int:
        """Validate the payload for its type and store it; returns the monotonic event id."""
        model = PAYLOAD_MODELS[type]
        try:
            validated = model.model_validate(
                payload.model_dump() if isinstance(payload, BaseModel) else payload
            )
        except ValidationError as err:
            raise ValueError(f"invalid payload for {type}: {err.error_count()} error(s)") from err
        async with self._session() as s, s.begin():
            row = EventRow(
                run_id=run_id, type=type.value, payload_json=validated.model_dump(mode="json")
            )
            s.add(row)
            await s.flush()
            return row.id

    async def events_after(self, run_id: str, after_id: int) -> list[EventEnvelope]:
        async with self._session() as s:
            rows = await s.scalars(
                select(EventRow)
                .where(EventRow.run_id == run_id, EventRow.id > after_id)
                .order_by(EventRow.id)
            )
            return [
                EventEnvelope.model_validate(
                    {
                        "id": r.id,
                        "run_id": r.run_id,
                        "ts": r.ts,
                        "type": r.type,
                        "payload": r.payload_json,
                    }
                )
                for r in rows
            ]

    # --- ledger inputs ---------------------------------------------------------------

    async def record_model_call(
        self,
        run_id: str | None,
        step: str | None,
        role: str,
        model_id: str,
        tokens_in: int,
        tokens_out: int,
        latency_ms: int,
        cost_usd: float | None,
        *,
        ratelimit_remaining: int | None = None,
        prompt_version: str | None = None,
        at: datetime | None = None,
    ) -> int:
        async with self._session() as s, s.begin():
            row = ModelCallRow(
                run_id=run_id,
                step=step,
                role=role,
                model_id=model_id,
                prompt_version=prompt_version,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                latency_ms=latency_ms,
                cost_usd=cost_usd,
                ratelimit_remaining=ratelimit_remaining,
                created_at=at or utcnow(),
            )
            s.add(row)
            await s.flush()
            return row.id

    async def record_job(
        self,
        run_id: str,
        kind: str,
        name: str,
        nebius_job_id: str | None,
        bucket: str | None,
        *,
        deadline: datetime | None = None,
        at: datetime | None = None,
    ) -> int:
        """Insert-or-ignore by the deterministic job name; returns the row id either way."""
        async with self._session() as s, s.begin():
            await s.execute(
                sqlite_insert(JobRow)
                .values(
                    run_id=run_id,
                    kind=kind,
                    name=name,
                    nebius_job_id=nebius_job_id,
                    bucket=bucket,
                    status="SUBMITTED",
                    deadline=deadline,
                    created_at=at or utcnow(),
                )
                .on_conflict_do_nothing(index_elements=["name"])
            )
            job_id = await s.scalar(select(JobRow.id).where(JobRow.name == name))
            if job_id is None:  # pragma: no cover - the row exists after the upsert
                raise RuntimeError(f"job {name} not recorded")
            return job_id

    async def update_job(
        self,
        name: str,
        status: str,
        *,
        gpu_seconds: float | None = None,
        cost_usd: float | None = None,
    ) -> None:
        async with self._session() as s, s.begin():
            row = await s.scalar(select(JobRow).where(JobRow.name == name))
            if row is None:
                raise KeyError(name)
            row.status = status
            if gpu_seconds is not None:
                row.gpu_seconds = gpu_seconds
            if cost_usd is not None:
                row.cost_usd = cost_usd

    async def daily_spend(self, day: date) -> DailySpend:
        """Sum of model and job costs whose rows were created on this UTC date."""
        start = datetime.combine(day, time(), tzinfo=UTC)
        end = start + timedelta(days=1)
        async with self._session() as s:
            inference = await s.scalar(
                select(func.coalesce(func.sum(ModelCallRow.cost_usd), 0.0)).where(
                    ModelCallRow.created_at >= start, ModelCallRow.created_at < end
                )
            )
            cloud = await s.scalar(
                select(func.coalesce(func.sum(JobRow.cost_usd), 0.0)).where(
                    JobRow.created_at >= start, JobRow.created_at < end
                )
            )
        return DailySpend(inference_usd=float(inference or 0.0), cloud_usd=float(cloud or 0.0))

    # --- claims and verdicts -----------------------------------------------------------

    async def save_claims(self, run_id: str, claims: list[Claim], *, selected: set[str]) -> None:
        async with self._session() as s, s.begin():
            for claim in claims:
                await s.merge(
                    ClaimRow(run_id=run_id, selected=claim.id in selected, **claim.model_dump())
                )

    async def get_claims(self, run_id: str) -> list[tuple[Claim, bool]]:
        async with self._session() as s:
            rows = await s.scalars(
                select(ClaimRow).where(ClaimRow.run_id == run_id).order_by(ClaimRow.id)
            )
            fields = Claim.model_fields
            return [
                (Claim.model_validate({k: getattr(r, k) for k in fields}), r.selected) for r in rows
            ]

    async def save_verdict(
        self,
        run_id: str,
        claim_id: str,
        verdict: Verdict,
        scale: Scale,
        config_sha256: str,
        explanation: str,
    ) -> None:
        async with self._session() as s, s.begin():
            await s.merge(
                VerdictRow(
                    run_id=run_id,
                    claim_id=claim_id,
                    verdict=verdict.kind.value,
                    reason_code=verdict.reason.value if verdict.reason else None,
                    scale=scale.value,
                    measured_mean=verdict.mean,
                    measured_std=verdict.std,
                    ci_low=verdict.ci_low,
                    ci_high=verdict.ci_high,
                    n_seeds=verdict.n_seeds,
                    tolerance_json=verdict.tolerance,
                    config_sha256=config_sha256,
                    explanation=explanation,
                )
            )

    async def get_verdict(self, run_id: str, claim_id: str) -> Verdict | None:
        async with self._session() as s:
            row = await s.get(VerdictRow, (run_id, claim_id))
            if row is None:
                return None
            return Verdict(
                kind=VerdictKind(row.verdict),
                reason=ReasonCode(row.reason_code) if row.reason_code else None,
                n_seeds=row.n_seeds,
                mean=row.measured_mean,
                std=row.measured_std,
                ci_low=row.ci_low,
                ci_high=row.ci_high,
                tolerance=row.tolerance_json,
            )

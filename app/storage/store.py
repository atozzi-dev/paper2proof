"""Persistence API used by the orchestrator, API and reaper. One writer process (the endpoint)."""

from datetime import UTC, date, datetime, time, timedelta
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import Update, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import CursorResult
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
    DiscoveryCache,
    EventRow,
    InviteRow,
    JobRow,
    LedgerDaily,
    LedgerEntry,
    ModelCallRow,
    OverlayDigest,
    Run,
    StepRow,
    VerdictRow,
    utcnow,
)

TERMINAL_STEP_STATUSES = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})


class StateConflict(Exception):
    """A compare-and-set on a run or step failed: someone else changed it first."""


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
    approval: str | None
    verdict_summary: str | None
    paper_sha256: str | None
    created_at: datetime
    finished_at: datetime | None


def _run_record(row: Run) -> RunRecord:
    return RunRecord(
        id=row.id,
        arxiv_id=row.arxiv_id,
        status=RunStatus(row.status),
        error_code=ErrorCode(row.error_code) if row.error_code else None,
        created_by=row.created_by,
        claim_hint=row.claim_hint,
        budget=row.budget_json,
        listed=row.listed,
        approval=row.approval,
        verdict_summary=row.verdict_summary,
        paper_sha256=row.paper_sha256,
        created_at=row.created_at,
        finished_at=row.finished_at,
    )


class StepRecord(_Record):
    name: StepName
    attempt: int
    status: str
    started_at: datetime
    finished_at: datetime | None
    error_code: ErrorCode | None
    output_ref: str | None


class JobRecord(_Record):
    name: str
    kind: str
    run_id: str
    nebius_job_id: str | None
    status: str
    deadline: datetime | None
    created_at: datetime
    gpu_seconds: float | None = None
    cost_usd: float | None = None


def _job_record(row: JobRow) -> JobRecord:
    return JobRecord(
        name=row.name,
        kind=row.kind,
        run_id=row.run_id,
        nebius_job_id=row.nebius_job_id,
        status=row.status,
        deadline=row.deadline,
        created_at=row.created_at,
        gpu_seconds=row.gpu_seconds,
        cost_usd=row.cost_usd,
    )


class ModelCallRecord(_Record):
    step: str | None
    role: str
    model_id: str
    prompt_version: str | None
    tokens_in: int
    tokens_out: int
    cost_usd: float | None


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
            return _run_record(row)

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

    async def get_job(self, name: str) -> JobRecord | None:
        async with self._session() as s:
            row = await s.scalar(select(JobRow).where(JobRow.name == name))
            return None if row is None else _job_record(row)

    async def list_jobs(self, run_id: str) -> list[JobRecord]:
        async with self._session() as s:
            rows = await s.scalars(
                select(JobRow).where(JobRow.run_id == run_id).order_by(JobRow.id)
            )
            return [_job_record(row) for row in rows]

    async def list_model_calls(self, run_id: str) -> list[ModelCallRecord]:
        async with self._session() as s:
            rows = await s.scalars(
                select(ModelCallRow).where(ModelCallRow.run_id == run_id).order_by(ModelCallRow.id)
            )
            return [
                ModelCallRecord(
                    step=row.step,
                    role=row.role,
                    model_id=row.model_id,
                    prompt_version=row.prompt_version,
                    tokens_in=row.tokens_in,
                    tokens_out=row.tokens_out,
                    cost_usd=row.cost_usd,
                )
                for row in rows
            ]

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

    # --- budget ledger (app.orchestrator.budget) -------------------------------------------

    async def add_ledger_entry(
        self, run_id: str | None, day: date, kind: str, amount: float
    ) -> None:
        async with self._session() as s, s.begin():
            s.add(LedgerEntry(run_id=run_id, day=day, kind=kind, amount=amount))

    async def ledger_total(
        self, kind: str, *, run_id: str | None = None, day: date | None = None
    ) -> float:
        """Committed spend of one kind, filtered by run and/or UTC day."""
        query = select(func.coalesce(func.sum(LedgerEntry.amount), 0.0)).where(
            LedgerEntry.kind == kind
        )
        if run_id is not None:
            query = query.where(LedgerEntry.run_id == run_id)
        if day is not None:
            query = query.where(LedgerEntry.day == day)
        async with self._session() as s:
            return float(await s.scalar(query) or 0.0)

    async def kill_switch(self, day: date) -> bool:
        async with self._session() as s:
            row = await s.get(LedgerDaily, day)
            return bool(row and row.kill_switch)

    async def set_kill_switch(self, day: date, on: bool) -> None:
        async with self._session() as s, s.begin():
            row = await s.get(LedgerDaily, day)
            if row is None:
                row = LedgerDaily(day=day, inference_usd=0.0, cloud_usd=0.0, kill_switch=on)
                s.add(row)
            row.kill_switch = on

    # --- FSM transitions (app.orchestrator.fsm) -------------------------------------------

    async def active_run_ids(self) -> list[str]:
        """Runs that are not terminal, oldest first (resumed on startup)."""
        active = [RunStatus.QUEUED.value, RunStatus.RUNNING.value]
        async with self._session() as s:
            rows = await s.scalars(
                select(Run.id).where(Run.status.in_(active)).order_by(Run.created_at, Run.id)
            )
            return list(rows)

    async def _cas(self, s: AsyncSession, statement: Update) -> bool:
        """Run a conditional UPDATE; True iff exactly one row matched. The condition is
        evaluated by SQLite under its write lock, so read-then-write races can't happen."""
        result = await s.execute(statement.execution_options(synchronize_session=False))
        return cast("CursorResult[Any]", result).rowcount == 1

    async def begin_run(self, run_id: str, step: StepName, attempt: int) -> None:
        """QUEUED -> RUNNING and open the first step, atomically (compare-and-set)."""
        async with self._session() as s, s.begin():
            moved = await self._cas(
                s,
                update(Run)
                .where(Run.id == run_id, Run.status == RunStatus.QUEUED.value)
                .values(status=RunStatus.RUNNING.value),
            )
            if not moved:
                raise StateConflict(f"run {run_id} is not QUEUED")
            if await s.get(StepRow, (run_id, step.value, attempt)) is None:
                s.add(StepRow(run_id=run_id, name=step.value, attempt=attempt, status="RUNNING"))

    async def transition(
        self,
        run_id: str,
        step: StepName,
        attempt: int,
        step_status: str,
        *,
        error_code: ErrorCode | None = None,
        output_ref: str | None = None,
        next_step: tuple[StepName, int] | None = None,
        run_status: RunStatus | None = None,
    ) -> None:
        """Close a step and either open the next one or set the run's final status, in one
        transaction: after a crash there is always exactly one RUNNING step to resume.
        Both rows are compare-and-set: a concurrent driver or an external status change
        (cancel, kill switch, admin) makes the transition fail instead of being overwritten."""
        if (next_step is None) == (run_status is None):
            raise ValueError("pass exactly one of next_step and run_status")
        now = utcnow()
        async with self._session() as s, s.begin():
            run_values: dict[str, object] = {"status": RunStatus.RUNNING.value}
            if next_step is not None and next_step[0] is StepName.AWAITING_APPROVAL:
                run_values["approval"] = None  # each approval step needs a fresh decision
            if run_status is not None:
                run_values = {
                    "status": run_status.value,
                    "error_code": error_code.value if error_code else None,
                    "finished_at": now if run_status.is_terminal else None,
                }
            if not await self._cas(
                s,
                update(Run)
                .where(Run.id == run_id, Run.status == RunStatus.RUNNING.value)
                .values(**run_values),
            ):
                raise StateConflict(f"run {run_id} is not RUNNING")
            step_values: dict[str, object] = {
                "status": step_status,
                "error_code": error_code.value if error_code else None,
                "finished_at": now,
            }
            if output_ref is not None:
                step_values["output_ref"] = output_ref
            if not await self._cas(
                s,
                update(StepRow)
                .where(
                    StepRow.run_id == run_id,
                    StepRow.name == step.value,
                    StepRow.attempt == attempt,
                    StepRow.status == "RUNNING",
                )
                .values(**step_values),
            ):
                raise StateConflict(f"step {step.value}#{attempt} of {run_id} is not RUNNING")
            if next_step is not None:
                name, next_attempt = next_step
                s.add(
                    StepRow(run_id=run_id, name=name.value, attempt=next_attempt, status="RUNNING")
                )

    async def close_step(self, run_id: str, step: StepName, attempt: int, status: str) -> None:
        """Close a RUNNING step without touching the run (used after a StateConflict)."""
        async with self._session() as s, s.begin():
            await self._cas(
                s,
                update(StepRow)
                .where(
                    StepRow.run_id == run_id,
                    StepRow.name == step.value,
                    StepRow.attempt == attempt,
                    StepRow.status == "RUNNING",
                )
                .values(status=status, finished_at=utcnow()),
            )

    # --- API support (A-13) -----------------------------------------------------------

    async def cancel_run(self, run_id: str) -> bool:
        """QUEUED/RUNNING -> CANCELLED (compare-and-set); the FSM stops at its next check."""
        active = (RunStatus.QUEUED.value, RunStatus.RUNNING.value)
        async with self._session() as s, s.begin():
            return await self._cas(
                s,
                update(Run)
                .where(Run.id == run_id, Run.status.in_(active))
                .values(status=RunStatus.CANCELLED.value, finished_at=utcnow()),
            )

    async def decide_approval(self, run_id: str, decision: str) -> bool:
        """Record the user's decision once: only while the run is RUNNING, its approval step is
        RUNNING and no decision exists yet (conditional UPDATE). False otherwise."""
        awaiting = (
            select(StepRow.run_id)
            .where(
                StepRow.run_id == run_id,
                StepRow.name == StepName.AWAITING_APPROVAL.value,
                StepRow.status == "RUNNING",
            )
            .exists()
        )
        async with self._session() as s, s.begin():
            return await self._cas(
                s,
                update(Run)
                .where(
                    Run.id == run_id,
                    Run.status == RunStatus.RUNNING.value,
                    Run.approval.is_(None),
                    awaiting,
                )
                .values(approval=decision),
            )

    async def set_listed(self, run_id: str, listed: bool) -> bool:
        async with self._session() as s, s.begin():
            run = await s.get(Run, run_id)
            if run is None:
                return False
            run.listed = listed
            return True

    async def list_gallery(self, limit: int = 100) -> list[RunRecord]:
        async with self._session() as s:
            rows = await s.scalars(
                select(Run).where(Run.listed.is_(True)).order_by(Run.created_at.desc()).limit(limit)
            )
            return [_run_record(r) for r in rows]

    async def verdict_counts(self) -> dict[str, int]:
        async with self._session() as s:
            rows = await s.execute(
                select(VerdictRow.verdict, func.count()).group_by(VerdictRow.verdict)
            )
            return {str(v): int(n) for v, n in rows.all()}

    async def list_verdicts(self, run_id: str) -> list[tuple[str, Verdict]]:
        async with self._session() as s:
            ids = await s.scalars(
                select(VerdictRow.claim_id)
                .where(VerdictRow.run_id == run_id)
                .order_by(VerdictRow.claim_id)
            )
            claim_ids = list(ids)
        verdicts = [(cid, await self.get_verdict(run_id, cid)) for cid in claim_ids]
        return [(cid, v) for cid, v in verdicts if v is not None]

    async def create_invite(
        self, code_hash: str, quota_per_day: int, expires_at: datetime | None
    ) -> None:
        async with self._session() as s, s.begin():
            s.add(
                InviteRow(
                    code_hash=code_hash,
                    quota_per_day=quota_per_day,
                    used_today=0,
                    expires_at=expires_at,
                )
            )

    async def invite_valid(self, code_hash: str, now: datetime) -> bool:
        async with self._session() as s:
            row = await s.get(InviteRow, code_hash)
            return row is not None and (row.expires_at is None or row.expires_at > now)

    async def consume_invite(self, code_hash: str, now: datetime) -> bool:
        """Count one live run against the invite's daily quota; False when exhausted/invalid.
        Two conditional UPDATEs (new day / same day): no read-modify-write race."""
        today = now.astimezone(UTC).date()
        valid = InviteRow.expires_at.is_(None) | (InviteRow.expires_at > now)
        async with self._session() as s, s.begin():
            if await self._cas(
                s,
                update(InviteRow)
                .where(
                    InviteRow.code_hash == code_hash,
                    valid,
                    InviteRow.used_on.is_(None) | (InviteRow.used_on != today),
                    InviteRow.quota_per_day >= 1,
                )
                .values(used_on=today, used_today=1),
            ):
                return True
            return await self._cas(
                s,
                update(InviteRow)
                .where(
                    InviteRow.code_hash == code_hash,
                    valid,
                    InviteRow.used_on == today,
                    InviteRow.used_today < InviteRow.quota_per_day,
                )
                .values(used_today=InviteRow.used_today + 1),
            )

    async def set_paper_sha256(self, run_id: str, sha256: str) -> None:
        async with self._session() as s, s.begin():
            await s.execute(update(Run).where(Run.id == run_id).values(paper_sha256=sha256))

    # --- discovery cache (A-17) ---------------------------------------------------------

    async def trust_overlay(
        self, cache_key: str, sha256: str, run_id: str | None, *, at: datetime | None = None
    ) -> None:
        """First writer wins: a later run can never replace a trusted digest."""
        async with self._session() as s, s.begin():
            await s.execute(
                sqlite_insert(OverlayDigest)
                .values(
                    cache_key=cache_key, sha256=sha256, run_id=run_id, created_at=at or utcnow()
                )
                .on_conflict_do_nothing(index_elements=["cache_key"])
            )

    async def trusted_overlays(self, limit: int = 300) -> dict[str, str]:
        """The most recent trusted digests (they ride in the provision manifest, <= 64 KiB)."""
        async with self._session() as s:
            rows = await s.execute(
                select(OverlayDigest.cache_key, OverlayDigest.sha256)
                .order_by(OverlayDigest.created_at.desc())
                .limit(limit)
            )
            return {key: digest for key, digest in rows}

    async def cache_get(
        self, provider: str, key: str, *, max_age: timedelta, now: datetime | None = None
    ) -> dict[str, Any] | None:
        async with self._session() as s:
            row = await s.get(DiscoveryCache, (provider, key))
            if row is None or (now or utcnow()) - row.created_at > max_age:
                return None
            return dict(row.response)

    async def cache_put(self, provider: str, key: str, response: dict[str, Any]) -> None:
        async with self._session() as s, s.begin():
            await s.merge(
                DiscoveryCache(provider=provider, key=key, response=response, created_at=utcnow())
            )

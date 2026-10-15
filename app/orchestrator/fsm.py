"""Typed finite-state machine that drives a run (architecture §6.1).

Each step is idempotent on (run_id, step, attempt). Every transition is persisted atomically
(Store.transition), so a restarted process resumes from the single RUNNING step with the same
attempt number, and deterministic job names make re-submissions return the existing job.
"""

import asyncio
import logging
import time
from collections.abc import Mapping
from concurrent.futures import Executor
from dataclasses import dataclass
from typing import Protocol

from app.domain.errors import P2PError
from app.domain.events import EventType
from app.domain.models import ErrorCode, RunStatus, StepName
from app.execution.backend import JobBackend
from app.orchestrator.budget import Budget, BudgetExceeded, BudgetLimits, KillSwitchActive
from app.orchestrator.context import RunContext
from app.storage.store import StateConflict, Store

log = logging.getLogger(__name__)

S = StepName
ALWAYS: frozenset[StepName | RunStatus] = frozenset(
    {RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.BUDGET_EXCEEDED}
)
TRANSITIONS: dict[StepName, frozenset[StepName | RunStatus]] = {
    S.INGESTING: frozenset({S.EXTRACTING_CLAIMS}),
    S.EXTRACTING_CLAIMS: frozenset({S.DISCOVERING}),
    S.DISCOVERING: frozenset({S.PLANNING}),
    S.PLANNING: frozenset({S.AWAITING_APPROVAL, S.PROVISIONING}),
    S.AWAITING_APPROVAL: frozenset({S.PROVISIONING}),
    S.PROVISIONING: frozenset({S.SMOKE_TESTING}),
    S.SMOKE_TESTING: frozenset({S.SMOKE_TESTING, S.PROVISIONING, S.EXECUTING}),
    S.EXECUTING: frozenset({S.ANALYZING}),
    S.ANALYZING: frozenset({S.VERDICT}),
    S.VERDICT: frozenset({RunStatus.COMPLETED}),
}
FIRST_STEP = S.INGESTING
STEP_STATUS_FOR_RUN = {RunStatus.COMPLETED: "SUCCEEDED", RunStatus.CANCELLED: "CANCELLED"}
MAX_ERROR_MESSAGE = 2000  # ErrorPayload limit


@dataclass(frozen=True)
class StepResult:
    next: StepName | RunStatus
    output_ref: str | None = None
    error_code: ErrorCode | None = None  # required with a FAILED/BUDGET_EXCEEDED `next`

    def __post_init__(self) -> None:
        failing = (RunStatus.FAILED, RunStatus.BUDGET_EXCEEDED)
        if self.next in failing and self.error_code is None:
            raise ValueError("a failing StepResult needs an error_code")


class Step(Protocol):
    name: StepName

    async def run(self, ctx: RunContext) -> StepResult: ...


def allowed(step: StepName, target: StepName | RunStatus) -> bool:
    return target in TRANSITIONS[step] or target in ALWAYS


@dataclass(frozen=True)
class _Ending:
    status: RunStatus
    error_code: ErrorCode | None
    message: str | None
    output_ref: str | None = None


class Orchestrator:
    def __init__(
        self,
        store: Store,
        budget: Budget,
        steps: Mapping[StepName, Step],
        *,
        limits: BudgetLimits,
        jobs: JobBackend,
        cpu_pool: Executor | None = None,
    ) -> None:
        self._store = store
        self._budget = budget
        self._steps = steps
        self._limits = limits
        self._jobs = jobs
        self._cpu_pool = cpu_pool
        self._slots = asyncio.Semaphore(limits.global_.concurrent_runs)
        self._tasks: dict[str, asyncio.Task[RunStatus]] = {}

    async def submit(self, run_id: str) -> "asyncio.Task[RunStatus]":
        """Start driving a run; a run already being driven returns the existing task."""
        current = self._tasks.get(run_id)
        if current is not None and not current.done():
            return current
        task = asyncio.create_task(self._drive(run_id), name=f"run-{run_id}")
        self._tasks[run_id] = task
        task.add_done_callback(lambda t: self._tasks.pop(run_id, None) if t is task else None)
        return task

    async def resume_all(self) -> list["asyncio.Task[RunStatus]"]:
        """On startup: restart every non-terminal run from its last unfinished step."""
        return [await self.submit(run_id) for run_id in await self._store.active_run_ids()]

    async def _emit(self, run_id: str, type: EventType, payload: dict[str, object]) -> None:
        await self._store.append_event(run_id, type, payload)

    async def _resume_point(self, run_id: str) -> tuple[StepName, int] | None:
        steps = await self._store.get_steps(run_id)
        running = [s for s in steps if s.status == "RUNNING"]
        if len(running) == 1:
            return running[0].name, running[0].attempt
        if not steps:
            return FIRST_STEP, 1
        return None  # inconsistent history: handled as an internal error

    async def _next_attempt(self, run_id: str, step: StepName) -> int:
        attempts = [s.attempt for s in await self._store.get_steps(run_id) if s.name is step]
        return max(attempts, default=0) + 1

    async def _drive(self, run_id: str) -> RunStatus:
        async with self._slots:
            run = await self._store.get_run(run_id)
            if run is None:
                raise KeyError(run_id)
            if run.status.is_terminal:
                return run.status
            point = await self._resume_point(run_id)
            if point is None:
                await self._store.set_run_status(run_id, RunStatus.FAILED, ErrorCode.INTERNAL_ERROR)
                return RunStatus.FAILED
            step, attempt = point
            if run.status is RunStatus.QUEUED:
                try:
                    await self._store.begin_run(run_id, step, attempt)
                except StateConflict:
                    return await self._current_status(run_id)
                await self._emit(run_id, EventType.RUN_STATUS, {"status": RunStatus.RUNNING})
            while True:
                # Stop between steps if someone (user, admin, reaper) ended the run meanwhile.
                current = await self._current_status(run_id)
                if current is not RunStatus.RUNNING:
                    await self._store.close_step(run_id, step, attempt, "CANCELLED")
                    return current
                try:
                    outcome = await self._run_step(run_id, step, attempt)
                except StateConflict:
                    await self._store.close_step(run_id, step, attempt, "CANCELLED")
                    return await self._current_status(run_id)
                if isinstance(outcome, RunStatus):
                    return outcome
                step, attempt = outcome

    async def _current_status(self, run_id: str) -> RunStatus:
        run = await self._store.get_run(run_id)
        return run.status if run is not None else RunStatus.FAILED

    async def _execute(self, ctx: RunContext) -> StepResult | _Ending:
        """Run the step and map exceptions to a terminal ending (never re-raises Exception)."""
        try:
            return await self._steps[ctx.step].run(ctx)
        except BudgetExceeded as err:
            message = f"budget limit reached: {err.limit_name}"
            return _Ending(RunStatus.BUDGET_EXCEEDED, ErrorCode.BUDGET_EXCEEDED, message)
        except KillSwitchActive:
            message = "kill switch active: no new cloud spend today"
            return _Ending(RunStatus.FAILED, ErrorCode.KILL_SWITCH, message)
        except P2PError as err:
            return _Ending(RunStatus.FAILED, err.code, err.message)
        except Exception:
            log.exception("step failed", extra={"run_id": ctx.run_id, "step": ctx.step.value})
            message = f"internal error in step {ctx.step.value}"
            return _Ending(RunStatus.FAILED, ErrorCode.INTERNAL_ERROR, message)

    async def _check(self, ctx: RunContext, result: StepResult) -> _Ending | None:
        """Validate the requested transition; returns an ending if the run must stop here."""
        target = result.next
        if not allowed(ctx.step, target):
            message = f"illegal transition {ctx.step.value} -> {target}"
            return _Ending(RunStatus.FAILED, ErrorCode.INTERNAL_ERROR, message)
        if isinstance(target, RunStatus):
            ended = None if target is RunStatus.COMPLETED else f"run ended: {target.value}"
            return _Ending(target, result.error_code, ended, result.output_ref)
        if ctx.step is S.SMOKE_TESTING and target is S.PROVISIONING:
            requests_done = await self._next_attempt(ctx.run_id, S.PROVISIONING) - 2
            if requests_done >= self._limits.per_run.package_requests:
                message = "package request limit reached"
                return _Ending(RunStatus.FAILED, ErrorCode.PACKAGE_REJECTED, message)
        return None

    async def _run_step(
        self, run_id: str, step: StepName, attempt: int
    ) -> RunStatus | tuple[StepName, int]:
        await self._emit(run_id, EventType.STEP_STARTED, {"step": step, "attempt": attempt})
        started = time.monotonic()
        ctx = RunContext(
            run_id=run_id,
            step=step,
            attempt=attempt,
            store=self._store,
            budget=self._budget,
            limits=self._limits,
            jobs=self._jobs,
            cpu_pool=self._cpu_pool,
        )
        result = await self._execute(ctx)
        ending = result if isinstance(result, _Ending) else await self._check(ctx, result)
        if ending is not None:
            return await self._finish(ctx, started, ending)
        if isinstance(result, _Ending) or not isinstance(result.next, StepName):
            raise RuntimeError("unreachable: endings are handled above")  # pragma: no cover
        target = result.next
        next_attempt = await self._next_attempt(run_id, target)
        await self._store.transition(
            run_id,
            step,
            attempt,
            "SUCCEEDED",
            output_ref=result.output_ref,
            next_step=(target, next_attempt),
        )
        await self._emit_finished(ctx, started, ok=True, error_code=None)
        return target, next_attempt

    async def _emit_finished(
        self, ctx: RunContext, started: float, *, ok: bool, error_code: ErrorCode | None
    ) -> None:
        payload: dict[str, object] = {
            "step": ctx.step,
            "attempt": ctx.attempt,
            "ok": ok,
            "error_code": error_code,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        await self._emit(ctx.run_id, EventType.STEP_FINISHED, payload)

    async def _finish(self, ctx: RunContext, started: float, ending: _Ending) -> RunStatus:
        """End the run from this step with a terminal status (one transaction), then emit."""
        status, code = ending.status, ending.error_code
        await self._store.transition(
            ctx.run_id,
            ctx.step,
            ctx.attempt,
            STEP_STATUS_FOR_RUN.get(status, "FAILED"),
            error_code=code,
            output_ref=ending.output_ref,
            run_status=status,
        )
        ok = status is RunStatus.COMPLETED
        await self._emit_finished(ctx, started, ok=ok, error_code=code)
        if code is not None and not ok and status is not RunStatus.CANCELLED:
            message = (ending.message or code.value)[:MAX_ERROR_MESSAGE]
            await self._emit(ctx.run_id, EventType.ERROR, {"code": code, "message": message})
        await self._emit(ctx.run_id, EventType.RUN_STATUS, {"status": status, "error_code": code})
        return status

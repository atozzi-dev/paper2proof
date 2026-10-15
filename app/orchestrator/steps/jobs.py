"""Run one job to a terminal state: budget first, ledger after, status events, watchdog, and
cancellation when the run is cancelled meanwhile (shared by PROVISIONING and EXECUTING).

The watchdog counts from the job's first submission (the `jobs` row), so a step resumed after a
restart keeps the original deadline. Each invocation settles the budget for the time it watched.
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.domain.events import EventType
from app.domain.models import RunStatus
from app.execution.backend import JobHandle, JobSpec, JobState
from app.execution.naming import bucket_name
from app.orchestrator.budget import SpendKind
from app.orchestrator.context import RunContext

STATUS_ATTEMPTS = 4  # transient control-plane errors while polling


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class JobOutcome:
    state: JobState
    handle: JobHandle
    elapsed_s: float
    timed_out: bool = False
    run_cancelled: bool = False


async def _status(
    ctx: RunContext, handle: JobHandle, sleep: Callable[[float], Awaitable[None]]
) -> JobState:
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(STATUS_ATTEMPTS),
        wait=wait_exponential(multiplier=2, max=30),
        retry=retry_if_exception_type(Exception),
        sleep=sleep,
        reraise=True,
    ):
        with attempt:
            return await ctx.jobs.status(handle)
    raise AssertionError("unreachable")  # pragma: no cover


async def run_job(
    ctx: RunContext,
    spec: JobSpec,
    *,
    usd_per_hour: float,
    poll_s: float,
    clock: Callable[[], datetime] = utcnow,
    sleep: Callable[[float], Awaitable[None]],
    gpu: bool = False,
) -> JobOutcome:
    """Submit (get-by-name first, so a resumed step finds its job) and wait for the job."""
    watchdog = timedelta(seconds=spec.watchdog_s)
    cost = await ctx.budget.check_and_reserve(
        ctx.run_id, SpendKind.CLOUD_USD, usd_per_hour * spec.watchdog_s / 3600
    )
    minutes = None
    if gpu:
        try:
            minutes = await ctx.budget.check_and_reserve(
                ctx.run_id, SpendKind.GPU_MINUTES, spec.watchdog_s / 60
            )
        except BaseException:
            await ctx.budget.commit(cost, 0)
            raise
    began = clock()
    try:
        existing = await ctx.store.get_job(spec.name)
    except BaseException:
        await ctx.budget.commit(cost, 0)
        if minutes is not None:
            await ctx.budget.commit(minutes, 0)
        raise
    started = existing.created_at if existing is not None else began

    async def settle() -> None:
        watched_s = max(0.0, (clock() - began).total_seconds())
        await ctx.budget.commit(cost, usd_per_hour * watched_s / 3600)
        if minutes is not None:
            await ctx.budget.commit(minutes, watched_s / 60)

    async def job_status(state: JobState) -> None:
        await ctx.emit(
            EventType.JOB_STATUS,
            {"kind": spec.kind.value, "name": spec.name, "state": state.value},
        )

    handle: JobHandle | None = None
    state = JobState.UNKNOWN
    timed_out = cancelled = False
    try:
        handle = await ctx.jobs.submit(spec)
        await ctx.store.record_job(
            ctx.run_id,
            spec.kind.value,
            spec.name,
            handle.nebius_id,
            bucket_name(ctx.run_id),
            deadline=started + watchdog,
            at=started,
        )
        last: JobState | None = None
        while True:
            state = await _status(ctx, handle, sleep)
            if state is not last:
                await job_status(state)
                last = state
            if state.is_terminal:
                break
            run = await ctx.store.get_run(ctx.run_id)
            if run is None or run.status is not RunStatus.RUNNING:
                cancelled = True
            elif clock() - started >= watchdog:
                timed_out = True
            if cancelled or timed_out:
                await ctx.jobs.cancel(handle)
                state = JobState.CANCELLED
                await job_status(state)
                break
            await sleep(poll_s)
    except asyncio.CancelledError:
        # Shutdown: the job keeps running and the resumed step re-attaches to it by name (the
        # reaper cancels it at its deadline if nobody does).
        await settle()
        raise
    except Exception:
        if handle is not None and not state.is_terminal:  # nobody would watch it any more
            with contextlib.suppress(Exception):
                await asyncio.shield(ctx.jobs.cancel(handle))
        await settle()
        raise
    await settle()
    elapsed_s = max(0.0, (clock() - started).total_seconds())
    await ctx.store.update_job(
        spec.name,
        state.value,
        gpu_seconds=elapsed_s if gpu else None,
        cost_usd=round(usd_per_hour * elapsed_s / 3600, 6),
    )
    return JobOutcome(state, handle, elapsed_s, timed_out=timed_out, run_cancelled=cancelled)

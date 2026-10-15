"""Run one job to a terminal state: budget first, ledger after, status events, watchdog, and
cancellation when the run is cancelled meanwhile (shared by PROVISIONING and EXECUTING)."""

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.domain.events import EventType
from app.domain.models import RunStatus
from app.execution.backend import JobHandle, JobSpec, JobState
from app.execution.naming import bucket_name
from app.orchestrator.budget import SpendKind
from app.orchestrator.context import RunContext


@dataclass(frozen=True)
class JobOutcome:
    state: JobState
    handle: JobHandle
    elapsed_s: float
    timed_out: bool = False
    run_cancelled: bool = False


async def run_job(
    ctx: RunContext,
    spec: JobSpec,
    *,
    usd_per_hour: float,
    poll_s: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]],
    gpu: bool = False,
) -> JobOutcome:
    """Submit (get-by-name first, so a resumed step finds its job) and wait for the job."""
    watchdog_h = spec.watchdog_s / 3600
    cost = await ctx.budget.check_and_reserve(
        ctx.run_id, SpendKind.CLOUD_USD, usd_per_hour * watchdog_h
    )
    minutes = None
    if gpu:
        minutes = await ctx.budget.check_and_reserve(
            ctx.run_id, SpendKind.GPU_MINUTES, spec.watchdog_s / 60
        )
    started = clock()
    try:
        handle = await ctx.jobs.submit(spec)
        deadline = datetime.now(UTC) + timedelta(seconds=spec.watchdog_s)
        await ctx.store.record_job(
            ctx.run_id,
            spec.kind.value,
            spec.name,
            handle.nebius_id,
            bucket_name(ctx.run_id),
            deadline=deadline,
        )
        last: JobState | None = None
        timed_out = cancelled = False
        while True:
            state = await ctx.jobs.status(handle)
            if state is not last:
                await ctx.emit(
                    EventType.JOB_STATUS,
                    {"kind": spec.kind.value, "name": spec.name, "state": state.value},
                )
                last = state
            if state.is_terminal:
                break
            run = await ctx.store.get_run(ctx.run_id)
            if run is None or run.status is not RunStatus.RUNNING:
                cancelled = True
            elif clock() - started >= spec.watchdog_s:
                timed_out = True
            if cancelled or timed_out:
                await ctx.jobs.cancel(handle)
                state = JobState.CANCELLED
                await ctx.emit(
                    EventType.JOB_STATUS,
                    {"kind": spec.kind.value, "name": spec.name, "state": state.value},
                )
                break
            await sleep(poll_s)
    except BaseException:
        elapsed = clock() - started
        await ctx.budget.commit(cost, usd_per_hour * elapsed / 3600)
        if minutes is not None:
            await ctx.budget.commit(minutes, elapsed / 60)
        raise
    elapsed = clock() - started
    actual = usd_per_hour * elapsed / 3600
    await ctx.budget.commit(cost, actual)
    if minutes is not None:
        await ctx.budget.commit(minutes, elapsed / 60)
    await ctx.store.update_job(
        spec.name, state.value, gpu_seconds=elapsed if gpu else None, cost_usd=round(actual, 6)
    )
    return JobOutcome(state, handle, elapsed, timed_out=timed_out, run_cancelled=cancelled)

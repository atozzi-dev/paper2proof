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
from app.execution.backend import CapacityError, JobHandle, JobKind, JobSpec, JobState
from app.execution.naming import bucket_name
from app.orchestrator.budget import BudgetExceeded, KillSwitchActive, SpendKind
from app.orchestrator.context import RunContext
from app.storage.store import JobRecord

STATUS_ATTEMPTS = 4  # transient control-plane errors while polling
STOP_WAIT_S = 300  # how long a cancelled job may take to stop


def utcnow() -> datetime:
    return datetime.now(UTC)


def event_kind(kind: JobKind) -> str:
    """The kind events carry (`JobKindName`, what the UI knows): the fetch job is part of
    provisioning, and its name tells it apart."""
    return JobKind.PROVISION.value if kind is JobKind.FETCH else kind.value


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


_SETTLED = frozenset(state.value for state in JobState if state.is_terminal)


async def _cancel_unwatched(ctx: RunContext, existing: JobRecord | None) -> None:
    """A recorded job that can't be watched any more is cancelled, never left running."""
    if existing is None or existing.status in _SETTLED or existing.nebius_job_id is None:
        return
    with contextlib.suppress(Exception):
        await asyncio.shield(
            ctx.jobs.cancel(JobHandle(name=existing.name, nebius_id=existing.nebius_job_id))
        )


async def stop_jobs(
    ctx: RunContext,
    prefix: str,
    *,
    poll_s: float,
    sleep: Callable[[float], Awaitable[None]],
    keep: Callable[[str], bool] = lambda name: False,
    wait_s: float = STOP_WAIT_S,
) -> bool:
    """Cancel the jobs named `prefix*` that are still running (except those `keep` names) and
    wait until they have stopped: a job left behind by a restart still mounts the run bucket
    read-write. False when one is still running after `wait_s`."""
    running = [
        handle
        for handle, state, _ in await ctx.jobs.list_jobs(prefix)
        if not state.is_terminal and not keep(handle.name)
    ]
    for handle in running:
        await ctx.jobs.cancel(handle)
    waited = 0.0
    while True:
        running = [h for h in running if not (await _status(ctx, h, sleep)).is_terminal]
        if not running:
            return True
        if waited >= wait_s:
            return False
        await sleep(poll_s)
        waited += poll_s


async def run_job(
    ctx: RunContext,
    spec: JobSpec,
    *,
    usd_per_hour: float,
    poll_s: float,
    clock: Callable[[], datetime] = utcnow,
    sleep: Callable[[float], Awaitable[None]],
    gpu: bool = False,
    started: datetime | None = None,
    on_poll: Callable[[], Awaitable[None]] | None = None,
) -> JobOutcome:
    """Submit (get-by-name first, so a resumed step finds its job) and wait for the job.
    `started` anchors a new job's watchdog when the caller already put a deadline derived from
    it into the job (a recorded job always keeps its first submission time). `on_poll` runs
    while the job is still running, before each wait (display only: it must not raise)."""
    watchdog = timedelta(seconds=spec.watchdog_s)
    existing = await ctx.store.get_job(spec.name)
    now = clock()
    reserve_s = float(spec.watchdog_s)
    began = now
    if existing is not None:
        # Re-attaching after a restart: reserve what is left of its watchdog. A job that was
        # still running is charged from its submission (the gap is never free, at worst it
        # counts twice); one that was settled already costs nothing more.
        started = existing.created_at
        reserve_s = max(0.0, (started + watchdog - now).total_seconds())
        if existing.status not in _SETTLED:
            began = started
    elif started is None:
        started = now
    try:
        cost = await ctx.budget.check_and_reserve(
            ctx.run_id, SpendKind.CLOUD_USD, usd_per_hour * reserve_s / 3600
        )
    except (BudgetExceeded, KillSwitchActive):
        await _cancel_unwatched(ctx, existing)
        raise
    minutes = None
    if gpu:
        try:
            minutes = await ctx.budget.check_and_reserve(
                ctx.run_id, SpendKind.GPU_MINUTES, reserve_s / 60
            )
        except BaseException as err:
            await ctx.budget.commit(cost, 0)
            if isinstance(err, BudgetExceeded | KillSwitchActive):
                await _cancel_unwatched(ctx, existing)
            raise

    async def settle(refused: bool = False) -> None:
        # A creation refused for lack of capacity left no job behind: nothing to pay.
        watched_s = 0.0 if refused else max(0.0, (clock() - began).total_seconds())
        await ctx.budget.commit(cost, usd_per_hour * watched_s / 3600)
        if minutes is not None:
            await ctx.budget.commit(minutes, watched_s / 60)

    async def job_status(state: JobState) -> None:
        await ctx.emit(
            EventType.JOB_STATUS,
            {"kind": event_kind(spec.kind), "name": spec.name, "state": state.value},
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
            platform=spec.platform,
            preset=spec.preset,
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
            if on_poll is not None:
                await on_poll()
            await sleep(poll_s)
    except asyncio.CancelledError:
        # Shutdown: the job keeps running and the resumed step re-attaches to it by name (the
        # reaper cancels it at its deadline if nobody does).
        await settle()
        raise
    except Exception as err:
        if handle is not None and not state.is_terminal:  # nobody would watch it any more
            with contextlib.suppress(Exception):
                await asyncio.shield(ctx.jobs.cancel(handle))
        await settle(refused=handle is None and isinstance(err, CapacityError))
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

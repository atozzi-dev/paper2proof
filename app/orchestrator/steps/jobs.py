"""Run one job to a terminal state: budget first, ledger after, status events, watchdog, and
cancellation when the run is cancelled meanwhile (shared by PROVISIONING and EXECUTING).

The watchdog counts from when the job leaves the platform queue (S4 waited ~16 minutes in
PROVISIONING with no error), recorded in the `jobs` row so a resumed step keeps that deadline;
a job still queued after QUEUE_CAP_S is cancelled (the caller treats it as no capacity). A
caller that baked a deadline into the job passes `started`, and its watchdog counts from there.
GPU minutes count from the last poll that still saw the job queued (never less than it ran);
cloud USD from submission (whether PROVISIONING is billed was never checked). A job cancelled
at the queue cap is watched until it stops; one that doesn't is left to the reaper. A step
resumed after a restart charges from submission. Each invocation settles the budget for the
time it watched.
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.domain.events import EventType
from app.domain.models import RunStatus
from app.execution.backend import CapacityError, JobHandle, JobSpec, JobState
from app.execution.naming import bucket_name
from app.orchestrator.budget import BudgetExceeded, KillSwitchActive, SpendKind
from app.orchestrator.context import RunContext
from app.storage.store import JOB_ACTIVE, JobRecord

STATUS_ATTEMPTS = 4  # transient control-plane errors while polling
STOP_WAIT_S = 300  # how long a cancelled job may take to stop
QUEUE_CAP_S = 1200  # PENDING (waiting for capacity) before the job is given up


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class JobOutcome:
    state: JobState
    handle: JobHandle
    elapsed_s: float
    timed_out: bool = False
    run_cancelled: bool = False
    queue_timed_out: bool = False  # still queued at the cap: no capacity
    stopped: bool = True  # False: cancelled but still not terminal (it may write meanwhile)


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


async def _await_stop(
    observe: Callable[[], Awaitable[tuple[JobState, datetime]]],
    sleep: Callable[[float], Awaitable[None]],
    poll_s: float,
    wait_s: float = STOP_WAIT_S,
) -> tuple[bool, JobState]:
    """Poll a cancelled job until it is terminal: (stopped, last state seen)."""
    waited = 0.0
    while True:
        state, _ = await observe()
        if state.is_terminal:
            return True, state
        if waited >= wait_s:
            return False, state
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
    queue_cap_s: float = QUEUE_CAP_S,
) -> JobOutcome:
    """Submit (get-by-name first, so a resumed step finds its job) and wait for the job.
    `started` anchors a new job's watchdog when the caller already put a deadline derived from
    it into the job (a recorded job keeps its first submission time, or the time it left the
    queue once recorded). `on_poll` runs while the job is still running, before each wait
    (display only: it must not raise)."""
    watchdog = timedelta(seconds=spec.watchdog_s)
    queue_cap = timedelta(seconds=queue_cap_s)
    fixed = started is not None
    existing = await ctx.store.get_job(spec.name)
    now = clock()
    submitted = started or now
    anchor: datetime | None = started  # the watchdog's start; None while it may still queue
    usd_since: datetime | None = submitted  # PROVISIONING may be billed: never checked (S4)
    gpu_since: datetime | None = None  # None: no GPU time yet (a fresh, queued job)
    mark = not fixed  # the row's deadline moves to anchor + watchdog once it leaves the queue
    if existing is not None:
        # Re-attaching after a restart: reserve what is left of its watchdog. A job that was
        # still running is charged from its submission (the gap is never free, at worst it
        # counts twice); one that was settled already costs nothing more.
        submitted = anchor = existing.created_at
        if not fixed and existing.status == JOB_ACTIVE and existing.deadline is not None:
            anchor = existing.deadline - watchdog
        usd_since = gpu_since = now if existing.status in _SETTLED else submitted
        mark = not fixed and existing.status == "SUBMITTED"
    gpu_reserve_s = (
        float(spec.watchdog_s)
        if anchor is None
        else max(0.0, (anchor + watchdog - now).total_seconds())
    )
    usd_reserve_s = gpu_reserve_s + (queue_cap_s if anchor is None else 0.0)
    try:
        cost = await ctx.budget.check_and_reserve(
            ctx.run_id, SpendKind.CLOUD_USD, usd_per_hour * usd_reserve_s / 3600
        )
    except (BudgetExceeded, KillSwitchActive):
        await _cancel_unwatched(ctx, existing)
        raise
    minutes = None
    if gpu:
        try:
            minutes = await ctx.budget.check_and_reserve(
                ctx.run_id, SpendKind.GPU_MINUTES, gpu_reserve_s / 60
            )
        except BaseException as err:
            await ctx.budget.commit(cost, 0)
            if isinstance(err, BudgetExceeded | KillSwitchActive):
                await _cancel_unwatched(ctx, existing)
            raise

    def since(start: datetime | None) -> float:
        return 0.0 if start is None else max(0.0, (clock() - start).total_seconds())

    async def settle(refused: bool = False) -> None:
        # A creation refused for lack of capacity left no job behind: nothing to pay.
        await ctx.budget.commit(cost, 0.0 if refused else usd_per_hour * since(usd_since) / 3600)
        if minutes is not None:
            await ctx.budget.commit(minutes, 0.0 if refused else since(gpu_since) / 60)

    async def job_status(state: JobState) -> None:
        await ctx.emit(
            EventType.JOB_STATUS,
            {"kind": spec.kind.value, "name": spec.name, "state": state.value},
        )

    handle: JobHandle | None = None
    state = JobState.UNKNOWN
    timed_out = cancelled = queue_timed_out = False
    stopped = True
    last_queued: datetime | None = None

    async def observe() -> tuple[JobState, datetime]:
        """One poll; GPU time starts at the last poll that still saw the job queued."""
        nonlocal last_queued, gpu_since, anchor, mark
        assert handle is not None  # noqa: S101 (polled only after submit)
        before = clock()
        current = await _status(ctx, handle, sleep)
        if current is JobState.PENDING:
            last_queued = before
            if anchor is None:  # seen queued again: an UNKNOWN poll before was not running
                gpu_since = None
            return current, clock()
        left = last_queued or submitted  # it left the queue after that poll
        if gpu_since is None:  # UNKNOWN too: it may be running (DELETING, an unmapped state)
            gpu_since = left
        if current is not JobState.UNKNOWN:
            if anchor is None:
                anchor = left
            if mark:
                mark = False
                await ctx.store.mark_job_active(spec.name, deadline=anchor + watchdog)
        return current, clock()

    try:
        handle = await ctx.jobs.submit(spec)
        await ctx.store.record_job(
            ctx.run_id,
            spec.kind.value,
            spec.name,
            handle.nebius_id,
            bucket_name(ctx.run_id),
            # the latest it may legitimately run: moved to activation + watchdog once it starts
            deadline=(anchor or submitted + queue_cap) + watchdog,
            at=submitted,
            platform=spec.platform,
            preset=spec.preset,
        )
        last: JobState | None = None
        while True:
            state, seen = await observe()
            if state is not last:
                await job_status(state)
                last = state
            if state.is_terminal:
                break
            run = await ctx.store.get_run(ctx.run_id)
            if run is None or run.status is not RunStatus.RUNNING:
                cancelled = True
            elif anchor is not None and seen - anchor >= watchdog:
                timed_out = True
            elif anchor is None and seen - submitted >= queue_cap:
                queue_timed_out = True
            if cancelled or timed_out or queue_timed_out:
                await ctx.jobs.cancel(handle)
                if queue_timed_out:
                    # "nothing ran" only once it has stopped: it may have started meanwhile
                    stopped, state = await _await_stop(observe, sleep, poll_s)
                else:
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
    elapsed_s = since(gpu_since)
    settled = existing is not None and existing.status in _SETTLED  # keep what it cost
    if stopped and not settled:  # unstopped: the row stays unsettled for the reaper
        await ctx.store.update_job(
            spec.name,
            state.value,
            gpu_seconds=elapsed_s if gpu else None,
            cost_usd=round(usd_per_hour * since(usd_since) / 3600, 6),
        )
    return JobOutcome(
        state,
        handle,
        elapsed_s,
        timed_out=timed_out,
        run_cancelled=cancelled,
        queue_timed_out=queue_timed_out,
        stopped=stopped,
    )

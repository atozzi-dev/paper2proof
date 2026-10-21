"""run_job re-attaching to a job recorded before a restart: budget and watching."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.domain.models import StepName
from app.execution.backend import JobHandle, JobKind, JobSpec, JobState
from app.execution.fake_backend import FakeJobBackend
from app.orchestrator.budget import BudgetExceeded, SpendKind
from app.orchestrator.context import RunContext
from app.orchestrator.steps.jobs import QUEUE_CAP_S, JobOutcome, run_job
from app.storage.store import Store
from tests.steps.conftest import RUN, ctx_at

NOW = datetime(2026, 10, 20, tzinfo=UTC)
DIGEST = "cr.eu-north1.nebius.cloud/reg/p2p-runner@sha256:" + "c" * 64
NAME = f"p2p-exec-{RUN[2:]}-1"


class Clock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return NOW + timedelta(seconds=self.t)

    async def sleep(self, seconds: float) -> None:
        self.t += seconds


def _spec(watchdog_s: int = 1500) -> JobSpec:
    return JobSpec(
        name=NAME, kind=JobKind.EXEC, image_digest_ref=DIGEST, platform="gpu-l40s-a",
        preset="1gpu-8vcpu-32gb", disk_gib=100, subnet_id="vpcsubnet-exec", volumes=[],
        inject_files={}, env={}, preemptible=False, watchdog_s=watchdog_s,
    )  # fmt: skip


async def _ctx(store: Store, backend: FakeJobBackend) -> RunContext:
    ctx = await ctx_at(store, StepName.EXECUTING, previous=[(StepName.SMOKE_TESTING, "s")])
    return RunContext(**{**ctx.__dict__, "jobs": backend})


async def _spend(ctx: RunContext, minutes: float) -> None:
    reservation = await ctx.budget.check_and_reserve(RUN, SpendKind.GPU_MINUTES, minutes)
    await ctx.budget.commit(reservation, minutes)


async def _recorded_running_job(store: Store, backend: FakeJobBackend) -> None:
    """A job submitted at NOW whose watcher died (control-plane restart)."""
    backend.final_state = JobState.RUNNING
    handle = await backend.submit(_spec())
    await store.record_job(RUN, "exec", NAME, handle.nebius_id, None, at=NOW)


async def test_reattaching_reserves_only_what_is_left_of_the_watchdog(store: Store) -> None:
    backend = FakeJobBackend()
    ctx = await _ctx(store, backend)
    await _recorded_running_job(store, backend)
    await _spend(ctx, 10)  # watched before the restart
    clock = Clock(t=10 * 60)  # 10 of its 25 minutes are gone: 15 left, 10 + 25 > 30 cap
    backend.jobs[NAME] = (*backend.jobs[NAME][:1], JobState.SUCCEEDED, *backend.jobs[NAME][2:])
    outcome = await run_job(
        ctx, _spec(), usd_per_hour=1.0, poll_s=30, clock=clock, sleep=clock.sleep, gpu=True
    )
    assert outcome.state is JobState.SUCCEEDED and backend.created == 1
    # charged from its submission: the restart gap is never free (at worst counted twice)
    assert await ctx.budget.run_spend(RUN, SpendKind.GPU_MINUTES) == pytest.approx(20)


async def test_a_job_that_no_longer_fits_the_budget_is_cancelled(store: Store) -> None:
    backend = FakeJobBackend()
    ctx = await _ctx(store, backend)
    await _recorded_running_job(store, backend)
    await _spend(ctx, 20)
    clock = Clock(t=60)  # 24 minutes left of its watchdog, only 10 in the budget
    with pytest.raises(BudgetExceeded):
        await run_job(
            ctx, _spec(), usd_per_hour=1.0, poll_s=30, clock=clock, sleep=clock.sleep, gpu=True
        )
    assert backend.cancelled == [NAME]  # never left running unwatched
    assert not ctx.budget._inflight


async def test_a_settled_job_is_not_charged_again(store: Store) -> None:
    backend = FakeJobBackend()
    ctx = await _ctx(store, backend)
    clock = Clock()
    await run_job(
        ctx, _spec(), usd_per_hour=1.0, poll_s=30, clock=clock, sleep=clock.sleep, gpu=True
    )
    first = await ctx.budget.run_spend(RUN, SpendKind.GPU_MINUTES)
    await store.update_job(NAME, "SUCCEEDED", gpu_seconds=600.0, cost_usd=1.5)  # what it ran
    before = await store.get_job(NAME)
    clock.t += 3600
    await run_job(
        ctx, _spec(), usd_per_hour=1.0, poll_s=30, clock=clock, sleep=clock.sleep, gpu=True
    )
    assert await ctx.budget.run_spend(RUN, SpendKind.GPU_MINUTES) == first
    assert backend.created == 1
    assert await store.get_job(NAME) == before  # re-attaching never rewrites what it cost


class ScriptedBackend(FakeJobBackend):
    """One state per status poll (the last one repeats); cancel() wins over the script."""

    def __init__(self, script: list[JobState]) -> None:
        super().__init__(final_state=script[0])
        self.script = list(script)

    async def status(self, handle: JobHandle) -> JobState:
        if handle.name in self.cancelled:
            return JobState.CANCELLED
        return self.script.pop(0) if len(self.script) > 1 else self.script[0]


def _states(*parts: tuple[JobState, int]) -> list[JobState]:
    return [state for state, n in parts for _ in range(n)]


P, S, R = JobState.PENDING, JobState.STARTING, JobState.RUNNING


async def _watch(
    store: Store,
    script: list[JobState],
    backend_cls: type["ScriptedBackend"] | None = None,
    **kw: Any,
) -> tuple[JobOutcome, RunContext, ScriptedBackend, Clock]:
    backend = (backend_cls or ScriptedBackend)(script)
    ctx = await _ctx(store, backend)
    clock = Clock()
    outcome = await run_job(
        ctx, _spec(), usd_per_hour=1.0, poll_s=30, clock=clock, sleep=clock.sleep, gpu=True, **kw
    )
    return outcome, ctx, backend, clock


async def test_queue_time_is_not_watched_nor_counted_as_gpu_time(store: Store) -> None:
    """S4: 16 minutes in PROVISIONING, then a 25-minute watchdog that still has room."""
    script = _states((P, 32), (S, 4), (R, 40), (JobState.SUCCEEDED, 1))
    outcome, ctx, _, clock = await _watch(store, script)
    assert outcome.state is JobState.SUCCEEDED and not outcome.timed_out
    assert clock.t == 76 * 30
    # charged from the last poll that still saw it queued (t=930 s): never less than it ran
    assert await ctx.budget.run_spend(RUN, SpendKind.GPU_MINUTES) == pytest.approx(22.5)
    record = await store.get_job(NAME)
    assert record is not None and record.gpu_seconds == pytest.approx(1350)
    # USD counts from submission: nobody has checked that PROVISIONING is not billed (S4)
    assert await ctx.budget.run_spend(RUN, SpendKind.CLOUD_USD) == pytest.approx(2280 / 3600)
    assert record.cost_usd == pytest.approx(2280 / 3600, abs=1e-6)


async def test_the_watchdog_counts_from_the_end_of_the_queue(store: Store) -> None:
    outcome, _, backend, clock = await _watch(store, _states((P, 10), (R, 1)))
    assert outcome.timed_out and not outcome.queue_timed_out
    assert clock.t == 270 + 1500 and backend.cancelled == [NAME]
    record = await store.get_job(NAME)
    assert record is not None and record.deadline == NOW + timedelta(seconds=270 + 1500)


async def test_a_job_stuck_in_the_queue_is_cancelled_at_the_queue_cap(store: Store) -> None:
    outcome, ctx, backend, clock = await _watch(store, [P])
    assert outcome.queue_timed_out and not outcome.timed_out
    assert outcome.state is JobState.CANCELLED and backend.cancelled == [NAME]
    assert clock.t == QUEUE_CAP_S and outcome.stopped
    assert await ctx.budget.run_spend(RUN, SpendKind.GPU_MINUTES) == 0
    assert await ctx.budget.run_spend(RUN, SpendKind.CLOUD_USD) == pytest.approx(1200 / 3600)


async def test_a_queued_job_is_recorded_with_its_latest_possible_deadline(store: Store) -> None:
    """The reaper must not cancel a job that is legitimately still queued."""
    backend = ScriptedBackend([P])
    ctx = await _ctx(store, backend)
    clock = Clock()
    seen: list[datetime | None] = []

    async def on_poll() -> None:
        record = await store.get_job(NAME)
        seen.append(None if record is None else record.deadline)

    await run_job(
        ctx, _spec(), usd_per_hour=1.0, poll_s=30, clock=clock, sleep=clock.sleep, gpu=True,
        on_poll=on_poll,
    )  # fmt: skip
    assert seen[0] == NOW + timedelta(seconds=QUEUE_CAP_S + 1500)


async def test_a_fixed_start_keeps_its_deadline_and_has_no_queue_cap(store: Store) -> None:
    """PROVISIONING bakes its deadline into the job: the watchdog counts from submission."""
    outcome, _, _, clock = await _watch(store, [P], started=NOW)
    assert outcome.timed_out and not outcome.queue_timed_out
    assert clock.t == 1500


async def test_a_job_that_ends_between_polls_is_charged_since_it_was_last_seen_queued(
    store: Store,
) -> None:
    outcome, ctx, _, _ = await _watch(store, _states((P, 3), (JobState.SUCCEEDED, 1)))
    assert outcome.state is JobState.SUCCEEDED
    assert await ctx.budget.run_spend(RUN, SpendKind.GPU_MINUTES) == pytest.approx(0.5)


async def test_a_resumed_active_job_keeps_the_deadline_set_when_it_left_the_queue(
    store: Store,
) -> None:
    backend = ScriptedBackend([R])
    ctx = await _ctx(store, backend)
    handle = await backend.submit(_spec())
    await store.record_job(RUN, "exec", NAME, handle.nebius_id, None, at=NOW)
    await store.mark_job_active(NAME, deadline=NOW + timedelta(seconds=600 + 1500))
    clock = Clock(t=1000)
    outcome = await run_job(
        ctx, _spec(), usd_per_hour=1.0, poll_s=30, clock=clock, sleep=clock.sleep, gpu=True
    )
    assert outcome.timed_out and clock.t >= 2100 and clock.t < 2100 + 30


async def test_a_resumed_job_seen_active_only_now_counts_from_its_submission(
    store: Store,
) -> None:
    """It may have left the queue at any time during the restart: assume the earliest."""
    backend = ScriptedBackend([R])
    ctx = await _ctx(store, backend)
    handle = await backend.submit(_spec())
    await store.record_job(RUN, "exec", NAME, handle.nebius_id, None, at=NOW)
    clock = Clock(t=600)
    outcome = await run_job(
        ctx, _spec(), usd_per_hour=1.0, poll_s=30, clock=clock, sleep=clock.sleep, gpu=True
    )
    assert outcome.timed_out and clock.t == 1500


def test_queue_and_watchdog_fit_in_the_platform_timeout() -> None:
    """The platform's own 1-hour timeout may count the queue too: it must never fire first."""
    from app.execution.nebius_backend import PLATFORM_TIMEOUT
    from app.orchestrator.steps.executing import EXEC_MARGIN_S, MAX_BUDGET_S

    assert PLATFORM_TIMEOUT.total_seconds() > QUEUE_CAP_S + MAX_BUDGET_S + EXEC_MARGIN_S


class SlowToCancel(ScriptedBackend):
    """cancel() is accepted, but the job keeps reporting RUNNING (Nebius CANCELLING)."""

    async def status(self, handle: JobHandle) -> JobState:
        if handle.name in self.cancelled:
            return R
        return await super().status(handle)


async def test_a_queue_timeout_whose_job_does_not_stop_is_reported_and_left_to_the_reaper(
    store: Store,
) -> None:
    """It may have started between the last poll and the cancel: never "nothing ran"."""
    outcome, ctx, _, _ = await _watch(store, [P], backend_cls=SlowToCancel, queue_cap_s=300)
    assert outcome.queue_timed_out and not outcome.stopped
    record = await store.get_job(NAME)
    assert record is not None and record.status not in ("SUCCEEDED", "FAILED", "CANCELLED")
    # billed from the last poll that saw it queued (t=270 s): it may have run since
    assert await ctx.budget.run_spend(RUN, SpendKind.GPU_MINUTES) > 0


async def test_a_job_unknown_after_the_queue_is_billed_and_still_capped(store: Store) -> None:
    outcome, ctx, _, clock = await _watch(store, _states((P, 3), (JobState.UNKNOWN, 1)))
    assert outcome.queue_timed_out and clock.t == QUEUE_CAP_S
    assert await ctx.budget.run_spend(RUN, SpendKind.GPU_MINUTES) == pytest.approx(
        (QUEUE_CAP_S - 60) / 60
    )


async def test_a_job_unknown_from_the_start_still_ends_at_the_queue_cap(store: Store) -> None:
    outcome, _, _, clock = await _watch(store, [JobState.UNKNOWN])
    assert outcome.queue_timed_out and clock.t == QUEUE_CAP_S


async def test_a_run_cancelled_while_its_job_is_queued_cancels_it_for_free(store: Store) -> None:
    backend = ScriptedBackend([P])
    ctx = await _ctx(store, backend)
    clock = Clock()

    async def cancel_the_run() -> None:
        if clock.t >= 300:
            await store.cancel_run(RUN)

    outcome = await run_job(
        ctx, _spec(), usd_per_hour=1.0, poll_s=30, clock=clock, sleep=clock.sleep, gpu=True,
        on_poll=cancel_the_run,
    )  # fmt: skip
    assert outcome.run_cancelled and backend.cancelled == [NAME]
    assert await ctx.budget.run_spend(RUN, SpendKind.GPU_MINUTES) == 0


async def test_a_resumed_job_seen_active_is_marked_active_from_its_submission(
    store: Store,
) -> None:
    """The reaper's deadline then matches the watchdog the step really applies."""
    backend = ScriptedBackend([R, JobState.SUCCEEDED])
    ctx = await _ctx(store, backend)
    handle = await backend.submit(_spec())
    await store.record_job(RUN, "exec", NAME, handle.nebius_id, None, at=NOW)
    seen: list[Any] = []

    async def on_poll() -> None:
        record = await store.get_job(NAME)
        seen.append(None if record is None else (record.status, record.deadline))

    clock = Clock(t=600)
    await run_job(
        ctx, _spec(), usd_per_hour=1.0, poll_s=30, clock=clock, sleep=clock.sleep, gpu=True,
        on_poll=on_poll,
    )  # fmt: skip
    assert seen[0] == ("ACTIVE", NOW + timedelta(seconds=1500))


async def test_an_unknown_poll_inside_the_queue_does_not_start_gpu_time(store: Store) -> None:
    """Seen queued again, it was not running: GPU time never exceeds the watchdog."""
    script = _states((P, 2), (JobState.UNKNOWN, 1), (P, 35), (R, 1))
    outcome, ctx, _, _ = await _watch(store, script)
    assert outcome.timed_out
    assert await ctx.budget.run_spend(RUN, SpendKind.GPU_MINUTES) <= 1500 / 60 + 0.5

"""run_job re-attaching to a job recorded before a restart: budget and watching."""

from datetime import UTC, datetime, timedelta

import pytest

from app.domain.models import StepName
from app.execution.backend import JobKind, JobSpec, JobState
from app.execution.fake_backend import FakeJobBackend
from app.orchestrator.budget import BudgetExceeded, SpendKind
from app.orchestrator.context import RunContext
from app.orchestrator.steps.jobs import run_job
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
    clock.t += 3600
    await run_job(
        ctx, _spec(), usd_per_hour=1.0, poll_s=30, clock=clock, sleep=clock.sleep, gpu=True
    )
    assert await ctx.budget.run_spend(RUN, SpendKind.GPU_MINUTES) == first
    assert backend.created == 1

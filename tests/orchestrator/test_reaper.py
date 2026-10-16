"""Reaper (execution-contracts §11) on a real Store, FakeJobBackend and a local bundle store."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.bundle.store import ArtifactBundleStore
from app.domain.models import RunStatus, StepName
from app.execution.backend import JobHandle, JobKind, JobSpec, JobState, VolumeMount
from app.execution.fake_backend import FakeJobBackend
from app.execution.naming import bucket_name, job_name
from app.models.pricing import load_cloud_prices
from app.orchestrator.budget import Budget, SpendKind, load_budget_limits
from app.orchestrator.reaper import Reaper, reaper_rates
from app.storage.artifacts import LocalArtifactStore
from app.storage.store import Store

ROOT = Path(__file__).resolve().parents[2]
RUN = "r_01jb7z00000000000000000000"
RUN2 = "r_01jb7z00000000000000000001"
RUN3 = "r_01jb7z00000000000000000002"
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
DIGEST = "cr.eu-north1.nebius.cloud/reg/p2p-runner@sha256:" + "a" * 64
RATES = {"prov": 0.132, "exec": 1.55, "smoke": 0.132}


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def spec(kind: JobKind, run_id: str, attempt: int = 1) -> JobSpec:
    return JobSpec(
        name=job_name(kind, run_id, attempt),
        kind=kind,
        image_digest_ref=DIGEST,
        platform="cpu-d3",
        preset="4vcpu-16gb",
        disk_gib=50,
        subnet_id="vpcsubnet-x",
        volumes=[VolumeMount(source=bucket_name(run_id), mount_path="/mnt/run", mode="rw")],
        inject_files={},
        env={},
        preemptible=False,
        watchdog_s=1200,
    )


def add_job(
    backend: FakeJobBackend, kind: JobKind, run_id: str, state: JobState, created: datetime
) -> JobHandle:
    s = spec(kind, run_id)
    handle = JobHandle(name=s.name, nebius_id=f"aijob-{s.name}")
    backend.jobs[s.name] = (handle, state, created, s)
    return handle


def add_bucket(backend: FakeJobBackend, run_id: str, created: datetime) -> None:
    backend.buckets.add(bucket_name(run_id))
    backend.bucket_created[bucket_name(run_id)] = created


class World:
    def __init__(self, store: Store, tmp_path: Path) -> None:
        self.store = store
        self.clock = Clock()
        self.backend = FakeJobBackend()
        self.bundles = ArtifactBundleStore(LocalArtifactStore(tmp_path / "artifacts"))
        limits = load_budget_limits(ROOT / "config" / "budgets.yaml")
        self.budget = Budget(store, limits, env="test", now=self.clock)
        self.reaper = Reaper(
            store, self.backend, self.bundles, self.budget, rates=RATES, clock=self.clock
        )

    async def run(self, run_id: str, status: RunStatus, step: StepName | None = None) -> None:
        """A run in `status`; with `step`, RUNNING with that step open."""
        await self.store.create_run(
            run_id, "2106.09685", created_by=None, claim_hint=None, budget={}
        )
        if step is not None:
            await self.store.begin_run(run_id, step, 1)
        elif status is not RunStatus.QUEUED:
            await self.store.set_run_status(run_id, status)

    async def record(self, handle: JobHandle, kind: str, run_id: str, started: datetime) -> None:
        await self.store.record_job(
            run_id,
            kind,
            handle.name,
            handle.nebius_id,
            bucket_name(run_id),
            deadline=started + timedelta(minutes=20),
            at=started,
        )


@pytest.fixture
def world(store: Store, tmp_path: Path) -> World:
    return World(store, tmp_path)


# --- jobs ---------------------------------------------------------------------------------------


async def test_cancels_a_job_past_its_recorded_deadline_plus_grace(world: World) -> None:
    await world.run(RUN, RunStatus.RUNNING, StepName.PROVISIONING)
    started = NOW - timedelta(minutes=40)
    handle = add_job(world.backend, JobKind.PROVISION, RUN, JobState.RUNNING, started)
    await world.record(handle, "prov", RUN, started)
    world.clock.now = started + timedelta(minutes=29)  # deadline + 9 min: still within grace
    assert (await world.reaper.tick())["job_cancelled_overdue"] == 0
    assert world.backend.cancelled == []
    world.clock.now = started + timedelta(minutes=31)
    actions = await world.reaper.tick()
    assert actions["job_cancelled_overdue"] == 1
    assert world.backend.cancelled == [handle.name]
    assert world.reaper.actions_total["job_cancelled_overdue"] == 1


async def test_unrecorded_job_gets_the_fallback_watchdog(world: World) -> None:
    await world.run(RUN, RunStatus.RUNNING, StepName.EXECUTING)
    handle = add_job(world.backend, JobKind.EXEC, RUN, JobState.RUNNING, NOW)
    world.clock.now = NOW + timedelta(minutes=54)  # 45 min fallback + 10 min grace
    await world.reaper.tick()
    assert world.backend.cancelled == []
    world.clock.now = NOW + timedelta(minutes=56)
    await world.reaper.tick()
    assert world.backend.cancelled == [handle.name]


async def test_a_job_of_a_terminal_run_is_cancelled_at_once(world: World) -> None:
    await world.run(RUN, RunStatus.CANCELLED)
    handle = add_job(world.backend, JobKind.EXEC, RUN, JobState.RUNNING, NOW)
    actions = await world.reaper.tick()
    assert actions["job_cancelled_orphan"] == 1
    assert world.backend.cancelled == [handle.name]


async def test_a_job_of_an_unknown_run_is_cancelled_at_once(world: World) -> None:
    handle = add_job(world.backend, JobKind.SMOKE, RUN2, JobState.STARTING, NOW)
    await world.reaper.tick()
    assert world.backend.cancelled == [handle.name]


async def test_terminal_jobs_and_live_jobs_within_deadline_are_left_alone(world: World) -> None:
    await world.run(RUN, RunStatus.RUNNING, StepName.EXECUTING)
    add_job(world.backend, JobKind.PROVISION, RUN, JobState.SUCCEEDED, NOW - timedelta(hours=5))
    add_job(world.backend, JobKind.EXEC, RUN, JobState.RUNNING, NOW - timedelta(minutes=10))
    await world.reaper.tick()
    assert world.backend.cancelled == []


async def test_other_p2p_jobs_get_the_fallback_and_foreign_jobs_are_never_touched(
    world: World,
) -> None:
    s = spec(JobKind.EXEC, RUN)
    spike = JobHandle(name="p2p-spike-s4", nebius_id="aijob-spike")
    world.backend.jobs[spike.name] = (spike, JobState.RUNNING, NOW - timedelta(hours=3), s)
    young = JobHandle(name="p2p-spike-s5", nebius_id="aijob-spike5")
    world.backend.jobs[young.name] = (young, JobState.RUNNING, NOW - timedelta(minutes=5), s)
    foreign = JobHandle(name="other-team-job", nebius_id="aijob-other")
    world.backend.jobs[foreign.name] = (foreign, JobState.RUNNING, NOW - timedelta(hours=3), s)
    await world.reaper.tick()
    assert world.backend.cancelled == [spike.name]


# --- ledger gaps --------------------------------------------------------------------------------


async def test_unsettled_job_of_a_terminal_run_is_charged_once(world: World) -> None:
    await world.run(RUN, RunStatus.FAILED)
    started = NOW - timedelta(minutes=30)
    handle = add_job(world.backend, JobKind.EXEC, RUN, JobState.SUCCEEDED, started)
    await world.record(handle, "exec", RUN, started)
    actions = await world.reaper.tick()
    assert actions["ledger_reconciled"] == 1
    cost = await world.budget.run_spend(RUN, SpendKind.CLOUD_USD)
    assert cost == pytest.approx(RATES["exec"] * 0.5)
    assert await world.budget.run_spend(RUN, SpendKind.GPU_MINUTES) == pytest.approx(30)
    record = await world.store.get_job(handle.name)
    assert record is not None
    assert record.status == "SUCCEEDED"
    assert record.cost_usd == pytest.approx(RATES["exec"] * 0.5)
    await world.reaper.tick()  # settled: never charged twice
    assert await world.budget.run_spend(RUN, SpendKind.CLOUD_USD) == pytest.approx(cost)


async def test_ledger_gap_is_capped_at_the_platform_timeout(world: World) -> None:
    await world.run(RUN, RunStatus.FAILED)
    started = NOW - timedelta(hours=5)
    handle = add_job(world.backend, JobKind.PROVISION, RUN, JobState.CANCELLED, started)
    await world.record(handle, "prov", RUN, started)
    await world.reaper.tick()
    assert await world.budget.run_spend(RUN, SpendKind.CLOUD_USD) == pytest.approx(RATES["prov"])
    assert await world.budget.run_spend(RUN, SpendKind.GPU_MINUTES) == 0


async def test_a_job_still_running_is_cancelled_then_charged(world: World) -> None:
    await world.run(RUN, RunStatus.CANCELLED)
    started = NOW - timedelta(minutes=6)
    handle = add_job(world.backend, JobKind.PROVISION, RUN, JobState.RUNNING, started)
    await world.record(handle, "prov", RUN, started)
    actions = await world.reaper.tick()  # cancelled first (FakeJobBackend: CANCELLED at once)
    assert actions["job_cancelled_orphan"] == 1
    assert actions["ledger_reconciled"] == 1
    assert await world.budget.run_spend(RUN, SpendKind.CLOUD_USD) == pytest.approx(
        RATES["prov"] * 0.1
    )


async def test_a_job_that_keeps_running_is_not_charged_yet(world: World) -> None:
    await world.run(RUN, RunStatus.CANCELLED)
    started = NOW - timedelta(minutes=6)
    handle = add_job(world.backend, JobKind.PROVISION, RUN, JobState.RUNNING, started)
    await world.record(handle, "prov", RUN, started)
    world.backend.cancel = never_stops(world.backend)  # type: ignore[method-assign]
    actions = await world.reaper.tick()
    assert actions["ledger_reconciled"] == 0
    record = await world.store.get_job(handle.name)
    assert record is not None
    assert record.status == "SUBMITTED"


async def test_a_vanished_job_is_settled_after_the_platform_timeout(world: World) -> None:
    await world.run(RUN, RunStatus.FAILED)
    started = NOW - timedelta(minutes=30)
    handle = JobHandle(name=job_name(JobKind.EXEC, RUN, 1), nebius_id="aijob-gone")
    await world.record(handle, "exec", RUN, started)  # not in the backend: status UNKNOWN
    assert (await world.reaper.tick())["ledger_reconciled"] == 0
    world.clock.now = started + timedelta(minutes=71)  # platform timeout + grace
    assert (await world.reaper.tick())["ledger_reconciled"] == 1
    assert await world.budget.run_spend(RUN, SpendKind.CLOUD_USD) == pytest.approx(RATES["exec"])
    record = await world.store.get_job(handle.name)
    assert record is not None
    assert record.status == "CANCELLED"
    assert (await world.reaper.tick())["ledger_reconciled"] == 0


async def test_settlement_is_atomic_and_charged_once(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await world.run(RUN, RunStatus.FAILED)
    started = NOW - timedelta(minutes=30)
    handle = add_job(world.backend, JobKind.PROVISION, RUN, JobState.SUCCEEDED, started)
    await world.record(handle, "prov", RUN, started)
    real_settle = world.store.settle_job
    calls = 0

    async def fails_once(*args: Any, **kwargs: Any) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("database is locked")
        return await real_settle(*args, **kwargs)

    monkeypatch.setattr(world.store, "settle_job", fails_once)
    assert (await world.reaper.tick())["error"] == 1
    assert await world.budget.run_spend(RUN, SpendKind.CLOUD_USD) == 0  # nothing half-written
    await world.reaper.tick()
    await world.reaper.tick()
    assert await world.budget.run_spend(RUN, SpendKind.CLOUD_USD) == pytest.approx(
        RATES["prov"] * 0.5
    )


async def test_settle_job_is_compare_and_set(world: World) -> None:
    await world.run(RUN, RunStatus.FAILED)
    handle = add_job(world.backend, JobKind.PROVISION, RUN, JobState.SUCCEEDED, NOW)
    await world.record(handle, "prov", RUN, NOW)
    entry = [(RUN, NOW.date(), SpendKind.CLOUD_USD.value, 1.0)]
    assert await world.store.settle_job(
        handle.name, "SUCCEEDED", cost_usd=1.0, gpu_seconds=None, ledger=entry
    )
    assert not await world.store.settle_job(
        handle.name, "SUCCEEDED", cost_usd=1.0, gpu_seconds=None, ledger=entry
    )
    assert await world.budget.run_spend(RUN, SpendKind.CLOUD_USD) == 1.0


async def test_an_unrecorded_job_of_a_terminal_run_is_recorded_and_charged(world: World) -> None:
    """Submitted, then the control plane died before record_job: never charged zero."""
    await world.run(RUN, RunStatus.CANCELLED)
    created = NOW - timedelta(minutes=12)
    handle = add_job(world.backend, JobKind.EXEC, RUN, JobState.RUNNING, created)
    actions = await world.reaper.tick()
    assert actions["job_cancelled_orphan"] == 1
    assert actions["ledger_reconciled"] == 1
    record = await world.store.get_job(handle.name)
    assert record is not None
    assert record.created_at == created
    assert await world.budget.run_spend(RUN, SpendKind.GPU_MINUTES) == pytest.approx(12)


async def test_jobs_of_active_runs_are_left_to_the_orchestrator(world: World) -> None:
    await world.run(RUN, RunStatus.RUNNING, StepName.PROVISIONING)
    started = NOW - timedelta(minutes=6)
    handle = add_job(world.backend, JobKind.PROVISION, RUN, JobState.SUCCEEDED, started)
    await world.record(handle, "prov", RUN, started)
    actions = await world.reaper.tick()
    assert actions["ledger_reconciled"] == 0
    assert await world.budget.run_spend(RUN, SpendKind.CLOUD_USD) == 0


async def test_one_failing_cancel_does_not_stop_the_others(world: World) -> None:
    await world.run(RUN, RunStatus.CANCELLED)
    await world.run(RUN2, RunStatus.CANCELLED)
    first = add_job(world.backend, JobKind.EXEC, RUN, JobState.RUNNING, NOW)
    second = add_job(world.backend, JobKind.EXEC, RUN2, JobState.RUNNING, NOW)
    real_cancel = world.backend.cancel

    async def refuses_first(handle: JobHandle) -> None:
        if handle == first:
            raise RuntimeError("PERMISSION_DENIED")
        await real_cancel(handle)

    world.backend.cancel = refuses_first  # type: ignore[method-assign]
    actions = await world.reaper.tick()
    assert actions["error"] >= 1
    assert world.backend.cancelled == [second.name]


# --- buckets ------------------------------------------------------------------------------------


async def test_deletes_an_old_bucket_of_a_terminal_run_with_a_bundle(world: World) -> None:
    await world.run(RUN, RunStatus.COMPLETED)
    add_bucket(world.backend, RUN, NOW - timedelta(hours=25))
    await world.bundles.put(RUN, b"bundle")
    actions = await world.reaper.tick()
    assert actions["bucket_deleted"] == 1
    assert world.backend.buckets == set()


async def test_never_deletes_a_bucket_without_a_bundle(world: World) -> None:
    await world.run(RUN, RunStatus.FAILED)
    add_bucket(world.backend, RUN, NOW - timedelta(days=3))
    actions = await world.reaper.tick()
    assert actions["bucket_kept_no_bundle"] == 1
    assert world.backend.buckets == {bucket_name(RUN)}


async def test_keeps_young_buckets_and_buckets_of_active_or_unknown_runs(world: World) -> None:
    await world.run(RUN, RunStatus.COMPLETED)
    await world.bundles.put(RUN, b"bundle")
    add_bucket(world.backend, RUN, NOW - timedelta(hours=23))
    await world.run(RUN2, RunStatus.RUNNING, StepName.ANALYZING)
    await world.bundles.put(RUN2, b"bundle")
    add_bucket(world.backend, RUN2, NOW - timedelta(days=2))
    await world.bundles.put(RUN3, b"bundle")  # no run row at all
    add_bucket(world.backend, RUN3, NOW - timedelta(days=2))
    actions = await world.reaper.tick()
    assert actions["bucket_deleted"] == 0
    assert len(world.backend.buckets) == 3


async def test_keeps_a_bucket_while_one_of_its_jobs_runs(world: World) -> None:
    await world.run(RUN, RunStatus.COMPLETED)
    await world.bundles.put(RUN, b"bundle")
    add_bucket(world.backend, RUN, NOW - timedelta(hours=25))
    world.backend.cancel = never_stops(world.backend)  # type: ignore[method-assign]
    add_job(world.backend, JobKind.EXEC, RUN, JobState.RUNNING, NOW - timedelta(minutes=1))
    actions = await world.reaper.tick()
    assert actions["job_cancelled_orphan"] == 1
    assert actions["bucket_deleted"] == 0
    assert world.backend.buckets == {bucket_name(RUN)}


def never_stops(backend: FakeJobBackend) -> Callable[[JobHandle], Awaitable[None]]:
    async def cancel(handle: JobHandle) -> None:
        backend.cancelled.append(handle.name)  # accepted, but the job keeps running

    return cancel


# --- gateway ------------------------------------------------------------------------------------


async def test_one_failing_bucket_does_not_stop_the_others(world: World) -> None:
    for run_id in (RUN, RUN2):
        await world.run(run_id, RunStatus.COMPLETED)
        await world.bundles.put(run_id, b"bundle")
        add_bucket(world.backend, run_id, NOW - timedelta(days=2))
    real_delete = world.backend.delete_run_bucket

    async def stuck_first(run_id: str) -> None:
        if run_id == RUN:
            raise RuntimeError("MalformedXML")
        await real_delete(run_id)

    world.backend.delete_run_bucket = stuck_first  # type: ignore[method-assign]
    actions = await world.reaper.tick()
    assert actions["bucket_delete_failed"] == 1
    assert actions["bucket_deleted"] == 1
    assert world.backend.buckets == {bucket_name(RUN)}


@pytest.mark.parametrize(
    "step", [StepName.PROVISIONING, StepName.SMOKE_TESTING, StepName.EXECUTING]
)
async def test_gateway_stays_up_while_a_run_needs_it(world: World, step: StepName) -> None:
    await world.run(RUN, RunStatus.RUNNING, step)
    world.backend.gateway_running = True
    actions = await world.reaper.tick()
    assert actions["gateway_stopped"] == 0
    assert world.backend.gateway_running


async def test_idle_gateway_is_stopped(world: World) -> None:
    await world.run(RUN, RunStatus.RUNNING, StepName.ANALYZING)
    world.backend.gateway_running = True
    actions = await world.reaper.tick()
    assert actions["gateway_stopped"] == 1
    assert not world.backend.gateway_running


# --- resilience ---------------------------------------------------------------------------------


async def test_one_failing_part_does_not_stop_the_others(world: World) -> None:
    await world.run(RUN, RunStatus.COMPLETED)
    await world.bundles.put(RUN, b"bundle")
    add_bucket(world.backend, RUN, NOW - timedelta(hours=25))

    async def broken(prefix: str) -> list[tuple[JobHandle, JobState, datetime]]:
        raise RuntimeError("list failed")

    world.backend.list_jobs = broken  # type: ignore[method-assign]
    world.backend.gateway_running = True
    actions = await world.reaper.tick()
    assert actions["error"] >= 1
    assert actions["bucket_deleted"] == 0  # can't prove that no job runs: keep the bucket
    assert actions["gateway_stopped"] == 1


async def test_run_forever_survives_a_failing_tick(world: World) -> None:
    calls = 0

    async def flaky_sleep(seconds: float) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise StopAsyncIteration  # ends the loop for the test

    async def boom() -> None:
        raise RuntimeError("tick failed")

    world.reaper.tick = boom  # type: ignore[assignment,method-assign]
    with pytest.raises(StopAsyncIteration):
        await world.reaper.run_forever(sleep=flaky_sleep)
    assert calls == 3


def test_rates_cover_every_job_kind() -> None:
    rates = reaper_rates(load_cloud_prices(ROOT / "config" / "pricing.yaml"))
    assert set(rates) == {kind.value for kind in JobKind}
    assert rates["exec"] > rates["prov"] > 0

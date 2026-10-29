"""JobSmokeExecutor: CPU smoke jobs in the exec subnet (the Sandboxes fallback), on fakes."""

import hashlib
import io
import json
import tarfile
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.domain.contracts import ArchiveDigests, DataArchive
from app.domain.models import StepName
from app.execution.backend import JobHandle, JobKind, JobSpec, JobState
from app.execution.config import ExecutionConfig
from app.execution.fake_backend import FakeJobBackend
from app.execution.job_smoke import SMOKE_SETUP_S, JobSmokeExecutor
from app.execution.naming import bucket_name
from app.execution.run_storage import FakeRunStorage
from app.execution.smoke import SmokeContractError, SmokeUnavailable
from app.models.pricing import load_cloud_prices
from app.orchestrator.budget import SpendKind
from app.orchestrator.context import RunContext
from app.storage.store import Store
from tests.steps.conftest import CPU_POOL, ROOT, RUN, ctx_at

DIGEST = "cr.eu-north1.nebius.cloud/reg/{}@sha256:" + "c" * 64
CFG = ExecutionConfig(
    provisioner_image=DIGEST.format("p2p-provisioner"),
    runner_image=DIGEST.format("p2p-runner"),
    runner_cpu_image=DIGEST.format("p2p-runner-cpu"),
    subnet_provision="vpcsubnet-provision",
    subnet_exec="vpcsubnet-exec",
    preemptible_gpu=False,
)
NOW = datetime(2026, 10, 20, tzinfo=UTC)
ARGV = ["python", "train.py", "--out", "out", "--smoke"]
ARCHIVES = ArchiveDigests(
    overlay="a" * 64, repo=None, data=[DataArchive(name="smoke", sha256="c" * 64)]
)


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> datetime:
        return NOW + timedelta(seconds=self.t)

    async def sleep(self, seconds: float) -> None:
        self.t += seconds


def _writes_result(storage: FakeRunStorage, echo: bool = True, **result: Any) -> Any:
    def on_submit(spec: JobSpec) -> None:
        manifest = json.loads(spec.inject_files["/etc/p2p/smoke.json"])
        iteration = manifest["iteration"]
        body = {"exit_code": 0, "stdout": "ok", "stderr": "", "duration_s": 2.5,
                "metrics": {"acc": 0.5}, **result}  # fmt: skip
        if echo:
            body["nonce"] = manifest["nonce"]
        storage.objects[(RUN, f"smoke/{iteration}/result.json")] = json.dumps(body).encode()

    return on_submit


async def _executor(
    store: Store, jobs: FakeJobBackend, storage: FakeRunStorage
) -> tuple[JobSmokeExecutor, RunContext]:
    ctx = await ctx_at(store, StepName.SMOKE_TESTING, previous=[(StepName.PROVISIONING, "p")])
    ctx = RunContext(**{**ctx.__dict__, "jobs": jobs})
    clock = Clock()
    executor = JobSmokeExecutor(
        ctx,
        archives=ARCHIVES,
        storage=storage,
        config=CFG,
        cloud=load_cloud_prices(ROOT / "config" / "pricing.yaml"),
        poll_s=5,
        clock=clock,
        sleep=clock.sleep,
    )
    return executor, ctx


async def test_a_smoke_run_is_an_offline_cpu_job(store: Store) -> None:
    storage = FakeRunStorage()
    jobs = FakeJobBackend(on_submit=_writes_result(storage))
    smoke, _ = await _executor(store, jobs, storage)
    env = await smoke.prepare(RUN, {"train.py": b"print(1)", "lib/u.py": b"x"})
    result = await smoke.run(env, ARGV, {"train.py": b"print(2)"}, 120)
    assert result.exit_code == 0
    assert result.metrics == {"acc": 0.5}
    ((_, _, _, spec),) = jobs.jobs.values()
    assert spec.kind is JobKind.SMOKE
    assert spec.name == f"p2p-smoke-{RUN[2:]}-1"
    assert spec.image_digest_ref == CFG.runner_cpu_image
    assert spec.subnet_id == "vpcsubnet-exec"
    assert {(v.source, v.mode) for v in spec.volumes} == {
        (bucket_name(RUN), "rw"),
        ("p2p-cache", "ro"),
    }
    manifest = json.loads(spec.inject_files["/etc/p2p/smoke.json"])
    nonce, digest = manifest.pop("nonce"), manifest.pop("workspace_sha256")
    assert manifest.pop("archives") == ARCHIVES.model_dump(mode="json")
    assert manifest == {
        "schema": "p2p.smoke/v1", "run_id": RUN, "iteration": 1, "argv": ARGV, "timeout_s": 120,
    }  # fmt: skip
    assert len(nonce) == 32
    archive = storage.objects[(RUN, "smoke/1/workspace.tar")]
    assert hashlib.sha256(archive).hexdigest() == digest
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        members = {m.name: tar.extractfile(m).read() for m in tar.getmembers()}  # type: ignore[union-attr]
    assert members == {"train.py": b"print(2)", "lib/u.py": b"x"}


async def test_iterations_continue_after_existing_jobs(store: Store) -> None:
    storage = FakeRunStorage()
    jobs = FakeJobBackend(on_submit=_writes_result(storage))
    smoke, _ = await _executor(store, jobs, storage)
    env = await smoke.prepare(RUN, {"train.py": b"x"})
    await smoke.run(env, ARGV, {}, 60)
    # A restarted control plane (new executor) must not reuse the finished job's name.
    again, _ = await _executor_reuse(store, jobs, storage)
    env = await again.prepare(RUN, {"train.py": b"x"})
    await again.run(env, ARGV, {}, 60)
    assert sorted(jobs.jobs) == [f"p2p-smoke-{RUN[2:]}-1", f"p2p-smoke-{RUN[2:]}-2"]


async def _executor_reuse(
    store: Store, jobs: FakeJobBackend, storage: FakeRunStorage
) -> tuple[JobSmokeExecutor, None]:
    ctx = RunContext(
        **{**(await _ctx_only(store)).__dict__, "jobs": jobs},
    )
    clock = Clock()
    executor = JobSmokeExecutor(
        ctx, archives=ARCHIVES, storage=storage, config=CFG,
        cloud=load_cloud_prices(ROOT / "config" / "pricing.yaml"),
        poll_s=5, clock=clock, sleep=clock.sleep,
    )  # fmt: skip
    return executor, None


async def _ctx_only(store: Store) -> RunContext:
    from app.orchestrator.budget import Budget, load_budget_limits

    limits = load_budget_limits(ROOT / "config" / "budgets.yaml")
    return RunContext(
        run_id=RUN, step=StepName.SMOKE_TESTING, attempt=1, store=store,
        budget=Budget(store, limits, env="test"), limits=limits, jobs=FakeJobBackend(),
        cpu_pool=CPU_POOL,
    )  # fmt: skip


async def test_branches_get_distinct_iterations(store: Store) -> None:
    storage = FakeRunStorage()
    jobs = FakeJobBackend(on_submit=_writes_result(storage))
    smoke, _ = await _executor(store, jobs, storage)
    env = await smoke.prepare(RUN, {"train.py": b"a"})
    results = await smoke.run_branches(env, [{"train.py": b"b"}, {"train.py": b"c"}], ARGV, 60)
    assert len(results) == 2
    assert len(jobs.jobs) == 2
    contents = set()
    for i in (1, 2):
        with tarfile.open(
            fileobj=io.BytesIO(storage.objects[(RUN, f"smoke/{i}/workspace.tar")])
        ) as tar:
            member = tar.extractfile("train.py")
            assert member is not None
            contents.add(member.read())
    assert contents == {b"b", b"c"}


async def test_untrusted_results_are_validated(store: Store) -> None:
    storage = FakeRunStorage()
    jobs = FakeJobBackend(on_submit=_writes_result(storage, metrics={"acc": "high", "ok": 1.0}))
    smoke, _ = await _executor(store, jobs, storage)
    env = await smoke.prepare(RUN, {"train.py": b"a"})
    result = await smoke.run(env, ARGV, {}, 60)
    assert result.metrics == {"ok": 1.0}


@pytest.mark.parametrize("state", [JobState.FAILED, JobState.RUNNING])
async def test_a_job_without_a_result_makes_the_executor_unavailable(
    store: Store, state: JobState
) -> None:
    storage = FakeRunStorage()
    smoke, ctx = await _executor(store, FakeJobBackend(state), storage)
    env = await smoke.prepare(RUN, {"train.py": b"a"})
    with pytest.raises(SmokeUnavailable):
        await smoke.run(env, ARGV, {}, 60)
    assert not ctx.budget._inflight


async def test_contract_checks_happen_before_any_job(store: Store) -> None:
    storage = FakeRunStorage()
    jobs = FakeJobBackend()
    smoke, _ = await _executor(store, jobs, storage)
    env = await smoke.prepare(RUN, {})
    with pytest.raises(SmokeContractError):
        await smoke.run(env, ["bash", "-c", "id"], {}, 60)
    with pytest.raises(SmokeContractError):
        await smoke.run(env, ARGV, {"../x.py": b""}, 60)
    with pytest.raises(SmokeContractError):
        await smoke.run(env, ARGV, {}, 10_000)
    assert not jobs.jobs


@pytest.mark.parametrize("case", ["no-nonce", "failed-job"])
async def test_results_that_are_not_this_jobs_are_rejected(store: Store, case: str) -> None:
    storage = FakeRunStorage()
    if case == "no-nonce":
        jobs = FakeJobBackend(on_submit=_writes_result(storage, echo=False))
    else:
        jobs = FakeJobBackend(JobState.FAILED, on_submit=_writes_result(storage))
    smoke, _ = await _executor(store, jobs, storage)
    env = await smoke.prepare(RUN, {"train.py": b"a"})
    with pytest.raises(SmokeUnavailable):
        await smoke.run(env, ARGV, {}, 60)


async def test_a_smoke_job_still_running_after_a_restart_is_cancelled(store: Store) -> None:
    storage = FakeRunStorage()
    jobs = FakeJobBackend(JobState.RUNNING)
    smoke, _ = await _executor(store, jobs, storage)
    env = await smoke.prepare(RUN, {"train.py": b"a"})
    orphan = JobSpec(
        name=f"p2p-smoke-{RUN[2:]}-1", kind=JobKind.SMOKE, image_digest_ref=CFG.runner_cpu_image,
        platform="cpu-d3", preset="4vcpu-16gb", disk_gib=30, subnet_id="vpcsubnet-exec",
        volumes=[], inject_files={}, env={}, preemptible=False, watchdog_s=300,
    )  # fmt: skip
    await jobs.submit(orphan)
    jobs.final_state = JobState.SUCCEEDED
    jobs.on_submit = _writes_result(storage)
    await smoke.run(env, ARGV, {}, 60)
    assert jobs.cancelled == [f"p2p-smoke-{RUN[2:]}-1"]
    assert f"p2p-smoke-{RUN[2:]}-2" in jobs.jobs


class SlowToStop(FakeJobBackend):
    """A cancelled job keeps running for `polls` status calls (the platform stops it later)."""

    def __init__(self, polls: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.polls = polls
        self.log: list[str] = []

    async def submit(self, spec: JobSpec) -> Any:
        self.log.append(f"submit {spec.name}")
        return await super().submit(spec)

    async def status(self, handle: Any) -> JobState:
        if handle.name in self.cancelled:
            if self.polls > 0:
                self.polls -= 1
                return JobState.RUNNING
            return JobState.CANCELLED
        return await super().status(handle)


def _orphan() -> JobSpec:
    return JobSpec(
        name=f"p2p-smoke-{RUN[2:]}-1", kind=JobKind.SMOKE, image_digest_ref=CFG.runner_cpu_image,
        platform="cpu-d3", preset="4vcpu-16gb", disk_gib=30, subnet_id="vpcsubnet-exec",
        volumes=[], inject_files={}, env={}, preemptible=False, watchdog_s=300,
    )  # fmt: skip


async def test_the_next_smoke_job_waits_until_the_orphan_has_stopped(store: Store) -> None:
    storage = FakeRunStorage()
    jobs = SlowToStop(2, final_state=JobState.RUNNING)
    smoke, _ = await _executor(store, jobs, storage)
    env = await smoke.prepare(RUN, {"train.py": b"a"})
    await jobs.submit(_orphan())
    jobs.final_state = JobState.SUCCEEDED
    jobs.on_submit = _writes_result(storage)
    await smoke.run(env, ARGV, {}, 60)
    assert jobs.polls == 0  # polled until it stopped, then the new job started
    assert jobs.log[-1] == f"submit p2p-smoke-{RUN[2:]}-2"


async def test_an_orphan_that_never_stops_blocks_the_smoke_run(store: Store) -> None:
    storage = FakeRunStorage()
    jobs = SlowToStop(10_000, final_state=JobState.RUNNING)
    smoke, _ = await _executor(store, jobs, storage)
    env = await smoke.prepare(RUN, {"train.py": b"a"})
    await jobs.submit(_orphan())
    with pytest.raises(SmokeUnavailable, match="still stopping"):
        await smoke.run(env, ARGV, {}, 60)
    assert f"p2p-smoke-{RUN[2:]}-2" not in jobs.jobs


async def test_the_watchdog_leaves_room_for_setup(store: Store) -> None:
    storage = FakeRunStorage()
    jobs = FakeJobBackend(on_submit=_writes_result(storage))
    smoke, _ = await _executor(store, jobs, storage)
    env = await smoke.prepare(RUN, {"train.py": b"a"})
    await smoke.run(env, ARGV, {}, 60)
    ((_, _, _, spec),) = jobs.jobs.values()
    assert spec.watchdog_s == 60 + SMOKE_SETUP_S
    assert SMOKE_SETUP_S >= 218 + 60  # S12: 218 s from create to start, plus extraction


class AlwaysQueued(FakeJobBackend):
    async def status(self, handle: JobHandle) -> JobState:
        return JobState.CANCELLED if handle.name in self.cancelled else JobState.PENDING


async def test_a_smoke_job_that_never_leaves_the_queue_makes_the_executor_unavailable(
    store: Store,
) -> None:
    storage = FakeRunStorage()
    jobs = AlwaysQueued(on_submit=_writes_result(storage))
    smoke, _ = await _executor(store, jobs, storage)
    env = await smoke.prepare(RUN, {"train.py": b"a"})
    with pytest.raises(SmokeUnavailable, match="queue"):
        await smoke.run(env, ARGV, {}, 60)
    assert jobs.cancelled == [f"p2p-smoke-{RUN[2:]}-1"]


async def test_the_smoke_platform_comes_from_the_config_and_is_priced_as_such(
    store: Store,
) -> None:
    """Hybrid deploy (docs/specs/hybrid-deploy.md): no CPU jobs on Nebius, so smoke jobs run on
    the GPU platform, and the budget reserves the GPU price (invariant 7)."""
    gpu = CFG.model_copy(update={"smoke_platform": "gpu-l40s-a", "smoke_preset": "1gpu-8vcpu-32gb"})
    storage = FakeRunStorage()
    jobs = FakeJobBackend(on_submit=_writes_result(storage))
    ctx = await ctx_at(store, StepName.SMOKE_TESTING, previous=[(StepName.PROVISIONING, "p")])
    ctx = RunContext(**{**ctx.__dict__, "jobs": jobs})
    clock = Clock()
    cloud = load_cloud_prices(ROOT / "config" / "pricing.yaml")
    smoke = JobSmokeExecutor(
        ctx, archives=ARCHIVES, storage=storage, config=gpu, cloud=cloud,
        poll_s=5, clock=clock, sleep=clock.sleep,
    )  # fmt: skip
    reserved: list[SpendKind] = []
    check = ctx.budget.check_and_reserve

    async def spy(run_id: str, kind: SpendKind, amount: float) -> Any:
        reserved.append(kind)
        return await check(run_id, kind, amount)

    ctx.budget.check_and_reserve = spy  # type: ignore[method-assign]
    env = await smoke.prepare(RUN, {"train.py": b"print(1)"})
    await smoke.run(env, ARGV, {"train.py": b"print(2)"}, 120)
    ((_, _, _, spec),) = jobs.jobs.values()
    assert (spec.platform, spec.preset) == ("gpu-l40s-a", "1gpu-8vcpu-32gb")
    assert smoke.usd_per_hour == cloud.hourly("gpu-l40s-a", "1gpu-8vcpu-32gb")
    assert SpendKind.GPU_MINUTES in reserved  # checked against the GPU caps before the job
    assert CFG.smoke_platform == "cpu-d3" and CFG.smoke_preset == "4vcpu-16gb"  # the default


class BrokenBackend(FakeJobBackend):
    async def submit(self, spec: JobSpec) -> JobHandle:
        from app.execution.nebius_backend import JobBackendError

        raise JobBackendError("Sandboxes upload: HTTP 502")


async def test_a_backend_that_cannot_start_the_job_makes_the_executor_unavailable(
    store: Store,
) -> None:
    """The Coder hears the executor is unavailable and may try again; the run never ends with
    an internal error for an outage of the job backend."""
    storage = FakeRunStorage()
    smoke, ctx = await _executor(store, BrokenBackend(), storage)
    env = await smoke.prepare(RUN, {"train.py": b"a"})
    with pytest.raises(SmokeUnavailable, match="HTTP 502"):
        await smoke.run(env, ARGV, {}, 60)
    assert not ctx.budget._inflight

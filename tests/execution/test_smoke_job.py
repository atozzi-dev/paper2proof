"""JobSmokeExecutor: CPU smoke jobs in the exec subnet (the Sandboxes fallback), on fakes."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.domain.models import StepName
from app.execution.backend import JobKind, JobSpec, JobState
from app.execution.config import ExecutionConfig
from app.execution.fake_backend import FakeJobBackend
from app.execution.job_smoke import JobSmokeExecutor
from app.execution.naming import bucket_name
from app.execution.run_storage import FakeRunStorage
from app.execution.smoke import SmokeContractError, SmokeUnavailable
from app.models.pricing import load_cloud_prices
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
)
NOW = datetime(2026, 10, 20, tzinfo=UTC)
ARGV = ["python", "train.py", "--out", "out", "--smoke"]


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> datetime:
        return NOW + timedelta(seconds=self.t)

    async def sleep(self, seconds: float) -> None:
        self.t += seconds


def _writes_result(storage: FakeRunStorage, **result: Any) -> Any:
    def on_submit(spec: JobSpec) -> None:
        iteration = json.loads(spec.inject_files["/etc/p2p/smoke.json"])["iteration"]
        body = {"exit_code": 0, "stdout": "ok", "stderr": "", "duration_s": 2.5,
                "metrics": {"acc": 0.5}, **result}  # fmt: skip
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
    assert manifest == {
        "schema": "p2p.smoke/v1", "run_id": RUN, "iteration": 1, "argv": ARGV, "timeout_s": 120,
    }  # fmt: skip
    assert storage.objects[(RUN, "smoke/1/workspace/train.py")] == b"print(2)"
    assert storage.objects[(RUN, "smoke/1/workspace/lib/u.py")] == b"x"


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
        ctx, storage=storage, config=CFG, cloud=load_cloud_prices(ROOT / "config" / "pricing.yaml"),
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
    assert {storage.objects[(RUN, f"smoke/{i}/workspace/train.py")] for i in (1, 2)} == {
        b"b",
        b"c",
    }


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

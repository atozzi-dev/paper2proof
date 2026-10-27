"""EXECUTING on fakes: the exec manifest, the GPU job, live.log as job.log events, the
watchdog and one on-demand retry after a preemption."""

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from app.domain.contracts import ExecManifest
from app.domain.errors import P2PError
from app.domain.events import JobLogPayload, WarningPayload
from app.domain.models import (
    Claim,
    ClaimType,
    DatasetReq,
    ErrorCode,
    Plan,
    PlanMode,
    RunStatus,
    Scale,
    StepName,
)
from app.execution.backend import JobHandle, JobKind, JobSpec, JobState
from app.execution.config import ExecutionConfig
from app.execution.fake_backend import FakeJobBackend
from app.execution.run_storage import FakeRunStorage
from app.models.pricing import load_cloud_prices
from app.orchestrator.budget import Budget, BudgetExceeded, SpendKind
from app.orchestrator.context import RunContext
from app.orchestrator.steps.executing import EXEC_MARGIN_S, ExecutingStep, exit_info, new_lines
from app.orchestrator.steps.jobs import QUEUE_CAP_S
from app.storage.artifacts import LocalArtifactStore
from app.storage.store import Store
from tests.steps.conftest import ROOT, RUN, ctx_at

DIGEST = "cr.eu-north1.nebius.cloud/reg/{}@sha256:" + "c" * 64
SLUG = RUN.removeprefix("r_")
PLAN_KEY = f"runs/{RUN}/plan.json"
REPORT_KEY = f"runs/{RUN}/provision_report.json"
SMOKE_KEY = f"runs/{RUN}/smoke_result.json"
NOW = datetime(2026, 10, 20, tzinfo=UTC)
CLAIM = Claim(
    id="c1",
    text="Ours beats ResNet-18 by 1.7 points on CIFAR-10",
    claim_type=ClaimType.RELATIVE,
    metric="top1_accuracy",
    higher_is_better=True,
    dataset="CIFAR-10",
    setting="200 epochs",
    reported_value=None,
    baseline_name="ResNet-18",
    claimed_delta=1.7,
    source_ref="Table 2",
)


def _config(preemptible: bool = True) -> ExecutionConfig:
    return ExecutionConfig(
        provisioner_image=DIGEST.format("p2p-provisioner"),
        runner_image=DIGEST.format("p2p-runner"),
        runner_cpu_image=DIGEST.format("p2p-runner-cpu"),
        subnet_provision="vpcsubnet-provision",
        subnet_exec="vpcsubnet-exec",
        preemptible_gpu=preemptible,
    )


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> datetime:
        return NOW + timedelta(seconds=self.t)

    async def sleep(self, seconds: float) -> None:
        self.t += seconds


class ScriptedBackend(FakeJobBackend):
    """Each job walks through its scripted states, one per status call (the last one stays).
    `on_status(name, n)` runs before the n-th status answer (writes live.log, results...)."""

    def __init__(self, scripts: dict[int, list[JobState]], on_status: Any = None) -> None:
        super().__init__()
        self.scripts = scripts
        self.on_status = on_status
        self.calls: dict[str, int] = {}
        self.specs: list[JobSpec] = []

    async def submit(self, spec: JobSpec) -> JobHandle:
        if spec.name not in self.jobs:
            self.specs.append(spec)
        return await super().submit(spec)

    async def status(self, handle: JobHandle) -> JobState:
        if handle.name in self.cancelled:
            return JobState.CANCELLED
        n = self.calls.get(handle.name, 0)
        self.calls[handle.name] = n + 1
        if self.on_status is not None:
            self.on_status(handle.name, n)
        script = self.scripts[int(handle.name.rsplit("-", 1)[1])]
        return script[min(n, len(script) - 1)]


def _manifest_of(spec: JobSpec) -> ExecManifest:
    return ExecManifest.model_validate(json.loads(spec.inject_files["/etc/p2p/manifest.json"]))


def _results(attempt: int, exit_reason: str = "completed", wall: float = 812.4) -> bytes:
    data: dict[str, Any] = json.loads(
        (ROOT / "tests" / "fixtures" / "contracts" / "results_valid.json").read_text()
    )
    data["attempt"] = attempt
    data["exit_reason"] = exit_reason
    data["wall_clock_s"] = wall
    return json.dumps(data).encode()


async def _setup(
    store: Store,
    tmp_path: Path,
    *,
    gpu_minutes: float = 10,
    repo: bool = True,
    datasets: tuple[str, ...] = ("uoft-cs/cifar10",),
) -> tuple[RunContext, LocalArtifactStore, FakeRunStorage]:
    artifacts = LocalArtifactStore(tmp_path)
    plan = Plan(
        mode=PlanMode.REPO_FIRST, claim_ids=["c1"], claim_types={"c1": ClaimType.RELATIVE},
        scale=Scale.REDUCED, seeds=[0, 1, 2], gpu_minutes_est=gpu_minutes,
        datasets=[DatasetReq(kind="hf", id=d) for d in datasets],
    )  # fmt: skip
    artifacts.put_json(PLAN_KEY, {"plan": plan.model_dump(mode="json"), "est_cost_usd": 0.3})
    artifacts.put_json(
        REPORT_KEY,
        {
            "overlay_sha256": "a" * 64,
            "repo": repo,
            "repo_sha256": "d" * 64 if repo else None,
            "data_archives": [
                {"name": "uoft-cs__cifar10", "sha256": "b" * 64},
                {"name": "smoke", "sha256": "e" * 64},
            ],
        },
    )
    artifacts.put_json(
        SMOKE_KEY,
        {
            "entrypoint": "workspace/train.py",
            "args": ["--epochs", "5"],
            "measures": [
                {
                    "claim_id": "c1",
                    "key": "method/top1_accuracy",
                    "baseline_key": "resnet18/top1_accuracy",
                    "unit": "fraction",
                }
            ],
            "unmeasured": [],
            "smoke_metrics": {},
            "workspace_sha256": "f" * 64,
        },
    )
    previous: list[tuple[StepName, str | None]] = [
        (StepName.INGESTING, "i"),
        (StepName.EXTRACTING_CLAIMS, "c"),
        (StepName.DISCOVERING, "d"),
        (StepName.PLANNING, PLAN_KEY),
        (StepName.PROVISIONING, REPORT_KEY),
        (StepName.SMOKE_TESTING, SMOKE_KEY),
    ]
    ctx = await ctx_at(store, StepName.EXECUTING, previous=previous)
    await store.save_claims(RUN, [CLAIM], selected={"c1"})
    return ctx, artifacts, FakeRunStorage()


def _step(
    artifacts: LocalArtifactStore,
    storage: FakeRunStorage,
    clock: FakeClock,
    *,
    preemptible: bool = True,
) -> ExecutingStep:
    return ExecutingStep(
        artifacts=artifacts,
        storage=storage,
        config=_config(preemptible),
        cloud=load_cloud_prices(ROOT / "config" / "pricing.yaml"),
        poll_s=30,
        clock=clock,
        sleep=clock.sleep,
    )


def _with(ctx: RunContext, backend: FakeJobBackend) -> RunContext:
    return RunContext(**{**ctx.__dict__, "jobs": backend})


async def _events(store: Store) -> list[Any]:
    return [e.payload for e in await store.events_after(RUN, 0)]


async def test_the_exec_job_runs_the_validated_manifest(store: Store, tmp_path: Path) -> None:
    ctx, artifacts, storage = await _setup(store, tmp_path)
    clock = FakeClock()

    def finish(name: str, n: int) -> None:
        storage.objects[(RUN, "results/1/results.json")] = _results(1)

    backend = ScriptedBackend({1: [JobState.RUNNING, JobState.SUCCEEDED]}, on_status=finish)
    result = await _step(artifacts, storage, clock).run(_with(ctx, backend))

    assert result.next is StepName.ANALYZING and result.output_ref is not None
    assert backend.gateway_running
    [spec] = backend.specs
    assert spec.name == f"p2p-exec-{SLUG}-1" and spec.kind is JobKind.EXEC
    assert spec.image_digest_ref == DIGEST.format("p2p-runner")
    assert spec.platform == "gpu-l40s-a" and spec.preset == "1gpu-8vcpu-32gb"
    assert spec.preemptible and spec.subnet_id == "vpcsubnet-exec" and spec.disk_gib == 100
    assert {(v.source, v.mount_path, v.mode) for v in spec.volumes} == {
        (f"p2p-run-{SLUG}", "/mnt/run", "rw"),
        ("p2p-cache", "/mnt/cache", "ro"),
    }
    manifest = _manifest_of(spec)
    assert manifest.attempt == 1 and manifest.image_digest == "sha256:" + "c" * 64
    assert manifest.entrypoint == "workspace/train.py" and manifest.args == ["--epochs", "5"]
    assert manifest.seeds == [0, 1, 2] and manifest.budget_seconds == 600
    assert spec.watchdog_s == 600 + EXEC_MARGIN_S
    [spec_c1] = manifest.claims
    assert spec_c1.claim_ref == "c1" and spec_c1.metric == "top1_accuracy"
    assert spec_c1.claim_type is ClaimType.RELATIVE and spec_c1.baseline_name == "ResNet-18"
    assert spec_c1.metric_key == "method/top1_accuracy"
    assert spec_c1.baseline_key == "resnet18/top1_accuracy"
    assert manifest.scale.full is False
    assert manifest.paths.repo == "/mnt/run/staging/repo.tar.zst"
    assert manifest.paths.results == "/mnt/run/results/1"
    assert manifest.paths.workspace == "/mnt/run/staging/workspace.tar"
    assert manifest.workspace_sha256 == "f" * 64
    assert manifest.archives.overlay == "a" * 64 and manifest.archives.repo == "d" * 64
    assert [a.name for a in manifest.archives.data] == ["uoft-cs__cifar10"]  # never smoke
    stored = artifacts.get_json(result.output_ref)
    assert stored["results_key"] == "results/1/results.json"
    assert stored["job_state"] == "SUCCEEDED"
    assert ExecManifest.model_validate(stored["manifest"]) == manifest
    # how the judged job ran, for the verdict's conditions (the spec it was submitted with)
    assert stored["hardware"] == {
        "platform": spec.platform,
        "preset": spec.preset,
        "preemptible": True,
    }
    assert await ctx.budget.run_spend(RUN, SpendKind.GPU_MINUTES) == pytest.approx(0.5)


async def test_without_a_repository_the_manifest_has_no_repo(store: Store, tmp_path: Path) -> None:
    ctx, artifacts, storage = await _setup(store, tmp_path, repo=False)
    backend = ScriptedBackend({1: [JobState.SUCCEEDED]})
    storage.objects[(RUN, "results/1/results.json")] = b"{}"
    await _step(artifacts, storage, FakeClock()).run(_with(ctx, backend))
    manifest = _manifest_of(backend.specs[0])
    assert manifest.paths.repo is None and manifest.archives.repo is None


async def test_the_injected_and_kept_manifests_are_identical_and_match_the_schema(
    store: Store, tmp_path: Path
) -> None:
    """The file injected into the job, the copy kept for retries and the one in the step
    output are the same document, and the runner's schema accepts it (no null scale fields)."""
    ctx, artifacts, storage = await _setup(store, tmp_path, repo=False)
    backend = ScriptedBackend({1: [JobState.SUCCEEDED]})
    storage.objects[(RUN, "results/1/results.json")] = _results(1)
    result = await _step(artifacts, storage, FakeClock()).run(_with(ctx, backend))
    assert result.output_ref is not None
    injected = json.loads(backend.specs[0].inject_files["/etc/p2p/manifest.json"])
    kept = artifacts.get_json(f"runs/{RUN}/exec_manifest_1.json")
    assert injected == kept == artifacts.get_json(result.output_ref)["manifest"]
    schema = json.loads((ROOT / "schemas" / "manifest.schema.json").read_text(encoding="utf-8"))
    assert list(Draft202012Validator(schema).iter_errors(injected)) == []
    assert injected["scale"] == {"full": False} and injected["paths"]["repo"] is None


async def test_the_budget_seconds_fit_the_per_run_gpu_minutes(store: Store, tmp_path: Path) -> None:
    ctx, artifacts, storage = await _setup(store, tmp_path, gpu_minutes=500)
    backend = ScriptedBackend({1: [JobState.SUCCEEDED]})
    await _step(artifacts, storage, FakeClock()).run(_with(ctx, backend))
    budget = _manifest_of(backend.specs[0]).budget_seconds
    assert budget == ctx.limits.per_run.gpu_minutes * 60 - EXEC_MARGIN_S
    assert backend.specs[0].watchdog_s == ctx.limits.per_run.gpu_minutes * 60


async def test_a_demo_configuration_never_uses_preemptible_gpus(
    store: Store, tmp_path: Path
) -> None:
    ctx, artifacts, storage = await _setup(store, tmp_path)
    backend = ScriptedBackend({1: [JobState.SUCCEEDED]})
    await _step(artifacts, storage, FakeClock(), preemptible=False).run(_with(ctx, backend))
    assert backend.specs[0].preemptible is False


async def test_a_preempted_job_is_retried_once_on_demand(store: Store, tmp_path: Path) -> None:
    ctx, artifacts, storage = await _setup(store, tmp_path)

    def second_succeeds(name: str, n: int) -> None:
        if name.endswith("-2"):
            storage.objects[(RUN, "results/2/results.json")] = _results(2)

    backend = ScriptedBackend(
        {1: [JobState.RUNNING, JobState.FAILED], 2: [JobState.RUNNING, JobState.SUCCEEDED]},
        on_status=second_succeeds,
    )
    result = await _step(artifacts, storage, FakeClock()).run(_with(ctx, backend))

    assert [s.name for s in backend.specs] == [f"p2p-exec-{SLUG}-1", f"p2p-exec-{SLUG}-2"]
    first, retry = backend.specs
    assert first.preemptible and not retry.preemptible
    assert _manifest_of(retry).attempt == 2
    assert _manifest_of(retry).paths.results == "/mnt/run/results/2"
    assert result.output_ref is not None
    stored = artifacts.get_json(result.output_ref)
    assert stored["results_key"] == "results/2/results.json"
    assert stored["manifest"]["attempt"] == 2
    assert stored["hardware"]["preemptible"] is False  # the on-demand retry
    warnings = [p for p in await _events(store) if isinstance(p, WarningPayload)]
    assert [w.code for w in warnings] == ["JOB_PREEMPTED"]


async def test_a_job_preempted_after_partial_seeds_is_retried(store: Store, tmp_path: Path) -> None:
    # SIGTERM from the platform makes the runner write partial results (budget_timeout)
    ctx, artifacts, storage = await _setup(store, tmp_path)
    storage.objects[(RUN, "results/1/results.json")] = b"stale"

    def partial(name: str, n: int) -> None:
        if name.endswith("-1"):
            storage.objects[(RUN, "results/1/results.json")] = _results(1, "budget_timeout", 120)
        else:
            storage.objects[(RUN, "results/2/results.json")] = _results(2)

    backend = ScriptedBackend({1: [JobState.FAILED], 2: [JobState.SUCCEEDED]}, on_status=partial)
    result = await _step(artifacts, storage, FakeClock()).run(_with(ctx, backend))
    assert len(backend.specs) == 2
    assert result.output_ref is not None
    assert artifacts.get_json(result.output_ref)["results_key"] == "results/2/results.json"


async def test_no_retry_when_the_gpu_budget_does_not_allow_it(store: Store, tmp_path: Path) -> None:
    # 20 planned minutes: the first job runs ~24.5 of its 25-minute watchdog, then is
    # preempted; the 5.5 minutes left can't hold even a 60 s budget plus the margin.
    ctx, artifacts, storage = await _setup(store, tmp_path, gpu_minutes=20)
    polls = 49
    backend = ScriptedBackend({1: [JobState.RUNNING] * polls + [JobState.FAILED]})
    result = await _step(artifacts, storage, FakeClock()).run(_with(ctx, backend))

    assert len(backend.specs) == 1
    assert result.next is StepName.ANALYZING and result.output_ref is not None
    stored = artifacts.get_json(result.output_ref)
    assert stored["results_key"] is None
    assert stored["exit_reason"] == "budget_timeout"
    assert stored["manifest"]["attempt"] == 1
    warnings = [p for p in await _events(store) if isinstance(p, WarningPayload)]
    assert [w.code for w in warnings] == ["JOB_PREEMPTED"]
    assert "budget" in warnings[0].message


async def test_partial_results_are_kept_when_there_is_no_retry(
    store: Store, tmp_path: Path
) -> None:
    ctx, artifacts, storage = await _setup(store, tmp_path, gpu_minutes=20)

    def partial(name: str, n: int) -> None:
        storage.objects[(RUN, "results/1/results.json")] = _results(1, "budget_timeout")

    backend = ScriptedBackend({1: [JobState.RUNNING] * 49 + [JobState.FAILED]}, on_status=partial)
    result = await _step(artifacts, storage, FakeClock()).run(_with(ctx, backend))
    assert result.output_ref is not None
    assert artifacts.get_json(result.output_ref)["results_key"] == "results/1/results.json"


async def test_the_runner_s_own_budget_timeout_is_not_a_preemption(
    store: Store, tmp_path: Path
) -> None:
    ctx, artifacts, storage = await _setup(store, tmp_path)  # budget_seconds = 600

    def timed_out(name: str, n: int) -> None:
        storage.objects[(RUN, "results/1/results.json")] = _results(1, "budget_timeout", 601)

    backend = ScriptedBackend({1: [JobState.FAILED]}, on_status=timed_out)
    result = await _step(artifacts, storage, FakeClock()).run(_with(ctx, backend))
    assert len(backend.specs) == 1
    assert result.output_ref is not None
    assert artifacts.get_json(result.output_ref)["results_key"] == "results/1/results.json"


async def test_a_retry_refused_by_the_daily_cloud_cap_keeps_the_first_attempt(
    store: Store, tmp_path: Path
) -> None:
    ctx, artifacts, storage = await _setup(store, tmp_path)
    cloud = load_cloud_prices(ROOT / "config" / "pricing.yaml")
    reserved_s = 900 + QUEUE_CAP_S  # USD is reserved for the queue too (PROVISIONING may bill)
    first = cloud.hourly("gpu-l40s-a", "1gpu-8vcpu-32gb", preemptible=True) * reserved_s / 3600
    retry = cloud.hourly("gpu-l40s-a", "1gpu-8vcpu-32gb") * reserved_s / 3600
    assert retry > first * 1.05
    limits = ctx.limits.model_copy(
        update={
            "global_": ctx.limits.global_.model_copy(update={"cloud_usd_per_day": first * 1.05})
        }
    )
    ctx = RunContext(
        **{**ctx.__dict__, "limits": limits, "budget": Budget(store, limits, env="test")}
    )
    backend = ScriptedBackend({1: [JobState.FAILED]})
    result = await _step(artifacts, storage, FakeClock()).run(_with(ctx, backend))
    assert len(backend.specs) == 1
    assert result.output_ref is not None
    stored = artifacts.get_json(result.output_ref)
    assert stored["results_key"] is None and stored["exit_reason"] == "budget_timeout"
    warnings = [p for p in await _events(store) if isinstance(p, WarningPayload)]
    assert len(warnings) == 2 and "cloud_usd_per_day" in warnings[1].message


async def test_an_experiment_error_is_not_a_preemption(store: Store, tmp_path: Path) -> None:
    ctx, artifacts, storage = await _setup(store, tmp_path)

    def crashed(name: str, n: int) -> None:
        storage.objects[(RUN, "results/1/results.json")] = _results(1, "experiment_error")

    backend = ScriptedBackend({1: [JobState.FAILED]}, on_status=crashed)
    result = await _step(artifacts, storage, FakeClock()).run(_with(ctx, backend))
    assert len(backend.specs) == 1
    assert result.output_ref is not None
    stored = artifacts.get_json(result.output_ref)
    assert stored["results_key"] == "results/1/results.json"
    assert stored["job_state"] == "FAILED"


async def test_an_on_demand_job_failing_without_results_is_a_runtime_error(
    store: Store, tmp_path: Path
) -> None:
    ctx, artifacts, storage = await _setup(store, tmp_path)
    storage.objects[(RUN, "results/1/results.json")] = _results(1)  # planted before the job
    backend = ScriptedBackend({1: [JobState.FAILED]})
    result = await _step(artifacts, storage, FakeClock(), preemptible=False).run(
        _with(ctx, backend)
    )
    assert len(backend.specs) == 1
    assert result.output_ref is not None
    stored = artifacts.get_json(result.output_ref)
    assert stored["results_key"] is None and stored["exit_reason"] == "experiment_error"


async def test_a_resumed_step_reattaches_to_its_job(store: Store, tmp_path: Path) -> None:
    ctx, artifacts, storage = await _setup(store, tmp_path)
    backend = ScriptedBackend({1: [JobState.SUCCEEDED]})
    first = await _step(artifacts, storage, FakeClock()).run(_with(ctx, backend))
    storage.objects[(RUN, "results/1/results.json")] = _results(1)  # written by that job
    again = await _step(artifacts, storage, FakeClock()).run(_with(ctx, backend))
    assert backend.created == 1  # get-by-name: no second GPU job
    assert first.output_ref == again.output_ref
    assert again.output_ref is not None
    assert artifacts.get_json(again.output_ref)["results_key"] == "results/1/results.json"


async def test_live_log_lines_become_job_log_events(store: Store, tmp_path: Path) -> None:
    ctx, artifacts, storage = await _setup(store, tmp_path)
    logs = {0: "epoch 1\nepoch 2\n", 1: "epoch 2\nepoch 3\n", 2: "epoch 3\n" + "x" * 900 + "\n"}

    def write_log(name: str, n: int) -> None:
        if n in logs:
            storage.objects[(RUN, "results/1/live.log")] = logs[n].encode()

    backend = ScriptedBackend(
        {1: [JobState.RUNNING, JobState.RUNNING, JobState.SUCCEEDED]}, on_status=write_log
    )
    await _step(artifacts, storage, FakeClock()).run(_with(ctx, backend))
    lines = [p.lines for p in await _events(store) if isinstance(p, JobLogPayload)]
    assert lines[:2] == [["epoch 1", "epoch 2"], ["epoch 3"]]
    assert lines[2] == ["x" * 500]  # truncated for display
    assert len(lines) == 3


async def test_an_oversized_live_log_is_skipped(store: Store, tmp_path: Path) -> None:
    ctx, artifacts, storage = await _setup(store, tmp_path)

    def huge(name: str, n: int) -> None:
        storage.objects[(RUN, "results/1/live.log")] = b"y\n" * (600 * 1024)

    backend = ScriptedBackend({1: [JobState.RUNNING, JobState.SUCCEEDED]}, on_status=huge)
    result = await _step(artifacts, storage, FakeClock()).run(_with(ctx, backend))
    assert result.next is StepName.ANALYZING
    assert not [p for p in await _events(store) if isinstance(p, JobLogPayload)]


async def test_the_watchdog_cancels_a_stuck_job(store: Store, tmp_path: Path) -> None:
    ctx, artifacts, storage = await _setup(store, tmp_path)
    backend = ScriptedBackend({1: [JobState.RUNNING]})
    with pytest.raises(P2PError) as err:
        await _step(artifacts, storage, FakeClock()).run(_with(ctx, backend))
    assert err.value.code is ErrorCode.TIMEOUT
    assert backend.cancelled == [f"p2p-exec-{SLUG}-1"]


async def test_a_cancelled_run_cancels_the_job(store: Store, tmp_path: Path) -> None:
    ctx, artifacts, storage = await _setup(store, tmp_path)
    backend = ScriptedBackend({1: [JobState.RUNNING]})
    step = _step(artifacts, storage, FakeClock())

    async def sleep_then_cancel(seconds: float) -> None:
        await store.cancel_run(RUN)

    step._sleep = sleep_then_cancel
    result = await step.run(_with(ctx, backend))
    assert result.next is RunStatus.CANCELLED
    assert backend.cancelled == [f"p2p-exec-{SLUG}-1"]


def _smoke_spec(iteration: int) -> JobSpec:
    return JobSpec(
        name=f"p2p-smoke-{SLUG}-{iteration}", kind=JobKind.SMOKE,
        image_digest_ref=DIGEST.format("p2p-runner-cpu"), platform="cpu-d3",
        preset="4vcpu-16gb", disk_gib=30, subnet_id="vpcsubnet-exec", volumes=[],
        inject_files={}, env={}, preemptible=False, watchdog_s=300,
    )  # fmt: skip


async def test_a_smoke_job_left_running_is_stopped_before_the_exec_job(
    store: Store, tmp_path: Path
) -> None:
    ctx, artifacts, storage = await _setup(store, tmp_path)
    backend = ScriptedBackend({1: [JobState.SUCCEEDED]})
    backend.final_state = JobState.RUNNING
    await backend.submit(_smoke_spec(3))  # left by a restart; it mounts the bucket rw
    order: list[str] = []
    original = backend.cancel

    async def cancel(handle: JobHandle) -> None:
        order.append(f"cancel {handle.name}")
        await original(handle)

    backend.cancel = cancel  # type: ignore[method-assign]
    backend.on_submit = lambda spec: order.append(f"submit {spec.name}")
    await _step(artifacts, storage, FakeClock()).run(_with(ctx, backend))
    assert order == [f"cancel p2p-smoke-{SLUG}-3", f"submit p2p-exec-{SLUG}-1"]


async def test_a_smoke_job_that_never_stops_blocks_the_exec_job(
    store: Store, tmp_path: Path
) -> None:
    ctx, artifacts, storage = await _setup(store, tmp_path)

    class Stuck(ScriptedBackend):
        async def status(self, handle: JobHandle) -> JobState:
            if "smoke" in handle.name:
                return JobState.RUNNING
            return await super().status(handle)

    backend = Stuck({1: [JobState.SUCCEEDED]})
    backend.final_state = JobState.RUNNING
    await backend.submit(_smoke_spec(1))
    backend.specs.clear()
    with pytest.raises(P2PError) as err:
        await _step(artifacts, storage, FakeClock()).run(_with(ctx, backend))
    assert err.value.code is ErrorCode.TIMEOUT
    assert backend.specs == []  # no exec job


@pytest.mark.parametrize(
    ("previous", "current", "expected"),
    [
        ([], ["a", "b"], ["a", "b"]),
        (["a", "b"], ["a", "b"], []),
        (["a", "b"], ["b", "c"], ["c"]),
        (["a", "b", "c"], ["b", "c", "d", "e"], ["d", "e"]),
        (["a"], ["x", "y"], ["x", "y"]),
        (["a", "b"], [], []),
    ],
)
def test_new_lines_skips_what_was_already_shown(
    previous: list[str], current: list[str], expected: list[str]
) -> None:
    assert new_lines(previous, current) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"not json", (None, None)),
        (b"[]", (None, None)),
        (b'{"exit_reason": "anything"}', (None, None)),
        (b'{"exit_reason": "budget_timeout", "wall_clock_s": true}', ("budget_timeout", None)),
        (b'{"exit_reason": "budget_timeout", "wall_clock_s": NaN}', ("budget_timeout", None)),
        (b'{"exit_reason": "completed", "wall_clock_s": 12}', ("completed", 12.0)),
    ],
)
def test_exit_info_reads_results_loosely(raw: bytes, expected: tuple[Any, Any]) -> None:
    assert exit_info(raw) == expected


async def test_a_report_without_valid_digests_fails_provisioning(
    store: Store, tmp_path: Path
) -> None:
    ctx, artifacts, storage = await _setup(store, tmp_path)
    artifacts.put_json(REPORT_KEY, {"overlay_sha256": "not a digest", "data_archives": []})
    with pytest.raises(P2PError) as err:
        await _step(artifacts, storage, FakeClock()).run(_with(ctx, ScriptedBackend({})))
    assert err.value.code is ErrorCode.PROVISION_FAILED


async def test_no_exec_job_when_the_gpu_minutes_are_spent(store: Store, tmp_path: Path) -> None:
    ctx, artifacts, storage = await _setup(store, tmp_path)
    spent = await ctx.budget.check_and_reserve(RUN, SpendKind.GPU_MINUTES, 25)
    await ctx.budget.commit(spent, 25)
    backend = ScriptedBackend({1: [JobState.SUCCEEDED]})
    with pytest.raises(BudgetExceeded):
        await _step(artifacts, storage, FakeClock()).run(_with(ctx, backend))
    assert backend.specs == []


async def test_an_oversized_results_file_is_left_to_analyzing(store: Store, tmp_path: Path) -> None:
    ctx, artifacts, storage = await _setup(store, tmp_path)

    def huge(name: str, n: int) -> None:
        storage.objects[(RUN, "results/1/results.json")] = b"x" * (300 * 1024)

    backend = ScriptedBackend({1: [JobState.FAILED]}, on_status=huge)
    result = await _step(artifacts, storage, FakeClock()).run(_with(ctx, backend))
    assert len(backend.specs) == 1  # not a preemption: ANALYZING makes it a CONTRACT_ERROR
    assert result.output_ref is not None
    assert artifacts.get_json(result.output_ref)["results_key"] == "results/1/results.json"


async def test_a_restart_during_the_retry_resumes_the_retry(store: Store, tmp_path: Path) -> None:
    ctx, artifacts, storage = await _setup(store, tmp_path)

    def restart_during_retry(name: str, n: int) -> None:
        if name.endswith("-2") and n == 0:
            raise asyncio.CancelledError  # the control plane shuts down

    backend = ScriptedBackend(
        {1: [JobState.FAILED], 2: [JobState.RUNNING, JobState.SUCCEEDED]},
        on_status=restart_during_retry,
    )
    with pytest.raises(asyncio.CancelledError):
        await _step(artifacts, storage, FakeClock()).run(_with(ctx, backend))
    # meanwhile the retry's untrusted code rewrites attempt 1's results
    storage.objects[(RUN, "results/1/results.json")] = _results(1)
    storage.objects[(RUN, "results/2/results.json")] = _results(2)
    result = await _step(artifacts, storage, FakeClock()).run(_with(ctx, backend))
    assert result.output_ref is not None
    stored = artifacts.get_json(result.output_ref)
    assert stored["results_key"] == "results/2/results.json"  # the retry is still watched
    assert backend.created == 2
    assert backend.calls[f"p2p-exec-{SLUG}-1"] == 1  # attempt 1 is not decided again


async def test_the_kill_switch_on_the_retry_keeps_the_first_attempt(
    store: Store, tmp_path: Path
) -> None:
    ctx, artifacts, storage = await _setup(store, tmp_path)

    def trip(name: str, n: int) -> None:
        if name.endswith("-1"):
            storage.objects[(RUN, "results/1/results.json")] = _results(1, "budget_timeout", 120)

    backend = ScriptedBackend({1: [JobState.FAILED]}, on_status=trip)
    step = _step(artifacts, storage, FakeClock())
    original = step._manifest

    async def manifest_then_trip(ctx_: RunContext, plan: Plan, attempt: int) -> Any:
        if attempt == 2:
            await ctx.budget.set_kill_switch(True)
        return await original(ctx_, plan, attempt)

    step._manifest = manifest_then_trip  # type: ignore[method-assign]
    result = await step.run(_with(ctx, backend))
    assert result.output_ref is not None
    assert artifacts.get_json(result.output_ref)["results_key"] == "results/1/results.json"
    assert len(backend.specs) == 1


async def test_a_long_live_log_is_diffed_quickly(store: Store, tmp_path: Path) -> None:
    # vertical tabs and other separators must not explode into a quadratic diff
    ctx, artifacts, storage = await _setup(store, tmp_path)

    def write_log(name: str, n: int) -> None:
        body = ("a" + chr(11)) * 200_000 + ("\n" + "b" * 10) * (n + 1)
        storage.objects[(RUN, "results/1/live.log")] = body.encode()[: 500 * 1024]

    backend = ScriptedBackend(
        {1: [JobState.RUNNING, JobState.RUNNING, JobState.SUCCEEDED]}, on_status=write_log
    )
    started = time.monotonic()
    await _step(artifacts, storage, FakeClock()).run(_with(ctx, backend))
    assert time.monotonic() - started < 5
    lines = [p.lines for p in await _events(store) if isinstance(p, JobLogPayload)]
    assert all(chr(11) not in line for batch in lines for line in batch)


async def test_a_report_without_its_repo_digest_fails_closed(store: Store, tmp_path: Path) -> None:
    ctx, artifacts, storage = await _setup(store, tmp_path)
    report = artifacts.get_json(REPORT_KEY)
    artifacts.put_json(REPORT_KEY, {**report, "repo": True, "repo_sha256": None})
    with pytest.raises(P2PError) as err:
        await _step(artifacts, storage, FakeClock()).run(_with(ctx, ScriptedBackend({})))
    assert err.value.code is ErrorCode.PROVISION_FAILED


async def test_a_dataset_without_its_archive_fails_closed(store: Store, tmp_path: Path) -> None:
    ctx, artifacts, storage = await _setup(
        store, tmp_path, datasets=("uoft-cs/cifar10", "other/set")
    )
    with pytest.raises(P2PError) as err:
        await _step(artifacts, storage, FakeClock()).run(_with(ctx, ScriptedBackend({})))
    assert err.value.code is ErrorCode.PROVISION_FAILED


async def test_a_smoke_result_without_a_workspace_digest_is_an_internal_error(
    store: Store, tmp_path: Path
) -> None:
    ctx, artifacts, storage = await _setup(store, tmp_path)
    smoke = artifacts.get_json(SMOKE_KEY)
    del smoke["workspace_sha256"]
    artifacts.put_json(SMOKE_KEY, smoke)
    with pytest.raises(P2PError) as err:
        await _step(artifacts, storage, FakeClock()).run(_with(ctx, ScriptedBackend({})))
    assert err.value.code is ErrorCode.INTERNAL_ERROR


class RetryNeverStops(ScriptedBackend):
    """The retry job stays queued, then ignores its cancellation (Nebius CANCELLING)."""

    async def status(self, handle: JobHandle) -> JobState:
        if handle.name.endswith("-2") and handle.name in self.cancelled:
            return JobState.RUNNING
        return await super().status(handle)


async def test_a_queued_retry_that_does_not_stop_never_lets_attempt_1_be_read(
    store: Store, tmp_path: Path
) -> None:
    """The retry may have started between the last poll and the cancel: it mounts the bucket
    read-write, so attempt 1's partial results can't be trusted any more."""
    ctx, artifacts, storage = await _setup(store, tmp_path)

    def partial(name: str, n: int) -> None:
        if name.endswith("-1"):
            storage.objects[(RUN, "results/1/results.json")] = _results(1, "budget_timeout", 120)

    backend = RetryNeverStops({1: [JobState.FAILED], 2: [JobState.PENDING]}, on_status=partial)
    result = await _step(artifacts, storage, FakeClock()).run(_with(ctx, backend))
    assert [s.name for s in backend.specs] == [f"p2p-exec-{SLUG}-1", f"p2p-exec-{SLUG}-2"]
    assert result.output_ref is not None
    stored = artifacts.get_json(result.output_ref)
    assert stored["manifest"]["attempt"] == 1
    assert (stored["results_key"], stored["exit_reason"]) == (None, "budget_timeout")
    codes = [p.code for p in await _events(store) if isinstance(p, WarningPayload)]
    assert "NO_GPU_CAPACITY" in codes


async def test_a_queued_retry_that_stopped_still_never_lets_attempt_1_be_read(
    store: Store, tmp_path: Path
) -> None:
    """It may have run between the last poll and the cancel, then stopped: no way to tell."""
    ctx, artifacts, storage = await _setup(store, tmp_path)

    def partial(name: str, n: int) -> None:
        if name.endswith("-1"):
            storage.objects[(RUN, "results/1/results.json")] = _results(1, "budget_timeout", 120)

    backend = ScriptedBackend({1: [JobState.FAILED], 2: [JobState.PENDING]}, on_status=partial)
    result = await _step(artifacts, storage, FakeClock()).run(_with(ctx, backend))
    assert result.output_ref is not None
    stored = artifacts.get_json(result.output_ref)
    assert (stored["results_key"], stored["exit_reason"]) == (None, "budget_timeout")


# --- plan B: the exec job in Sandboxes (docs/specs/hybrid-deploy.md) -----------------------


async def test_an_exec_job_in_sandboxes_runs_runner_cpu_and_never_falls_back_to_a_gpu(
    store: Store, tmp_path: Path
) -> None:
    ctx, artifacts, storage = await _setup(store, tmp_path)
    config = _config(preemptible=False).model_copy(
        update={
            "runner_image": DIGEST.format("p2p-runner-cpu"),
            "exec_platform": "sandbox",
            "exec_preset": "4vcpu-4gb",
            "exec_fallback": None,
        }
    )

    def finish(name: str, n: int) -> None:
        storage.objects[(RUN, "results/1/results.json")] = b"{}"

    backend = ScriptedBackend({1: [JobState.SUCCEEDED]}, on_status=finish)
    backend.no_capacity = {"sandbox": 3}  # the instance limit, three times
    clock = FakeClock()
    step = ExecutingStep(
        artifacts=artifacts, storage=storage, config=config,
        cloud=load_cloud_prices(ROOT / "config" / "pricing.yaml"), poll_s=30, clock=clock,
        sleep=clock.sleep,
    )  # fmt: skip
    result = await step.run(_with(ctx, backend))
    assert [(s.platform, s.preset) for s in backend.specs] == [("sandbox", "4vcpu-4gb")] * 4
    assert {s.image_digest_ref for s in backend.specs} == {DIGEST.format("p2p-runner-cpu")}
    assert result.output_ref is not None
    stored = artifacts.get_json(result.output_ref)
    assert stored["hardware"] == {
        "platform": "sandbox",
        "preset": "4vcpu-4gb",
        "preemptible": False,
    }
    assert stored["manifest"]["image_digest"] == DIGEST.format("p2p-runner-cpu").rsplit("@", 1)[1]
    messages = [p.message for p in await _events(store) if isinstance(p, WarningPayload)]
    assert messages and all("GPU" not in m for m in messages)

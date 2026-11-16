"""EXECUTING on fakes: the exec manifest, the GPU job, live.log as job.log events, the
watchdog and one on-demand retry after a preemption."""

import asyncio
import json
import logging
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
    ExecTarget,
    FallbackReason,
    GpuFallback,
    Plan,
    PlanArtifact,
    PlanMode,
    RunStatus,
    Scale,
    SetupItem,
    SetupKind,
    StepName,
)
from app.execution.backend import JobHandle, JobKind, JobSpec, JobState
from app.execution.config import ExecutionConfig
from app.execution.fake_backend import FakeJobBackend
from app.execution.nebius_backend import JobBackendError, JobCreateFailed
from app.execution.run_storage import FakeRunStorage
from app.models.pricing import load_cloud_prices
from app.orchestrator.budget import (
    Budget,
    BudgetExceeded,
    KillSwitchActive,
    SpendKind,
    load_budget_limits,
)
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


def _config(preemptible: bool = True, *, sandbox: bool = False) -> ExecutionConfig:
    in_sandboxes = {"exec_platform": "sandbox", "exec_preset": "4vcpu-4gb", "exec_fallback": None}
    return ExecutionConfig.model_validate(
        {
            "provisioner_image": DIGEST.format("p2p-provisioner"),
            "runner_image": DIGEST.format("p2p-runner"),
            "runner_cpu_image": DIGEST.format("p2p-runner-cpu"),
            "subnet_provision": "vpcsubnet-provision",
            "subnet_exec": "vpcsubnet-exec",
            "preemptible_gpu": preemptible,
            **(in_sandboxes if sandbox else {}),
        }
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
    sandbox: bool = False,
) -> ExecutingStep:
    return ExecutingStep(
        artifacts=artifacts,
        storage=storage,
        config=_config(preemptible, sandbox=sandbox),
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


async def test_the_manifest_carries_the_time_target(store: Store, tmp_path: Path) -> None:
    """A-46: after the verdict's minimum the runner starts a seed only while it should end
    within the target (a plan of three seeds, as approved before A-46, still builds)."""
    ctx, artifacts, storage = await _setup(store, tmp_path)
    backend = ScriptedBackend({1: [JobState.SUCCEEDED]})
    await _step(artifacts, storage, FakeClock()).run(_with(ctx, backend))
    manifest = _manifest_of(backend.specs[0])
    assert manifest.seeds == [0, 1, 2] and manifest.min_seeds == 3
    assert manifest.target_seconds == ctx.limits.per_run.seed_target_s == 600


@pytest.mark.parametrize("sandbox", [True, False])
async def test_in_sandboxes_the_experiment_gets_the_runs_whole_budget(
    store: Store, tmp_path: Path, sandbox: bool
) -> None:
    """KAN (eval set B, 2026-10-30): planned at 8 minutes, it finished one seed of three and
    ended INCONCLUSIVE. A CPU sandbox costs cents per hour and stops when the script does, so
    the Planner's estimate does not cap it there; on GPUs it still does."""
    ctx, artifacts, storage = await _setup(store, tmp_path, gpu_minutes=8)
    backend = ScriptedBackend({1: [JobState.SUCCEEDED]})
    await _step(artifacts, storage, FakeClock(), sandbox=sandbox).run(_with(ctx, backend))
    budget = _manifest_of(backend.specs[0]).budget_seconds
    whole = ctx.limits.per_run.cpu_minutes * 60 - EXEC_MARGIN_S  # A-47: the CPU allowance
    assert budget == (whole if sandbox else 8 * 60)


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
    # 10 minutes under the cap planned: the first job runs all but 30 s of its watchdog (the
    # plan + 5 minutes), then is preempted; the 5.5 minutes left can't hold even a 60 s budget
    # plus the margin.
    planned = int(load_budget_limits(ROOT / "config" / "budgets.yaml").per_run.gpu_minutes) - 10
    ctx, artifacts, storage = await _setup(store, tmp_path, gpu_minutes=planned)
    polls = 2 * (planned + 5) - 1
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
    planned = int(load_budget_limits(ROOT / "config" / "budgets.yaml").per_run.gpu_minutes) - 10
    ctx, artifacts, storage = await _setup(store, tmp_path, gpu_minutes=planned)

    def partial(name: str, n: int) -> None:
        storage.objects[(RUN, "results/1/results.json")] = _results(1, "budget_timeout")

    polls = 2 * (planned + 5) - 1  # all but 30 s of its watchdog: no room for a retry
    backend = ScriptedBackend(
        {1: [JobState.RUNNING] * polls + [JobState.FAILED]}, on_status=partial
    )
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
    used = ctx.limits.per_run.gpu_minutes - 5  # 5 minutes left: not even the margin
    spent = await ctx.budget.check_and_reserve(RUN, SpendKind.GPU_MINUTES, used)
    await ctx.budget.commit(spent, used)
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

    async def manifest_then_trip(ctx_: RunContext, plan: Plan, attempt: int, where: Any) -> Any:
        if attempt == 2:
            await ctx.budget.set_kill_switch(True)
        return await original(ctx_, plan, attempt, where)

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


# --- the effective scale follows the setup the run used (A-43) ------------------------------

EPOCHS = SetupItem(id="s1", name="epochs", kind=SetupKind.NUMBER, paper="200", planned="200")


def _record(used: str) -> dict[str, Any]:
    return {
        "id": "s1",
        "name": "epochs",
        "kind": "NUMBER",
        "paper": "200",
        "planned": "200",
        "used": used,
        "where": "workspace/train.py",
        "matches_paper": used == "200",
    }


async def _full_scale(
    store: Store,
    tmp_path: Path,
    *,
    setup: list[SetupItem] | None,
    records: list[dict[str, Any]] | None,
) -> bool:
    """Whether a plan labelled FULL, with this setup and these smoke records, runs at full scale."""
    ctx, artifacts, storage = await _setup(store, tmp_path)
    plan = artifacts.get_json(PLAN_KEY)
    plan["plan"]["scale"] = "FULL"
    plan["plan"]["setup"] = [item.model_dump(mode="json") for item in setup or []]
    artifacts.put_json(PLAN_KEY, plan)
    if records is not None:
        artifacts.put_json(SMOKE_KEY, {**artifacts.get_json(SMOKE_KEY), "setup": records})

    def finish(name: str, n: int) -> None:
        storage.objects[(RUN, "results/1/results.json")] = _results(1)

    backend = ScriptedBackend({1: [JobState.RUNNING, JobState.SUCCEEDED]}, on_status=finish)
    await _step(artifacts, storage, FakeClock()).run(_with(ctx, backend))
    [spec] = backend.specs
    return _manifest_of(spec).scale.full


async def test_a_setup_the_run_followed_keeps_full_scale(store: Store, tmp_path: Path) -> None:
    assert await _full_scale(store, tmp_path, setup=[EPOCHS], records=[_record("200")]) is True


async def test_a_deviating_setup_runs_at_reduced_scale(store: Store, tmp_path: Path) -> None:
    """Production, 2026-10-30: 200 steps under a plan that said full scale."""
    assert await _full_scale(store, tmp_path, setup=[EPOCHS], records=[_record("20")]) is False


async def test_a_plan_with_conditions_and_no_records_is_not_full_scale(
    store: Store, tmp_path: Path
) -> None:
    """An attempt from before `finish` recorded the setup, or a resumed one."""
    assert await _full_scale(store, tmp_path, setup=[EPOCHS], records=None) is False


async def test_a_plan_with_conditions_and_an_empty_record_list_is_not_full_scale(
    store: Store, tmp_path: Path
) -> None:
    assert await _full_scale(store, tmp_path, setup=[EPOCHS], records=[]) is False


async def test_a_plan_without_conditions_keeps_its_scale(store: Store, tmp_path: Path) -> None:
    """Plans stored before the setup existed."""
    assert await _full_scale(store, tmp_path, setup=None, records=None) is True


async def test_an_unreadable_setup_record_is_an_internal_error(
    store: Store, tmp_path: Path
) -> None:
    with pytest.raises(P2PError) as err:
        await _full_scale(store, tmp_path, setup=[EPOCHS], records=[{"id": "s1"}])
    assert err.value.code is ErrorCode.INTERNAL_ERROR


async def test_records_for_other_conditions_are_not_full_scale(
    store: Store, tmp_path: Path
) -> None:
    other = {**_record("200"), "id": "s9"}
    assert await _full_scale(store, tmp_path, setup=[EPOCHS], records=[other]) is False


# --- GPU when needed (A-47): the GPU window and the fallback to the CPU plan ---------------

FALLBACK_KEY = f"runs/{RUN}/gpu_fallback.json"
WINDOW_KEY = f"runs/{RUN}/gpu_window.json"
PROVISIONED_KEY = f"runs/{RUN}/fallback_provisioned.json"


def _auto_config(preemptible: bool = False) -> ExecutionConfig:
    return _config(preemptible).model_copy(update={"cpu_exec": ("sandbox", "4vcpu-4gb")})


def _auto_step(
    artifacts: LocalArtifactStore, storage: FakeRunStorage, clock: FakeClock
) -> ExecutingStep:
    return ExecutingStep(
        artifacts=artifacts,
        storage=storage,
        config=_auto_config(),
        cloud=load_cloud_prices(ROOT / "config" / "pricing.yaml"),
        poll_s=30,
        clock=clock,
        sleep=clock.sleep,
    )


async def _gpu_target(
    store: Store, tmp_path: Path
) -> tuple[RunContext, LocalArtifactStore, FakeRunStorage]:
    """A run PLANNING sent to the GPU, with its CPU plan to fall back to."""
    ctx, artifacts, storage = await _setup(store, tmp_path)
    stored = artifacts.get_json(PLAN_KEY)
    gpu = Plan.model_validate(stored["plan"]).model_copy(update={"gpu_minutes_est": 40})
    cpu = gpu.model_copy(update={"gpu_minutes_est": 28})
    artifact = PlanArtifact(
        plan=gpu,
        est_cost_usd=1.0,
        target=ExecTarget.GPU,
        cpu_plan=cpu,
        est_cost_max_usd=1.7,
    )
    artifacts.put_json(PLAN_KEY, artifact.model_dump(mode="json"))
    return ctx, artifacts, storage


def _fallback_of(artifacts: LocalArtifactStore) -> GpuFallback:
    return GpuFallback.model_validate(artifacts.get_json(FALLBACK_KEY))


async def _fell_back_with(
    store: Store, result: Any, artifacts: LocalArtifactStore, reason: FallbackReason
) -> None:
    assert (result.next, result.output_ref) == (StepName.PROVISIONING, FALLBACK_KEY)
    record = _fallback_of(artifacts)
    assert (record.reason, record.gpu_attempt) == (reason, 1)
    warnings = [p for p in await _events(store) if isinstance(p, WarningPayload)]
    assert [w.message for w in warnings if w.code == "NO_GPU_FALLBACK_CPU"] == [record.message]


async def test_with_no_gpu_capacity_the_run_falls_back_to_its_cpu_plan(
    store: Store, tmp_path: Path
) -> None:
    ctx, artifacts, storage = await _gpu_target(store, tmp_path)
    backend = ScriptedBackend({1: [JobState.SUCCEEDED]})
    backend.no_capacity = {"gpu-l40s-a": 99, "gpu-l40s-d": 99}
    clock = FakeClock()
    result = await _auto_step(artifacts, storage, clock).run(_with(ctx, backend))
    await _fell_back_with(store, result, artifacts, FallbackReason.NO_CAPACITY)
    assert _fallback_of(artifacts).message == (
        "no GPU was free within 10 minutes: the CPU plan runs instead"
    )
    assert backend.jobs == {} and clock.t < ctx.limits.per_run.gpu_wait_s


async def test_a_gpu_job_still_queued_at_the_end_of_the_window_falls_back(
    store: Store, tmp_path: Path
) -> None:
    ctx, artifacts, storage = await _gpu_target(store, tmp_path)
    backend = ScriptedBackend({1: [JobState.PENDING]})
    clock = FakeClock()
    result = await _auto_step(artifacts, storage, clock).run(_with(ctx, backend))
    await _fell_back_with(store, result, artifacts, FallbackReason.QUEUE_TIMEOUT)
    assert backend.cancelled == [f"p2p-exec-{SLUG}-1"]
    window = ctx.limits.per_run.gpu_wait_s
    assert window <= clock.t < window + 120  # the window, a poll and the stop wait


async def test_the_window_counts_from_the_first_creation_across_a_restart(
    store: Store, tmp_path: Path
) -> None:
    ctx, artifacts, storage = await _gpu_target(store, tmp_path)
    clock = FakeClock()
    clock.t = 500.0  # the first try was created 500 s ago, before a restart
    artifacts.put_json(WINDOW_KEY, {"started_at": NOW.isoformat()})
    backend = ScriptedBackend({1: [JobState.PENDING]})
    result = await _auto_step(artifacts, storage, clock).run(_with(ctx, backend))
    await _fell_back_with(store, result, artifacts, FallbackReason.QUEUE_TIMEOUT)
    assert clock.t < ctx.limits.per_run.gpu_wait_s + 120  # not another full window


class RefusedCreate(ScriptedBackend):
    """The platform fails the creation without a reason (S5, 2026-10-27: code=13)."""

    async def submit(self, spec: JobSpec) -> JobHandle:
        raise JobCreateFailed(f"{spec.name}: job creation failed")


async def test_a_gpu_job_the_platform_fails_to_create_falls_back_and_alerts(
    store: Store, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ctx, artifacts, storage = await _gpu_target(store, tmp_path)
    with caplog.at_level(logging.WARNING):
        result = await _auto_step(artifacts, storage, FakeClock()).run(
            _with(ctx, RefusedCreate({}))
        )
    await _fell_back_with(store, result, artifacts, FallbackReason.PLATFORM_ERROR)
    assert _fallback_of(artifacts).message == (
        "the GPU job could not start (platform error): the CPU plan runs instead"
    )
    assert any(getattr(r, "alert", False) for r in caplog.records)


async def test_a_used_up_gpu_quota_falls_back(store: Store, tmp_path: Path) -> None:
    ctx, artifacts, storage = await _gpu_target(store, tmp_path)
    backend = ScriptedBackend({1: [JobState.SUCCEEDED]})
    backend.quota_exceeded = ["compute.instance.gpu.l40s"]
    result = await _auto_step(artifacts, storage, FakeClock()).run(_with(ctx, backend))
    await _fell_back_with(store, result, artifacts, FallbackReason.QUOTA)


async def test_a_daily_cloud_budget_too_small_for_the_gpu_falls_back(
    store: Store, tmp_path: Path
) -> None:
    ctx, artifacts, storage = await _gpu_target(store, tmp_path)
    cap = ctx.limits.global_.cloud_usd_per_day
    spent = await ctx.budget.check_and_reserve(RUN, SpendKind.CLOUD_USD, cap - 0.05)
    await ctx.budget.commit(spent, cap - 0.05)
    backend = ScriptedBackend({1: [JobState.SUCCEEDED]})
    result = await _auto_step(artifacts, storage, FakeClock()).run(_with(ctx, backend))
    await _fell_back_with(store, result, artifacts, FallbackReason.DAILY_CAP)
    assert backend.specs == []


async def test_the_kill_switch_stops_the_run_instead_of_falling_back(
    store: Store, tmp_path: Path
) -> None:
    ctx, artifacts, storage = await _gpu_target(store, tmp_path)
    await ctx.budget.set_kill_switch(True)
    with pytest.raises(KillSwitchActive):
        await _auto_step(artifacts, storage, FakeClock()).run(
            _with(ctx, ScriptedBackend({1: [JobState.SUCCEEDED]}))
        )
    with pytest.raises(FileNotFoundError):
        artifacts.get_json(FALLBACK_KEY)


async def test_a_gpu_job_that_starts_by_the_end_of_the_window_runs(
    store: Store, tmp_path: Path
) -> None:
    ctx, artifacts, storage = await _gpu_target(store, tmp_path)
    queued = [JobState.PENDING] * 19  # 19 polls of 30 s: still inside the 600 s window
    backend = ScriptedBackend({1: [*queued, JobState.RUNNING, JobState.SUCCEEDED]})
    storage.objects[(RUN, "results/1/results.json")] = _results(1)
    result = await _auto_step(artifacts, storage, FakeClock()).run(_with(ctx, backend))
    assert result.next is StepName.ANALYZING
    with pytest.raises(FileNotFoundError):
        artifacts.get_json(FALLBACK_KEY)
    assert backend.specs[0].platform == "gpu-l40s-a"
    assert backend.specs[0].image_digest_ref == DIGEST.format("p2p-runner")


async def test_a_gpu_job_that_ran_and_failed_is_the_experiment_s_result(
    store: Store, tmp_path: Path
) -> None:
    """A CUDA out-of-memory, say: the plan had 48 GB; no fallback."""
    ctx, artifacts, storage = await _gpu_target(store, tmp_path)

    def crashed(name: str, n: int) -> None:
        storage.objects[(RUN, "results/1/results.json")] = _results(1, "experiment_error", 30)

    backend = ScriptedBackend({1: [JobState.RUNNING, JobState.FAILED]}, on_status=crashed)
    result = await _auto_step(artifacts, storage, FakeClock()).run(_with(ctx, backend))
    assert result.next is StepName.ANALYZING
    with pytest.raises(FileNotFoundError):
        artifacts.get_json(FALLBACK_KEY)


async def test_a_gpu_job_that_failed_before_it_ran_falls_back(store: Store, tmp_path: Path) -> None:
    """Never seen running, no results written: the platform failed it (a pull, a mount)."""
    ctx, artifacts, storage = await _gpu_target(store, tmp_path)
    backend = ScriptedBackend({1: [JobState.PENDING, JobState.FAILED]})
    result = await _auto_step(artifacts, storage, FakeClock()).run(_with(ctx, backend))
    await _fell_back_with(store, result, artifacts, FallbackReason.PLATFORM_ERROR)


class NeverStops(ScriptedBackend):
    """The queued GPU job ignores its cancellation (Nebius CANCELLING): it may still start."""

    async def status(self, handle: JobHandle) -> JobState:
        if handle.name in self.cancelled:
            return JobState.RUNNING
        return await super().status(handle)

    async def list_jobs(self, prefix: str) -> list[tuple[JobHandle, JobState, datetime]]:
        return [(h, JobState.RUNNING, t) for h, _, t in await super().list_jobs(prefix)]


async def test_no_fallback_while_the_gpu_job_may_still_run(store: Store, tmp_path: Path) -> None:
    ctx, artifacts, storage = await _gpu_target(store, tmp_path)
    backend = NeverStops({1: [JobState.PENDING]})
    with pytest.raises(P2PError) as err:
        await _auto_step(artifacts, storage, FakeClock()).run(_with(ctx, backend))
    assert err.value.code is ErrorCode.TIMEOUT
    with pytest.raises(FileNotFoundError):
        artifacts.get_json(FALLBACK_KEY)


async def test_after_the_fallback_the_cpu_plan_runs_in_sandboxes(
    store: Store, tmp_path: Path
) -> None:
    ctx, artifacts, storage = await _gpu_target(store, tmp_path)
    record = GpuFallback(
        reason=FallbackReason.NO_CAPACITY,
        message="no GPU was free within 10 minutes: the CPU plan runs instead",
        at=NOW,
        gpu_attempt=1,
    )
    artifacts.put_json(FALLBACK_KEY, record.model_dump(mode="json"))
    artifacts.put_json(PROVISIONED_KEY, {"attempt": 2})
    storage.objects[(RUN, "results/3/results.json")] = _results(3)
    backend = ScriptedBackend({3: [JobState.SUCCEEDED]})
    result = await _auto_step(artifacts, storage, FakeClock()).run(_with(ctx, backend))
    assert result.next is StepName.ANALYZING
    [spec] = backend.specs
    assert (spec.name, spec.platform, spec.preemptible) == (f"p2p-exec-{SLUG}-3", "sandbox", False)
    assert spec.image_digest_ref == DIGEST.format("p2p-runner-cpu")
    manifest = _manifest_of(spec)
    cpu_cap = ctx.limits.per_run.cpu_minutes * 60
    assert manifest.budget_seconds <= cpu_cap - EXEC_MARGIN_S
    assert manifest.seeds == Plan.model_validate(artifacts.get_json(PLAN_KEY)["cpu_plan"]).seeds


async def test_a_step_resumed_after_the_fallback_record_goes_back_to_provisioning(
    store: Store, tmp_path: Path
) -> None:
    ctx, artifacts, storage = await _gpu_target(store, tmp_path)
    record = GpuFallback(
        reason=FallbackReason.QUOTA,
        message="the GPU quota is exhausted: the CPU plan runs instead",
        at=NOW,
        gpu_attempt=1,
    )
    artifacts.put_json(FALLBACK_KEY, record.model_dump(mode="json"))
    backend = ScriptedBackend({})
    result = await _auto_step(artifacts, storage, FakeClock()).run(_with(ctx, backend))
    assert (result.next, result.output_ref) == (StepName.PROVISIONING, FALLBACK_KEY)
    assert backend.specs == []


async def test_a_gpu_job_running_when_the_step_resumes_after_the_window_is_kept(
    store: Store, tmp_path: Path
) -> None:
    """A restart 30 minutes into a GPU run: the window is over, but the job left the queue
    long ago; it is re-attached, never cancelled for a fallback (security review)."""
    ctx, artifacts, storage = await _gpu_target(store, tmp_path)
    artifacts.put_json(WINDOW_KEY, {"started_at": NOW.isoformat()})
    name = f"p2p-exec-{SLUG}-1"
    backend = ScriptedBackend({1: [JobState.RUNNING, JobState.SUCCEEDED]})
    await store.record_job(
        RUN, "exec", name, "fakejob-0", "p2p-run-x", deadline=NOW + timedelta(hours=2), at=NOW,
        platform="gpu-l40s-a", preset="1gpu-8vcpu-32gb",
    )  # fmt: skip
    await store.mark_job_active(name, deadline=NOW + timedelta(hours=1))
    clock = FakeClock()
    clock.t = 1800.0
    storage.objects[(RUN, "results/1/results.json")] = _results(1)
    result = await _auto_step(artifacts, storage, clock).run(_with(ctx, backend))
    assert result.next is StepName.ANALYZING
    assert backend.cancelled == []
    with pytest.raises(FileNotFoundError):
        artifacts.get_json(FALLBACK_KEY)


class StatusBreaks(ScriptedBackend):
    """The job exists, but its status can't be trusted (a job id whose name differs)."""

    async def status(self, handle: JobHandle) -> JobState:
        raise JobBackendError(f"{handle.name}: the job id names another job")


async def test_a_broken_status_fails_the_run_instead_of_falling_back(
    store: Store, tmp_path: Path
) -> None:
    ctx, artifacts, storage = await _gpu_target(store, tmp_path)
    with pytest.raises(JobBackendError):
        await _auto_step(artifacts, storage, FakeClock()).run(_with(ctx, StatusBreaks({})))
    with pytest.raises(FileNotFoundError):
        artifacts.get_json(FALLBACK_KEY)


class CreatedButNotFound(ScriptedBackend):
    """The creation may have made a job nobody can see yet: never a fallback."""

    async def submit(self, spec: JobSpec) -> JobHandle:
        raise JobBackendError(f"{spec.name}: created but not found")


async def test_a_creation_that_may_have_made_a_job_never_falls_back(
    store: Store, tmp_path: Path
) -> None:
    ctx, artifacts, storage = await _gpu_target(store, tmp_path)
    with pytest.raises(JobBackendError):
        await _auto_step(artifacts, storage, FakeClock()).run(_with(ctx, CreatedButNotFound({})))
    with pytest.raises(FileNotFoundError):
        artifacts.get_json(FALLBACK_KEY)


async def test_a_gpu_plan_without_a_cpu_plan_never_falls_back(store: Store, tmp_path: Path) -> None:
    """A GPU plan from the designed deploy, resumed under auto: nothing to fall back to."""
    ctx, artifacts, storage = await _gpu_target(store, tmp_path)
    stored = artifacts.get_json(PLAN_KEY)
    artifacts.put_json(PLAN_KEY, {**stored, "cpu_plan": None})
    backend = ScriptedBackend({1: [JobState.SUCCEEDED]})
    backend.no_capacity = {"gpu-l40s-a": 99, "gpu-l40s-d": 99}
    result = await _auto_step(artifacts, storage, FakeClock()).run(_with(ctx, backend))
    assert result.next is StepName.ANALYZING  # INCONCLUSIVE/CAPACITY, as before A-47
    with pytest.raises(FileNotFoundError):
        artifacts.get_json(FALLBACK_KEY)


async def _recorded_gpu_job(store: Store, backend: ScriptedBackend) -> str:
    """Attempt 1's GPU job, created and recorded before a restart (it never left the queue)."""
    spec = _spec_named(f"p2p-exec-{SLUG}-1")
    handle = await backend.submit(spec)
    await store.record_job(
        RUN, "exec", spec.name, handle.nebius_id, "p2p-run-x", deadline=NOW + timedelta(hours=2),
        at=NOW, platform="gpu-l40s-a", preset="1gpu-8vcpu-32gb",
    )  # fmt: skip
    return spec.name


def _spec_named(name: str) -> JobSpec:
    return JobSpec(
        name=name, kind=JobKind.EXEC, image_digest_ref=DIGEST.format("p2p-runner"),
        platform="gpu-l40s-a", preset="1gpu-8vcpu-32gb", disk_gib=100,
        subnet_id="vpcsubnet-exec", volumes=[], inject_files={}, env={}, preemptible=False,
        watchdog_s=2700,
    )  # fmt: skip


async def test_a_queued_gpu_job_resumed_after_a_restart_still_gets_only_its_window(
    store: Store, tmp_path: Path
) -> None:
    """Final review, Critical 1: re-attached by name, the queued job still gets only what is
    left of the window counted from its first creation, then the run falls back."""
    ctx, artifacts, storage = await _gpu_target(store, tmp_path)
    artifacts.put_json(WINDOW_KEY, {"started_at": NOW.isoformat()})
    backend = ScriptedBackend({1: [JobState.PENDING]})
    await _recorded_gpu_job(store, backend)
    clock = FakeClock()
    clock.t = 300.0  # restarted half-way through the window
    result = await _auto_step(artifacts, storage, clock).run(_with(ctx, backend))
    await _fell_back_with(store, result, artifacts, FallbackReason.QUEUE_TIMEOUT)
    assert backend.created == 1  # re-attached: no second job
    assert clock.t < ctx.limits.per_run.gpu_wait_s + 120


async def test_a_gpu_job_cancelled_from_the_queue_before_a_restart_falls_back(
    store: Store, tmp_path: Path
) -> None:
    """Final review, Important 3: the queue-timeout cancel happened, then a crash before the
    fallback was recorded (or the platform cancelled it): never ran, so the CPU plan runs."""
    ctx, artifacts, storage = await _gpu_target(store, tmp_path)
    artifacts.put_json(WINDOW_KEY, {"started_at": NOW.isoformat()})
    backend = ScriptedBackend({1: [JobState.PENDING]})
    name = await _recorded_gpu_job(store, backend)
    backend.cancelled.append(name)
    await store.update_job(name, "CANCELLED", gpu_seconds=0.0, cost_usd=0.0)
    clock = FakeClock()
    clock.t = 700.0  # after the window
    result = await _auto_step(artifacts, storage, clock).run(_with(ctx, backend))
    await _fell_back_with(store, result, artifacts, FallbackReason.QUEUE_TIMEOUT)


async def test_a_gpu_job_that_ran_before_a_restart_and_then_failed_is_its_result(
    store: Store, tmp_path: Path
) -> None:
    """Final review, Important 4: its row says it left the queue before the restart; failing
    without results.json afterwards is the experiment's outcome, not a platform failure."""
    ctx, artifacts, storage = await _gpu_target(store, tmp_path)
    artifacts.put_json(WINDOW_KEY, {"started_at": NOW.isoformat()})
    backend = ScriptedBackend({1: [JobState.FAILED]})
    name = await _recorded_gpu_job(store, backend)
    await store.mark_job_active(name, deadline=NOW + timedelta(hours=1))
    clock = FakeClock()
    clock.t = 1500.0
    result = await _auto_step(artifacts, storage, clock).run(_with(ctx, backend))
    assert result.next is StepName.ANALYZING
    assert result.output_ref is not None
    assert artifacts.get_json(result.output_ref)["exit_reason"] == "experiment_error"
    with pytest.raises(FileNotFoundError):
        artifacts.get_json(FALLBACK_KEY)


class CancelsSlowly(ScriptedBackend):
    """After our cancel the job reports RUNNING (Nebius CANCELLING) once, then CANCELLED."""

    async def status(self, handle: JobHandle) -> JobState:
        if handle.name in self.cancelled:
            self.after_cancel = getattr(self, "after_cancel", 0) + 1
            return JobState.RUNNING if self.after_cancel == 1 else JobState.CANCELLED
        return await super().status(handle)


async def test_our_own_cancel_of_a_queued_gpu_job_never_marks_it_as_started(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CANCELLING maps to RUNNING: the row must not say the job left the queue, or a restart
    would take the cancelled job for one that ran."""
    ctx, artifacts, storage = await _gpu_target(store, tmp_path)
    marked: list[str] = []
    original = store.mark_job_active

    async def spy(name: str, **kwargs: Any) -> None:
        marked.append(name)
        await original(name, **kwargs)

    monkeypatch.setattr(store, "mark_job_active", spy)
    result = await _auto_step(artifacts, storage, FakeClock()).run(
        _with(ctx, CancelsSlowly({1: [JobState.PENDING]}))
    )
    await _fell_back_with(store, result, artifacts, FallbackReason.QUEUE_TIMEOUT)
    assert marked == []


async def test_no_fallback_when_the_cpu_plan_would_have_no_time_left(
    store: Store, tmp_path: Path
) -> None:
    """Minor 1: the run's experiment minutes are nearly spent: re-provisioning and a Coder pass
    for a CPU attempt that can't run would only cost; the run ends without capacity, as before."""
    ctx, artifacts, storage = await _gpu_target(store, tmp_path)
    used = ctx.limits.per_run.cpu_minutes - 2
    spent = await ctx.budget.check_and_reserve(RUN, SpendKind.GPU_MINUTES, used)
    await ctx.budget.commit(spent, used)
    backend = ScriptedBackend({1: [JobState.SUCCEEDED]})
    backend.no_capacity = {"gpu-l40s-a": 99, "gpu-l40s-d": 99}
    result = await _auto_step(artifacts, storage, FakeClock()).run(_with(ctx, backend))
    assert result.next is StepName.ANALYZING and result.output_ref is not None
    assert artifacts.get_json(result.output_ref)["exit_reason"] == "capacity"
    with pytest.raises(FileNotFoundError):
        artifacts.get_json(FALLBACK_KEY)


async def test_no_capacity_warning_when_no_gpu_job_was_ever_requested(
    store: Store, tmp_path: Path
) -> None:
    """Minor 2: the window ended before any try (an outage between the window file and the
    first create): no claim about a platform's capacity."""
    ctx, artifacts, storage = await _gpu_target(store, tmp_path)
    artifacts.put_json(WINDOW_KEY, {"started_at": NOW.isoformat()})
    clock = FakeClock()
    clock.t = 700.0
    backend = ScriptedBackend({1: [JobState.SUCCEEDED]})
    result = await _auto_step(artifacts, storage, clock).run(_with(ctx, backend))
    await _fell_back_with(store, result, artifacts, FallbackReason.NO_CAPACITY)
    codes = [p.code for p in await _events(store) if isinstance(p, WarningPayload)]
    assert "NO_GPU_CAPACITY" not in codes and backend.specs == []


async def test_the_execution_record_names_the_runner_the_job_ran(
    store: Store, tmp_path: Path
) -> None:
    """Minor 9: the bundle reads the runner from here, not from the deploy of the day."""
    ctx, artifacts, storage = await _gpu_target(store, tmp_path)
    storage.objects[(RUN, "results/1/results.json")] = _results(1)
    backend = ScriptedBackend({1: [JobState.SUCCEEDED]})
    result = await _auto_step(artifacts, storage, FakeClock()).run(_with(ctx, backend))
    assert result.output_ref is not None
    assert artifacts.get_json(result.output_ref)["runner_image"] == DIGEST.format("p2p-runner")


async def test_a_manifest_kept_before_a_runner_change_names_the_runner_that_runs(
    store: Store, tmp_path: Path
) -> None:
    """A run resumed after a deploy with a new runner, before its job was created: the job
    runs the new image, so its manifest (and the bundle's replay) must name it."""
    ctx, artifacts, storage = await _setup(store, tmp_path)
    step = _step(artifacts, storage, FakeClock())
    plan = Plan.model_validate(artifacts.get_json(PLAN_KEY)["plan"])
    old = await step._manifest(ctx, plan, 1, step._where(ExecTarget.CPU))
    assert old is not None
    stale = old.model_copy(update={"image_digest": "sha256:" + "9" * 64})
    artifacts.put_json(
        f"runs/{RUN}/exec_manifest_1.json", stale.model_dump(mode="json", by_alias=True)
    )
    storage.objects[(RUN, "results/1/results.json")] = _results(1)
    backend = ScriptedBackend({1: [JobState.SUCCEEDED]})
    await step.run(_with(ctx, backend))
    assert _manifest_of(backend.specs[0]).image_digest == "sha256:" + "c" * 64
    kept = artifacts.get_json(f"runs/{RUN}/exec_manifest_1.json")
    assert kept["image_digest"] == "sha256:" + "c" * 64

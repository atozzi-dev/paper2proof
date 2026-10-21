"""Reliability drills (A-32, architecture §15): each failure mode against the real orchestrator,
steps and API, on the fakes (FakeJobBackend, FakeModelClient, respx). Nothing here talks to a
real service.

| Failure                          | Expected behaviour                                         |
|----------------------------------|------------------------------------------------------------|
| 429 storm on Token Factory       | header-aware retries, then the role's fallback             |
| Sandboxes down                   | the smoke executor switches to CPU smoke jobs              |
| no GPU capacity                  | 3 retries 2 min apart, gpu-l40s-d, then INCONCLUSIVE/CAPACITY |
| Tavily (and HF) down             | degraded discovery (DISCOVERY_DEGRADED), the run goes on   |
| daily cloud cap reached          | kill switch: new runs 402, GPU jobs refused, gallery works |
| control plane killed mid-run     | resumed from the DB, the GPU job re-attached by name       |
"""

import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from app.api.main import AppState, create_app
from app.discovery.hf_papers import HF_API
from app.discovery.tavily import TAVILY_API, TavilyClient
from app.domain.discovery import Discovery
from app.domain.events import ErrorPayload, EventType, JobStatusPayload, WarningPayload
from app.domain.models import (
    ErrorCode,
    ReasonCode,
    RunStatus,
    Scale,
    StepName,
    Verdict,
    VerdictKind,
)
from app.execution.backend import (
    CapacityError,
    JobKind,
    JobSpec,
    JobState,
    QuotaExceeded,
    VolumeMount,
)
from app.execution.fake_backend import FakeJobBackend
from app.execution.naming import bucket_name, job_name
from app.execution.run_storage import FakeRunStorage
from app.execution.sandboxes import SANDBOXES_API, SandboxesSmokeExecutor
from app.execution.smoke import FakeSmokeExecutor, FallbackSmokeExecutor
from app.execution.smoke_factory import FALLBACK_AFTER
from app.ingestion.arxiv import EXPORT_API, ArxivClient, RateLimiter
from app.models.client import Role, StoreCallRecorder, TokenFactoryClient, load_models_config
from app.models.fake import FakeModelClient
from app.models.pricing import Pricing, load_cloud_prices
from app.models.router import resolve_roles
from app.orchestrator.budget import Budget, KillSwitchActive, SpendKind, load_budget_limits
from app.orchestrator.context import RunContext
from app.orchestrator.fsm import Orchestrator, Step, StepResult
from app.orchestrator.steps.analyzing import AnalyzingStep
from app.orchestrator.steps.discovering import DiscoveringStep
from app.orchestrator.steps.executing import CAPACITY_WAIT_S, ExecutingStep
from app.orchestrator.steps.jobs import run_job
from app.orchestrator.steps.smoke_testing import SmokeTestingStep
from app.orchestrator.steps.verdict import VerdictStep
from app.settings import Settings
from app.storage.artifacts import LocalArtifactStore
from app.storage.store import Store
from tests.api.conftest import INVITE, login
from tests.discovery.test_discovery import ARXIV_ID, CODE_ATOM, NO_CODE_ATOM, _routes
from tests.models.conftest import BASE, SUPER, ULTRA, Sleeps, completion, config, models_list
from tests.steps.conftest import CPU_POOL, LLM_FIXTURES, ROOT, RUN, ctx_at
from tests.steps.test_execute_to_verdict import _results as verdict_results
from tests.steps.test_execute_to_verdict import _setup as verdict_setup
from tests.steps.test_executing import (
    SLUG,
    FakeClock,
    ScriptedBackend,
    _config,
    _manifest_of,
    _step,
    _with,
)
from tests.steps.test_executing import _setup as executing_setup
from tests.steps.test_smoke_testing import PASS
from tests.steps.test_smoke_testing import _setup as smoke_setup

LIMITS = load_budget_limits(ROOT / "config" / "budgets.yaml")
PRIMARY, FALLBACK = "gpu-l40s-a", "gpu-l40s-d"
PRIMARY_PRESET, FALLBACK_PRESET = "1gpu-8vcpu-32gb", "1gpu-16vcpu-96gb"
EXEC_1 = f"p2p-exec-{SLUG}-1"
ALWAYS = 10**6  # submissions refused for lack of capacity: "never"


class ProcessKilled(BaseException):
    """The control-plane process dies: not an Exception, so nothing in the app handles it."""


async def _no_sleep(_: float) -> None:
    return None


async def _events(store: Store) -> list[Any]:
    return [e.payload for e in await store.events_after(RUN, 0)]


async def _warnings(store: Store, code: str) -> list[str]:
    return [
        p.message for p in await _events(store) if isinstance(p, WarningPayload) and p.code == code
    ]


def _orchestrator(store: Store, jobs: FakeJobBackend, steps: dict[StepName, Step]) -> Orchestrator:
    budget = Budget(store, LIMITS, env="test")
    return Orchestrator(store, budget, steps, limits=LIMITS, jobs=jobs, cpu_pool=CPU_POOL)


def _executing(
    artifacts: LocalArtifactStore,
    storage: FakeRunStorage,
    clock: FakeClock,
    sleep: Callable[[float], Awaitable[None]],
) -> ExecutingStep:
    """EXECUTING as tests/steps builds it, with its own way of waiting (or of dying)."""
    return ExecutingStep(
        artifacts=artifacts,
        storage=storage,
        config=_config(),
        cloud=load_cloud_prices(ROOT / "config" / "pricing.yaml"),
        poll_s=30,
        clock=clock,
        sleep=sleep,
    )


# --- 1. 429 storm on Token Factory -> the role's fallback (config/models.yaml) -------------------


def _token_factory(store: Store, sleeps: Sleeps) -> TokenFactoryClient:
    cfg = config()
    return TokenFactoryClient(
        cfg,
        resolve_roles(models_list(), cfg),
        Pricing.load(LLM_FIXTURES / "pricing_test.yaml"),
        Budget(store, LIMITS, env="test"),
        StoreCallRecorder(store),
        api_key=SecretStr("test-key-not-real"),
        sleep=sleeps,
    )


def _storm(*storming: str, content: str) -> respx.Route:
    """Every request for a `storming` model gets a 429 with its reset headers."""

    def answer(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        if model in storming:
            return httpx.Response(
                429,
                json={"error": {"message": "rate limit exceeded", "type": "rate_limit"}},
                headers={"retry-after": "2", "x-ratelimit-remaining-tokens": "0"},
            )
        return completion(content, model=model)

    return respx.post(f"{BASE}chat/completions").mock(side_effect=answer)


async def _judge_verdict(
    store: Store, tmp_path: Path, models: TokenFactoryClient
) -> tuple[Verdict | None, str]:
    """ANALYZING then VERDICT on a reduced-scale run that trails its baseline."""
    raw = verdict_results([0.90, 0.91, 0.90], [0.92, 0.93, 0.92])
    ctx, artifacts, storage = await verdict_setup(store, tmp_path, raw, StepName.ANALYZING)
    analyzed = await AnalyzingStep(artifacts, storage).run(ctx)
    assert analyzed.output_ref is not None
    await store.transition(
        RUN, StepName.ANALYZING, 1, "SUCCEEDED", output_ref=analyzed.output_ref,
        next_step=(StepName.VERDICT, 1),
    )  # fmt: skip
    result = await VerdictStep(models, artifacts).run(
        RunContext(**{**ctx.__dict__, "step": StepName.VERDICT})
    )
    assert result.next is RunStatus.COMPLETED
    explanation = artifacts.get_json(f"runs/{RUN}/verdict.json")["explanation_md"]
    return await store.get_verdict(RUN, "c1"), str(explanation)


def test_the_judge_falls_back_to_the_reader_in_config() -> None:
    fallbacks = load_models_config(ROOT / "config" / "models.yaml").fallbacks
    assert fallbacks[Role.JUDGE] is Role.READER
    assert fallbacks[Role.PLANNER] is Role.READER
    assert fallbacks[Role.CODER] is Role.PLANNER


@respx.mock
async def test_a_429_storm_switches_the_judge_to_its_fallback_role(
    store: Store, tmp_path: Path
) -> None:
    sleeps = Sleeps()
    prose = "Inconclusive: at reduced scale the method trailed its baseline on every seed."
    route = _storm(ULTRA, content=prose)
    verdict, explanation = await _judge_verdict(store, tmp_path, _token_factory(store, sleeps))

    models = [json.loads(call.request.content)["model"] for call in route.calls]
    assert models == [ULTRA] * 4 + [SUPER]  # max_retries on the judge, then one on the reader
    assert sleeps.calls == [2.0, 2.0, 2.0]  # retry-after honoured between the judge's attempts
    calls = await store.list_model_calls(RUN)
    assert [(c.step, c.role, c.model_id) for c in calls] == [("VERDICT", "reader", SUPER)]
    assert verdict is not None and verdict.reason is ReasonCode.REDUCED_SCALE_CONTRADICTS
    assert explanation.startswith(prose)  # the fallback wrote the prose, decide() the verdict


@respx.mock
async def test_a_429_storm_on_the_fallback_too_keeps_the_verdict(
    store: Store, tmp_path: Path
) -> None:
    route = _storm(ULTRA, SUPER, content="unused")
    verdict, explanation = await _judge_verdict(store, tmp_path, _token_factory(store, Sleeps()))

    assert route.call_count == 8  # 4 on the judge, 4 on the reader: then MODEL_UNAVAILABLE
    assert verdict is not None
    assert (verdict.kind, verdict.reason) == (
        VerdictKind.INCONCLUSIVE,
        ReasonCode.REDUCED_SCALE_CONTRADICTS,
    )
    assert explanation.startswith("Inconclusive (reduced scale contradicts)")  # the template
    # 429s are refused before any work: the storm costs nothing (P1-11)
    assert await store.ledger_total(SpendKind.INFERENCE_USD.value, run_id=RUN) == 0


# --- 2. Sandboxes down -> CPU smoke jobs ---------------------------------------------------------


@respx.mock
async def test_sandboxes_down_switches_the_smoke_runs_to_cpu_jobs(
    store: Store, tmp_path: Path
) -> None:
    ctx, artifacts = await smoke_setup(store, tmp_path)
    down = respx.route(url__startswith=SANDBOXES_API).mock(
        side_effect=httpx.ConnectError("Sandboxes unreachable")
    )
    sandboxes = SandboxesSmokeExecutor(
        api_key=SecretStr("test-key-not-real"),
        project_id="project-e00test",
        base_image="img-runner-cpu-0001",
        budget=ctx.budget,
        usd_per_hour=0.1,
        sleep=_no_sleep,
    )
    jobs = FakeSmokeExecutor([PASS])  # stands in for JobSmokeExecutor (CPU smoke jobs)
    smoke = FallbackSmokeExecutor(sandboxes, jobs, threshold=FALLBACK_AFTER)
    models = FakeModelClient(LLM_FIXTURES, case="sandboxes_down")
    step = SmokeTestingStep(models, artifacts, FakeRunStorage(), lambda c, a: smoke)

    result = await step.run(ctx)

    assert result.next is StepName.EXECUTING and result.output_ref is not None
    assert down.call_count == FALLBACK_AFTER  # three Sandboxes errors in a row...
    assert smoke.active is jobs and len(jobs.runs) == 1  # ...then the CPU job passed
    _, messages = models.requests[-1]
    tool_results = [str(m.content) for m in messages if m.role == "tool"]
    assert sum("smoke executor is unavailable" in t for t in tool_results) == FALLBACK_AFTER
    assert artifacts.get_json(result.output_ref)["entrypoint"] == "workspace/train.py"


# --- 3. no GPU capacity -> gpu-l40s-d -> INCONCLUSIVE/CAPACITY -----------------------------------


class CapacityBackend(ScriptedBackend):
    """ScriptedBackend whose platforms refuse new jobs for lack of capacity."""

    def __init__(self, scripts: dict[int, list[JobState]], refuse: dict[str, int], **kw: Any):
        super().__init__(scripts, **kw)
        self.no_capacity = dict(refuse)


async def test_no_capacity_on_l40s_a_retries_then_runs_on_l40s_d(
    store: Store, tmp_path: Path
) -> None:
    ctx, artifacts, storage = await executing_setup(store, tmp_path)
    clock = FakeClock()

    def finish(name: str, n: int) -> None:
        storage.objects[(RUN, "results/1/results.json")] = b"{}"

    backend = CapacityBackend({1: [JobState.SUCCEEDED]}, {PRIMARY: ALWAYS}, on_status=finish)
    result = await _step(artifacts, storage, clock).run(_with(ctx, backend))

    tried = [(s.name, s.platform, s.preset) for s in backend.specs]
    assert tried == [(EXEC_1, PRIMARY, "1gpu-8vcpu-32gb")] * 4 + [
        (EXEC_1, FALLBACK, "1gpu-16vcpu-96gb")
    ]  # same deterministic name: never two jobs for one attempt
    assert backend.created == 1
    assert clock.t >= 3 * CAPACITY_WAIT_S  # 2 minutes between the retries on gpu-l40s-a
    assert len(await _warnings(store, "NO_GPU_CAPACITY")) == 4
    assert result.output_ref is not None
    stored = artifacts.get_json(result.output_ref)
    assert stored["results_key"] == "results/1/results.json"
    assert (stored["hardware"]["platform"], stored["hardware"]["preset"]) == (
        FALLBACK,
        "1gpu-16vcpu-96gb",
    )
    assert artifacts.get_json(f"runs/{RUN}/exec_platform_1.json")["platform"] == FALLBACK
    job = await store.get_job(EXEC_1)  # the reaper charges it at the fallback's rate
    assert job is not None and (job.platform, job.preset) == (FALLBACK, FALLBACK_PRESET)


async def test_capacity_that_comes_back_keeps_the_primary_platform(
    store: Store, tmp_path: Path
) -> None:
    ctx, artifacts, storage = await executing_setup(store, tmp_path)
    clock = FakeClock()
    backend = CapacityBackend({1: [JobState.SUCCEEDED]}, {PRIMARY: 2})
    result = await _step(artifacts, storage, clock).run(_with(ctx, backend))
    assert [s.platform for s in backend.specs] == [PRIMARY] * 3
    assert clock.t == 2 * CAPACITY_WAIT_S
    assert result.output_ref is not None
    stored = artifacts.get_json(result.output_ref)
    assert (stored["hardware"]["platform"], stored["hardware"]["preset"]) == (
        PRIMARY,
        PRIMARY_PRESET,
    )


async def test_a_job_on_the_primary_platform_records_gpu_l40s_a(
    store: Store, tmp_path: Path
) -> None:
    """The run conditions shown with the verdict name the platform the job really ran on."""
    ctx, artifacts, storage = await executing_setup(store, tmp_path)

    def finish(name: str, n: int) -> None:
        storage.objects[(RUN, "results/1/results.json")] = b"{}"

    backend = CapacityBackend({1: [JobState.SUCCEEDED]}, {}, on_status=finish)
    result = await _step(artifacts, storage, FakeClock()).run(_with(ctx, backend))
    assert [(s.platform, s.preset) for s in backend.specs] == [(PRIMARY, PRIMARY_PRESET)]
    assert result.output_ref == f"runs/{RUN}/execution_1.json"
    stored = artifacts.get_json(result.output_ref)
    assert (stored["hardware"]["platform"], stored["hardware"]["preset"]) == (
        PRIMARY,
        PRIMARY_PRESET,
    )


async def test_refused_creations_cost_nothing(store: Store, tmp_path: Path) -> None:
    ctx, artifacts, storage = await executing_setup(store, tmp_path)
    backend = CapacityBackend({1: [JobState.SUCCEEDED]}, {PRIMARY: ALWAYS, FALLBACK: ALWAYS})
    await _step(artifacts, storage, FakeClock()).run(_with(ctx, backend))
    assert await ctx.budget.run_spend(RUN, SpendKind.GPU_MINUTES) == 0
    assert await ctx.budget.run_spend(RUN, SpendKind.CLOUD_USD) == 0
    assert await store.list_jobs(RUN) == []


async def test_no_capacity_anywhere_ends_inconclusive_capacity(
    store: Store, tmp_path: Path
) -> None:
    await executing_setup(store, tmp_path)
    artifacts, storage, clock = LocalArtifactStore(tmp_path), FakeRunStorage(), FakeClock()
    backend = CapacityBackend({1: [JobState.SUCCEEDED]}, {PRIMARY: ALWAYS, FALLBACK: ALWAYS})
    steps: dict[StepName, Step] = {
        StepName.EXECUTING: _step(artifacts, storage, clock),
        StepName.ANALYZING: AnalyzingStep(artifacts, storage),
        StepName.VERDICT: VerdictStep(FakeModelClient(LLM_FIXTURES, case="honest"), artifacts),
    }
    [task] = await _orchestrator(store, backend, steps).resume_all()

    assert await task is RunStatus.COMPLETED
    verdict = await store.get_verdict(RUN, "c1")
    assert verdict is not None
    assert (verdict.kind, verdict.reason, verdict.n_seeds) == (
        VerdictKind.INCONCLUSIVE,
        ReasonCode.CAPACITY,
        0,
    )
    assert backend.created == 0
    stored = artifacts.get_json(f"runs/{RUN}/execution_1.json")
    assert (stored["results_key"], stored["exit_reason"]) == (None, "capacity")
    explanation = artifacts.get_json(f"runs/{RUN}/verdict.json")["explanation_md"]
    assert explanation.startswith("Inconclusive (capacity)")
    assert "no GPU capacity" in explanation  # the Limitations say why


async def test_a_gpu_job_that_never_leaves_the_queue_ends_inconclusive_capacity(
    store: Store, tmp_path: Path
) -> None:
    """S4: the platform accepts the job and waits for a GPU with no error. Past the queue cap
    it is cancelled (its name is taken: no other placement) and nothing is charged."""
    ctx, _, _ = await executing_setup(store, tmp_path)
    artifacts, storage, clock = LocalArtifactStore(tmp_path), FakeRunStorage(), FakeClock()
    backend = CapacityBackend({1: [JobState.PENDING]}, {})
    steps: dict[StepName, Step] = {
        StepName.EXECUTING: _step(artifacts, storage, clock),
        StepName.ANALYZING: AnalyzingStep(artifacts, storage),
        StepName.VERDICT: VerdictStep(FakeModelClient(LLM_FIXTURES, case="honest"), artifacts),
    }
    [task] = await _orchestrator(store, backend, steps).resume_all()

    assert await task is RunStatus.COMPLETED
    verdict = await store.get_verdict(RUN, "c1")
    assert verdict is not None
    assert (verdict.kind, verdict.reason) == (VerdictKind.INCONCLUSIVE, ReasonCode.CAPACITY)
    assert [s.name for s in backend.specs] == [EXEC_1] and backend.cancelled == [EXEC_1]
    assert await ctx.budget.run_spend(RUN, SpendKind.GPU_MINUTES) == 0
    [warning] = await _warnings(store, "NO_GPU_CAPACITY")
    assert "20 minutes" in warning


async def test_a_run_cancelled_while_waiting_for_capacity_stops(
    store: Store, tmp_path: Path
) -> None:
    ctx, artifacts, storage = await executing_setup(store, tmp_path)
    clock = FakeClock()

    async def cancel_during_wait(seconds: float) -> None:
        await clock.sleep(seconds)
        await store.cancel_run(RUN)

    step = _executing(artifacts, storage, clock, cancel_during_wait)
    backend = CapacityBackend({1: [JobState.SUCCEEDED]}, {PRIMARY: ALWAYS})
    result = await step.run(_with(ctx, backend))
    assert result.next is RunStatus.CANCELLED
    assert len(backend.specs) == 1  # no further tries after the cancellation


async def test_no_capacity_for_the_on_demand_retry_keeps_the_first_attempt(
    store: Store, tmp_path: Path
) -> None:
    ctx, artifacts, storage = await executing_setup(store, tmp_path)
    backend = CapacityBackend({1: [JobState.RUNNING, JobState.FAILED]}, {})

    def preempted(name: str, n: int) -> None:
        if n == 1:  # the preempted job is gone: from now on there is no GPU anywhere
            backend.no_capacity = {PRIMARY: ALWAYS, FALLBACK: ALWAYS}

    backend.on_status = preempted
    result = await _step(artifacts, storage, FakeClock()).run(_with(ctx, backend))
    assert result.output_ref is not None
    stored = artifacts.get_json(result.output_ref)
    assert stored["manifest"]["attempt"] == 1
    assert (stored["results_key"], stored["exit_reason"]) == (None, "budget_timeout")
    assert (stored["hardware"]["platform"], stored["hardware"]["preset"]) == (
        PRIMARY,
        PRIMARY_PRESET,
    )  # attempt 1's
    assert backend.created == 1


async def test_a_preempted_fallback_job_without_a_retry_records_the_fallback(
    store: Store, tmp_path: Path
) -> None:
    ctx, artifacts, storage = await executing_setup(store, tmp_path)
    backend = CapacityBackend({1: [JobState.RUNNING, JobState.FAILED]}, {PRIMARY: ALWAYS})

    def preempted(name: str, n: int) -> None:
        if n == 1:
            backend.no_capacity = {PRIMARY: ALWAYS, FALLBACK: ALWAYS}

    backend.on_status = preempted
    result = await _step(artifacts, storage, FakeClock()).run(_with(ctx, backend))
    assert result.output_ref is not None
    stored = artifacts.get_json(result.output_ref)
    assert stored["manifest"]["attempt"] == 1
    assert (stored["hardware"]["platform"], stored["hardware"]["preset"]) == (
        FALLBACK,
        FALLBACK_PRESET,
    )


async def test_the_kill_switch_during_a_capacity_wait_refuses_the_next_try(
    store: Store, tmp_path: Path
) -> None:
    ctx, artifacts, storage = await executing_setup(store, tmp_path)
    clock = FakeClock()

    async def cap_reached_during_wait(seconds: float) -> None:
        await clock.sleep(seconds)
        await ctx.budget.set_kill_switch(True)

    step = _executing(artifacts, storage, clock, cap_reached_during_wait)
    backend = CapacityBackend({1: [JobState.SUCCEEDED]}, {PRIMARY: ALWAYS})
    with pytest.raises(KillSwitchActive):
        await step.run(_with(ctx, backend))
    assert backend.submit_calls == 1  # the refused first try: the next one never submitted
    assert backend.created == 0 and backend.jobs == {}
    assert await store.list_jobs(RUN) == []
    assert await ctx.budget.run_spend(RUN, SpendKind.CLOUD_USD) == 0


async def test_a_resumed_step_reattaches_to_its_job_on_the_fallback_platform(
    store: Store, tmp_path: Path
) -> None:
    ctx, artifacts, storage = await executing_setup(store, tmp_path)
    backend = CapacityBackend({1: [JobState.RUNNING, JobState.SUCCEEDED]}, {PRIMARY: ALWAYS})
    clock = FakeClock()

    async def die_while_watching(seconds: float) -> None:
        if seconds != CAPACITY_WAIT_S:
            raise ProcessKilled
        await clock.sleep(seconds)

    dying = _executing(artifacts, storage, clock, die_while_watching)
    with pytest.raises(ProcessKilled):
        await dying.run(_with(ctx, backend))
    tries = len(backend.specs)

    restarted = RunContext(**{**ctx.__dict__, "budget": Budget(store, LIMITS, env="test")})
    await _step(artifacts, storage, FakeClock()).run(_with(restarted, backend))  # new process
    assert backend.created == 1
    assert len(backend.specs) == tries  # straight back to its job on gpu-l40s-d
    assert backend.jobs[EXEC_1][3].platform == FALLBACK
    stored = artifacts.get_json(f"runs/{RUN}/execution_1.json")
    assert stored["hardware"]["platform"] == FALLBACK


@dataclass
class _ProvisionLike:
    """A CPU job step (as PROVISIONING runs it) on a platform with no capacity."""

    name: StepName = StepName.PROVISIONING

    async def run(self, ctx: RunContext) -> StepResult:
        spec = JobSpec(
            name=job_name(JobKind.PROVISION, ctx.run_id, ctx.attempt),
            kind=JobKind.PROVISION,
            image_digest_ref="cr.eu-north1.nebius.cloud/reg/p2p-provisioner@sha256:" + "a" * 64,
            platform="cpu-d3",
            preset="4vcpu-16gb",
            disk_gib=50,
            subnet_id="vpcsubnet-provision",
            volumes=[VolumeMount(source=bucket_name(ctx.run_id), mount_path="/mnt/run", mode="rw")],
            inject_files={},
            env={},
            preemptible=False,
            watchdog_s=1200,
        )
        await run_job(ctx, spec, usd_per_hour=0.1, poll_s=1, sleep=_no_sleep)
        return StepResult(next=StepName.SMOKE_TESTING)


async def test_no_capacity_for_a_cpu_job_fails_the_run_with_capacity(store: Store) -> None:
    await ctx_at(store, StepName.PROVISIONING, previous=[(StepName.PLANNING, "p")])
    backend = FakeJobBackend()
    backend.no_capacity = {"cpu-d3": ALWAYS}
    [task] = await _orchestrator(
        store, backend, {StepName.PROVISIONING: _ProvisionLike()}
    ).resume_all()
    assert await task is RunStatus.FAILED
    run = await store.get_run(RUN)
    assert run is not None and run.error_code is ErrorCode.CAPACITY


async def test_a_used_up_project_quota_fails_the_run_at_once_and_names_it(
    store: Store,
) -> None:
    """S5/S6: compute.instance.non-gpu.vcpu was 0. Waiting never helps: an operator must raise
    the quota, so the run says which one instead of "no capacity, try again later"."""
    await ctx_at(store, StepName.PROVISIONING, previous=[(StepName.PLANNING, "p")])
    backend = FakeJobBackend()
    backend.quota_exceeded = ["compute.instance.non-gpu.vcpu"]
    [task] = await _orchestrator(
        store, backend, {StepName.PROVISIONING: _ProvisionLike()}
    ).resume_all()
    assert await task is RunStatus.FAILED
    run = await store.get_run(RUN)
    assert run is not None and run.error_code is ErrorCode.CAPACITY
    [error] = [e.payload for e in await store.events_after(RUN, 0) if e.type is EventType.ERROR]
    assert isinstance(error, ErrorPayload)
    assert "compute.instance.non-gpu.vcpu" in error.message and "quota" in error.message
    assert "try again later" not in error.message
    assert backend.created == 0


async def test_a_quota_refusal_of_the_gpu_job_is_never_retried(
    store: Store, tmp_path: Path
) -> None:
    ctx, artifacts, storage = await executing_setup(store, tmp_path)
    clock = FakeClock()
    backend = CapacityBackend({1: [JobState.SUCCEEDED]}, {})
    backend.quota_exceeded = ["compute.disk.count"]
    with pytest.raises(QuotaExceeded):
        await _step(artifacts, storage, clock).run(_with(ctx, backend))
    assert backend.submit_calls == 1 and clock.t < CAPACITY_WAIT_S  # no 2-minute retries
    assert await _warnings(store, "NO_GPU_CAPACITY") == []


async def test_the_fake_backend_raises_capacity_errors_before_creating() -> None:
    backend = FakeJobBackend()
    backend.no_capacity = {PRIMARY: 1}
    gpu = JobSpec(
        name=f"p2p-exec-{SLUG}-1", kind=JobKind.EXEC,
        image_digest_ref="cr.eu-north1.nebius.cloud/reg/p2p-runner@sha256:" + "b" * 64,
        platform=PRIMARY, preset="1gpu-8vcpu-32gb", disk_gib=100, subnet_id="vpcsubnet-exec",
        volumes=[], inject_files={}, env={}, preemptible=True, watchdog_s=900,
    )  # fmt: skip
    with pytest.raises(CapacityError):
        await backend.submit(gpu)
    assert backend.jobs == {} and backend.created == 0
    await backend.submit(gpu)  # capacity came back
    assert backend.created == 1


# --- 4. Tavily down -> degraded discovery ------------------------------------------------------


async def _discovering(
    store: Store, tmp_path: Path
) -> tuple[RunContext, DiscoveringStep, LocalArtifactStore]:
    artifacts = LocalArtifactStore(tmp_path)
    ctx = await ctx_at(
        store,
        StepName.DISCOVERING,
        previous=[(StepName.INGESTING, "x"), (StepName.EXTRACTING_CLAIMS, "y")],
        arxiv_id=ARXIV_ID,
    )
    step = DiscoveringStep(
        store=store,
        arxiv=ArxivClient(limiter=RateLimiter(0.0, sleep=_no_sleep)),
        tavily=TavilyClient(SecretStr("test-key-not-real")),
        models=FakeModelClient(LLM_FIXTURES),
        artifacts=artifacts,
    )
    return ctx, step, artifacts


async def _discover_once(
    ctx: RunContext, step: DiscoveringStep, artifacts: LocalArtifactStore
) -> Discovery:
    result = await step.run(ctx)
    assert result.next is StepName.PLANNING and result.output_ref is not None
    return Discovery.model_validate(artifacts.get_json(result.output_ref))


@respx.mock
async def test_tavily_down_degrades_discovery_to_hf_and_arxiv(store: Store, tmp_path: Path) -> None:
    _routes(hf_summary="No link here.", atom=NO_CODE_ATOM, datasets=["uoft-cs/cifar10"])
    tavily = respx.route(url__startswith=TAVILY_API).mock(
        side_effect=httpx.ConnectTimeout("Tavily unreachable")
    )
    discovery = await _discover_once(*await _discovering(store, tmp_path))

    assert discovery.repo is None
    assert [d.id for d in discovery.datasets] == ["uoft-cs/cifar10"]  # HF still answered
    assert any(n.startswith("DISCOVERY_DEGRADED") for n in discovery.notes)
    assert await _warnings(store, "DISCOVERY_DEGRADED")
    assert tavily.call_count == 1  # degrades at the first failure: no retry storm
    assert await store.ledger_total(SpendKind.TAVILY_CREDITS.value, run_id=RUN) == 0


@respx.mock
async def test_tavily_and_hf_down_still_find_the_authors_link(store: Store, tmp_path: Path) -> None:
    respx.route(url__startswith=HF_API).mock(side_effect=httpx.ConnectError("HF unreachable"))
    respx.route(url__startswith=TAVILY_API).mock(side_effect=httpx.ConnectError("down"))
    respx.get(EXPORT_API).mock(return_value=httpx.Response(200, content=CODE_ATOM))
    discovery = await _discover_once(*await _discovering(store, tmp_path))

    assert discovery.repo is not None and discovery.repo.official
    assert discovery.repo.url == "https://github.com/microsoft/LoRA"
    assert discovery.datasets == []
    assert any("Hugging Face" in m for m in await _warnings(store, "DISCOVERY_DEGRADED"))


@respx.mock
async def test_an_hf_outage_is_not_cached(store: Store, tmp_path: Path) -> None:
    ctx, step, artifacts = await _discovering(store, tmp_path)
    respx.route(url__startswith=TAVILY_API).mock(side_effect=httpx.ConnectError("down"))
    respx.get(EXPORT_API).mock(return_value=httpx.Response(200, content=NO_CODE_ATOM))
    hf_up = False

    def hugging_face(request: httpx.Request) -> httpx.Response:
        if not hf_up:
            return httpx.Response(503)
        if request.url.path.endswith("/repos"):
            datasets = [{"id": "uoft-cs/cifar10"}]
            return httpx.Response(200, json={"datasets": datasets, "models": [], "spaces": []})
        return httpx.Response(200, json={"id": ARXIV_ID, "summary": "No link here."})

    respx.route(url__startswith=HF_API).mock(side_effect=hugging_face)
    assert (await _discover_once(ctx, step, artifacts)).datasets == []
    assert any("HTTP 503" in m for m in await _warnings(store, "DISCOVERY_DEGRADED"))

    hf_up = True  # HF is back: the outage was not cached for 24 hours
    again = await _discover_once(ctx, step, artifacts)
    assert [d.id for d in again.datasets] == ["uoft-cs/cifar10"]


# --- 5. kill switch -> 402 for new runs, GPU jobs refused, the gallery still works ---------------


@pytest.fixture
async def app_state(store: Store) -> AppState:
    settings = Settings(
        p2p_env="test", session_key=SecretStr("s" * 64), admin_token=SecretStr("a" * 64)
    )
    await store.create_invite(hashlib.sha256(INVITE.encode()).hexdigest(), 3, None)

    async def submit(run_id: str) -> None:
        return None

    return AppState(
        settings=settings,
        store=store,
        budget=Budget(store, LIMITS, env="test"),
        limits=LIMITS,
        submit=submit,
        sse_poll_s=0.01,
    )


@pytest.fixture
async def browsers(
    app_state: AppState,
) -> AsyncIterator[tuple[httpx.AsyncClient, httpx.AsyncClient]]:
    """A logged-in user and an anonymous visitor on the same app."""
    app = create_app(app_state)

    def client() -> httpx.AsyncClient:
        transport = httpx.ASGITransport(app=app, client=("203.0.113.7", 4321))
        return httpx.AsyncClient(transport=transport, base_url="https://testserver")

    async with client() as user, client() as visitor:
        await login(user)
        yield user, visitor


async def _curated_run(store: Store, run_id: str) -> None:
    await store.create_run(run_id, "2106.09685", created_by=None, claim_hint=None, budget={})
    await store.set_run_status(run_id, RunStatus.COMPLETED)
    verdict = Verdict(
        kind=VerdictKind.REPRODUCED, reason=None, n_seeds=3, mean=0.02, std=0.001,
        ci_low=0.018, ci_high=0.022, tolerance={"rho": 0.5, "delta": 0.017},
    )  # fmt: skip
    await store.save_verdict(run_id, "c1", verdict, Scale.FULL, "0" * 64, "Reproduced.")
    await store.set_listed(run_id, True)


async def test_the_daily_cloud_cap_trips_the_kill_switch_and_new_runs_get_402(
    store: Store, app_state: AppState, browsers: tuple[httpx.AsyncClient, httpx.AsyncClient]
) -> None:
    user, visitor = browsers
    listed = "r_01jb7z00000000000000000077"
    await _curated_run(store, listed)
    cap = LIMITS.global_.cloud_usd_per_day
    spend = await app_state.budget.check_and_reserve(None, SpendKind.CLOUD_USD, cap)
    await app_state.budget.commit(spend, cap)  # a day's cloud spend reaches the cap
    assert await app_state.budget.kill_switch_active()

    refused = await user.post("/api/runs", json={"arxiv_id": "2410.01234"})
    assert refused.status_code == 402
    assert refused.json() == {"error": "daily budget reached: new runs are paused"}

    gallery = await visitor.get("/api/gallery")
    assert gallery.status_code == 200 and [c["id"] for c in gallery.json()] == [listed]
    assert (await visitor.get("/api/stats")).json() == {"runs_by_verdict": {"REPRODUCED": 1}}
    shown = await visitor.get(f"/api/runs/{listed}")
    assert shown.status_code == 200 and shown.json()["verdicts"][0]["verdict"]["kind"] == (
        "REPRODUCED"
    )
    assert (await visitor.get(f"/api/runs/{listed}/events.json")).status_code == 200
    assert (await visitor.get("/healthz")).status_code == 200

    await app_state.budget.set_kill_switch(False)  # the admin lifts it (or the day ends)
    assert (await user.post("/api/runs", json={"arxiv_id": "2410.01234"})).status_code == 202


async def test_a_402_does_not_use_up_the_invite_quota(
    store: Store, app_state: AppState, browsers: tuple[httpx.AsyncClient, httpx.AsyncClient]
) -> None:
    user, _ = browsers
    await app_state.budget.set_kill_switch(True)
    for _ in range(5):
        assert (await user.post("/api/runs", json={"arxiv_id": "2410.01234"})).status_code == 402
    await app_state.budget.set_kill_switch(False)
    created = [await user.post("/api/runs", json={"arxiv_id": "2410.01234"}) for _ in range(3)]
    assert [r.status_code for r in created] == [202, 202, 202]  # the invite's 3 a day


async def test_the_kill_switch_refuses_the_gpu_job_of_a_run_in_flight(
    store: Store, tmp_path: Path
) -> None:
    ctx, artifacts, storage = await executing_setup(store, tmp_path)
    await ctx.budget.set_kill_switch(True)
    backend = FakeJobBackend()
    [task] = await _orchestrator(
        store, backend, {StepName.EXECUTING: _step(artifacts, storage, FakeClock())}
    ).resume_all()
    assert await task is RunStatus.FAILED
    run = await store.get_run(RUN)
    assert run is not None and run.error_code is ErrorCode.KILL_SWITCH
    assert backend.submit_calls == 0  # refused before anything was created


# --- 6. the control plane dies mid-EXECUTING -> resumed from the DB, no duplicate job ------------


def _results_for(spec: JobSpec, values: list[float], baseline: list[float]) -> bytes:
    """A results.json that honours the manifest the job received."""
    m = _manifest_of(spec)
    return json.dumps(
        {
            "schema": "p2p.results/v1",
            "run_id": m.run_id,
            "attempt": m.attempt,
            "claim_ref": m.claim_ref,
            "claim_type": m.claim_type.value,
            "metric": m.metric,
            "higher_is_better": m.higher_is_better,
            "scale": m.scale.model_dump(exclude_none=True),
            "per_seed": [
                {"seed": s, "value": v, "ok": True} for s, v in zip(m.seeds, values, strict=True)
            ],
            "baseline": {
                "name": m.baseline_name,
                "per_seed": [
                    {"seed": s, "value": v} for s, v in zip(m.seeds, baseline, strict=True)
                ],
            },
            "wall_clock_s": 590.0,
            "gpu": "NVIDIA L40S",
            "exit_reason": "completed",
        }
    ).encode()


def _pipeline(
    artifacts: LocalArtifactStore,
    storage: FakeRunStorage,
    sleep: Callable[[float], Awaitable[None]],
) -> dict[StepName, Step]:
    return {
        StepName.EXECUTING: _executing(artifacts, storage, FakeClock(), sleep),
        StepName.ANALYZING: AnalyzingStep(artifacts, storage),
        StepName.VERDICT: VerdictStep(FakeModelClient(LLM_FIXTURES, case="honest"), artifacts),
    }


@pytest.mark.parametrize("dies", ["while watching the job", "between create and record"])
async def test_a_restart_mid_executing_resumes_without_a_second_gpu_job(
    store: Store, tmp_path: Path, dies: str
) -> None:
    await executing_setup(store, tmp_path)
    artifacts, storage = LocalArtifactStore(tmp_path), FakeRunStorage()

    def job_writes_results(name: str, n: int) -> None:
        if n == 1:  # the job finishes while the control plane is down (or just after)
            spec = backend.jobs[name][3]
            raw = _results_for(spec, [0.931, 0.929, 0.930], [0.912, 0.913, 0.911])
            storage.objects[(RUN, "results/1/results.json")] = raw

    backend = ScriptedBackend(
        {1: [JobState.RUNNING, JobState.SUCCEEDED]}, on_status=job_writes_results
    )

    async def killed(_: float) -> None:
        raise ProcessKilled

    if dies == "between create and record":

        def crash_after_create(spec: JobSpec) -> None:
            raise ProcessKilled

        backend.on_submit = crash_after_create
        first = _pipeline(artifacts, storage, _no_sleep)
    else:
        first = _pipeline(artifacts, storage, killed)

    [task] = await _orchestrator(store, backend, first).resume_all()
    with pytest.raises(ProcessKilled):
        await task
    run = await store.get_run(RUN)
    assert run is not None and run.status is RunStatus.RUNNING  # left mid-flight
    recorded = await store.get_job(EXEC_1)
    assert (recorded is None) == (dies == "between create and record")

    # Restart: a new process over the same DB, bucket and (remote) job backend.
    backend.on_submit = None
    [task] = await _orchestrator(
        store, backend, _pipeline(artifacts, storage, _no_sleep)
    ).resume_all()
    assert await task is RunStatus.COMPLETED

    assert backend.created == 1  # get-by-name: the very same GPU job, never a second one
    assert [s.name for s in backend.specs] == [EXEC_1]
    job = await store.get_job(EXEC_1)
    assert job is not None and job.status == "SUCCEEDED"
    if recorded is not None:
        assert job.created_at == recorded.created_at  # the watchdog keeps its first deadline
    executing = [s for s in await store.get_steps(RUN) if s.name is StepName.EXECUTING]
    assert [(s.attempt, s.status) for s in executing] == [(1, "SUCCEEDED")]
    verdict = await store.get_verdict(RUN, "c1")
    assert verdict is not None and verdict.n_seeds == 3
    states = [p.state for p in await _events(store) if isinstance(p, JobStatusPayload)]
    assert states[-1] == "SUCCEEDED"

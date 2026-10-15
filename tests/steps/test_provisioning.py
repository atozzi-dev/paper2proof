import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.domain.discovery import Discovery, RepoRef
from app.domain.errors import P2PError
from app.domain.events import JobStatusPayload, WarningPayload
from app.domain.models import (
    ClaimType,
    ErrorCode,
    PackageReq,
    Plan,
    PlanMode,
    RunStatus,
    Scale,
    StepName,
)
from app.execution.backend import JobKind, JobSpec, JobState
from app.execution.config import ExecutionConfig
from app.execution.fake_backend import FakeJobBackend
from app.execution.naming import bucket_name
from app.execution.package_policy import PackagePolicy, PyPIClient, load_policy_config
from app.execution.run_storage import FakeRunStorage
from app.models.pricing import load_cloud_prices
from app.orchestrator.budget import SpendKind
from app.orchestrator.context import RunContext
from app.orchestrator.steps.provisioning import ProvisioningStep
from app.storage.artifacts import LocalArtifactStore
from app.storage.store import Store
from tests.steps.conftest import ROOT, RUN, ctx_at

DIGEST = "cr.eu-north1.nebius.cloud/reg/{}@sha256:" + "c" * 64
EXEC_CFG = ExecutionConfig(
    provisioner_image=DIGEST.format("p2p-provisioner"),
    runner_image=DIGEST.format("p2p-runner"),
    runner_cpu_image=DIGEST.format("p2p-runner-cpu"),
    subnet_provision="vpcsubnet-provision",
    subnet_exec="vpcsubnet-exec",
)
PLAN_KEY = f"runs/{RUN}/plan.json"
DISCOVERY_KEY = f"runs/{RUN}/discovery.json"
NOW = datetime(2026, 10, 20, tzinfo=UTC)


def _pypi() -> httpx.Response:
    first = NOW - timedelta(days=2000)
    releases = {
        f"1.{i}": [{"upload_time_iso_8601": (first + timedelta(days=i)).isoformat()}]
        for i in range(20)
    }
    return httpx.Response(200, json={"releases": releases})


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds


async def _setup(
    store: Store,
    tmp_path: Path,
    *,
    packages: list[str],
    smoke_request: list[str] | None = None,
) -> tuple[RunContext, LocalArtifactStore]:
    artifacts = LocalArtifactStore(tmp_path)
    plan = Plan(
        mode=PlanMode.REPO_FIRST,
        claim_ids=["c1"],
        claim_types={"c1": ClaimType.RELATIVE},
        scale=Scale.REDUCED,
        seeds=[0, 1, 2],
        packages=[PackageReq(name=n) for n in packages],
        datasets=[],
        gpu_minutes_est=10,
    )
    artifacts.put_json(PLAN_KEY, {"plan": plan.model_dump(mode="json"), "est_cost_usd": 0.2})
    discovery = Discovery(
        repo=RepoRef(url="https://github.com/org/repo", source="paper", official=True)
    )
    artifacts.put_json(DISCOVERY_KEY, discovery.model_dump(mode="json"))
    previous = [
        (StepName.INGESTING, "i"),
        (StepName.EXTRACTING_CLAIMS, "c"),
        (StepName.DISCOVERING, DISCOVERY_KEY),
        (StepName.PLANNING, PLAN_KEY),
    ]
    if smoke_request is None:
        return await ctx_at(store, StepName.PROVISIONING, previous=previous), artifacts
    request_key = f"runs/{RUN}/package_request_1.json"
    artifacts.put_json(request_key, {"requested_packages": [{"name": n} for n in smoke_request]})
    previous += [(StepName.PROVISIONING, "p1"), (StepName.SMOKE_TESTING, request_key)]
    return await ctx_at(store, StepName.PROVISIONING, previous=previous), artifacts


def _step(
    store: Store, artifacts: LocalArtifactStore, storage: FakeRunStorage, clock: FakeClock
) -> ProvisioningStep:
    policy = PackagePolicy(load_policy_config(ROOT / "config"), PyPIClient(store), now=lambda: NOW)
    return ProvisioningStep(
        policy=policy,
        storage=storage,
        artifacts=artifacts,
        config=EXEC_CFG,
        cloud=load_cloud_prices(ROOT / "config" / "pricing.yaml"),
        poll_s=15,
        clock=clock,
        sleep=clock.sleep,
    )


def _writes_report(storage: FakeRunStorage) -> Any:
    def on_submit(spec: JobSpec) -> None:
        report = {
            "schema": "p2p.provision-report/v1",
            "requirement_files": {"requirements.txt": "torch\n"},
        }
        storage.objects[(RUN, "staging/provision_report.json")] = json.dumps(report).encode()

    return on_submit


@respx.mock
async def test_provision_job_is_submitted_safely_and_succeeds(store: Store, tmp_path: Path) -> None:
    respx.get("https://pypi.org/pypi/timm/json").mock(return_value=_pypi())
    ctx, artifacts = await _setup(store, tmp_path, packages=["timm"])
    storage, clock = FakeRunStorage(), FakeClock()
    jobs = FakeJobBackend(on_submit=_writes_report(storage))
    ctx = RunContext(**{**ctx.__dict__, "jobs": jobs})
    result = await _step(store, artifacts, storage, clock).run(ctx)
    assert result.next is StepName.SMOKE_TESTING
    ((_, _, _, spec),) = jobs.jobs.values()
    assert spec.kind is JobKind.PROVISION
    assert spec.name == f"p2p-prov-{RUN[2:]}-1"
    assert spec.image_digest_ref == EXEC_CFG.provisioner_image
    assert spec.subnet_id == "vpcsubnet-provision"
    assert {(v.source, v.mode) for v in spec.volumes} == {
        (bucket_name(RUN), "rw"),
        ("p2p-cache", "rw"),
    }
    manifest = json.loads(spec.inject_files["/etc/p2p/provision.json"])
    assert manifest["packages"] == ["timm"]
    assert manifest["repo"] == {"url": "https://github.com/org/repo", "ref": None}
    assert manifest["hf_token_secret"] is None
    assert jobs.gateway_running
    assert artifacts.get_json(f"runs/{RUN}/provision_report.json")["requirement_files"]
    assert await ctx.budget.run_spend(RUN, SpendKind.CLOUD_USD) >= 0
    events = [e.payload for e in await store.events_after(RUN, 0)]
    assert any(isinstance(p, JobStatusPayload) and p.state == "SUCCEEDED" for p in events)


@respx.mock
async def test_rejected_plan_packages_are_dropped_with_a_warning(
    store: Store, tmp_path: Path
) -> None:
    respx.get("https://pypi.org/pypi/reqeusts/json").mock(return_value=_pypi())
    ctx, artifacts = await _setup(store, tmp_path, packages=["reqeusts"])
    storage = FakeRunStorage()
    ctx = RunContext(**{**ctx.__dict__, "jobs": FakeJobBackend(on_submit=_writes_report(storage))})
    await _step(store, artifacts, storage, FakeClock()).run(ctx)
    spec = next(iter(ctx.jobs.jobs.values()))[3]  # type: ignore[attr-defined]
    assert json.loads(spec.inject_files["/etc/p2p/provision.json"])["packages"] == []
    events = [e.payload for e in await store.events_after(RUN, 0)]
    assert any(isinstance(p, WarningPayload) and p.code == "PACKAGE_REJECTED" for p in events)


async def test_provisioner_failure_surfaces_its_reason(store: Store, tmp_path: Path) -> None:
    ctx, artifacts = await _setup(store, tmp_path, packages=[])
    storage = FakeRunStorage()

    def fail(spec: JobSpec) -> None:
        storage.objects[(RUN, "staging/provision_error.json")] = json.dumps(
            {"error": "installing packages: a requested package has no wheel"}
        ).encode()

    ctx = RunContext(**{**ctx.__dict__, "jobs": FakeJobBackend(JobState.FAILED, on_submit=fail)})
    with pytest.raises(P2PError) as err:
        await _step(store, artifacts, storage, FakeClock()).run(ctx)
    assert err.value.code is ErrorCode.PROVISION_FAILED
    assert "no wheel" in err.value.message


async def test_watchdog_cancels_a_stuck_job(store: Store, tmp_path: Path) -> None:
    ctx, artifacts = await _setup(store, tmp_path, packages=[])
    jobs = FakeJobBackend(JobState.RUNNING)
    ctx = RunContext(**{**ctx.__dict__, "jobs": jobs})
    clock = FakeClock()
    with pytest.raises(P2PError) as err:
        await _step(store, artifacts, FakeRunStorage(), clock).run(ctx)
    assert err.value.code is ErrorCode.TIMEOUT
    assert jobs.cancelled == [f"p2p-prov-{RUN[2:]}-1"]
    assert clock.t >= 20 * 60


async def test_cancelling_the_run_cancels_the_job(store: Store, tmp_path: Path) -> None:
    ctx, artifacts = await _setup(store, tmp_path, packages=[])
    jobs = FakeJobBackend(JobState.RUNNING)
    ctx = RunContext(**{**ctx.__dict__, "jobs": jobs})
    clock = FakeClock()
    step = _step(store, artifacts, FakeRunStorage(), clock)

    async def sleep_then_cancel(seconds: float) -> None:
        clock.t += seconds
        await store.cancel_run(RUN)

    step._sleep = sleep_then_cancel
    result = await step.run(ctx)
    assert result.next is RunStatus.CANCELLED
    assert jobs.cancelled


@respx.mock
async def test_coder_requested_packages_are_added_when_accepted(
    store: Store, tmp_path: Path
) -> None:
    respx.get("https://pypi.org/pypi/einops/json").mock(return_value=_pypi())
    ctx, artifacts = await _setup(store, tmp_path, packages=[], smoke_request=["einops"])
    storage = FakeRunStorage()
    ctx = RunContext(**{**ctx.__dict__, "jobs": FakeJobBackend(on_submit=_writes_report(storage))})
    await _step(store, artifacts, storage, FakeClock()).run(ctx)
    spec = next(iter(ctx.jobs.jobs.values()))[3]  # type: ignore[attr-defined]
    assert spec.name.endswith("-2")
    assert json.loads(spec.inject_files["/etc/p2p/provision.json"])["packages"] == ["einops"]


@respx.mock
async def test_all_requested_packages_rejected_fails(store: Store, tmp_path: Path) -> None:
    ctx, artifacts = await _setup(store, tmp_path, packages=[], smoke_request=["git+https://x/y"])
    with pytest.raises(P2PError) as err:
        await _step(store, artifacts, FakeRunStorage(), FakeClock()).run(ctx)
    assert err.value.code is ErrorCode.PACKAGE_REJECTED

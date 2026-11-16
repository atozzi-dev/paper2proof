"""The plan a step works on, and how many package requests a run made (A-47)."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.domain.errors import P2PError
from app.domain.models import (
    ClaimType,
    ErrorCode,
    ExecTarget,
    FallbackReason,
    GpuFallback,
    Plan,
    PlanArtifact,
    PlanMode,
    Scale,
    StepName,
)
from app.orchestrator.fallback import fallback_key, package_requests
from app.orchestrator.steps.common import active_plan
from app.storage.artifacts import LocalArtifactStore
from app.storage.store import StepRecord, Store
from tests.steps.conftest import RUN, ctx_at

NOW = datetime(2026, 11, 16, tzinfo=UTC)
PLAN_KEY = f"runs/{RUN}/plan.json"


def _plan(scale: Scale, minutes: float) -> Plan:
    return Plan(
        mode=PlanMode.FROM_SCRATCH,
        claim_ids=["c1"],
        claim_types={"c1": ClaimType.RELATIVE},
        scale=scale,
        seeds=[0, 1, 2],
        gpu_minutes_est=minutes,
    )


GPU_PLAN, CPU_PLAN = _plan(Scale.FULL, 40), _plan(Scale.REDUCED, 28)
FALLBACK = GpuFallback(
    reason=FallbackReason.NO_CAPACITY,
    message="no GPU was free within 10 minutes: the CPU plan runs instead",
    at=NOW,
    gpu_attempt=1,
)


async def _read(store: Store, tmp_path: Path, plan_json: dict[str, Any]) -> Any:
    artifacts = LocalArtifactStore(tmp_path)
    artifacts.put_json(PLAN_KEY, plan_json)
    ctx = await ctx_at(store, StepName.EXECUTING, previous=[(StepName.PLANNING, PLAN_KEY)])

    async def read_json(key: str) -> Any:
        return artifacts.get_json(key)

    return ctx, artifacts, read_json


async def test_a_plan_from_before_the_gpu_target_is_run_on_cpu(
    store: Store, tmp_path: Path
) -> None:
    plan_json = {"plan": CPU_PLAN.model_dump(mode="json"), "est_cost_usd": 0.1}
    ctx, _, read_json = await _read(store, tmp_path, plan_json)
    active = await active_plan(ctx, read_json)
    assert (active.plan, active.target, active.fallback) == (CPU_PLAN, ExecTarget.CPU, None)


async def test_a_gpu_target_runs_its_gpu_plan(store: Store, tmp_path: Path) -> None:
    artifact = PlanArtifact(
        plan=GPU_PLAN,
        est_cost_usd=0.9,
        target=ExecTarget.GPU,
        cpu_plan=CPU_PLAN,
        est_cost_max_usd=1.7,
    )
    ctx, _, read_json = await _read(store, tmp_path, artifact.model_dump(mode="json"))
    active = await active_plan(ctx, read_json)
    assert (active.plan, active.target, active.fallback) == (GPU_PLAN, ExecTarget.GPU, None)


async def test_after_the_fallback_the_cpu_plan_runs(store: Store, tmp_path: Path) -> None:
    artifact = PlanArtifact(
        plan=GPU_PLAN,
        est_cost_usd=0.9,
        target=ExecTarget.GPU,
        cpu_plan=CPU_PLAN,
        est_cost_max_usd=1.7,
    )
    ctx, artifacts, read_json = await _read(store, tmp_path, artifact.model_dump(mode="json"))
    artifacts.put_json(fallback_key(RUN), FALLBACK.model_dump(mode="json"))
    active = await active_plan(ctx, read_json)
    assert (active.plan, active.target, active.fallback) == (CPU_PLAN, ExecTarget.CPU, FALLBACK)


async def test_a_fallback_without_a_cpu_plan_is_an_internal_error(
    store: Store, tmp_path: Path
) -> None:
    artifact = PlanArtifact(
        plan=GPU_PLAN, est_cost_usd=0.9, target=ExecTarget.GPU, est_cost_max_usd=1.7
    )
    ctx, artifacts, read_json = await _read(store, tmp_path, artifact.model_dump(mode="json"))
    artifacts.put_json(fallback_key(RUN), FALLBACK.model_dump(mode="json"))
    with pytest.raises(P2PError) as err:
        await active_plan(ctx, read_json)
    assert err.value.code is ErrorCode.INTERNAL_ERROR


def _step(name: StepName, attempt: int, ref: str | None = None) -> StepRecord:
    return StepRecord(
        name=name,
        attempt=attempt,
        status="SUCCEEDED",
        started_at=NOW,
        finished_at=NOW,
        error_code=None,
        output_ref=ref,
    )


def test_every_return_to_provisioning_but_the_fallback_is_a_package_request() -> None:
    plain = [_step(StepName.PROVISIONING, a) for a in (1, 2, 3)]
    assert package_requests(plain, 3) == 2
    assert package_requests(plain, 1) == 0
    fell_back = [*plain, _step(StepName.EXECUTING, 1, fallback_key(RUN))]
    assert package_requests(fell_back, 3) == 1  # attempt 2 or 3 was the fallback's
    ran = [*plain, _step(StepName.EXECUTING, 1, f"runs/{RUN}/execution_1.json")]
    assert package_requests(ran, 3) == 2

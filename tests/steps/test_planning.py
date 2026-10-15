import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.domain.discovery import Discovery, RepoRef
from app.domain.events import ApprovalRequiredPayload, PlanReadyPayload, WarningPayload
from app.domain.models import (
    Claim,
    ClaimType,
    ErrorCode,
    Plan,
    PlanMode,
    RunStatus,
    Scale,
    StepName,
)
from app.models.fake import FakeModelClient
from app.models.pricing import load_cloud_prices
from app.orchestrator.cost_estimate import estimate_plan_cost
from app.orchestrator.steps.awaiting_approval import AwaitingApprovalStep
from app.orchestrator.steps.planning import PlanningStep
from app.storage.artifacts import LocalArtifactStore
from app.storage.store import Store
from tests.steps.conftest import LLM_FIXTURES, ROOT, RUN, ctx_at

DISCOVERY = f"runs/{RUN}/discovery.json"
CLOUD = load_cloud_prices(ROOT / "config" / "pricing.yaml")


def _claim(cid: str, kind: ClaimType) -> Claim:
    return Claim(
        id=cid,
        text="Ours beats ResNet-18 on CIFAR-10",
        claim_type=kind,
        metric="top1_accuracy",
        higher_is_better=True,
        dataset="CIFAR-10",
        setting="200 epochs",
        reported_value=94.1 if kind is ClaimType.ABSOLUTE else None,
        baseline_name="ResNet-18" if kind is ClaimType.RELATIVE else None,
        source_ref="Table 2",
    )


async def _plan(
    store: Store, tmp_path: Path, case: str, *, preemptible: bool = True
) -> tuple[Plan, StepName | RunStatus, list[object]]:
    artifacts = LocalArtifactStore(tmp_path)
    discovery = Discovery(repo=RepoRef(url="https://github.com/a/b", source="paper", official=True))
    artifacts.put_json(DISCOVERY, discovery.model_dump(mode="json"))
    ctx = await ctx_at(
        store,
        StepName.PLANNING,
        previous=[
            (StepName.INGESTING, "i"),
            (StepName.EXTRACTING_CLAIMS, "c"),
            (StepName.DISCOVERING, DISCOVERY),
        ],
    )
    claims = [_claim("c1", ClaimType.RELATIVE), _claim("c2", ClaimType.ABSOLUTE)]
    await store.save_claims(RUN, claims, selected={"c1", "c2"})
    step = PlanningStep(
        FakeModelClient(LLM_FIXTURES, case=case), artifacts, CLOUD, preemptible=preemptible
    )
    result = await step.run(ctx)
    assert result.output_ref is not None
    plan = Plan.model_validate(artifacts.get_json(result.output_ref)["plan"])
    events = [e.payload for e in await store.events_after(RUN, 0)]
    return plan, result.next, events


async def test_two_seeds_are_raised_to_three_and_noted(store: Store, tmp_path: Path) -> None:
    plan, nxt, events = await _plan(store, tmp_path, "two_seeds")
    assert len(plan.seeds) == 3
    assert any("seeds" in n for n in plan.notes)
    ready = [p for p in events if isinstance(p, PlanReadyPayload)]
    assert ready and not ready[0].needs_approval
    assert nxt is StepName.PROVISIONING  # cheap: auto-approved


async def test_over_budget_plans_are_clamped_and_reduced(store: Store, tmp_path: Path) -> None:
    plan, _, _ = await _plan(store, tmp_path, "over_gpu")
    assert plan.gpu_minutes_est == 30  # config/budgets.yaml per_run.gpu_minutes
    assert plan.scale is Scale.REDUCED
    assert any("GPU" in n for n in plan.notes)


async def test_absolute_only_at_reduced_scale_warns(store: Store, tmp_path: Path) -> None:
    _, _, events = await _plan(store, tmp_path, "absolute_reduced")
    warnings = [p for p in events if isinstance(p, WarningPayload)]
    assert [w.code for w in warnings] == ["ABSOLUTE_AT_REDUCED_SCALE"]


async def test_unselected_claims_are_dropped(store: Store, tmp_path: Path) -> None:
    plan, _, _ = await _plan(store, tmp_path, "foreign_claim")
    assert plan.claim_ids == ["c1"]
    assert set(plan.claim_types) == {"c1"}


async def test_expensive_plans_wait_for_approval(store: Store, tmp_path: Path) -> None:
    _, nxt, events = await _plan(store, tmp_path, "expensive", preemptible=False)
    assert nxt is StepName.AWAITING_APPROVAL
    ready = [p for p in events if isinstance(p, PlanReadyPayload)]
    assert ready[0].needs_approval and ready[0].est_cost_usd > 0.60


def test_cost_estimate_uses_pricing_yaml() -> None:
    plan = Plan(
        mode=PlanMode.FROM_SCRATCH,
        claim_ids=["c1"],
        claim_types={"c1": ClaimType.RELATIVE},
        scale=Scale.REDUCED,
        seeds=[0, 1, 2],
        gpu_minutes_est=60,
    )
    on_demand = estimate_plan_cost(plan, CLOUD, inference_so_far=0.2, preemptible=False)
    spot = estimate_plan_cost(plan, CLOUD, inference_so_far=0.2, preemptible=True)
    gpu_hour = 1.35 + 8 * 0.012 + 32 * 0.0032  # gpu-l40s-a 1gpu-8vcpu-32gb, on demand
    spot_hour = 0.65 + 8 * 0.006 + 32 * 0.0016  # same preset, preemptible
    provision = 20 / 60 * (4 * 0.015 + 16 * 0.0045)  # cpu-d3 4vcpu-16gb for 20 min
    assert on_demand == pytest.approx(0.2 + gpu_hour + provision, abs=1e-4)
    assert spot == pytest.approx(0.2 + spot_hour + provision, abs=1e-4)


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)
        await asyncio.sleep(0)


async def _approval_ctx(store: Store) -> object:
    return await ctx_at(
        store,
        StepName.AWAITING_APPROVAL,
        previous=[(StepName.INGESTING, "i"), (StepName.PLANNING, "p")],
    )


async def test_approval_timeout_cancels(store: Store) -> None:
    ctx = await _approval_ctx(store)
    clock = FakeClock()
    step = AwaitingApprovalStep(
        timeout=timedelta(minutes=30), poll_s=60, clock=clock, sleep=clock.sleep
    )
    result = await step.run(ctx)  # type: ignore[arg-type]
    assert result.next is RunStatus.CANCELLED
    assert result.error_code is ErrorCode.APPROVAL_TIMEOUT
    events = [e.payload for e in await store.events_after(RUN, 0)]
    assert any(isinstance(p, ApprovalRequiredPayload) for p in events)


@pytest.mark.parametrize(
    ("decision", "expected"),
    [("approved", StepName.PROVISIONING), ("rejected", RunStatus.CANCELLED)],
)
async def test_approval_decisions(
    store: Store, decision: str, expected: StepName | RunStatus
) -> None:
    ctx = await _approval_ctx(store)
    assert await store.decide_approval(RUN, decision)
    clock = FakeClock()
    step = AwaitingApprovalStep(
        timeout=timedelta(minutes=30), poll_s=1, clock=clock, sleep=clock.sleep
    )
    result = await step.run(ctx)  # type: ignore[arg-type]
    assert result.next is expected

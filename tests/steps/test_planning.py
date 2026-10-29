import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypeVar

import pytest
from pydantic import BaseModel

from app.discovery.hf_datasets import DatasetProblem, DatasetReport, RunCpu
from app.domain.discovery import Discovery, RepoRef
from app.domain.errors import P2PError
from app.domain.events import ApprovalRequiredPayload, PlanReadyPayload, WarningPayload
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
from app.models.client import Message, Role
from app.models.fake import FakeModelClient
from app.models.pricing import load_cloud_prices
from app.orchestrator.budget import BudgetExceeded
from app.orchestrator.cost_estimate import estimate_plan_cost
from app.orchestrator.steps.awaiting_approval import AwaitingApprovalStep
from app.orchestrator.steps.planning import CPU_HARDWARE, DatasetCheck, PlanningStep, correct_plan
from app.storage.artifacts import LocalArtifactStore
from app.storage.store import Store
from tests.steps.conftest import LLM_FIXTURES, ROOT, RUN, ctx_at

DISCOVERY = f"runs/{RUN}/discovery.json"
T = TypeVar("T", bound=BaseModel)
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
    store: Store,
    tmp_path: Path,
    case: str,
    *,
    preemptible: bool = True,
    dataset_check: DatasetCheck | None = None,
    models: FakeModelClient | None = None,
    hardware: str | None = None,
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
        models or FakeModelClient(LLM_FIXTURES, case=case),
        artifacts,
        CLOUD,
        preemptible=preemptible,
        dataset_check=dataset_check,
        **({"hardware": hardware} if hardware else {}),
    )
    result = await step.run(ctx)
    assert result.output_ref is not None
    plan = Plan.model_validate(artifacts.get_json(result.output_ref)["plan"])
    events = [e.payload for e in await store.events_after(RUN, 0)]
    return plan, result.next, events


def _prompt(models: FakeModelClient) -> str:
    _, messages = models.requests[-1]
    return "\n".join(str(m.content) for m in messages)


async def test_the_planner_plans_for_one_l40s_by_default(store: Store, tmp_path: Path) -> None:
    models = FakeModelClient(LLM_FIXTURES, case="two_seeds")
    await _plan(store, tmp_path, "two_seeds", models=models)
    assert "one L40S GPU (48 GB)" in _prompt(models)


async def test_in_sandboxes_the_planner_plans_for_a_cpu(store: Store, tmp_path: Path) -> None:
    models = FakeModelClient(LLM_FIXTURES, case="two_seeds")
    await _plan(store, tmp_path, "two_seeds", models=models, hardware=CPU_HARDWARE)
    prompt = _prompt(models)
    assert CPU_HARDWARE in prompt and "L40S" not in prompt


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
    sandboxed = estimate_plan_cost(
        plan, CLOUD, inference_so_far=0.2, preemptible=True, exec_on=("sandbox", "4vcpu-4gb")
    )
    sandbox_hour = 4 * 0.015 + 4 * 0.0045  # 4 vCPU, 4 GB (S14), priced like cpu-d3
    assert sandboxed == pytest.approx(0.2 + sandbox_hour + provision, abs=1e-4)


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


def test_plan_datasets_are_validated_and_capped() -> None:
    datasets = [
        DatasetReq(kind="hf", id="../etc"),
        DatasetReq(kind="hf", id="a/b", split="train[:10%]"),
        *(DatasetReq(kind="hf", id=f"org/d{i}", max_bytes=8_000_000_000) for i in range(4)),
    ]
    plan = Plan(
        mode=PlanMode.REPO_FIRST,
        claim_ids=["c1"],
        claim_types={"c1": ClaimType.RELATIVE},
        scale=Scale.REDUCED,
        seeds=[0, 1, 2],
        datasets=datasets,
        gpu_minutes_est=10,
    )
    corrected, notes = correct_plan(plan, selected={"c1": ClaimType.RELATIVE}, gpu_cap=30)
    assert [d.id for d in corrected.datasets] == ["org/d0", "org/d1"]  # a third breaks 20 GiB
    assert any("dataset" in n for n in notes)


def test_a_claim_keeps_the_type_the_paper_gives_it() -> None:
    """The verdict judges a claim by its extracted type: a Planner that tests an ABSOLUTE
    claim as a comparison would make the Coder measure a baseline the paper never names
    (seen in production: "NMU vs a linear layer"), and the verdict would ignore it anyway."""
    plan = Plan(
        mode=PlanMode.FROM_SCRATCH,
        claim_ids=["c1", "c2"],
        claim_types={"c1": ClaimType.RELATIVE, "c2": ClaimType.RELATIVE},
        scale=Scale.REDUCED,
        seeds=[0, 1, 2],
        gpu_minutes_est=10,
    )
    paper = {"c1": ClaimType.ABSOLUTE, "c2": ClaimType.RELATIVE}
    corrected, notes = correct_plan(plan, selected=paper, gpu_cap=30)
    assert corrected.claim_types == paper
    assert notes == ["claim types follow the paper: c1 is tested as ABSOLUTE"]
    unchanged, none = correct_plan(corrected, selected=paper, gpu_cap=30)
    assert unchanged.claim_types == paper and none == []


ON_HF = {"uoft-cs/cifar10", "scikit-learn/iris"}


async def _on_hf(datasets: list[DatasetReq], run_cpu: RunCpu) -> DatasetReport:
    """Stands in for HfDatasets.check: only the ids in ON_HF exist."""
    return DatasetReport(
        kept=[d for d in datasets if d.id in ON_HF],
        dropped=[
            DatasetProblem(id=d.id, split=d.split, reason="not found on Hugging Face (or private)")
            for d in datasets
            if d.id not in ON_HF
        ],
        unchecked=[],
    )


async def test_a_made_up_dataset_gets_the_planner_one_more_try(
    store: Store, tmp_path: Path
) -> None:
    models = FakeModelClient(LLM_FIXTURES, case="made_up_dataset")
    plan, _, events = await _plan(
        store, tmp_path, "made_up_dataset", dataset_check=_on_hf, models=models
    )
    assert [d.id for d in plan.datasets] == ["uoft-cs/cifar10", "scikit-learn/iris"]
    assert [name for _, name, _ in models.calls] == [
        "planner_experiment_plan",
        "planner_dataset_feedback",
    ]
    feedback = models.requests[1][1][-1].content
    assert isinstance(feedback, str)
    assert "mstz/wine_quality" in feedback and "not found on Hugging Face" in feedback
    assert models.requests[1][1][-2].role == "assistant"  # the first plan, as it was proposed
    assert any("mstz/wine_quality" in n for n in plan.notes)
    assert not [p for p in events if isinstance(p, WarningPayload)]  # fixed: nothing to warn


async def test_a_dataset_still_missing_after_the_retry_is_dropped_with_a_warning(
    store: Store, tmp_path: Path
) -> None:
    models = FakeModelClient(LLM_FIXTURES, case="still_made_up")
    plan, _, events = await _plan(
        store, tmp_path, "still_made_up", dataset_check=_on_hf, models=models
    )
    assert plan.datasets == []
    assert len(models.calls) == 2  # one retry, never a loop
    warnings = [p for p in events if isinstance(p, WarningPayload)]
    assert [w.code for w in warnings] == ["DATASET_UNAVAILABLE"]
    assert "mstz/wine_quality_v2" in warnings[0].message
    assert any("mstz/wine_quality_v2" in n and "not found" in n for n in plan.notes)


async def test_datasets_hugging_face_could_not_check_are_kept_and_noted(
    store: Store, tmp_path: Path
) -> None:
    async def down(datasets: list[DatasetReq], run_cpu: RunCpu) -> DatasetReport:
        return DatasetReport(kept=datasets, dropped=[], unchecked=[d.id for d in datasets])

    models = FakeModelClient(LLM_FIXTURES, case="made_up_dataset")
    plan, _, _ = await _plan(store, tmp_path, "made_up_dataset", dataset_check=down, models=models)
    assert [d.id for d in plan.datasets] == ["mstz/wine_quality", "uoft-cs/cifar10"]
    assert len(models.calls) == 1
    assert any("could not be checked" in n for n in plan.notes)


async def test_a_dataset_the_retry_kept_is_noted_as_dropped_not_replaced(
    store: Store, tmp_path: Path
) -> None:
    models = FakeModelClient(LLM_FIXTURES, case="kept_made_up")
    plan, _, _ = await _plan(store, tmp_path, "kept_made_up", dataset_check=_on_hf, models=models)
    about = [n for n in plan.notes if "mstz/wine_quality" in n]
    assert len(about) == 1 and "dropped" in about[0]  # the conditions never claim a fix


class _RetryFails(FakeModelClient):
    """The Planner's second answer is unusable (a P2PError after repair)."""

    async def structured(
        self,
        role: Role,
        messages: list[Message],
        schema: type[T],
        *,
        run_id: str | None = None,
        step: str | None = None,
        prompt: str | None = None,
    ) -> T:
        if prompt == "planner_dataset_feedback":
            raise P2PError(ErrorCode.SCHEMA_ERROR, "unusable plan")
        return await super().structured(
            role, messages, schema, run_id=run_id, step=step, prompt=prompt
        )


async def test_an_unusable_retry_keeps_the_first_plan_without_the_missing_dataset(
    store: Store, tmp_path: Path
) -> None:
    models = _RetryFails(LLM_FIXTURES, case="made_up_dataset")
    plan, _, events = await _plan(
        store, tmp_path, "made_up_dataset", dataset_check=_on_hf, models=models
    )
    assert [d.id for d in plan.datasets] == ["uoft-cs/cifar10"]
    warnings = [p for p in events if isinstance(p, WarningPayload)]
    assert [w.code for w in warnings] == ["DATASET_UNAVAILABLE"]


class _RetryOverBudget(FakeModelClient):
    async def structured(
        self,
        role: Role,
        messages: list[Message],
        schema: type[T],
        *,
        run_id: str | None = None,
        step: str | None = None,
        prompt: str | None = None,
    ) -> T:
        if prompt == "planner_dataset_feedback":
            raise BudgetExceeded("inference_usd")
        return await super().structured(
            role, messages, schema, run_id=run_id, step=step, prompt=prompt
        )


async def test_the_budget_running_out_on_the_retry_fails_the_step(
    store: Store, tmp_path: Path
) -> None:
    models = _RetryOverBudget(LLM_FIXTURES, case="made_up_dataset")
    with pytest.raises(BudgetExceeded):  # never swallowed like an unusable answer
        await _plan(store, tmp_path, "made_up_dataset", dataset_check=_on_hf, models=models)


async def test_planned_claims_take_the_type_the_paper_gives(store: Store, tmp_path: Path) -> None:
    """A plan stored before correct_plan kept types (or any path around it) cannot change the
    type the verdict judges a claim by."""
    from app.orchestrator.steps.common import planned_claims
    from tests.steps.conftest import ctx_at

    ctx = await ctx_at(store, StepName.PLANNING, previous=[(StepName.INGESTING, "i")])
    await store.save_claims(RUN, [_claim("c2", ClaimType.ABSOLUTE)], selected={"c2"})
    plan = Plan(
        mode=PlanMode.FROM_SCRATCH,
        claim_ids=["c2"],
        claim_types={"c2": ClaimType.RELATIVE},
        scale=Scale.REDUCED,
        seeds=[0, 1, 2],
        gpu_minutes_est=10,
    )
    [(claim, kind)] = await planned_claims(ctx, plan)
    assert claim.id == "c2" and kind is ClaimType.ABSOLUTE


async def test_the_planner_reads_the_paper_s_own_schedule(store: Store, tmp_path: Path) -> None:
    """Scale is the paper's schedule against the budget: the Planner needs the schedule."""
    from app.domain.paper import IngestedPaper, Section

    paper = IngestedPaper(
        arxiv_id="0000.00001",
        sha256="0" * 64,
        title="t",
        sections=[Section(title="Training", text="Adam for 300 epochs, batch 128.")],
        tables=[],
        source_kind="latex",
    )
    LocalArtifactStore(tmp_path).put_json("i", paper.model_dump(mode="json"))
    models = FakeModelClient(LLM_FIXTURES, case="two_seeds")
    await _plan(store, tmp_path, "two_seeds", models=models)
    prompt = _prompt(models)
    block = prompt[prompt.index('<untrusted_document source="arxiv:0000.00001"') :]
    assert "Adam for 300 epochs, batch 128." in block.split("</untrusted_document>")[0]


async def test_the_planner_works_without_the_ingested_paper(store: Store, tmp_path: Path) -> None:
    models = FakeModelClient(LLM_FIXTURES, case="two_seeds")
    await _plan(store, tmp_path, "two_seeds", models=models)  # "i" was never written
    assert "(not available)" in _prompt(models)

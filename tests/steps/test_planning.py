import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypeVar

import httpx
import pytest
import respx
from pydantic import BaseModel

from app.discovery.github import GITHUB_API
from app.discovery.hf_datasets import DatasetProblem, DatasetReport, RunCpu
from app.discovery.known_datasets import named_in
from app.domain.discovery import MAX_DATASETS, Discovery, RepoRef
from app.domain.errors import P2PError
from app.domain.events import ApprovalRequiredPayload, PlanReadyPayload, WarningPayload
from app.domain.models import (
    Claim,
    ClaimType,
    DatasetReq,
    ErrorCode,
    ExecTarget,
    PackageReq,
    Plan,
    PlanArtifact,
    PlanMode,
    RunStatus,
    Scale,
    SetupItem,
    SetupKind,
    StepName,
)
from app.execution.package_policy import PYPI
from app.models.client import Message, Role
from app.models.fake import FakeModelClient
from app.models.pricing import load_cloud_prices
from app.orchestrator.budget import BudgetExceeded, load_budget_limits
from app.orchestrator.cost_estimate import estimate_plan_cost
from app.orchestrator.steps.awaiting_approval import AwaitingApprovalStep
from app.orchestrator.steps.planning import (
    MIN_SEEDS,
    DatasetCheck,
    PackageCheck,
    PlanningStep,
    PlanTargets,
    correct_plan,
    cpu_hardware,
    needs_gpu,
)
from app.storage.artifacts import LocalArtifactStore
from app.storage.store import Store
from app.verdict.config import load_verdict_config
from tests.steps.conftest import LLM_FIXTURES, ROOT, RUN, ctx_at

DISCOVERY = f"runs/{RUN}/discovery.json"
T = TypeVar("T", bound=BaseModel)
CLOUD = load_cloud_prices(ROOT / "config" / "pricing.yaml")
OFFICIAL = RepoRef(url="https://github.com/a/b", source="paper", official=True)
LIMITS = load_budget_limits(ROOT / "config" / "budgets.yaml").per_run
SANDBOX_ON, L40S_ON = ("sandbox", "4vcpu-4gb"), ("gpu-l40s-a", "1gpu-8vcpu-32gb")
CPU_ONLY = PlanTargets(cpu=SANDBOX_ON, gpu=None)
AUTO = PlanTargets(cpu=SANDBOX_ON, gpu=L40S_ON)
GPU_INVITE = "ab" * 32


@pytest.fixture(autouse=True)
def pypi() -> Iterator[respx.MockRouter]:
    """PyPI and GitHub as each test sets them up: by default PyPI knows no project named b (the
    default repository's name). Nothing reaches the network."""
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{PYPI}/b/json", name="b").respond(404)
        router.get(f"{GITHUB_API}/repos/a/b", name="github").respond(200, json={"full_name": "a/b"})
        yield router


def _claim(cid: str, kind: ClaimType, dataset: str = "CIFAR-10") -> Claim:
    return Claim(
        id=cid,
        text=f"Ours beats ResNet-18 on {dataset}",
        claim_type=kind,
        metric="top1_accuracy",
        higher_is_better=True,
        dataset=dataset,
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
    package_check: PackageCheck | None = None,
    models: FakeModelClient | None = None,
    targets: PlanTargets | None = None,
    gpu_runs: int | None = None,
    repo: RepoRef | None = OFFICIAL,
    dataset: str = "CIFAR-10",
    popular: frozenset[str] = frozenset({"b"}),
    runs: int = 1,
) -> tuple[Plan, StepName | RunStatus, list[object]]:
    artifacts = LocalArtifactStore(tmp_path)
    discovery = Discovery(repo=repo)
    artifacts.put_json(DISCOVERY, discovery.model_dump(mode="json"))
    if gpu_runs is not None:  # the run's invite and its daily GPU runs (A-47)
        await store.create_invite(GPU_INVITE, 3, None, gpu_runs_per_day=gpu_runs)
    ctx = await ctx_at(
        store,
        StepName.PLANNING,
        invite_hash=GPU_INVITE if gpu_runs is not None else None,
        previous=[
            (StepName.INGESTING, "i"),
            (StepName.EXTRACTING_CLAIMS, "c"),
            (StepName.DISCOVERING, DISCOVERY),
        ],
    )
    claims = [_claim("c1", ClaimType.RELATIVE, dataset), _claim("c2", ClaimType.ABSOLUTE, dataset)]
    await store.save_claims(RUN, claims, selected={"c1", "c2"})
    step = PlanningStep(
        models or FakeModelClient(LLM_FIXTURES, case=case),
        artifacts,
        CLOUD,
        preemptible=preemptible,
        dataset_check=dataset_check,
        package_check=package_check,
        popular=popular,
        **({"targets": targets} if targets else {}),
    )
    for _ in range(runs):  # more than once: the same attempt resumed after a restart
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
    assert "one NVIDIA L40S GPU (48 GB, CUDA)" in _prompt(models)


async def test_in_sandboxes_the_planner_plans_for_a_cpu(store: Store, tmp_path: Path) -> None:
    models = FakeModelClient(LLM_FIXTURES, case="two_seeds")
    await _plan(store, tmp_path, "two_seeds", models=models, targets=CPU_ONLY)
    prompt = _prompt(models)
    assert cpu_hardware(LIMITS.cpu_minutes) in prompt and "L40S" not in prompt
    assert f'"experiment_minutes": {json.dumps(LIMITS.cpu_minutes)}' in prompt


async def test_the_plan_lists_up_to_max_seeds_seeds(store: Store, tmp_path: Path) -> None:
    """The Planner proposed two: the plan lists ten (config/budgets.yaml per_run.max_seeds),
    the runner runs as many as fit in the time target (A-46). A policy, not a deviation from
    the paper: no note, so no Limitations line."""
    plan, nxt, events = await _plan(store, tmp_path, "two_seeds")
    assert len(plan.seeds) == 10
    assert not any("seeds" in n for n in plan.notes)
    ready = [p for p in events if isinstance(p, PlanReadyPayload)]
    assert ready and not ready[0].needs_approval
    assert nxt is StepName.PROVISIONING  # cheap: auto-approved


async def test_over_budget_plans_are_clamped_and_reduced(store: Store, tmp_path: Path) -> None:
    plan, _, _ = await _plan(store, tmp_path, "over_gpu")
    assert plan.gpu_minutes_est == LIMITS.gpu_minutes  # config/budgets.yaml per_run
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
    corrected, notes = correct_plan(
        plan, selected={"c1": ClaimType.RELATIVE}, gpu_cap=30, max_seeds=3
    )
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
    corrected, notes = correct_plan(plan, selected=paper, gpu_cap=30, max_seeds=3)
    assert corrected.claim_types == paper
    assert notes == ["claim types follow the paper: c1 is tested as ABSOLUTE"]
    unchanged, none = correct_plan(corrected, selected=paper, gpu_cap=30, max_seeds=3)
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
        store, tmp_path, "still_made_up", dataset_check=_on_hf, models=models, dataset="Wine"
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


# --- the authors' package: an official repository planned REPO_FIRST -------------------------


def _links(*urls: str) -> httpx.Response:
    urls_by_label = {f"link {i}": url for i, url in enumerate(urls)}
    return httpx.Response(200, json={"info": {"project_urls": urls_by_label}})


async def test_the_authors_package_is_planned_when_pypi_links_the_official_repository(
    store: Store, tmp_path: Path, pypi: respx.MockRouter
) -> None:
    """InterpretML: the official repository's sources need a compiled library that only the
    authors' PyPI package ships, and the Planner did not plan it."""
    pypi["b"].mock(return_value=_links("https://b.dev", "https://github.com/a/b"))
    plan, _, _ = await _plan(store, tmp_path, "two_seeds")  # REPO_FIRST, plans timm only
    assert plan.packages == [PackageReq(name="b"), PackageReq(name="timm", specifier="==1.0.9")]
    assert any("authors' package b was added" in n for n in plan.notes)
    assert not pypi["github"].called  # PyPI links the repository as discovered


async def test_the_authors_package_goes_through_the_package_policy(
    store: Store, tmp_path: Path, pypi: respx.MockRouter
) -> None:
    pypi["b"].mock(return_value=_links("https://github.com/a/b"))
    seen: list[list[PackageReq]] = []

    async def refuse_b(packages: list[PackageReq]) -> list[PackageReq]:
        seen.append(packages)
        return [p for p in packages if p.name != "b"]

    plan, _, _ = await _plan(store, tmp_path, "two_seeds", package_check=refuse_b)
    assert [[p.name for p in packages] for packages in seen] == [["b", "timm"]]
    assert [p.name for p in plan.packages] == ["timm"]
    assert not any("was added" in n for n in plan.notes)
    assert any("authors' package b" in n and "refused" in n for n in plan.notes)


async def test_packages_the_policy_refuses_are_noted(
    store: Store, tmp_path: Path, pypi: respx.MockRouter
) -> None:
    """InterpretML: lime (sdists only) was dropped silently and nobody saw why."""
    pypi["b"].mock(return_value=_links("https://github.com/a/b"))

    async def refuse_timm(packages: list[PackageReq]) -> list[PackageReq]:
        return [p for p in packages if p.name != "timm"]

    plan, _, _ = await _plan(store, tmp_path, "two_seeds", package_check=refuse_timm)
    assert [p.name for p in plan.packages] == ["b"]
    assert "refused by the package policy: timm" in plan.notes


@pytest.mark.parametrize("specifier", ["0.7.3", "latest", ">= 0.6 (EBM)", "*"])
def test_a_package_with_an_unparsable_version_keeps_its_name(specifier: str) -> None:
    """InterpretML (eval set B, 2026-10-30): interpret was refused at planning without a PyPI
    lookup, so for its syntax, and the Coder then declared every claim unmeasured. The version
    is the Planner's guess: the package is still judged by the policy, without it."""
    from app.orchestrator.steps.planning import plain_specifiers

    planned = [PackageReq(name="interpret", specifier=specifier), PackageReq(name="timm")]
    packages, unpinned = plain_specifiers(planned)
    assert packages == [PackageReq(name="interpret"), PackageReq(name="timm")]
    assert unpinned == ["interpret"]
    kept = [PackageReq(name="timm", specifier="==1.0.9"), PackageReq(name="x y", specifier="?")]
    assert plain_specifiers(kept) == (kept, [])  # an invalid name is the policy's to refuse


def test_the_unpinned_note_names_only_packages_the_policy_accepted() -> None:
    from app.orchestrator.steps.planning import unpinned_note

    accepted = [PackageReq(name=f"p{i}") for i in range(12)]
    note = unpinned_note(["pip", *(p.name for p in accepted)], accepted)
    assert note is not None and "pip" not in note
    assert note.startswith("no plain version was given for p0, p1") and "and 2 more:" in note
    assert unpinned_note(["pip"], accepted) is None


def test_the_refused_packages_note_shows_only_valid_names_and_is_capped() -> None:
    """The names come from the Planner: a name the policy would not even parse is not quoted."""
    from app.orchestrator.steps.planning import refused_note

    hostile = ["git+https://evil.example/x", "a\nb", "x" * 200, *(f"pkg{i}" for i in range(12))]
    note = refused_note(hostile)
    assert note.startswith("refused by the package policy: 3 invalid names, pkg0, pkg1")
    assert "evil" not in note and "\n" not in note and "x" * 200 not in note
    assert note.endswith("pkg9 and 2 more")


async def test_a_search_found_repository_whose_popular_package_links_elsewhere_adds_nothing(
    store: Store, tmp_path: Path, pypi: respx.MockRouter
) -> None:
    pypi["b"].mock(return_value=_links("https://github.com/real-owner/b"))
    found = RepoRef(url="https://github.com/a/b", source="tavily", official=False)
    plan, _, _ = await _plan(store, tmp_path, "two_seeds", repo=found)
    assert [p.name for p in plan.packages] == ["timm"]
    assert not any("authors' package" in n for n in plan.notes)


async def test_the_authors_package_the_policy_accepts_is_noted_as_added(
    store: Store, tmp_path: Path, pypi: respx.MockRouter
) -> None:
    pypi["b"].mock(return_value=_links("https://github.com/a/b"))

    async def accept(packages: list[PackageReq]) -> list[PackageReq]:
        return packages

    plan, _, _ = await _plan(store, tmp_path, "two_seeds", package_check=accept)
    assert [p.name for p in plan.packages] == ["b", "timm"]
    assert any("authors' package b was added" in n for n in plan.notes)


@pytest.mark.parametrize(
    "answer",
    [
        _links("https://github.com/someone/b", "https://github.com/a/b-extra"),
        httpx.Response(404),
        httpx.ConnectError("down"),
    ],
    ids=["links-elsewhere", "not-on-pypi", "network-error"],
)
async def test_no_authors_package_without_a_link_back_to_the_repository(
    store: Store, tmp_path: Path, pypi: respx.MockRouter, answer: httpx.Response | Exception
) -> None:
    if isinstance(answer, Exception):
        pypi["b"].mock(side_effect=answer)
    else:
        pypi["b"].mock(return_value=answer)
    plan, _, _ = await _plan(store, tmp_path, "two_seeds")
    assert pypi["b"].called
    assert [p.name for p in plan.packages] == ["timm"]
    assert not any("authors' package" in n for n in plan.notes)


@pytest.mark.parametrize(
    ("case", "repo"),
    [
        ("two_seeds", None),
        ("made_up_dataset", OFFICIAL),  # FROM_SCRATCH
        ("two_seeds", RepoRef(url="https://github.com/a/timm", source="paper", official=True)),
    ],
    ids=["no-repository", "from-scratch", "already-planned"],
)
async def test_the_authors_package_is_looked_up_only_for_a_repository_run_as_is(
    store: Store, tmp_path: Path, pypi: respx.MockRouter, case: str, repo: RepoRef | None
) -> None:
    pypi["b"].mock(return_value=_links("https://github.com/a/b"))
    plan, _, _ = await _plan(store, tmp_path, case, repo=repo)  # any other request would fail
    assert not pypi["b"].called and not pypi["github"].called
    assert "b" not in [p.name for p in plan.packages]
    assert not any("authors' package" in n for n in plan.notes)


@pytest.mark.parametrize(
    ("popular", "planned"), [(frozenset({"b"}), ["b", "timm"]), (frozenset(), ["timm"])]
)
async def test_a_repository_found_by_search_gets_its_popular_authors_package(
    store: Store,
    tmp_path: Path,
    pypi: respx.MockRouter,
    popular: frozenset[str],
    planned: list[str],
) -> None:
    """InterpretML (production, 2026-10-30): search found interpretml/interpret, not marked
    official. A popular package whose PyPI page links to the very repository is that project's,
    whoever found the repository; an unpopular one is never added."""
    pypi["b"].mock(return_value=_links("https://github.com/a/b"))
    found = RepoRef(url="https://github.com/a/b", source="tavily", official=False)
    plan, _, _ = await _plan(store, tmp_path, "two_seeds", repo=found, popular=popular)
    assert [p.name for p in plan.packages] == planned
    added = any("authors' package b was added" in n for n in plan.notes)
    assert added is bool(popular)


# --- known datasets the claims name ----------------------------------------------------------


async def _keep(datasets: list[DatasetReq], run_cpu: RunCpu) -> DatasetReport:
    return DatasetReport(kept=datasets, dropped=[], unchecked=[])


async def test_a_claim_on_mnist_plans_its_hugging_face_copy(store: Store, tmp_path: Path) -> None:
    """Distillation: the Planner planned no data for MNIST claims ("loaded via torchvision"),
    but experiments run offline, where torchvision cannot download."""
    checked: list[list[DatasetReq]] = []

    async def keep(datasets: list[DatasetReq], run_cpu: RunCpu) -> DatasetReport:
        checked.append(datasets)
        return await _keep(datasets, run_cpu)

    plan, _, _ = await _plan(store, tmp_path, "two_seeds", dataset="MNIST", dataset_check=keep)
    assert plan.datasets == [DatasetReq(kind="hf", id="ylecun/mnist")]
    assert checked == [plan.datasets]  # added before the dataset check, which judges it too
    assert any("MNIST" in n and "ylecun/mnist" in n for n in plan.notes)


@pytest.mark.parametrize(
    ("dataset", "expected"),
    [
        ("Fashion-MNIST", ["zalando-datasets/fashion_mnist"]),
        ("CIFAR-100", ["uoft-cs/cifar100"]),
        ("digits (scikit-learn load_digits, MNIST-like)", []),
        ("synthetic data generated in code", []),
    ],
)
async def test_known_datasets_are_planned_once_and_only_when_a_claim_names_them(
    store: Store, tmp_path: Path, dataset: str, expected: list[str]
) -> None:
    plan, _, _ = await _plan(store, tmp_path, "two_seeds", dataset=dataset, dataset_check=_keep)
    assert [d.id for d in plan.datasets] == expected


async def test_the_retried_plan_gets_the_known_dataset_too(store: Store, tmp_path: Path) -> None:
    models = FakeModelClient(LLM_FIXTURES, case="still_made_up")  # CIFAR-10 claims, wine data
    plan, _, _ = await _plan(store, tmp_path, "still_made_up", dataset_check=_on_hf, models=models)
    assert [d.id for d in plan.datasets] == ["uoft-cs/cifar10"]
    assert len(models.calls) == 2


def _plan_with(*datasets: DatasetReq, claims: tuple[str, ...] = ("c1",)) -> Plan:
    return Plan(
        mode=PlanMode.FROM_SCRATCH,
        claim_ids=list(claims),
        claim_types={c: ClaimType.RELATIVE for c in claims},
        scale=Scale.REDUCED,
        seeds=[0, 1, 2],
        datasets=list(datasets),
        gpu_minutes_est=10,
    )


MNIST = named_in("MNIST", "")
RELATIVE = {"c1": ClaimType.RELATIVE, "c2": ClaimType.RELATIVE}


def _seeds_plan(seeds: list[int]) -> Plan:
    return Plan(
        mode=PlanMode.FROM_SCRATCH,
        claim_ids=["c1"],
        claim_types={"c1": ClaimType.RELATIVE},
        scale=Scale.REDUCED,
        seeds=seeds,
        gpu_minutes_est=10,
    )


@pytest.mark.parametrize(
    ("proposed", "planned"),
    [
        ([42, 7, 13], [42, 7, 13, 0, 1, 2, 3, 4, 5, 6]),
        ([1], [1, 0, 2, 3, 4, 5, 6, 7, 8, 9]),
        (list(range(20, 35)), list(range(20, 30))),
        (list(range(10)), list(range(10))),
        # numpy, torch and random all take a seed below 2**31; the replay's --seeds too
        ([2**31, 5, 2**31 - 1], [5, 2**31 - 1, 0, 1, 2, 3, 4, 6, 7, 8]),
    ],
)
def test_the_planners_seeds_come_first_then_the_smallest_unused(
    proposed: list[int], planned: list[int]
) -> None:
    corrected, notes = correct_plan(
        _seeds_plan(proposed), selected=RELATIVE, gpu_cap=30, max_seeds=10
    )
    assert corrected.seeds == planned
    assert notes == []


def test_the_minimum_seeds_are_the_verdicts() -> None:
    """The runner's minimum before the time rule is the verdict's: fewer would decide
    INSUFFICIENT_SEEDS on a run that stopped by design."""
    assert load_verdict_config(ROOT / "config" / "verdict.yaml").min_seeds == MIN_SEEDS


@pytest.mark.parametrize("planned", ["ylecun/mnist", "YLeCun/MNIST", "mnist"])
def test_a_known_dataset_already_planned_is_not_added_again(planned: str) -> None:
    plan = _plan_with(DatasetReq(kind="hf", id=planned, split="test"))
    corrected, notes = correct_plan(
        plan, selected=RELATIVE, gpu_cap=30, max_seeds=3, named={"c1": MNIST}
    )
    assert corrected.datasets == plan.datasets and notes == []


def test_a_dataset_planned_twice_is_kept_once_with_its_first_split() -> None:
    """Distillation (production, 2026-10-30): MNIST planned as train and test. The provisioner
    names a dataset's archive after its id: two archives of the same name broke the run."""
    plan = _plan_with(
        DatasetReq(kind="hf", id="ylecun/mnist", split="train"),
        DatasetReq(kind="hf", id="uoft-cs/cifar10"),
        DatasetReq(kind="hf", id="YLeCun/MNIST", split="test"),  # Hugging Face ids ignore case
    )
    corrected, notes = correct_plan(plan, selected=RELATIVE, gpu_cap=30, max_seeds=3)
    assert [(d.id, d.split) for d in corrected.datasets] == [
        ("ylecun/mnist", "train"),
        ("uoft-cs/cifar10", None),
    ]
    assert notes == [
        "dataset YLeCun/MNIST dropped: ylecun/mnist is already planned and one split per "
        "dataset is fetched (the experiment holds out part of it for the other split)"
    ]


def test_two_claims_on_one_known_dataset_add_it_once() -> None:
    plan = _plan_with(claims=("c1", "c2"))
    named = {"c1": MNIST, "c2": MNIST}
    corrected, notes = correct_plan(plan, selected=RELATIVE, gpu_cap=30, max_seeds=3, named=named)
    assert [d.id for d in corrected.datasets] == ["ylecun/mnist"] and len(notes) == 1


def test_only_the_claims_the_plan_tests_add_known_datasets() -> None:
    plan = _plan_with()  # c2 is selected but the Planner does not test it
    corrected, notes = correct_plan(
        plan, selected=RELATIVE, gpu_cap=30, max_seeds=3, named={"c2": MNIST}
    )
    assert corrected.datasets == [] and notes == []


@pytest.mark.parametrize(
    "planned",
    [
        [DatasetReq(kind="hf", id=f"org/d{i}") for i in range(MAX_DATASETS)],
        [DatasetReq(kind="hf", id=f"org/h{i}", max_bytes=10_000_000_000) for i in range(2)],
    ],
    ids=["datasets-cap", "bytes-cap"],
)
def test_known_datasets_respect_the_caps(planned: list[DatasetReq]) -> None:
    plan = _plan_with(*planned)
    corrected, notes = correct_plan(
        plan, selected=RELATIVE, gpu_cap=30, max_seeds=3, named={"c1": MNIST}
    )
    assert corrected.datasets == planned
    assert len(notes) == 1 and "MNIST" in notes[0] and "not added" in notes[0]


async def test_an_authors_package_that_is_not_popular_is_never_added_automatically(
    store: Store, tmp_path: Path, pypi: respx.MockRouter
) -> None:
    """Its PyPI links are self-declared: a squatter can point them at the official repository.
    Only a popular name (the top PyPI list plus the curated one) is the real project."""
    pypi["b"].mock(return_value=_links("https://github.com/a/b"))
    plan, _, _ = await _plan(store, tmp_path, "two_seeds", popular=frozenset())
    assert [p.name for p in plan.packages] == ["timm"]
    assert any("authors' package b" in n and "not added" in n for n in plan.notes)


def test_the_popular_set_is_the_package_policy_s() -> None:
    from app.execution.package_policy import load_policy_config
    from app.orchestrator.steps.planning import POPULAR

    assert load_policy_config(ROOT / "config").popular == POPULAR
    assert "interpret" in POPULAR  # InterpretML's own package (Microsoft), curated


async def test_a_huge_claim_field_is_matched_within_a_bound() -> None:
    from app.orchestrator.steps.planning import NAMED_CHARS

    assert NAMED_CHARS <= 4096


# --- the paper's setup: the scale is derived, never a label (A-43) -------------------------


def _setup_plan(scale: Scale, *, planned: str = "200", listed: bool = True) -> Plan:
    epochs = SetupItem(
        id="x", name="epochs", kind=SetupKind.NUMBER, paper="200", planned=planned, source_ref="4"
    )
    return Plan(
        mode=PlanMode.FROM_SCRATCH,
        claim_ids=["c1"],
        claim_types={"c1": ClaimType.RELATIVE},
        scale=scale,
        seeds=[0, 1, 2],
        gpu_minutes_est=10,
        setup=[epochs] if listed else [],
    )


async def test_full_scale_needs_every_listed_condition_at_the_paper_s_value(
    store: Store, tmp_path: Path
) -> None:
    plan, _, _ = await _plan(store, tmp_path, "setup_full")
    assert plan.scale is Scale.FULL
    assert [(i.id, i.name, i.planned) for i in plan.setup] == [
        ("s1", "epochs", "200"),
        ("s2", "optimizer", "SGD"),
    ]
    assert not any("scale REDUCED" in n for n in plan.notes)


async def test_a_planned_value_below_the_paper_s_makes_the_plan_reduced(
    store: Store, tmp_path: Path
) -> None:
    """Production, 2026-10-30: plans labelled FULL ran 200 steps where the paper runs thousands."""
    plan, _, _ = await _plan(store, tmp_path, "setup_reduced_by_plan")
    assert plan.scale is Scale.REDUCED
    assert "scale REDUCED: epochs planned 20, the paper uses 200" in plan.notes


async def test_a_plan_without_conditions_is_reduced(store: Store, tmp_path: Path) -> None:
    plan, _, _ = await _plan(store, tmp_path, "setup_missing")
    assert plan.scale is Scale.REDUCED and plan.setup == []
    assert "scale REDUCED: the plan lists no condition of the paper's setup to check" in plan.notes


async def test_a_plan_that_lists_matching_conditions_stays_full(
    store: Store, tmp_path: Path
) -> None:
    plan, _, _ = await _plan(store, tmp_path, "two_seeds")
    assert plan.scale is Scale.FULL and len(plan.setup) == 2


def test_the_code_never_raises_the_scale() -> None:
    corrected, notes = correct_plan(
        _setup_plan(Scale.REDUCED), selected={"c1": ClaimType.RELATIVE}, gpu_cap=30, max_seeds=3
    )
    assert corrected.scale is Scale.REDUCED and notes == []
    assert [i.id for i in corrected.setup] == ["s1"]


def test_a_reduced_plan_without_conditions_gets_no_extra_note() -> None:
    """It is reduced already: the missing list changes nothing, so there is nothing to say."""
    corrected, notes = correct_plan(
        _setup_plan(Scale.REDUCED, listed=False),
        selected={"c1": ClaimType.RELATIVE},
        gpu_cap=30,
        max_seeds=3,
    )
    assert corrected.scale is Scale.REDUCED and notes == []


def test_correcting_a_corrected_setup_changes_nothing() -> None:
    selected = {"c1": ClaimType.RELATIVE}
    once, notes = correct_plan(
        _setup_plan(Scale.FULL, planned="20"), selected=selected, gpu_cap=30, max_seeds=3
    )
    assert once.scale is Scale.REDUCED
    assert notes == ["scale REDUCED: epochs planned 20, the paper uses 200"]
    twice, again = correct_plan(once, selected=selected, gpu_cap=30, max_seeds=3)
    assert twice == once and again == []


async def test_the_planner_is_asked_for_the_paper_s_conditions(
    store: Store, tmp_path: Path
) -> None:
    models = FakeModelClient(LLM_FIXTURES, case="setup_full")
    await _plan(store, tmp_path, "setup_full", models=models)
    prompt = _prompt(models)
    assert "- setup:" in prompt and "`planned`" in prompt


def test_a_reduction_stated_on_a_condition_code_cannot_keep_still_reduces_the_plan() -> None:
    """Security review, 2026-11-13: the gate failed open when the Planner's own reduction sat
    on a condition that was dropped."""
    plan = _setup_plan(Scale.FULL)
    garbage = SetupItem(id="y", name="optimizer", kind=SetupKind.CHOICE, paper="—", planned="Adam")
    plan = plan.model_copy(update={"setup": [*plan.setup, garbage]})
    corrected, notes = correct_plan(
        plan, selected={"c1": ClaimType.RELATIVE}, gpu_cap=30, max_seeds=3
    )
    assert corrected.scale is Scale.REDUCED
    assert any("could not be kept planned something else" in n for n in notes)


# --- GPU when needed (A-47) -----------------------------------------------------------------


def _artifact(tmp_path: Path) -> PlanArtifact:
    return PlanArtifact.model_validate(
        LocalArtifactStore(tmp_path).get_json(f"runs/{RUN}/plan.json")
    )


def _planner_calls(models: FakeModelClient) -> int:
    return sum(1 for role, _ in models.requests if role is Role.PLANNER)


async def _gpu_runs_used(store: Store) -> int:
    [invite] = [i for i in await store.list_invites() if i.code_hash == GPU_INVITE]
    return invite.gpu_used_today


def _base_plan(**changes: object) -> Plan:
    data: dict[str, object] = {
        "mode": PlanMode.REPO_FIRST,
        "claim_ids": ["c1"],
        "claim_types": {"c1": ClaimType.RELATIVE},
        "scale": Scale.FULL,
        "seeds": [0, 1, 2],
        "gpu_minutes_est": 10,
        **changes,
    }
    return Plan.model_validate(data)


@pytest.mark.parametrize(
    ("changes", "gpu"),
    [
        ({"scale": Scale.REDUCED}, True),
        ({"gpu_minutes_est": 10}, False),
        ({"gpu_minutes_est": 15}, False),  # half of the 30 CPU minutes: still the CPU
        ({"gpu_minutes_est": 15.1}, True),
    ],
)
def test_a_cpu_plan_needs_a_gpu_when_cut_or_close_to_the_cpu_time(
    changes: dict[str, object], gpu: bool
) -> None:
    assert needs_gpu(_base_plan(**changes), LIMITS) is gpu


async def test_with_auto_a_plan_that_fits_the_cpu_stays_on_cpu(
    store: Store, tmp_path: Path
) -> None:
    models = FakeModelClient(LLM_FIXTURES, case="cpu_fits")
    await _plan(store, tmp_path, "cpu_fits", models=models, targets=AUTO, gpu_runs=1)
    artifact = _artifact(tmp_path)
    assert (artifact.target, artifact.cpu_plan, artifact.gpu_skipped) == (
        ExecTarget.CPU,
        None,
        None,
    )
    assert _planner_calls(models) == 1
    assert await _gpu_runs_used(store) == 0  # the invite's GPU run is still there


async def test_with_auto_a_cut_plan_gets_a_gpu_plan(store: Store, tmp_path: Path) -> None:
    models = FakeModelClient(LLM_FIXTURES, case="gpu_needed")
    plan, _, events = await _plan(
        store, tmp_path, "gpu_needed", models=models, targets=AUTO, gpu_runs=1
    )
    artifact = _artifact(tmp_path)
    assert artifact.target is ExecTarget.GPU
    assert (plan.scale, plan.gpu_minutes_est) == (Scale.FULL, 25)
    assert artifact.cpu_plan is not None and artifact.cpu_plan.scale is Scale.REDUCED
    assert _planner_calls(models) == 2
    first, second = (m for role, m in models.requests if role is Role.PLANNER)
    gpu_prompt = "\n".join(str(m.content) for m in second)
    assert "NVIDIA L40S" in gpu_prompt and "L40S" not in "\n".join(str(m.content) for m in first)
    assert f'"experiment_minutes": {json.dumps(LIMITS.gpu_minutes)}' in gpu_prompt
    hourly = CLOUD.hourly(*L40S_ON, preemptible=True)
    assert artifact.est_cost_max_usd is not None
    assert artifact.est_cost_max_usd - artifact.est_cost_usd == pytest.approx(
        (LIMITS.gpu_minutes - 25) / 60 * hourly, abs=1e-3
    )  # priced on the GPU platform; the worst case is the whole GPU allowance
    [ready] = [p for p in events if isinstance(p, PlanReadyPayload)]
    assert (ready.target, ready.cpu_plan) == (ExecTarget.GPU, artifact.cpu_plan)
    assert ready.est_cost_max_usd == artifact.est_cost_max_usd
    assert await _gpu_runs_used(store) == 1


async def test_with_auto_no_gpu_run_left_keeps_the_cpu_plan(store: Store, tmp_path: Path) -> None:
    models = FakeModelClient(LLM_FIXTURES, case="gpu_needed")
    plan, _, events = await _plan(
        store, tmp_path, "gpu_needed", models=models, targets=AUTO, gpu_runs=0
    )
    artifact = _artifact(tmp_path)
    skipped = "a GPU would help this experiment, but this invite has no GPU runs left today"
    assert (artifact.target, artifact.gpu_skipped, plan.scale) == (
        ExecTarget.CPU,
        skipped,
        Scale.REDUCED,
    )
    assert _planner_calls(models) == 1
    [ready] = [p for p in events if isinstance(p, PlanReadyPayload)]
    assert ready.gpu_skipped == skipped


class _GpuPlanFails(FakeModelClient):
    """The Planner's second answer, the GPU plan, is unusable (a P2PError after repair)."""

    planner_calls = 0

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
        if prompt == "planner_experiment_plan":
            self.planner_calls += 1
            if self.planner_calls == 2:
                raise P2PError(ErrorCode.SCHEMA_ERROR, "unusable plan")
        return await super().structured(
            role, messages, schema, run_id=run_id, step=step, prompt=prompt
        )


async def test_with_auto_a_failed_gpu_plan_keeps_the_cpu_plan(store: Store, tmp_path: Path) -> None:
    models = _GpuPlanFails(LLM_FIXTURES, case="gpu_needed")
    await _plan(store, tmp_path, "gpu_needed", models=models, targets=AUTO, gpu_runs=1)
    artifact = _artifact(tmp_path)
    assert (artifact.target, artifact.gpu_skipped) == (
        ExecTarget.CPU,
        "the GPU plan could not be made: the CPU plan runs",
    )


async def test_in_sandboxes_a_cut_plan_never_asks_for_a_gpu(store: Store, tmp_path: Path) -> None:
    models = FakeModelClient(LLM_FIXTURES, case="gpu_needed")
    await _plan(store, tmp_path, "gpu_needed", models=models, targets=CPU_ONLY, gpu_runs=1)
    artifact = _artifact(tmp_path)
    assert (artifact.target, artifact.gpu_skipped) == (ExecTarget.CPU, None)
    assert _planner_calls(models) == 1
    assert await _gpu_runs_used(store) == 0


async def test_the_designed_deploy_plans_once_for_the_gpu(store: Store, tmp_path: Path) -> None:
    """P2P_EXEC_BACKEND=gpu: every experiment on the GPU, no CPU to fall back to."""
    models = FakeModelClient(LLM_FIXTURES, case="cpu_fits")
    targets = PlanTargets(cpu=None, gpu=L40S_ON)
    await _plan(store, tmp_path, "cpu_fits", models=models, targets=targets)
    artifact = _artifact(tmp_path)
    assert (artifact.target, artifact.cpu_plan) == (ExecTarget.GPU, None)
    assert artifact.est_cost_max_usd is not None
    assert _planner_calls(models) == 1 and "NVIDIA L40S" in _prompt(models)


async def test_planning_resumed_after_a_restart_takes_no_second_gpu_run(
    store: Store, tmp_path: Path
) -> None:
    """The GPU run PLANNING took is recorded on the run: a resumed step uses it again instead
    of taking another (security review, A-47)."""
    await _plan(store, tmp_path, "gpu_needed_twice", targets=AUTO, gpu_runs=2, runs=2)
    assert await _gpu_runs_used(store) == 1
    assert _artifact(tmp_path).target is ExecTarget.GPU

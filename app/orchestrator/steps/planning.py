"""PLANNING: the Planner (Ultra) proposes an experiment; deterministic code corrects it to the
budget and invariants, estimates the cost and decides auto-approval (architecture §6, A-18)."""

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping

from app.discovery.hf_datasets import DatasetProblem, DatasetReport, RunCpu
from app.domain.discovery import (
    HF_DATASET_ID,
    HF_SPLIT,
    MAX_DATASETS,
    MAX_DATASETS_BYTES,
    Discovery,
)
from app.domain.errors import P2PError
from app.domain.events import EventType
from app.domain.models import ClaimType, DatasetReq, PackageReq, Plan, Scale, StepName
from app.models.client import Message, ModelClient, Role
from app.models.pricing import CloudPrices
from app.models.prompt_files import load_prompt
from app.models.untrusted import UNTRUSTED_NOTICE, wrap_untrusted
from app.orchestrator.budget import SpendKind
from app.orchestrator.context import RunContext
from app.orchestrator.cost_estimate import EXEC_PLATFORM, EXEC_PRESET, estimate_plan_cost
from app.orchestrator.fsm import StepResult
from app.orchestrator.steps.common import (
    PAPER_EXCERPT_CHARS,
    ingested_paper,
    output_of,
    paper_excerpt,
)
from app.storage.artifacts import ArtifactStore

MIN_SEEDS = 3
PackageCheck = Callable[[list[PackageReq]], Awaitable[list[PackageReq]]]
DatasetCheck = Callable[[list[DatasetReq], RunCpu], Awaitable[DatasetReport]]


GPU_HARDWARE = "one L40S GPU (48 GB)"
# the hybrid deploy without GPU capacity (docs/specs/hybrid-deploy.md)
CPU_HARDWARE = (
    "a CPU only (no GPU): one Token Factory Sandboxes microVM with 4 vCPUs and 4 GB of RAM, "
    "offline while the experiment runs (provisioning fetches packages, the repository and data "
    "before), the experiment at most 30 minutes in all; scale down only GPU-sized training "
    "that this CPU and memory cannot finish in that time"
)


def _problems(dropped: list[DatasetProblem]) -> str:
    """Code-written lines; the ids already match HF_DATASET_ID (correct_plan)."""
    return "\n".join(f"- {d.id} (split {d.split or 'train'}): {d.reason}" for d in dropped)


def correct_plan(
    plan: Plan, *, selected: Mapping[str, ClaimType], gpu_cap: float
) -> tuple[Plan, list[str]]:
    """Deterministic post-checks. Returns the corrected plan and what was changed. `selected`
    maps each selected claim to the type the paper gives it: the Planner picks which claims to
    test, never their type (the verdict judges the claim as extracted)."""
    notes: list[str] = []
    claim_ids = [c for c in plan.claim_ids if c in selected]
    if len(claim_ids) != len(plan.claim_ids):
        notes.append("dropped claims that were not selected")
    retyped = [c for c in claim_ids if plan.claim_types.get(c, selected[c]) is not selected[c]]
    if retyped:
        notes.append(
            "claim types follow the paper: "
            + ", ".join(f"{c} is tested as {selected[c].value}" for c in retyped)
        )
    seeds = list(plan.seeds)
    if len(seeds) < MIN_SEEDS:
        extra = [s for s in range(100) if s not in seeds][: MIN_SEEDS - len(seeds)]
        seeds += extra
        notes.append(f"seeds raised to {MIN_SEEDS} (the verdict needs at least {MIN_SEEDS})")
    datasets: list[DatasetReq] = []
    for dataset in plan.datasets:  # the provisioner would refuse anything else (fail early)
        valid = HF_DATASET_ID.fullmatch(dataset.id) and (
            dataset.split is None or HF_SPLIT.fullmatch(dataset.split)
        )
        total = sum(d.max_bytes for d in datasets) + dataset.max_bytes
        if valid and len(datasets) < MAX_DATASETS and total <= MAX_DATASETS_BYTES:
            datasets.append(dataset)
    if len(datasets) != len(plan.datasets):
        notes.append(
            f"dropped datasets that are invalid or over the cap ({MAX_DATASETS} datasets, "
            f"{MAX_DATASETS_BYTES // 2**30} GiB in total)"
        )
    gpu, scale = plan.gpu_minutes_est, plan.scale
    if gpu > gpu_cap:
        gpu, scale = gpu_cap, Scale.REDUCED
        notes.append(f"GPU estimate clamped to the {gpu_cap:g}-minute budget: scale REDUCED")
    corrected = plan.model_copy(
        update={
            "claim_ids": claim_ids,
            "claim_types": {c: selected[c] for c in claim_ids},
            "seeds": seeds,
            "datasets": datasets,
            "gpu_minutes_est": gpu,
            "scale": scale,
            "notes": [*plan.notes, *notes],
        }
    )
    return Plan.model_validate(corrected.model_dump()), notes


class PlanningStep:
    name = StepName.PLANNING

    def __init__(
        self,
        models: ModelClient,
        artifacts: ArtifactStore,
        cloud: CloudPrices,
        *,
        preemptible: bool,
        package_check: PackageCheck | None = None,
        dataset_check: DatasetCheck | None = None,
        exec_on: tuple[str, str] = (EXEC_PLATFORM, EXEC_PRESET),
        hardware: str = GPU_HARDWARE,
    ) -> None:
        self._models = models
        self._artifacts = artifacts
        self._cloud = cloud
        self._preemptible = preemptible
        self._package_check = package_check
        self._dataset_check = dataset_check
        self._exec_on = exec_on  # the configured exec platform and preset (the estimate's price)
        self._hardware = hardware  # what the experiment runs on, as the Planner reads it

    async def run(self, ctx: RunContext) -> StepResult:
        claims = [c for c, selected in await ctx.store.get_claims(ctx.run_id) if selected]
        discovery_key = await output_of(ctx, StepName.DISCOVERING)
        discovery = Discovery.model_validate(
            await asyncio.to_thread(self._artifacts.get_json, discovery_key)
        )
        limits = ctx.limits.per_run
        prompt = load_prompt("planner_experiment_plan")
        paper = await ingested_paper(ctx, self._artifacts)
        budget = json.dumps({"experiment_minutes": limits.gpu_minutes, "min_seeds": MIN_SEEDS})
        user = prompt.render(
            hardware=self._hardware,
            budget=budget,
            claims=wrap_untrusted(
                json.dumps([c.model_dump(mode="json") for c in claims], indent=1),
                source="claims",
                max_chars=30_000,
            ),
            discovery=wrap_untrusted(
                discovery.model_dump_json(indent=1), source="discovery", max_chars=10_000
            ),
            paper=wrap_untrusted(
                await ctx.run_cpu(paper_excerpt, paper, PAPER_EXCERPT_CHARS),
                source=f"arxiv:{paper.arxiv_id}",
                max_chars=PAPER_EXCERPT_CHARS,
            )
            if paper is not None
            else "(not available)",
        )
        messages = [
            Message(role="system", content=UNTRUSTED_NOTICE),
            Message(role="user", content=user),
        ]
        proposed = await self._models.structured(
            Role.PLANNER, messages, Plan, run_id=ctx.run_id, step=ctx.step.value, prompt=prompt.name
        )
        selected = {c.id: c.claim_type for c in claims}
        plan, _ = correct_plan(proposed, selected=selected, gpu_cap=limits.gpu_minutes)
        if self._dataset_check is not None and plan.datasets:
            history = [*messages, _said(proposed)]
            plan = await self._fetchable(ctx, self._dataset_check, plan, history, selected)
        if self._package_check is not None:
            plan = plan.model_copy(update={"packages": await self._package_check(plan.packages)})
        else:
            plan = plan.model_copy(
                update={"notes": [*plan.notes, "packages are validated at provisioning"]}
            )

        if plan.scale is Scale.REDUCED and all(
            t is ClaimType.ABSOLUTE for t in plan.claim_types.values()
        ):
            await ctx.emit(
                EventType.WARNING,
                {
                    "code": "ABSOLUTE_AT_REDUCED_SCALE",
                    "message": "only ABSOLUTE claims at reduced scale: the verdict will be "
                    "inconclusive; prefer a RELATIVE comparison",
                },
            )

        spent = await ctx.budget.run_spend(ctx.run_id, SpendKind.INFERENCE_USD)
        cost = estimate_plan_cost(
            plan,
            self._cloud,
            inference_so_far=spent,
            preemptible=self._preemptible,
            exec_on=self._exec_on,
        )
        needs_approval = cost >= ctx.limits.approval.auto_approve_below_usd
        key = f"runs/{ctx.run_id}/plan.json"
        await asyncio.to_thread(
            self._artifacts.put_json,
            key,
            {"plan": plan.model_dump(mode="json"), "est_cost_usd": cost},
        )
        await ctx.emit(
            EventType.PLAN_READY,
            {
                "plan": plan.model_dump(mode="json"),
                "est_cost_usd": cost,
                "needs_approval": needs_approval,
            },
        )
        target = StepName.AWAITING_APPROVAL if needs_approval else StepName.PROVISIONING
        return StepResult(next=target, output_ref=key)

    async def _fetchable(
        self,
        ctx: RunContext,
        check: DatasetCheck,
        plan: Plan,
        history: list[Message],
        selected: Mapping[str, ClaimType],
    ) -> Plan:
        """Datasets that cannot be fetched (a made-up id, gated, no parquet for the split, over
        the cap) get the Planner one more try with code-written reasons; whatever still fails
        is dropped, noted and warned about. Never a loop: one retry at most."""
        gpu_cap = ctx.limits.per_run.gpu_minutes
        report = await check(plan.datasets, ctx.run_cpu)
        notes: list[str] = []
        if report.dropped:
            feedback = load_prompt("planner_dataset_feedback")
            try:
                retried = await self._models.structured(
                    Role.PLANNER,
                    [
                        *history,
                        Message(
                            role="user", content=feedback.render(problems=_problems(report.dropped))
                        ),
                    ],
                    Plan,
                    run_id=ctx.run_id,
                    step=ctx.step.value,
                    prompt=feedback.name,
                )
            except P2PError:  # no usable second plan: keep the first, minus what cannot run
                plan = plan.model_copy(update={"datasets": report.kept})
            else:
                plan, _ = correct_plan(retried, selected=selected, gpu_cap=gpu_cap)
                kept = {d.id for d in plan.datasets}
                notes += [
                    f"dataset {d.id} replaced: {d.reason}"
                    for d in report.dropped
                    if d.id not in kept  # still planned: noted as dropped below, never as fixed
                ]
                report = await check(plan.datasets, ctx.run_cpu)
                plan = plan.model_copy(update={"datasets": report.kept})
            for d in report.dropped:
                notes.append(f"dataset {d.id} dropped: {d.reason}")
                await ctx.emit(
                    EventType.WARNING,
                    {
                        "code": "DATASET_UNAVAILABLE",
                        "message": f"dataset {d.id} cannot be fetched ({d.reason}): the "
                        "experiment runs without it",
                    },
                )
        if report.unchecked:
            notes.append(
                "datasets that could not be checked on Hugging Face (checked again at "
                f"provisioning): {', '.join(report.unchecked)}"
            )
        return plan.model_copy(update={"notes": [*plan.notes, *notes]})


def _said(plan: Plan) -> Message:
    """The Planner's first plan, as it proposed it, for the retry's conversation."""
    return Message(role="assistant", content=plan.model_dump_json())

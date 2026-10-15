"""PLANNING: the Planner (Ultra) proposes an experiment; deterministic code corrects it to the
budget and invariants, estimates the cost and decides auto-approval (architecture §6, A-18)."""

import asyncio
import json
from collections.abc import Awaitable, Callable

from app.domain.discovery import Discovery
from app.domain.events import EventType
from app.domain.models import ClaimType, PackageReq, Plan, Scale, StepName
from app.models.client import Message, ModelClient, Role
from app.models.pricing import CloudPrices
from app.models.prompt_files import load_prompt
from app.models.untrusted import UNTRUSTED_NOTICE, wrap_untrusted
from app.orchestrator.budget import SpendKind
from app.orchestrator.context import RunContext
from app.orchestrator.cost_estimate import estimate_plan_cost
from app.orchestrator.fsm import StepResult
from app.orchestrator.steps.common import output_of
from app.storage.artifacts import ArtifactStore

MIN_SEEDS = 3
PackageCheck = Callable[[list[PackageReq]], Awaitable[list[PackageReq]]]


def correct_plan(plan: Plan, *, selected: set[str], gpu_cap: float) -> tuple[Plan, list[str]]:
    """Deterministic post-checks. Returns the corrected plan and what was changed."""
    notes: list[str] = []
    claim_ids = [c for c in plan.claim_ids if c in selected]
    if len(claim_ids) != len(plan.claim_ids):
        notes.append("dropped claims that were not selected")
    seeds = list(plan.seeds)
    if len(seeds) < MIN_SEEDS:
        extra = [s for s in range(100) if s not in seeds][: MIN_SEEDS - len(seeds)]
        seeds += extra
        notes.append(f"seeds raised to {MIN_SEEDS} (the verdict needs at least {MIN_SEEDS})")
    gpu, scale = plan.gpu_minutes_est, plan.scale
    if gpu > gpu_cap:
        gpu, scale = gpu_cap, Scale.REDUCED
        notes.append(f"GPU estimate clamped to the {gpu_cap:g}-minute budget: scale REDUCED")
    corrected = plan.model_copy(
        update={
            "claim_ids": claim_ids,
            "claim_types": {c: plan.claim_types[c] for c in claim_ids},
            "seeds": seeds,
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
    ) -> None:
        self._models = models
        self._artifacts = artifacts
        self._cloud = cloud
        self._preemptible = preemptible
        self._package_check = package_check

    async def run(self, ctx: RunContext) -> StepResult:
        claims = [c for c, selected in await ctx.store.get_claims(ctx.run_id) if selected]
        discovery_key = await output_of(ctx, StepName.DISCOVERING)
        discovery = Discovery.model_validate(
            await asyncio.to_thread(self._artifacts.get_json, discovery_key)
        )
        limits = ctx.limits.per_run
        prompt = load_prompt("planner_experiment_plan")
        budget = json.dumps(
            {"gpu_minutes": limits.gpu_minutes, "min_seeds": MIN_SEEDS, "gpu": "1x L40S 48GB"}
        )
        user = prompt.render(
            budget=budget,
            claims=wrap_untrusted(
                json.dumps([c.model_dump(mode="json") for c in claims], indent=1),
                source="claims",
                max_chars=30_000,
            ),
            discovery=wrap_untrusted(
                discovery.model_dump_json(indent=1), source="discovery", max_chars=10_000
            ),
        )
        proposed = await self._models.structured(
            Role.PLANNER,
            [Message(role="system", content=UNTRUSTED_NOTICE), Message(role="user", content=user)],
            Plan,
            run_id=ctx.run_id,
            step=ctx.step.value,
            prompt=prompt.name,
        )
        plan, _ = correct_plan(
            proposed, selected={c.id for c in claims}, gpu_cap=limits.gpu_minutes
        )
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
            plan, self._cloud, inference_so_far=spent, preemptible=self._preemptible
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

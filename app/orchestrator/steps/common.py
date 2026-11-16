"""Helpers shared by step implementations."""

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from app.domain.errors import P2PError
from app.domain.models import (
    Claim,
    ClaimType,
    ErrorCode,
    ExecTarget,
    GpuFallback,
    Plan,
    PlanArtifact,
    StepName,
)
from app.domain.paper import IngestedPaper, Section
from app.orchestrator.context import RunContext
from app.orchestrator.fallback import fallback_key
from app.storage.artifacts import ArtifactStore

PAPER_EXCERPT_CHARS = 16_000  # what the Planner and the Coder read of the paper itself
# sections that describe the experiment, read first (titles are paper text: matched, not shown)
_SETUP = re.compile(
    r"experiment|set-?up|implementation|training|hyper|detail|dataset|data|method|result|"
    r"evaluation|appendix|protocol|benchmark",
    re.IGNORECASE,
)


async def output_of(ctx: RunContext, step: StepName) -> str:
    """The output_ref of the latest successful attempt of an earlier step."""
    done = [
        s
        for s in await ctx.store.get_steps(ctx.run_id)
        if s.name is step and s.status == "SUCCEEDED" and s.output_ref
    ]
    if not done:
        raise P2PError(ErrorCode.INTERNAL_ERROR, f"no output from step {step.value}")
    latest = max(done, key=lambda s: s.attempt)
    assert latest.output_ref is not None  # noqa: S101 (filtered above; narrows the type)
    return latest.output_ref


@dataclass(frozen=True)
class ActivePlan:
    """The plan the run executes now (A-47): PLANNING's for its target, or after the GPU
    fallback the CPU plan, with the record of why."""

    plan: Plan
    target: ExecTarget
    fallback: GpuFallback | None
    can_fall_back: bool = False  # a GPU target with a CPU plan, not fallen back yet


async def active_plan(ctx: RunContext, read_json: Callable[[str], Awaitable[Any]]) -> ActivePlan:
    """What PROVISIONING, SMOKE_TESTING, EXECUTING and VERDICT work on: plan.json, unless
    EXECUTING fell back to its CPU plan (runs/<id>/gpu_fallback.json)."""
    artifact = PlanArtifact.model_validate(await read_json(await output_of(ctx, StepName.PLANNING)))
    try:
        raw = await read_json(fallback_key(ctx.run_id))
    except FileNotFoundError:
        return ActivePlan(
            plan=artifact.plan,
            target=artifact.target,
            fallback=None,
            can_fall_back=artifact.target is ExecTarget.GPU and artifact.cpu_plan is not None,
        )
    if artifact.cpu_plan is None:
        raise P2PError(ErrorCode.INTERNAL_ERROR, "a GPU fallback without a CPU plan")
    return ActivePlan(
        plan=artifact.cpu_plan, target=ExecTarget.CPU, fallback=GpuFallback.model_validate(raw)
    )


async def planned_claims(ctx: RunContext, plan: Plan) -> list[tuple[Claim, ClaimType]]:
    """Every claim the plan tests, in the plan's order, each with the type the paper gives it
    (correct_plan keeps the plan's types equal to it; the verdict judges this one)."""
    claims = {c.id: c for c, _ in await ctx.store.get_claims(ctx.run_id)}
    planned = [(claims[i], claims[i].claim_type) for i in plan.claim_ids if i in claims]
    if not planned:
        raise P2PError(ErrorCode.INTERNAL_ERROR, "the plan has no stored claim")
    return planned


def paper_excerpt(paper: IngestedPaper, max_chars: int) -> str:
    """The paper's text for the Planner and the Coder: the sections that describe the
    experiment first, then the others, cut at `max_chars`. Production, 2026-10-29: with only
    the Reader's claim summaries they guessed the sample size, the noise and the epochs, and a
    planted false claim came out Reproduced."""
    first: list[Section] = []
    rest: list[Section] = []
    for section in paper.sections:  # one pass, a bounded title (CPU pool: run_cpu)
        (first if _SETUP.search(section.title[:200]) else rest).append(section)
    out = ""
    for section in [*first, *rest]:
        left = max_chars - len(out)
        if left <= 0:
            break
        out += f"## {section.title}\n{section.text}\n\n"[:left]
    return out.rstrip()


async def ingested_paper(ctx: RunContext, artifacts: ArtifactStore) -> IngestedPaper | None:
    """The paper INGESTING stored; None when it cannot be read (the steps work without it)."""
    try:
        data = await asyncio.to_thread(artifacts.get_json, await output_of(ctx, StepName.INGESTING))
        return IngestedPaper.model_validate(data)
    except (P2PError, FileNotFoundError, ValidationError, ValueError):
        return None

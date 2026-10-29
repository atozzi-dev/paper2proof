"""Helpers shared by step implementations."""

from app.domain.errors import P2PError
from app.domain.models import Claim, ClaimType, ErrorCode, Plan, StepName
from app.orchestrator.context import RunContext


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


async def planned_claims(ctx: RunContext, plan: Plan) -> list[tuple[Claim, ClaimType]]:
    """Every claim the plan tests, in the plan's order, each with the type the paper gives it
    (correct_plan keeps the plan's types equal to it; the verdict judges this one)."""
    claims = {c.id: c for c, _ in await ctx.store.get_claims(ctx.run_id)}
    planned = [(claims[i], claims[i].claim_type) for i in plan.claim_ids if i in claims]
    if not planned:
        raise P2PError(ErrorCode.INTERNAL_ERROR, "the plan has no stored claim")
    return planned

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


async def selected_claim(ctx: RunContext, plan: Plan) -> tuple[Claim, ClaimType]:
    """The claim the plan tests (the first stored one) and its type as planned."""
    claims = {c.id: c for c, _ in await ctx.store.get_claims(ctx.run_id)}
    for claim_id in plan.claim_ids:
        if claim_id in claims:
            return claims[claim_id], plan.claim_types.get(claim_id, claims[claim_id].claim_type)
    raise P2PError(ErrorCode.INTERNAL_ERROR, "the plan has no stored claim")

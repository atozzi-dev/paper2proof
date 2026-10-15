"""Helpers shared by step implementations."""

from app.domain.errors import P2PError
from app.domain.models import ErrorCode, StepName
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

"""VERDICT: decide() is the only thing that decides (invariant 2). The reason is adjusted
deterministically for how the execution ended, the Limitations are written by code, and the
Judge only writes prose, which is discarded if it names any other verdict."""

import asyncio
import json
from pathlib import Path
from typing import Any

from app.domain.contracts import ExecManifest
from app.domain.errors import P2PError
from app.domain.events import EventType
from app.domain.models import (
    ErrorCode,
    Plan,
    ReasonCode,
    RunStatus,
    StepName,
    Verdict,
    VerdictKind,
)
from app.models.client import Message, ModelClient, Role
from app.models.prompt_files import load_prompt
from app.models.untrusted import UNTRUSTED_NOTICE, wrap_untrusted
from app.orchestrator.budget import BudgetExceeded, KillSwitchActive
from app.orchestrator.context import RunContext
from app.orchestrator.fsm import StepResult
from app.orchestrator.steps.common import output_of
from app.storage.artifacts import ArtifactStore
from app.verdict.config import DEFAULT_PATH, config_sha256, load_verdict_config
from app.verdict.engine import decide
from app.verdict.explanation import NOT_REPRODUCED_LABEL, contradicts, template_explanation
from app.verdict.limitations import limitations
from app.verdict.results import Analysis

LABELS = {
    VerdictKind.REPRODUCED: "Reproduced",
    VerdictKind.REPRODUCED_AT_REDUCED_SCALE: "Reproduced at reduced scale",
    VerdictKind.NOT_REPRODUCED: NOT_REPRODUCED_LABEL,
    VerdictKind.INCONCLUSIVE: "Inconclusive",
}
_EXIT_REASONS = {"budget_timeout": ReasonCode.BUDGET, "experiment_error": ReasonCode.RUNTIME_ERROR}


def adjust_reason(verdict: Verdict, exit_reason: str) -> Verdict:
    """Too few seeds because the run timed out or crashed says more than INSUFFICIENT_SEEDS."""
    if verdict.reason is ReasonCode.INSUFFICIENT_SEEDS and exit_reason in _EXIT_REASONS:
        return verdict.model_copy(update={"reason": _EXIT_REASONS[exit_reason]})
    return verdict


class VerdictStep:
    name = StepName.VERDICT

    def __init__(
        self, models: ModelClient, artifacts: ArtifactStore, *, config_path: Path = DEFAULT_PATH
    ) -> None:
        self._models = models
        self._artifacts = artifacts
        self._config = load_verdict_config(config_path)
        self._config_sha = config_sha256(config_path)

    async def _json(self, key: str) -> Any:
        return await asyncio.to_thread(self._artifacts.get_json, key)

    async def _explain(
        self, ctx: RunContext, verdict: Verdict, claim_json: str, limits: str
    ) -> str | None:
        prompt = load_prompt("judge_explain").render(
            verdict_kind=verdict.kind.value,
            verdict_label=LABELS[verdict.kind],
            verdict_enum=", ".join(k.value for k in VerdictKind),
            verdict=verdict.model_dump_json(indent=1),
            limitations=wrap_untrusted(limits, source="limitations", max_chars=6000),
            claim=wrap_untrusted(claim_json, source="paper", max_chars=4000),
            untrusted_notice=UNTRUSTED_NOTICE,
        )
        try:
            completion = await self._models.complete(
                Role.JUDGE,
                [Message(role="user", content=prompt)],
                run_id=ctx.run_id,
                step=ctx.step.value,
                prompt="judge_explain",
            )
        except (P2PError, BudgetExceeded, KillSwitchActive):
            return None  # the verdict never depends on the Judge being available
        return completion.content

    async def run(self, ctx: RunContext) -> StepResult:
        stored = await self._json(await output_of(ctx, StepName.ANALYZING))
        analysis = Analysis.model_validate(stored["analysis"])
        manifest = ExecManifest.model_validate(stored["manifest"])
        plan = Plan.model_validate(
            (await self._json(await output_of(ctx, StepName.PLANNING)))["plan"]
        )
        claims = {c.id: c for c, _ in await ctx.store.get_claims(ctx.run_id)}
        claim = claims.get(manifest.claim_ref)
        if claim is None or analysis.measurements is None:
            raise P2PError(ErrorCode.INTERNAL_ERROR, "no claim or measurements to decide on")
        verdict = adjust_reason(
            decide(claim, analysis.measurements, self._config), analysis.exit_reason
        )
        limits = limitations(
            scale=manifest.scale,
            planned_seeds=manifest.seeds,
            used_seeds=analysis.used_seeds,
            gpu=analysis.gpu,
            exit_reason=analysis.exit_reason,
            deviations=plan.notes,
        )
        text = await self._explain(ctx, verdict, json.dumps(claim.model_dump(mode="json")), limits)
        if text is None or contradicts(text, verdict.kind):
            text = template_explanation(verdict, claim)
        explanation = f"{text.strip()}\n\n### Limitations\n\n{limits}"
        await ctx.store.save_verdict(
            ctx.run_id,
            claim.id,
            verdict,
            analysis.measurements.scale,
            self._config_sha,
            explanation,
        )
        await ctx.emit(
            EventType.VERDICT_READY,
            {
                "claim_id": claim.id,
                "verdict": verdict.model_dump(mode="json"),
                "explanation_md": explanation,
            },
        )
        return StepResult(next=RunStatus.COMPLETED)

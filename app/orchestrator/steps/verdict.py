"""VERDICT: decide() is the only thing that decides (invariant 2). The reason is adjusted
deterministically for how the execution ended, the Limitations are written by code, and the
Judge only writes prose, which is discarded if it names any other verdict."""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from app.bundle.build import BundleBuilder
from app.domain.contracts import ExecManifest
from app.domain.errors import P2PError
from app.domain.events import EventType
from app.domain.models import (
    Claim,
    ClaimType,
    Measurements,
    Plan,
    ReasonCode,
    RunStatus,
    Scale,
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
from app.orchestrator.steps.common import output_of, planned_claims
from app.storage.artifacts import ArtifactStore
from app.verdict.config import DEFAULT_PATH, config_sha256, load_verdict_config
from app.verdict.engine import decide
from app.verdict.explanation import NOT_REPRODUCED_LABEL, contradicts, template_explanation
from app.verdict.limitations import limitations
from app.verdict.results import Analysis
from app.verdict.scale import MetricUnit, align_to_claim
from app.verdict.summary import verdict_summary

log = logging.getLogger(__name__)
BUNDLE_TIMEOUT_S = 180.0  # bucket reads and two KMS calls; the verdict is already out

LABELS = {
    VerdictKind.REPRODUCED: "Reproduced",
    VerdictKind.REPRODUCED_AT_REDUCED_SCALE: "Reproduced at reduced scale",
    VerdictKind.NOT_REPRODUCED: NOT_REPRODUCED_LABEL,
    VerdictKind.INCONCLUSIVE: "Inconclusive",
}
_EXIT_REASONS = {
    "budget_timeout": ReasonCode.BUDGET,
    "experiment_error": ReasonCode.RUNTIME_ERROR,
    "capacity": ReasonCode.CAPACITY,
}


def not_measured(verdict: Verdict) -> Verdict:
    """A claim the experiment does not measure: decide() saw no seeds; the reason says why."""
    if verdict.reason is ReasonCode.INSUFFICIENT_SEEDS:
        return verdict.model_copy(update={"reason": ReasonCode.NOT_MEASURED})
    return verdict


def adjust_reason(verdict: Verdict, exit_reason: str) -> Verdict:
    """Too few seeds because the run timed out, crashed or found no GPU says more than
    INSUFFICIENT_SEEDS."""
    if verdict.reason is ReasonCode.INSUFFICIENT_SEEDS and exit_reason in _EXIT_REASONS:
        return verdict.model_copy(update={"reason": _EXIT_REASONS[exit_reason]})
    return verdict


class VerdictStep:
    name = StepName.VERDICT

    def __init__(
        self,
        models: ModelClient,
        artifacts: ArtifactStore,
        *,
        config_path: Path = DEFAULT_PATH,
        bundler: BundleBuilder | None = None,
        bundle_timeout_s: float = BUNDLE_TIMEOUT_S,
    ) -> None:
        self._models = models
        self._artifacts = artifacts
        self._bundler = bundler
        self._bundle_timeout_s = bundle_timeout_s
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

    async def _bundle(self, ctx: RunContext) -> None:
        """The Proof Bundle never decides anything, so it never fails the run: without one
        the run still completes, with a warning (docs/specs/proof-bundle.md)."""
        if self._bundler is None:
            return
        try:
            outcome = await asyncio.wait_for(self._bundler.build(ctx), self._bundle_timeout_s)
        except Exception as err:  # errors may quote bucket data: the type only, above DEBUG
            log.warning(
                "proof bundle not built: %s", type(err).__name__, extra={"run_id": ctx.run_id}
            )
            log.debug("proof bundle not built", exc_info=True, extra={"run_id": ctx.run_id})
            await ctx.emit(
                EventType.WARNING,
                {"code": "BUNDLE_FAILED", "message": "the Proof Bundle could not be built"},
            )
            return
        if not outcome.signed:
            await ctx.emit(
                EventType.WARNING,
                {"code": "BUNDLE_UNSIGNED", "message": "the Proof Bundle is not signed"},
            )

    async def run(self, ctx: RunContext) -> StepResult:
        stored = await self._json(await output_of(ctx, StepName.ANALYZING))
        analysis = Analysis.model_validate(stored["analysis"])
        manifest = ExecManifest.model_validate(stored["manifest"])
        hardware = stored.get("hardware") if isinstance(stored.get("hardware"), dict) else {}
        platform, preset = hardware.get("platform"), hardware.get("preset")
        where = (  # where the judged job ran, for the Limitations
            platform if isinstance(platform, str) else None,
            preset if isinstance(preset, str) else None,
        )
        plan = Plan.model_validate(
            (await self._json(await output_of(ctx, StepName.PLANNING)))["plan"]
        )
        smoke = await self._json(await output_of(ctx, StepName.SMOKE_TESTING))
        units, unmeasured = _declared(smoke)
        decided = [
            await self._decide(ctx, claim, analysis, manifest, plan, units, unmeasured, where)
            for claim, _ in await planned_claims(ctx, plan)
        ]
        stored_verdicts = [v for _, v in await ctx.store.list_verdicts(ctx.run_id)]
        await ctx.store.set_verdict_summary(ctx.run_id, verdict_summary(stored_verdicts))
        await asyncio.to_thread(
            self._artifacts.put_json,
            f"runs/{ctx.run_id}/verdict.json",
            {  # what the Proof Bundle records about the decisions
                "schema": "p2p.verdicts/v1",
                "attempt": manifest.attempt,
                "config": self._config.model_dump(mode="json"),
                "config_sha256": self._config_sha,
                "verdicts": decided,
            },
        )
        await self._bundle(ctx)
        return StepResult(next=RunStatus.COMPLETED)

    async def _decide(
        self,
        ctx: RunContext,
        claim: Claim,
        analysis: Analysis,
        manifest: ExecManifest,
        plan: Plan,
        units: dict[str, str],
        unmeasured: dict[str, str],
        where: tuple[str | None, str | None] = (None, None),
    ) -> dict[str, Any]:
        """One claim: decide() on its measurements (none if it was not measured), the reason
        adjusted by code, the Limitations by code, the Judge's prose only as explanation."""
        measured = analysis.claims.get(claim.id)
        unit = units.get(claim.id)
        if measured is None:  # declared unmeasured at finish (or not in this job)
            empty = Measurements(
                scale=Scale.FULL if manifest.scale.full else Scale.REDUCED,
                per_seed=[],
                baseline_per_seed=[] if claim.claim_type is ClaimType.RELATIVE else None,
            )
            measurements, scale_factor, scale_note = empty, 1.0, None
            verdict = not_measured(decide(claim, empty, self._config))
            await ctx.emit(  # a claim left out shows in the run, not only in its Limitations
                EventType.WARNING,
                {
                    "code": "CLAIM_NOT_MEASURED",
                    "message": f"claim {claim.id} was not measured by the experiment",
                },
            )
        else:
            # deterministic percent/fraction alignment from the unit declared and checked at
            # finish, noted in the Limitations (verdict/scale.py); runs from before it: no unit
            measurements, scale_factor, scale_note = align_to_claim(
                claim, measured.measurements, _UNITS.get(unit or "")
            )
            verdict = adjust_reason(decide(claim, measurements, self._config), analysis.exit_reason)
        limits = limitations(
            scale=manifest.scale,
            planned_seeds=manifest.seeds,
            used_seeds=measured.used_seeds if measured else [],
            gpu=analysis.gpu,
            exit_reason=analysis.exit_reason,
            deviations=plan.notes,
            scale_note=scale_note,
            unmeasured_reason=unmeasured.get(claim.id, "") if measured is None else None,
            platform=where[0],
            preset=where[1],
        )
        text = (
            await self._explain(ctx, verdict, json.dumps(claim.model_dump(mode="json")), limits)
            if measured is not None  # nothing to explain beyond the code's own reason
            else None
        )
        if text is None or contradicts(text, verdict.kind):
            text = template_explanation(verdict, claim)
        explanation = f"{text.strip()}\n\n### Limitations\n\n{limits}"
        await ctx.store.save_verdict(
            ctx.run_id,
            claim.id,
            verdict,
            measurements.scale,
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
        return {
            "claim": claim.model_dump(mode="json"),
            "verdict": verdict.model_dump(mode="json"),
            "measurements": measurements.model_dump(mode="json") if measured else None,
            "measured": measured.measurements.model_dump(mode="json") if measured else None,
            "scale_factor": scale_factor,
            "unit": unit,
            "limitations": limits,
            "explanation_md": explanation,
        }


_UNITS: dict[str, MetricUnit] = {"fraction": "fraction", "percent": "percent", "raw": "raw"}


def _declared(smoke: Any) -> tuple[dict[str, str], dict[str, str]]:
    """What the Coder declared at finish: each measured claim's unit, each unmeasured claim's
    reason (model text, shown only through the code-written Limitations)."""
    if not isinstance(smoke, dict):
        return {}, {}
    units = {
        str(m["claim_id"]): str(m["unit"])
        for m in smoke.get("measures") or []
        if isinstance(m, dict) and "claim_id" in m and "unit" in m
    }
    unmeasured = {
        str(u["claim_id"]): str(u.get("reason") or "")
        for u in smoke.get("unmeasured") or []
        if isinstance(u, dict) and "claim_id" in u
    }
    return units, unmeasured

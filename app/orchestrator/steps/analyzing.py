"""ANALYZING: read results.json from the run bucket and turn it into Measurements, treating it
as untrusted input (execution-contracts §7.1). A broken contract fails the run with
CONTRACT_ERROR; every other exit reason goes on to VERDICT, which accounts for it.

The Utility model may summarize the end of live.log for display; the summary is stored next to
the analysis and never feeds the verdict (invariant 2)."""

import asyncio
from typing import Any

from app.domain.contracts import ExecManifest
from app.domain.errors import P2PError
from app.domain.models import ErrorCode, StepName
from app.execution.run_storage import RunStorage
from app.models.client import Message, ModelClient, Role
from app.models.prompt_files import load_prompt
from app.models.untrusted import UNTRUSTED_NOTICE, wrap_untrusted
from app.orchestrator.budget import BudgetExceeded, KillSwitchActive
from app.orchestrator.context import RunContext
from app.orchestrator.fsm import StepResult
from app.orchestrator.steps.common import output_of
from app.orchestrator.steps.executing import printable
from app.storage.artifacts import ArtifactStore
from app.verdict.explanation import mentions_verdict
from app.verdict.results import MAX_RESULTS_BYTES, Analysis, analyze_results, no_results

MAX_LOG_BYTES = 512 * 1024  # live.log: the runner's last 200 lines
LOG_CHARS = 12_000
SUMMARY_CHARS = 1_000


class AnalyzingStep:
    name = StepName.ANALYZING

    def __init__(
        self, artifacts: ArtifactStore, storage: RunStorage, models: ModelClient | None = None
    ) -> None:
        self._artifacts = artifacts
        self._storage = storage
        self._models = models

    async def _log_summary(self, ctx: RunContext, attempt: int) -> str | None:
        if self._models is None:
            return None
        try:
            raw = await self._storage.get_bytes(
                ctx.run_id, f"results/{attempt}/live.log", max_bytes=MAX_LOG_BYTES
            )
        except ValueError:
            return None
        if not raw:
            return None
        text = raw.decode("utf-8", "replace")[-LOG_CHARS:]
        prompt = load_prompt("utility_parse_log").render(
            log=wrap_untrusted(text, source="job log", max_chars=LOG_CHARS),
            untrusted_notice=UNTRUSTED_NOTICE,
        )
        try:
            completion = await self._models.complete(
                Role.UTILITY,
                [Message(role="user", content=prompt)],
                run_id=ctx.run_id,
                step=ctx.step.value,
                prompt="utility_parse_log",
            )
        except (P2PError, BudgetExceeded, KillSwitchActive):
            return None  # display only: it never costs the measurement
        summary = printable(completion.content or "").strip()[:SUMMARY_CHARS]
        return None if not summary or mentions_verdict(summary) else summary

    async def run(self, ctx: RunContext) -> StepResult:
        execution: dict[str, Any] = await asyncio.to_thread(
            self._artifacts.get_json, await output_of(ctx, StepName.EXECUTING)
        )
        manifest = ExecManifest.model_validate(execution["manifest"])  # written by us
        if execution.get("results_key") is None:  # EXECUTING found no results to read
            reason = execution.get("exit_reason")
            analysis = no_results(
                manifest, "budget_timeout" if reason == "budget_timeout" else "experiment_error"
            )
        else:
            analysis = await self._analyze(ctx, manifest, str(execution["results_key"]))
        if analysis.exit_reason == "contract_error":
            raise P2PError(ErrorCode.CONTRACT_ERROR, analysis.problem or "results.json is invalid")
        key = f"runs/{ctx.run_id}/analysis_{manifest.attempt}.json"
        await asyncio.to_thread(
            self._artifacts.put_json,
            key,
            {
                "analysis": analysis.model_dump(mode="json"),
                "manifest": execution["manifest"],
                "log_summary": await self._log_summary(ctx, manifest.attempt),
            },
        )
        return StepResult(next=StepName.VERDICT, output_ref=key)

    async def _analyze(self, ctx: RunContext, manifest: ExecManifest, key: str) -> Analysis:
        try:
            raw = await self._storage.get_bytes(ctx.run_id, key, max_bytes=MAX_RESULTS_BYTES)
        except ValueError:
            raw = b"x" * (MAX_RESULTS_BYTES + 1)  # over the cap: a contract error
        analysis: Analysis = await ctx.run_cpu(analyze_results, raw or b"", manifest)
        return analysis

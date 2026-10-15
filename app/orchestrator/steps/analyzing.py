"""ANALYZING: read results.json from the run bucket and turn it into Measurements, treating it
as untrusted input (execution-contracts §7.1). A broken contract fails the run with
CONTRACT_ERROR; every other exit reason goes on to VERDICT, which accounts for it."""

import asyncio
from typing import Any

from app.domain.contracts import ExecManifest
from app.domain.errors import P2PError
from app.domain.models import ErrorCode, StepName
from app.execution.run_storage import RunStorage
from app.orchestrator.context import RunContext
from app.orchestrator.fsm import StepResult
from app.orchestrator.steps.common import output_of
from app.storage.artifacts import ArtifactStore
from app.verdict.results import MAX_RESULTS_BYTES, analyze_results


class AnalyzingStep:
    name = StepName.ANALYZING

    def __init__(self, artifacts: ArtifactStore, storage: RunStorage) -> None:
        self._artifacts = artifacts
        self._storage = storage

    async def run(self, ctx: RunContext) -> StepResult:
        execution: dict[str, Any] = await asyncio.to_thread(
            self._artifacts.get_json, await output_of(ctx, StepName.EXECUTING)
        )
        manifest = ExecManifest.model_validate(execution["manifest"])  # written by us
        try:
            raw = await self._storage.get_bytes(
                ctx.run_id, str(execution["results_key"]), max_bytes=MAX_RESULTS_BYTES
            )
        except ValueError:
            raw = b"x" * (MAX_RESULTS_BYTES + 1)  # over the cap: a contract error below
        analysis = await ctx.run_cpu(analyze_results, raw or b"", manifest)
        if analysis.exit_reason == "contract_error":
            raise P2PError(ErrorCode.CONTRACT_ERROR, analysis.problem or "results.json is invalid")
        key = f"runs/{ctx.run_id}/analysis_{manifest.attempt}.json"
        await asyncio.to_thread(
            self._artifacts.put_json,
            key,
            {"analysis": analysis.model_dump(mode="json"), "manifest": execution["manifest"]},
        )
        return StepResult(next=StepName.VERDICT, output_ref=key)

"""The conditions a run's verdict was reached under (invariant 10: "Not reproduced under our
conditions" is always shown next to them). Read from what the run stores, all written by our
own code: the plan (PLANNING), the exec manifest and the analysis of the results (ANALYZING),
and how the GPU job was submitted (EXECUTING). Display only: anything missing or unreadable
gives fewer conditions, never an error."""

import asyncio
import logging
from typing import Any

from pydantic import BaseModel, ValidationError

from app.domain.contracts import ExecManifest
from app.domain.models import (
    GpuFallback,
    Plan,
    PlanArtifact,
    PlanMode,
    Scale,
    SetupRecord,
    StepName,
)
from app.orchestrator.fallback import fallback_key
from app.orchestrator.setup_check import clean
from app.storage.artifacts import ArtifactStore
from app.storage.store import StepRecord, Store
from app.verdict.explanation import mentions_verdict
from app.verdict.limitations import WITHHELD
from app.verdict.results import Analysis, ExitReason

log = logging.getLogger(__name__)


class ScaleView(BaseModel):
    scale: Scale  # of the measurements (equal to the manifest's, checked in ANALYZING)
    fraction_of_data: float | None
    epochs: int | None
    paper_epochs: int | None


class HardwareView(BaseModel):
    platform: str | None  # Nebius platform and preset the GPU job was submitted with
    preset: str | None
    preemptible: bool | None
    gpu: str | None  # the GPU the runner reported (plain text, validated in ANALYZING)


class RunConditions(BaseModel):
    attempt: int  # the GPU job attempt the verdict is based on
    scale: ScaleView
    seeds_planned: list[int]
    seeds_used: list[int]  # seeds that gave at least one claim a value
    seeds_used_by_claim: dict[str, list[int]]  # by claim id: a seed may fail one claim only
    # the planned seeds the time rule left out (A-46): never run, so never failed
    seeds_not_started: list[int] = []
    hardware: HardwareView
    code: PlanMode | None  # None when the plan can't be read
    deviations: list[str]  # deviations from the paper recorded in the plan
    # what the run used for each condition of the plan's setup; the name and the values
    # are the paper's and the Coder's words (model text, bounded): shown as plain text
    setup: list[SetupRecord]
    exit_reason: ExitReason
    # A-47: why a run planned for the GPU ran its CPU plan (control-plane text), or None
    gpu_fallback: str | None = None


class _Hardware(BaseModel):
    platform: str
    preset: str
    preemptible: bool


def _latest_refs(steps: list[StepRecord]) -> dict[StepName, str]:
    """output_ref of the latest successful attempt of each step."""
    refs: dict[StepName, tuple[int, str]] = {}
    for step in steps:
        if step.status != "SUCCEEDED" or not step.output_ref:
            continue
        if step.name not in refs or step.attempt > refs[step.name][0]:
            refs[step.name] = (step.attempt, step.output_ref)
    return {name: ref for name, (_, ref) in refs.items()}


async def _read(artifacts: ArtifactStore, key: str | None) -> dict[str, Any] | None:
    if key is None:
        return None
    try:
        data = await asyncio.to_thread(artifacts.get_json, key)
    except FileNotFoundError:
        return None
    except Exception as err:  # unreadable or store unavailable: the run view still loads
        log.warning("run artifact not readable: %s", type(err).__name__)
        return None
    return data if isinstance(data, dict) else None


async def run_conditions(
    store: Store, artifacts: ArtifactStore | None, run_id: str
) -> RunConditions | None:
    """None until the results of a GPU job have been analyzed (or when they can't be read)."""
    if artifacts is None:
        return None
    try:
        return await _conditions(store, artifacts, run_id)
    except Exception as err:  # display only: the run view always loads
        log.warning("run conditions not built: %s", type(err).__name__, extra={"run_id": run_id})
        return None


def _shown_gpu(gpu: str | None) -> str | None:
    """The GPU name comes from results.json, written next to untrusted code: never shown when
    it reads like a verdict."""
    return None if gpu is None or mentions_verdict(gpu) else gpu


async def _conditions(store: Store, artifacts: ArtifactStore, run_id: str) -> RunConditions | None:
    refs = _latest_refs(await store.get_steps(run_id))
    analyzed = await _read(artifacts, refs.get(StepName.ANALYZING))
    if analyzed is None:
        return None
    try:
        analysis = Analysis.model_validate(analyzed.get("analysis"))
        manifest = ExecManifest.model_validate(analyzed.get("manifest"))
    except ValidationError:
        log.warning("run conditions not readable", extra={"run_id": run_id})
        return None
    if not analysis.claims:  # a contract error: nothing was measured
        return None
    fallback = await _fallback(artifacts, run_id)
    plan = await _plan(artifacts, refs.get(StepName.PLANNING), fell_back=fallback is not None)
    hardware = await _hardware(artifacts, refs.get(StepName.EXECUTING), manifest.attempt)
    return RunConditions(
        attempt=manifest.attempt,
        scale=ScaleView(
            scale=Scale.FULL if manifest.scale.full else Scale.REDUCED,
            fraction_of_data=manifest.scale.fraction_of_data,
            epochs=manifest.scale.epochs,
            paper_epochs=manifest.scale.paper_epochs,
        ),
        seeds_planned=manifest.seeds,
        seeds_used=analysis.used_seeds,
        seeds_used_by_claim={ref: c.used_seeds for ref, c in analysis.claims.items()},
        seeds_not_started=analysis.seeds_not_started,
        hardware=HardwareView(
            platform=hardware.platform if hardware else None,
            preset=hardware.preset if hardware else None,
            preemptible=hardware.preemptible if hardware else None,
            gpu=_shown_gpu(analysis.gpu),
        ),
        code=plan.mode if plan else None,
        deviations=plan.notes if plan else [],
        setup=_setup(await _read(artifacts, refs.get(StepName.SMOKE_TESTING))),
        exit_reason=analysis.exit_reason,
        gpu_fallback=fallback.message if fallback is not None else None,
    )


def _setup(smoke: dict[str, Any] | None) -> list[SetupRecord]:
    """What `finish` recorded for the plan's setup (smoke_result.json); records that cannot be
    read are left out, like every other part of the conditions."""
    raw = smoke.get("setup") if smoke else None
    try:
        records = [SetupRecord.model_validate(r) for r in raw] if isinstance(raw, list) else []
    except ValidationError:
        log.warning("setup records not readable")
        return []
    return [
        record.model_copy(
            update={
                "name": _shown(record.name),
                "paper": _shown(record.paper),
                "planned": _shown(record.planned),
                "used": _shown(record.used),
                "where": _shown(record.where),
            }
        )
        for record in records
    ]


def _shown(text: str) -> str:
    """A setup record's text is the paper's or the Coder's: shown clean, on one line, and
    never when it reads like a verdict (as for the GPU name)."""
    cleaned = clean(text)
    return WITHHELD if not cleaned or mentions_verdict(cleaned) else cleaned


async def _plan(artifacts: ArtifactStore, key: str | None, *, fell_back: bool) -> Plan | None:
    """The plan that ran: PLANNING's, or its CPU plan after the GPU fallback (A-47)."""
    stored = await _read(artifacts, key)
    try:
        artifact = PlanArtifact.model_validate(stored) if stored else None
    except ValidationError:
        return None
    if artifact is None:
        return None
    return artifact.cpu_plan if fell_back and artifact.cpu_plan is not None else artifact.plan


async def _fallback(artifacts: ArtifactStore, run_id: str) -> GpuFallback | None:
    """EXECUTING's record of a GPU run that ran its CPU plan, if it fell back (A-47)."""
    stored = await _read(artifacts, fallback_key(run_id))
    try:
        return GpuFallback.model_validate(stored) if stored else None
    except ValidationError:
        return None


async def _hardware(artifacts: ArtifactStore, key: str | None, attempt: int) -> _Hardware | None:
    """How the judged attempt's job was submitted. EXECUTING points at its last attempt: the
    judged one is the same (ANALYZING reads that output), checked through the manifest."""
    execution = await _read(artifacts, key)
    if execution is None:
        return None
    manifest = execution.get("manifest")
    if not isinstance(manifest, dict) or manifest.get("attempt") != attempt:
        return None
    try:
        return _Hardware.model_validate(execution.get("hardware"))
    except ValidationError:
        return None

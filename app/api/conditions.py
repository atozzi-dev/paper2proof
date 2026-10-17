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
from app.domain.models import Plan, PlanMode, Scale, StepName
from app.storage.artifacts import ArtifactStore
from app.storage.store import StepRecord, Store
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
    seeds_used: list[int]
    hardware: HardwareView
    code: PlanMode | None  # None when the plan can't be read
    deviations: list[str]  # deviations from the paper recorded in the plan
    exit_reason: ExitReason


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
    if analysis.measurements is None:
        return None
    plan = await _plan(artifacts, refs.get(StepName.PLANNING))
    hardware = await _hardware(artifacts, refs.get(StepName.EXECUTING), manifest.attempt)
    return RunConditions(
        attempt=manifest.attempt,
        scale=ScaleView(
            scale=analysis.measurements.scale,
            fraction_of_data=manifest.scale.fraction_of_data,
            epochs=manifest.scale.epochs,
            paper_epochs=manifest.scale.paper_epochs,
        ),
        seeds_planned=manifest.seeds,
        seeds_used=analysis.used_seeds,
        hardware=HardwareView(
            platform=hardware.platform if hardware else None,
            preset=hardware.preset if hardware else None,
            preemptible=hardware.preemptible if hardware else None,
            gpu=analysis.gpu,
        ),
        code=plan.mode if plan else None,
        deviations=plan.notes if plan else [],
        exit_reason=analysis.exit_reason,
    )


async def _plan(artifacts: ArtifactStore, key: str | None) -> Plan | None:
    stored = await _read(artifacts, key)
    try:
        return Plan.model_validate(stored.get("plan")) if stored else None
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

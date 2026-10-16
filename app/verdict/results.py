"""results.json -> Measurements (docs/specs/execution-contracts.md §7.1).

The file is written by the same job that runs untrusted code, so it is untrusted input: size
cap, schema, every identifying field equal to the stored manifest, seeds unique and from the
manifest. Any violation turns the run into `contract_error`. Scale and direction come from the
manifest and the stored claim, never from the file. Pure (runs in the CPU pool).
"""

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict

from app.domain.contracts import ExecManifest
from app.domain.models import ClaimType, Measurements, Scale

MAX_RESULTS_BYTES = 256 * 1024
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas" / "results.schema.json"
_GPU = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._()/+-]{0,63}")  # shown as hardware: plain text only
ExitReason = Literal["completed", "experiment_error", "budget_timeout", "contract_error"]


class Analysis(BaseModel):
    model_config = ConfigDict(frozen=True)

    exit_reason: ExitReason
    measurements: Measurements | None
    used_seeds: list[int]
    wall_clock_s: float | None = None
    gpu: str | None = None
    problem: str | None = None


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))


def _no_constants(name: str) -> float:
    raise ValueError(f"{name} is not a number")


def _contract(problem: str) -> Analysis:
    return Analysis(exit_reason="contract_error", measurements=None, used_seeds=[], problem=problem)


def _seeds_ok(entries: list[dict[str, Any]], allowed: set[int]) -> bool:
    seeds = [e["seed"] for e in entries]
    return len(seeds) == len(set(seeds)) and set(seeds) <= allowed


def analyze_results(raw: bytes, manifest: ExecManifest) -> Analysis:
    if len(raw) > MAX_RESULTS_BYTES:
        return _contract("results.json is larger than 256 KiB")
    try:
        data: Any = json.loads(raw, parse_constant=_no_constants)
    except (ValueError, RecursionError):
        return _contract("results.json is not valid JSON")
    error = next(iter(_validator().iter_errors(data)), None)
    if error is not None:
        return _contract(f"results.json does not match its schema: {error.message[:200]}")
    expected = {
        "run_id": manifest.run_id,
        "attempt": manifest.attempt,
        "claim_ref": manifest.claim_ref,
        "claim_type": manifest.claim_type.value,
        "metric": manifest.metric,
        "higher_is_better": manifest.higher_is_better,
    }
    for key, value in expected.items():
        if data[key] != value:
            return _contract(f"results.json {key} does not match the manifest")
    scale = {k: v for k, v in data["scale"].items() if v is not None}
    if scale != manifest.scale.model_dump(exclude_none=True):
        return _contract("results.json scale does not match the manifest")
    relative = manifest.claim_type is ClaimType.RELATIVE
    baseline = data.get("baseline")
    if relative != isinstance(baseline, dict):
        return _contract("results.json baseline does not match the claim type")
    if relative and baseline["name"] != manifest.baseline_name:
        return _contract("results.json baseline name does not match the manifest")
    allowed = set(manifest.seeds)
    if not _seeds_ok(data["per_seed"], allowed) or (
        relative and not _seeds_ok(baseline["per_seed"], allowed)
    ):
        return _contract("results.json seeds are duplicated or not in the manifest")

    values = {
        e["seed"]: float(e["value"])
        for e in data["per_seed"]
        if e.get("ok") is not False and e["value"] is not None
    }
    base = (
        {e["seed"]: float(e["value"]) for e in baseline["per_seed"] if e["value"] is not None}
        if relative
        else {}
    )
    used = sorted(s for s in values if not relative or s in base)
    measurements = Measurements(
        scale=Scale.FULL if manifest.scale.full else Scale.REDUCED,
        per_seed=[values[s] for s in used],
        baseline_per_seed=[base[s] for s in used] if relative else None,
    )
    return Analysis(
        exit_reason=data["exit_reason"],
        measurements=measurements,
        used_seeds=used,
        wall_clock_s=float(data["wall_clock_s"]),
        gpu=data["gpu"] if _GPU.fullmatch(data["gpu"]) else "unknown",
    )


def no_results(manifest: ExecManifest, exit_reason: ExitReason) -> Analysis:
    """The exec job ended without a results.json (preempted with no retry, or it died before
    writing one): no seed counts, and the ending explains why (VERDICT adjusts the reason)."""
    relative = manifest.claim_type is ClaimType.RELATIVE
    measurements = Measurements(
        scale=Scale.FULL if manifest.scale.full else Scale.REDUCED,
        per_seed=[],
        baseline_per_seed=[] if relative else None,
    )
    return Analysis(exit_reason=exit_reason, measurements=measurements, used_seeds=[])

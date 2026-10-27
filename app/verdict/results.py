"""results.json -> Measurements (docs/specs/execution-contracts.md §7.1).

The file is written by the same job that runs untrusted code, so it is untrusted input: size
cap, schema, every identifying field equal to the stored manifest, seeds unique and from the
manifest, every claim of the manifest in the same order. Any violation turns the run into
`contract_error`. Scale and direction come from the manifest and the stored claim, never from
the file. Pure (runs in the CPU pool).
"""

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict

from app.domain.contracts import ClaimSpec, ExecManifest
from app.domain.models import ClaimType, Measurements, Scale

MAX_RESULTS_BYTES = 256 * 1024
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas" / "results.schema.json"
_GPU = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._()/+-]{0,63}")  # shown as hardware: plain text only
# `capacity` never comes from results.json: EXECUTING found no GPU to run the job on.
ExitReason = Literal[
    "completed", "experiment_error", "budget_timeout", "contract_error", "capacity"
]


class ClaimAnalysis(BaseModel):
    model_config = ConfigDict(frozen=True)

    measurements: Measurements
    used_seeds: list[int]


class Analysis(BaseModel):
    model_config = ConfigDict(frozen=True)

    exit_reason: ExitReason
    claims: dict[str, ClaimAnalysis]  # by claim_ref, in the manifest's order; empty on error
    wall_clock_s: float | None = None
    gpu: str | None = None
    problem: str | None = None

    @property
    def used_seeds(self) -> list[int]:
        """Every seed that gave at least one claim a usable value (the run's conditions)."""
        return sorted({s for c in self.claims.values() for s in c.used_seeds})


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))


def _no_constants(name: str) -> float:
    raise ValueError(f"{name} is not a number")


def _contract(problem: str) -> Analysis:
    return Analysis(exit_reason="contract_error", claims={}, problem=problem)


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
    for key in ("run_id", "attempt"):
        if data[key] != getattr(manifest, key):
            return _contract(f"results.json {key} does not match the manifest")
    scale = {k: v for k, v in data["scale"].items() if v is not None}
    if scale != manifest.scale.model_dump(exclude_none=True):
        return _contract("results.json scale does not match the manifest")
    if [c["claim_ref"] for c in data["claims"]] != [c.claim_ref for c in manifest.claims]:
        return _contract("results.json claims do not match the manifest")
    claims: dict[str, ClaimAnalysis] = {}
    for spec, entry in zip(manifest.claims, data["claims"], strict=True):
        analysed = _claim(spec, entry, set(manifest.seeds), manifest.scale.full)
        if isinstance(analysed, str):
            return _contract(f"results.json claim {spec.claim_ref}: {analysed}")
        claims[spec.claim_ref] = analysed
    return Analysis(
        exit_reason=data["exit_reason"],
        claims=claims,
        wall_clock_s=float(data["wall_clock_s"]),
        gpu=data["gpu"] if _GPU.fullmatch(data["gpu"]) else "unknown",
    )


def _claim(
    spec: ClaimSpec, entry: dict[str, Any], allowed: set[int], full: bool
) -> ClaimAnalysis | str:
    """One claim's measurements, or what does not match its manifest entry."""
    expected = {
        "claim_type": spec.claim_type.value,
        "metric": spec.metric,
        "metric_key": spec.metric_key,
        "higher_is_better": spec.higher_is_better,
    }
    for key, value in expected.items():
        if entry[key] != value:
            return f"{key} does not match the manifest"
    relative = spec.claim_type is ClaimType.RELATIVE
    baseline = entry["baseline"]
    if relative != isinstance(baseline, dict):
        return "baseline does not match the claim type"
    if relative and (baseline["name"], baseline["key"]) != (spec.baseline_name, spec.baseline_key):
        return "baseline does not match the manifest"
    if not _seeds_ok(entry["per_seed"], allowed) or (
        relative and not _seeds_ok(baseline["per_seed"], allowed)
    ):
        return "seeds are duplicated or not in the manifest"
    try:  # a JSON integer can be too large for a float: the runner never writes one
        values = {
            e["seed"]: float(e["value"])
            for e in entry["per_seed"]
            if e.get("ok") is not False and e["value"] is not None
        }
        base = (
            {e["seed"]: float(e["value"]) for e in baseline["per_seed"] if e["value"] is not None}
            if relative
            else {}
        )
    except OverflowError:
        return "a value is out of range"
    used = sorted(s for s in values if not relative or s in base)
    return ClaimAnalysis(
        measurements=Measurements(
            scale=Scale.FULL if full else Scale.REDUCED,
            per_seed=[values[s] for s in used],
            baseline_per_seed=[base[s] for s in used] if relative else None,
        ),
        used_seeds=used,
    )


def no_results(manifest: ExecManifest, exit_reason: ExitReason) -> Analysis:
    """The exec job ended without a results.json (preempted with no retry, it died before
    writing one, or it never ran for lack of GPU capacity): no seed counts for any claim, and
    the ending explains why (VERDICT adjusts the reason)."""
    scale = Scale.FULL if manifest.scale.full else Scale.REDUCED
    claims = {
        spec.claim_ref: ClaimAnalysis(
            measurements=Measurements(
                scale=scale,
                per_seed=[],
                baseline_per_seed=[] if spec.claim_type is ClaimType.RELATIVE else None,
            ),
            used_seeds=[],
        )
        for spec in manifest.claims
    }
    return Analysis(exit_reason=exit_reason, claims=claims)

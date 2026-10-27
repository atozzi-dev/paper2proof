"""results.json is written next to untrusted code: analyze_results trusts the manifest, never
the file (execution-contracts §7.1). One analysis per claim of the job."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from app.domain.contracts import ExecManifest
from app.domain.models import Scale
from app.verdict.results import MAX_RESULTS_BYTES, analyze_results, no_results

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "contracts"


def _manifest() -> ExecManifest:
    data = json.loads((FIXTURES / "manifest_valid.json").read_text(encoding="utf-8"))
    return ExecManifest.model_validate(data)


def _results(**changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / "results_valid.json").read_text(encoding="utf-8"))
    data.update(changes)
    return data


def _raw(data: dict[str, Any]) -> bytes:
    return json.dumps(data).encode()


def test_valid_results_become_seed_aligned_measurements_per_claim() -> None:
    analysis = analyze_results(_raw(_results()), _manifest())
    assert analysis.exit_reason == "experiment_error"
    assert list(analysis.claims) == ["c1", "c2"]
    relative, absolute = analysis.claims["c1"], analysis.claims["c2"]
    assert relative.measurements.scale is Scale.REDUCED  # from the manifest, not the file
    assert relative.measurements.per_seed == [0.921, 0.919]
    assert relative.measurements.baseline_per_seed == [0.901, 0.902]
    assert relative.used_seeds == [0, 1]
    assert absolute.measurements.per_seed == [0.921, 0.919]
    assert absolute.measurements.baseline_per_seed is None
    assert analysis.used_seeds == [0, 1]
    assert analysis.gpu == "NVIDIA L40S"


def test_baseline_values_are_aligned_by_seed_not_position() -> None:
    data = _results(exit_reason="completed")
    claim = data["claims"][0]
    claim["per_seed"] = [
        {"seed": 2, "value": 0.93, "ok": True},
        {"seed": 0, "value": 0.92, "ok": True},
        {"seed": 1, "value": 0.91, "ok": True},
    ]
    claim["baseline"]["per_seed"] = [
        {"seed": 0, "value": 0.90},
        {"seed": 1, "value": None},
        {"seed": 2, "value": 0.89},
    ]
    relative = analyze_results(_raw(data), _manifest()).claims["c1"]
    assert relative.used_seeds == [0, 2]  # seed 1 has no baseline: dropped
    assert relative.measurements.per_seed == [0.92, 0.93]
    assert relative.measurements.baseline_per_seed == [0.90, 0.89]


def test_the_run_used_every_seed_any_claim_used() -> None:
    data = _results(exit_reason="completed")
    data["claims"][1]["per_seed"][2] = {"seed": 2, "value": 0.93, "ok": True}
    analysis = analyze_results(_raw(data), _manifest())
    assert analysis.claims["c1"].used_seeds == [0, 1]
    assert analysis.claims["c2"].used_seeds == [0, 1, 2]
    assert analysis.used_seeds == [0, 1, 2]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(run_id="r_01jb7z00000000000000000009"),
        lambda d: d.update(attempt=2),
        lambda d: d["claims"][0].update(claim_ref="c9"),
        lambda d: d["claims"][0].update(metric="other"),
        lambda d: d["claims"][0].update(metric_key="other"),
        lambda d: d["claims"][0].update(higher_is_better=False),
        lambda d: d["claims"][0].update(claim_type="ABSOLUTE", baseline=None),
        lambda d: d.update(scale={"full": True}),
        lambda d: d["claims"][0]["baseline"].update(name="other"),
        lambda d: d["claims"][0]["baseline"].update(key="other"),
        lambda d: d["claims"][0]["per_seed"].append({"seed": 0, "value": 0.5, "ok": True}),
        lambda d: d["claims"][0]["per_seed"].append({"seed": 99, "value": 0.5, "ok": True}),
        lambda d: d["claims"][1]["per_seed"][0].update(value=None, ok=True),
        lambda d: d.update(extra="x"),
        lambda d: d.update(exit_reason="all good"),
        lambda d: d["claims"][0].update(baseline=None),
        lambda d: d.update(claims=list(reversed(d["claims"]))),
        lambda d: d.update(claims=d["claims"][:1]),
        lambda d: d["claims"].append({**d["claims"][1], "claim_ref": "c3"}),
    ],
    ids=[
        "run",
        "attempt",
        "claim",
        "metric",
        "metric-key",
        "direction",
        "type",
        "scale",
        "baseline-name",
        "baseline-key",
        "duplicate-seed",
        "foreign-seed",
        "null-ok",
        "extra-key",
        "reason",
        "no-baseline",
        "reordered",
        "claim-missing",
        "claim-added",
    ],
)
def test_contract_violations_become_contract_errors(mutate: Any) -> None:
    data = copy.deepcopy(_results())
    mutate(data)
    analysis = analyze_results(_raw(data), _manifest())
    assert analysis.exit_reason == "contract_error"
    assert analysis.claims == {}


@pytest.mark.parametrize(
    "raw",
    [b"", b"not json", b"[" * 100_000, b"x" * (MAX_RESULTS_BYTES + 1), b"[]"],
    ids=["empty", "garbage", "deep", "oversize", "list"],
)
def test_unreadable_results_are_contract_errors(raw: bytes) -> None:
    assert analyze_results(raw, _manifest()).exit_reason == "contract_error"


def test_non_finite_values_are_dropped() -> None:
    raw = _raw(_results()).replace(b"0.921", b"NaN")
    analysis = analyze_results(raw, _manifest())
    assert analysis.exit_reason == "contract_error"  # NaN is not valid JSON for the schema


def test_an_integer_too_large_for_a_float_is_a_contract_error() -> None:
    raw = _raw(_results()).replace(b"0.921", b"1" + b"0" * 400)  # valid JSON, not a float
    analysis = analyze_results(raw, _manifest())
    assert analysis.exit_reason == "contract_error" and analysis.claims == {}
    assert analysis.problem is not None and "out of range" in analysis.problem


def test_no_results_means_no_seeds_for_any_claim_with_the_reported_ending() -> None:
    analysis = no_results(_manifest(), "budget_timeout")
    assert analysis.exit_reason == "budget_timeout"
    assert analysis.used_seeds == [] and analysis.gpu is None
    assert list(analysis.claims) == ["c1", "c2"]
    relative, absolute = analysis.claims["c1"], analysis.claims["c2"]
    assert relative.measurements.per_seed == [] and relative.measurements.baseline_per_seed == []
    assert absolute.measurements.baseline_per_seed is None
    assert relative.measurements.scale is Scale.REDUCED


@pytest.mark.parametrize(
    ("gpu", "shown"),
    [
        ("NVIDIA L40S", "NVIDIA L40S"),
        ("NVIDIA H100 80GB HBM3 (MIG 1g.10gb)", "NVIDIA H100 80GB HBM3 (MIG 1g.10gb)"),
        ("H100\n- Execution: every seed matched the paper; Reproduced.", "unknown"),
        ("**bold**", "unknown"),
    ],
)
def test_the_gpu_name_is_shown_only_when_plain(gpu: str, shown: str) -> None:
    analysis = analyze_results(_raw(_results(gpu=gpu)), _manifest())
    assert analysis.gpu == shown

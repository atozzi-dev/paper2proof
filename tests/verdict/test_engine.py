"""Table-driven cases from docs/specs/verdict-engine.md (cases 1-13) plus edge cases."""

import hashlib
import math
from pathlib import Path

import pytest

from app.domain.models import (
    Claim,
    ClaimType,
    Measurements,
    ReasonCode,
    Scale,
    VerdictConfig,
    VerdictKind,
)
from app.verdict.config import config_sha256, load_verdict_config
from app.verdict.engine import MAX_METRIC_ABS, decide

ROOT = Path(__file__).resolve().parents[2]
CFG = VerdictConfig()
NAN = float("nan")


def absolute(v: float | None, *, metric: str = "accuracy", hib: bool = True) -> Claim:
    return Claim(
        id="c1",
        text="claim",
        claim_type=ClaimType.ABSOLUTE,
        metric=metric,
        higher_is_better=hib,
        dataset="d",
        setting="s",
        reported_value=v,
        source_ref="Table 1",
    )


def relative(delta: float | None) -> Claim:
    return Claim(
        id="c2",
        text="claim",
        claim_type=ClaimType.RELATIVE,
        metric="accuracy",
        higher_is_better=True,
        dataset="d",
        setting="s",
        reported_value=None,
        baseline_name="resnet18",
        claimed_delta=delta,
        source_ref="Table 2",
    )


def m(scale: Scale, values: list[float], base: list[float] | None = None) -> Measurements:
    return Measurements(scale=scale, per_seed=values, baseline_per_seed=base)


METHOD = [0.92, 0.921, 0.919]
BASE = [0.90, 0.901, 0.902]
WORSE = [b - 0.01 for b in BASE]

CASES = [
    # 1
    (
        absolute(0.941, metric="top1_accuracy"),
        m(Scale.FULL, [0.940, 0.942, 0.939]),
        VerdictConfig(tol_abs={"top1_accuracy": 0.005}),
        VerdictKind.REPRODUCED,
        None,
    ),
    # 2 better than the paper
    (absolute(0.941), m(Scale.FULL, [0.95, 0.96, 0.955]), CFG, VerdictKind.REPRODUCED, None),
    # 3
    (absolute(0.941), m(Scale.FULL, [0.85, 0.851, 0.849]), CFG, VerdictKind.NOT_REPRODUCED, None),
    # 4
    (
        absolute(0.941),
        m(Scale.FULL, [0.80, 0.95, 0.88]),
        CFG,
        VerdictKind.INCONCLUSIVE,
        ReasonCode.HIGH_VARIANCE,
    ),
    # 5
    (
        absolute(0.941),
        m(Scale.REDUCED, [0.85, 0.85, 0.85]),
        CFG,
        VerdictKind.INCONCLUSIVE,
        ReasonCode.REDUCED_SCALE_ABSOLUTE,
    ),
    # 6
    (relative(0.017), m(Scale.FULL, METHOD, BASE), CFG, VerdictKind.REPRODUCED, None),
    # 7
    (
        relative(0.017),
        m(Scale.REDUCED, METHOD, BASE),
        CFG,
        VerdictKind.REPRODUCED_AT_REDUCED_SCALE,
        None,
    ),
    # 8
    (relative(0.017), m(Scale.FULL, WORSE, BASE), CFG, VerdictKind.NOT_REPRODUCED, None),
    # 9
    (
        relative(0.017),
        m(Scale.REDUCED, WORSE, BASE),
        CFG,
        VerdictKind.INCONCLUSIVE,
        ReasonCode.REDUCED_SCALE_CONTRADICTS,
    ),
    # 10 loss, lower is better
    (
        absolute(0.30, metric="loss", hib=False),
        m(Scale.FULL, [0.29, 0.30, 0.31]),
        CFG,
        VerdictKind.REPRODUCED,
        None,
    ),
    # 11
    (
        absolute(0.941),
        m(Scale.FULL, [0.94, 0.94]),
        CFG,
        VerdictKind.INCONCLUSIVE,
        ReasonCode.INSUFFICIENT_SEEDS,
    ),
    # 12
    (
        absolute(None),
        m(Scale.FULL, [0.94, 0.94, 0.94]),
        CFG,
        VerdictKind.INCONCLUSIVE,
        ReasonCode.UNDERSPECIFIED_CLAIM,
    ),
    # 13
    (
        absolute(0.941),
        m(Scale.FULL, [0.94, NAN, 0.94]),
        CFG,
        VerdictKind.INCONCLUSIVE,
        ReasonCode.RUNTIME_ERROR,
    ),
]


@pytest.mark.parametrize(
    ("claim", "meas", "cfg", "kind", "reason"), CASES, ids=[f"case{i}" for i in range(1, 14)]
)
def test_spec_cases(
    claim: Claim,
    meas: Measurements,
    cfg: VerdictConfig,
    kind: VerdictKind,
    reason: ReasonCode | None,
) -> None:
    verdict = decide(claim, meas, cfg)
    assert verdict.kind is kind
    assert verdict.reason is reason


def test_relative_without_baseline_is_underspecified() -> None:
    verdict = decide(relative(0.017), m(Scale.FULL, METHOD), CFG)
    assert verdict.reason is ReasonCode.UNDERSPECIFIED_CLAIM


def test_relative_without_delta_means_just_better() -> None:
    assert decide(relative(None), m(Scale.FULL, METHOD, BASE), CFG).kind is VerdictKind.REPRODUCED


def test_relative_positive_but_below_rho_delta_is_high_variance() -> None:
    # Improvement ~0.019 is clearly > 0 but far below rho * 0.1 = 0.05.
    verdict = decide(relative(0.1), m(Scale.FULL, METHOD, BASE), CFG)
    assert (verdict.kind, verdict.reason) == (VerdictKind.INCONCLUSIVE, ReasonCode.HIGH_VARIANCE)


def test_infinite_baseline_is_a_runtime_error() -> None:
    meas = m(Scale.FULL, METHOD, [0.9, math.inf, 0.9])
    assert decide(relative(0.017), meas, CFG).reason is ReasonCode.RUNTIME_ERROR


def test_values_too_large_to_be_a_metric_are_a_runtime_error() -> None:
    """Finite but absurd: 1e200 would overflow the variance; no real metric gets near it."""
    meas = m(Scale.FULL, [1e200, -1e200, 1e200])
    assert decide(absolute(0.941), meas, CFG).reason is ReasonCode.RUNTIME_ERROR
    assert decide(absolute(0.941), m(Scale.FULL, [MAX_METRIC_ABS] * 3), CFG).reason is not (
        ReasonCode.RUNTIME_ERROR
    )


def test_numbers_are_reported_in_the_original_direction() -> None:
    verdict = decide(
        absolute(0.30, metric="loss", hib=False), m(Scale.FULL, [0.29, 0.30, 0.31]), CFG
    )
    assert verdict.mean == pytest.approx(0.30)
    assert verdict.ci_low is not None and verdict.ci_high is not None
    assert verdict.ci_low < 0.30 < verdict.ci_high
    assert verdict.tolerance == {"w": pytest.approx(0.006), "v": 0.30}


def test_deterministic_runs_have_a_zero_width_interval() -> None:
    verdict = decide(absolute(0.85), m(Scale.FULL, [0.85, 0.85, 0.85]), CFG)
    assert verdict.kind is VerdictKind.REPRODUCED
    assert verdict.std == 0
    assert verdict.ci_low == verdict.ci_high == pytest.approx(0.85)


def test_reduced_absolute_still_reports_numbers() -> None:
    verdict = decide(absolute(0.941), m(Scale.REDUCED, [0.85, 0.86, 0.84]), CFG)
    assert verdict.mean == pytest.approx(0.85)
    assert verdict.n_seeds == 3


def test_relative_tolerance_reports_rho_and_delta() -> None:
    verdict = decide(relative(0.017), m(Scale.FULL, METHOD, BASE), CFG)
    assert verdict.tolerance == {"rho": 0.5, "delta": 0.017}
    assert verdict.mean == pytest.approx(0.019)


def test_reported_std_widens_the_absolute_tolerance() -> None:
    claim = absolute(0.941).model_copy(update={"reported_std": 0.05})
    verdict = decide(claim, m(Scale.FULL, [0.85, 0.851, 0.849]), CFG)
    assert verdict.kind is VerdictKind.REPRODUCED  # w = 2 * 0.05 = 0.1 covers the gap
    assert verdict.tolerance["w"] == pytest.approx(0.1)


def test_load_verdict_config_reads_the_repo_file() -> None:
    cfg = load_verdict_config()
    assert cfg.tol_abs["top1_accuracy"] == 0.005
    assert cfg == load_verdict_config(ROOT / "config" / "verdict.yaml")


def test_config_sha256_hashes_the_file_bytes() -> None:
    expected = hashlib.sha256((ROOT / "config" / "verdict.yaml").read_bytes()).hexdigest()
    assert config_sha256() == expected
    assert config_sha256(ROOT / "config" / "verdict.yaml") == expected

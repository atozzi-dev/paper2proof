"""Table-driven cases from docs/specs/verdict-engine.md (cases 1-18) plus edge cases."""

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
    # 14 a percent claim measured as a fraction: 100 times off
    (
        absolute(94.1),
        m(Scale.FULL, [0.940, 0.941, 0.939]),
        CFG,
        VerdictKind.INCONCLUSIVE,
        ReasonCode.IMPLAUSIBLE_GAP,
    ),
    # 15 mean 0.277 with a wide CI: the gap, not the variance
    (
        absolute(0.9),
        m(Scale.FULL, [0.01, 0.02, 0.8]),
        CFG,
        VerdictKind.INCONCLUSIVE,
        ReasonCode.IMPLAUSIBLE_GAP,
    ),
    # 16 a falsified claim, 0.90 of the paper's value
    (absolute(0.990), m(Scale.FULL, [0.894, 0.895, 0.896]), CFG, VerdictKind.NOT_REPRODUCED, None),
    # 17 exactly k = 2 times off: inside the band
    (
        absolute(2.0, metric="score"),
        m(Scale.FULL, [1.0, 1.0, 1.0]),
        CFG,
        VerdictKind.NOT_REPRODUCED,
        None,
    ),
    # 18 opposite signs: no ratio
    (absolute(0.5), m(Scale.FULL, [-2.0, -2.1, -1.9]), CFG, VerdictKind.NOT_REPRODUCED, None),
]


@pytest.mark.parametrize(
    ("claim", "meas", "cfg", "kind", "reason"),
    CASES,
    ids=[f"case{i}" for i in range(1, len(CASES) + 1)],
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


NO_GAP_RULE = VerdictConfig(implausible_ratio=None)  # what the verdict was before the rule
PLANTED = [  # the canary set's falsified claims (production eval, 2026-10-29): wrong, not broken
    (absolute(0.990), [0.894, 0.895, 0.896]),
    (absolute(0.920), [0.845, 0.846, 0.847]),
    (absolute(1.300, metric="rmse", hib=False), [1.428, 1.429, 1.430]),
    (absolute(0.900), [0.79, 0.80, 0.81]),
    (absolute(0.910), [0.82, 0.83, 0.84]),
]


@pytest.mark.parametrize(("claim", "values"), PLANTED)
def test_a_claim_off_by_tens_of_percent_is_still_not_reproduced(
    claim: Claim, values: list[float]
) -> None:
    assert decide(claim, m(Scale.FULL, values), CFG).kind is VerdictKind.NOT_REPRODUCED


@pytest.mark.parametrize(
    ("claim", "values"),
    [
        (absolute(94.1), [0.940, 0.941, 0.939]),  # percent claim, fraction measured: ratio 0.01
        (absolute(94.1), [10.0, 10.0, 10.0]),  # a diverged run (chance on 10 classes), scaled
        (absolute(0.30, metric="loss", hib=False), [1.2, 1.25, 1.3]),  # 4 times the paper's loss
        (absolute(-0.5, metric="log_lik"), [-3.0, -3.1, -2.9]),  # both negative, 6 times off
    ],
)
def test_a_measured_value_more_than_k_times_off_is_an_implausible_gap(
    claim: Claim, values: list[float]
) -> None:
    """Broken training or a unit mismatch, not evidence against the paper: never an accusation."""
    assert decide(claim, m(Scale.FULL, values), NO_GAP_RULE).kind is VerdictKind.NOT_REPRODUCED
    verdict = decide(claim, m(Scale.FULL, values), CFG)
    assert (verdict.kind, verdict.reason) == (VerdictKind.INCONCLUSIVE, ReasonCode.IMPLAUSIBLE_GAP)
    assert verdict.mean == pytest.approx(sum(values) / 3)  # the numbers are still reported
    assert verdict.ci_low is not None and verdict.ci_high is not None
    assert verdict.tolerance["v"] == claim.reported_value


def test_a_wide_interval_far_off_the_paper_names_the_gap_not_the_variance() -> None:
    claim, values = absolute(0.9), [0.01, 0.02, 0.8]  # mean 0.277: more than twice lower
    assert decide(claim, m(Scale.FULL, values), NO_GAP_RULE).reason is ReasonCode.HIGH_VARIANCE
    assert decide(claim, m(Scale.FULL, values), CFG).reason is ReasonCode.IMPLAUSIBLE_GAP


@pytest.mark.parametrize(
    ("claim", "values"),
    [
        (absolute(0.5), [-2.0, -2.1, -1.9]),  # opposite signs: no ratio to speak of
        (absolute(0.9), [0.0, 0.0, 0.0]),  # a zero mean
        (absolute(0.0), [-1.0, -1.1, -0.9]),  # a zero paper value
        (absolute(2.0, metric="score"), [1.0, 1.0, 1.0]),  # exactly k times: inside the band
    ],
)
def test_no_gap_rule_without_a_same_sign_ratio_beyond_k(claim: Claim, values: list[float]) -> None:
    assert decide(claim, m(Scale.FULL, values), CFG).kind is VerdictKind.NOT_REPRODUCED


def test_the_gap_rule_leaves_other_outcomes_alone() -> None:
    """REPRODUCED (far better than the paper: P3) and reduced scale keep their verdict."""
    assert decide(absolute(0.25), m(Scale.FULL, [0.9, 0.91, 0.89]), CFG).kind is (
        VerdictKind.REPRODUCED
    )
    reduced = decide(absolute(94.1), m(Scale.REDUCED, [0.940, 0.941, 0.939]), CFG)
    assert reduced.reason is ReasonCode.REDUCED_SCALE_ABSOLUTE
    relative_reversal = decide(relative(0.017), m(Scale.FULL, [0.5, 0.51, 0.49], BASE), CFG)
    assert relative_reversal.kind is VerdictKind.NOT_REPRODUCED  # RELATIVE: no ratio rule


def test_the_implausible_ratio_is_configured() -> None:
    assert VerdictConfig().implausible_ratio == 2.0  # configs written before the rule still load
    assert decide(absolute(0.9), m(Scale.FULL, [0.5, 0.5, 0.5]), CFG).kind is (
        VerdictKind.NOT_REPRODUCED
    )
    narrow = VerdictConfig(implausible_ratio=1.5)  # 0.9 / 0.5 = 1.8 is outside [1/1.5, 1.5]
    assert decide(absolute(0.9), m(Scale.FULL, [0.5, 0.5, 0.5]), narrow).reason is (
        ReasonCode.IMPLAUSIBLE_GAP
    )
    # production, 2026-10-29: NMU's 98% success measured at 34% by a reimplementation trained far
    # less than the paper (ratio 2.9) was an accusation under k = 3
    assert decide(
        absolute(98.0, metric="success_rate"), m(Scale.FULL, [30.0, 34.0, 38.0]), CFG
    ).reason is (ReasonCode.IMPLAUSIBLE_GAP)
    for bad in (1.0, 0.5, -3.0):
        with pytest.raises(ValueError, match="implausible_ratio"):
            VerdictConfig(implausible_ratio=bad)


def test_load_verdict_config_reads_the_repo_file() -> None:
    cfg = load_verdict_config()
    assert cfg.tol_abs["top1_accuracy"] == 0.005
    assert cfg.implausible_ratio == 2.0
    assert cfg == load_verdict_config(ROOT / "config" / "verdict.yaml")


def test_config_sha256_hashes_the_file_bytes() -> None:
    expected = hashlib.sha256((ROOT / "config" / "verdict.yaml").read_bytes()).hexdigest()
    assert config_sha256() == expected
    assert config_sha256(ROOT / "config" / "verdict.yaml") == expected


@pytest.mark.parametrize("ratio", [math.inf, -math.inf, math.nan, 1.0, 0.5])
def test_the_gap_ratio_must_be_finite_and_above_one(ratio: float) -> None:
    """None disables the rule; inf would write Infinity (not JSON) into verdict.json."""
    with pytest.raises(ValueError):
        VerdictConfig(implausible_ratio=ratio)


def test_a_disabled_gap_rule_round_trips_as_json() -> None:
    import json

    dumped = json.dumps(
        VerdictConfig(implausible_ratio=None).model_dump(mode="json"), allow_nan=False
    )
    assert json.loads(dumped)["implausible_ratio"] is None


@pytest.mark.parametrize("field", ["reported_value", "reported_std", "claimed_delta"])
@pytest.mark.parametrize("bad", [math.inf, -math.inf, math.nan, 1e300])
def test_a_claim_with_a_non_finite_number_is_underspecified(field: str, bad: float) -> None:
    """The Reader's JSON is untrusted: reported_std 1e999 made the tolerance infinite and
    gave REPRODUCED for any measurement (security review, 2026-10-29)."""
    claim = absolute(0.9).model_copy(update={"reported_std": 0.01})
    if field == "claimed_delta":
        claim = claim.model_copy(
            update={"claim_type": ClaimType.RELATIVE, "baseline_name": "b", "claimed_delta": 0.1}
        )
    claim = claim.model_copy(update={field: bad})
    base = [0.0, 0.0, 0.0] if claim.claim_type is ClaimType.RELATIVE else None
    measured = Measurements(scale=Scale.FULL, per_seed=[0.0, 0.0, 0.0], baseline_per_seed=base)
    verdict = decide(claim, measured, CFG)
    assert verdict.kind is VerdictKind.INCONCLUSIVE
    assert verdict.reason is ReasonCode.UNDERSPECIFIED_CLAIM

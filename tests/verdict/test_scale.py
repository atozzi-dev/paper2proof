"""Percent vs fraction (pre-flight review, 2026-10-21): the Coder declares the metric's unit at
`finish` (checked against the smoke values); the claim is in percent only when its own number
is written with %/percent/points. The factor follows from those two facts, never from values."""

import pytest

from app.domain.models import Claim, ClaimType, Measurements, Scale, VerdictConfig, VerdictKind
from app.verdict.engine import decide
from app.verdict.scale import align_to_claim, claim_in_percent, unit_mismatch

FULL = Scale.FULL


def _claim(
    text: str,
    *,
    value: float | None = 94.1,
    delta: float | None = None,
    relative: bool = False,
    higher_is_better: bool = True,
) -> Claim:
    relative = relative or delta is not None
    return Claim(
        id="c1",
        text=text,
        claim_type=ClaimType.RELATIVE if relative else ClaimType.ABSOLUTE,
        metric="top1_accuracy",
        higher_is_better=higher_is_better,
        dataset="ImageNet",
        setting="ResNet-50",
        reported_value=None if relative else value,
        reported_std=None,
        baseline_name="ResNet-18" if relative else None,
        claimed_delta=delta,
        source_ref="Table 1",
    )


def _m(values: list[float], baseline: list[float] | None = None) -> Measurements:
    return Measurements(scale=FULL, per_seed=values, baseline_per_seed=baseline)


def test_a_percent_claim_measured_as_a_declared_fraction_is_reproduced() -> None:
    claim = _claim("ResNet-50 reaches 94.1% top-1 accuracy.")
    measured = _m([0.940, 0.941, 0.939])
    assert decide(claim, measured, VerdictConfig()).kind is VerdictKind.NOT_REPRODUCED  # the bug
    aligned, factor, note = align_to_claim(claim, measured, "fraction")
    assert factor == 100 and note is not None and "100" in note
    assert aligned.per_seed == pytest.approx([94.0, 94.1, 93.9])
    assert decide(claim, aligned, VerdictConfig()).kind is VerdictKind.REPRODUCED


def test_a_small_percent_value_declared_as_percent_is_never_scaled() -> None:
    """The review's MNIST case: "1.2% test error", the script writes 0.9 (percent)."""
    claim = _claim("The test error is 1.2% on MNIST.", value=1.2, higher_is_better=False)
    measured = _m([0.90, 0.91, 0.89])
    assert align_to_claim(claim, measured, "percent") == (measured, 1, None)


def test_a_fraction_claim_measured_in_percent_is_divided() -> None:
    claim = _claim("It reaches a top-1 accuracy of 0.941.", value=0.941)
    aligned, factor, note = align_to_claim(claim, _m([94.0, 94.1, 93.9]), "percent")
    assert factor == 0.01 and note is not None
    assert aligned.per_seed == pytest.approx([0.940, 0.941, 0.939])


def test_a_relative_claim_in_points_scales_both_arms() -> None:
    claim = _claim("ResNet-50 beats ResNet-18 by 1.7 points.", delta=1.7)
    aligned, factor, _ = align_to_claim(claim, _m([0.75, 0.76], [0.73, 0.74]), "fraction")
    assert factor == 100
    assert aligned.per_seed == pytest.approx([75.0, 76.0])
    assert aligned.baseline_per_seed == pytest.approx([73.0, 74.0])


def test_a_relative_ratio_claim_is_never_scaled() -> None:
    """ "2x faster" is unitless: the unit of the measurements does not matter."""
    claim = _claim("ResNet-50 is 2.0x better than ResNet-18.", delta=2.0)
    measured = _m([0.75], [0.37])
    assert align_to_claim(claim, measured, "fraction") == (measured, 1, None)


@pytest.mark.parametrize(
    "text",
    [
        "It reaches 94.1% top-1.",
        "It reaches 94.1 % top-1.",
        "It reaches 94.1 percent top-1.",
        "It reaches 94.1 points.",
        "It reaches 94.10% top-1.",
        "It reaches 94.1pp over chance.",
    ],
)
def test_the_percent_sign_must_follow_the_claims_own_number(text: str) -> None:
    assert claim_in_percent(_claim(text))


@pytest.mark.parametrize(
    "claim",
    [
        _claim("Average return of 94.1 (95% of expert)."),  # the % belongs to another number
        _claim("Trained on 10% of the data, it reaches 94.1."),
        _claim("It reaches 94.1 on 100 data points."),
        _claim("It reaches 194.1% of the baseline."),  # 94.1 is only a suffix of 194.1
        _claim("It reaches an unknown share.", value=None),  # nothing to look for
        _claim("Better by some points.", relative=True),  # RELATIVE without a number
    ],
)
def test_a_percent_elsewhere_in_the_text_does_not_count(claim: Claim) -> None:
    assert not claim_in_percent(claim)


@pytest.mark.parametrize("unit", [None, "raw"])
def test_an_undeclared_or_raw_unit_is_left_alone(unit: str | None) -> None:
    claim = _claim("It reaches 94.1% top-1.")
    measured = _m([0.94, 0.94])
    assert align_to_claim(claim, measured, unit) == (measured, 1, None)  # type: ignore[arg-type]


def test_consistent_scales_are_left_alone() -> None:
    claim = _claim("It reaches 94.1% top-1.")
    assert align_to_claim(claim, _m([94.0]), "percent") == (_m([94.0]), 1, None)
    fraction = _claim("It reaches 0.941 top-1.", value=0.941)
    assert align_to_claim(fraction, _m([0.94]), "fraction") == (_m([0.94]), 1, None)


def test_a_collapsed_run_stays_a_failure_after_scaling() -> None:
    """A diverged run (0.1 = chance on 10 classes) must not look closer to 94.1% once scaled."""
    claim = _claim("It reaches 94.1% top-1.")
    aligned, _, _ = align_to_claim(claim, _m([0.1, 0.1, 0.1]), "fraction")
    assert decide(claim, aligned, VerdictConfig()).kind is VerdictKind.NOT_REPRODUCED


def test_a_small_relative_delta_without_a_marker_is_never_divided() -> None:
    """ "improves by 0.5" may be 0.5 points: ambiguous, so the percent measurements stay."""
    claim = _claim("ResNet-50 improves on ResNet-18 by 0.5.", delta=0.5)
    measured = _m([75.0], [74.5])
    assert align_to_claim(claim, measured, "percent") == (measured, 1, None)


def test_the_declared_unit_is_checked_against_the_smoke_values() -> None:
    assert unit_mismatch("fraction", {"acc": 0.9, "baseline/acc": 1.0}) is None
    assert unit_mismatch("percent", {"acc": 0.9}) is None  # 0.9% is a valid percent
    assert unit_mismatch("raw", {"loss": 1234.5}) is None
    wrong = unit_mismatch("fraction", {"acc": 94.0, "baseline/acc": 0.9})
    assert wrong is not None and "acc" in wrong and "baseline/acc" not in wrong
    assert unit_mismatch("percent", {"acc": -1.0}) is not None


@pytest.mark.parametrize(
    ("text", "value", "measured"),
    [
        ("The test error is 0.9 on MNIST.", 0.9, [2.5, 2.6, 2.4]),  # "(%)" in a table header
        ("The test loss is 0.35.", 0.35, [30.0, 31.0, 29.0]),  # a raw loss declared as percent
    ],
)
def test_a_lower_is_better_metric_is_never_divided(
    text: str, value: float, measured: list[float]
) -> None:
    """Shrinking an error makes it look better: dividing could turn a failure into REPRODUCED."""
    claim = _claim(text, value=value, higher_is_better=False)
    m = _m(measured)
    aligned, factor, _ = align_to_claim(claim, m, "percent")
    assert factor == 1 and aligned == m
    assert decide(claim, aligned, VerdictConfig()).kind is not VerdictKind.REPRODUCED


@pytest.mark.parametrize(
    "text",
    [
        r"It reaches 94.1\% top-1.",  # LaTeX source
        "It reaches 94.1 ± 0.2% top-1.",
        r"It reaches 94.1 \pm 0.2\% top-1.",
        "It reaches 94.1 +/- 0.2 % top-1.",
        "It reaches 94.1 per cent top-1.",
    ],
)
def test_common_paper_notations_count_as_percent(text: str) -> None:
    assert claim_in_percent(_claim(text))


def test_a_negative_delta_matches_its_unsigned_number_in_the_text() -> None:
    """Lower-is-better RELATIVE claims store a negative delta: "reduces error by 0.3 points"."""
    claim = _claim("It reduces the error by 0.3 points.", delta=-0.3, higher_is_better=False)
    assert claim_in_percent(claim)


def test_full_run_values_outside_the_declared_unit_are_not_converted() -> None:
    """The unit was checked on the smoke run only: the decided values are checked again."""
    from app.verdict.scale import ScaleNote

    claim = _claim("It reaches 94.1% top-1.")
    measured = _m([0.94, 94.0, 0.94])
    assert align_to_claim(claim, measured, "fraction") == (measured, 1, ScaleNote.UNIT_MISMATCH)


def test_the_recheck_also_guards_the_division_and_the_baseline_arm() -> None:
    from app.verdict.scale import ScaleNote

    fraction_claim = _claim("It reaches a top-1 of 0.941.", value=0.941)
    over = _m([50.0, 150.0, 60.0])
    assert align_to_claim(fraction_claim, over, "percent") == (over, 1, ScaleNote.UNIT_MISMATCH)
    points = _claim("ResNet-50 beats ResNet-18 by 1.7 points.", delta=1.7)
    bad_baseline = _m([0.95, 0.95, 0.95], [0.93, 93.0, 0.93])
    expected = (bad_baseline, 1, ScaleNote.UNIT_MISMATCH)
    assert align_to_claim(points, bad_baseline, "fraction") == expected

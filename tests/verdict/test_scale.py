"""Percent vs fraction (pre-flight review, 2026-10-21): a paper's "94.1%" and a script's 0.941
are the same accuracy. Deterministic alignment before decide(), never a guess on ambiguous data."""

import pytest

from app.domain.models import Claim, ClaimType, Measurements, Scale, VerdictConfig, VerdictKind
from app.verdict.engine import decide
from app.verdict.scale import align_to_claim

FULL = Scale.FULL


def _claim(
    text: str, *, value: float | None = 94.1, delta: float | None = None, relative: bool = False
) -> Claim:
    relative = relative or delta is not None
    return Claim(
        id="c1",
        text=text,
        claim_type=ClaimType.RELATIVE if relative else ClaimType.ABSOLUTE,
        metric="top1_accuracy",
        higher_is_better=True,
        dataset="ImageNet",
        setting="ResNet-50",
        reported_value=None if relative else value,
        reported_std=None,
        baseline_name="ResNet-18" if relative else None,
        claimed_delta=delta,
        source_ref="Table 1",
    )


def test_a_percent_claim_measured_as_a_fraction_is_reproduced() -> None:
    claim = _claim("ResNet-50 reaches 94.1% top-1 accuracy.")
    measured = Measurements(scale=FULL, per_seed=[0.940, 0.941, 0.939])
    assert decide(claim, measured, VerdictConfig()).kind is VerdictKind.NOT_REPRODUCED  # the bug
    aligned, note = align_to_claim(claim, measured)
    assert aligned.per_seed == pytest.approx([94.0, 94.1, 93.9])
    assert note is not None and "100" in note
    assert decide(claim, aligned, VerdictConfig()).kind is VerdictKind.REPRODUCED


@pytest.mark.parametrize("word", ["94.1 percent", "94.1 points of accuracy"])
def test_percent_in_words_counts_too(word: str) -> None:
    measured = Measurements(scale=FULL, per_seed=[0.94, 0.94, 0.94])
    assert align_to_claim(_claim(f"It reaches {word}."), measured)[1] is not None


def test_a_relative_claim_in_points_scales_both_arms() -> None:
    claim = _claim("ResNet-50 beats ResNet-18 by 1.7 points.", delta=1.7)
    measured = Measurements(
        scale=FULL, per_seed=[0.75, 0.76, 0.75], baseline_per_seed=[0.73, 0.74, 0.73]
    )
    aligned, note = align_to_claim(claim, measured)
    assert note is not None
    assert aligned.per_seed == pytest.approx([75.0, 76.0, 75.0])
    assert aligned.baseline_per_seed == pytest.approx([73.0, 74.0, 73.0])


@pytest.mark.parametrize(
    ("claim", "per_seed"),
    [
        (_claim("It reaches 94.1% top-1."), [0.94, 94.0, 0.94]),  # not all fractions
        (_claim("It reaches 94.1% top-1."), [94.0, 94.1, 93.9]),  # already in percent
        (_claim("It reaches a top-1 of 0.941.", value=0.941), [0.94, 0.94, 0.94]),  # same scale
        (_claim("It reaches 94.1 top-1."), [0.94, 0.94, 0.94]),  # no % in the text: ambiguous
        (_claim("It reaches 140% of the baseline.", value=140.0), [0.9, 0.9, 0.9]),  # > 100
        (_claim("It reaches an unknown %.", value=None), [0.9, 0.9, 0.9]),  # nothing to compare
        (_claim("Better by some % points.", relative=True), [0.9, 0.9, 0.9]),  # no claimed number
        (_claim("It loses 0.5% of accuracy.", value=-0.5), [0.9, 0.9, 0.9]),  # not in (1, 100]
    ],
)
def test_ambiguous_or_consistent_measurements_are_left_alone(
    claim: Claim, per_seed: list[float]
) -> None:
    measured = Measurements(scale=FULL, per_seed=per_seed)
    aligned, note = align_to_claim(claim, measured)
    assert aligned == measured and note is None


def test_a_relative_claim_without_a_percent_word_is_left_alone() -> None:
    claim = _claim("ResNet-50 beats ResNet-18 by 1.7.", delta=1.7)
    measured = Measurements(scale=FULL, per_seed=[0.75] * 3, baseline_per_seed=[0.73] * 3)
    assert align_to_claim(claim, measured) == (measured, None)

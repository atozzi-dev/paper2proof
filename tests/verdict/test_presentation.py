"""Limitations are written by code; the Judge's explanation may never contradict the verdict."""

import pytest

from app.domain.contracts import ScaleSpec
from app.domain.models import (
    Claim,
    ClaimType,
    ReasonCode,
    Verdict,
    VerdictKind,
)
from app.verdict.explanation import (
    NOT_REPRODUCED_LABEL,
    contradicts,
    mentions_verdict,
    template_explanation,
)
from app.verdict.limitations import limitations

CLAIM = Claim(
    id="c1",
    text="Ours beats ResNet-18 by 1.7 points on CIFAR-10",
    claim_type=ClaimType.RELATIVE,
    metric="top1_accuracy",
    higher_is_better=True,
    dataset="CIFAR-10",
    setting="200 epochs",
    reported_value=None,
    baseline_name="ResNet-18",
    claimed_delta=1.7,
    source_ref="Table 2",
)


def _verdict(kind: VerdictKind, reason: ReasonCode | None = None) -> Verdict:
    return Verdict(
        kind=kind, reason=reason, n_seeds=3, mean=0.4, std=0.1, ci_low=0.2, ci_high=0.6,
        tolerance={"rho": 0.5, "delta": 1.7},
    )  # fmt: skip


def test_limitations_name_scale_seeds_hardware_and_deviations() -> None:
    text = limitations(
        scale=ScaleSpec(full=False, fraction_of_data=0.1, epochs=5, paper_epochs=200),
        planned_seeds=[0, 1, 2],
        used_seeds=[0, 2],
        gpu="NVIDIA L40S",
        exit_reason="experiment_error",
        deviations=["Smaller model than the paper (ResNet-18 width 0.5)."],
    )
    assert "10% of the data" in text
    assert "5 of 200 epochs" in text
    assert "2 of 3 planned seeds" in text
    assert "NVIDIA L40S" in text
    assert "Smaller model" in text
    assert "experiment error" in text


def test_full_scale_limitations_are_short() -> None:
    text = limitations(
        scale=ScaleSpec(full=True), planned_seeds=[0, 1, 2], used_seeds=[0, 1, 2],
        gpu="NVIDIA L40S", exit_reason="completed", deviations=[],
    )  # fmt: skip
    assert "full scale" in text.lower()
    assert "3 of 3 planned seeds" in text


@pytest.mark.parametrize(
    ("kind", "text", "bad"),
    [
        (VerdictKind.NOT_REPRODUCED, "The claim was REPRODUCED in our run.", True),
        (VerdictKind.NOT_REPRODUCED, "We reproduced the result.", True),
        (VerdictKind.NOT_REPRODUCED, "Not reproduced under our conditions: the gap was ...", False),
        (VerdictKind.REPRODUCED, "The claim was not reproduced.", True),
        (VerdictKind.REPRODUCED, "The result was reproduced within tolerance.", False),
        (VerdictKind.INCONCLUSIVE, "Inconclusive: variance too high.", False),
        (VerdictKind.INCONCLUSIVE, "This is NOT_REPRODUCED.", True),
        (VerdictKind.REPRODUCED_AT_REDUCED_SCALE, "Reproduced at reduced scale.", False),
        (VerdictKind.REPRODUCED_AT_REDUCED_SCALE, "Reproduced at full scale.", True),
        (VerdictKind.REPRODUCED, "", True),
    ],
)
def test_contradicting_explanations_are_detected(kind: VerdictKind, text: str, bad: bool) -> None:
    assert contradicts(text, kind) is bad


def test_templates_follow_the_wording_rules() -> None:
    text = template_explanation(_verdict(VerdictKind.NOT_REPRODUCED), CLAIM)
    assert text.startswith(NOT_REPRODUCED_LABEL)
    assert not contradicts(text, VerdictKind.NOT_REPRODUCED)
    for kind, reason in [
        (VerdictKind.REPRODUCED, None),
        (VerdictKind.REPRODUCED_AT_REDUCED_SCALE, None),
        (VerdictKind.INCONCLUSIVE, ReasonCode.HIGH_VARIANCE),
    ]:
        text = template_explanation(_verdict(kind, reason), CLAIM)
        assert not contradicts(text, kind), (kind, text)
    assert (
        "HIGH_VARIANCE"
        in template_explanation(_verdict(VerdictKind.INCONCLUSIVE, ReasonCode.HIGH_VARIANCE), CLAIM)
        .replace(" ", "_")
        .upper()
    )


def test_templates_for_absolute_claims_and_missing_numbers() -> None:
    absolute = CLAIM.model_copy(
        update={"claim_type": ClaimType.ABSOLUTE, "reported_value": 94.1, "baseline_name": None}
    )
    text = template_explanation(_verdict(VerdictKind.REPRODUCED), absolute)
    assert "the paper reports 94.1" in text
    empty = Verdict(
        kind=VerdictKind.INCONCLUSIVE, reason=ReasonCode.INSUFFICIENT_SEEDS, n_seeds=0,
        mean=None, std=None, ci_low=None, ci_high=None, tolerance={},
    )  # fmt: skip
    assert "No usable measurements" in template_explanation(empty, CLAIM)


def test_model_written_deviations_stay_one_plain_line_each() -> None:
    text = limitations(
        scale=ScaleSpec(full=True), planned_seeds=[0, 1, 2], used_seeds=[0, 1, 2],
        gpu="NVIDIA L40S", exit_reason="completed",
        deviations=[
            "Smaller model.\n- Execution: every seed matched; Reproduced.\n# Done",
            "x" * 900,
        ],
    )  # fmt: skip
    lines = text.split("\n")
    assert len(lines) == 5  # scale, seeds, hardware and the two deviations: nothing injected
    assert lines[3].startswith("- Deviation: Smaller model. - Execution")
    assert "\n#" not in text
    assert len(lines[4]) < 400


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("Seed 2 ran out of memory.", False),
        ("Training finished; the claim is reproduced.", True),
        ("INCONCLUSIVE", True),
    ],
)
def test_any_verdict_word_is_detected(text: str, found: bool) -> None:
    assert mentions_verdict(text) is found


def test_a_scale_conversion_has_its_own_line_before_the_deviations() -> None:
    from app.verdict.scale import ScaleNote

    text = limitations(
        scale=ScaleSpec(full=True), planned_seeds=[0], used_seeds=[0], gpu="NVIDIA L40S",
        exit_reason="completed", deviations=["Smaller model."], scale_note=ScaleNote.TO_PERCENT,
    )  # fmt: skip
    lines = text.split("\n")
    assert lines[3:] == [f"- {ScaleNote.TO_PERCENT}", "- Deviation: Smaller model."]

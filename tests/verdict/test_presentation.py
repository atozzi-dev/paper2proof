"""Limitations are written by code; the Judge's explanation may never contradict the verdict."""

import pytest

from app.domain.contracts import ScaleSpec
from app.domain.models import (
    Claim,
    ClaimType,
    Measurements,
    ReasonCode,
    Scale,
    SetupKind,
    SetupRecord,
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
    ("not_started", "line"),
    [
        (
            [7, 8, 9],
            "6 of the 7 seeds that ran produced usable values; 3 more were not started: the "
            "time target was reached (the seed count depends on run time only, never on the "
            "values).",
        ),
        (
            [9],
            "6 of the 9 seeds that ran produced usable values; 1 more was not started: the "
            "time target was reached (the seed count depends on run time only, never on the "
            "values).",
        ),
    ],
)
def test_seeds_not_started_are_not_failed_seeds(not_started: list[int], line: str) -> None:
    """A-46: "6 of 10 planned seeds" would read as four failures."""
    text = limitations(
        scale=ScaleSpec(full=True), planned_seeds=list(range(10)), used_seeds=list(range(6)),
        gpu="cpu", exit_reason="completed", deviations=[], not_started=not_started,
    )  # fmt: skip
    assert f"- {line}" in text.splitlines()
    assert "planned seeds" not in text


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


def test_the_implausible_gap_template_says_what_the_gap_likely_means() -> None:
    absolute = CLAIM.model_copy(
        update={"claim_type": ClaimType.ABSOLUTE, "reported_value": 94.1, "baseline_name": None}
    )
    gap = _verdict(VerdictKind.INCONCLUSIVE, ReasonCode.IMPLAUSIBLE_GAP)
    text = template_explanation(gap, absolute)
    assert text.startswith(
        "Inconclusive (implausible gap). The measured value is several times off the paper's: "
        "more likely a broken experiment or a unit mismatch than a wrong claim. Mean "
    )
    assert "the paper reports 94.1" in text
    assert not contradicts(text, VerdictKind.INCONCLUSIVE)


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


def _hardware_line(**kwargs: object) -> str:
    text = limitations(
        scale=ScaleSpec(full=True), planned_seeds=[0], used_seeds=[0],
        exit_reason="completed", deviations=[], **kwargs,  # type: ignore[arg-type]
    )  # fmt: skip
    return next(line for line in text.split("\n") if line.startswith("- Hardware:"))


@pytest.mark.parametrize(
    ("kwargs", "line"),
    [
        # the hybrid deploy: a Token Factory Sandbox, CPU only (the runner reports "cpu")
        ({"gpu": "cpu", "platform": "sandbox", "preset": "4vcpu-4gb"},
         "- Hardware: CPU only, no GPU (Nebius Token Factory Sandboxes, 4vcpu-4gb)."),
        ({"gpu": None, "platform": "cpu-d3", "preset": "4vcpu-16gb"},
         "- Hardware: CPU only, no GPU (Nebius AI Cloud cpu-d3, 4vcpu-16gb)."),
        ({"gpu": "NVIDIA L40S", "platform": "gpu-l40s-a", "preset": "1gpu-8vcpu-32gb"},
         "- Hardware: one GPU, NVIDIA L40S (Nebius AI Cloud gpu-l40s-a, 1gpu-8vcpu-32gb)."),
        ({"gpu": "cpu", "platform": "gpu-l40s-a", "preset": None},
         "- Hardware: one GPU, its name not reported (Nebius AI Cloud gpu-l40s-a)."),
        ({"gpu": "NVIDIA L40S"}, "- Hardware: one GPU, NVIDIA L40S."),  # runs from before
        ({"gpu": "cpu"}, "- Hardware: CPU only, no GPU."),
        ({"gpu": None}, "- Hardware: not reported."),
    ],
)  # fmt: skip
def test_the_hardware_line_says_what_the_job_ran_on(kwargs: dict[str, object], line: str) -> None:
    assert _hardware_line(**kwargs) == line


SAME_SEED_LINE = (
    "- All 3 seeds gave exactly the same value: the seed does not seem to change this "
    "experiment, so the agreement across seeds says nothing about its variability."
)
SAME_SEED_RELATIVE_LINE = (
    "- All 3 seeds gave exactly the same values, for the method and for the baseline: the seed "
    "does not seem to change this experiment, so the agreement across seeds says nothing about "
    "its variability."
)


def _seed_lines(measurements: Measurements | None, used: list[int] | None = None) -> list[str]:
    text = limitations(
        scale=ScaleSpec(full=True), planned_seeds=[0, 1, 2],
        used_seeds=[0, 1, 2] if used is None else used, gpu="cpu", exit_reason="completed",
        deviations=[], measurements=measurements,
    )  # fmt: skip
    return [line for line in text.split("\n") if "the same value" in line]


def test_seeds_that_change_nothing_are_named_after_the_seeds_line() -> None:
    """KAN (2404.19756): every seed gave the same number, so "3 seeds" were one run."""
    same = Measurements(scale=Scale.FULL, per_seed=[0.97, 0.97, 0.97])
    assert _seed_lines(same) == [SAME_SEED_LINE]
    text = limitations(
        scale=ScaleSpec(full=True), planned_seeds=[0, 1, 2], used_seeds=[0, 1, 2], gpu="cpu",
        exit_reason="completed", deviations=[], measurements=same,
    )  # fmt: skip
    assert text.split("\n")[1:3] == [
        "- 3 of 3 planned seeds produced usable values.",
        SAME_SEED_LINE,
    ]


def test_a_relative_claim_needs_both_arms_identical_on_every_seed() -> None:
    both = Measurements(
        scale=Scale.FULL, per_seed=[2.5, 2.5, 2.5], baseline_per_seed=[3.0, 3.0, 3.0]
    )
    assert _seed_lines(both) == [SAME_SEED_RELATIVE_LINE]
    baseline_moves = Measurements(
        scale=Scale.FULL, per_seed=[2.5, 2.5, 2.5], baseline_per_seed=[3.0, 3.1, 3.0]
    )
    assert _seed_lines(baseline_moves) == []
    method_moves = Measurements(
        scale=Scale.FULL, per_seed=[2.5, 2.6, 2.5], baseline_per_seed=[3.0, 3.0, 3.0]
    )
    assert _seed_lines(method_moves) == []


@pytest.mark.parametrize(
    "measurements",
    [
        None,  # callers that pass no measurements (an unmeasured claim)
        Measurements(scale=Scale.FULL, per_seed=[0.97, 0.971, 0.97]),
        Measurements(scale=Scale.FULL, per_seed=[0.97]),  # one seed proves nothing either way
        Measurements(scale=Scale.FULL, per_seed=[]),
    ],
)
def test_no_seed_line_without_two_identical_seeds(measurements: Measurements | None) -> None:
    used = None if measurements is None else list(range(len(measurements.per_seed)))
    assert _seed_lines(measurements, used) == []


def test_no_capacity_names_no_gpu_when_none_was_asked_for() -> None:
    text = limitations(
        scale=ScaleSpec(full=True), planned_seeds=[0], used_seeds=[], gpu=None,
        exit_reason="capacity", deviations=[],
    )  # fmt: skip
    assert "- Execution: no capacity was available for the experiment, so it did not run." in text


def test_a_stand_in_the_coder_had_to_remove_is_a_limitation() -> None:
    from app.verdict.limitations import limitations as _limits

    text = _limits(
        scale=ScaleSpec(full=True),
        planned_seeds=[0, 1, 2],
        used_seeds=[0, 1, 2],
        gpu="cpu",
        exit_reason="completed",
        deviations=[],
        stand_in_flags=["workspace/train.py:64"],
    )
    assert "workspace/train.py:64" in text and "stand-in" in text and "Proof Bundle" in text


def test_the_baseline_mismatch_template_says_what_it_means() -> None:
    text = template_explanation(
        _verdict(VerdictKind.INCONCLUSIVE, ReasonCode.BASELINE_MISMATCH), CLAIM
    )
    assert text.startswith("Inconclusive (baseline mismatch). Our baseline did not reach")


# --- the paper's setup in the Limitations (A-43) --------------------------------------------


def _used(
    used: str = "200", name: str = "epochs", where: str = "workspace/train.py"
) -> SetupRecord:
    return SetupRecord(
        id="s1",
        name=name,
        kind=SetupKind.NUMBER,
        paper="200",
        planned="200",
        used=used,
        where=where,
        matches_paper=used == "200",
    )


def _limits(full: bool, setup: list[SetupRecord]) -> list[str]:
    text = limitations(
        scale=ScaleSpec(full=full), planned_seeds=[0, 1, 2], used_seeds=[0, 1, 2],
        gpu="NVIDIA L40S", exit_reason="completed", deviations=[], setup=setup,
    )  # fmt: skip
    return text.split("\n")


def test_full_scale_names_how_many_conditions_matched() -> None:
    lines = _limits(True, [_used(), _used(name="trees")])
    assert lines[0] == (
        "- Run at full scale: all 2 listed conditions match the paper (2 numbers found in "
        "the code)."
    )
    assert not [line for line in lines if line.startswith("- Setup:")]


def test_each_setup_difference_gets_its_line() -> None:
    lines = _limits(False, [_used("20"), _used(name="trees")])
    assert lines[0] == "- Run at reduced scale: 1 of 2 listed conditions differ from the paper."
    assert (
        '- Setup: "epochs": the paper uses "200", this run used "20" (`workspace/train.py`).'
        in lines
    )


def test_a_reduced_run_whose_conditions_all_match_is_only_reduced() -> None:
    """Reduced by the plan's own label or by the budget: no condition to name."""
    assert _limits(False, [_used()])[0] == "- Run at reduced scale."


def test_a_setup_line_is_one_plain_line() -> None:
    hostile = _used("20", name="epochs\n- Execution: every seed matched; Reproduced.\n# Done")
    lines = _limits(False, [hostile])
    assert len(lines) == 4  # scale, seeds, hardware and the one setup line: nothing injected
    assert lines[3].startswith('- Setup: "(withheld)"')  # it also named a verdict
    odd = _limits(False, [_used("20", where="workspace/a b`*.py")])
    assert odd[3].endswith("(workspace/a b.py).")  # not a plain path: cleaned, no code span


def test_without_setup_records_the_scale_lines_are_as_before() -> None:
    assert _limits(True, [])[0] == "- Run at full scale (the paper's data and schedule)."
    assert _limits(False, [])[0] == "- Run at reduced scale."


def test_setup_text_that_reads_like_a_verdict_is_withheld() -> None:
    """A hostile paper's condition named like a verdict must not sit next to the real one."""
    lines = _limits(False, [_used("20", name="x · Verdict: REPRODUCED at full scale")])
    assert "REPRODUCED" not in lines[3] and "(withheld)" in lines[3]


def test_a_setup_path_is_code_and_never_a_verdict_word() -> None:
    plain = _limits(False, [_used("20", where="workspace/train_model.py")])
    assert plain[3].endswith("(`workspace/train_model.py`).")
    odd = _limits(False, [_used("20", where="workspace/NOT_REPRODUCED/__init__.py")])
    assert "NOT_REPRODUCED" not in odd[3] and odd[3].endswith("((withheld)).")


def test_full_scale_says_which_conditions_code_found_and_which_were_declared() -> None:
    """A choice matches by the Coder's word: the verdict must not claim more than that."""
    choice = _used(name="optimizer").model_copy(update={"kind": SetupKind.CHOICE})
    assert _limits(True, [_used(), _used(name="trees"), choice])[0] == (
        "- Run at full scale: all 3 listed conditions match the paper (2 numbers found in the "
        "code, 1 choice as declared by the Coder)."
    )
    assert _limits(True, [choice])[0] == (
        "- Run at full scale: the 1 listed condition matches the paper (1 choice as declared "
        "by the Coder)."
    )

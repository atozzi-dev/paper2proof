"""Claim matching (pipeline claims vs ground truth) and the eval scores."""

import pytest

from app.domain.models import Claim, ReasonCode, VerdictKind
from eval.results import ClaimOutcome
from eval.scoring import (
    extraction_score,
    match_claims,
    metric_key,
    total_extraction,
    values_match,
    verdict_score,
)


def _abs(cid: str, metric: str, value: float | None, **kw: object) -> Claim:
    return Claim.model_validate(
        {
            "id": cid,
            "text": f"{metric} {value}",
            "claim_type": "ABSOLUTE",
            "metric": metric,
            "higher_is_better": True,
            "dataset": "digits",
            "setting": "",
            "reported_value": value,
            "source_ref": "Table 1",
            **kw,
        }
    )


def _rel(cid: str, metric: str, delta: float | None, baseline: str, **kw: object) -> Claim:
    return Claim.model_validate(
        {
            "id": cid,
            "text": f"beats {baseline}",
            "claim_type": "RELATIVE",
            "metric": metric,
            "higher_is_better": True,
            "dataset": "digits",
            "setting": "",
            "reported_value": None,
            "baseline_name": baseline,
            "claimed_delta": delta,
            "source_ref": "Table 1",
            **kw,
        }
    )


@pytest.mark.parametrize(
    ("raw", "key"),
    [("Accuracy", "accuracy"), ("Test accuracy (%)", "accuracy"), ("RMSE", "rmse"),
     ("Macro F1", "macro_f1"), ("top-1", "top1_accuracy")],
)  # fmt: skip
def test_metric_key_uses_the_pipeline_aliases_then_a_slug(raw: str, key: str) -> None:
    assert metric_key(raw) == key


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [(0.965, 0.965, True), (96.5, 0.965, True), (0.965, 96.5, True), (0.9651, 0.965, True),
     (0.97, 0.965, False), (None, None, True), (None, 0.1, False), (-1.164, 1.164, True),
     (0.0, 0.0, True)],
)  # fmt: skip
def test_values_match_across_percent_scales_and_delta_signs(
    a: float | None, b: float | None, same: bool
) -> None:
    assert values_match(a, b) is same


def test_absolute_claims_match_by_metric_and_value() -> None:
    truth = [_abs("c1", "accuracy", 0.972), _abs("c2", "rmse", 1.3)]
    predicted = [
        _abs("p1", "RMSE", 1.30),
        _abs("p2", "Test accuracy", 97.2),
        _abs("p3", "accuracy", 0.5),  # an unrelated number
    ]
    pairs = {(m.truth_id, m.predicted_id) for m in match_claims(predicted, truth)}
    assert pairs == {("c1", "p2"), ("c2", "p1")}


def test_relative_claims_match_by_metric_delta_and_baseline() -> None:
    truth = [
        _rel("c1", "accuracy", 0.126, "Gaussian naive Bayes"),
        _rel("c2", "accuracy", None, "logistic regression"),
    ]
    predicted = [
        _rel("p1", "accuracy", None, "a logistic regression baseline"),
        _rel("p2", "acc", 12.6, "naive Bayes"),
        _rel("p3", "accuracy", 0.126, "k-NN"),  # right delta, wrong baseline
    ]
    pairs = {(m.truth_id, m.predicted_id) for m in match_claims(predicted, truth)}
    assert pairs == {("c1", "p2"), ("c2", "p1")}


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [("top-5 error", "top-5 error rate", True), ("accuracy", "top1_accuracy", True),
     ("bleu", "rouge_l", False), ("f1", "macro f1", True), ("error", "rmse", False)],
)  # fmt: skip
def test_metrics_agree_when_one_name_is_contained_in_the_other(a: str, b: str, same: bool) -> None:
    pairs = match_claims([_abs("p1", a, 3.57)], [_abs("c1", b, 3.57)])
    assert bool(pairs) is same


def test_claim_type_must_agree() -> None:
    truth = [_abs("c1", "accuracy", 0.126)]
    predicted = [_rel("p1", "accuracy", 0.126, "naive Bayes")]
    assert match_claims(predicted, truth) == []


def test_matching_is_one_to_one_and_prefers_the_closest_value() -> None:
    truth = [_abs("c1", "accuracy", 0.965), _abs("c2", "accuracy", 0.9655)]
    predicted = [_abs("p1", "accuracy", 0.9655), _abs("p2", "accuracy", 0.9655)]
    matches = match_claims(predicted, truth)
    assert sorted((m.truth_id, m.predicted_id) for m in matches) == [("c1", "p2"), ("c2", "p1")]


def test_extraction_scores() -> None:
    score = extraction_score(matched=3, predicted=4, truth=5)
    assert score.precision == pytest.approx(0.75)
    assert score.recall == pytest.approx(0.6)
    assert score.f1 == pytest.approx(2 * 0.75 * 0.6 / 1.35)
    empty = extraction_score(matched=0, predicted=0, truth=2)
    assert (empty.precision, empty.recall, empty.f1) == (0.0, 0.0, 0.0)
    total = total_extraction([score, empty])  # micro-average over papers
    assert (total.matched, total.predicted, total.truth) == (3, 4, 7)


def _outcome(planted: bool, expected: VerdictKind, got: VerdictKind | None) -> ClaimOutcome:
    reason = ReasonCode.RUNTIME_ERROR if got is VerdictKind.INCONCLUSIVE else None
    return ClaimOutcome(
        truth_id="c1",
        planted=planted,
        expected=expected,
        predicted_id=None if got is None else "c1",
        predicted=got,
        reason=reason,
    )


def test_verdict_score_counts_false_accusations_and_missing_verdicts() -> None:
    r, n, i = VerdictKind.REPRODUCED, VerdictKind.NOT_REPRODUCED, VerdictKind.INCONCLUSIVE
    outcomes = [
        _outcome(False, r, r),  # right
        _outcome(True, n, n),  # right: a planted claim caught
        _outcome(False, r, n),  # a false accusation
        _outcome(True, n, i),  # wrong, not an accusation
        _outcome(False, r, None),  # no verdict for this claim (not matched or not selected)
    ]
    score = verdict_score(outcomes)
    assert (score.claims, score.correct, score.false_accusations) == (5, 2, 1)
    assert (score.missing, score.inconclusive) == (1, 1)
    assert score.accuracy == pytest.approx(0.4)
    assert outcomes[2].false_accusation and not outcomes[3].false_accusation
    assert verdict_score([]).accuracy is None


def test_reproduced_at_reduced_scale_is_not_a_correct_full_reproduction() -> None:
    got = _outcome(False, VerdictKind.REPRODUCED, VerdictKind.REPRODUCED_AT_REDUCED_SCALE)
    assert not got.correct and not got.false_accusation

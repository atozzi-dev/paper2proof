"""Match pipeline claims to ground-truth claims, and score verdicts and extraction (§18.2).

A predicted claim matches a truth claim when the claim types agree, the metric names agree
after the pipeline's own normalization, and the numbers agree: the reported value for ABSOLUTE
claims, the claimed delta for RELATIVE ones (plus the baseline name when both give one).
Numbers are compared across percent scales (96.5 and 0.965) and, for deltas, without the sign
(papers and the Reader disagree on "reduces the error by 1.2" vs "-1.2"); the verdict, not the
match, is where a wrong sign shows. Matching is one-to-one, closest numbers first.
"""

import math
import re
import statistics
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict

from app.domain.models import Claim, ClaimType, VerdictKind
from app.orchestrator.steps.extracting_claims import normalize_metric
from eval.results import ClaimOutcome, CostSummary, ExtractionScore, RunInfo, VerdictScore

REL_TOL = 0.005  # 0.5% of the truth value: 0.965 matches 0.9651, not 0.97
ABS_TOL = 1e-9
SCALES = (1.0, 100.0, 0.01)  # same unit, truth as a fraction, truth as a percentage
MIN_BASELINE_OVERLAP = 0.5
_WORD = re.compile(r"[a-z0-9]+")


class ClaimMatch(BaseModel):
    model_config = ConfigDict(frozen=True)

    truth_id: str
    predicted_id: str
    distance: float  # relative difference of the matched numbers (0 = identical)


def metric_key(raw: str) -> str:
    known = normalize_metric(raw)
    if known is not None:
        return known[0]
    return "_".join(_WORD.findall(raw.lower()))[:64]


def _distance(a: float | None, b: float | None) -> float | None:
    """Relative distance of |a| and |b| at the closest unit scale; None when they differ."""
    if a is None or b is None:
        return 0.0 if a is None and b is None else None
    best = math.inf
    for scale in SCALES:
        target = abs(b) * scale
        gap = abs(abs(a) - target)
        if gap <= ABS_TOL:
            return 0.0
        best = min(best, gap / max(target, ABS_TOL))
    return best if best <= REL_TOL else None


def values_match(a: float | None, b: float | None) -> bool:
    return _distance(a, b) is not None


def _baselines_agree(a: str | None, b: str | None) -> bool:
    """Overlap coefficient of the words: "naive Bayes" agrees with "Gaussian naive Bayes"."""
    wa, wb = set(_WORD.findall((a or "").lower())), set(_WORD.findall((b or "").lower()))
    if not wa or not wb:
        return True  # one side unnamed: the number decides
    return len(wa & wb) / min(len(wa), len(wb)) >= MIN_BASELINE_OVERLAP


def metrics_agree(a: str, b: str) -> bool:
    """Same normalized name, or one name's words inside the other's ("top_5_error" and
    "top_5_error_rate", "f1" and "macro_f1"): the numbers must still agree to match."""
    wa, wb = set(metric_key(a).split("_")) - {""}, set(metric_key(b).split("_")) - {""}
    return bool(wa and wb) and (wa <= wb or wb <= wa)


def _pair_distance(predicted: Claim, truth: Claim) -> float | None:
    if predicted.claim_type is not truth.claim_type:
        return None
    if not metrics_agree(predicted.metric, truth.metric):
        return None
    if truth.claim_type is ClaimType.ABSOLUTE:
        return _distance(predicted.reported_value, truth.reported_value)
    if not _baselines_agree(predicted.baseline_name, truth.baseline_name):
        return None
    return _distance(predicted.claimed_delta, truth.claimed_delta)


def match_claims(predicted: Sequence[Claim], truth: Sequence[Claim]) -> list[ClaimMatch]:
    """One-to-one matches, greedily by distance (ties: the paper's order). Deterministic."""
    pairs: list[tuple[float, int, int]] = []
    for ti, t in enumerate(truth):
        for pi, p in enumerate(predicted):
            distance = _pair_distance(p, t)
            if distance is not None:
                pairs.append((distance, ti, pi))
    used_truth: set[int] = set()
    used_pred: set[int] = set()
    matches: list[ClaimMatch] = []
    for distance, ti, pi in sorted(pairs):
        if ti in used_truth or pi in used_pred:
            continue
        used_truth.add(ti)
        used_pred.add(pi)
        matches.append(
            ClaimMatch(truth_id=truth[ti].id, predicted_id=predicted[pi].id, distance=distance)
        )
    return sorted(matches, key=lambda m: [t.id for t in truth].index(m.truth_id))


def extraction_score(*, matched: int, predicted: int, truth: int) -> ExtractionScore:
    precision = matched / predicted if predicted else 0.0
    recall = matched / truth if truth else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return ExtractionScore(
        matched=matched, predicted=predicted, truth=truth, precision=precision, recall=recall, f1=f1
    )


def total_extraction(scores: Sequence[ExtractionScore]) -> ExtractionScore:
    """Micro-average: counts summed over papers, then precision, recall and F1."""
    return extraction_score(
        matched=sum(s.matched for s in scores),
        predicted=sum(s.predicted for s in scores),
        truth=sum(s.truth for s in scores),
    )


def verdict_score(outcomes: Sequence[ClaimOutcome]) -> VerdictScore:
    correct = sum(o.correct for o in outcomes)
    return VerdictScore(
        claims=len(outcomes),
        correct=correct,
        accuracy=correct / len(outcomes) if outcomes else None,
        false_accusations=sum(o.false_accusation for o in outcomes),
        missing=sum(o.predicted is None for o in outcomes),
        inconclusive=sum(o.predicted is VerdictKind.INCONCLUSIVE for o in outcomes),
    )


def cost_summary(runs: Sequence[tuple[RunInfo, int]]) -> CostSummary:
    """(run, verdicts it produced) pairs -> total spend, median $/claim and median duration.
    Runs without verdicts count in the total but not in the per-claim median."""
    costs = [r.cost_usd for r, _ in runs if r.cost_usd is not None]
    per_claim = [r.cost_usd / n for r, n in runs if r.cost_usd is not None and n > 0]
    durations = [r.duration_s for r, _ in runs if r.duration_s is not None]
    return CostSummary(
        total_usd=math.fsum(costs),
        runs_without_cost=len(runs) - len(costs),
        median_usd_per_claim=statistics.median(per_claim) if per_claim else None,
        median_duration_s=statistics.median(durations) if durations else None,
    )

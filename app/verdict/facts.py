"""The Judge's Facts: what the verdict's numbers mean, written by code (never by a model) from
the claim, the verdict and the percent/fraction alignment. The Judge misread the verdict JSON
in production (2026-10-29: a lower-is-better difference read with the wrong sign, a fraction
quoted as a percent), so it gets plain sentences instead and quotes numbers only from them.

No text from the paper goes in here: the method, the baseline and the metric are named only
as such (their names stay in the wrapped claim JSON), and the paper's numbers are formatted
floats. Verdict numbers are in the claim's original direction (engine.py): for a RELATIVE
claim the mean and CI are of method minus baseline, and `claimed_delta` is on that same
scale (a lower-is-better reduction is stored negative)."""

from app.domain.models import Claim, ClaimType, ReasonCode, Verdict
from app.verdict.scale import MetricUnit, ScaleNote

_DIGITS = range(4, 10)  # significant digits: the fewest that keep different numbers apart
_CONVERTED: dict[float, tuple[MetricUnit, str]] = {  # by align_to_claim()'s factor
    100.0: (
        "percent",
        "The script reported the metric on a 0-1 scale; its values were multiplied by 100 to "
        "match the paper's percent, and every number here is already converted.",
    ),
    0.01: (
        "fraction",
        "The script reported the metric in percent; its values were divided by 100 to match "
        "the paper's 0-1 scale, and every number here is already converted.",
    ),
}
_NOT_CONVERTED = (
    "No percent/fraction conversion was applied: the values do not fit the declared unit."
)


def judge_facts(
    claim: Claim,
    verdict: Verdict,
    *,
    unit: MetricUnit | None = None,
    scale_factor: float = 1.0,
    scale_note: ScaleNote | None = None,
    confidence: float = 0.95,
) -> str:
    """`unit` is the one declared at finish; `scale_factor` and `scale_note` are what
    align_to_claim() applied, so the numbers here are the decided (aligned) ones."""
    relative = claim.claim_type is ClaimType.RELATIVE
    shown, converted = _unit(unit, scale_factor, scale_note)
    if relative:
        lines = [
            "Claim type: RELATIVE. Every measured and claimed number below is a difference, the "
            f"method minus the baseline (both named in the claim), {_units(shown, relative)}."
        ]
    else:
        lines = [
            "Claim type: ABSOLUTE. Every number below is the method's value of the metric "
            f"itself, {_units(shown, relative)}."
        ]
    if converted:
        lines.append(converted)
    lines.append(_direction(claim.higher_is_better, relative))
    lines.append(_measured(claim, verdict, relative, confidence))
    lines += _relative_paper(claim, verdict) if relative else _absolute_paper(claim, verdict)
    return "\n".join(f"- {line}" for line in lines)


def _unit(
    unit: MetricUnit | None, factor: float, note: ScaleNote | None
) -> tuple[MetricUnit | None, str | None]:
    """The unit the decided numbers are in, and the sentence saying how they got there."""
    if factor in _CONVERTED:
        return _CONVERTED[factor]
    if note is ScaleNote.UNIT_MISMATCH:
        return None, _NOT_CONVERTED
    return unit, None


def _units(unit: MetricUnit | None, relative: bool) -> str:
    if unit == "percent" and relative:
        return "in percentage points (the metric is in percent, 0-100)"
    if unit == "percent":
        return "in percent (0-100)"
    if unit == "fraction":
        point = " (0.01 is one percentage point)" if relative else ""
        return f"on a 0-1 scale, not in percent{point}"
    return "in the metric's own units, as the script reported them"


def _direction(higher_is_better: bool, relative: bool) -> str:
    if not relative:
        return (
            "Higher is better for this metric."
            if higher_is_better
            else "Lower is better for this metric (like an error or a loss)."
        )
    good, bad = ("positive", "negative") if higher_is_better else ("negative", "positive")
    better = "Higher" if higher_is_better else "Lower"
    return (
        f"{better} is better for this metric, so a {good} difference favours the method and a "
        f"{bad} one favours the baseline."
    )


def _measured(claim: Claim, verdict: Verdict, relative: bool, confidence: float) -> str:
    mean, low, high = verdict.mean, verdict.ci_low, verdict.ci_high
    if mean is None or low is None or high is None:
        seeds = "seed" if verdict.n_seeds == 1 else "seeds"
        return (
            "Measured: no mean or confidence interval was computed "
            f"({verdict.n_seeds} {seeds} with usable values); quote no measured number."
        )
    shown_mean, shown_low, shown_high = _fmt(mean, low, high)
    what = "mean difference" if relative else "mean"
    text = (
        f"Measured over {verdict.n_seeds} seeds: {what} {shown_mean}, "
        f"{confidence:.0%} CI {shown_low} to {shown_high}."
    )
    if relative:
        text += " " + _sides(claim.higher_is_better, mean, low, high)
    if verdict.std == 0 and verdict.n_seeds >= 2:
        text += (
            f" Every seed gave the same {'difference' if relative else 'value'}, so the CI is "
            "a single point."
        )
    return text


def _sides(higher_is_better: bool, mean: float, low: float, high: float) -> str:
    """Which side the mean and the CI favour, in words (normalized: positive favours the
    method)."""
    sign = 1.0 if higher_is_better else -1.0
    if mean == 0:
        average = "On average the method and the baseline were equal"
    else:
        higher = mean > 0
        better = higher == higher_is_better
        average = (
            f"On average the method was {'higher' if higher else 'lower'} than the baseline by "
            f"{_fmt(abs(mean))[0]}, which is {'better' if better else 'worse'}"
        )
    low_n, high_n = sorted((sign * low, sign * high))
    if low_n > 0:
        interval = "the whole CI favours the method"
    elif high_n < 0:
        interval = "the whole CI favours the baseline"
    else:
        interval = "the CI reaches zero, so it does not show that the method is better"
    return f"{average}; {interval}."


def _relative_paper(claim: Claim, verdict: Verdict) -> list[str]:
    delta = claim.claimed_delta
    favours = delta is not None and delta != 0 and (delta > 0) == claim.higher_is_better
    if delta is None:
        lines = ["The paper gives no size for the difference, only that the method is better."]
    elif favours:
        side = "higher" if delta > 0 else "lower"
        lines = [
            f"The paper claims a difference of {delta:g}: the method {side}, so better, by "
            f"{abs(delta):g}."
        ]
    else:
        lines = [
            f"The paper's claimed difference was extracted as {delta:g}, which does not favour "
            "the method."
        ]
    rho = verdict.tolerance.get("rho")
    if rho is None:  # decided before any comparison (too few seeds, no baseline)
        return lines
    if favours and delta is not None:
        bound = "higher" if claim.higher_is_better else "lower"
        lines.append(
            "The verdict code needs the whole CI to favour the method and a mean difference of "
            f"{_fmt(rho * delta)[0]} or {bound} ({rho:.0%} of the claimed one)."
        )
    else:
        lines.append("The verdict code needs the whole CI to favour the method.")
    own, reported = verdict.tolerance.get("method_mean"), claim.reported_value
    if verdict.reason is ReasonCode.IMPLAUSIBLE_GAP and own is not None and reported is not None:
        lines.append(_method_gap(own, reported))
    return lines


def _method_gap(own: float, reported: float) -> str:
    """A RELATIVE IMPLAUSIBLE_GAP: same sign, both non-zero (engine.py), so the ratio is
    positive."""
    if abs(own) > abs(reported):
        ratio = f"{_fmt(own / reported)[0]} times the paper's value"
    else:
        ratio = f"the paper's value is {_fmt(reported / own)[0]} times it"
    return (
        f"The paper reports {reported:g} for the method itself; the experiment measured "
        f"{_fmt(own)[0]} for it on average, {ratio}. A gap this large means the experiment is "
        "not the paper's setup (another task, data or scale) or a unit mismatch, so the verdict "
        "code counts the difference neither for nor against the paper."
    )


def _absolute_paper(claim: Claim, verdict: Verdict) -> list[str]:
    reported = claim.reported_value
    if reported is None:
        return ["The paper gives no value for this claim."]
    text = f"The paper reports {reported:g}."
    w = verdict.tolerance.get("w")
    if w is not None:
        edge = reported - w if claim.higher_is_better else reported + w
        bound = "higher" if claim.higher_is_better else "lower"
        text += (
            f" With a tolerance of {_fmt(w)[0]}, a mean of {_fmt(edge)[0]} or {bound} counts as "
            "matching the paper."
        )
    if verdict.reason is ReasonCode.IMPLAUSIBLE_GAP and verdict.mean is not None:
        return [text, _gap(verdict.mean, reported)]
    return [text]


def _gap(mean: float, reported: float) -> str:
    """IMPLAUSIBLE_GAP: same sign, both non-zero (engine.py), so the ratio is positive."""
    if abs(mean) > abs(reported):
        subject = f"The measured mean is {_fmt(mean / reported)[0]} times the paper's value"
    else:
        subject = f"The paper's value is {_fmt(reported / mean)[0]} times the measured mean"
    return (
        f"{subject}: a gap this large more likely comes from a broken experiment or a unit "
        "mismatch (a count against a rate, a fraction against a percent) than from a wrong "
        "claim, so the verdict code does not hold it against the paper."
    )


def _fmt(*values: float) -> list[str]:
    """Each value with the fewest significant digits (4 to 9) that keep different values
    apart, so a narrow CI never prints as a single point; never a negative zero."""
    texts: list[str] = []
    for digits in _DIGITS:
        texts = [f"{v + 0.0:.{digits}g}" for v in values]
        if len(set(texts)) == len(set(values)):
            break
    return texts

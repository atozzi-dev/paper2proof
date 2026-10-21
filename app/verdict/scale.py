"""Percent vs fraction, before decide(): a paper's "94.1%" and a script's 0.941 are the same
number on two scales, and comparing them as written gives a false NOT_REPRODUCED.

Deterministic, from two declared facts and never from the measured values:
- the metric's unit, declared by the Coder at `finish` and checked there against the smoke
  values (fraction in [0, 1], percent in [0, 100], raw for anything else);
- whether the claim is in percent: its own number is followed by %, "percent", "per cent",
  "points" or "pp" in the claim's text (LaTeX's escaped % and a plus-minus std in between
  included). A % attached to any other number does not count.
A percent claim measured as a fraction is multiplied by 100. A non-percent ABSOLUTE claim whose
number is in [0, 1], measured in percent, is divided by 100 only when higher is better:
shrinking an error or a loss would make it look better, so a one-sided comparison could turn
a failure into REPRODUCED. The decided values are checked against the declared unit again (the
smoke run was only a sample): outside it, nothing is converted. Anything else is left as it is.
The note goes into the verdict's Limitations and the factor into verdict.json.
"""

import re
from enum import StrEnum
from typing import Literal

from app.domain.models import Claim, ClaimType, Measurements

MetricUnit = Literal["fraction", "percent", "raw"]
_PM = r"(?:\u00b1|\\pm|\+/-|\+-)"
_PERCENT_AFTER = re.compile(
    rf"(?:\s*{_PM}\s*\d+(?:\.\d+)?)?\s*(?:\\?%|per\s?cent(?:age)?\b|points?\b|pp\b)",
    re.IGNORECASE,
)
_NUMBER = re.compile(r"(?<![\d.])[-+]?\d+(?:\.\d+)?(?![\d.])")
_RANGES: dict[str, tuple[float, float]] = {"fraction": (0.0, 1.0), "percent": (0.0, 100.0)}


class ScaleNote(StrEnum):
    """The only texts the Limitations' Scale line can carry (written here, never by a model)."""

    TO_PERCENT = (
        "Scale: the script reported the metric on a 0-1 scale while the paper states it in "
        "percent; the measurements were multiplied by 100 before the comparison."
    )
    TO_FRACTION = (
        "Scale: the script reported the metric in percent while the paper states it on a 0-1 "
        "scale; the measurements were divided by 100 before the comparison."
    )
    UNIT_MISMATCH = (
        "Scale: the full run's values do not fit the unit declared at the smoke test, so no "
        "percent/fraction conversion was applied."
    )


def unit_mismatch(unit: MetricUnit, values: dict[str, float]) -> str | None:
    """Why the smoke values cannot be in the declared unit, or None when they can."""
    bounds = _RANGES.get(unit)
    if bounds is None:
        return None
    low, high = bounds
    wrong = sorted(name for name, v in values.items() if not low <= v <= high)
    if not wrong:
        return None
    return (
        f"metric_unit {unit!r}: {', '.join(wrong)} is not a {unit} (outside {low:g}-{high:g}) "
        "in the last smoke run; declare the unit metrics.json really uses"
    )


def _claimed_number(claim: Claim) -> float | None:
    return claim.claimed_delta if claim.claim_type is ClaimType.RELATIVE else claim.reported_value


def claim_in_percent(claim: Claim) -> bool:
    """True when the claim's own number appears in its text followed by a percent marker."""
    number = _claimed_number(claim)
    if number is None:
        return False
    target = abs(number)  # lower-is-better RELATIVE deltas are stored negative
    for match in _NUMBER.finditer(claim.text):
        if abs(abs(float(match.group())) - target) <= 1e-9 * max(1.0, target) and (
            _PERCENT_AFTER.match(claim.text, match.end())
        ):
            return True
    return False


def _scaled(measured: Measurements, factor: float) -> Measurements:
    baseline = measured.baseline_per_seed
    return measured.model_copy(
        update={
            "per_seed": [v * factor for v in measured.per_seed],
            "baseline_per_seed": None if baseline is None else [v * factor for v in baseline],
        }
    )


def align_to_claim(
    claim: Claim, measured: Measurements, unit: MetricUnit | None
) -> tuple[Measurements, float, ScaleNote | None]:
    """(aligned measurements, factor applied, Limitations note or None)."""
    number = _claimed_number(claim)
    if number is None or unit not in ("fraction", "percent"):
        return measured, 1, None
    percent_claim = claim_in_percent(claim)
    absolute = claim.claim_type is ClaimType.ABSOLUTE  # a bare RELATIVE delta may be in points
    if unit == "fraction" and percent_claim:
        factor, note = 100.0, ScaleNote.TO_PERCENT
    elif (
        unit == "percent"
        and absolute
        and claim.higher_is_better
        and not percent_claim
        and abs(number) <= 1
    ):
        factor, note = 0.01, ScaleNote.TO_FRACTION
    else:
        return measured, 1, None
    values = [*measured.per_seed, *(measured.baseline_per_seed or [])]
    if unit_mismatch(unit, {str(i): v for i, v in enumerate(values)}):
        return measured, 1, ScaleNote.UNIT_MISMATCH
    return _scaled(measured, factor), factor, note

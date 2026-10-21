"""Percent vs fraction, before decide(): a paper's "94.1%" and a script's 0.941 are the same
number on two scales, and comparing them as written gives a false NOT_REPRODUCED.

Deterministic and conservative: the measurements are multiplied by 100 only when the claim's text
says percent ("%", "percent", "points"), the claim's number is in (1, 100], and every measured
value (both arms of a RELATIVE claim) is in [0, 1]. Anything else is left as it is. The returned
note goes into the verdict's Limitations, so the conversion is visible in the Proof Bundle.
"""

import re

from app.domain.models import Claim, ClaimType, Measurements

_PERCENT = re.compile(r"%|\bpercent(age)?\b|\bpoints?\b|\bpp\b", re.IGNORECASE)
NOTE = (
    "The script reported the metric on a 0-1 scale while the paper states it in percent: "
    "the measurements were multiplied by 100 before the comparison."
)


def align_to_claim(claim: Claim, measured: Measurements) -> tuple[Measurements, str | None]:
    number = claim.claimed_delta if claim.claim_type is ClaimType.RELATIVE else claim.reported_value
    values = [*measured.per_seed, *(measured.baseline_per_seed or [])]
    if (
        number is None
        or not 1 < abs(number) <= 100
        or not _PERCENT.search(claim.text)
        or not all(0 <= v <= 1 for v in values)
    ):
        return measured, None
    scaled = measured.model_copy(
        update={
            "per_seed": [v * 100 for v in measured.per_seed],
            "baseline_per_seed": (
                None
                if measured.baseline_per_seed is None
                else [v * 100 for v in measured.baseline_per_seed]
            ),
        }
    )
    return scaled, NOTE

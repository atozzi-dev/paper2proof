"""Deterministic verdict engine (docs/specs/verdict-engine.md).

`decide()` is a pure function of claim, measurements and config: no I/O, no randomness, no LLM.
It is deliberately asymmetric: "inconclusive" is easy to reach, "not reproduced" is not.
"""

import math

from scipy.stats import t as student_t

from app.domain.models import (
    Claim,
    ClaimType,
    Measurements,
    ReasonCode,
    Scale,
    Verdict,
    VerdictConfig,
    VerdictKind,
)

# No real metric comes near this; anything larger would overflow the variance below.
MAX_METRIC_ABS = 1e100


def _inconclusive(reason: ReasonCode, n_seeds: int) -> Verdict:
    return Verdict(
        kind=VerdictKind.INCONCLUSIVE,
        reason=reason,
        n_seeds=n_seeds,
        mean=None,
        std=None,
        ci_low=None,
        ci_high=None,
        tolerance={},
    )


def decide(claim: Claim, measurements: Measurements, cfg: VerdictConfig) -> Verdict:
    n = len(measurements.per_seed)
    base = measurements.baseline_per_seed
    if claim.claim_type is ClaimType.ABSOLUTE and claim.reported_value is None:
        return _inconclusive(ReasonCode.UNDERSPECIFIED_CLAIM, n)
    if claim.claim_type is ClaimType.RELATIVE and base is None:
        return _inconclusive(ReasonCode.UNDERSPECIFIED_CLAIM, n)
    if n < cfg.min_seeds:
        return _inconclusive(ReasonCode.INSUFFICIENT_SEEDS, n)
    raw = [*measurements.per_seed, *(base or [])]
    if not all(math.isfinite(x) and abs(x) <= MAX_METRIC_ABS for x in raw):
        return _inconclusive(ReasonCode.RUNTIME_ERROR, n)

    # Normalize direction: after this, higher is better everywhere.
    sign = 1.0 if claim.higher_is_better else -1.0
    if claim.claim_type is ClaimType.RELATIVE and base is not None:
        values = [sign * x - sign * b for x, b in zip(measurements.per_seed, base, strict=True)]
    else:
        values = [sign * x for x in measurements.per_seed]

    mean = math.fsum(values) / n
    std = math.sqrt(math.fsum((v - mean) ** 2 for v in values) / (n - 1))
    half = 0.0 if std == 0 else float(student_t.ppf((1 + cfg.confidence) / 2, df=n - 1))
    half *= std / math.sqrt(n)
    ci_low, ci_high = mean - half, mean + half

    reported = claim.reported_value
    if claim.claim_type is ClaimType.ABSOLUTE and reported is not None:
        v = sign * reported
        w = max(
            2 * (claim.reported_std or 0.0),
            cfg.tol_rel * abs(v),
            cfg.tol_abs.get(claim.metric, cfg.default_tol_abs),
        )
        tolerance = {"w": w, "v": reported}
        if measurements.scale is Scale.REDUCED:
            kind, reason = VerdictKind.INCONCLUSIVE, ReasonCode.REDUCED_SCALE_ABSOLUTE
        elif mean >= v - w:
            kind, reason = VerdictKind.REPRODUCED, None
        elif ci_high < v - w:
            kind, reason = VerdictKind.NOT_REPRODUCED, None
        else:
            kind, reason = VerdictKind.INCONCLUSIVE, ReasonCode.HIGH_VARIANCE
    else:  # RELATIVE (an ABSOLUTE claim without a value returned above)
        delta = sign * (claim.claimed_delta or 0.0)
        tolerance = {"rho": cfg.rho, "delta": claim.claimed_delta or 0.0}
        full = measurements.scale is Scale.FULL
        if ci_low > 0 and mean >= cfg.rho * delta:
            kind = VerdictKind.REPRODUCED if full else VerdictKind.REPRODUCED_AT_REDUCED_SCALE
            reason = None
        elif ci_high <= 0:
            kind = VerdictKind.NOT_REPRODUCED if full else VerdictKind.INCONCLUSIVE
            reason = None if full else ReasonCode.REDUCED_SCALE_CONTRADICTS
        else:
            kind, reason = VerdictKind.INCONCLUSIVE, ReasonCode.HIGH_VARIANCE

    # Report numbers in the claim's original direction.
    lo, hi = (ci_low, ci_high) if sign > 0 else (-ci_high, -ci_low)
    return Verdict(
        kind=kind,
        reason=reason,
        n_seeds=n,
        mean=sign * mean,
        std=std,
        ci_low=lo,
        ci_high=hi,
        tolerance=tolerance,
    )

"""Properties P1-P8 from docs/specs/verdict-engine.md (Hypothesis, 500 examples each)."""

import math

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from app.domain.models import (
    Claim,
    ClaimType,
    Measurements,
    ReasonCode,
    Scale,
    VerdictConfig,
    VerdictKind,
)
from app.verdict.engine import decide

CFG = VerdictConfig()
NO_GAP_RULE = VerdictConfig(implausible_ratio=math.inf)  # the verdict before the gap rule
K = CFG.implausible_ratio
EXAMPLES = settings(max_examples=500, deadline=None)

value = st.floats(min_value=-100, max_value=100, allow_nan=False, allow_subnormal=False)
# Metric values with a realistic resolution (1e-4). Used where the test itself transforms the
# data: with arbitrary floats, spreading [0.25, 0.25 + 1e-93] rounds the tiny difference away and
# the "spread" data is no longer the same data (a rounding artefact of the test, not the engine).
grid_value = st.integers(min_value=-1_000_000, max_value=1_000_000).map(lambda i: i / 10_000)
small_pos = st.floats(min_value=1e-3, max_value=10, allow_nan=False)


@st.composite
def claims(draw: st.DrawFn, claim_type: ClaimType | None = None) -> Claim:
    kind = claim_type or draw(st.sampled_from(list(ClaimType)))
    return Claim(
        id="c",
        text="t",
        claim_type=kind,
        metric=draw(st.sampled_from(["accuracy", "loss", "f1", "bleu"])),
        higher_is_better=draw(st.booleans()),
        dataset="d",
        setting="s",
        reported_value=draw(value) if kind is ClaimType.ABSOLUTE else None,
        reported_std=draw(st.none() | st.floats(min_value=0, max_value=5)),
        baseline_name="b" if kind is ClaimType.RELATIVE else None,
        claimed_delta=draw(st.none() | value) if kind is ClaimType.RELATIVE else None,
        source_ref="Table 1",
    )


@st.composite
def cases(
    draw: st.DrawFn, scale: Scale | None = None, values: st.SearchStrategy[float] = value
) -> tuple[Claim, Measurements]:
    claim = draw(claims())
    k = draw(st.integers(min_value=3, max_value=8))
    per_seed = draw(st.lists(values, min_size=k, max_size=k))
    base = (
        draw(st.lists(values, min_size=k, max_size=k))
        if claim.claim_type is ClaimType.RELATIVE
        else None
    )
    meas = Measurements(
        scale=scale or draw(st.sampled_from(list(Scale))), per_seed=per_seed, baseline_per_seed=base
    )
    return claim, meas


@EXAMPLES
@given(cases(scale=Scale.REDUCED))
def test_p1_no_false_accusation_at_reduced_scale(case: tuple[Claim, Measurements]) -> None:
    claim, meas = case
    assert decide(claim, meas, CFG).kind is not VerdictKind.NOT_REPRODUCED


def _spread(values: list[float], factor: float) -> list[float]:
    centre = sum(values) / len(values)
    return [centre + factor * (v - centre) for v in values]


@EXAMPLES
@given(cases(scale=Scale.FULL, values=grid_value), st.floats(min_value=1.5, max_value=10))
def test_p2_more_variance_never_turns_into_not_reproduced(
    case: tuple[Claim, Measurements], factor: float
) -> None:
    claim, meas = case
    verdict = decide(claim, meas, CFG)
    if verdict.reason is not ReasonCode.HIGH_VARIANCE:
        return
    base = meas.baseline_per_seed
    spread = Measurements(
        scale=meas.scale,
        per_seed=_spread(meas.per_seed, factor),
        baseline_per_seed=_spread(base, factor) if base is not None else None,
    )
    assert decide(claim, spread, CFG).kind is not VerdictKind.NOT_REPRODUCED


@EXAMPLES
@given(st.data(), st.floats(min_value=1e-3, max_value=10))
def test_p3_better_is_never_worse(data: st.DataObject, eps: float) -> None:
    claim = data.draw(claims(ClaimType.ABSOLUTE)).model_copy(update={"higher_is_better": True})
    k = data.draw(st.integers(min_value=3, max_value=8))
    per_seed = data.draw(st.lists(value, min_size=k, max_size=k))
    meas = Measurements(scale=Scale.FULL, per_seed=per_seed)
    if decide(claim, meas, CFG).kind is not VerdictKind.REPRODUCED:
        return
    better = Measurements(scale=Scale.FULL, per_seed=[v + eps for v in per_seed])
    assert decide(claim, better, CFG).kind is VerdictKind.REPRODUCED


def _negate(x: float | None) -> float | None:
    return None if x is None else -x


@EXAMPLES
@given(cases())
def test_p4_direction_symmetry(case: tuple[Claim, Measurements]) -> None:
    claim, meas = case
    flipped_claim = claim.model_copy(
        update={
            "higher_is_better": not claim.higher_is_better,
            "reported_value": _negate(claim.reported_value),
            "claimed_delta": _negate(claim.claimed_delta),
        }
    )
    base = meas.baseline_per_seed
    flipped = Measurements(
        scale=meas.scale,
        per_seed=[-v for v in meas.per_seed],
        baseline_per_seed=[-b for b in base] if base is not None else None,
    )
    assert decide(flipped_claim, flipped, CFG).kind is decide(claim, meas, CFG).kind


@EXAMPLES
@given(cases())
def test_p5_purity(case: tuple[Claim, Measurements]) -> None:
    claim, meas = case
    before = (claim.model_dump(), meas.model_dump())
    assert decide(claim, meas, CFG) == decide(claim, meas, CFG)
    assert (claim.model_dump(), meas.model_dump()) == before


@EXAMPLES
@given(cases(), st.integers(min_value=0, max_value=2))
def test_p6_reason_iff_inconclusive(case: tuple[Claim, Measurements], drop: int) -> None:
    claim, meas = case
    # Also exercise the early exits: fewer seeds than min_seeds.
    short = Measurements(
        scale=meas.scale,
        per_seed=meas.per_seed[: len(meas.per_seed) - drop],
        baseline_per_seed=(
            meas.baseline_per_seed[: len(meas.per_seed) - drop]
            if meas.baseline_per_seed is not None
            else None
        ),
    )
    verdict = decide(claim, short, CFG)
    assert (verdict.reason is not None) == (verdict.kind is VerdictKind.INCONCLUSIVE)


def _far_off(claim: Claim, meas: Measurements) -> bool:
    """P7's premise, from the inputs: the mean of the measurements and the paper's value have
    the same sign, neither is zero, and one is more than k times the other."""
    v = claim.reported_value
    mean = math.fsum(meas.per_seed) / len(meas.per_seed)
    if v is None or v == 0 or mean == 0 or (v > 0) != (mean > 0):
        return False
    return abs(mean) > K * abs(v) or abs(v) > K * abs(mean)


@st.composite
def far_off_cases(draw: st.DrawFn) -> tuple[Claim, Measurements]:
    """An ABSOLUTE claim measured k times off or more, either way, at any scale."""
    reported = draw(value.filter(lambda x: abs(x) >= 1e-3))
    claim = draw(claims(ClaimType.ABSOLUTE)).model_copy(update={"reported_value": reported})
    factor = draw(st.floats(min_value=K * 1.01, max_value=1000))
    centre = reported * factor if draw(st.booleans()) else reported / factor
    k = draw(st.integers(min_value=3, max_value=8))
    jitter = st.floats(min_value=-0.004, max_value=0.004)  # keeps the mean beyond k
    per_seed = [centre * (1 + draw(jitter)) for _ in range(k)]
    return claim, Measurements(scale=draw(st.sampled_from(list(Scale))), per_seed=per_seed)


@EXAMPLES
@given(far_off_cases())
def test_p7_a_value_k_times_off_is_never_an_accusation(case: tuple[Claim, Measurements]) -> None:
    claim, meas = case
    assume(_far_off(claim, meas))
    assert decide(claim, meas, CFG).kind is not VerdictKind.NOT_REPRODUCED


@EXAMPLES
@given(st.one_of(cases(), far_off_cases()))
def test_p8_the_gap_rule_only_replaces_an_accusation_or_high_variance(
    case: tuple[Claim, Measurements],
) -> None:
    claim, meas = case
    before, after = decide(claim, meas, NO_GAP_RULE), decide(claim, meas, CFG)
    if after == before:
        return
    assert claim.claim_type is ClaimType.ABSOLUTE and meas.scale is Scale.FULL
    assert _far_off(claim, meas)
    assert before.kind is VerdictKind.NOT_REPRODUCED or before.reason is ReasonCode.HIGH_VARIANCE
    assert (after.kind, after.reason) == (VerdictKind.INCONCLUSIVE, ReasonCode.IMPLAUSIBLE_GAP)
    assert after.model_copy(update={"kind": before.kind, "reason": before.reason}) == before

"""The gallery's verdict summary is derived from the deterministic verdicts only."""

import itertools

from app.domain.models import ReasonCode, Verdict, VerdictKind
from app.verdict.summary import MAX_SUMMARY_CHARS, verdict_summary


def _v(kind: VerdictKind) -> Verdict:
    reason = ReasonCode.BUDGET if kind is VerdictKind.INCONCLUSIVE else None
    return Verdict(
        kind=kind, reason=reason, n_seeds=3, mean=None, std=None, ci_low=None, ci_high=None,
        tolerance={},
    )  # fmt: skip


def test_one_verdict() -> None:
    assert verdict_summary([_v(VerdictKind.NOT_REPRODUCED)]) == "1 NOT_REPRODUCED"


def test_counts_in_a_fixed_order_whatever_the_claim_order() -> None:
    verdicts = [
        _v(VerdictKind.NOT_REPRODUCED),
        _v(VerdictKind.REPRODUCED),
        _v(VerdictKind.NOT_REPRODUCED),
    ]
    assert verdict_summary(verdicts) == "1 REPRODUCED · 2 NOT_REPRODUCED"
    assert verdict_summary(list(reversed(verdicts))) == "1 REPRODUCED · 2 NOT_REPRODUCED"


def test_no_verdict_is_no_summary() -> None:
    assert verdict_summary([]) is None


def test_always_fits_the_column_never_cuts_a_kind_and_never_hides_a_verdict() -> None:
    kinds = list(VerdictKind)
    for n in range(1, len(kinds) + 1):
        for combo in itertools.combinations(kinds, n):
            summary = verdict_summary([_v(k) for k in combo for _ in range(12)])
            assert summary is not None and len(summary) <= MAX_SUMMARY_CHARS
            counted = 0
            for part in summary.split(" · "):
                count, kind = part.split(" ")
                if kind == "more":  # what did not fit is counted, never dropped silently
                    counted += int(count.lstrip("+"))
                    continue
                assert int(count) == 12 and VerdictKind(kind) in combo
                counted += 12
            assert counted == 12 * n


def test_a_summary_that_does_not_fit_says_how_many_verdicts_are_left_out() -> None:
    verdicts = [_v(k) for k in VerdictKind for _ in range(12)]
    assert verdict_summary(verdicts) == "12 REPRODUCED · 12 REPRODUCED_AT_REDUCED_SCALE · +24 more"

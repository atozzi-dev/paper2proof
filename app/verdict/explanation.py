"""Explanation guard and templates. The Judge model only writes prose (invariant 2): its text is
used only if it names no verdict other than the one decide() returned; otherwise, or when the
Judge is unavailable, a template written here is used instead."""

import re

from app.domain.models import Claim, ClaimType, Verdict, VerdictKind

NOT_REPRODUCED_LABEL = "Not reproduced under our conditions"  # invariant 10
_SEP = r"[\s_-]+"
_MENTIONS = [  # longest first: each match is removed before the shorter patterns run
    (
        VerdictKind.REPRODUCED_AT_REDUCED_SCALE,
        re.compile(rf"reproduced{_SEP}at{_SEP}reduced{_SEP}scale", re.I),
    ),
    (VerdictKind.NOT_REPRODUCED, re.compile(rf"not{_SEP}reproduced", re.I)),
    (VerdictKind.INCONCLUSIVE, re.compile(r"inconclusive", re.I)),
    (VerdictKind.REPRODUCED, re.compile(r"reproduced", re.I)),
]


def mentions_verdict(text: str) -> bool:
    """True when the text names any verdict (display-only text must never look like one)."""
    return any(pattern.search(text) for _, pattern in _MENTIONS)


def contradicts(text: str, kind: VerdictKind) -> bool:
    """True when the text is empty or mentions a verdict other than `kind`."""
    if not text.strip():
        return True
    found: set[VerdictKind] = set()
    for mentioned, pattern in _MENTIONS:
        if pattern.search(text):
            found.add(mentioned)
            text = pattern.sub(" ", text)
    return bool(found - {kind})


def _numbers(verdict: Verdict, claim: Claim) -> str:
    if verdict.mean is None:
        return "No usable measurements were collected."
    interval = ""
    if verdict.ci_low is not None and verdict.ci_high is not None:
        interval = f" (95% CI {verdict.ci_low:.4g} to {verdict.ci_high:.4g})"
    if claim.claim_type is ClaimType.RELATIVE:
        claimed = (
            f"{claim.claimed_delta:g}" if claim.claimed_delta is not None else "an improvement"
        )
        return (
            f"Mean difference in {claim.metric} against {claim.baseline_name}: "
            f"{verdict.mean:.4g}{interval} over {verdict.n_seeds} seeds; "
            f"the paper claims {claimed}."
        )
    return (
        f"Mean {claim.metric}: {verdict.mean:.4g}{interval} over {verdict.n_seeds} seeds; "
        f"the paper reports {claim.reported_value:g}."
    )


def template_explanation(verdict: Verdict, claim: Claim) -> str:
    numbers = _numbers(verdict, claim)
    if verdict.kind is VerdictKind.NOT_REPRODUCED:
        return f"{NOT_REPRODUCED_LABEL}. {numbers}"
    if verdict.kind is VerdictKind.REPRODUCED:
        return f"Reproduced within the configured tolerance. {numbers}"
    if verdict.kind is VerdictKind.REPRODUCED_AT_REDUCED_SCALE:
        return f"Reproduced at reduced scale. {numbers}"
    reason = verdict.reason.value.replace("_", " ").lower() if verdict.reason else "unknown"
    return f"Inconclusive ({reason}). {numbers}"

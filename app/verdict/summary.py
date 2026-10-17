"""The run's one-line verdict summary (gallery cards), derived from the deterministic verdicts
only: never from model text (invariant 2). Raw kinds are stored; the UI shows them with its
copy rules (invariant 10)."""

from collections import Counter
from collections.abc import Iterable

from app.domain.models import Verdict, VerdictKind

MAX_SUMMARY_CHARS = 64  # runs.verdict_summary is String(64)
SEPARATOR = " · "


def verdict_summary(verdicts: Iterable[Verdict]) -> str | None:
    """`"<n> <KIND>"` per kind, in VerdictKind order, e.g. `1 REPRODUCED · 1 NOT_REPRODUCED`.
    Kinds that don't fit the column are counted as `+<n> more`, never cut or dropped."""
    counts = Counter(v.kind for v in verdicts)
    parts = [(counts[kind], f"{counts[kind]} {kind.value}") for kind in VerdictKind if counts[kind]]
    if not parts:
        return None
    shown: list[str] = []
    for i, (_, text) in enumerate(parts):
        rest = sum(n for n, _ in parts[i + 1 :])
        tail = [f"+{rest} more"] if rest else []
        if len(SEPARATOR.join([*shown, text, *tail])) > MAX_SUMMARY_CHARS:
            left = sum(n for n, _ in parts[i:])
            return SEPARATOR.join([*shown, f"+{left} more"])
        shown.append(text)
    return SEPARATOR.join(shown)

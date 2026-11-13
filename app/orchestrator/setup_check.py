"""The paper's setup against what a run uses (docs/specs/execution-contracts.md §6.1).

The Planner lists the conditions the paper states (`Plan.setup`) and the Coder declares, at
finish, the value its script uses for each. Everything here is deterministic and pure: models
supply the facts, this code compares them and decides whether the run is at the paper's scale.

The cross-check against the cited file is a text search, a tripwire and not a proof: a value
written in a constant the script never uses passes it (docs/LIMITATIONS.md).
"""

import math
import re
from collections.abc import Mapping, Sequence

from app.domain.models import SetupItem, SetupKind, SetupRecord

MAX_SETUP_ITEMS = 8
MIN_CHOICE_CHARS = 2  # a shorter normalized value would be found in any file
_UNSIGNED = r"(?:\d[\d_]*(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
_NUMBER = re.compile(rf"[-+]?{_UNSIGNED}")
# A numeric token of a file: never part of a name or of a longer number, and without commas,
# which separate arguments in code (`f(1,2)` holds 1 and 2).
_TOKEN = re.compile(rf"(?<![\w.]){_UNSIGNED}(?![\w.])")


def as_number(text: str) -> float | None:
    """The finite number a value states ("2000", "2e3", "2,000"), or None."""
    cleaned = text.strip().replace(",", "")
    if not _NUMBER.fullmatch(cleaned):
        return None
    try:
        value = float(cleaned)
    except ValueError:  # "2__000" passes the pattern, not float()
        return None
    return value if math.isfinite(value) else None


def normal(text: str) -> str:
    """Casefolded, letters and digits only: "L-BFGS" and "lbfgs" are the same choice."""
    return "".join(ch for ch in text.casefold() if ch.isalnum())


def same_value(kind: SetupKind, a: str, b: str) -> bool:
    if kind is SetupKind.NUMBER:
        x, y = as_number(a), as_number(b)
        return x is not None and y is not None and math.isclose(x, y, rel_tol=1e-9, abs_tol=0.0)
    return bool(normal(a)) and normal(a) == normal(b)


def usable(item: SetupItem) -> bool:
    """Whether code can compare this condition at all."""
    if item.kind is SetupKind.NUMBER:
        return as_number(item.paper) is not None and as_number(item.planned) is not None
    return min(len(normal(item.paper)), len(normal(item.planned))) >= MIN_CHOICE_CHARS


def written_in(kind: SetupKind, value: str, text: str) -> bool:
    """Whether `text` (a file) states `value`: a number as a whole numeric token of the same
    value (the sign is punctuation there), a choice as normalized text."""
    if kind is SetupKind.NUMBER:
        target = as_number(value)
        if target is None:
            return False
        for token in _TOKEN.finditer(text):
            found = as_number(token.group())
            if found is not None and math.isclose(found, abs(target), rel_tol=1e-9, abs_tol=0.0):
                return True
        return False
    needle = normal(value)
    return len(needle) >= MIN_CHOICE_CHARS and needle in normal(text)


def correct_setup(items: Sequence[SetupItem]) -> tuple[list[SetupItem], list[str]]:
    """The conditions code can check: comparable, one per name, the first MAX_SETUP_ITEMS,
    renumbered s1, s2, ... The note is code-written and gives only a count."""
    kept: list[SetupItem] = []
    names: set[str] = set()
    for item in items:
        name = normal(item.name)
        if not usable(item) or name in names or len(kept) == MAX_SETUP_ITEMS:
            continue
        names.add(name)
        kept.append(item.model_copy(update={"id": f"s{len(kept) + 1}"}))
    dropped = len(items) - len(kept)
    if not dropped:
        return kept, []
    plural = "s" if dropped != 1 else ""
    return kept, [
        f"{dropped} setup condition{plural} dropped (not comparable, repeated or beyond the "
        f"first {MAX_SETUP_ITEMS})"
    ]


def reduced_by(items: Sequence[SetupItem]) -> list[str]:
    """Why a plan with these (corrected) conditions cannot be at full scale: nothing to check
    it against, or a value planned away from the paper's. Empty when nothing reduces it."""
    if not items:
        return ["scale REDUCED: the plan lists no condition of the paper's setup to check"]
    return [
        f"scale REDUCED: {item.name} planned {item.planned}, the paper uses {item.paper}"
        for item in items
        if not same_value(item.kind, item.planned, item.paper)
    ]


def setup_problems(
    items: Sequence[SetupItem],
    declared: Sequence[tuple[str, str, str]],
    files: Mapping[str, str],
) -> list[str]:
    """What is wrong with a finish declaration, `(id, value, where)` per condition: ids first,
    then unknown files, then values not written in the file they cite; one kind at a time.
    `files` maps `workspace/<path>` and `repo/<path>` to their text. The messages go back to
    the Coder outside the untrusted wrapper, so they carry ids and the Coder's own words only,
    never a condition's name or values (the paper's text)."""
    ids = [ident for ident, _, _ in declared]
    known = {item.id: item for item in items}
    problems = [f"{i} is declared more than once" for i in dict.fromkeys(ids) if ids.count(i) > 1]
    problems += [
        f"{i} is not a condition of the plan" for i in dict.fromkeys(ids) if i not in known
    ]
    problems += [f"{item.id} is not declared" for item in items if item.id not in ids]
    if problems:
        return problems
    problems = [
        f"{ident}: {where} is not a file of the workspace or of the repository"
        for ident, _, where in declared
        if where not in files
    ]
    if problems:
        return problems
    return [
        f"{ident}: {value} is not written in {where}"
        for ident, value, where in declared
        if not written_in(known[ident].kind, value, files[where])
    ]


def setup_records(
    items: Sequence[SetupItem], declared: Sequence[tuple[str, str, str]]
) -> list[SetupRecord]:
    """One record per condition, in the plan's order, for a declaration `setup_problems`
    accepted. `matches_paper` compares the value used with the paper's, never with the plan's:
    a reduction the plan chose is still a difference from the paper."""
    used = {ident: (value, where) for ident, value, where in declared}
    return [
        SetupRecord(
            id=item.id,
            name=item.name,
            kind=item.kind,
            paper=item.paper,
            planned=item.planned,
            used=used[item.id][0],
            where=used[item.id][1],
            matches_paper=same_value(item.kind, used[item.id][0], item.paper),
        )
        for item in items
    ]

"""The paper's setup against what a run uses (docs/specs/execution-contracts.md §6.1).

The Planner lists the conditions the paper states (`Plan.setup`) and the Coder declares, at
finish, what its script uses for each. Everything here is deterministic and pure: models
supply the facts, this code compares them and decides whether the run is at the paper's scale.

Numbers are compared by value and looked up in the file the declaration cites: a text search,
a tripwire and not a proof (a value written in a constant the script never uses passes it).
Choices are stated by papers in prose that no text comparison matches with code, so for them
the Coder's declaration decides, as for a metric's unit (docs/LIMITATIONS.md).

The gate fails closed: a reduction the Planner states is never lost with a condition code
cannot keep, and every text is cleaned before it is stored (it is shown next to verdicts).
"""

import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import NamedTuple

from app.domain.models import SetupItem, SetupKind, SetupRecord

MAX_SETUP_ITEMS = 8
MIN_CHOICE_CHARS = 2  # a shorter normalized choice says nothing
STORED_CHARS = 80  # the bound of SetupRecord's text fields
_UNSIGNED = r"(?:\d[\d_]*(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
_NUMBER = re.compile(rf"[-+]?{_UNSIGNED}")
# A numeric token of a file: never part of a name or of a longer number, and without commas,
# which separate arguments in code (`f(1,2)` holds 1 and 2).
_TOKEN = re.compile(rf"(?<![\w.]){_UNSIGNED}(?![\w.])")


class Declared(NamedTuple):
    """What `finish` declares for one condition: the value the script uses, the file that
    states it, and whether that is the paper's choice (read for a CHOICE only)."""

    id: str
    value: str
    where: str
    as_paper: bool


def clean(text: str) -> str:
    """One line with no control, format or other invisible characters (ANSI escapes, bidi
    overrides): model text that is stored and later shown next to verdicts."""
    spaced = "".join(" " if ch.isspace() else ch for ch in text)
    visible = "".join(ch for ch in spaced if not unicodedata.category(ch).startswith("C"))
    return " ".join(visible.split())


def _cut(text: str) -> str:
    """The Planner's text is read leniently and cut here to what a record holds."""
    return text[:STORED_CHARS].rstrip()


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


def _planned(item: SetupItem) -> str:
    """Left out, the planned value is the paper's."""
    return item.paper if item.planned is None else item.planned


def _plans_other(item: SetupItem) -> bool:
    """The Planner states a value other than the paper's: a reduction, however it is written
    and whether or not code can keep the condition."""
    if item.planned is None:
        return False
    if same_value(SetupKind.NUMBER, item.planned, item.paper):
        return False
    if same_value(SetupKind.CHOICE, item.planned, item.paper):
        return False
    return clean(item.planned) != clean(item.paper)


def _tidy(item: SetupItem) -> SetupItem | None:
    """The condition with its text cleaned, or None when nothing is left of its name or of the
    paper's value. A NUMBER the paper states in words ("200 epochs") is kept as a CHOICE:
    dropped, it would take the reduction planned on it out of the gate."""
    name, paper = _cut(clean(item.name)), _cut(clean(item.paper))
    planned = None if item.planned is None else _cut(clean(item.planned)) or None
    if not name or not paper:
        return None
    # The kind follows the values, not the Planner's label: numbers are compared as numbers
    # (as text, "1.0" and "10" would normalize to the same choice), words as a choice.
    numeric = as_number(paper) is not None and (planned is None or as_number(planned) is not None)
    kind = SetupKind.NUMBER if numeric else SetupKind.CHOICE
    return item.model_copy(
        update={
            "name": name,
            "kind": kind,
            "paper": paper,
            "planned": planned,
            "id": item.id[:8],
            "source_ref": _cut(clean(item.source_ref)),
        }
    )


def usable(item: SetupItem) -> bool:
    """Whether code can compare this condition at all."""
    if item.kind is SetupKind.NUMBER:
        return as_number(item.paper) is not None and as_number(_planned(item)) is not None
    return min(len(normal(item.paper)), len(normal(_planned(item)))) >= MIN_CHOICE_CHARS


def _select(items: Sequence[SetupItem]) -> tuple[list[SetupItem], list[SetupItem]]:
    """(kept, dropped): the conditions code can check, one per name, at most MAX_SETUP_ITEMS,
    those that plan a reduction first so that the cap or a repeated name never drops one; and
    the listed conditions that were not kept, as the Planner wrote them."""
    tidied = [(index, _tidy(item)) for index, item in enumerate(items)]
    candidates = [(i, t) for i, t in tidied if t is not None and usable(t)]
    candidates.sort(key=lambda pair: not _plans_other(pair[1]))  # stable: reductions first
    kept: list[tuple[int, SetupItem]] = []
    names: set[str] = set()
    for index, item in candidates:
        name = normal(item.name)
        if name in names or len(kept) == MAX_SETUP_ITEMS:
            continue
        names.add(name)
        kept.append((index, item))
    taken = {index for index, _ in kept}
    return [item for _, item in kept], [item for i, item in enumerate(items) if i not in taken]


def correct_setup(items: Sequence[SetupItem]) -> tuple[list[SetupItem], list[str]]:
    """The conditions code can check (`_select`), renumbered s1, s2, ..., each with its text
    cleaned and its planned value filled in. The note is code-written and gives only a
    count."""
    selected, dropped = _select(items)
    kept = [
        item.model_copy(update={"id": f"s{number}", "planned": _planned(item)})
        for number, item in enumerate(selected, start=1)
    ]
    if not dropped:
        return kept, []
    plural = "s" if len(dropped) != 1 else ""
    return kept, [
        f"{len(dropped)} setup condition{plural} dropped (not comparable, repeated or beyond "
        f"the first {MAX_SETUP_ITEMS})"
    ]


def reduced_by(items: Sequence[SetupItem], listed: Sequence[SetupItem] | None = None) -> list[str]:
    """Why a plan with these (corrected) conditions cannot be at full scale: nothing to check
    it against, a value planned away from the paper's, or, among the conditions the Planner
    `listed`, one that could not be kept but planned something else. Empty when nothing
    reduces it."""
    lost = sum(_plans_other(item) for item in _select(listed)[1]) if listed is not None else 0
    notes = (
        [
            f"scale REDUCED: {item.name} planned {_planned(item)}, the paper uses {item.paper}"
            for item in items
            if not same_value(item.kind, _planned(item), item.paper)
        ]
        if items
        else ["scale REDUCED: the plan lists no condition of the paper's setup to check"]
    )
    if lost:  # code-written, a count only: the dropped text is not repeated
        plural = "s" if lost != 1 else ""
        notes.append(
            f"scale REDUCED: {lost} listed condition{plural} that could not be kept planned "
            "something else than the paper's value"
        )
    return notes


def _numbers_in(text: str) -> list[float]:
    """Every number written in a file as a whole numeric token (unsigned: the sign is
    punctuation there), found in one pass."""
    found = (as_number(token.group()) for token in _TOKEN.finditer(text))
    return [value for value in found if value is not None]


def number_written_in(value: str, text: str) -> bool:
    """Whether `text` (a file) states the number `value`."""
    return _written(value, _numbers_in(text))


def _written(value: str, numbers: Sequence[float]) -> bool:
    target = as_number(value)
    if target is None:
        return False
    return any(math.isclose(n, abs(target), rel_tol=1e-9, abs_tol=0.0) for n in numbers)


def setup_problems(
    items: Sequence[SetupItem], declared: Sequence[Declared], files: Mapping[str, str]
) -> list[str]:
    """What is wrong with a finish declaration: ids first, then unknown files, then values
    that are empty, numbers that are not numbers or not written in the file they cite; one
    kind at a time. `files` maps `workspace/<path>` and `repo/<path>` to their text; each
    cited file is scanned once. The messages go back to the Coder outside the untrusted
    wrapper, so they carry ids and the Coder's own words only, never a condition's name or
    values (the paper's text)."""
    ids = [d.id for d in declared]
    known = {item.id: item for item in items}
    problems = [f"{i} is declared more than once" for i in dict.fromkeys(ids) if ids.count(i) > 1]
    problems += [
        f"{i} is not a condition of the plan" for i in dict.fromkeys(ids) if i not in known
    ]
    problems += [f"{item.id} is not declared" for item in items if item.id not in ids]
    if problems:
        return problems
    problems = [
        f"{d.id}: {clean(d.where)} is not a file of the workspace or of the repository"
        for d in declared
        if d.where not in files
    ]
    if problems:
        return problems
    numbers: dict[str, list[float]] = {}
    for d in declared:
        value = clean(d.value)
        if not value:
            problems.append(f"{d.id}: the declared value is empty")
        elif known[d.id].kind is not SetupKind.NUMBER:
            continue  # a choice is declared, not looked up
        elif as_number(value) is None:
            problems.append(f"{d.id}: {value} is not a number")
        else:
            if d.where not in numbers:
                numbers[d.where] = _numbers_in(files[d.where])
            if not _written(value, numbers[d.where]):
                problems.append(f"{d.id}: {value} is not written in {clean(d.where)}")
    return problems


def _matches_paper(item: SetupItem, declared: Declared) -> bool:
    """A number by its value against the paper's, never against the plan's (a reduction the
    plan chose is still a difference from the paper); a choice by the declaration."""
    if item.kind is SetupKind.NUMBER:
        return same_value(item.kind, declared.value, item.paper)
    return declared.as_paper


def setup_records(items: Sequence[SetupItem], declared: Sequence[Declared]) -> list[SetupRecord]:
    """One record per condition, in the plan's order, for a declaration `setup_problems`
    accepted. Every text is cleaned: the records are shown with the verdicts."""
    by_id = {d.id: d for d in declared}
    return [
        SetupRecord(
            id=item.id,
            name=clean(item.name),
            kind=item.kind,
            paper=clean(item.paper),
            planned=clean(_planned(item)),
            used=clean(by_id[item.id].value),
            where=clean(by_id[item.id].where),
            matches_paper=_matches_paper(item, by_id[item.id]),
        )
        for item in items
    ]


def off_plan(items: Sequence[SetupItem], declared: Sequence[Declared]) -> list[str]:
    """Ids whose declaration departs from the plan: a number other than the planned one, or a
    choice declared not the paper's while the plan follows the paper. Worth one hint: the
    planned value may simply not run here."""
    by_id = {d.id: d for d in declared}
    out = []
    for item in items:
        used = by_id[item.id]
        if item.kind is SetupKind.NUMBER:
            departs = not same_value(item.kind, used.value, _planned(item))
        else:
            departs = not used.as_paper and same_value(item.kind, _planned(item), item.paper)
        if departs:
            out.append(item.id)
    return out

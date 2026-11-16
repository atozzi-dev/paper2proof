"""The Limitations section of every verdict, written by code from the manifest and the results
(never by a model): scale, seeds (and seeds that changed nothing), hardware, any
percent/fraction conversion and deviations from the paper's setup."""

import re
from collections.abc import Sequence

from app.domain.contracts import ScaleSpec
from app.domain.models import Measurements, SetupKind, SetupRecord
from app.verdict.explanation import mentions_verdict
from app.verdict.scale import ScaleNote

DEVIATION_CHARS = 300
_SPACE = re.compile(r"\s+")
_MARKUP = re.compile(r"[#*_`>|\[\]<]")
_PATH = re.compile(r"(?:workspace|repo)/[A-Za-z0-9._/-]{1,190}")  # printed as it is

_EXIT = {
    "completed": None,
    "experiment_error": "at least one seed failed (experiment error)",
    "budget_timeout": "the run hit its time budget before every seed finished",
    "contract_error": "the results broke the execution contract",
    "capacity": "no capacity was available for the experiment, so it did not run",
}
SANDBOX_PLATFORM = "sandbox"  # app/execution/sandbox_jobs.py (Token Factory Sandboxes)


def _seeds_line(used: int, planned: int, not_started: int) -> str:
    """Seeds the time rule left out are not failures (A-46): "6 of 10 planned seeds" would read
    as four of them."""
    if not not_started:
        return f"{used} of {planned} planned seeds produced usable values."
    more = "1 more was" if not_started == 1 else f"{not_started} more were"
    return (
        f"{used} of the {planned - not_started} seeds that ran produced usable values; {more} "
        "not started: the time target was reached (the seed count depends on run time only, "
        "never on the values)."
    )


def limitations(
    *,
    scale: ScaleSpec,
    planned_seeds: list[int],
    used_seeds: list[int],
    gpu: str | None,
    exit_reason: str,
    deviations: list[str],
    scale_note: ScaleNote | None = None,
    unmeasured_reason: str | None = None,
    platform: str | None = None,
    preset: str | None = None,
    measurements: Measurements | None = None,
    stand_in_flags: list[str] | None = None,
    setup: Sequence[SetupRecord] = (),
    not_started: Sequence[int] = (),
) -> str:
    """`gpu` is what the job reported (results.json: "cpu" without a GPU); `platform` and
    `preset` are where EXECUTING submitted it (None for runs recorded before they were kept).
    `measurements` are the claim's decided values, one per used seed (None: not measured)."""
    lines: list[str] = []
    differ = [record for record in setup if not record.matches_paper]
    if scale.full and setup:  # EXECUTING made it full because every record matches
        lines.append(f"Run at full scale: {matched_text(setup)}.")
    elif scale.full:  # a run from before the setup was listed
        lines.append("Run at full scale (the paper's data and schedule).")
    else:
        parts = []
        if scale.fraction_of_data is not None:
            parts.append(f"{scale.fraction_of_data:.0%} of the data")
        if scale.epochs is not None:
            of = f" of {scale.paper_epochs}" if scale.paper_epochs else ""
            parts.append(f"{scale.epochs}{of} epochs")
        if differ:
            parts.append(f"{len(differ)} of {len(setup)} listed conditions differ from the paper")
        detail = f": {', '.join(parts)}" if parts else ""
        lines.append(f"Run at reduced scale{detail}.")
    if unmeasured_reason is not None:  # the Coder's reason is model text: plain, bounded
        why = _plain(unmeasured_reason)
        lines.append(
            "Not measured: the experiment does not measure this claim"
            + (f" (the Coder's reason, model text: {why})." if why else ".")
        )
    lines.append(_seeds_line(len(used_seeds), len(planned_seeds), len(not_started)))
    same = _same_on_every_seed(measurements)
    if same:
        lines.append(same)
    lines.append(f"Hardware: {_hardware(gpu, platform, preset)}.")
    problem = _EXIT.get(exit_reason)
    if problem:
        lines.append(f"Execution: {problem}.")
    if scale_note:  # written by app/verdict/scale.py, never by a model
        lines.append(scale_note)
    if stand_in_flags:  # code-written workspace/<path>:<line> (VerdictStep checks the shape)
        lines.append(
            "The Coder's script first said it used a stand-in for the paper's method or data ("
            + ", ".join(stand_in_flags)
            + ") and was changed before the experiment ran: check the code in the Proof Bundle."
        )
    lines += [f"Setup: {setup_line(r)}." for r in differ]
    lines += [f"Deviation: {_plain(d)}" for d in deviations if _plain(d)]
    return "\n".join(f"- {line}" for line in lines)


def _same_on_every_seed(measurements: Measurements | None) -> str | None:
    """Seeds that changed nothing (KAN, 2404.19756: std 0, a CI collapsed to a point): the
    seeds agree only because they were one run, which the verdict cannot see. Exact equality,
    on every arm the claim has (method, and baseline for a RELATIVE claim)."""
    if measurements is None or len(measurements.per_seed) < 2:
        return None
    baseline = measurements.baseline_per_seed
    arms = [measurements.per_seed] if baseline is None else [measurements.per_seed, baseline]
    if any(len(set(arm)) > 1 for arm in arms):
        return None
    which = "value" if baseline is None else "values, for the method and for the baseline"
    return (
        f"All {len(measurements.per_seed)} seeds gave exactly the same {which}: the seed does "
        "not seem to change this experiment, so the agreement across seeds says nothing about "
        "its variability."
    )


def _hardware(gpu: str | None, platform: str | None, preset: str | None) -> str:
    where = ""
    if platform:
        service = (
            "Nebius Token Factory Sandboxes"
            if platform == SANDBOX_PLATFORM
            else (f"Nebius AI Cloud {platform}")
        )
        where = f" ({service}{f', {preset}' if preset else ''})"
    reported = gpu if gpu and gpu.lower() not in ("cpu", "unknown") else None
    if (platform or "").startswith("gpu-") or (platform is None and reported):
        return f"one GPU, {reported or 'its name not reported'}{where}"
    if platform or gpu:
        return f"CPU only, no GPU{where}"
    return "not reported"


WITHHELD = "(withheld)"


def shown(text: str) -> str:
    """Model text inside a code-written line: one plain bounded line, and never a verdict word
    (a hostile paper can name a condition "Verdict: REPRODUCED at full scale")."""
    plain = _plain(text)
    return WITHHELD if not plain or mentions_verdict(plain) else plain


def _shown_path(where: str) -> str:
    """A plain workspace or repository path is printed as code, so its underscores stay as
    they are; anything else is cleaned like other model text."""
    if mentions_verdict(where):
        return WITHHELD
    return f"`{where}`" if _PATH.fullmatch(where) else _plain(where) or WITHHELD


def matched_text(setup: Sequence[SetupRecord]) -> str:
    """How a run whose every listed condition matches stands against the paper's setup, without
    claiming more than was checked: numbers were found in the code, choices are the Coder's
    declaration."""
    numbers = sum(record.kind is SetupKind.NUMBER for record in setup)
    choices = len(setup) - numbers
    parts = []
    if numbers:
        parts.append(f"{numbers} number{'s' if numbers != 1 else ''} found in the code")
    if choices:
        parts.append(f"{choices} choice{'s' if choices != 1 else ''} as declared by the Coder")
    head = (
        "the 1 listed condition matches the paper"
        if len(setup) == 1
        else f"all {len(setup)} listed conditions match the paper"
    )
    return f"{head} ({', '.join(parts)})"


def setup_line(record: SetupRecord) -> str:
    """One difference between the run and the paper's setup, in the words used everywhere it
    is shown (the Limitations, the SETUP_DEVIATION warning). The quoted parts are the paper's
    and the Coder's text."""
    return (
        f'"{shown(record.name)}": the paper uses "{shown(record.paper)}", this run used '
        f'"{shown(record.used)}" ({_shown_path(record.where)})'
    )


def _plain(text: str) -> str:
    """Deviations are written by the planner model: one line, no markup, bounded, so they can
    never pass for a line written here."""
    text = _SPACE.sub(" ", _MARKUP.sub("", text)).strip()
    if len(text) > DEVIATION_CHARS:
        text = text[: DEVIATION_CHARS - 3].rstrip() + "..."
    return text

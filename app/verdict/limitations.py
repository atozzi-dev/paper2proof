"""The Limitations section of every verdict, written by code from the manifest and the results
(never by a model): scale, seeds, hardware, any percent/fraction conversion and deviations
from the paper's setup."""

import re

from app.domain.contracts import ScaleSpec
from app.verdict.scale import ScaleNote

DEVIATION_CHARS = 300
_SPACE = re.compile(r"\s+")
_MARKUP = re.compile(r"[#*_`>|\[\]<]")

_EXIT = {
    "completed": None,
    "experiment_error": "at least one seed failed (experiment error)",
    "budget_timeout": "the run hit its time budget before every seed finished",
    "contract_error": "the results broke the execution contract",
    "capacity": "no GPU capacity was available, so the experiment did not run",
}


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
) -> str:
    lines: list[str] = []
    if scale.full:
        lines.append("Run at full scale (the paper's data and schedule).")
    else:
        parts = []
        if scale.fraction_of_data is not None:
            parts.append(f"{scale.fraction_of_data:.0%} of the data")
        if scale.epochs is not None:
            of = f" of {scale.paper_epochs}" if scale.paper_epochs else ""
            parts.append(f"{scale.epochs}{of} epochs")
        detail = f": {', '.join(parts)}" if parts else ""
        lines.append(f"Run at reduced scale{detail}.")
    if unmeasured_reason is not None:  # the Coder's reason is model text: plain, bounded
        why = _plain(unmeasured_reason)
        lines.append(
            "Not measured: the experiment does not measure this claim"
            + (f" (the Coder's reason, model text: {why})." if why else ".")
        )
    lines.append(f"{len(used_seeds)} of {len(planned_seeds)} planned seeds produced usable values.")
    lines.append(f"Hardware: {gpu or 'unknown'}, one GPU.")
    problem = _EXIT.get(exit_reason)
    if problem:
        lines.append(f"Execution: {problem}.")
    if scale_note:  # written by app/verdict/scale.py, never by a model
        lines.append(scale_note)
    lines += [f"Deviation: {_plain(d)}" for d in deviations if _plain(d)]
    return "\n".join(f"- {line}" for line in lines)


def _plain(text: str) -> str:
    """Deviations are written by the planner model: one line, no markup, bounded, so they can
    never pass for a line written here."""
    text = _SPACE.sub(" ", _MARKUP.sub("", text)).strip()
    if len(text) > DEVIATION_CHARS:
        text = text[: DEVIATION_CHARS - 3].rstrip() + "..."
    return text

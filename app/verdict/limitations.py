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
    "capacity": "no capacity was available for the experiment, so it did not run",
}
SANDBOX_PLATFORM = "sandbox"  # app/execution/sandbox_jobs.py (Token Factory Sandboxes)


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
) -> str:
    """`gpu` is what the job reported (results.json: "cpu" without a GPU); `platform` and
    `preset` are where EXECUTING submitted it (None for runs recorded before they were kept)."""
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
    lines.append(f"Hardware: {_hardware(gpu, platform, preset)}.")
    problem = _EXIT.get(exit_reason)
    if problem:
        lines.append(f"Execution: {problem}.")
    if scale_note:  # written by app/verdict/scale.py, never by a model
        lines.append(scale_note)
    lines += [f"Deviation: {_plain(d)}" for d in deviations if _plain(d)]
    return "\n".join(f"- {line}" for line in lines)


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


def _plain(text: str) -> str:
    """Deviations are written by the planner model: one line, no markup, bounded, so they can
    never pass for a line written here."""
    text = _SPACE.sub(" ", _MARKUP.sub("", text)).strip()
    if len(text) > DEVIATION_CHARS:
        text = text[: DEVIATION_CHARS - 3].rstrip() + "..."
    return text

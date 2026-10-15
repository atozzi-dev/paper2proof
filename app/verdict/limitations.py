"""The Limitations section of every verdict, written by code from the manifest and the results
(never by a model): scale, seeds, hardware and deviations from the paper's setup."""

from app.domain.contracts import ScaleSpec

_EXIT = {
    "completed": None,
    "experiment_error": "at least one seed failed (experiment error)",
    "budget_timeout": "the run hit its time budget before every seed finished",
    "contract_error": "the results broke the execution contract",
}


def limitations(
    *,
    scale: ScaleSpec,
    planned_seeds: list[int],
    used_seeds: list[int],
    gpu: str | None,
    exit_reason: str,
    deviations: list[str],
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
    lines.append(f"{len(used_seeds)} of {len(planned_seeds)} planned seeds produced usable values.")
    lines.append(f"Hardware: {gpu or 'unknown'}, one GPU.")
    problem = _EXIT.get(exit_reason)
    if problem:
        lines.append(f"Execution: {problem}.")
    lines += [f"Deviation: {d}" for d in deviations]
    return "\n".join(f"- {line}" for line in lines)

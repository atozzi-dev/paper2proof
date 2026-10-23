"""ReproBench-mini report: Markdown tables for the README and the JSON for /api/stats.

Reads eval/results/{set_a,set_b,set_c,ablation}.json (a set not run yet is shown as such).
Free and offline:

    uv run python -m eval.report            # writes eval/results/report.md and stats.json

Verdicts use the UI copy rules (docs/specs/api-and-events.md): NOT_REPRODUCED is
"Not reproduced under our conditions", with the conditions stated next to the table.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from app.domain.models import VerdictKind
from eval.pipeline import write_result
from eval.results import (
    RESULT_FILES,
    AblationResult,
    CaseA,
    ClaimOutcome,
    CostSummary,
    EvalResults,
    ExtractionScore,
    SetAResult,
    SetBResult,
    SetCResult,
)

RESULTS_DIR = Path(__file__).resolve().parent / "results"
NOT_RUN = "_Not run yet._\n"
SET_B_CONDITIONS = (
    "Conditions: the scale, seeds and hardware of each run's plan, which can differ from the "
    "paper's; each run's Proof Bundle lists them.\n"
)
TARGET_ACCURACY = 0.80  # architecture §18.2
TARGET_FALSE_ACCUSATIONS = 0
TARGET_INCONCLUSIVE = 0.40
TARGET_F1 = 0.75
LABELS = {
    VerdictKind.REPRODUCED: "Reproduced",
    VerdictKind.REPRODUCED_AT_REDUCED_SCALE: "Reproduced at reduced scale",
    VerdictKind.NOT_REPRODUCED: "Not reproduced under our conditions",
    VerdictKind.INCONCLUSIVE: "Inconclusive",
}
MODALITY_LABELS = {"text": "text", "table": "tables", "figure": "figures"}


def load_results(root: Path = RESULTS_DIR) -> EvalResults:
    found: dict[str, object] = {}
    for key, name in RESULT_FILES.items():
        path = root / name
        if path.is_file():
            found[key] = json.loads(path.read_text(encoding="utf-8"))
    return EvalResults.model_validate(found)


# --- formatting ------------------------------------------------------------------------------


def _usd(value: float | None, digits: int = 2) -> str:
    return "n/a" if value is None else f"${value:.{digits}f}"


def _minutes(seconds: float | None) -> str:
    return "n/a" if seconds is None else f"{seconds / 60:.1f} min"


def _ratio(value: float | None, num: int, den: int) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}% ({num}/{den})"


def _met(ok: bool, target: str) -> str:
    return f"{target}: {'met' if ok else 'not met'}"


def _row(cells: list[str]) -> str:
    return "|" + "|".join(f" {c} " if c else " " for c in cells) + "|"


def _table(header: list[str], rows: list[list[str]]) -> str:
    lines = [_row(header), "|" + "---|" * len(header), *(_row(r) for r in rows)]
    return "\n".join(lines) + "\n"


def _total(cost: CostSummary) -> str:
    text = _usd(cost.total_usd)
    if cost.runs_without_cost:
        plural = "s" if cost.runs_without_cost > 1 else ""
        text += f" ({cost.runs_without_cost} run{plural} without a cost)"
    return text


def _models(models: dict[str, str]) -> str:
    return ", ".join(f"{role} {model}" for role, model in models.items())


def _section(title: str, body: str | None) -> str:
    return f"### {title}\n\n" + (body if body is not None else NOT_RUN)


# --- sections --------------------------------------------------------------------------------


def _verdict_cell(case: CaseA, outcome: ClaimOutcome) -> str:
    if outcome.predicted is None:
        why = case.run.error or (case.run.error_code.value if case.run.error_code else None)
        if why is None and outcome.predicted_id is None:
            why = "not extracted"
        return f"no verdict ({why})" if why else "no verdict"
    label = LABELS[outcome.predicted]
    return f"{label} ({outcome.reason.value})" if outcome.reason else label


def set_a_section(result: SetAResult) -> str:
    v, e, c = result.verdicts, result.extraction, result.cost
    intro = (
        f"{len(result.cases)} papers, {v.claims} claims; "
        "the expected verdicts are for full scale over each canary's seeds.\n\n"
    )
    accuracy_ok = v.accuracy is not None and v.accuracy >= TARGET_ACCURACY
    summary = _table(
        ["Metric", "Result", "Target"],
        [
            [
                "Verdict accuracy",
                _ratio(v.accuracy, v.correct, v.claims),
                _met(accuracy_ok, f">= {TARGET_ACCURACY:.0%}"),
            ],
            [
                "Coverage (claims with a decisive verdict)",
                _ratio(v.coverage, v.judged, v.claims),
                "",
            ],
            [
                "Accuracy on the judged claims",
                _ratio(v.judged_accuracy, v.judged_correct, v.judged),
                "",
            ],
            [
                "False accusations",
                str(v.false_accusations),
                _met(v.false_accusations <= TARGET_FALSE_ACCUSATIONS, "0"),
            ],
            ["Claims without a verdict", str(v.missing), ""],
            ["Extraction F1", f"{e.f1:.2f}", ""],
            ["Median cost per claim", _usd(c.median_usd_per_claim), ""],
            ["Median run time", _minutes(c.median_duration_s), ""],
            ["Total cost", _total(c), ""],
        ],
    )
    claims = _table(
        ["Canary", "Claim", "Planted", "Expected", "Verdict"],
        [
            [
                case.name,
                o.truth_id,
                "yes" if o.planted else "no",
                LABELS[o.expected],
                _verdict_cell(case, o),
            ]
            for case in result.cases
            for o in case.claims
        ],
    )
    return intro + summary + "\n" + claims


def set_b_section(result: SetBResult) -> str:
    verdicts = sum(result.by_kind.values())
    inconclusive = result.by_kind.get(VerdictKind.INCONCLUSIVE, 0)
    rate = result.inconclusive_rate
    c = result.cost
    intro = f"{len(result.cases)} papers, {verdicts} verdicts.\n\n"
    summary = _table(
        ["Metric", "Result", "Target"],
        [
            [
                "Inconclusive rate",
                _ratio(rate, inconclusive, verdicts),
                _met(
                    rate is not None and rate <= TARGET_INCONCLUSIVE,
                    f"<= {TARGET_INCONCLUSIVE:.0%}",
                ),
            ],
            ["Failed runs", str(result.failed_runs), ""],
            ["Median cost per claim", _usd(c.median_usd_per_claim), ""],
            ["Median run time", _minutes(c.median_duration_s), ""],
            ["Total cost", _total(c), ""],
        ],
    )
    kinds = _table(
        ["Verdict", "Claims"],
        [[LABELS[k], str(result.by_kind[k])] for k in VerdictKind if k in result.by_kind],
    )
    text = intro + summary + "\n" + kinds + "\n" + SET_B_CONDITIONS
    if result.by_reason:
        reasons = _table(
            ["Inconclusive reason", "Claims"],
            [[r.value, str(n)] for r, n in result.by_reason.items()],
        )
        text += "\n" + reasons
    return text


def _scores(e: ExtractionScore) -> list[str]:
    return [f"{e.precision:.2f}", f"{e.recall:.2f}", f"{e.f1:.2f}"]


def set_c_section(result: SetCResult) -> str:
    e = result.extraction
    intro = (
        f"{len(result.cases)} papers, {e.truth} annotated claims, one Batch API job "
        f"({result.arm} models: {_models(result.models)}).\n\n"
    )
    rows = [
        ["Precision", f"{e.precision:.2f} ({e.matched}/{e.predicted})", ""],
        ["Recall", f"{e.recall:.2f} ({e.matched}/{e.truth})", ""],
        ["F1", f"{e.f1:.2f}", _met(e.f1 >= TARGET_F1, f">= {TARGET_F1}")],
    ]
    rows += [
        [
            f"Recall, claims stated in {MODALITY_LABELS[m]}",
            f"{result.recall_by_modality[m]:.2f}",
            "",
        ]
        for m in MODALITY_LABELS
        if m in result.recall_by_modality
    ]
    rows.append(["Batch cost", _usd(result.cost_usd, 4), ""])
    return intro + _table(["Metric", "Result", "Target"], rows)


def ablation_section(result: AblationResult) -> str:
    intro = (
        f"Set C ({result.papers} papers) with the routed models against every role on the "
        "Ultra model.\n\n"
    )
    rows = [
        [a.arm, _models(a.models), *_scores(a.extraction), _usd(a.cost_usd, 4)] for a in result.arms
    ]
    return intro + _table(["Configuration", "Models", "Precision", "Recall", "F1", "Cost"], rows)


def render_markdown(results: EvalResults) -> str:
    a, b, c, ab = results.set_a, results.set_b, results.set_c, results.ablation
    sections = [
        _section("Set A: canary papers", set_a_section(a) if a else None),
        _section("Set B: real papers", set_b_section(b) if b else None),
        _section("Set C: claim extraction", set_c_section(c) if c else None),
        _section("Routing ablation", ablation_section(ab) if ab else None),
    ]
    return "## ReproBench-mini\n\n" + "\n".join(sections)


# --- /api/stats ------------------------------------------------------------------------------


class _Stats(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SetAStats(_Stats):
    papers: int
    claims: int
    verdict_accuracy: float | None
    coverage: float | None
    judged_accuracy: float | None
    false_accusations: int
    extraction_f1: float
    median_usd_per_claim: float | None
    median_duration_s: float | None


class SetBStats(_Stats):
    papers: int
    verdicts: int
    inconclusive_rate: float | None
    failed_runs: int
    median_usd_per_claim: float | None
    median_duration_s: float | None


class SetCStats(_Stats):
    papers: int
    claims: int
    precision: float
    recall: float
    f1: float
    cost_usd: float | None


class AblationStats(_Stats):
    arm: str
    f1: float
    cost_usd: float | None


class EvalStats(_Stats):
    """The eval numbers for /api/stats ("eval numbers" in docs/specs/api-and-events.md)."""

    generated_at: datetime | None
    set_a: SetAStats | None
    set_b: SetBStats | None
    set_c: SetCStats | None
    ablation: list[AblationStats] | None


def _r(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def _rf(value: float) -> float:
    return round(value, 4)


def stats_json(results: EvalResults) -> EvalStats:
    a, b, c, ab = results.set_a, results.set_b, results.set_c, results.ablation
    stamps = [r.generated_at for r in (a, b, c, ab) if r is not None]
    return EvalStats(
        generated_at=max(stamps) if stamps else None,
        set_a=SetAStats(
            papers=len(a.cases),
            claims=a.verdicts.claims,
            verdict_accuracy=_r(a.verdicts.accuracy),
            coverage=_r(a.verdicts.coverage),
            judged_accuracy=_r(a.verdicts.judged_accuracy),
            false_accusations=a.verdicts.false_accusations,
            extraction_f1=_rf(a.extraction.f1),
            median_usd_per_claim=_r(a.cost.median_usd_per_claim),
            median_duration_s=_r(a.cost.median_duration_s),
        )
        if a
        else None,
        set_b=SetBStats(
            papers=len(b.cases),
            verdicts=sum(b.by_kind.values()),
            inconclusive_rate=_r(b.inconclusive_rate),
            failed_runs=b.failed_runs,
            median_usd_per_claim=_r(b.cost.median_usd_per_claim),
            median_duration_s=_r(b.cost.median_duration_s),
        )
        if b
        else None,
        set_c=SetCStats(
            papers=len(c.cases),
            claims=c.extraction.truth,
            precision=_rf(c.extraction.precision),
            recall=_rf(c.extraction.recall),
            f1=_rf(c.extraction.f1),
            cost_usd=_r(c.cost_usd),
        )
        if c
        else None,
        ablation=[
            AblationStats(arm=arm.arm, f1=_rf(arm.extraction.f1), cost_usd=_r(arm.cost_usd))
            for arm in ab.arms
        ]
        if ab
        else None,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--results", type=Path, default=RESULTS_DIR)
    parser.add_argument("--markdown", type=Path, default=RESULTS_DIR / "report.md")
    parser.add_argument("--stats", type=Path, default=RESULTS_DIR / "stats.json")
    args = parser.parse_args(argv)
    results = load_results(args.results)
    args.markdown.parent.mkdir(parents=True, exist_ok=True)
    args.markdown.write_text(render_markdown(results), encoding="utf-8", newline="\n")
    write_result(args.stats, stats_json(results))
    print(f"wrote {args.markdown} and {args.stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

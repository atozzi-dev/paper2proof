"""eval/report.py on fixture results: the README tables and the /api/stats JSON."""

import json
from pathlib import Path

from eval import report

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "eval" / "results"

NR = "Not reproduced under our conditions"  # the copy rule (invariant 10)
SET_A = f"""\
### Set A: canary papers

3 papers, 5 claims; the expected verdicts are for full scale over each canary's seeds.

| Metric | Result | Target |
|---|---|---|
| Verdict accuracy | 60.0% (3/5) | >= 80%: not met |
| Coverage (claims with a decisive verdict) | 60.0% (3/5) | |
| Accuracy on the judged claims | 100.0% (3/3) | |
| False accusations | 0 | 0: met |
| Claims without a verdict | 1 | |
| Extraction F1 | 0.80 | |
| Median cost per claim | $0.18 | |
| Median run time | 10.0 min | |
| Total cost | $0.72 (1 run without a cost) | |

| Canary | Claim | Planted | Expected | Verdict |
|---|---|---|---|---|
| digits-mlp | c1 | no | Reproduced | Reproduced |
| digits-mlp | c2 | no | Reproduced | Reproduced |
| friedman-boosting | c1 | yes | {NR} | {NR} |
| friedman-boosting | c2 | no | Reproduced | Inconclusive (HIGH_VARIANCE) |
| wine-boosting | c1 | yes | {NR} | no verdict (ConnectError) |
"""

SET_B = """\
### Set B: real papers

2 papers, 2 verdicts.

| Metric | Result | Target |
|---|---|---|
| Inconclusive rate | 50.0% (1/2) | <= 40%: not met |
| Failed runs | 1 | |
| Median cost per claim | $0.40 | |
| Median run time | 12.5 min | |
| Total cost | $1.00 | |

| Verdict | Claims |
|---|---|
| Reproduced | 1 |
| Inconclusive | 1 |

Conditions: the scale, seeds and hardware of each run's plan, which can differ from the \
paper's; each run's Proof Bundle lists them.

| Inconclusive reason | Claims |
|---|---|
| BUDGET | 1 |
"""

SET_C = """\
### Set C: claim extraction

2 papers, 10 annotated claims, one Batch API job (routed models: reader vendor/reader-model).

| Metric | Result | Target |
|---|---|---|
| Precision | 0.78 (7/9) | |
| Recall | 0.70 (7/10) | |
| F1 | 0.74 | >= 0.75: not met |
| Recall, claims stated in text | 1.00 | |
| Recall, claims stated in tables | 0.50 | |
| Recall, claims stated in figures | 0.00 | |
| Batch cost | $0.0225 | |
"""

ABLATION = """\
### Routing ablation

Set C (2 papers) with the routed models against every role on the Ultra model.

| Configuration | Models | Precision | Recall | F1 | Cost |
|---|---|---|---|---|---|
| routed | reader vendor/reader-model | 0.78 | 0.70 | 0.74 | $0.0225 |
| all-ultra | reader vendor/ultra-model | 0.89 | 0.80 | 0.84 | $0.0750 |
"""


def test_markdown_tables_for_every_set() -> None:
    markdown = report.render_markdown(report.load_results(FIXTURES))
    assert markdown.startswith("## ReproBench-mini\n\n")
    for section in (SET_A, SET_B, SET_C, ABLATION):
        assert section in markdown
    # the order of the sections
    positions = [markdown.index(s.splitlines()[0]) for s in (SET_A, SET_B, SET_C, ABLATION)]
    assert positions == sorted(positions)


def test_a_set_not_run_yet_says_so(tmp_path: Path) -> None:
    markdown = report.render_markdown(report.load_results(tmp_path))
    assert markdown.count("_Not run yet._") == 4


def test_stats_json_for_api_stats() -> None:
    stats = report.stats_json(report.load_results(FIXTURES))
    assert stats.model_dump(mode="json") == {
        "generated_at": "2026-10-26T09:30:00Z",
        "set_a": {
            "papers": 3,
            "claims": 5,
            "verdict_accuracy": 0.6,
            "coverage": 0.6,
            "judged_accuracy": 1.0,
            "false_accusations": 0,
            "extraction_f1": 0.8,
            "median_usd_per_claim": 0.18,
            "median_duration_s": 600.0,
        },
        "set_b": {
            "papers": 2,
            "verdicts": 2,
            "inconclusive_rate": 0.5,
            "failed_runs": 1,
            "median_usd_per_claim": 0.4,
            "median_duration_s": 750.0,
        },
        "set_c": {
            "papers": 2,
            "claims": 10,
            "precision": 0.7778,
            "recall": 0.7,
            "f1": 0.7368,
            "cost_usd": 0.0225,
        },
        "ablation": [
            {"arm": "routed", "f1": 0.7368, "cost_usd": 0.0225},
            {"arm": "all-ultra", "f1": 0.8421, "cost_usd": 0.075},
        ],
    }


def test_cli_writes_both_files(tmp_path: Path) -> None:
    md, js = tmp_path / "report.md", tmp_path / "stats.json"
    assert report.main(["--results", str(FIXTURES), "--markdown", str(md), "--stats", str(js)]) == 0
    assert SET_A in md.read_text(encoding="utf-8")
    assert json.loads(js.read_text(encoding="utf-8"))["set_a"]["false_accusations"] == 0

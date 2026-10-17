"""Eval set B: real arXiv papers (eval/set_b.yaml) through the full pipeline.

No ground truth is assumed: the set measures how the pipeline behaves on real material.
Reported: verdicts by kind, INCONCLUSIVE reasons and rate (target <= 40%, architecture §18.2),
failed runs, median cost per claim and median run time.

    P2P_EVAL_INVITE=... uv run python -m eval.run_set_b --api-url URL --confirm-spend

Costs money: every paper is a real run on the deployment.
"""

import argparse
import asyncio
import sys
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field

from app.domain.models import ReasonCode, VerdictKind
from eval.pipeline import (
    ARXIV_ID,
    HttpPipeline,
    Pipeline,
    add_pipeline_args,
    attempt,
    check_api_url,
    invite_from_env,
    write_result,
)
from eval.results import RESULT_FILES, CaseB, RunInfo, SetBResult, VerdictOutcome
from eval.scoring import cost_summary

EVAL_DIR = Path(__file__).resolve().parent
SET_B = EVAL_DIR / "set_b.yaml"
RESULTS_DIR = EVAL_DIR / "results"


class SetBPaper(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    arxiv_id: str = Field(pattern=ARXIV_ID.pattern)  # invariant 11: arXiv IDs only
    claim_hint: str | None = Field(default=None, max_length=500)
    notes: str | None = None  # why it is in the set, known reproducibility reports


class SetB(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    papers: list[SetBPaper] = Field(max_length=20)


def load_set_b(path: Path = SET_B) -> SetB:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return SetB.model_validate({"papers": raw.get("papers") or []})


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def run_set_b(
    pipeline: Pipeline, config: SetB, *, now: Callable[[], datetime] = _utcnow
) -> SetBResult:
    cases: list[CaseB] = []
    runs: list[tuple[RunInfo, int]] = []
    for paper in config.papers:
        info, run = await attempt(pipeline, paper.arxiv_id, claim_hint=paper.claim_hint)
        verdicts = [
            VerdictOutcome(claim_id=cid, kind=v.kind, reason=v.reason)
            for cid, v in sorted((run.verdicts if run else {}).items())
        ]
        cases.append(CaseB(arxiv_id=paper.arxiv_id, run=info, verdicts=verdicts))
        runs.append((info, len(verdicts)))
    every = [v for c in cases for v in c.verdicts]
    by_kind = Counter(v.kind for v in every)
    by_reason = Counter(v.reason for v in every if v.reason is not None)
    return SetBResult(
        generated_at=now(),
        cases=cases,
        by_kind={k: by_kind[k] for k in VerdictKind if by_kind[k]},
        by_reason={r: by_reason[r] for r in ReasonCode if by_reason[r]},
        inconclusive_rate=by_kind[VerdictKind.INCONCLUSIVE] / len(every) if every else None,
        failed_runs=sum(c.run.status is None or c.run.error_code is not None for c in cases),
        cost=cost_summary(runs),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    add_pipeline_args(parser)
    parser.add_argument("--set", type=Path, default=SET_B, help="the set B file")
    parser.add_argument("--out", type=Path, default=RESULTS_DIR / RESULT_FILES["set_b"])
    args = parser.parse_args(argv)
    if not args.confirm_spend:
        print("set B spends real money: pass --confirm-spend", file=sys.stderr)
        return 2
    config = load_set_b(args.set)
    if not config.papers:
        print(f"{args.set} lists no papers yet: pick them first", file=sys.stderr)
        return 2
    base_url = check_api_url(args.api_url)
    invite = invite_from_env()

    async def go() -> SetBResult:
        async with httpx.AsyncClient(base_url=base_url, timeout=30) as http:
            return await run_set_b(HttpPipeline(http, invite, approve=args.approve), config)

    result = asyncio.run(go())
    write_result(args.out, result)
    print(f"wrote {args.out}: inconclusive rate {result.inconclusive_rate}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

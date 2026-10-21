"""Eval set A: the canary papers (eval/canaries, A-29) through the full pipeline.

Each canary's claims are matched to the pipeline's claims (eval/scoring.py) and the pipeline's
verdict for the matched claim is compared with truth.yaml. Targets (architecture §18.2):
verdict accuracy >= 80% and 0 false accusations (a true claim reported NOT_REPRODUCED).

    P2P_EVAL_INVITE=... uv run python -m eval.run_set_a --api-url URL --confirm-spend

Costs money: every canary is a real run (Token Factory, jobs). Every deployment serves the
canary e-prints under their 0000.000NN IDs (app/ingestion/canaries.py, allowlisted bytes only).
"""

import argparse
import asyncio
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

import httpx

from app.domain.models import Claim
from eval.canaries import build
from eval.pipeline import (
    HttpPipeline,
    Pipeline,
    PipelineRun,
    add_pipeline_args,
    attempt,
    check_api_url,
    invite_from_env,
    write_result,
)
from eval.results import RESULT_FILES, CaseA, ClaimOutcome, RunInfo, SetAResult
from eval.scoring import (
    cost_summary,
    extraction_score,
    match_claims,
    total_extraction,
    verdict_score,
)

RESULTS_DIR = Path(__file__).resolve().parent / "results"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _plain(claim: build.TruthClaim) -> Claim:
    return Claim.model_validate(claim.model_dump(exclude={"planted"}))


def score_case(canary: build.Canary, run: PipelineRun | None) -> tuple[list[ClaimOutcome], int]:
    """Outcomes per truth claim, and the number of claims the pipeline extracted."""
    truth = canary.truth
    predicted = [c.claim for c in run.claims] if run else []
    matches = match_claims(predicted, [_plain(c) for c in truth.claims])
    matched = {m.truth_id: m.predicted_id for m in matches}
    outcomes = []
    for claim in truth.claims:
        pid = matched.get(claim.id)
        verdict = run.verdicts.get(pid) if run and pid else None
        outcomes.append(
            ClaimOutcome(
                truth_id=claim.id,
                planted=claim.planted,
                expected=truth.verdicts[claim.id],
                predicted_id=pid,
                predicted=verdict.kind if verdict else None,
                reason=verdict.reason if verdict else None,
            )
        )
    return outcomes, len(predicted)


async def run_set_a(
    pipeline: Pipeline,
    canaries: Sequence[build.Canary],
    *,
    now: Callable[[], datetime] = _utcnow,
) -> SetAResult:
    cases: list[CaseA] = []
    runs: list[tuple[RunInfo, int]] = []
    for canary in canaries:  # one at a time: the deployment allows two concurrent runs
        info, run = await attempt(pipeline, canary.truth.arxiv_id)
        outcomes, n_predicted = score_case(canary, run)
        matched = sum(o.predicted_id is not None for o in outcomes)
        cases.append(
            CaseA(
                name=canary.name,
                arxiv_id=canary.truth.arxiv_id,
                run=info,
                extraction=extraction_score(
                    matched=matched, predicted=n_predicted, truth=len(outcomes)
                ),
                claims=outcomes,
            )
        )
        runs.append((info, len(run.verdicts) if run else 0))
    return SetAResult(
        generated_at=now(),
        cases=cases,
        verdicts=verdict_score([o for c in cases for o in c.claims]),
        extraction=total_extraction([c.extraction for c in cases]),
        cost=cost_summary(runs),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    add_pipeline_args(parser)
    parser.add_argument("--only", nargs="*", default=None, help="canary names to run")
    parser.add_argument("--out", type=Path, default=RESULTS_DIR / RESULT_FILES["set_a"])
    args = parser.parse_args(argv)
    if not args.confirm_spend:
        print("set A spends real money: pass --confirm-spend", file=sys.stderr)
        return 2
    canaries = [c for c in build.discover() if args.only is None or c.name in args.only]
    base_url = check_api_url(args.api_url)
    invite = invite_from_env()

    async def go() -> SetAResult:
        async with httpx.AsyncClient(base_url=base_url, timeout=30) as http:
            return await run_set_a(HttpPipeline(http, invite, approve=args.approve), canaries)

    result = asyncio.run(go())
    write_result(args.out, result)
    score = result.verdicts
    print(
        f"wrote {args.out}: accuracy {score.accuracy}, false accusations {score.false_accusations}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

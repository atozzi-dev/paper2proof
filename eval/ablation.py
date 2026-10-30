"""Routing ablation (architecture §18.3): set C with the routed config against the same set
with every role on the Ultra model, reporting extraction F1 and cost for each arm.

"Ultra" is whatever model the routed config gives the Ultra role (--like, default planner):
model IDs come from config/models.yaml resolved against GET /v1/models, never from here.

    uv run python -m eval.ablation --confirm-spend

Costs money: two batches over set C (the all-Ultra arm costs several times the routed one).
"""

import argparse
import asyncio
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import httpx

from app.domain.paper import IngestedPaper
from app.ingestion.arxiv import ArxivClient
from app.models.client import ResolvedModel, Role
from app.settings import Settings
from eval.pipeline import write_result
from eval.results import RESULT_FILES, AblationArm, AblationResult, SetCResult
from eval.run_set_c import (
    Annotation,
    ArxivPaperLoader,
    Extractor,
    Live,
    add_set_c_args,
    load_annotations,
    load_papers,
    run_set_c,
)

RESULTS_DIR = Path(__file__).resolve().parent / "results"


def uniform(resolved: Mapping[Role, ResolvedModel], *, like: Role) -> dict[Role, ResolvedModel]:
    """Every role routed to the model of `like` (the all-Ultra arm)."""
    return {role: resolved[like] for role in resolved}


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def run_ablation(
    papers: Mapping[str, IngestedPaper],
    annotations: Sequence[Annotation],
    arms: Sequence[tuple[str, Extractor]],
    *,
    now: Callable[[], datetime] = _utcnow,
) -> tuple[AblationResult, list[SetCResult]]:
    """The same papers and requests on every arm, one batch per arm, one after the other."""
    results = [
        await run_set_c(papers, annotations, extractor, arm=name, now=now)
        for name, extractor in arms
    ]
    summary = AblationResult(
        generated_at=now(),
        papers=len(annotations),
        arms=[
            AblationArm(arm=r.arm, models=r.models, extraction=r.extraction, cost_usd=r.cost_usd)
            for r in results
        ],
    )
    return summary, results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    add_set_c_args(parser)
    parser.add_argument(
        "--like", type=Role, default=Role.PLANNER, help="the role whose model is 'Ultra'"
    )
    parser.add_argument("--out", type=Path, default=RESULTS_DIR / RESULT_FILES["ablation"])
    args = parser.parse_args(argv)
    if not args.confirm_spend:
        print("the ablation spends real money: pass --confirm-spend", file=sys.stderr)
        return 2
    annotations = load_annotations(args.annotations, include_unreviewed=args.include_unreviewed)
    if not annotations:
        print("no reviewed annotations yet (task H-07)", file=sys.stderr)
        return 2
    live = Live(Settings())

    async def go() -> AblationResult:
        async with httpx.AsyncClient(timeout=60) as http:
            with ProcessPoolExecutor(max_workers=min(4, os.cpu_count() or 1)) as pool:
                papers = await load_papers(ArxivPaperLoader(ArxivClient(http), pool), annotations)
            resolved = await live.resolve(http)
            arms: list[tuple[str, Extractor]] = [
                ("routed", live.runner(resolved, step="EVAL_ABLATION")),
                ("all-ultra", live.runner(uniform(resolved, like=args.like), step="EVAL_ABLATION")),
            ]
            summary, _ = await run_ablation(papers, annotations, arms)
            return summary

    result = asyncio.run(go())
    write_result(args.out, result)
    for arm in result.arms:
        print(f"{arm.arm}: F1 {arm.extraction.f1:.3f}, cost {arm.cost_usd}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

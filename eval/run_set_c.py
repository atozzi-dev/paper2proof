"""Eval set C: claim extraction only, through the Token Factory Batch API (architecture §18.3).

Each annotated paper (eval/set_c/annotations/<arxiv_id>.yaml, task H-07) is ingested, sent to
the Reader with the production prompt and request (reader_messages, the routed model, the
structured-output tool), and the extracted claims are matched to the annotation
(eval/scoring.py). Reported: precision, recall and F1 (target F1 >= 0.75, §18.2) and recall per
modality (text, table, figure).

Differences from the live step, by design of a single batch: no Utility repair of a broken
Reader output (it counts as zero claims, SCHEMA_ERROR), metric names are normalized with the
deterministic aliases only (no Utility call), and no Vision pass over PDF pages.

    uv run python -m eval.run_set_c --confirm-spend

Costs money: one Reader call per paper at the batch price. Needs NEBIUS_API_KEY.
"""

import argparse
import asyncio
import os
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Executor, ProcessPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, Self

import httpx
import openai
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.errors import P2PError
from app.domain.models import Claim
from app.domain.paper import IngestedPaper
from app.ingestion.arxiv import ArxivClient
from app.models.batch import BatchOutcome, BatchRequest, BatchResult, BatchRunner
from app.models.client import (
    ModelsConfig,
    ResolvedModel,
    Role,
    StoreCallRecorder,
    load_models_config,
)
from app.models.pricing import Pricing
from app.models.router import fetch_models, resolve_roles
from app.models.structured import parse_structured, request_args
from app.orchestrator.budget import Budget, load_budget_limits
from app.orchestrator.steps.extracting_claims import (
    READER_PROMPT,
    ReaderOutput,
    fill_from_tables,
    latex_table_rows,
    normalize_metric,
    reader_messages,
)
from app.orchestrator.steps.ingesting import PaperSource, _parse
from app.settings import Settings
from app.storage.db import make_engine
from app.storage.store import Store
from eval.pipeline import ARXIV_ID, write_result
from eval.results import RESULT_FILES, CaseC, Modality, SetCResult
from eval.scoring import extraction_score, match_claims, metric_key, total_extraction

EVAL_DIR = Path(__file__).resolve().parent
ANNOTATIONS = EVAL_DIR / "set_c" / "annotations"
RESULTS_DIR = EVAL_DIR / "results"
MODALITIES: tuple[Modality, ...] = ("text", "table", "figure")


class AnnotatedClaim(Claim):
    modality: Modality = "text"  # where the number is stated: text, a table, or a figure


class Annotation(BaseModel):
    """eval/set_c/annotations/<arxiv_id>.yaml: the claims a careful reader extracts (H-07)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    arxiv_id: str = Field(pattern=ARXIV_ID.pattern)
    reviewed: bool  # false: a draft (pre-filled or example), not scored by default
    annotator: str | None = None
    notes: str | None = None
    claims: list[AnnotatedClaim]

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        ids = [c.id for c in self.claims]
        if len(set(ids)) != len(ids):
            raise ValueError("claim ids must be unique within a paper")
        return self

    def truth(self) -> list[Claim]:
        return [Claim.model_validate(c.model_dump(exclude={"modality"})) for c in self.claims]


def load_annotations(
    root: Path = ANNOTATIONS, *, include_unreviewed: bool = False
) -> list[Annotation]:
    annotations = []
    for path in sorted(root.glob("*.yaml")):
        annotation = Annotation.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        if path.stem != annotation.arxiv_id:
            raise ValueError(
                f"{path.name} must be named after its arxiv_id ({annotation.arxiv_id})"
            )
        if annotation.reviewed or include_unreviewed:
            annotations.append(annotation)
    return annotations


class PaperLoader(Protocol):
    async def load(self, arxiv_id: str) -> IngestedPaper: ...


class Extractor(Protocol):
    """What set C needs from app.models.batch.BatchRunner (and tests fake)."""

    config: ModelsConfig
    resolved: dict[Role, ResolvedModel]

    async def run(self, requests: Sequence[BatchRequest]) -> BatchOutcome: ...


class ArxivPaperLoader:
    """Downloads the e-print and parses it in the process pool (invariant 12), like INGESTING."""

    def __init__(self, source: PaperSource, pool: Executor) -> None:
        self._source = source
        self._pool = pool

    async def load(self, arxiv_id: str) -> IngestedPaper:
        data = await self._source.source(arxiv_id)
        loop = asyncio.get_running_loop()
        paper, _pdf = await loop.run_in_executor(self._pool, _parse, data, arxiv_id, None)
        return paper


async def load_papers(
    loader: PaperLoader, annotations: Sequence[Annotation]
) -> dict[str, IngestedPaper]:
    return {a.arxiv_id: await loader.load(a.arxiv_id) for a in annotations}


def reader_request(paper: IngestedPaper, config: ModelsConfig) -> BatchRequest:
    mode = config.structured_output.get(Role.READER, "tool")
    return BatchRequest(
        case_id=paper.arxiv_id,
        role=Role.READER,
        messages=reader_messages(paper),
        prompt=READER_PROMPT,
        **request_args(ReaderOutput, mode),
    )


def extracted_claims(
    result: BatchResult | None, paper: IngestedPaper, config: ModelsConfig
) -> tuple[list[Claim], str | None]:
    """The Reader's claims as the live step builds them, or none and why."""
    if result is None:
        return [], "missing from the batch output"
    if result.completion is None:
        return [], result.error or "batch error"
    mode = config.structured_output.get(Role.READER, "tool")
    try:
        output = parse_structured(result.completion, ReaderOutput, mode)
    except P2PError as err:
        return [], err.code.value
    rows = latex_table_rows(paper)
    claims = []
    for index, draft in enumerate(output.claims, start=1):
        known = normalize_metric(draft.metric)
        metric, higher = (
            known if known else (metric_key(draft.metric) or "metric", draft.higher_is_better)
        )
        claim = Claim(
            id=f"c{index}",
            **draft.model_dump(exclude={"metric", "higher_is_better"}),
            metric=metric,
            higher_is_better=higher,
        )
        claims.append(fill_from_tables(claim, rows))
    return claims, None


def _recall(matched: Counter[Modality], total: Counter[Modality]) -> dict[Modality, float]:
    return {m: matched[m] / total[m] for m in MODALITIES if total[m]}


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def run_set_c(
    papers: Mapping[str, IngestedPaper],
    annotations: Sequence[Annotation],
    extractor: Extractor,
    *,
    arm: str,
    now: Callable[[], datetime] = _utcnow,
) -> SetCResult:
    requests = [reader_request(papers[a.arxiv_id], extractor.config) for a in annotations]
    outcome = await extractor.run(requests)
    cases: list[CaseC] = []
    matched_all: Counter[Modality] = Counter()
    total_all: Counter[Modality] = Counter()
    for annotation, request in zip(annotations, requests, strict=True):
        paper = papers[annotation.arxiv_id]
        predicted, error = extracted_claims(
            outcome.results.get(request.custom_id), paper, extractor.config
        )
        hits = {m.truth_id for m in match_claims(predicted, annotation.truth())}
        matched = Counter(c.modality for c in annotation.claims if c.id in hits)
        total = Counter(c.modality for c in annotation.claims)
        matched_all.update(matched)
        total_all.update(total)
        cases.append(
            CaseC(
                arxiv_id=annotation.arxiv_id,
                extraction=extraction_score(
                    matched=len(hits), predicted=len(predicted), truth=len(annotation.claims)
                ),
                recall_by_modality=_recall(matched, total),
                error=error,
            )
        )
    roles = sorted({r.role for r in requests}, key=list(Role).index)
    return SetCResult(
        generated_at=now(),
        arm=arm,
        models={r.value: extractor.resolved[r].model_id for r in roles},
        batch_id=outcome.batch_id,
        batch_status=outcome.status,
        cases=cases,
        extraction=total_extraction([c.extraction for c in cases]),
        recall_by_modality=_recall(matched_all, total_all),
        tokens_in=outcome.tokens_in,
        tokens_out=outcome.tokens_out,
        cost_usd=outcome.cost_usd,
        charged_usd=outcome.charged_usd,
    )


# --- CLI (paid runs only) ---------------------------------------------------------------------


class Live:
    """Everything a paid batch needs, from the same settings and config files as the app."""

    def __init__(self, settings: Settings) -> None:
        if settings.nebius_api_key is None:
            raise ValueError("NEBIUS_API_KEY is not set")
        self._key = settings.nebius_api_key
        self.config = load_models_config(settings.config_dir / "models.yaml")
        self.pricing = Pricing.load(settings.config_dir / "pricing.yaml")
        self.store = Store(make_engine(settings.db_url))
        # The eval keeps its own ledger in this DB (by default the local dev DB), separate from
        # the deployment's: env="dev" so the lower dev_inference_usd_per_day cap applies, which
        # leaves the deployment's daily headroom on the shared Token Factory account.
        self.budget = Budget(
            self.store, load_budget_limits(settings.config_dir / "budgets.yaml"), env="dev"
        )
        # No SDK retries: creating a file or a batch is not idempotent, and a retried create
        # could start a second batch outside the ledger.
        self.openai = openai.AsyncOpenAI(
            api_key=self._key.get_secret_value(),
            base_url=self.config.provider.base_url,
            max_retries=0,
        )

    async def resolve(self, http: httpx.AsyncClient) -> dict[Role, ResolvedModel]:
        listed = await fetch_models(http, self.config.provider.base_url, self._key)
        return resolve_roles(listed, self.config)

    def runner(self, resolved: Mapping[Role, ResolvedModel], *, step: str) -> BatchRunner:
        return BatchRunner(
            self.openai,
            self.config,
            resolved,
            self.pricing,
            self.budget,
            StoreCallRecorder(self.store),
            step=step,
        )


def add_set_c_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--annotations", type=Path, default=ANNOTATIONS)
    parser.add_argument(
        "--include-unreviewed", action="store_true", help="also score drafts (reviewed: false)"
    )
    parser.add_argument(
        "--confirm-spend", action="store_true", help="required: the batch costs money"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    add_set_c_args(parser)
    parser.add_argument("--out", type=Path, default=RESULTS_DIR / RESULT_FILES["set_c"])
    args = parser.parse_args(argv)
    if not args.confirm_spend:
        print("set C spends real money: pass --confirm-spend", file=sys.stderr)
        return 2
    annotations = load_annotations(args.annotations, include_unreviewed=args.include_unreviewed)
    if not annotations:
        print("no reviewed annotations yet (task H-07)", file=sys.stderr)
        return 2
    live = Live(Settings())

    async def go() -> SetCResult:
        async with httpx.AsyncClient(timeout=60) as http:
            with ProcessPoolExecutor(max_workers=min(4, os.cpu_count() or 1)) as pool:
                papers = await load_papers(ArxivPaperLoader(ArxivClient(http), pool), annotations)
            resolved = await live.resolve(http)
            runner = live.runner(resolved, step="EVAL_SET_C")
            return await run_set_c(papers, annotations, runner, arm="routed")

    result = asyncio.run(go())
    write_result(args.out, result)
    print(f"wrote {args.out}: F1 {result.extraction.f1:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

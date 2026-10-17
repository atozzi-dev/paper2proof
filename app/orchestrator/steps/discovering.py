"""DISCOVERING: official repository and datasets for the paper (spike S-9 decision).

1. GitHub links written by the authors (arXiv comment/abstract, HF summary): official, no LLM.
2. Otherwise Tavily search (basic, then advanced), extract on the top 3 URLs, Utility ranking.
3. Tavily or Hugging Face problems degrade discovery (warning DISCOVERY_DEGRADED); the run
   never fails here.
4. Canary papers (allowlisted sha256) skip discovery entirely: their titles are public, so a
   repository anyone could publish must never steer the Coder whose code runs locally.
"""

import asyncio
from datetime import timedelta

import httpx

from app.discovery.hf_papers import HfPapers, HfUnavailable
from app.discovery.rank import github_repos, rank_candidates
from app.discovery.tavily import TavilyClient, TavilyResult, TavilyUnavailable
from app.domain.discovery import HF_DATASET_ID, Discovery, RepoRef
from app.domain.events import EventType
from app.domain.models import DatasetReq, StepName
from app.ingestion.arxiv import ArxivClient, ArxivMetadata
from app.models.client import ModelClient
from app.orchestrator.budget import BudgetExceeded
from app.orchestrator.context import RunContext
from app.orchestrator.fsm import StepResult
from app.storage.artifacts import ArtifactStore
from app.storage.store import Store

DEGRADED = "DISCOVERY_DEGRADED"
METADATA_TTL = timedelta(hours=24)


class DiscoveringStep:
    name = StepName.DISCOVERING

    def __init__(
        self,
        *,
        store: Store,
        arxiv: ArxivClient,
        tavily: TavilyClient,
        models: ModelClient,
        artifacts: ArtifactStore,
        http: httpx.AsyncClient | None = None,
        canaries: frozenset[str] = frozenset(),
    ) -> None:
        self._hf = HfPapers(store, http)
        self._canaries = canaries
        self._arxiv = arxiv
        self._tavily = tavily
        self._models = models
        self._artifacts = artifacts
        self._store = store

    async def _metadata(self, arxiv_id: str) -> ArxivMetadata:
        key = f"arxiv-meta-{arxiv_id}"
        hit = await self._store.cache_get("arxiv", key, max_age=METADATA_TTL)
        if hit is not None:
            return ArxivMetadata.model_validate(hit)
        try:
            meta = await self._arxiv.metadata(arxiv_id)
        except Exception:
            return ArxivMetadata(arxiv_id=arxiv_id, title=None)
        await self._store.cache_put("arxiv", key, meta.model_dump())
        return meta

    async def _search(
        self, ctx: RunContext, title: str | None, arxiv_id: str, notes: list[str]
    ) -> RepoRef | None:
        query = f'"{title}" official code github' if title else f"arXiv {arxiv_id} github code"
        budget, run_id, store = ctx.budget, ctx.run_id, self._store
        candidates: dict[str, str] = {}

        def collect(results: list[TavilyResult]) -> None:
            for result in results:
                for url in github_repos(f"{result.url} {result.content}"):
                    candidates.setdefault(url, result.content[:300])

        results = await self._tavily.search(
            query, "basic", budget=budget, run_id=run_id, store=store
        )
        collect(results)
        if not candidates:
            results = await self._tavily.search(
                query, "advanced", budget=budget, run_id=run_id, store=store
            )
            collect(results)
        top = [r.url for r in results[:3]]
        if top:
            extracted = await self._tavily.extract(
                top, "official code repository", budget=budget, run_id=run_id, store=store
            )
            collect(extracted)
        if not candidates:
            notes.append("no repository found by search")
            return None
        url, note = await rank_candidates(
            self._models, candidates, title=title, run_id=ctx.run_id, step=ctx.step.value
        )
        if note:
            notes.append(note)
        return RepoRef(url=url, source="tavily", official=False) if url else None

    async def run(self, ctx: RunContext) -> StepResult:
        run = await ctx.store.get_run(ctx.run_id)
        arxiv_id = run.arxiv_id if run else ""
        notes: list[str] = []
        if run is not None and (run.paper_sha256 or "").lower() in self._canaries:
            note = "canary paper: no repository or dataset discovery (self-contained experiment)"
            return await self._done(ctx, Discovery(repo=None, datasets=[], notes=[note]))
        meta = await self._metadata(arxiv_id)
        hf_summary, hf_datasets = "", []
        try:
            hf_summary = await self._hf.summary(arxiv_id)
            hf_datasets = await self._hf.dataset_ids(arxiv_id)
        except HfUnavailable as err:
            await self._degrade(ctx, notes, f"{err}: no Hugging Face links or datasets")
        datasets = [DatasetReq(kind="hf", id=d) for d in hf_datasets if HF_DATASET_ID.fullmatch(d)]

        official = [
            RepoRef(url=url, source="paper", official=True)
            for url in github_repos(f"{meta.comment or ''} {meta.abstract or ''}")
        ] + [RepoRef(url=url, source="hf", official=True) for url in github_repos(hf_summary)]
        repo = official[0] if official else None

        if repo is None:
            try:
                repo = await self._search(ctx, meta.title, arxiv_id, notes)
            except (TavilyUnavailable, BudgetExceeded) as err:
                reason = str(err) if isinstance(err, TavilyUnavailable) else "Tavily budget reached"
                await self._degrade(ctx, notes, reason)

        return await self._done(ctx, Discovery(repo=repo, datasets=datasets, notes=notes))

    async def _degrade(self, ctx: RunContext, notes: list[str], reason: str) -> None:
        notes.append(f"{DEGRADED}: {reason}")
        await ctx.emit(EventType.WARNING, {"code": DEGRADED, "message": reason})

    async def _done(self, ctx: RunContext, discovery: Discovery) -> StepResult:
        key = f"runs/{ctx.run_id}/discovery.json"
        await asyncio.to_thread(self._artifacts.put_json, key, discovery.model_dump(mode="json"))
        return StepResult(next=StepName.PLANNING, output_ref=key)

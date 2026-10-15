"""INGESTING: download the paper, parse it in the CPU pool, store IngestedPaper as an artifact."""

import asyncio
import hashlib
from typing import Protocol

from app.domain.errors import P2PError
from app.domain.models import ErrorCode, StepName
from app.domain.paper import IngestedPaper, PageImage
from app.ingestion.archive import ArchiveError, extract_source
from app.ingestion.arxiv import ArxivMetadata
from app.ingestion.latex import parse_latex
from app.ingestion.pdf import pages_with_tables_or_figures, parse_pdf
from app.ingestion.raster import RenderedPage, rasterize_pages
from app.orchestrator.context import RunContext
from app.orchestrator.fsm import StepResult
from app.storage.artifacts import ArtifactStore


class PaperSource(Protocol):
    async def metadata(self, arxiv_id: str) -> ArxivMetadata: ...
    async def source(self, arxiv_id: str) -> bytes: ...


def _parse(data: bytes, arxiv_id: str, title: str | None) -> tuple[IngestedPaper, bytes | None]:
    """Module-level so it can run in a ProcessPoolExecutor."""
    source = extract_source(data)
    if source.kind == "pdf" and source.pdf is not None:
        paper = parse_pdf(source.pdf, arxiv_id=arxiv_id, title=title)
        return paper.model_copy(update={"sha256": hashlib.sha256(data).hexdigest()}), source.pdf
    paper = parse_latex(source.files, arxiv_id=arxiv_id, title=title)
    return paper.model_copy(update={"sha256": hashlib.sha256(data).hexdigest()}), None


def _render(pdf: bytes) -> list[RenderedPage]:
    return rasterize_pages(pdf, pages_with_tables_or_figures(pdf))


class IngestingStep:
    name = StepName.INGESTING

    def __init__(
        self, papers: PaperSource, artifacts: ArtifactStore, *, parse_timeout_s: float = 120
    ) -> None:
        self._papers = papers
        self._artifacts = artifacts
        self._parse_timeout = parse_timeout_s

    async def run(self, ctx: RunContext) -> StepResult:
        run = await ctx.store.get_run(ctx.run_id)
        if run is None:
            raise P2PError(ErrorCode.INGEST_FAILED, "run not found")
        try:
            meta = await self._papers.metadata(run.arxiv_id)
        except Exception:  # metadata is optional: the title also comes from the source
            meta = ArxivMetadata(arxiv_id=run.arxiv_id, title=None)
        try:
            data = await self._papers.source(run.arxiv_id)
        except Exception as err:
            raise P2PError(ErrorCode.INGEST_FAILED, "could not download the paper") from err
        try:
            paper, pdf = await ctx.run_cpu(
                _parse, data, run.arxiv_id, meta.title, timeout_s=self._parse_timeout
            )
        except TimeoutError:
            raise P2PError(ErrorCode.INGEST_FAILED, "parsing the paper took too long") from None
        except (ArchiveError, ValueError) as err:
            raise P2PError(ErrorCode.INGEST_FAILED, f"unusable paper source: {err}") from err

        prefix = f"runs/{ctx.run_id}"
        if pdf is not None:
            images = []
            for page in await ctx.run_cpu(_render, pdf, timeout_s=self._parse_timeout):
                key = f"{prefix}/pages/{page.page}.png"
                await asyncio.to_thread(self._artifacts.put_bytes, key, page.png)
                images.append(PageImage(page=page.page, key=key))
            paper = paper.model_copy(update={"page_images": images})
        key = f"{prefix}/ingested.json"
        await asyncio.to_thread(self._artifacts.put_json, key, paper.model_dump(mode="json"))
        await ctx.store.set_paper_sha256(ctx.run_id, paper.sha256)
        return StepResult(next=StepName.EXTRACTING_CLAIMS, output_ref=key)

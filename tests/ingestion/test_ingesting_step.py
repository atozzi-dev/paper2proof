import asyncio
from concurrent.futures import Executor, ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from app.domain.errors import P2PError
from app.domain.models import ErrorCode, StepName
from app.domain.paper import IngestedPaper
from app.execution.fake_backend import FakeJobBackend
from app.ingestion.arxiv import ArxivMetadata
from app.orchestrator.budget import Budget, load_budget_limits
from app.orchestrator.context import RunContext
from app.orchestrator.steps.ingesting import IngestingStep
from app.storage.artifacts import LocalArtifactStore
from app.storage.store import Store
from tests.ingestion.helpers import canary_tar, make_tar, minimal_pdf

ROOT = Path(__file__).resolve().parents[2]
RUN = "r_01jb7z00000000000000000000"


class FakeArxiv:
    def __init__(self, source: bytes) -> None:
        self._source = source

    async def metadata(self, arxiv_id: str) -> ArxivMetadata:
        return ArxivMetadata(arxiv_id=arxiv_id, title="From metadata")

    async def source(self, arxiv_id: str) -> bytes:
        return self._source


async def _ctx(store: Store, pool: Executor) -> RunContext:
    await store.create_run(RUN, "0000.00001", created_by=None, claim_hint=None, budget={})
    limits = load_budget_limits(ROOT / "config" / "budgets.yaml")
    return RunContext(
        run_id=RUN,
        step=StepName.INGESTING,
        attempt=1,
        store=store,
        budget=Budget(store, limits, env="test"),
        limits=limits,
        jobs=FakeJobBackend(),
        cpu_pool=pool,
    )


async def test_latex_paper_is_ingested_off_the_event_loop(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts = LocalArtifactStore(tmp_path)
    loop = asyncio.get_running_loop()
    used: list[Any] = []
    original = loop.run_in_executor

    def spy(executor: Any, fn: Any, *args: Any) -> Any:
        used.append(executor)
        return original(executor, fn, *args)

    monkeypatch.setattr(loop, "run_in_executor", spy)
    with ThreadPoolExecutor(1) as pool:
        ctx = await _ctx(store, pool)
        result = await IngestingStep(FakeArxiv(canary_tar()), artifacts).run(ctx)
        assert used and used[0] is pool  # parsing ran in the CPU pool (later: to_thread I/O)
    assert result.next is StepName.EXTRACTING_CLAIMS
    assert result.output_ref == f"runs/{RUN}/ingested.json"
    paper = IngestedPaper.model_validate(artifacts.get_json(result.output_ref))
    assert len(paper.tables) == 2
    assert paper.title == "Logistic Regression on Digits Is Enough"
    run = await store.get_run(RUN)
    assert run is not None and run.paper_sha256 == paper.sha256


async def test_pdf_paper_gets_page_images(store: Store, tmp_path: Path) -> None:
    artifacts = LocalArtifactStore(tmp_path)
    pdf = minimal_pdf(["Intro", "Table 1: results"])
    with ThreadPoolExecutor(1) as pool:
        result = await IngestingStep(FakeArxiv(pdf), artifacts).run(await _ctx(store, pool))
    assert result.output_ref is not None
    paper = IngestedPaper.model_validate(artifacts.get_json(result.output_ref))
    assert paper.source_kind == "pdf"
    assert paper.title == "From metadata"
    assert [img.page for img in paper.page_images] == [2]
    assert artifacts.get_bytes(paper.page_images[0].key).startswith(b"\x89PNG")


async def test_hostile_archive_fails_with_ingest_failed(store: Store, tmp_path: Path) -> None:
    with ThreadPoolExecutor(1) as pool:
        ctx = await _ctx(store, pool)
        with pytest.raises(P2PError) as err:
            await IngestingStep(
                FakeArxiv(make_tar({"../x": b"x"})), LocalArtifactStore(tmp_path)
            ).run(ctx)
    assert err.value.code is ErrorCode.INGEST_FAILED


def test_artifact_keys_cannot_escape_the_root(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    for bad in ("../x", "/abs", "a/../../b", "a\\b", ""):
        with pytest.raises(ValueError):
            store.put_bytes(bad, b"x")

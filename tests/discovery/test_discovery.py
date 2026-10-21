import json
from pathlib import Path

import httpx
import pytest
import respx
from pydantic import SecretStr

from app.discovery.hf_papers import HF_API
from app.discovery.rank import github_repos
from app.discovery.tavily import TAVILY_API, TavilyClient
from app.domain.discovery import Discovery
from app.domain.events import WarningPayload
from app.domain.models import StepName
from app.ingestion.arxiv import EXPORT_API, ArxivClient, RateLimiter
from app.models.fake import FakeModelClient
from app.orchestrator.budget import SpendKind
from app.orchestrator.steps.discovering import DiscoveringStep
from app.storage.artifacts import LocalArtifactStore
from app.storage.store import Store
from tests.steps.conftest import LLM_FIXTURES, RUN, ctx_at

ARXIV_ID = "2106.09685"
NO_CODE_ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"
 xmlns:arxiv="http://arxiv.org/schemas/atom"><entry><title>LoRA</title>
 <summary>We propose LoRA.</summary></entry></feed>"""
CODE_ATOM = NO_CODE_ATOM.replace(
    b"</entry>", b"<arxiv:comment>Code: https://github.com/microsoft/LoRA.</arxiv:comment></entry>"
)


async def _no_sleep(_: float) -> None:
    return None


def _routes(*, hf_summary: str, atom: bytes, datasets: list[str] | None = None) -> None:
    respx.get(f"{HF_API}/papers/{ARXIV_ID}").mock(
        return_value=httpx.Response(200, json={"id": ARXIV_ID, "summary": hf_summary})
    )
    respx.get(f"{HF_API}/arxiv/{ARXIV_ID}/repos").mock(
        return_value=httpx.Response(
            200, json={"datasets": [{"id": d} for d in datasets or []], "models": [], "spaces": []}
        )
    )
    respx.get(EXPORT_API).mock(return_value=httpx.Response(200, content=atom))


async def _discover(
    store: Store,
    tmp_path: Path,
    *,
    case: str = "default",
    tavily_key: str | None = "test-key-not-real",
    runs: int = 1,
) -> tuple[Discovery, FakeModelClient]:
    fake = FakeModelClient(LLM_FIXTURES, case=case)
    artifacts = LocalArtifactStore(tmp_path)
    ctx = await ctx_at(
        store,
        StepName.DISCOVERING,
        previous=[(StepName.INGESTING, "x"), (StepName.EXTRACTING_CLAIMS, "y")],
        arxiv_id=ARXIV_ID,
    )
    step = DiscoveringStep(
        store=store,
        arxiv=ArxivClient(limiter=RateLimiter(0.0, sleep=_no_sleep)),
        tavily=TavilyClient(SecretStr(tavily_key) if tavily_key else None),
        models=fake,
        artifacts=artifacts,
    )
    for _ in range(runs):
        result = await step.run(ctx)
    assert result.next is StepName.PLANNING
    assert result.output_ref is not None
    return Discovery.model_validate(artifacts.get_json(result.output_ref)), fake


def test_github_urls_are_canonicalized() -> None:
    text = (
        "Code: https://github.com/microsoft/LoRA. Also https://github.com/a/b.git and "
        "https://github.com/a/b/tree/main/src, https://github.com/topics/ml, "
        "https://github.com/x"
    )
    assert github_repos(text) == ["https://github.com/microsoft/LoRA", "https://github.com/a/b"]


@respx.mock
async def test_hf_hit_short_circuits_tavily(store: Store, tmp_path: Path) -> None:
    _routes(
        hf_summary="We propose LoRA. Code at https://github.com/microsoft/LoRA",
        atom=NO_CODE_ATOM,
        datasets=["glue"],
    )
    tavily = respx.post(f"{TAVILY_API}/search")
    discovery, fake = await _discover(store, tmp_path)
    assert discovery.repo is not None
    assert discovery.repo.url == "https://github.com/microsoft/LoRA"
    assert discovery.repo.official
    assert [d.id for d in discovery.datasets] == ["glue"]
    assert tavily.call_count == 0
    assert fake.calls == []  # an author-provided link needs no ranking


@respx.mock
async def test_arxiv_comment_link_is_official(store: Store, tmp_path: Path) -> None:
    _routes(hf_summary="No link here.", atom=CODE_ATOM)
    discovery, _ = await _discover(store, tmp_path)
    assert discovery.repo is not None
    assert (discovery.repo.url, discovery.repo.source) == (
        "https://github.com/microsoft/LoRA",
        "paper",
    )


@respx.mock
async def test_hf_miss_falls_back_to_tavily_search_and_extract(
    store: Store, tmp_path: Path
) -> None:
    _routes(hf_summary="No link here.", atom=NO_CODE_ATOM)
    search = respx.post(f"{TAVILY_API}/search").mock(
        side_effect=[
            httpx.Response(200, json={"results": [], "usage": {"credits": 1}}),  # basic: nothing
            httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "url": "https://github.com/someone/lora-reimpl",
                            "content": "a reimplementation",
                        },
                        {
                            "url": "https://paperswithcode.com/paper/lora",
                            "content": "official code",
                        },
                    ],
                    "usage": {"credits": 2},
                },
            ),
        ]
    )
    extract = respx.post(f"{TAVILY_API}/extract").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://paperswithcode.com/paper/lora",
                        "raw_content": "Official: https://github.com/authors/lora-official",
                    }
                ],
                "usage": {"credits": 1},
            },
        )
    )
    discovery, fake = await _discover(store, tmp_path)
    assert discovery.repo is not None
    assert discovery.repo.url == "https://github.com/authors/lora-official"
    assert (discovery.repo.source, discovery.repo.official) == ("tavily", False)
    bodies = [json.loads(c.request.read()) for c in search.calls]
    assert [b["search_depth"] for b in bodies] == ["basic", "advanced"]
    assert all("api_key" not in b for b in bodies)  # key only in the Authorization header
    assert extract.call_count == 1
    assert [r for r, _, _ in fake.calls][-1].value == "utility"
    ctx_budget = await store.ledger_total(SpendKind.TAVILY_CREDITS.value, run_id=RUN)
    assert ctx_budget == 4  # 1 + 2 + 1 credits from usage


@respx.mock
async def test_cache_prevents_a_second_http_call(store: Store, tmp_path: Path) -> None:
    _routes(hf_summary="Code at https://github.com/microsoft/LoRA", atom=NO_CODE_ATOM)
    await _discover(store, tmp_path, runs=2)
    assert respx.calls.call_count == 3  # HF paper, HF repos, arXiv metadata: once each


@respx.mock
async def test_a_failed_metadata_fetch_is_not_cached(store: Store, tmp_path: Path) -> None:
    """arXiv down once must not hide the paper's code link for the next 24 hours."""
    _routes(hf_summary="Code at https://github.com/microsoft/LoRA", atom=NO_CODE_ATOM)
    respx.get(EXPORT_API).mock(
        side_effect=[httpx.Response(503), httpx.Response(200, content=NO_CODE_ATOM)]
    )
    await _discover(store, tmp_path, runs=2)
    assert respx.get(EXPORT_API).call_count == 2  # asked again: the failure was not cached


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(429, json={"detail": "rate limit"}),
        httpx.Response(
            432, json={"detail": {"error": "This request exceeds your plan's set usage limit."}}
        ),
    ],
)
@respx.mock
async def test_tavily_quota_errors_degrade_gracefully(
    store: Store, tmp_path: Path, response: httpx.Response
) -> None:
    _routes(hf_summary="No link here.", atom=NO_CODE_ATOM)
    respx.post(f"{TAVILY_API}/search").mock(return_value=response)
    discovery, _ = await _discover(store, tmp_path)
    assert discovery.repo is None
    assert any("DISCOVERY_DEGRADED" in n for n in discovery.notes)
    warnings = [e.payload for e in await store.events_after(RUN, 0)]
    assert any(isinstance(p, WarningPayload) and p.code == "DISCOVERY_DEGRADED" for p in warnings)


@respx.mock
async def test_ranker_cannot_invent_a_repository(store: Store, tmp_path: Path) -> None:
    _routes(hf_summary="No link here.", atom=NO_CODE_ATOM)
    respx.post(f"{TAVILY_API}/search").mock(
        return_value=httpx.Response(
            200, json={"results": [{"url": "https://github.com/someone/lora", "content": ""}]}
        )
    )
    respx.post(f"{TAVILY_API}/extract").mock(return_value=httpx.Response(200, json={"results": []}))
    discovery, _ = await _discover(store, tmp_path, case="invented")
    assert discovery.repo is None
    assert any("not among the candidates" in n for n in discovery.notes)


@respx.mock
async def test_without_a_tavily_key_discovery_still_completes(store: Store, tmp_path: Path) -> None:
    _routes(hf_summary="No link here.", atom=NO_CODE_ATOM)
    discovery, _ = await _discover(store, tmp_path, tavily_key=None)
    assert discovery.repo is None
    assert any("DISCOVERY_DEGRADED" in n for n in discovery.notes)


@respx.mock  # no route: any HTTP call (arXiv, HF, Tavily) fails the test
async def test_a_canary_paper_never_searches_for_code(store: Store, tmp_path: Path) -> None:
    """Canary titles are public: a repository anyone could publish must never steer the Coder
    whose code runs on the developer machine (A-29 review)."""
    canary = "c" * 64
    fake = FakeModelClient(LLM_FIXTURES)
    artifacts = LocalArtifactStore(tmp_path)
    ctx = await ctx_at(
        store,
        StepName.DISCOVERING,
        previous=[(StepName.INGESTING, "x"), (StepName.EXTRACTING_CLAIMS, "y")],
        arxiv_id="0000.00001",
    )
    await store.set_paper_sha256(RUN, canary)
    step = DiscoveringStep(
        store=store,
        arxiv=ArxivClient(limiter=RateLimiter(0.0, sleep=_no_sleep)),
        tavily=TavilyClient(SecretStr("test-key-not-real")),
        models=fake,
        artifacts=artifacts,
        canaries=frozenset({canary}),
    )
    result = await step.run(ctx)
    assert result.next is StepName.PLANNING and result.output_ref is not None
    discovery = Discovery.model_validate(artifacts.get_json(result.output_ref))
    assert discovery.repo is None and discovery.datasets == []
    assert any("canary" in note for note in discovery.notes)
    assert not fake.calls

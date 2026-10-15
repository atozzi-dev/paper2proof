import httpx
import pytest
import respx

from app.ingestion.arxiv import ArxivClient, RateLimiter
from tests.ingestion.helpers import canary_tar, minimal_pdf

ATOM = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2106.09685v2</id>
    <title>LoRA: Low-Rank Adaptation of
      Large Language Models</title>
    <summary>An abstract.</summary>
  </entry>
</feed>"""


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


async def test_rate_limiter_spaces_calls_by_three_seconds() -> None:
    clock = FakeClock()
    limiter = RateLimiter(3.0, clock=clock, sleep=clock.sleep)
    await limiter.wait()
    clock.now += 1.0
    await limiter.wait()
    await limiter.wait()
    assert clock.sleeps == [pytest.approx(2.0), pytest.approx(3.0)]


@respx.mock
async def test_metadata_title_and_user_agent() -> None:
    route = respx.get("https://export.arxiv.org/api/query").mock(
        return_value=httpx.Response(200, content=ATOM)
    )
    clock = FakeClock()
    client = ArxivClient(limiter=RateLimiter(3.0, clock=clock, sleep=clock.sleep))
    meta = await client.metadata("2106.09685")
    assert meta.title == "LoRA: Low-Rank Adaptation of Large Language Models"
    request = route.calls[0].request
    assert request.url.params["id_list"] == "2106.09685"
    assert "paper2proof" in request.headers["user-agent"]
    assert "github.com" in request.headers["user-agent"]  # contact, as arXiv asks


@respx.mock
async def test_source_tarball_and_pdf_fallback() -> None:
    respx.get("https://arxiv.org/e-print/2106.09685").mock(
        return_value=httpx.Response(200, content=canary_tar())
    )
    respx.get("https://arxiv.org/e-print/2001.00001").mock(return_value=httpx.Response(404))
    respx.get("https://arxiv.org/pdf/2001.00001").mock(
        return_value=httpx.Response(200, content=minimal_pdf(["x"]))
    )
    clock = FakeClock()
    client = ArxivClient(limiter=RateLimiter(3.0, clock=clock, sleep=clock.sleep))
    assert (await client.source("2106.09685"))[:2] == b"\x1f\x8b"
    assert (await client.source("2001.00001")).startswith(b"%PDF")


@respx.mock
async def test_oversized_download_is_refused() -> None:
    respx.get("https://arxiv.org/e-print/2106.09685").mock(
        return_value=httpx.Response(200, content=b"x" * 2000)
    )
    clock = FakeClock()
    client = ArxivClient(limiter=RateLimiter(3.0, clock=clock, sleep=clock.sleep), max_bytes=1000)
    with pytest.raises(ValueError, match="large"):
        await client.source("2106.09685")


@pytest.mark.parametrize("bad", ["../x", "2106.09685/../../x", "http://evil"])
async def test_ids_are_validated_again(bad: str) -> None:
    client = ArxivClient()
    with pytest.raises(ValueError):
        await client.source(bad)

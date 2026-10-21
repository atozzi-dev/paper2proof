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


def test_the_arxiv_id_pattern_takes_ascii_digits_only() -> None:
    from app.ingestion.arxiv import ARXIV_ID

    assert ARXIV_ID.fullmatch("2410.01234")
    assert not ARXIV_ID.fullmatch(
        "".join(chr(0x0660 + int(c)) if c.isdigit() else c for c in "2410.01234")
    )


def _client(http: httpx.AsyncClient | None = None) -> ArxivClient:
    clock = FakeClock()
    return ArxivClient(http, limiter=RateLimiter(0.0, clock=clock, sleep=clock.sleep))


@respx.mock
async def test_a_failed_metadata_fetch_raises_instead_of_an_empty_answer() -> None:
    """An empty answer would be cached by DISCOVERING for 24 hours."""
    respx.get("https://export.arxiv.org/api/query").mock(return_value=httpx.Response(503))
    with pytest.raises(ValueError, match="503"):
        await _client().metadata("2106.09685")


@respx.mock
async def test_arxiv_redirects_are_followed_with_the_shared_client() -> None:
    """main.py's shared client never follows redirects: arXiv's own ones are followed here."""
    respx.get("https://arxiv.org/e-print/2106.09685").mock(
        return_value=httpx.Response(
            301, headers={"Location": "https://arxiv.org/e-print/2106.09685v2"}
        )
    )
    respx.get("https://arxiv.org/e-print/2106.09685v2").mock(
        return_value=httpx.Response(200, content=canary_tar())
    )
    async with httpx.AsyncClient(follow_redirects=False) as http:
        assert (await _client(http).source("2106.09685"))[:2] == b"\x1f\x8b"


@pytest.mark.parametrize(
    "location",
    [
        "https://evil.example/e-print/2106.09685",
        "http://arxiv.org/e-print/2106.09685v2",  # never down to plain http
        "https://arxiv.org.evil.example/x",
        "//evil.example/x",  # protocol-relative
        "https://arxiv.org:8443/x",  # another port
        "https://user:pw@arxiv.org/x",  # credentials in the URL
        "https://arxiv.org:99999/x",  # httpx.InvalidURL: a ValueError like any refusal
    ],
)
@respx.mock
async def test_a_redirect_off_arxiv_is_refused(location: str) -> None:
    respx.get("https://arxiv.org/e-print/2106.09685").mock(
        return_value=httpx.Response(302, headers={"Location": location})
    )
    other = respx.get(url__regex=r".*evil.*|http://arxiv\.org/.*").mock(
        return_value=httpx.Response(200, content=canary_tar())
    )
    respx.get("https://arxiv.org/pdf/2106.09685").mock(return_value=httpx.Response(404))
    async with httpx.AsyncClient(follow_redirects=False) as http:
        with pytest.raises(ValueError):
            await _client(http).source("2106.09685")
    assert other.call_count == 0


@respx.mock
async def test_a_redirect_loop_is_bounded() -> None:
    respx.get("https://arxiv.org/e-print/2106.09685").mock(
        return_value=httpx.Response(
            302, headers={"Location": "https://arxiv.org/e-print/2106.09685"}
        )
    )
    respx.get("https://arxiv.org/pdf/2106.09685").mock(return_value=httpx.Response(404))
    async with httpx.AsyncClient(follow_redirects=False) as http:
        with pytest.raises(ValueError):
            await _client(http).source("2106.09685")


@respx.mock
async def test_a_relative_redirect_stays_on_arxiv() -> None:
    respx.get("https://arxiv.org/e-print/2106.09685").mock(
        return_value=httpx.Response(302, headers={"Location": "/e-print/2106.09685v2"})
    )
    respx.get("https://arxiv.org/e-print/2106.09685v2").mock(
        return_value=httpx.Response(200, content=canary_tar())
    )
    async with httpx.AsyncClient(follow_redirects=False) as http:
        assert (await _client(http).source("2106.09685"))[:2] == bytes([0x1F, 0x8B])

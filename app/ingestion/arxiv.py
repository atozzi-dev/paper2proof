"""arXiv client: metadata (export API) and e-print source, politely rate limited."""

import asyncio
import re
import time
from collections.abc import Awaitable, Callable

import httpx
from defusedxml import ElementTree
from pydantic import BaseModel

ARXIV_ID = re.compile(r"^\d{4}\.\d{4,5}(v\d+)?$", re.ASCII)  # without it \d is any digit
USER_AGENT = "paper2proof/0.1 (+https://github.com/atozzi-dev/paper2proof)"
EXPORT_API = "https://export.arxiv.org/api/query"
EPRINT = "https://arxiv.org/e-print/"
PDF = "https://arxiv.org/pdf/"
MAX_DOWNLOAD = 60 * 1024 * 1024
MAX_REDIRECTS = 5
_ARXIV_HOSTS = frozenset({"arxiv.org", "www.arxiv.org", "export.arxiv.org"})
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV = "{http://arxiv.org/schemas/atom}"


class ArxivMetadata(BaseModel):
    arxiv_id: str
    title: str | None
    abstract: str | None = None
    comment: str | None = None  # authors often put "Code: https://github.com/..." here


class RateLimiter:
    """At most one request per `interval` seconds across every caller sharing the instance."""

    def __init__(
        self,
        interval: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._interval = interval
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._last: float | None = None

    async def wait(self) -> None:
        async with self._lock:
            now = self._clock()
            if self._last is not None and now - self._last < self._interval:
                await self._sleep(self._interval - (now - self._last))
            self._last = self._clock()


SHARED_LIMITER = RateLimiter(3.0)  # arXiv asks for 1 request every 3 seconds


def _check(arxiv_id: str) -> str:
    if not ARXIV_ID.fullmatch(arxiv_id):  # invariant 11, again at the boundary
        raise ValueError(f"not an arXiv ID: {arxiv_id!r}")
    return arxiv_id


def _arxiv_redirect(response: httpx.Response) -> httpx.URL:
    """The redirect's target, only when it is https on an arXiv host (default port, no
    credentials); a malformed Location is a ValueError like any refused one."""
    location = response.headers.get("location", "")
    try:
        target = response.request.url.join(location)
    except httpx.InvalidURL:
        raise ValueError("arXiv sent an invalid redirect") from None
    if (
        target.scheme != "https"
        or target.host not in _ARXIV_HOSTS
        or target.port not in (None, 443)
        or target.userinfo
    ):
        raise ValueError(f"arXiv redirected off arXiv: {str(target)[:200]}")
    return target


class ArxivClient:
    def __init__(
        self,
        http: httpx.AsyncClient | None = None,
        *,
        limiter: RateLimiter = SHARED_LIMITER,
        max_bytes: int = MAX_DOWNLOAD,
    ) -> None:
        self._http = http
        self._limiter = limiter
        self._max_bytes = max_bytes

    async def _get(self, url: str, params: dict[str, str] | None = None) -> tuple[int, bytes]:
        """GET with arXiv's own redirects followed by hand (the shared client follows none):
        https only, arXiv hosts only, at most MAX_REDIRECTS."""
        headers = {"User-Agent": USER_AGENT}
        client = self._http or httpx.AsyncClient(timeout=60, follow_redirects=False)
        target = httpx.URL(url)
        try:
            for _ in range(MAX_REDIRECTS + 1):
                await self._limiter.wait()
                async with client.stream(
                    "GET", target, params=params, headers=headers, follow_redirects=False
                ) as response:
                    if response.status_code in _REDIRECTS:
                        target = _arxiv_redirect(response)
                        params = None  # the Location carries its own query
                        continue
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > self._max_bytes:
                            raise ValueError("download too large")
                        chunks.append(chunk)
                    return response.status_code, b"".join(chunks)
            raise ValueError("too many redirects from arXiv")
        finally:
            if self._http is None:
                await client.aclose()

    async def metadata(self, arxiv_id: str) -> ArxivMetadata:
        status, body = await self._get(EXPORT_API, {"id_list": _check(arxiv_id)})
        if status != 200:  # never an empty answer: callers would cache it as the truth
            raise ValueError(f"arXiv metadata returned HTTP {status} for {arxiv_id}")
        entry = ElementTree.fromstring(body).find(f"{_ATOM}entry")

        def text(tag: str) -> str | None:
            value = entry.findtext(tag) if entry is not None else None
            return " ".join(value.split()) if value else None

        return ArxivMetadata(
            arxiv_id=arxiv_id,
            title=text(f"{_ATOM}title"),
            abstract=text(f"{_ATOM}summary"),
            comment=text(f"{_ARXIV}comment"),
        )

    async def source(self, arxiv_id: str) -> bytes:
        """The e-print (tar.gz, gzipped .tex or PDF); falls back to the PDF."""
        _check(arxiv_id)
        status, body = await self._get(EPRINT + arxiv_id)
        if status == 200 and body:
            return body
        status, body = await self._get(PDF + arxiv_id)
        if status != 200:
            raise ValueError(f"arXiv returned HTTP {status} for {arxiv_id}")
        return body

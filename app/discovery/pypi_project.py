"""The authors' own package on PyPI. An official repository whose sources need a build step (a
compiled library: InterpretML's EBM) installs through the package its authors publish, and the
Planner does not always plan it. A PyPI project counts as the authors' only when its own
metadata links back to that very repository: one that merely shares the repository's name could
be anybody's (supply-chain risk). Read-only and unauthenticated; any doubt adds nothing. What is
found is only a request: the package policy still decides (invariant 3)."""

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Collection

import httpx

from app.discovery.github import _get_json, api_url
from app.domain.discovery import GITHUB_REPO
from app.execution.package_policy import PYPI, normalize
from app.ingestion.arxiv import USER_AGENT

TIMEOUT_S = 10.0  # the whole lookup: PyPI, and GitHub when the repository may have moved
MAX_BODY_BYTES = 8 * 2**20  # a project's JSON lists every release file (catboost: ~1.2 MB)
MAX_LINKS = 50
_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?$")  # PEP 508 name
# http(s), github.com (or www.) without credentials or port, then <owner>/<repo>; anything
# after the repository (/tree/..., /issues, #readme) still points into it
_LINK = re.compile(
    r"^https?://(?:www\.)?github\.com/"
    r"([A-Za-z0-9_.-]{1,100})/([A-Za-z0-9_.-]{1,100})(?:[/?#].*)?$",
    re.IGNORECASE,
)
_HEADERS = {"Accept": "application/json", "User-Agent": USER_AGENT}

RunCpu = Callable[[Callable[[bytes], list[str]], bytes], Awaitable[list[str]]]


def repo_of(link: object) -> str | None:
    """The `owner/repo` (lowercase) a link points into, without a trailing .git; None for
    anything that is not a link into a GitHub repository."""
    match = _LINK.fullmatch(link.strip()) if isinstance(link, str) else None
    if match is None:
        return None
    owner, repo = match[1].lower(), match[2].lower().removesuffix(".git")
    if not owner.strip(".") or not repo.strip("."):
        return None  # "." and ".." are path segments, not names
    return f"{owner}/{repo}"


def project_links(body: bytes) -> list[str]:
    """The links a PyPI project's metadata declares: the `info.project_urls` values, then
    `home_page` and `download_url`. Never the description: a README may link anything.
    CPU-bound on a large answer: runs in the process pool (invariant 12)."""
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        return []
    info = data.get("info") if isinstance(data, dict) else None
    if not isinstance(info, dict):
        return []
    urls = info.get("project_urls")
    links = [*(urls.values() if isinstance(urls, dict) else []), info.get("home_page")]
    links.append(info.get("download_url"))
    return [link for link in links if isinstance(link, str)][:MAX_LINKS]


async def _inline(fn: Callable[[bytes], list[str]], body: bytes) -> list[str]:
    return fn(body)


async def _pypi(client: httpx.AsyncClient, name: str) -> bytes | None:
    """A project's JSON; None for anything but a 200 within the byte cap (never a redirect)."""
    async with client.stream(
        "GET", f"{PYPI}/{name}/json", headers=_HEADERS, timeout=TIMEOUT_S, follow_redirects=False
    ) as response:
        if response.status_code != 200:
            return None
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body += chunk
            if len(body) > MAX_BODY_BYTES:
                return None
        return bytes(body)


async def _moved_to(client: httpx.AsyncClient, repo_url: str) -> str | None:
    """The `owner/repo` (lowercase) GitHub serves the repository under now: a transferred or
    renamed repository answers with a move (InterpretML: microsoft/interpret is now
    interpretml/interpret). The same read-only API call PROVISIONING makes (github.py)."""
    url = api_url(repo_url)
    data = await _get_json(client, url) if url is not None else None
    name = data.get("full_name") if isinstance(data, dict) else None
    return name.lower() if isinstance(name, str) else None


def _candidates(repo: str) -> list[str]:
    """The repository's name and, when different, its normalized form: valid PyPI names only."""
    names = [repo] if repo == normalize(repo) else [repo, normalize(repo)]
    return [n for n in names if _NAME.fullmatch(n)]


async def _find(
    client: httpx.AsyncClient, repo_url: str, names: list[str], run_cpu: RunCpu
) -> str | None:
    owner_repo = repo_of(repo_url)
    for name in names:
        body = await _pypi(client, name)
        if body is None:
            continue
        linked = {repo for link in await run_cpu(project_links, body) if (repo := repo_of(link))}
        if owner_repo in linked:
            return normalize(name)
        if linked and await _moved_to(client, repo_url) in linked:
            return normalize(name)
        return None  # PyPI answered: every spelling of the name is this same project
    return None


async def authors_package(
    repo_url: str,
    *,
    planned: Collection[str],
    http: httpx.AsyncClient | None = None,
    run_cpu: RunCpu | None = None,
) -> str | None:
    """The PyPI name of the authors' package of the repository at `repo_url` (a canonical
    https://github.com/<owner>/<repo>, linked by the authors): the project named after the
    repository whose metadata links back to it. None when unknown, when there is none, or when
    a name in `planned` (normalized) already stands for it."""
    if not GITHUB_REPO.fullmatch(repo_url) or repo_of(repo_url) is None:
        return None
    names = _candidates(repo_url.rsplit("/", 1)[-1])
    if not names or any(normalize(n) in planned for n in names):
        return None
    client = http or httpx.AsyncClient()
    try:
        async with asyncio.timeout(TIMEOUT_S):
            return await _find(client, repo_url, names, run_cpu or _inline)
    except (httpx.HTTPError, ValueError, RecursionError, TimeoutError):
        return None
    finally:
        if http is None:
            await client.aclose()

"""Repository metadata from the GitHub REST API: unauthenticated, read-only, never a clone.
PROVISIONING reads a repository's size before a fetch job is billed for cloning one that the
provisioner would refuse (images/provisioner/provision.py: MAX_REPO_BYTES, kept equal by a
test). Any doubt (rate limit, outage, odd answer) is an unknown size, never a reason to skip."""

import asyncio
import json
import re
from typing import Any

import httpx

from app.ingestion.arxiv import USER_AGENT

GITHUB_API = "https://api.github.com"
MAX_REPO_BYTES = 2**30  # the provisioner's cap on a clone
TIMEOUT_S = 10.0  # the whole lookup; an unknown size only means the fetch job checks
MAX_BODY_BYTES = 256 * 1024  # a repository's metadata is a few KiB
_MOVED = re.compile(r"^https://api\.github\.com/repositories/[0-9]{1,20}$")  # a renamed repo
_REDIRECTS = {301, 302, 307, 308}
_REPO = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]{1,100})/([A-Za-z0-9_.-]{1,100})$")
_HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": USER_AGENT,  # GitHub refuses requests without one
    "X-GitHub-Api-Version": "2022-11-28",
}


def api_url(repo_url: str) -> str | None:
    """The REST URL of a canonical https://github.com/<owner>/<repo>; None for anything else."""
    match = _REPO.fullmatch(repo_url)
    if match is None or any(not part.strip(".") for part in match.groups()):
        return None  # "." and ".." are path segments, not names
    return f"{GITHUB_API}/repos/{match[1]}/{match[2]}"


async def _get_json(client: httpx.AsyncClient, url: str) -> Any:
    """The JSON answer, following at most one move of a renamed repository and only within the
    API (/repositories/<id>); None for anything else, or for a body over MAX_BODY_BYTES."""
    for _ in range(2):
        async with client.stream(
            "GET", url, headers=_HEADERS, timeout=TIMEOUT_S, follow_redirects=False
        ) as response:
            if response.status_code in _REDIRECTS:
                url = response.headers.get("location", "")
                if not _MOVED.fullmatch(url):
                    return None
                continue
            if response.status_code != 200:
                return None
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > MAX_BODY_BYTES:
                    return None
            return json.loads(body)
    return None


async def repo_size_kib(repo_url: str, http: httpx.AsyncClient | None = None) -> int | None:
    """What GitHub reports as the repository's size, in KiB (its packed history: an estimate
    of what the provisioner's clone measures, not the same number); None when unknown."""
    url = api_url(repo_url)
    if url is None:
        return None
    client = http or httpx.AsyncClient()
    try:
        async with asyncio.timeout(TIMEOUT_S):
            data = await _get_json(client, url)
    except (httpx.HTTPError, ValueError, RecursionError, TimeoutError):
        return None
    finally:
        if http is None:
            await client.aclose()
    size = data.get("size") if isinstance(data, dict) else None
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        return None
    return size

"""Repository metadata from the GitHub REST API: unauthenticated, read-only, never a clone.
PROVISIONING reads a repository's size before a fetch job is billed for cloning one that the
provisioner would refuse (images/provisioner/provision.py: MAX_REPO_BYTES, kept equal by a
test). Any doubt (rate limit, outage, odd answer) is an unknown size, never a reason to skip."""

import re

import httpx

from app.ingestion.arxiv import USER_AGENT

GITHUB_API = "https://api.github.com"
MAX_REPO_BYTES = 2**30  # the provisioner's cap on a clone
TIMEOUT_S = 10.0  # an unknown size only means the fetch job checks, as it always did
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


async def repo_size_kib(repo_url: str, http: httpx.AsyncClient | None = None) -> int | None:
    """What GitHub reports as the repository's size, in KiB (its packed history: an estimate
    of what the provisioner's clone measures, not the same number); None when unknown."""
    url = api_url(repo_url)
    if url is None:
        return None
    client = http or httpx.AsyncClient()
    try:
        response = await client.get(
            url, headers=_HEADERS, timeout=TIMEOUT_S, follow_redirects=True
        )  # a renamed repository answers 301 to /repositories/<id>
        if response.status_code != 200:
            return None
        data = response.json()
    except (httpx.HTTPError, ValueError, RecursionError):
        return None
    finally:
        if http is None:
            await client.aclose()
    size = data.get("size") if isinstance(data, dict) else None
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        return None
    return size

"""GitHub link extraction and candidate ranking. The model may only pick among candidates."""

import re
from typing import Literal

from pydantic import BaseModel, Field

from app.models.client import Message, ModelClient, Role
from app.models.prompt_files import load_prompt
from app.models.untrusted import UNTRUSTED_NOTICE, wrap_untrusted

_GITHUB = re.compile(r"https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")
_NOT_OWNERS = frozenset(
    {"topics", "features", "orgs", "about", "marketplace", "sponsors", "settings", "login",
     "explore", "collections", "pricing", "enterprise", "search", "apps", "site"}
)  # fmt: skip


def github_repos(text: str) -> list[str]:
    """Canonical https://github.com/<owner>/<repo> URLs found in text, in order, deduplicated."""
    repos: list[str] = []
    for owner, name in _GITHUB.findall(text):
        name = name.rstrip(".").removesuffix(".git").rstrip(".")
        if not name or owner.lower() in _NOT_OWNERS:
            continue
        url = f"https://github.com/{owner}/{name}"
        if url not in repos:
            repos.append(url)
    return repos


class RankedSources(BaseModel):
    best_url: str | None = Field(max_length=300)
    kind: Literal["official", "author_fork", "third_party", "none"]


async def rank_candidates(
    models: ModelClient,
    candidates: dict[str, str],
    *,
    title: str | None,
    run_id: str,
    step: str,
) -> tuple[str | None, str | None]:
    """(chosen URL, note). The Utility model ranks official > author fork > third party; its
    answer is accepted only if it is one of the candidates (it can't invent a repository)."""
    prompt = load_prompt("utility_rank_sources")
    listing = "\n".join(f"- {url}: {snippet[:300]}" for url, snippet in candidates.items())
    user = prompt.render(
        title=wrap_untrusted(title or "unknown", source="paper-title", max_chars=300),
        candidates=wrap_untrusted(listing, source="search-results", max_chars=8000),
    )
    ranked = await models.structured(
        Role.UTILITY,
        [Message(role="system", content=UNTRUSTED_NOTICE), Message(role="user", content=user)],
        RankedSources,
        run_id=run_id,
        step=step,
        prompt=prompt.name,
    )
    if ranked.best_url is None or ranked.kind == "none":
        return None, "no plausible repository among the search results"
    if ranked.best_url not in candidates:
        return None, "the ranker picked a repository not among the candidates: ignored"
    return ranked.best_url, None

"""Hugging Face Papers API (shape verified by spike S-9: no structured repo field; GitHub links
appear in the summary; /arxiv/<id>/repos lists HF datasets and models)."""

import hashlib
from datetime import timedelta
from typing import Any

import httpx

from app.ingestion.arxiv import USER_AGENT
from app.storage.store import Store

HF_API = "https://huggingface.co/api"
CACHE_TTL = timedelta(hours=24)


class HfUnavailable(Exception):
    """Hugging Face did not answer (unreachable, 429 or 5xx, malformed body): never cached."""


async def cached_get_json(
    store: Store, provider: str, url: str, http: httpx.AsyncClient | None = None
) -> dict[str, Any] | None:
    """GET a JSON document through the 24 h discovery cache; None on any other non-200 (a 404:
    HF has no page for the paper). An outage raises HfUnavailable and is not cached."""
    key = hashlib.sha256(url.encode()).hexdigest()
    hit = await store.cache_get(provider, key, max_age=CACHE_TTL)
    if hit is not None:
        return hit.get("body")
    client = http or httpx.AsyncClient(timeout=30)
    try:
        response = await client.get(url, headers={"User-Agent": USER_AGENT})
    except httpx.HTTPError as err:
        raise HfUnavailable(f"Hugging Face unreachable: {type(err).__name__}") from None
    finally:
        if http is None:
            await client.aclose()
    if response.status_code == 429 or response.status_code >= 500:
        raise HfUnavailable(f"Hugging Face returned HTTP {response.status_code}")
    data: Any = None
    if response.status_code == 200:
        try:
            data = response.json()
        except ValueError:
            raise HfUnavailable("Hugging Face returned a malformed body") from None
    body = data if isinstance(data, dict) else None
    await store.cache_put(provider, key, {"body": body})
    return body


class HfPapers:
    def __init__(self, store: Store, http: httpx.AsyncClient | None = None) -> None:
        self._store = store
        self._http = http

    async def summary(self, arxiv_id: str) -> str:
        paper = await cached_get_json(self._store, "hf", f"{HF_API}/papers/{arxiv_id}", self._http)
        if not paper:
            return ""
        return " ".join(str(paper.get(k) or "") for k in ("summary", "ai_summary"))

    async def dataset_ids(self, arxiv_id: str) -> list[str]:
        url = f"{HF_API}/arxiv/{arxiv_id}/repos"
        repos = await cached_get_json(self._store, "hf", url, self._http)
        datasets = (repos or {}).get("datasets") or []
        return [str(d["id"]) for d in datasets if isinstance(d, dict) and d.get("id")]

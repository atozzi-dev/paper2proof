"""Tavily search and extract (functional runtime use for the hackathon's Tavily prize).
Credits go through the budget (SpendKind.TAVILY_CREDITS) before each call (invariant 7)."""

import hashlib
import math
from datetime import timedelta
from typing import Any, Literal

import httpx
from pydantic import BaseModel, SecretStr

from app.orchestrator.budget import Budget, SpendKind
from app.storage.store import Store

TAVILY_API = "https://api.tavily.com"
CACHE_TTL = timedelta(hours=24)
CREDITS = {"basic": 1, "advanced": 2}


class TavilyUnavailable(Exception):
    """No key, quota exhausted, rate limited or down: discovery degrades, the run continues."""


class TavilyResult(BaseModel):
    url: str
    content: str = ""


class TavilyClient:
    def __init__(self, api_key: SecretStr | None, http: httpx.AsyncClient | None = None) -> None:
        self._key = api_key
        self._http = http

    @property
    def enabled(self) -> bool:
        return self._key is not None and bool(self._key.get_secret_value())

    async def _post(
        self,
        path: str,
        body: dict[str, Any],
        *,
        credits: int,
        budget: Budget,
        run_id: str | None,
        store: Store,
    ) -> dict[str, Any]:
        if self._key is None or not self._key.get_secret_value():
            raise TavilyUnavailable("Tavily is not configured")
        key = hashlib.sha256(f"{path}:{sorted(body.items())}".encode()).hexdigest()
        hit = await store.cache_get("tavily", key, max_age=CACHE_TTL)
        if hit is not None:
            return hit
        reservation = await budget.check_and_reserve(run_id, SpendKind.TAVILY_CREDITS, credits)
        spent = 0.0  # settled in `finally`, whatever happens below
        client = self._http or httpx.AsyncClient(timeout=60)
        try:
            try:
                response = await client.post(
                    f"{TAVILY_API}/{path}",
                    json={**body, "include_usage": True},
                    headers={"Authorization": f"Bearer {self._key.get_secret_value()}"},
                )
            except httpx.HTTPError as err:
                raise TavilyUnavailable(f"Tavily unreachable: {type(err).__name__}") from None
            if response.status_code != 200:
                raise TavilyUnavailable(f"Tavily returned HTTP {response.status_code}")
            spent = credits  # answered: assume billed until the usage field says otherwise
            try:
                data = response.json()
            except ValueError:
                raise TavilyUnavailable("Tavily returned a malformed body") from None
            if not isinstance(data, dict):
                raise TavilyUnavailable("Tavily returned a malformed body")
            usage = data.get("usage")
            used = usage.get("credits") if isinstance(usage, dict) else None
            if isinstance(used, int | float) and math.isfinite(used) and used >= 0:
                spent = float(used)
        finally:
            if self._http is None:
                await client.aclose()
            await budget.commit(reservation, spent)
        await store.cache_put("tavily", key, data)
        return data

    async def search(
        self,
        query: str,
        depth: Literal["basic", "advanced"],
        *,
        budget: Budget,
        run_id: str | None,
        store: Store,
    ) -> list[TavilyResult]:
        data = await self._post(
            "search",
            {"query": query, "search_depth": depth, "max_results": 5},
            credits=CREDITS[depth],
            budget=budget,
            run_id=run_id,
            store=store,
        )
        return [TavilyResult.model_validate(r) for r in data.get("results", []) if "url" in r]

    async def extract(
        self, urls: list[str], query: str, *, budget: Budget, run_id: str | None, store: Store
    ) -> list[TavilyResult]:
        data = await self._post(
            "extract",
            {"urls": urls, "query": query},
            credits=math.ceil(len(urls) / 5),
            budget=budget,
            run_id=run_id,
            store=store,
        )
        return [
            TavilyResult(url=r["url"], content=str(r.get("raw_content") or ""))
            for r in data.get("results", [])
            if "url" in r
        ]

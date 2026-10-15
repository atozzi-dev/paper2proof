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
        client = self._http or httpx.AsyncClient(timeout=60)
        try:
            response = await client.post(
                f"{TAVILY_API}/{path}",
                json={**body, "include_usage": True},
                headers={"Authorization": f"Bearer {self._key.get_secret_value()}"},
            )
        except httpx.HTTPError as err:
            await budget.commit(reservation, 0)
            raise TavilyUnavailable(f"Tavily unreachable: {type(err).__name__}") from None
        finally:
            if self._http is None:
                await client.aclose()
        if response.status_code != 200:
            await budget.commit(reservation, 0)
            raise TavilyUnavailable(f"Tavily returned HTTP {response.status_code}")
        data: dict[str, Any] = response.json()
        used = (data.get("usage") or {}).get("credits")
        await budget.commit(reservation, float(used) if isinstance(used, int | float) else credits)
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

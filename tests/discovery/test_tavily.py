"""Tavily credits are always settled, whatever the response looks like."""

from pathlib import Path

import httpx
import pytest
import respx
from pydantic import SecretStr

from app.discovery.tavily import TAVILY_API, TavilyClient, TavilyUnavailable
from app.orchestrator.budget import Budget, SpendKind, load_budget_limits
from app.storage.store import Store

ROOT = Path(__file__).resolve().parents[2]


def _budget(store: Store) -> Budget:
    return Budget(store, load_budget_limits(ROOT / "config" / "budgets.yaml"), env="test")


@respx.mock
@pytest.mark.parametrize("credits", [float("nan"), float("inf"), -3, "lots"])
async def test_odd_usage_values_are_settled_as_the_estimate(store: Store, credits: object) -> None:
    respx.post(f"{TAVILY_API}/search").mock(
        return_value=httpx.Response(
            200,
            content=f'{{"results": [], "usage": {{"credits": {credits!r}}}}}'.replace("'", '"')
            .replace("nan", "NaN")
            .replace("inf", "Infinity"),
        )
    )
    budget = _budget(store)
    client = TavilyClient(SecretStr("test-key-not-real"))
    await client.search("q", "advanced", budget=budget, run_id=None, store=store)
    assert not budget._inflight
    assert await budget.day_spend(SpendKind.TAVILY_CREDITS) == 2


@respx.mock
async def test_a_malformed_body_still_settles_the_reservation(store: Store) -> None:
    respx.post(f"{TAVILY_API}/search").mock(return_value=httpx.Response(200, content=b"<html>"))
    budget = _budget(store)
    client = TavilyClient(SecretStr("test-key-not-real"))
    with pytest.raises(TavilyUnavailable):
        await client.search("q", "basic", budget=budget, run_id=None, store=store)
    assert not budget._inflight
    assert await budget.day_spend(SpendKind.TAVILY_CREDITS) == 1  # it may have been billed

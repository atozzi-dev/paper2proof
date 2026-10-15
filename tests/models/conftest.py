"""Helpers for respx-mocked Token Factory tests."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.models.client import (
    ModelsConfig,
    Role,
    StoreCallRecorder,
    TokenFactoryClient,
    load_models_config,
)
from app.models.pricing import Pricing
from app.models.router import resolve_roles
from app.orchestrator.budget import Budget, load_budget_limits
from app.storage.store import Store

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
BASE = "https://tf.test/v1/"
SUPER = "nvidia/nemotron-3-super-120b-a12b"
ULTRA = "nvidia/Nemotron-3-Ultra-550b-a55b"
NANO = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"


def models_list() -> list[dict[str, Any]]:
    data: list[dict[str, Any]] = json.loads((FIXTURES / "models_list.json").read_text())["data"]
    return data


def config() -> ModelsConfig:
    cfg = load_models_config(ROOT / "config" / "models.yaml")
    return cfg.model_copy(update={"provider": cfg.provider.model_copy(update={"base_url": BASE})})


def completion(
    content: str | None = "ok",
    *,
    model: str = SUPER,
    tool_calls: list[dict[str, Any]] | None = None,
    tokens: tuple[int, int] = (10, 5),
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = [
            {
                "id": c.get("id", f"call_{i}"),
                "type": "function",
                "function": {"name": c["name"], "arguments": c["arguments"]},
            }
            for i, c in enumerate(tool_calls)
        ]
    body = {
        "id": "cmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": tokens[0],
            "completion_tokens": tokens[1],
            "total_tokens": sum(tokens),
        },
    }
    return httpx.Response(200, json=body, headers=headers or {})


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
def sleeps() -> Sleeps:
    return Sleeps()


@pytest.fixture
async def client(store: Store, sleeps: Sleeps) -> TokenFactoryClient:
    cfg = config()
    limits = load_budget_limits(ROOT / "config" / "budgets.yaml")
    return TokenFactoryClient(
        cfg,
        resolve_roles(models_list(), cfg),
        Pricing.load(FIXTURES / "pricing_test.yaml"),
        Budget(store, limits, env="test"),
        StoreCallRecorder(store),
        api_key=SecretStr("test-key-not-real"),
        sleep=sleeps,
    )


ALL_ROLES = list(Role)

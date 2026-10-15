"""App fixtures: real Store/Budget on a temp DB, a recording orchestrator, HTTPS test client."""

import hashlib
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from app.api.main import AppState, create_app
from app.orchestrator.budget import Budget, load_budget_limits
from app.settings import Settings
from app.storage.store import Store

ROOT = Path(__file__).resolve().parents[2]
ADMIN = "a" * 64
INVITE = "invite-code-123"


class RecordingSubmit:
    def __init__(self) -> None:
        self.run_ids: list[str] = []

    async def __call__(self, run_id: str) -> None:
        self.run_ids.append(run_id)


@pytest.fixture
def submitted() -> RecordingSubmit:
    return RecordingSubmit()


@pytest.fixture
async def state(store: Store, submitted: RecordingSubmit) -> AppState:
    settings = Settings(
        p2p_env="test", session_key=SecretStr("s" * 64), admin_token=SecretStr(ADMIN)
    )
    limits = load_budget_limits(ROOT / "config" / "budgets.yaml")
    await store.create_invite(hashlib.sha256(INVITE.encode()).hexdigest(), 3, None)
    return AppState(
        settings=settings,
        store=store,
        budget=Budget(store, limits, env="test"),
        limits=limits,
        submit=submitted,
        sse_poll_s=0.01,
    )


async def _client(state: AppState) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app(state), client=("203.0.113.7", 4321))
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
        yield client


@pytest.fixture
async def client(state: AppState) -> AsyncIterator[httpx.AsyncClient]:
    async for c in _client(state):
        yield c


@pytest.fixture
async def other_client(state: AppState) -> AsyncIterator[httpx.AsyncClient]:
    """A second browser (separate cookie jar) on the same app."""
    async for c in _client(state):
        yield c


async def login(client: httpx.AsyncClient, code: str = INVITE) -> httpx.Response:
    return await client.post("/api/session", json={"invite_code": code})

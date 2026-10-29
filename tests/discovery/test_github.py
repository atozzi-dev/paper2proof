"""What GitHub reports as a repository's size: read before a fetch job is billed for cloning a
repository the provisioner would refuse. Any doubt is "unknown", never a reason to skip."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
import respx

from app.discovery import github
from app.discovery.github import GITHUB_API, MAX_REPO_BYTES, api_url, repo_size_kib
from app.ingestion.arxiv import USER_AGENT

ROOT = Path(__file__).resolve().parents[2]
REPO = "https://github.com/catboost/catboost"
API = f"{GITHUB_API}/repos/catboost/catboost"


def _provision() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "provision", ROOT / "images/provisioner/provision.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["provision"] = module
    spec.loader.exec_module(module)
    return module


def test_the_cap_is_the_provisioners() -> None:
    assert _provision().MAX_REPO_BYTES == MAX_REPO_BYTES


def test_the_api_url_of_a_canonical_repository() -> None:
    assert api_url(REPO) == API


@pytest.mark.parametrize(
    "url",
    [
        "https://gitlab.com/catboost/catboost",
        "http://github.com/catboost/catboost",
        "https://github.com/catboost",
        "https://github.com/catboost/catboost/tree/master",
        "https://github.com/../catboost",
        "https://github.com/catboost/..",
        "https://github.com/catboost/catboost?x=1",
    ],
)
def test_anything_but_a_canonical_repository_has_no_api_url(url: str) -> None:
    assert api_url(url) is None


@respx.mock
async def test_the_size_is_read_without_credentials() -> None:
    route = respx.get(API).mock(return_value=httpx.Response(200, json={"size": 1_739_568}))
    assert await repo_size_kib(REPO) == 1_739_568
    request = route.calls[0].request
    assert request.headers["user-agent"] == USER_AGENT
    assert "authorization" not in request.headers


@respx.mock
async def test_a_renamed_repository_is_followed() -> None:
    moved = f"{GITHUB_API}/repositories/42"
    respx.get(API).mock(return_value=httpx.Response(301, headers={"location": moved}))
    respx.get(moved).mock(return_value=httpx.Response(200, json={"size": 7}))
    assert await repo_size_kib(REPO) == 7


@respx.mock
async def test_a_shared_client_is_used_and_left_open() -> None:
    respx.get(API).mock(return_value=httpx.Response(200, json={"size": 5}))
    async with httpx.AsyncClient() as http:
        assert await repo_size_kib(REPO, http) == 5
        assert not http.is_closed


@respx.mock
async def test_a_non_github_url_makes_no_request() -> None:
    route = respx.route()
    assert await repo_size_kib("https://gitlab.com/catboost/catboost") is None
    assert not route.called


@respx.mock
@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(404, json={"message": "Not Found"}),
        httpx.Response(403, json={"message": "API rate limit exceeded"}),
        httpx.Response(429),
        httpx.Response(500),
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json=[1, 2]),
        httpx.Response(200, json={"size": "big"}),
        httpx.Response(200, json={"size": True}),
        httpx.Response(200, json={"size": -1}),
        httpx.Response(200, json={}),
    ],
    ids=["404", "403", "429", "500", "malformed", "list", "str", "bool", "negative", "absent"],
)
async def test_any_doubt_is_an_unknown_size(answer: httpx.Response) -> None:
    respx.get(API).mock(return_value=answer)
    assert await repo_size_kib(REPO) is None


@respx.mock
@pytest.mark.parametrize("error", [httpx.ConnectTimeout("slow"), httpx.ConnectError("down")])
async def test_an_unreachable_api_is_an_unknown_size(error: Exception) -> None:
    respx.get(API).mock(side_effect=error)
    assert await repo_size_kib(REPO) is None


async def test_the_request_has_a_short_timeout() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.extensions["timeout"])
        return httpx.Response(200, json={"size": 1})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert await repo_size_kib(REPO, http) == 1
    assert seen and all(v == github.TIMEOUT_S for v in seen.values())

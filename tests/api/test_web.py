"""The built UI (web/dist) served by the API, after every API route, under the same CSP."""

import dataclasses
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from app.api.main import AppState, create_app


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>Paper2Proof</title>")
    (root / "assets" / "index-abc123.js").write_text("console.log(1)")
    (tmp_path / "secret.txt").write_text("outside the UI")
    return root


@pytest.fixture
async def web(state: AppState, dist: Path) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(dataclasses.replace(state, web_dist=dist))
    transport = httpx.ASGITransport(app=app, client=("203.0.113.7", 4321))
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
        yield client


async def test_the_ui_is_served_at_the_root_with_the_csp(web: httpx.AsyncClient) -> None:
    page = await web.get("/")
    assert page.status_code == 200 and "<title>Paper2Proof</title>" in page.text
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert page.headers["cache-control"] == "no-cache"  # a deploy changes it at once


async def test_hashed_assets_are_cached_for_good(web: httpx.AsyncClient) -> None:
    asset = await web.get("/assets/index-abc123.js")
    assert asset.status_code == 200
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"


async def test_api_routes_still_win(web: httpx.AsyncClient) -> None:
    assert (await web.get("/healthz")).json() == {"ok": True}
    assert (await web.get("/api/gallery")).headers["content-type"].startswith("application/json")


@pytest.mark.parametrize(
    "path", ["/../secret.txt", "/%2e%2e/secret.txt", "/assets/../../secret.txt"]
)
async def test_nothing_outside_the_ui_is_served(web: httpx.AsyncClient, path: str) -> None:
    response = await web.get(path)
    assert response.status_code == 404 or "outside the UI" not in response.text


async def test_without_a_build_the_root_is_not_found(client: httpx.AsyncClient) -> None:
    assert (await client.get("/")).status_code == 404

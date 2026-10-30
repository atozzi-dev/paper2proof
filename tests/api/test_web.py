"""The built UI (web/dist) served by the API, after every API route, under the same CSP."""

import dataclasses
import os
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from starlette.types import Message

from app.api.main import API_ROOTS, AppState, create_app


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>Paper2Proof</title>")
    (root / "assets" / "index-abc123.js").write_text("console.log(1)")
    (root / "assets" / "inter-latin-wght-normal-abc123.woff2").write_bytes(b"wOF2")
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


async def test_fonts_are_served_as_fonts_whatever_the_os_mime_table(
    web: httpx.AsyncClient,
) -> None:
    font = await web.get("/assets/inter-latin-wght-normal-abc123.woff2")
    assert font.status_code == 200
    assert font.headers["content-type"] == "font/woff2"  # nosniff: never text/plain


async def test_api_routes_still_win(web: httpx.AsyncClient) -> None:
    assert (await web.get("/healthz")).json() == {"ok": True}
    assert (await web.get("/api/gallery")).headers["content-type"].startswith("application/json")


async def test_revalidated_and_ranged_assets_stay_immutable(web: httpx.AsyncClient) -> None:
    first = await web.get("/assets/index-abc123.js")
    again = await web.get(
        "/assets/index-abc123.js", headers={"if-none-match": first.headers["etag"]}
    )
    ranged = await web.get("/assets/index-abc123.js", headers={"range": "bytes=0-3"})
    assert (again.status_code, ranged.status_code) == (304, 206)
    for response in (again, ranged):
        assert response.headers["cache-control"] == "public, max-age=31536000, immutable"


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        ("POST", "/healthz", 405),
        ("GET", "/admin/drain", 405),
        ("HEAD", "/api/gallery", 405),
        ("GET", "/api/nothing-here", 404),
    ],
)
async def test_api_paths_never_fall_through_to_the_ui(
    web: httpx.AsyncClient, method: str, path: str, status: int
) -> None:
    response = await web.request(method, path)
    assert response.status_code == status
    assert "cache-control" not in response.headers  # an API error, not a static file
    assert "default-src 'self'" in response.headers["content-security-policy"]


@pytest.mark.parametrize("path", ["/.", "/assets", "/assets/.."])
async def test_directories_are_not_found_never_redirected(
    web: httpx.AsyncClient, path: str
) -> None:
    response = await _raw(web, path)
    assert response.status_code == 404
    assert "location" not in response.headers


@pytest.mark.parametrize(
    "path",
    ["/../secret.txt", "/%2e%2e/secret.txt", "/assets/../../secret.txt", r"/..\secret.txt"],
)
async def test_nothing_outside_the_ui_is_served(web: httpx.AsyncClient, path: str) -> None:
    response = await _raw(web, path)
    assert response.status_code == 404
    assert "outside the UI" not in response.text
    assert "default-src 'self'" in response.headers["content-security-policy"]


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
async def test_a_link_out_of_the_ui_is_not_followed(web: httpx.AsyncClient, dist: Path) -> None:
    (dist / "leak.txt").symlink_to(dist.parent / "secret.txt")
    response = await web.get("/leak.txt")
    assert response.status_code == 404 and "outside the UI" not in response.text


def test_every_api_path_is_kept_from_the_ui(state: AppState) -> None:
    paths = create_app(state).openapi()["paths"]
    assert {path.split("/")[1] for path in paths} <= API_ROOTS


async def _raw(web: httpx.AsyncClient, path: str) -> httpx.Response:
    """The path exactly as a server receives it (httpx normalizes dot segments away)."""
    app = web._transport.app  # type: ignore[attr-defined]
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
        "scheme": "https", "path": path, "raw_path": path.encode(), "root_path": "",
        "query_string": b"", "headers": [(b"host", b"testserver")],
        "client": ("203.0.113.7", 4321), "server": ("testserver", 443),
    }  # fmt: skip
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    headers = [(k.decode(), v.decode()) for k, v in start["headers"]]
    return httpx.Response(start["status"], headers=headers, content=body)


async def test_without_a_build_the_root_is_not_found(client: httpx.AsyncClient) -> None:
    assert (await client.get("/")).status_code == 404

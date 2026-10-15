import httpx
import pytest

HEADERS = {
    "content-security-policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; frame-ancestors 'none'"
    ),
    "strict-transport-security": "max-age=31536000",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
}


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/healthz"),
        ("GET", "/readyz"),
        ("GET", "/api/gallery"),
        ("GET", "/api/stats"),
        ("GET", "/api/runs/r_00000000000000000000000000"),
        ("POST", "/api/runs"),
        ("POST", "/admin/drain"),
        ("GET", "/does-not-exist"),
    ],
)
async def test_every_response_has_the_security_headers(
    client: httpx.AsyncClient, method: str, path: str
) -> None:
    response = await client.request(method, path)
    for name, value in HEADERS.items():
        assert response.headers.get(name) == value, (path, name)


async def test_health(client: httpx.AsyncClient) -> None:
    assert (await client.get("/healthz")).json() == {"ok": True}


async def test_readyz_reports_failing_checks(client: httpx.AsyncClient) -> None:
    response = await client.get("/readyz")
    assert response.status_code == 503  # model roles not resolved in tests
    assert response.json()["checks"]["db"] is True
    assert response.json()["checks"]["models"] is False


async def test_stats_are_public(client: httpx.AsyncClient) -> None:
    body = (await client.get("/api/stats")).json()
    assert body["runs_by_verdict"] == {}


async def test_unhandled_errors_are_500_with_headers_and_no_trace(state: object) -> None:
    from app.api.main import AppState, create_app

    assert isinstance(state, AppState)
    app = create_app(state)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("secret detail /etc/x")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
        response = await c.get("/boom")
    assert response.status_code == 500
    assert "secret detail" not in response.text
    for name, value in HEADERS.items():
        assert response.headers.get(name) == value

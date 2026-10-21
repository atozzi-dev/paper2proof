import httpx
import pytest

HEADERS = {
    "content-security-policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; "
        "object-src 'none'"
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


async def _origin_status(public_https: bool, origin: str, **extra: str) -> int:
    """POST through OriginCheckMiddleware as the endpoint's proxy delivers it: plain http."""
    from starlette.responses import PlainTextResponse

    from app.api.security_headers import OriginCheckMiddleware

    async def ok(scope: object, receive: object, send: object) -> None:
        await PlainTextResponse("ok")(scope, receive, send)  # type: ignore[arg-type]

    app = OriginCheckMiddleware(ok, public_https=public_https)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://p2p.endpoints.example") as c:
        response = await c.post("/api/session", headers={"Origin": origin, **extra})
    return response.status_code


async def test_behind_the_tls_proxy_the_browser_origin_is_https() -> None:
    """uvicorn sees http (the proxy terminates TLS); the browser sends Origin: https://<host>."""
    assert await _origin_status(True, "https://p2p.endpoints.example") == 200
    assert await _origin_status(True, "https://evil.example") == 403
    assert await _origin_status(True, "http://p2p.endpoints.example") == 403  # prod is https only


async def test_a_forwarded_host_from_the_proxy_is_accepted() -> None:
    headers = {"X-Forwarded-Host": "p2p.public.example"}
    assert await _origin_status(True, "https://p2p.public.example", **headers) == 200
    assert await _origin_status(True, "https://evil.example", **headers) == 403


async def test_outside_prod_the_scheme_of_the_request_counts() -> None:
    assert await _origin_status(False, "http://p2p.endpoints.example") == 200
    assert await _origin_status(False, "https://p2p.endpoints.example") == 403


def test_prod_builds_the_api_with_the_https_origin_check(state: object) -> None:
    from app.api.main import AppState, create_app

    assert isinstance(state, AppState)
    prod = state.settings.model_copy(update={"p2p_env": "prod"})
    app = create_app(AppState(**{**state.__dict__, "settings": prod}))
    checks = [m for m in app.user_middleware if m.cls.__name__ == "OriginCheckMiddleware"]
    assert [m.kwargs for m in checks] == [{"public_https": True}]


async def test_a_forwarded_host_counts_only_behind_the_proxy() -> None:
    headers = {"X-Forwarded-Host": "p2p.public.example"}
    assert await _origin_status(False, "http://p2p.public.example", **headers) == 403


async def test_a_proxy_list_or_a_default_port_still_matches() -> None:
    listed = {"X-Forwarded-Host": "p2p.public.example, internal.example"}
    assert await _origin_status(True, "https://p2p.public.example", **listed) == 200
    assert (
        await _origin_status(True, "https://p2p.public.example", Host="p2p.public.example:443")
        == 200
    )


async def test_a_null_origin_is_refused() -> None:
    assert await _origin_status(True, "null") == 403


async def test_without_origin_a_cross_site_fetch_is_refused_but_tools_pass() -> None:
    from starlette.responses import PlainTextResponse

    from app.api.security_headers import OriginCheckMiddleware

    async def ok(scope: object, receive: object, send: object) -> None:
        await PlainTextResponse("ok")(scope, receive, send)  # type: ignore[arg-type]

    transport = httpx.ASGITransport(app=OriginCheckMiddleware(ok, public_https=True))
    async with httpx.AsyncClient(transport=transport, base_url="http://h.example") as c:
        cross = await c.post("/api/session", headers={"Sec-Fetch-Site": "cross-site"})
        same = await c.post("/api/session", headers={"Sec-Fetch-Site": "same-origin"})
        tool = await c.post("/api/session")  # admin CLI, probes: no Origin, no Sec-Fetch-*
    assert (cross.status_code, same.status_code, tool.status_code) == (403, 200, 200)


def test_outside_prod_the_api_keeps_the_request_scheme(state: object) -> None:
    from app.api.main import AppState, create_app

    assert isinstance(state, AppState)
    app = create_app(state)
    checks = [m for m in app.user_middleware if m.cls.__name__ == "OriginCheckMiddleware"]
    assert [m.kwargs for m in checks] == [{"public_https": False}]


async def test_no_cors_preflight_is_ever_allowed(client: httpx.AsyncClient) -> None:
    """X-Forwarded-Host is safe to trust only while no cross-origin request may set headers."""
    response = await client.options(
        "/api/runs",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in response.headers


async def test_a_trailing_slash_is_not_redirected(client: httpx.AsyncClient) -> None:
    """A slash redirect is built from the scope: behind the proxy that is an http:// link."""
    response = await client.get("/api/runs/")
    assert response.status_code != 307 and "location" not in response.headers

"""Pure ASGI middleware (streams included):
- SecurityHeadersMiddleware: the four security headers on every response, including the
  generic 500 it sends itself for unhandled errors (Starlette's ServerErrorMiddleware sits
  outside user middleware, so its 500s would otherwise carry no headers).
- BodySizeLimitMiddleware: 413 above MAX_BODY_BYTES.
- OriginCheckMiddleware: unsafe methods with a foreign Origin get 403 (CSRF defence in depth
  on top of SameSite=Strict, for sibling subdomains of the managed endpoint domain).
"""

import json
import logging

from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger(__name__)
MAX_BODY_BYTES = 64 * 1024
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (
        b"content-security-policy",
        b"default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        b"connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; "
        b"object-src 'none'",
    ),
    (b"strict-transport-security", b"max-age=31536000"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
)
_NAMES = frozenset(name for name, _ in SECURITY_HEADERS)


async def send_json(send: Send, status: int, body: dict[str, str]) -> None:
    payload = json.dumps(body).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(payload)).encode()),
    ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": payload})


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def send_with_headers(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() not in _NAMES]
                message = {**message, "headers": [*headers, *SECURITY_HEADERS]}
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        except Exception:
            log.exception("unhandled error", extra={"path": scope.get("path", "")})
            if started:
                raise
            await send_json(send_with_headers, 500, {"error": "internal error"})


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = dict(scope.get("headers", [])).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            await send_json(send, 413, {"error": "request body too large"})
            return
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise ValueError("request body too large")
            return message

        await self.app(scope, limited_receive, send)


class OriginCheckMiddleware:
    """A browser's unsafe request must come from our own origin. In prod the endpoint's proxy
    terminates TLS and uvicorn sees plain http, so the public scheme is https whatever the
    request scope says; the host is Host, or X-Forwarded-Host when the proxy rewrites it (a
    cross-site page cannot set that header on a simple request)."""

    def __init__(self, app: ASGIApp, *, public_https: bool = False) -> None:
        self.app = app
        self.public_https = public_https

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] in UNSAFE_METHODS:
            headers = dict(scope.get("headers", []))
            origin = headers.get(b"origin")
            scheme = b"https" if self.public_https else scope.get("scheme", "https").encode()
            hosts = {headers.get(b"host", b""), headers.get(b"x-forwarded-host", b"")} - {b""}
            if origin is not None and origin not in {scheme + b"://" + h for h in hosts}:
                await send_json(send, 403, {"error": "cross-origin request refused"})
                return
        await self.app(scope, receive, send)

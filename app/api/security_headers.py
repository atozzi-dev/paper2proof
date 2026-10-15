"""Pure ASGI middleware: the four security headers on every HTTP response (streams included)."""

from starlette.types import ASGIApp, Message, Receive, Scope, Send

SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (
        b"content-security-policy",
        b"default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        b"connect-src 'self'; frame-ancestors 'none'",
    ),
    (b"strict-transport-security", b"max-age=31536000"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                names = {SECURITY_HEADERS[i][0] for i in range(len(SECURITY_HEADERS))}
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() not in names]
                message = {**message, "headers": [*headers, *SECURITY_HEADERS]}
            await send(message)

        await self.app(scope, receive, send_with_headers)

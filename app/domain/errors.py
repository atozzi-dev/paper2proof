"""Exceptions that carry a machine-readable ErrorCode (FSM error_code, SSE `error` event)."""

from app.domain.models import ErrorCode


class P2PError(Exception):
    """Base for expected failures. `message` is user-visible: never put secrets or traces in it."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message

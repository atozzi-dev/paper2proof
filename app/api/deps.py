"""Shared state and request dependencies (session cookie, client IP, rate limits)."""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel

from app.api.ratelimit import RateLimiter
from app.bundle.store import BundleStore
from app.orchestrator.budget import Budget, BudgetLimits
from app.settings import Settings
from app.storage.store import RunRecord, Store

SESSION_COOKIE = "__Host-p2p_session"  # __Host-: Secure, Path=/, no Domain (no sibling planting)
SESSION_MAX_AGE_S = 7 * 24 * 3600
API_PER_MINUTE = 60
SESSION_PER_MINUTE = 5
RUN_ID = re.compile(r"^r_[0-9a-z]{26}$")


@dataclass
class AppState:
    settings: Settings
    store: Store
    budget: Budget
    limits: BudgetLimits
    submit: Callable[[str], Awaitable[object]]  # Orchestrator.submit
    bundles: BundleStore | None = None  # None: bundles are not served (404)
    models_ready: bool = False
    replication_ready: bool = True  # set by the prod start script check (Litestream)
    draining: bool = False
    web_dist: Path | None = None  # the built UI (web/dist), served after every API route
    sse_poll_s: float = 0.5
    sse_heartbeat_s: float = 15.0
    sse_streams_per_session: int = 2
    sse_streams_total: int = 50
    limiter: RateLimiter = field(default_factory=RateLimiter)
    open_streams: dict[str, int] = field(default_factory=dict)


class Session(BaseModel):
    sid: str  # random per login; owns the runs it creates
    invite: str  # sha256 of the invite code (quota)


def get_state(request: Request) -> AppState:
    state: AppState = request.app.state.p2p
    return state


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def serializer(state: AppState) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(state.settings.session_key.get_secret_value(), salt="p2p-session")


def read_session(request: Request) -> Session | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    try:
        data = serializer(get_state(request)).loads(token, max_age=SESSION_MAX_AGE_S)
        return Session.model_validate(data)
    except (BadSignature, ValueError):
        return None


def require_session(request: Request) -> Session:
    session = read_session(request)
    if session is None:
        raise HTTPException(401, "a valid session is required")
    return session


def api_rate_limit(request: Request) -> None:
    if not get_state(request).limiter.allow("api", client_ip(request), API_PER_MINUTE):
        raise HTTPException(429, "too many requests")


async def visible_run(request: Request, run_id: str, *, owner_only: bool = False) -> RunRecord:
    """Gallery runs are public; any other run is visible only to the session that created it.
    Everyone else gets 404 (never 403), so run IDs can't be probed."""
    if not RUN_ID.fullmatch(run_id):
        raise HTTPException(404, "not found")
    run = await get_state(request).store.get_run(run_id)
    session = read_session(request)
    is_owner = run is not None and session is not None and run.created_by == session.sid
    if run is None or not (is_owner or (run.listed and not owner_only)):
        raise HTTPException(404, "not found")
    return run

"""Admin endpoints, authenticated with X-Admin-Token compared in constant time.

Not part of the public OpenAPI schema. `python -m app.admin_cli` calls them
(docs/OPERATIONS.md)."""

import hashlib
import hmac
import secrets
from datetime import UTC, date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from app.api.deps import RUN_ID, client_ip, get_state
from app.domain.models import RunStatus, VerdictKind
from app.settings import MIN_SECRET_LEN

DEFAULT_QUOTA = 3
ADMIN_FAILURES_PER_MINUTE = 10
INVITE_ID_CHARS = 12  # the listed id: a prefix of the code's sha256, never the code
SUMMARY_MAX_CHARS = 64  # runs.verdict_summary


def require_admin(request: Request, x_admin_token: Annotated[str | None, Header()] = None) -> None:
    state = get_state(request)
    expected = state.settings.admin_token.get_secret_value().encode()
    given = (x_admin_token or "").encode()
    # Always compare (constant time), and never accept an empty or short configured token.
    ok = hmac.compare_digest(given, expected) and len(expected) >= MIN_SECRET_LEN
    if not ok:
        if not state.limiter.allow("admin-fail", client_ip(request), ADMIN_FAILURES_PER_MINUTE):
            raise HTTPException(429, "too many requests")
        raise HTTPException(401, "admin token required")


# include_in_schema=False: the public /api/openapi.json never advertises the admin surface.
router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)], include_in_schema=False)


class Toggle(BaseModel):
    on: bool


class Listing(BaseModel):
    listed: bool
    # Architecture 16.1: a NOT_REPRODUCED run is listed only after a human review of it.
    reviewed: bool = False


class InviteRequest(BaseModel):
    quota_per_day: int = Field(default=DEFAULT_QUOTA, ge=1, le=100)
    expires_at: datetime | None = None


class InviteCreated(BaseModel):
    code: str  # shown once: only its sha256 is stored


class InviteView(BaseModel):
    id: str
    quota_per_day: int
    used_today: int
    used_on: date | None
    expires_at: datetime | None
    active: bool


class RevokeInvite(BaseModel):
    id: str = Field(pattern=r"^[0-9a-f]{12,64}$")  # a prefix of the code's sha256


class AdminRun(BaseModel):
    id: str
    arxiv_id: str
    status: RunStatus
    listed: bool
    created_at: datetime
    verdicts: list[VerdictKind]


@router.post("/drain", status_code=204)
async def drain(body: Toggle, request: Request) -> Response:
    get_state(request).draining = body.on
    return Response(status_code=204)


@router.post("/kill-switch", status_code=204)
async def kill_switch(body: Toggle, request: Request) -> Response:
    await get_state(request).budget.set_kill_switch(body.on)
    return Response(status_code=204)


def verdict_summary(kinds: list[VerdictKind]) -> str:
    """The gallery card's line: the distinct verdict kinds in claim order (the UI shows each
    with its copy, e.g. "Not reproduced under our conditions")."""
    summary = ""
    for kind in dict.fromkeys(kinds):
        candidate = f"{summary}, {kind.value}" if summary else kind.value
        if len(candidate) > SUMMARY_MAX_CHARS:
            break
        summary = candidate
    return summary


@router.get("/runs")
async def list_runs(
    request: Request,
    listed: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[AdminRun]:
    """Curation candidates, newest first, with their verdict kinds."""
    store = get_state(request).store
    return [
        AdminRun(
            id=run.id,
            arxiv_id=run.arxiv_id,
            status=run.status,
            listed=run.listed,
            created_at=run.created_at,
            verdicts=await store.verdict_kinds(run.id),
        )
        for run in await store.list_runs(limit=limit, listed=listed)
    ]


@router.post("/gallery/{run_id}", status_code=204)
async def curate(run_id: str, body: Listing, request: Request) -> Response:
    """List a finished run in the public gallery, or take one down. Nothing is ever listed
    automatically, a run still in progress is never listed, and a NOT_REPRODUCED verdict needs
    `reviewed: true`. Unlisting always succeeds."""
    store = get_state(request).store
    run = await store.get_run(run_id) if RUN_ID.fullmatch(run_id) else None
    if run is None:
        raise HTTPException(404, "not found")
    summary = None
    if body.listed:
        if not run.status.is_terminal:
            raise HTTPException(409, "the run has not finished: only finished runs are listed")
        kinds = await store.verdict_kinds(run_id)
        if VerdictKind.NOT_REPRODUCED in kinds and not body.reviewed:
            raise HTTPException(
                409, "a NOT_REPRODUCED verdict is listed only after a review: send reviewed: true"
            )
        summary = verdict_summary(kinds) or None
    if not await store.set_listed(run_id, body.listed, verdict_summary=summary):
        raise HTTPException(404, "not found")
    return Response(status_code=204)


@router.post("/invites", status_code=201)
async def create_invite(body: InviteRequest, request: Request) -> InviteCreated:
    code = secrets.token_urlsafe(18)
    digest = hashlib.sha256(code.encode()).hexdigest()
    await get_state(request).store.create_invite(digest, body.quota_per_day, body.expires_at)
    return InviteCreated(code=code)


@router.get("/invites")
async def list_invites(request: Request) -> list[InviteView]:
    now = datetime.now(UTC)
    return [
        InviteView(
            id=invite.code_hash[:INVITE_ID_CHARS],
            quota_per_day=invite.quota_per_day,
            used_today=invite.used_today,
            used_on=invite.used_on,
            expires_at=invite.expires_at,
            active=invite.active(now),
        )
        for invite in await get_state(request).store.list_invites()
    ]


@router.post("/invites/revoke", status_code=204)
async def revoke_invite(body: RevokeInvite, request: Request) -> Response:
    """The invite expires now: no new session and no new run (existing sessions included)."""
    matches = await get_state(request).store.revoke_invite(body.id, datetime.now(UTC))
    if matches == 0:
        raise HTTPException(404, "no invite with this id")
    if matches > 1:
        raise HTTPException(409, "several invites match: give more characters of the id")
    return Response(status_code=204)

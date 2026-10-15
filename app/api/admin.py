"""Admin endpoints, authenticated with X-Admin-Token compared in constant time."""

import hashlib
import hmac
import secrets
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.api.deps import get_state

DEFAULT_QUOTA = 3


def require_admin(request: Request, x_admin_token: Annotated[str | None, Header()] = None) -> None:
    expected = get_state(request).settings.admin_token.get_secret_value().encode()
    given = (x_admin_token or "").encode()
    if not hmac.compare_digest(given, expected):
        raise HTTPException(401, "admin token required")


router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


class Toggle(BaseModel):
    on: bool


class Listing(BaseModel):
    listed: bool


class InviteRequest(BaseModel):
    quota_per_day: int = Field(default=DEFAULT_QUOTA, ge=1, le=100)
    expires_at: datetime | None = None


class InviteCreated(BaseModel):
    code: str  # shown once: only its sha256 is stored


@router.post("/drain", status_code=204)
async def drain(body: Toggle, request: Request) -> Response:
    get_state(request).draining = body.on
    return Response(status_code=204)


@router.post("/kill-switch", status_code=204)
async def kill_switch(body: Toggle, request: Request) -> Response:
    await get_state(request).budget.set_kill_switch(body.on)
    return Response(status_code=204)


@router.post("/gallery/{run_id}", status_code=204)
async def curate(run_id: str, body: Listing, request: Request) -> Response:
    if not await get_state(request).store.set_listed(run_id, body.listed):
        raise HTTPException(404, "not found")
    return Response(status_code=204)


@router.post("/invites", status_code=201)
async def create_invite(body: InviteRequest, request: Request) -> InviteCreated:
    code = secrets.token_urlsafe(18)
    digest = hashlib.sha256(code.encode()).hexdigest()
    await get_state(request).store.create_invite(digest, body.quota_per_day, body.expires_at)
    return InviteCreated(code=code)

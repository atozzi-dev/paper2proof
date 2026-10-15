"""POST /api/session: invite code -> signed, HttpOnly, Secure, SameSite=Strict cookie."""

import hashlib
import secrets
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.api.deps import (
    SESSION_COOKIE,
    SESSION_MAX_AGE_S,
    SESSION_PER_MINUTE,
    Session,
    client_ip,
    get_state,
    serializer,
)

router = APIRouter()


class SessionRequest(BaseModel):
    invite_code: str = Field(min_length=1, max_length=128)


def invite_hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


@router.post("/api/session", status_code=204)
async def create_session(body: SessionRequest, request: Request) -> Response:
    state = get_state(request)
    if not state.limiter.allow("session", client_ip(request), SESSION_PER_MINUTE):
        raise HTTPException(429, "too many requests")
    code_hash = invite_hash(body.invite_code)
    if not await state.store.invite_valid(code_hash, datetime.now(UTC)):
        raise HTTPException(401, "invalid invite code")
    session = Session(sid=secrets.token_hex(16), invite=code_hash)
    token = serializer(state).dumps(session.model_dump())
    response = Response(status_code=204)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_MAX_AGE_S,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )
    return response

"""Run endpoints (docs/specs/api-and-events.md)."""

import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.deps import (
    Session,
    api_rate_limit,
    client_ip,
    get_state,
    read_session,
    require_session,
    visible_run,
)
from app.api.sse import event_stream, replay_stream
from app.domain.ids import new_run_id
from app.domain.models import Claim, ErrorCode, RunStatus, StepName, Verdict
from app.storage.store import StepRecord

router = APIRouter(dependencies=[Depends(api_rate_limit)])
ARXIV_ID = re.compile(r"^\d{4}\.\d{4,5}(v\d+)?$")  # invariant 11: arXiv IDs only
DRAIN_RETRY_AFTER_S = 120


class CreateRun(BaseModel):
    arxiv_id: str = Field(max_length=32)
    claim_hint: str | None = Field(default=None, max_length=500)


class Created(BaseModel):
    run_id: str
    events_url: str


class ClaimView(BaseModel):
    claim: Claim
    selected: bool


class VerdictView(BaseModel):
    claim_id: str
    verdict: Verdict


class RunView(BaseModel):
    id: str
    arxiv_id: str
    status: RunStatus
    error_code: ErrorCode | None
    created_at: datetime
    finished_at: datetime | None
    listed: bool
    approval: str | None
    steps: list[StepRecord]
    claims: list[ClaimView]
    verdicts: list[VerdictView]


@router.post("/api/runs", status_code=202)
async def create_run(
    body: CreateRun, request: Request, session: Annotated[Session, Depends(require_session)]
) -> Created:
    state = get_state(request)
    if not ARXIV_ID.fullmatch(body.arxiv_id):
        raise HTTPException(400, "arxiv_id must look like 2410.01234 or 2410.01234v2")
    if await state.budget.kill_switch_active():
        raise HTTPException(402, "daily budget reached: new runs are paused")
    if state.draining:
        raise HTTPException(
            503, "deploy in progress", headers={"Retry-After": str(DRAIN_RETRY_AFTER_S)}
        )
    if not await state.store.consume_invite(session.invite, datetime.now(UTC)):
        raise HTTPException(429, "daily quota for this invite reached")
    run_id = new_run_id()
    await state.store.create_run(
        run_id,
        body.arxiv_id,
        created_by=session.sid,
        claim_hint=body.claim_hint,
        budget=state.limits.per_run.model_dump(),
    )
    await state.submit(run_id)
    return Created(run_id=run_id, events_url=f"/api/runs/{run_id}/events")


@router.get("/api/runs/{run_id}")
async def get_run(run_id: str, request: Request) -> RunView:
    run = await visible_run(request, run_id)
    store = get_state(request).store
    return RunView(
        id=run.id,
        arxiv_id=run.arxiv_id,
        status=run.status,
        error_code=run.error_code,
        created_at=run.created_at,
        finished_at=run.finished_at,
        listed=run.listed,
        approval=run.approval,
        steps=await store.get_steps(run_id),
        claims=[ClaimView(claim=c, selected=s) for c, s in await store.get_claims(run_id)],
        verdicts=[VerdictView(claim_id=c, verdict=v) for c, v in await store.list_verdicts(run_id)],
    )


@router.get("/api/runs/{run_id}/events")
async def events(
    run_id: str,
    request: Request,
    last_event_id: Annotated[str | None, Header()] = None,
    after: Annotated[int, Query(ge=0)] = 0,
    replay: Annotated[int, Query(ge=0, le=1)] = 0,
    speed: Annotated[float, Query(gt=0, le=10_000)] = 10,
) -> StreamingResponse:
    state = get_state(request)
    await visible_run(request, run_id)
    session = read_session(request)
    key = session.sid if session else f"ip:{client_ip(request)}"
    total = sum(state.open_streams.values())
    if (
        state.open_streams.get(key, 0) >= state.sse_streams_per_session
        or total >= state.sse_streams_total
    ):
        raise HTTPException(429, "too many open streams: use events.json polling")
    start = int(last_event_id) if last_event_id and last_event_id.isdigit() else after

    async def finished() -> bool:
        run = await state.store.get_run(run_id)
        return run is None or run.status.is_terminal

    async def body() -> AsyncIterator[bytes]:
        state.open_streams[key] = state.open_streams.get(key, 0) + 1
        try:
            if replay:
                stream = replay_stream(state.store, run_id, speed=speed)
            else:
                stream = event_stream(
                    state.store,
                    run_id,
                    after_id=start,
                    poll_s=state.sse_poll_s,
                    heartbeat_s=state.sse_heartbeat_s,
                    finished=finished,
                )
            async for chunk in stream:
                yield chunk
        finally:
            state.open_streams[key] -= 1
            if state.open_streams[key] <= 0:
                state.open_streams.pop(key, None)

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return StreamingResponse(body(), media_type="text/event-stream", headers=headers)


@router.get("/api/runs/{run_id}/events.json")
async def events_json(
    run_id: str, request: Request, after: Annotated[int, Query(ge=0)] = 0
) -> dict[str, list[dict[str, object]]]:
    await visible_run(request, run_id)
    stored = await get_state(request).store.events_after(run_id, after)
    return {"events": [e.model_dump(mode="json") for e in stored]}


async def _awaiting_approval(request: Request, run_id: str) -> bool:
    steps = await get_state(request).store.get_steps(run_id)
    return any(s.name is StepName.AWAITING_APPROVAL and s.status == "RUNNING" for s in steps)


async def _decide(request: Request, run_id: str, decision: str) -> Response:
    await visible_run(request, run_id, owner_only=True)
    if not await _awaiting_approval(request, run_id):
        raise HTTPException(409, "run is not awaiting approval")
    await get_state(request).store.set_approval(run_id, decision)
    return Response(status_code=204)


@router.post("/api/runs/{run_id}/approve", status_code=204)
async def approve(run_id: str, request: Request) -> Response:
    return await _decide(request, run_id, "approved")


@router.post("/api/runs/{run_id}/reject", status_code=204)
async def reject(run_id: str, request: Request) -> Response:
    return await _decide(request, run_id, "rejected")


@router.post("/api/runs/{run_id}/cancel", status_code=202)
async def cancel(run_id: str, request: Request) -> Response:
    await visible_run(request, run_id, owner_only=True)
    await get_state(request).store.cancel_run(run_id)
    return Response(status_code=202)


@router.get("/api/runs/{run_id}/bundle")
async def bundle(run_id: str, request: Request) -> Response:
    await visible_run(request, run_id)
    raise HTTPException(404, "no bundle for this run yet")  # presigned redirect lands in A-26

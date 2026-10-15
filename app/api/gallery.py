"""Public, curated gallery and aggregate stats (no login)."""

from datetime import datetime

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.api.deps import api_rate_limit, get_state
from app.domain.models import RunStatus

router = APIRouter(dependencies=[Depends(api_rate_limit)])


class RunCard(BaseModel):
    id: str
    arxiv_id: str
    status: RunStatus
    verdict_summary: str | None
    created_at: datetime


class Stats(BaseModel):
    runs_by_verdict: dict[str, int]


@router.get("/api/gallery")
async def gallery(request: Request) -> list[RunCard]:
    runs = await get_state(request).store.list_gallery()
    return [
        RunCard(
            id=r.id,
            arxiv_id=r.arxiv_id,
            status=r.status,
            verdict_summary=r.verdict_summary,
            created_at=r.created_at,
        )
        for r in runs
    ]


@router.get("/api/stats")
async def stats(request: Request) -> Stats:
    return Stats(runs_by_verdict=await get_state(request).store.verdict_counts())

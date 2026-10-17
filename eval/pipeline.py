"""The full pipeline as the eval sees it (sets A and B): one run per arXiv ID, injected so the
runners are tested on fakes. `HttpPipeline` drives a deployed Paper2Proof API the way a user
does (invite session, POST /api/runs, polling), so the eval measures what is deployed and every
model call and job goes through that deployment's budget and ledger (invariant 7)."""

import argparse
import asyncio
import json
import os
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr

from app.api.runs import EventsPage, RunView
from app.domain.events import CostUpdatePayload
from app.domain.ids import RUN_ID_PATTERN
from app.domain.models import Claim, ErrorCode, RunStatus, StepName, Verdict
from eval.results import RunInfo

RUN_ID = re.compile(RUN_ID_PATTERN)
ARXIV_ID = re.compile(r"^\d{4}\.\d{4,5}(v\d+)?$")  # invariant 11


class PipelineClaim(BaseModel):
    model_config = ConfigDict(frozen=True)

    claim: Claim
    selected: bool


class PipelineRun(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    arxiv_id: str
    status: RunStatus
    error_code: ErrorCode | None
    claims: list[PipelineClaim]
    verdicts: dict[str, Verdict]  # claim_id -> verdict
    cost_usd: float | None  # the run's last cost.update total
    duration_s: float | None


class Pipeline(Protocol):
    async def run(self, arxiv_id: str, *, claim_hint: str | None = None) -> PipelineRun: ...


class HttpPipeline:
    def __init__(
        self,
        http: httpx.AsyncClient,
        invite_code: SecretStr,
        *,
        approve: bool,
        poll_s: float = 15.0,
        timeout_s: float = 3 * 3600.0,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        """`http` has the API's base_url. `approve`: approve plans that wait for approval
        (they cost more than the auto-approve threshold); otherwise reject them."""
        self._http = http
        self._invite = invite_code
        self._approve = approve
        self._poll_s = poll_s
        self._timeout_s = timeout_s
        self._sleep = sleep or asyncio.sleep
        self._logged_in = False

    async def _login(self) -> None:
        if self._logged_in:
            return
        response = await self._http.post(
            "/api/session", json={"invite_code": self._invite.get_secret_value()}
        )
        response.raise_for_status()
        self._logged_in = True

    async def _view(self, run_id: str) -> RunView:
        response = await self._http.get(f"/api/runs/{run_id}")
        response.raise_for_status()
        return RunView.model_validate(response.json())

    @staticmethod
    def _awaiting(view: RunView) -> bool:
        last = view.steps[-1] if view.steps else None
        return (
            last is not None
            and last.name is StepName.AWAITING_APPROVAL
            and last.finished_at is None
            and view.approval is None
        )

    async def _cost(self, run_id: str) -> float | None:
        response = await self._http.get(f"/api/runs/{run_id}/events.json")
        response.raise_for_status()
        page = EventsPage.model_validate(response.json())
        totals = [
            e.payload.total_usd for e in page.events if isinstance(e.payload, CostUpdatePayload)
        ]
        return totals[-1] if totals else None

    async def run(self, arxiv_id: str, *, claim_hint: str | None = None) -> PipelineRun:
        if not ARXIV_ID.fullmatch(arxiv_id):
            raise ValueError(f"not an arXiv ID: {arxiv_id!r}")
        await self._login()
        created = await self._http.post(
            "/api/runs", json={"arxiv_id": arxiv_id, "claim_hint": claim_hint}
        )
        created.raise_for_status()
        run_id = str(created.json()["run_id"])
        if not RUN_ID.fullmatch(run_id):
            raise ValueError("the API returned a malformed run_id")
        waited = 0.0
        decided = False
        while True:
            view = await self._view(run_id)
            if view.status.is_terminal:
                break
            if not decided and self._awaiting(view):
                decision = "approve" if self._approve else "reject"
                (await self._http.post(f"/api/runs/{run_id}/{decision}")).raise_for_status()
                decided = True
            if waited >= self._timeout_s:
                await self._http.post(f"/api/runs/{run_id}/cancel")
                raise TimeoutError(f"run {run_id} did not finish in {self._timeout_s:.0f} s")
            await self._sleep(self._poll_s)
            waited += self._poll_s
        duration = (
            (view.finished_at - view.created_at).total_seconds() if view.finished_at else None
        )
        return PipelineRun(
            run_id=run_id,
            arxiv_id=arxiv_id,
            status=view.status,
            error_code=view.error_code,
            claims=[PipelineClaim(claim=c.claim, selected=c.selected) for c in view.claims],
            verdicts={v.claim_id: v.verdict for v in view.verdicts},
            cost_usd=await self._cost(run_id),
            duration_s=duration,
        )


# --- CLI plumbing shared by run_set_a and run_set_b ------------------------------------------

INVITE_ENV = "P2P_EVAL_INVITE"  # an invite code with a daily quota large enough for the set
_LOCAL = re.compile(r"^http://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?/?$")


def add_pipeline_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--api-url", required=True, help="the deployment, e.g. https://...")
    parser.add_argument(
        "--approve",
        action="store_true",
        help="approve plans that wait for approval (they cost more); default: reject them",
    )
    parser.add_argument(
        "--confirm-spend",
        action="store_true",
        help="required: every run spends real money on Token Factory and Nebius",
    )


def check_api_url(url: str) -> str:
    if url.startswith("https://") or _LOCAL.fullmatch(url):
        return url.rstrip("/")
    raise ValueError("--api-url must be https:// (or http://localhost for make dev)")


def invite_from_env() -> SecretStr:
    value = os.environ.get(INVITE_ENV, "")
    if not value:
        raise ValueError(f"set {INVITE_ENV} to an invite code (it is never printed)")
    return SecretStr(value)


def write_result(path: Path, result: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")


async def attempt(
    pipeline: Pipeline, arxiv_id: str, *, claim_hint: str | None = None
) -> tuple[RunInfo, PipelineRun | None]:
    """Run one paper; a harness failure (network, timeout) is recorded, not raised, so one bad
    paper never loses the rest of the set. Only the exception type is kept."""
    try:
        run = await pipeline.run(arxiv_id, claim_hint=claim_hint)
    except Exception as err:
        return RunInfo(run_id=None, status=None, error=type(err).__name__), None
    info = RunInfo(
        run_id=run.run_id,
        status=run.status,
        error_code=run.error_code,
        cost_usd=run.cost_usd,
        duration_s=run.duration_s,
    )
    return info, run

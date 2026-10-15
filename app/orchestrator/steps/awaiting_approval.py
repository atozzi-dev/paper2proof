"""AWAITING_APPROVAL: wait for the owner's decision (POST approve/reject) or time out.

The deadline is computed from this step's persisted start time, so a restart doesn't extend it.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from app.domain.events import EventType
from app.domain.models import ErrorCode, RunStatus, StepName
from app.orchestrator.context import RunContext
from app.orchestrator.fsm import StepResult


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AwaitingApprovalStep:
    name = StepName.AWAITING_APPROVAL

    def __init__(
        self,
        *,
        timeout: timedelta,
        poll_s: float = 2.0,
        clock: Callable[[], datetime] = _utcnow,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._timeout = timeout
        self._poll_s = poll_s
        self._clock = clock
        self._sleep = sleep

    async def run(self, ctx: RunContext) -> StepResult:
        steps = await ctx.store.get_steps(ctx.run_id)
        mine = [s for s in steps if s.name is self.name and s.attempt == ctx.attempt]
        started = mine[0].started_at if mine else self._clock()
        started = min(started, self._clock())  # tests may run on a fake clock in the past
        deadline = started + self._timeout
        await ctx.emit(EventType.APPROVAL_REQUIRED, {"expires_at": deadline.isoformat()})
        while True:
            run = await ctx.store.get_run(ctx.run_id)
            if run is None or run.status is not RunStatus.RUNNING:
                return StepResult(next=RunStatus.CANCELLED)
            if run.approval == "approved":
                return StepResult(next=StepName.PROVISIONING)
            if run.approval == "rejected":
                return StepResult(next=RunStatus.CANCELLED)
            if self._clock() >= deadline:
                return StepResult(next=RunStatus.CANCELLED, error_code=ErrorCode.APPROVAL_TIMEOUT)
            await self._sleep(self._poll_s)

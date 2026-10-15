"""What a step gets to work with. Steps never talk to the FSM directly."""

import asyncio
from collections.abc import Callable
from concurrent.futures import Executor
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from app.domain.events import EventType
from app.domain.models import StepName
from app.execution.backend import JobBackend
from app.orchestrator.budget import Budget, BudgetLimits
from app.storage.store import Store


@dataclass(frozen=True)
class RunContext:
    run_id: str
    step: StepName
    attempt: int  # idempotency key is (run_id, step, attempt)
    store: Store
    budget: Budget
    limits: BudgetLimits
    jobs: JobBackend
    cpu_pool: Executor | None

    async def emit(self, type: EventType, payload: BaseModel | dict[str, Any]) -> int:
        return await self.store.append_event(self.run_id, type, payload)

    async def run_cpu[T](
        self, fn: Callable[..., T], *args: Any, timeout_s: float | None = None
    ) -> T:
        """CPU-bound work (parsing, rasterizing, hashing) off the event loop (invariant 12).
        With `timeout_s`, TimeoutError is raised when the work takes longer; the worker
        finishes on its own (pure functions over bounded inputs)."""
        if self.cpu_pool is None:
            raise RuntimeError("no CPU pool configured")
        future = asyncio.get_running_loop().run_in_executor(self.cpu_pool, fn, *args)
        return await asyncio.wait_for(future, timeout_s) if timeout_s else await future

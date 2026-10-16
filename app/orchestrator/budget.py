"""Budget, ledger and kill switch (architecture §6.3, config/budgets.yaml).

Every model call and every job reserves an estimate here *before* it is made (invariant 7) and
commits the actual amount afterwards. Committed spend is persisted (append-only ledger), so caps
survive restarts; reservations still in flight are held in memory under a lock.
"""

import asyncio
import itertools
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field

from app.settings import Env

EPSILON = 1e-9
DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "budgets.yaml"


class SpendKind(StrEnum):
    TOKENS_IN = "tokens_in"
    TOKENS_OUT = "tokens_out"
    INFERENCE_USD = "inference_usd"
    CLOUD_USD = "cloud_usd"
    GPU_MINUTES = "gpu_minutes"
    TAVILY_CREDITS = "tavily_credits"


# The kill switch stops new cloud spend; model calls of runs already in flight still pass.
KILL_SWITCH_KINDS = frozenset({SpendKind.CLOUD_USD, SpendKind.GPU_MINUTES})


class BudgetExceeded(Exception):
    def __init__(self, limit_name: str) -> None:
        super().__init__(f"budget limit reached: {limit_name}")
        self.limit_name = limit_name


class KillSwitchActive(Exception):
    def __init__(self) -> None:
        super().__init__("kill switch active: no new cloud spend today (UTC)")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class PerRunLimits(_Strict):
    tokens_in: int = Field(gt=0)
    tokens_out: int = Field(gt=0)
    inference_usd: float = Field(gt=0)
    gpu_minutes: float = Field(gt=0)
    fix_iterations: int = Field(gt=0)
    package_requests: int = Field(ge=0)
    tavily_credits: int = Field(ge=0)


class GlobalLimits(_Strict):
    concurrent_runs: int = Field(gt=0)
    cloud_usd_per_day: float = Field(gt=0)
    inference_usd_per_day: float = Field(gt=0)
    dev_inference_usd_per_day: float = Field(gt=0)
    tavily_credits_per_day: int = Field(ge=0)


class ApprovalLimits(_Strict):
    auto_approve_below_usd: float = Field(ge=0)
    approval_timeout_min: int = Field(gt=0)


class InviteLimits(_Strict):
    default_quota_per_day: int = Field(gt=0)


class BudgetLimits(_Strict):
    per_run: PerRunLimits
    global_: GlobalLimits = Field(alias="global")
    approval: ApprovalLimits
    invites: InviteLimits


def load_budget_limits(path: Path = DEFAULT_PATH) -> BudgetLimits:
    return BudgetLimits.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


@dataclass(frozen=True)
class Reservation:
    id: int
    run_id: str | None
    kind: SpendKind
    amount: float
    day: date  # UTC day the spend is attributed to


class LedgerStore(Protocol):
    async def add_ledger_entry(
        self, run_id: str | None, day: date, kind: str, amount: float
    ) -> None: ...

    async def ledger_total(
        self, kind: str, *, run_id: str | None = None, day: date | None = None
    ) -> float: ...

    async def kill_switch(self, day: date) -> bool: ...

    async def set_kill_switch(self, day: date, on: bool) -> None: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Budget:
    def __init__(
        self,
        store: LedgerStore,
        limits: BudgetLimits,
        *,
        env: Env,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._store = store
        self._limits = limits
        self._env = env
        self._now = now
        self._lock = asyncio.Lock()
        self._inflight: dict[int, Reservation] = {}
        self._ids = itertools.count(1)

    def _today(self) -> date:
        return self._now().astimezone(UTC).date()

    def _caps(self, run_id: str | None, kind: SpendKind) -> list[tuple[str, float, bool]]:
        """(limit name, cap, per_run?) pairs that apply to this kind, per-run caps first."""
        caps: list[tuple[str, float, bool]] = []
        per_run = self._limits.per_run
        day = self._limits.global_
        if run_id is not None and kind is not SpendKind.CLOUD_USD:
            caps.append((kind.value, float(getattr(per_run, kind.value)), True))
        if kind is SpendKind.INFERENCE_USD:
            caps.append(("inference_usd_per_day", day.inference_usd_per_day, False))
            if self._env == "dev":
                caps.append(("dev_inference_usd_per_day", day.dev_inference_usd_per_day, False))
        elif kind is SpendKind.CLOUD_USD:
            caps.append(("cloud_usd_per_day", day.cloud_usd_per_day, False))
        elif kind is SpendKind.TAVILY_CREDITS:
            caps.append(("tavily_credits_per_day", float(day.tavily_credits_per_day), False))
        return caps

    def _inflight_total(self, kind: SpendKind, run_id: str | None, day: date | None) -> float:
        return math.fsum(
            r.amount
            for r in self._inflight.values()
            if r.kind is kind
            and (run_id is None or r.run_id == run_id)
            and (day is None or r.day == day)
        )

    async def check_and_reserve(
        self, run_id: str | None, kind: SpendKind, amount: float
    ) -> Reservation:
        if not math.isfinite(amount) or amount < 0:
            raise ValueError(f"invalid amount: {amount}")
        day = self._today()
        async with self._lock:
            if kind in KILL_SWITCH_KINDS and await self._store.kill_switch(day):
                raise KillSwitchActive
            for name, cap, per_run in self._caps(run_id, kind):
                scope_run, scope_day = (run_id, None) if per_run else (None, day)
                used = await self._store.ledger_total(kind.value, run_id=scope_run, day=scope_day)
                used += self._inflight_total(kind, scope_run, scope_day)
                if used + amount > cap + EPSILON:
                    raise BudgetExceeded(name)
            reservation = Reservation(next(self._ids), run_id, kind, amount, day)
            self._inflight[reservation.id] = reservation
            return reservation

    async def commit(self, reservation: Reservation, actual: float) -> None:
        """Record the actual spend (0 for a call that failed) and release the reservation."""
        if not math.isfinite(actual) or actual < 0:
            raise ValueError(f"invalid amount: {actual}")
        async with self._lock:
            if self._inflight.pop(reservation.id, None) is None:
                raise ValueError(f"reservation {reservation.id} already committed")
            await self._store.add_ledger_entry(
                reservation.run_id, reservation.day, reservation.kind.value, actual
            )
            if reservation.kind is SpendKind.CLOUD_USD:
                await self._trip_if_over(reservation.day)

    async def enforce_daily_cloud_cap(self, day: date) -> None:
        """Trip the kill switch when cloud spend written without a reservation (the reaper
        settling a job nobody watched) reached the daily cap."""
        async with self._lock:
            await self._trip_if_over(day)

    async def _trip_if_over(self, day: date) -> None:
        spent = await self._store.ledger_total(SpendKind.CLOUD_USD.value, day=day)
        if spent >= self._limits.global_.cloud_usd_per_day - EPSILON:
            await self._store.set_kill_switch(day, True)

    async def run_spend(self, run_id: str, kind: SpendKind) -> float:
        return await self._store.ledger_total(kind.value, run_id=run_id)

    async def day_spend(self, kind: SpendKind) -> float:
        return await self._store.ledger_total(kind.value, day=self._today())

    async def kill_switch_active(self) -> bool:
        return await self._store.kill_switch(self._today())

    async def set_kill_switch(self, on: bool) -> None:
        await self._store.set_kill_switch(self._today(), on)

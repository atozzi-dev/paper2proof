"""Reaper (docs/specs/execution-contracts.md §11): the safety belt behind the orchestrator.

Every 5 minutes it cancels jobs nobody watches any more, settles the ledger for them, deletes old
run buckets once their bundle is stored, and stops the idle egress gateway. Each part, and each
item inside a part, runs on its own: one failing never stops the others, and nothing raises out
of `tick()`. One reaper per deployment: the gateway idle state lives in the backend's process.
"""

import asyncio
import logging
import re
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, date, datetime, timedelta

from app.bundle.store import BundleStore
from app.domain.models import StepName
from app.execution.backend import JobBackend, JobHandle, JobKind, JobState
from app.execution.job_smoke import SMOKE_PLATFORM, SMOKE_PRESET
from app.execution.naming import bucket_name
from app.models.pricing import CloudPrices
from app.observability import metrics
from app.orchestrator.budget import Budget, SpendKind
from app.orchestrator.cost_estimate import (
    EXEC_FALLBACK_PLATFORM,
    EXEC_FALLBACK_PRESET,
    EXEC_PLATFORM,
    EXEC_PRESET,
    PROVISION_PLATFORM,
    PROVISION_PRESET,
)
from app.storage.store import TERMINAL_STEP_STATUSES, JobRecord, Store

log = logging.getLogger(__name__)

INTERVAL_S = 300
JOB_GRACE = timedelta(seconds=600)  # past the job's own watchdog
FALLBACK_WATCHDOG = timedelta(minutes=45)  # a job with no `jobs` row (architecture §7)
PLATFORM_TIMEOUT = timedelta(hours=1)  # the platform kills every job by then (§8)
BUCKET_MAX_AGE = timedelta(hours=24)
GATEWAY_IDLE_MINUTES = 30
GATEWAY_STEPS = frozenset({StepName.PROVISIONING, StepName.SMOKE_TESTING, StepName.EXECUTING})
METRIC = "p2p_reaper_actions_total"
_OUR_JOB = re.compile(r"^p2p-(prov|fetch|exec|smoke)-([0-9a-z]{26})-[0-9]{1,4}$")
_RUN_BUCKET = re.compile(r"^p2p-run-([0-9a-z]{26})$")


def reaper_rates(
    cloud: CloudPrices, *, smoke: tuple[str, str] = (SMOKE_PLATFORM, SMOKE_PRESET)
) -> dict[str, float]:
    """On-demand USD/hour per job kind: an upper bound for time nobody measured, used when the
    job's platform is not recorded (an exec job may be on either GPU platform). `smoke` is the
    configured smoke platform and preset (the GPU one in the hybrid deploy)."""
    return {
        JobKind.PROVISION.value: cloud.hourly(PROVISION_PLATFORM, PROVISION_PRESET),
        JobKind.FETCH.value: cloud.hourly(PROVISION_PLATFORM, PROVISION_PRESET),
        JobKind.EXEC.value: max(
            cloud.hourly(EXEC_PLATFORM, EXEC_PRESET),
            cloud.hourly(EXEC_FALLBACK_PLATFORM, EXEC_FALLBACK_PRESET),
        ),
        JobKind.SMOKE.value: cloud.hourly(*smoke),
    }


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class Reaper:
    def __init__(
        self,
        store: Store,
        jobs: JobBackend,
        bundles: BundleStore,
        budget: Budget,
        *,
        rates: Mapping[str, float],
        cloud: CloudPrices | None = None,
        clock: Callable[[], datetime] = _utcnow,
        interval_s: float = INTERVAL_S,
    ) -> None:
        self._store = store
        self._jobs = jobs
        self._bundles = bundles
        self._budget = budget
        self._rates = dict(rates)
        self._cloud = cloud
        self._clock = clock
        self._interval_s = interval_s
        self.actions_total: Counter[str] = Counter()  # exported as p2p_reaper_actions_total

    def _count(self, actions: Counter[str], action: str, **fields: object) -> None:
        actions[action] += 1
        self.actions_total[action] += 1
        metrics.reaper_action(action)
        log.info(METRIC, extra={"metric": METRIC, "action": action, **fields})

    async def tick(self) -> Counter[str]:
        """One pass. Returns the actions it took (also added to `actions_total`)."""
        actions: Counter[str] = Counter()
        for part, name in (
            (self._reap_jobs, "jobs"),
            (self._settle_ledger, "ledger"),
            (self._reap_buckets, "buckets"),
            (self._stop_idle_gateway, "gateway"),
        ):
            try:
                await part(actions)
            except Exception:
                log.exception("reaper: %s pass failed", name)
                self._count(actions, "error", part=name)
        return actions

    async def run_forever(self, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        """The background loop the app starts (A-14 wiring). Never dies of a failed pass."""
        while True:
            try:
                await self.tick()
            except Exception:  # tick() catches per part; this is the last line of defence
                log.exception("reaper: tick failed")
            await sleep(self._interval_s)

    async def _isolated(
        self, actions: Counter[str], part: str, item: str, work: Awaitable[None]
    ) -> None:
        """One failing item (a refused cancel, an unreadable job) never stops the rest."""
        try:
            await work
        except Exception:
            log.exception("reaper: %s %s failed", part, item)
            self._count(actions, "error", part=part, item=item)

    # --- jobs ----------------------------------------------------------------------------------

    async def _reap_jobs(self, actions: Counter[str]) -> None:
        listed = await self._jobs.list_jobs("p2p-")
        now = self._clock()
        oldest: dict[str, float] = {}
        for handle, state, created in listed:
            if not state.is_terminal:
                match = _OUR_JOB.fullmatch(handle.name)
                kind = match.group(1) if match else "other"
                age = (now - _aware(created)).total_seconds()
                oldest[kind] = max(oldest.get(kind, 0.0), age)
        metrics.jobs_running(oldest)
        for handle, state, created in listed:
            if not state.is_terminal:
                work = self._reap_job(handle, created, actions)
                await self._isolated(actions, "jobs", handle.name, work)

    async def _reap_job(self, handle: JobHandle, created: datetime, actions: Counter[str]) -> None:
        now = self._clock()
        match = _OUR_JOB.fullmatch(handle.name)
        if match is None:  # another p2p-* job (a spike, a manual test): fallback only
            if now > _aware(created) + FALLBACK_WATCHDOG + JOB_GRACE:
                await self._cancel(handle, "job_cancelled_overdue", actions)
            return
        kind, run_id = match.group(1), f"r_{match.group(2)}"
        run = await self._store.get_run(run_id)
        record = await self._store.get_job(handle.name)
        if run is not None and run.status.is_terminal and record is None:
            # Submitted, then the control plane died before record_job: record it so the ledger
            # pass charges it (never zero). An active run records it itself when it resumes.
            await self._store.record_job(
                run_id,
                kind,
                handle.name,
                handle.nebius_id,
                bucket_name(run_id),
                deadline=_aware(created) + FALLBACK_WATCHDOG,
                at=_aware(created),
            )
        if run is None or run.status.is_terminal:  # nobody watches it, and it mounts rw
            await self._cancel(handle, "job_cancelled_orphan", actions)
            return
        if record is not None and record.deadline is not None:
            deadline = _aware(record.deadline)
        else:  # submitted before a crash, never recorded
            deadline = _aware(created) + FALLBACK_WATCHDOG
        if now > deadline + JOB_GRACE:
            await self._cancel(handle, "job_cancelled_overdue", actions)

    async def _cancel(self, handle: JobHandle, action: str, actions: Counter[str]) -> None:
        await self._jobs.cancel(handle)
        self._count(actions, action, job=handle.name)
        # Every cancellation by the reaper is an alert: the orchestrator should have done it.
        log.error("reaper cancelled a job", extra={"alert": True, "job": handle.name})

    # --- ledger gaps ---------------------------------------------------------------------------

    async def _settle_ledger(self, actions: Counter[str]) -> None:
        for record in await self._store.unsettled_jobs():
            await self._isolated(actions, "ledger", record.name, self._settle(record, actions))

    async def _settle(self, record: JobRecord, actions: Counter[str]) -> None:
        run = await self._store.get_run(record.run_id)
        if run is not None and not run.status.is_terminal:
            return  # its step re-attaches by name and settles it
        if record.nebius_job_id is None:
            return
        state = await self._jobs.status(JobHandle(name=record.name, nebius_id=record.nebius_job_id))
        if not state.is_terminal:
            past_timeout = _aware(record.created_at) + PLATFORM_TIMEOUT + JOB_GRACE
            if state is not JobState.UNKNOWN or self._clock() < past_timeout:
                return  # cancelled above; charged once it has stopped
            state = JobState.CANCELLED  # vanished: the platform stopped it by its timeout
        await self._charge(record, state, actions)

    def _rate(self, record: JobRecord) -> float:
        """On-demand USD/hour of the platform and preset the job was recorded with, else (not
        recorded, or not priced) the rate of its kind."""
        if self._cloud is not None and record.platform and record.preset:
            if record.platform in self._cloud.platforms:
                return self._cloud.hourly(record.platform, record.preset)
            log.warning("reaper: no price for platform %s", record.platform)
        return self._rates.get(record.kind, max(self._rates.values(), default=0.0))

    async def _charge(self, record: JobRecord, state: JobState, actions: Counter[str]) -> None:
        """From first submission to now, capped at the platform timeout, at the on-demand rate
        of where the job runs. The part a step already watched may count twice: never zero (the
        budget errs high). Job row and ledger entries are written in one transaction, once."""
        started = _aware(record.created_at)
        now = self._clock()
        elapsed_s = max(0.0, (min(now, started + PLATFORM_TIMEOUT) - started).total_seconds())
        cost = self._rate(record) * elapsed_s / 3600
        gpu = record.kind == JobKind.EXEC.value
        day = now.astimezone(UTC).date()
        ledger: list[tuple[str | None, date, str, float]] = [
            (record.run_id, day, SpendKind.CLOUD_USD.value, cost)
        ]
        if gpu:
            ledger.append((record.run_id, day, SpendKind.GPU_MINUTES.value, elapsed_s / 60))
        settled = await self._store.settle_job(
            record.name,
            state.value,
            cost_usd=round(cost, 6),
            gpu_seconds=elapsed_s if gpu else None,
            ledger=ledger,
        )
        if settled:
            for _, _, kind, amount in ledger:
                metrics.spend(kind, amount)
            await self._budget.enforce_daily_cloud_cap(day)
            await self._budget.publish_run_cost(record.run_id)  # display only: never raises
            self._count(actions, "ledger_reconciled", job=record.name, cost_usd=round(cost, 6))

    # --- buckets -------------------------------------------------------------------------------

    async def _reap_buckets(self, actions: Counter[str]) -> None:
        # Listed after the jobs pass; if this fails, nothing proves that no job still mounts a
        # bucket, so the pass fails and nothing is deleted.
        live = {
            m.group(2)
            for handle, state, _ in await self._jobs.list_jobs("p2p-")
            if not state.is_terminal and (m := _OUR_JOB.fullmatch(handle.name))
        }
        now = self._clock()
        for name, created in await self._jobs.list_run_buckets():
            match = _RUN_BUCKET.fullmatch(name)
            if match is None or now - _aware(created) < BUCKET_MAX_AGE:
                continue
            slug = match.group(1)
            run_id = f"r_{slug}"
            run = await self._store.get_run(run_id)
            if run is None or not run.status.is_terminal or slug in live:
                continue
            if not await self._bundles.exists(run_id):
                self._count(actions, "bucket_kept_no_bundle", bucket=name)
                continue
            try:
                await self._jobs.delete_run_bucket(run_id)
            except Exception:  # still emptying, planted keys...: retried on the next tick
                log.exception("reaper: bucket %s not deleted", name)
                self._count(actions, "bucket_delete_failed", bucket=name)
                continue
            self._count(actions, "bucket_deleted", bucket=name)

    # --- gateway -------------------------------------------------------------------------------

    async def _stop_idle_gateway(self, actions: Counter[str]) -> None:
        for run_id in await self._store.active_run_ids():
            steps = await self._store.get_steps(run_id)
            if any(
                s.name in GATEWAY_STEPS and s.status not in TERMINAL_STEP_STATUSES for s in steps
            ):
                return
        if await self._jobs.gateway_idle_check(GATEWAY_IDLE_MINUTES):
            self._count(actions, "gateway_stopped")

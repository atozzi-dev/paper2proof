"""EXECUTING: the GPU job runs the Coder's script once per planned seed (execution-contracts
§7-8). The manifest is built by code from validated artifacts and kept before submission, so a
resumed step re-attaches to its job with the very manifest the job received.

A preemptible job that ends without its results (or with partial results written on SIGTERM
before its budget ran out) is retried once on demand when the GPU budget left allows it;
otherwise VERDICT reports what there is (INCONCLUSIVE/BUDGET when no seed finished).
A job the platform refuses for lack of GPU capacity is tried again on gpu-l40s-a after 2
minutes, up to 3 times, then once on gpu-l40s-d; with no capacity on either the attempt ends
without a job (INCONCLUSIVE/CAPACITY), see execution-contracts §8.
`live.log` is read from the bucket while the job runs and shown as `job.log` events: it is
written next to untrusted code, so it is only ever displayed, truncated.
"""

import asyncio
import json
import logging
import math
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import ValidationError

from app.domain.contracts import ArchiveDigests, ExecManifest, ManifestPaths, ScaleSpec
from app.domain.errors import P2PError
from app.domain.events import MAX_LINES, EventType
from app.domain.models import ClaimType, ErrorCode, Plan, RunStatus, Scale, StepName
from app.execution.backend import CapacityError, JobKind, JobSpec, JobState, VolumeMount
from app.execution.config import ExecutionConfig
from app.execution.naming import job_name, run_slug
from app.execution.run_storage import RunStorage
from app.models.pricing import CloudPrices
from app.orchestrator.budget import BudgetExceeded, KillSwitchActive, SpendKind
from app.orchestrator.context import RunContext
from app.orchestrator.cost_estimate import (
    EXEC_FALLBACK_PLATFORM,
    EXEC_FALLBACK_PRESET,
    EXEC_PLATFORM,
    EXEC_PRESET,
)
from app.orchestrator.fsm import StepResult
from app.orchestrator.steps.common import output_of, selected_claim
from app.orchestrator.steps.jobs import JobOutcome, run_job, stop_jobs, utcnow
from app.storage.artifacts import ArtifactStore
from app.verdict.results import MAX_RESULTS_BYTES

log = logging.getLogger(__name__)

EXEC_MARGIN_S = 300  # watchdog = budget_seconds + margin (execution-contracts §8)
MIN_BUDGET_S, MAX_BUDGET_S = 60, 1800  # schemas/manifest.schema.json
MAX_ATTEMPTS = 2  # the first job and one on-demand retry after a preemption
PREEMPT_SLACK_S = 5.0  # a runner that hit its own budget reports at least budget_seconds
MAX_LIVE_LOG_BYTES = 512 * 1024
LINE_CHARS = 500
LIVE_LINES = 200  # what the runner mirrors into live.log
EXEC_DISK_GIB = 100
PRIMARY = (EXEC_PLATFORM, EXEC_PRESET)
FALLBACK = (EXEC_FALLBACK_PLATFORM, EXEC_FALLBACK_PRESET)  # execution-contracts §8
CAPACITY_RETRIES = 3  # on the primary platform, before the fallback (architecture §15)
CAPACITY_WAIT_S = 120
NO_CAPACITY = "NO_GPU_CAPACITY"
_REASONS = frozenset({"completed", "experiment_error", "budget_timeout", "contract_error"})


def new_lines(previous: list[str], current: list[str]) -> list[str]:
    """Lines of `current` not shown yet: live.log is rewritten with its last lines, so skip
    the longest overlap between the end of `previous` and the start of `current`."""
    for k in range(min(len(previous), len(current)), 0, -1):
        if previous[-k:] == current[:k]:
            return current[k:]
    return current


def log_lines(raw: bytes) -> list[str]:
    """The last lines of live.log for display: split on newlines only (the runner writes its
    last LIVE_LINES), truncated, control and format characters dropped. Pure (CPU pool)."""
    lines = raw.decode("utf-8", "replace").split("\n")[-LIVE_LINES - 1 :]
    shown = ["".join(ch for ch in line[:LINE_CHARS] if _printable(ch)) for line in lines]
    while shown and not shown[-1]:
        shown.pop()
    return shown[-LIVE_LINES:]


def _printable(ch: str) -> bool:
    return ch == "\t" or unicodedata.category(ch)[0] != "C"


def printable(text: str) -> str:
    """Text without control or format characters (ANSI, bidi, CR), tabs kept."""
    return "".join(ch for ch in text if _printable(ch))


def _workspace_digest(smoke: Any) -> str:
    digest = smoke.get("workspace_sha256") if isinstance(smoke, dict) else None
    if not isinstance(digest, str):
        raise P2PError(ErrorCode.INTERNAL_ERROR, "the smoke result has no workspace digest")
    return digest


def exit_info(raw: bytes) -> tuple[str | None, float | None]:
    """exit_reason and wall_clock_s of a results.json, loosely (only to tell a preemption from
    an ending the runner reported itself; ANALYZING validates the file). Pure (CPU pool)."""
    try:
        data: Any = json.loads(raw)
    except (ValueError, RecursionError):
        return None, None
    if not isinstance(data, dict) or data.get("exit_reason") not in _REASONS:
        return None, None
    wall = data.get("wall_clock_s")
    if isinstance(wall, bool) or not isinstance(wall, int | float) or not math.isfinite(wall):
        return str(data["exit_reason"]), None
    return str(data["exit_reason"]), float(wall)


class _LiveLog:
    """Turns new live.log lines into job.log events (display only: never raises)."""

    def __init__(self, ctx: RunContext, storage: RunStorage, key: str) -> None:
        self._ctx = ctx
        self._storage = storage
        self._key = key
        self._shown: list[str] = []

    async def __call__(self) -> None:
        try:
            raw = await self._storage.get_bytes(
                self._ctx.run_id, self._key, max_bytes=MAX_LIVE_LOG_BYTES
            )
            if not raw:
                return
            lines = await self._ctx.run_cpu(log_lines, raw)
            fresh = new_lines(self._shown, lines)
            self._shown = lines
            if fresh:
                await self._ctx.emit(
                    EventType.JOB_LOG, {"kind": JobKind.EXEC.value, "lines": fresh[-MAX_LINES:]}
                )
        except Exception:
            log.debug("live.log not shown", extra={"run_id": self._ctx.run_id}, exc_info=True)


@dataclass(frozen=True)
class _Placed:
    outcome: JobOutcome
    spec: JobSpec


_Unplaced = Literal["no_capacity", "cancelled"]


class ExecutingStep:
    name = StepName.EXECUTING

    def __init__(
        self,
        *,
        artifacts: ArtifactStore,
        storage: RunStorage,
        config: ExecutionConfig,
        cloud: CloudPrices,
        poll_s: float = 15,
        clock: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._artifacts = artifacts
        self._storage = storage
        self._config = config
        self._cloud = cloud
        self._poll_s = poll_s
        self._clock = clock
        self._sleep = sleep

    async def _json(self, key: str) -> Any:
        return await asyncio.to_thread(self._artifacts.get_json, key)

    async def _put(self, key: str, value: dict[str, Any]) -> None:
        await asyncio.to_thread(self._artifacts.put_json, key, value)

    async def _budget_seconds(self, ctx: RunContext, plan: Plan) -> int | None:
        """The planned GPU time, within what the run's GPU minutes still allow (the watchdog
        margin included); None when not even the minimum fits."""
        spent = await ctx.budget.run_spend(ctx.run_id, SpendKind.GPU_MINUTES)
        left_s = int((ctx.limits.per_run.gpu_minutes - spent) * 60) - EXEC_MARGIN_S
        planned = max(math.ceil(plan.gpu_minutes_est * 60), MIN_BUDGET_S)
        budget = min(planned, MAX_BUDGET_S, left_s)
        return budget if budget >= MIN_BUDGET_S else None

    async def _archives(self, ctx: RunContext, plan: Plan) -> ArchiveDigests:
        """Digests from the validated provision report artifact, for exactly the repository
        and the plan's datasets (fail closed: a missing one is a provisioning failure, not a
        run without it); the smoke subset is not for the exec job."""
        report = await self._json(await output_of(ctx, StepName.PROVISIONING))
        report = report if isinstance(report, dict) else {}
        data = [
            a
            for a in report.get("data_archives") or []
            if isinstance(a, dict) and a.get("name") != "smoke"
        ]
        expected = sorted(d.id.replace("/", "__") for d in plan.datasets)
        failed = P2PError(ErrorCode.PROVISION_FAILED, "the provision report has no valid digests")
        try:
            archives = ArchiveDigests.model_validate(
                {
                    "overlay": report.get("overlay_sha256"),
                    "repo": report.get("repo_sha256"),
                    "data": data,
                }
            )
        except ValidationError:
            raise failed from None
        if report.get("repo") is True and archives.repo is None:
            raise failed
        if sorted(a.name for a in archives.data) != expected:
            raise failed
        return archives

    async def _manifest(self, ctx: RunContext, plan: Plan, attempt: int) -> ExecManifest | None:
        """The attempt's manifest: the one kept before its job was submitted, or a new one
        (None when the GPU budget left is too small for it)."""
        key = f"runs/{ctx.run_id}/exec_manifest_{attempt}.json"
        try:
            return ExecManifest.model_validate(await self._json(key))
        except FileNotFoundError:
            pass
        budget = await self._budget_seconds(ctx, plan)
        if budget is None:
            return None
        claim, kind = await selected_claim(ctx, plan)
        smoke = await self._json(await output_of(ctx, StepName.SMOKE_TESTING))
        archives = await self._archives(ctx, plan)
        manifest = ExecManifest(
            schema_="p2p.manifest/v1",
            run_id=ctx.run_id,
            attempt=attempt,
            image_digest=self._config.runner_image.rsplit("@", 1)[1],
            entrypoint=str(smoke["entrypoint"]),
            args=[str(a) for a in smoke["args"]],
            seeds=plan.seeds,
            budget_seconds=budget,
            paths=ManifestPaths(
                overlay="/mnt/run/staging/overlay.tar.zst",
                repo="/mnt/run/staging/repo.tar.zst" if archives.repo else None,
                data_dir="/mnt/run/staging/data",
                workspace="/mnt/run/staging/workspace.tar",
                cache="/mnt/cache",
                results=f"/mnt/run/results/{attempt}",
            ),
            claim_ref=claim.id,
            metric=claim.metric,
            higher_is_better=claim.higher_is_better,
            claim_type=kind,
            baseline_name=(
                (claim.baseline_name or "")[:128] or None if kind is ClaimType.RELATIVE else None
            ),
            scale=ScaleSpec(full=plan.scale is Scale.FULL),
            archives=archives,
            workspace_sha256=_workspace_digest(smoke),
        )
        await self._put(key, manifest.model_dump(mode="json", by_alias=True))
        return manifest

    def _spec(
        self, manifest: ExecManifest, bucket: str, preemptible: bool, where: tuple[str, str]
    ) -> JobSpec:
        body = manifest.model_dump_json(by_alias=True).encode()
        return JobSpec(
            name=job_name(JobKind.EXEC, manifest.run_id, manifest.attempt),
            kind=JobKind.EXEC,
            image_digest_ref=self._config.runner_image,
            platform=where[0],
            preset=where[1],
            disk_gib=EXEC_DISK_GIB,
            subnet_id=self._config.subnet_exec,
            volumes=[
                VolumeMount(source=bucket, mount_path="/mnt/run", mode="rw"),
                VolumeMount(source="p2p-cache", mount_path="/mnt/cache", mode="ro"),
            ],
            inject_files={"/etc/p2p/manifest.json": body},
            env={"PYTHONUNBUFFERED": "1"},
            preemptible=preemptible,
            watchdog_s=manifest.budget_seconds + EXEC_MARGIN_S,
        )

    async def _clear_stale(self, ctx: RunContext, name: str, attempt: int) -> None:
        """A fresh job's results path may hold files planted by an earlier smoke job (they
        share the bucket): remove them so only this job's own output is read back."""
        if await ctx.store.get_job(name) is not None:
            return
        if any(h.name == name for h, _, _ in await ctx.jobs.list_jobs(name)):
            return  # submitted before a crash, never recorded: its output is its own
        for leaf in ("results.json", "live.log"):
            await self._storage.delete(ctx.run_id, f"results/{attempt}/{leaf}")

    async def _done(
        self,
        ctx: RunContext,
        manifest: ExecManifest,
        state: JobState | None,
        *,
        where: tuple[str, str] | None,
        has_results: bool,
        exit_reason: str | None = None,
    ) -> StepResult:
        """`state` and `where` None: no job was ever created for the attempt (no GPU capacity).
        `where` is the platform and preset the attempt's job ran on (the run's conditions)."""
        key = f"runs/{ctx.run_id}/execution_{manifest.attempt}.json"
        await self._put(
            key,
            {
                "manifest": manifest.model_dump(mode="json", by_alias=True),
                "results_key": f"results/{manifest.attempt}/results.json" if has_results else None,
                "exit_reason": None if has_results else exit_reason,
                "job_state": None if state is None else state.value,
                "platform": None if where is None else where[0],
                "preset": None if where is None else where[1],
            },
        )
        return StepResult(next=StepName.ANALYZING, output_ref=key)

    def _placement_key(self, ctx: RunContext, attempt: int) -> str:
        return f"runs/{ctx.run_id}/exec_platform_{attempt}.json"

    async def _moved(self, ctx: RunContext, attempt: int) -> bool:
        """Whether the attempt moved to the fallback platform (kept before that submission, so
        a resumed step finds its job and pays the right price)."""
        try:
            await self._json(self._placement_key(ctx, attempt))
        except FileNotFoundError:
            return False
        return True

    async def _placements(self, ctx: RunContext, attempt: int) -> list[tuple[str, str]]:
        """Where the attempt's job may be created, in order: the primary platform and its
        retries, then the fallback; only the fallback once the step moved there."""
        if await self._moved(ctx, attempt):
            return [FALLBACK]
        return [PRIMARY] * (1 + CAPACITY_RETRIES) + [FALLBACK]

    async def _ran_on(self, ctx: RunContext, attempt: int) -> tuple[str, str]:
        """Where an attempt that had a job ran: the fallback once it moved there, else the
        primary platform (a job is only ever created on one of the two)."""
        return FALLBACK if await self._moved(ctx, attempt) else PRIMARY

    async def _no_capacity(self, ctx: RunContext, message: str) -> None:
        await ctx.emit(EventType.WARNING, {"code": NO_CAPACITY, "message": message})

    async def _place(
        self,
        ctx: RunContext,
        manifest: ExecManifest,
        bucket: str,
        preemptible: bool,
        tail: Callable[[], Awaitable[None]],
    ) -> _Placed | _Unplaced:
        """Submit and watch the attempt's job where there is GPU capacity (architecture §15).
        Every try uses the same job name: a refused creation leaves no job behind."""
        places = await self._placements(ctx, manifest.attempt)
        for i, where in enumerate(places):
            if i > 0:
                if where == places[i - 1]:
                    await self._no_capacity(
                        ctx, f"no GPU capacity on {where[0]}: retrying in 2 minutes"
                    )
                    await self._sleep(CAPACITY_WAIT_S)
                else:
                    await self._no_capacity(
                        ctx, f"no GPU capacity on {places[i - 1][0]}: trying {where[0]}"
                    )
                    await self._put(
                        self._placement_key(ctx, manifest.attempt),
                        {"platform": where[0], "preset": where[1]},
                    )
                run = await ctx.store.get_run(ctx.run_id)
                if run is None or run.status is not RunStatus.RUNNING:
                    return "cancelled"
            spec = self._spec(manifest, bucket, preemptible, where)
            await self._clear_stale(ctx, spec.name, manifest.attempt)
            try:
                outcome = await run_job(
                    ctx,
                    spec,
                    usd_per_hour=self._cloud.hourly(where[0], where[1], preemptible=preemptible),
                    poll_s=self._poll_s,
                    clock=self._clock,
                    sleep=self._sleep,
                    gpu=True,
                    on_poll=tail,
                )
            except CapacityError:
                log.info("no GPU capacity", extra={"run_id": ctx.run_id, "platform": where[0]})
                continue
            return _Placed(outcome, spec)
        tried = " or ".join(dict.fromkeys(p for p, _ in places))
        await self._no_capacity(ctx, f"no GPU capacity on {tried}: the GPU job did not run")
        return "no_capacity"

    async def _warn(self, ctx: RunContext, message: str) -> None:
        await ctx.emit(EventType.WARNING, {"code": "JOB_PREEMPTED", "message": message})

    async def run(self, ctx: RunContext) -> StepResult:
        plan = Plan.model_validate(
            (await self._json(await output_of(ctx, StepName.PLANNING)))["plan"]
        )
        smoke = f"p2p-smoke-{run_slug(ctx.run_id)}-"  # left by a restart, before `finish`
        if not await stop_jobs(ctx, smoke, poll_s=self._poll_s, sleep=self._sleep):
            raise P2PError(ErrorCode.TIMEOUT, "an earlier smoke job did not stop")
        previous = await self._retry_started(ctx, plan)
        first = 1 if previous is None else 2
        for attempt in range(first, MAX_ATTEMPTS + 1):
            manifest = await self._manifest(ctx, plan, attempt)
            if manifest is None and previous is None:
                raise BudgetExceeded("gpu_minutes")
            if manifest is None:
                assert previous is not None  # noqa: S101 (narrows the type)
                await self._warn(
                    ctx, "the GPU job was preempted; no retry: the GPU budget left is too small"
                )
                return await self._no_retry(ctx, *previous)
            preemptible = self._config.preemptible_gpu and attempt == 1
            if previous is not None:
                await self._warn(ctx, "the GPU job was preempted: retrying once on demand")
            bucket = await ctx.jobs.ensure_run_bucket(ctx.run_id)
            await ctx.jobs.ensure_gateway_up()
            tail = _LiveLog(ctx, self._storage, f"results/{attempt}/live.log")
            try:
                placed = await self._place(ctx, manifest, bucket, preemptible, tail)
            except (BudgetExceeded, KillSwitchActive) as err:
                if previous is None:
                    raise
                await self._warn(ctx, f"the on-demand retry was not started ({err})")
                exec_jobs = f"p2p-exec-{run_slug(ctx.run_id)}-"  # a re-attached retry, cancelled
                if not await stop_jobs(ctx, exec_jobs, poll_s=self._poll_s, sleep=self._sleep):
                    kept, state, _ = previous  # it may still write: never read attempt 1
                    return await self._no_retry(ctx, kept, state, None)
                return await self._no_retry(ctx, *previous)
            if placed == "cancelled":
                return StepResult(next=RunStatus.CANCELLED)
            if placed == "no_capacity":
                if previous is None:
                    return await self._done(
                        ctx, manifest, None, where=None, has_results=False, exit_reason="capacity"
                    )
                await self._warn(ctx, "the on-demand retry was not started (no GPU capacity)")
                return await self._no_retry(ctx, *previous)
            outcome, spec = placed.outcome, placed.spec
            await tail()
            if outcome.run_cancelled:
                return StepResult(next=RunStatus.CANCELLED)
            if outcome.timed_out:
                raise P2PError(
                    ErrorCode.TIMEOUT,
                    f"the GPU job did not finish within {spec.watchdog_s} s",
                )
            ran_on = (spec.platform, spec.preset)
            if outcome.state is JobState.SUCCEEDED:
                return await self._done(
                    ctx, manifest, outcome.state, where=ran_on, has_results=True
                )
            reason, wall = await self._exit_info(ctx, attempt)
            preempted = reason is None or (
                reason == "budget_timeout"
                and (wall is None or wall < manifest.budget_seconds - PREEMPT_SLACK_S)
            )
            if not (preemptible and preempted):
                return await self._done(
                    ctx,
                    manifest,
                    outcome.state,
                    where=ran_on,
                    has_results=reason is not None,
                    exit_reason="experiment_error",
                )
            previous = (manifest, outcome.state, reason)
            await self._put(  # decided once: a resumed step goes straight to the retry
                f"runs/{ctx.run_id}/exec_preempted_{attempt}.json",
                {"state": outcome.state.value, "reason": reason},
            )
        raise AssertionError(
            "unreachable: only the first attempt is preemptible"
        )  # pragma: no cover

    async def _retry_started(
        self, ctx: RunContext, plan: Plan
    ) -> tuple[ExecManifest, JobState, str | None] | None:
        """The first attempt's outcome when its retry was already set up before a restart:
        attempt 1 is never judged again from a bucket the retry's code can write to."""
        try:
            await self._json(f"runs/{ctx.run_id}/exec_manifest_2.json")
            outcome = await self._json(f"runs/{ctx.run_id}/exec_preempted_1.json")
        except FileNotFoundError:
            return None
        first = await self._manifest(ctx, plan, 1)
        assert first is not None  # noqa: S101 (kept before its job was submitted)
        reason = outcome.get("reason")
        return first, JobState(outcome["state"]), reason if isinstance(reason, str) else None

    async def _exit_info(self, ctx: RunContext, attempt: int) -> tuple[str | None, float | None]:
        try:
            raw = await self._storage.get_bytes(
                ctx.run_id, f"results/{attempt}/results.json", max_bytes=MAX_RESULTS_BYTES
            )
        except ValueError:
            return "contract_error", None  # oversized: ANALYZING turns it into CONTRACT_ERROR
        return await ctx.run_cpu(exit_info, raw) if raw else (None, None)

    async def _no_retry(
        self, ctx: RunContext, manifest: ExecManifest, state: JobState, reason: str | None
    ) -> StepResult:
        """Partial results of the preempted job if it wrote any, else none (INCONCLUSIVE/BUDGET).
        `manifest` is the preempted attempt's: its record names where that job ran."""
        return await self._done(
            ctx,
            manifest,
            state,
            where=await self._ran_on(ctx, manifest.attempt),
            has_results=reason is not None,
            exit_reason="budget_timeout",
        )

"""EXECUTING: the GPU job runs the Coder's script once per planned seed (execution-contracts
§7-8). The manifest is built by code from validated artifacts and kept before submission, so a
resumed step re-attaches to its job with the very manifest the job received.

A preemptible job that ends without its results (or with partial results written on SIGTERM
before its budget ran out) is retried once on demand when the GPU budget left allows it;
otherwise VERDICT reports what there is (INCONCLUSIVE/BUDGET when no seed finished).
A job the platform refuses for lack of GPU capacity is tried again on gpu-l40s-a after 2
minutes, up to 3 times, then once on gpu-l40s-d; with no capacity on either the attempt ends
without a job (INCONCLUSIVE/CAPACITY), see execution-contracts §8.
A job the platform accepts but keeps queued past QUEUE_CAP_S (S4: no error, just waiting) is
cancelled and the attempt ends the same way: its deterministic name is taken by then.
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
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import ValidationError

from app.domain.contracts import (
    ArchiveDigests,
    ClaimSpec,
    ExecManifest,
    ManifestPaths,
    ScaleSpec,
)
from app.domain.errors import P2PError
from app.domain.events import MAX_LINES, EventType
from app.domain.models import (
    Claim,
    ClaimType,
    ErrorCode,
    ExecTarget,
    FallbackReason,
    GpuFallback,
    Plan,
    RunStatus,
    Scale,
    SetupRecord,
    StepName,
)
from app.execution.backend import (
    CapacityError,
    JobKind,
    JobSpec,
    JobState,
    QuotaExceeded,
    VolumeMount,
)
from app.execution.config import ExecutionConfig
from app.execution.naming import job_name, run_slug
from app.execution.nebius_backend import JobCreateFailed
from app.execution.run_storage import RunStorage
from app.execution.sandbox_jobs import SANDBOX_PLATFORM
from app.models.pricing import CloudPrices
from app.orchestrator.budget import BudgetExceeded, KillSwitchActive, SpendKind
from app.orchestrator.context import RunContext
from app.orchestrator.fallback import fallback_key
from app.orchestrator.fsm import StepResult
from app.orchestrator.steps.common import active_plan, output_of, planned_claims
from app.orchestrator.steps.jobs import QUEUE_CAP_S, JobOutcome, run_job, stop_jobs, utcnow
from app.orchestrator.steps.planning import MIN_SEEDS
from app.storage.artifacts import ArtifactStore
from app.verdict.results import MAX_RESULTS_BYTES

log = logging.getLogger(__name__)

EXEC_MARGIN_S = 300  # watchdog = budget_seconds + margin (execution-contracts §8)
MIN_BUDGET_S = 60  # schemas/manifest.schema.json
MAX_BUDGET_S = 3600  # the schema's bound: an hour on the GPU; on CPU, cpu_minutes (A-47)
MAX_ATTEMPTS = 2  # the first job and one on-demand retry after a preemption
FALLBACK_ATTEMPT = MAX_ATTEMPTS + 1  # the CPU plan's job after a GPU fallback (A-47)
NO_GPU_FALLBACK = "NO_GPU_FALLBACK_CPU"
_FALLBACK_MESSAGES = {
    FallbackReason.NO_CAPACITY: "no GPU was free within {minutes} minutes",
    FallbackReason.QUEUE_TIMEOUT: "no GPU was free within {minutes} minutes",
    FallbackReason.QUOTA: "the GPU quota is exhausted",
    FallbackReason.PLATFORM_ERROR: "the GPU job could not start (platform error)",
    FallbackReason.DAILY_CAP: "the daily cloud budget does not cover a GPU job",
}
PREEMPT_SLACK_S = 5.0  # a runner that hit its own budget reports at least budget_seconds
MAX_LIVE_LOG_BYTES = 512 * 1024
LINE_CHARS = 500
LIVE_LINES = 200  # what the runner mirrors into live.log
EXEC_DISK_GIB = 100
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


def _noun(where: tuple[str, str]) -> str:
    return "GPU " if where[0].startswith("gpu-") else ""


def _job(where: tuple[str, str]) -> str:
    return "GPU job" if where[0].startswith("gpu-") else "experiment job"


def _printable(ch: str) -> bool:
    return ch == "\t" or unicodedata.category(ch)[0] != "C"


def printable(text: str) -> str:
    """Text without control or format characters (ANSI, bidi, CR), tabs kept."""
    return "".join(ch for ch in text if _printable(ch))


def _claim_specs(planned: list[tuple[Claim, ClaimType]], smoke: Any) -> list[ClaimSpec]:
    """The manifest's claims: every planned claim the Coder measured, in the plan's order, with
    the metrics.json keys it declared at finish (validated there against the smoke run)."""
    measures = smoke.get("measures") if isinstance(smoke, dict) else None
    if not isinstance(measures, list):
        raise P2PError(ErrorCode.INTERNAL_ERROR, "the smoke result declares no measures")
    by_claim = {m["claim_id"]: m for m in measures}
    specs = []
    for claim, kind in planned:
        measure = by_claim.get(claim.id)
        if measure is None:
            continue  # declared unmeasured: VERDICT decides it without seeds
        relative = kind is ClaimType.RELATIVE
        specs.append(
            ClaimSpec(
                claim_ref=claim.id,
                claim_type=kind,
                metric=claim.metric,
                higher_is_better=claim.higher_is_better,
                metric_key=measure["key"],
                # the name is only echoed back: a RELATIVE claim without one still runs
                baseline_name=((claim.baseline_name or "")[:128] or "baseline")
                if relative
                else None,
                baseline_key=measure.get("baseline_key") if relative else None,
            )
        )
    if not specs:
        raise P2PError(ErrorCode.INTERNAL_ERROR, "no planned claim is measured")
    return specs


def full_scale(plan: Plan, smoke: dict[str, Any]) -> bool:
    """The scale the experiment runs at (execution-contracts §6.1): full only when the plan is,
    every condition of its setup has a record from `finish`, and each record matches the
    paper. A plan stored before the setup existed lists none and keeps its scale."""
    try:
        records = [SetupRecord.model_validate(r) for r in smoke.get("setup") or []]
    except ValidationError:
        raise P2PError(
            ErrorCode.INTERNAL_ERROR, "the smoke result's setup is not readable"
        ) from None
    return (
        plan.scale is Scale.FULL
        and len(records) == len(plan.setup)
        and {record.id for record in records} == {item.id for item in plan.setup}
        and all(record.matches_paper for record in records)
    )


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


_Unplaced = Literal["no_capacity", "queue_timeout", "cancelled"]
_Refused = Literal["quota", "platform_error", "daily_cap"]  # a GPU job with a fallback only
_REASON: dict[str, FallbackReason] = {
    "no_capacity": FallbackReason.NO_CAPACITY,
    "queue_timeout": FallbackReason.QUEUE_TIMEOUT,
    "quota": FallbackReason.QUOTA,
    "platform_error": FallbackReason.PLATFORM_ERROR,
    "daily_cap": FallbackReason.DAILY_CAP,
}


@dataclass(frozen=True)
class _Where:
    """Where the run's target executes (A-47): the platform with its capacity fallback, the
    runner image the manifest names, and whether it is a GPU (an hour; a window with auto)."""

    primary: tuple[str, str]
    fallback: tuple[str, str] | None
    runner_image: str
    gpu: bool


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

    async def _created(self, ctx: RunContext, attempt: int) -> bool:
        """Whether the attempt's job may have been created: recorded, known to a backend (a
        Sandboxes spawn intent), or listed by its name. Fails closed: a listing error says yes."""
        name = job_name(JobKind.EXEC, ctx.run_id, attempt)
        if await ctx.store.get_job(name) is not None:
            return True
        knows = getattr(ctx.jobs, "knows", None)
        if knows is not None and await knows(name):
            return True
        try:
            return any(h.name == name for h, _, _ in await ctx.jobs.list_jobs(name))
        except Exception:
            log.warning("exec job listing failed", extra={"run_id": ctx.run_id}, exc_info=True)
            return True

    async def _exists(self, key: str) -> bool:
        try:
            await self._json(key)
        except FileNotFoundError:
            return False
        return True

    async def _put(self, key: str, value: dict[str, Any]) -> None:
        await asyncio.to_thread(self._artifacts.put_json, key, value)

    def _where(self, target: ExecTarget) -> _Where:
        """A CPU target runs in Sandboxes when the deploy has both (auto); otherwise the exec
        config is the deploy's one place (Sandboxes, or the GPU with its fallback)."""
        config = self._config
        if target is ExecTarget.CPU and config.cpu_exec is not None:
            return _Where(config.cpu_exec, None, config.runner_cpu_image, gpu=False)
        return _Where(
            (config.exec_platform, config.exec_preset),
            config.exec_fallback,
            config.runner_image,
            gpu=config.exec_platform != SANDBOX_PLATFORM,
        )

    async def _budget_seconds(self, ctx: RunContext, plan: Plan, where: _Where) -> int | None:
        """The planned GPU time, within what the run's GPU minutes still allow (the watchdog
        margin included); None when not even the minimum fits. In Sandboxes (CPU, cents per
        hour) the run's whole allowance: the Planner's estimate is a guess, and a job that
        outlives it ends the run without enough seeds (KAN, eval set B)."""
        limits = ctx.limits.per_run
        spent = await ctx.budget.run_spend(ctx.run_id, SpendKind.GPU_MINUTES)
        if where.gpu:  # A-47: up to an hour, within the plan's estimate
            allowance, cap = limits.gpu_minutes, MAX_BUDGET_S
            planned = max(math.ceil(plan.gpu_minutes_est * 60), MIN_BUDGET_S)
        else:  # Sandboxes: the whole CPU allowance (the estimate is a guess)
            allowance = limits.cpu_minutes
            cap = planned = int(limits.cpu_minutes * 60)
        left_s = int((allowance - spent) * 60) - EXEC_MARGIN_S
        budget = min(planned, cap, left_s)
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

    async def _manifest(
        self, ctx: RunContext, plan: Plan, attempt: int, where: _Where
    ) -> ExecManifest | None:
        """The attempt's manifest: the one kept before its job was submitted, or a new one
        (None when the GPU budget left is too small for it)."""
        key = f"runs/{ctx.run_id}/exec_manifest_{attempt}.json"
        try:
            stored = ExecManifest.model_validate(await self._json(key))
        except FileNotFoundError:
            stored = None
        if stored is not None:
            digest = where.runner_image.rsplit("@", 1)[1]
            if stored.image_digest == digest or await self._created(ctx, attempt):
                return stored  # what its job received
            # kept before a deploy changed the runner, and no job yet: the job will run the new
            # image, so the manifest (and the bundle's replay) must name it
            fresh = stored.model_copy(update={"image_digest": digest})
            await self._put(key, fresh.model_dump(mode="json", by_alias=True))
            return fresh
        budget = await self._budget_seconds(ctx, plan, where)
        if budget is None:
            return None
        smoke = await self._json(await output_of(ctx, StepName.SMOKE_TESTING))
        claims = _claim_specs(await planned_claims(ctx, plan), smoke)
        archives = await self._archives(ctx, plan)
        manifest = ExecManifest(
            schema_="p2p.manifest/v2",
            run_id=ctx.run_id,
            attempt=attempt,
            image_digest=where.runner_image.rsplit("@", 1)[1],
            entrypoint=str(smoke["entrypoint"]),
            args=[str(a) for a in smoke["args"]],
            seeds=plan.seeds,
            budget_seconds=budget,
            # after the verdict's minimum, a seed starts only while it should end in time
            # (A-46); a plan approved before A-46 lists three seeds: nothing more to start
            min_seeds=min(MIN_SEEDS, len(plan.seeds)),
            target_seconds=ctx.limits.per_run.seed_target_s,
            paths=ManifestPaths(
                overlay="/mnt/run/staging/overlay.tar.zst",
                repo="/mnt/run/staging/repo.tar.zst" if archives.repo else None,
                data_dir="/mnt/run/staging/data",
                workspace="/mnt/run/staging/workspace.tar",
                cache="/mnt/cache",
                results=f"/mnt/run/results/{attempt}",
            ),
            claims=claims,
            scale=ScaleSpec(full=full_scale(plan, smoke)),
            archives=archives,
            workspace_sha256=_workspace_digest(smoke),
        )
        await self._put(key, manifest.model_dump(mode="json", by_alias=True))
        return manifest

    def _preemptible(self, attempt: int) -> bool:
        """Only the first attempt may be preemptible; the retry after a preemption is not."""
        return self._config.preemptible_gpu and attempt == 1

    def _spec(
        self,
        manifest: ExecManifest,
        bucket: str,
        preemptible: bool,
        where: tuple[str, str],
        image: str,
    ) -> JobSpec:
        body = manifest.model_dump_json(by_alias=True).encode()
        return JobSpec(
            name=job_name(JobKind.EXEC, manifest.run_id, manifest.attempt),
            kind=JobKind.EXEC,
            image_digest_ref=image,
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
        image: str | None = None,
    ) -> StepResult:
        """`state` and `where` None: no job was ever created for the attempt (no GPU capacity).
        `where` is the platform and preset the attempt's job ran on (the run's conditions),
        `image` the runner it ran (by digest: what the bundle's replay pulls)."""
        key = f"runs/{ctx.run_id}/execution_{manifest.attempt}.json"
        await self._put(
            key,
            {
                "manifest": manifest.model_dump(mode="json", by_alias=True),
                "results_key": f"results/{manifest.attempt}/results.json" if has_results else None,
                "exit_reason": None if has_results else exit_reason,
                "job_state": None if state is None else state.value,
                # the runner's repository with the digest the manifest names (what it pulled)
                "runner_image": None
                if image is None
                else f"{image.rsplit('@', 1)[0]}@{manifest.image_digest}",
                "hardware": None  # how the judged job ran (the verdict's conditions)
                if where is None
                else {
                    "platform": where[0],
                    "preset": where[1],
                    "preemptible": self._preemptible(manifest.attempt),
                },
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

    async def _placements(
        self, ctx: RunContext, attempt: int, where: _Where
    ) -> list[tuple[str, str]]:
        """Where the attempt's job may be created, in order: the primary platform and its
        retries, then the fallback (if any); only the fallback once the step moved there."""
        if where.fallback is not None and await self._moved(ctx, attempt):
            return [where.fallback]
        retries = [where.primary] * (1 + CAPACITY_RETRIES)
        return retries if where.fallback is None else [*retries, where.fallback]

    async def _ran_on(self, ctx: RunContext, attempt: int, where: _Where) -> tuple[str, str]:
        """Where an attempt that had a job ran: the fallback once it moved there, else the
        primary platform (a job is only ever created on one of the two)."""
        if where.fallback is not None and await self._moved(ctx, attempt):
            return where.fallback
        return where.primary

    async def _window(self, ctx: RunContext) -> datetime:
        """The end of the GPU window (A-47): gpu_wait_s from the first creation, kept so that
        a restart continues it instead of starting a new one."""
        key = f"runs/{ctx.run_id}/gpu_window.json"
        try:
            started = datetime.fromisoformat(str((await self._json(key))["started_at"]))
        except FileNotFoundError:
            started = self._clock()
            await self._put(key, {"started_at": started.isoformat()})
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        return started + timedelta(seconds=ctx.limits.per_run.gpu_wait_s)

    async def _fall_back(
        self, ctx: RunContext, reason: FallbackReason, manifest: ExecManifest
    ) -> StepResult:
        """The GPU job never ran: once every exec job of the run is stopped, record why and
        send the run back through PROVISIONING and SMOKE_TESTING on its CPU plan (A-47). The
        record is this attempt's output_ref, so the step records alone tell it happened."""
        exec_jobs = f"p2p-exec-{run_slug(ctx.run_id)}-"
        if not await stop_jobs(
            ctx, exec_jobs, poll_s=self._poll_s, sleep=self._sleep, clock=self._clock
        ):
            raise P2PError(ErrorCode.TIMEOUT, "the GPU job did not stop: no CPU fallback yet")
        gpu_attempt = manifest.attempt
        key = fallback_key(ctx.run_id)
        if not await self._exists(key) and not await self._cpu_can_run(ctx):
            await self._no_capacity(  # a CPU attempt that can't run would only cost
                ctx, "the GPU job did not run, and the run's time left can't hold its CPU plan"
            )
            return await self._done(
                ctx, manifest, None, where=None, has_results=False, exit_reason="capacity"
            )
        try:
            await self._json(key)  # recorded before a restart: once
        except FileNotFoundError:
            minutes = f"{ctx.limits.per_run.gpu_wait_s / 60:g}"
            why = _FALLBACK_MESSAGES[reason].format(minutes=minutes)
            message = f"{why}: the CPU plan runs instead"
            await ctx.emit(EventType.WARNING, {"code": NO_GPU_FALLBACK, "message": message})
            if reason is FallbackReason.PLATFORM_ERROR:  # not capacity: an operator should look
                log.warning(
                    "GPU job never started: platform error",
                    extra={"run_id": ctx.run_id, "alert": True},
                )
            record = GpuFallback(
                reason=reason, message=message, at=self._clock(), gpu_attempt=gpu_attempt
            )
            await self._put(key, record.model_dump(mode="json"))
        return StepResult(next=StepName.PROVISIONING, output_ref=key)

    async def _no_capacity(self, ctx: RunContext, message: str) -> None:
        await ctx.emit(EventType.WARNING, {"code": NO_CAPACITY, "message": message})

    async def _place(
        self,
        ctx: RunContext,
        manifest: ExecManifest,
        bucket: str,
        preemptible: bool,
        tail: Callable[[], Awaitable[None]],
        target: _Where,
        deadline: datetime | None = None,
    ) -> _Placed | _Unplaced | _Refused:
        """Submit and watch the attempt's job where there is GPU capacity (architecture §15).
        Every try uses the same job name: a refused creation leaves no job behind. With a
        `deadline` (a GPU target that can fall back, A-47) the tries stop at the end of the
        GPU window, and a refusal that a wait never helps is returned instead of raised."""
        places = await self._placements(ctx, manifest.attempt, target)
        recorded = await ctx.store.get_job(job_name(JobKind.EXEC, ctx.run_id, manifest.attempt))
        tries = 0
        for i, where in enumerate(places):
            if deadline is not None and recorded is None and self._clock() >= deadline:
                break  # a job created before a restart is re-attached instead
            if i > 0:
                if where == places[i - 1]:
                    if deadline is not None and (
                        self._clock() + timedelta(seconds=CAPACITY_WAIT_S) >= deadline
                    ):
                        break  # the next try would come after the window
                    await self._no_capacity(
                        ctx, f"no {_noun(where)}capacity on {where[0]}: retrying in 2 minutes"
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
            spec = self._spec(manifest, bucket, preemptible, where, target.runner_image)
            await self._clear_stale(ctx, spec.name, manifest.attempt)
            queue_cap_s = float(QUEUE_CAP_S)
            if deadline is not None:  # the queue gets what is left of the window
                existing = await ctx.store.get_job(spec.name)
                start = self._clock() if existing is None else existing.created_at
                if start.tzinfo is None:
                    start = start.replace(tzinfo=UTC)
                queue_cap_s = max((deadline - start).total_seconds(), 1.0)
            tries += 1
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
                    queue_cap_s=queue_cap_s,
                    requeue=deadline is not None,
                )
            except CapacityError:
                log.info("no GPU capacity", extra={"run_id": ctx.run_id, "platform": where[0]})
                continue
            except QuotaExceeded:
                if deadline is None:
                    raise
                return "quota"
            except JobCreateFailed:  # never one raised while polling, or uncertain
                if deadline is None:
                    raise
                log.warning("GPU job creation failed", extra={"run_id": ctx.run_id}, exc_info=True)
                return "platform_error"
            except BudgetExceeded as err:
                if deadline is None or err.limit_name != "cloud_usd_per_day":
                    raise
                return "daily_cap"
            if outcome.queue_timed_out:  # accepted, then never placed: its name is taken now
                minutes = round(queue_cap_s / 60)
                await self._no_capacity(
                    ctx,
                    f"no {_noun(where)}capacity on {where[0]} after {minutes} minutes in the "
                    f"queue: the {_job(where)} did not run",
                )
                return "queue_timeout"  # it existed: it may have run before the cancel
            return _Placed(outcome, spec)
        if not tries:  # the window ended before any try: no claim about any platform
            return "no_capacity"
        tried = " or ".join(dict.fromkeys(p for p, _ in places))
        await self._no_capacity(
            ctx, f"no {_noun(places[0])}capacity on {tried}: the {_job(places[0])} did not run"
        )
        return "no_capacity"

    async def _cpu_can_run(self, ctx: RunContext) -> bool:
        """Whether the run's experiment minutes left still hold a CPU attempt (A-47): the GPU
        try's minutes (a billed queue, a pull) count against the same per-run allowance."""
        spent = await ctx.budget.run_spend(ctx.run_id, SpendKind.GPU_MINUTES)
        left_s = int((ctx.limits.per_run.cpu_minutes - spent) * 60) - EXEC_MARGIN_S
        return left_s >= MIN_BUDGET_S

    async def _warn(self, ctx: RunContext, message: str) -> None:
        await ctx.emit(EventType.WARNING, {"code": "JOB_PREEMPTED", "message": message})

    async def run(self, ctx: RunContext) -> StepResult:
        active = await active_plan(ctx, self._json)  # A-47: the CPU plan after a fallback
        plan, where = active.plan, self._where(active.target)
        if active.fallback is not None and not await self._exists(
            f"runs/{ctx.run_id}/fallback_provisioned.json"
        ):  # resumed between the record and the transition: provision the CPU plan first
            return StepResult(next=StepName.PROVISIONING, output_ref=fallback_key(ctx.run_id))
        smoke = f"p2p-smoke-{run_slug(ctx.run_id)}-"  # left by a restart, before `finish`
        if not await stop_jobs(
            ctx, smoke, poll_s=self._poll_s, sleep=self._sleep, clock=self._clock
        ):
            raise P2PError(ErrorCode.TIMEOUT, "an earlier smoke job did not stop")
        # a GPU target with a CPU plan to fall back to gets a window to start in (A-47)
        can_fall_back = active.can_fall_back and self._config.cpu_exec is not None
        if active.fallback is not None:
            previous, attempts = None, [FALLBACK_ATTEMPT]
        else:
            previous = await self._retry_started(ctx, plan, where)
            attempts = list(range(1 if previous is None else 2, MAX_ATTEMPTS + 1))
        for attempt in attempts:
            manifest = await self._manifest(ctx, plan, attempt, where)
            if manifest is None and previous is None:
                raise BudgetExceeded("gpu_minutes")
            if manifest is None:
                assert previous is not None  # noqa: S101 (narrows the type)
                await self._warn(
                    ctx, "the GPU job was preempted; no retry: the GPU budget left is too small"
                )
                return await self._no_retry(ctx, *previous)
            preemptible = self._preemptible(attempt)
            if previous is not None:
                await self._warn(ctx, "the GPU job was preempted: retrying once on demand")
            bucket = await ctx.jobs.ensure_run_bucket(ctx.run_id)
            await ctx.jobs.ensure_gateway_up()
            tail = _LiveLog(ctx, self._storage, f"results/{attempt}/live.log")
            deadline = await self._window(ctx) if can_fall_back and attempt == 1 else None
            try:
                placed = await self._place(
                    ctx, manifest, bucket, preemptible, tail, where, deadline
                )
            except (BudgetExceeded, KillSwitchActive) as err:
                if previous is None:
                    raise
                await self._warn(ctx, f"the on-demand retry was not started ({err})")
                exec_jobs = f"p2p-exec-{run_slug(ctx.run_id)}-"  # a re-attached retry, cancelled
                if not await stop_jobs(
                    ctx, exec_jobs, poll_s=self._poll_s, sleep=self._sleep, clock=self._clock
                ):
                    kept, state, _ = previous  # it may still write: never read attempt 1
                    return await self._no_retry(ctx, kept, state, None)
                return await self._no_retry(ctx, *previous)
            if placed == "cancelled":
                return StepResult(next=RunStatus.CANCELLED)
            if not isinstance(placed, _Placed) and deadline is not None:
                return await self._fall_back(ctx, _REASON[placed], manifest)
            if not isinstance(placed, _Placed):  # no capacity, or still queued at the cap
                if previous is None:
                    return await self._done(
                        ctx, manifest, None, where=None, has_results=False, exit_reason="capacity"
                    )
                await self._warn(ctx, "the on-demand retry was not started (no GPU capacity)")
                if placed == "queue_timeout":  # it may have written: never read attempt 1
                    kept, state, _ = previous
                    return await self._no_retry(ctx, kept, state, None)
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
                    ctx,
                    manifest,
                    outcome.state,
                    where=ran_on,
                    has_results=True,
                    image=spec.image_digest_ref,
                )
            reason, wall = await self._exit_info(ctx, attempt)
            if (
                deadline is not None
                and outcome.state in (JobState.FAILED, JobState.CANCELLED)
                and not outcome.ran
                and reason is None
            ):  # never left the queue, nothing written: the platform failed it, or it was
                # cancelled from the queue (ours, before a restart) (A-47)
                why = (
                    FallbackReason.PLATFORM_ERROR
                    if outcome.state is JobState.FAILED
                    else FallbackReason.QUEUE_TIMEOUT
                )
                return await self._fall_back(ctx, why, manifest)
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
                    image=spec.image_digest_ref,
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
        self, ctx: RunContext, plan: Plan, where: _Where
    ) -> tuple[ExecManifest, JobState, str | None] | None:
        """The first attempt's outcome when its retry was already set up before a restart:
        attempt 1 is never judged again from a bucket the retry's code can write to."""
        try:
            await self._json(f"runs/{ctx.run_id}/exec_manifest_2.json")
            outcome = await self._json(f"runs/{ctx.run_id}/exec_preempted_1.json")
        except FileNotFoundError:
            return None
        first = await self._manifest(ctx, plan, 1, where)
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
            where=await self._ran_on(ctx, manifest.attempt, self._where(ExecTarget.GPU)),
            has_results=reason is not None,
            exit_reason="budget_timeout",
            image=self._where(ExecTarget.GPU).runner_image,
        )

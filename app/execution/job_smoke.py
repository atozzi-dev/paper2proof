"""JobSmokeExecutor: smoke runs as CPU Serverless Jobs in the `exec` subnet (no internet), the
fallback when Sandboxes is unavailable (execution-contracts §8/§10).

The workspace goes to the run bucket as one archive, `smoke/<iteration>/workspace.tar`,
whose sha256 travels in the injected manifest with a fresh nonce; the job runs
`images/runner/smoke.py` (same runner-cpu image), which checks the digest, and writes
`smoke/<iteration>/result.json`. A result counts only when the job SUCCEEDED and the result
echoes the nonce: earlier smoke jobs share the bucket and could have planted files.
Iterations are allocated after the highest smoke job the backend already has (a restarted
control plane never reuses a name), and a smoke job left running by a restart is cancelled.
"""

import asyncio
import json
import secrets
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from app.domain.contracts import ArchiveDigests
from app.execution.backend import JobKind, JobSpec, JobState, VolumeMount
from app.execution.config import ExecutionConfig
from app.execution.naming import job_name, run_slug
from app.execution.run_storage import RunStorage
from app.execution.smoke import (
    SmokeContractError,
    SmokeResult,
    SmokeUnavailable,
    pack_with_digest,
    parse_smoke_result,
    run_variants,
    validate_argv,
    validate_timeout,
    validate_workspace,
)
from app.models.pricing import CloudPrices
from app.orchestrator.context import RunContext
from app.orchestrator.steps.jobs import run_job, utcnow

SMOKE_PLATFORM, SMOKE_PRESET = "cpu-d3", "4vcpu-16gb"
SMOKE_SETUP_S = 180  # job start, image pull and extraction before the script runs (S-12)
MAX_RESULT_BYTES = 256 * 1024


def _parse(raw: bytes, nonce: str) -> SmokeResult:
    try:
        data: Any = json.loads(raw)
    except (ValueError, RecursionError):
        raise SmokeUnavailable("the smoke job left an unreadable result") from None
    if not isinstance(data, dict) or data.get("nonce") != nonce:
        raise SmokeUnavailable("the smoke result does not belong to this job")
    try:
        return parse_smoke_result(data)
    except SmokeContractError as err:
        raise SmokeUnavailable(f"the smoke job left an invalid result: {err}") from None


class JobSmokeExecutor:
    def __init__(
        self,
        ctx: RunContext,
        *,
        archives: ArchiveDigests,
        storage: RunStorage,
        config: ExecutionConfig,
        cloud: CloudPrices,
        poll_s: float = 5,
        clock: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._ctx = ctx
        self._archives = archives  # what the smoke runner may extract, with digests
        self._storage = storage
        self._config = config
        self._usd_per_hour = cloud.hourly(SMOKE_PLATFORM, SMOKE_PRESET)
        self._poll_s = poll_s
        self._clock = clock
        self._sleep = sleep
        self._base: dict[str, dict[str, bytes]] = {}
        self._last = 0
        self._lock = asyncio.Lock()

    async def prepare(self, run_id: str, files: dict[str, bytes]) -> str:
        if run_id != self._ctx.run_id:
            raise SmokeContractError("one executor per run")
        self._base[run_id] = validate_workspace(files)
        return run_id

    async def _next_iteration(self, run_id: str) -> int:
        prefix = f"p2p-smoke-{run_slug(run_id)}-"
        async with self._lock:
            listed = await self._ctx.jobs.list_jobs(prefix)
            for handle, state, _ in listed:
                suffix = handle.name.removeprefix(prefix)
                orphaned = suffix.isdigit() and int(suffix) > self._last  # not started here
                if orphaned and not state.is_terminal:
                    await self._ctx.jobs.cancel(handle)
            existing = [
                int(handle.name.removeprefix(prefix))
                for handle, _, _ in listed
                if handle.name.removeprefix(prefix).isdigit()
            ]
            self._last = max([self._last, *existing]) + 1
            if self._last > 9999:
                raise SmokeUnavailable("smoke iteration limit reached")
            return self._last

    async def run(
        self, env_id: str, argv: list[str], files: dict[str, bytes], timeout_s: int
    ) -> SmokeResult:
        argv = validate_argv(argv)
        workspace = validate_workspace({**self._base.get(env_id, {}), **validate_workspace(files)})
        validate_timeout(timeout_s)
        ctx = self._ctx
        archive, digest = await ctx.run_cpu(pack_with_digest, workspace)
        iteration = await self._next_iteration(env_id)
        nonce = secrets.token_hex(16)
        await self._storage.put_bytes(env_id, f"smoke/{iteration}/workspace.tar", archive)
        manifest = {
            "schema": "p2p.smoke/v1",
            "run_id": env_id,
            "iteration": iteration,
            "argv": argv,
            "timeout_s": timeout_s,
            "workspace_sha256": digest,
            "nonce": nonce,
            "archives": self._archives.model_dump(mode="json"),
        }
        bucket = await ctx.jobs.ensure_run_bucket(env_id)
        spec = JobSpec(
            name=job_name(JobKind.SMOKE, env_id, iteration),
            kind=JobKind.SMOKE,
            image_digest_ref=self._config.runner_cpu_image,
            platform=SMOKE_PLATFORM,
            preset=SMOKE_PRESET,
            disk_gib=30,
            subnet_id=self._config.subnet_exec,
            volumes=[
                VolumeMount(source=bucket, mount_path="/mnt/run", mode="rw"),
                VolumeMount(source="p2p-cache", mount_path="/mnt/cache", mode="ro"),
            ],
            inject_files={"/etc/p2p/smoke.json": json.dumps(manifest).encode()},
            env={"PYTHONUNBUFFERED": "1"},
            preemptible=False,
            watchdog_s=timeout_s + SMOKE_SETUP_S,
        )
        started = time.monotonic()
        outcome = await run_job(
            ctx,
            spec,
            usd_per_hour=self._usd_per_hour,
            poll_s=self._poll_s,
            clock=self._clock,
            sleep=self._sleep,
        )
        if outcome.run_cancelled:
            raise SmokeUnavailable("the run was cancelled")
        if outcome.timed_out:
            raise SmokeUnavailable(
                f"the smoke job did not finish within {timeout_s + SMOKE_SETUP_S} s"
            )
        if outcome.state is not JobState.SUCCEEDED:
            raise SmokeUnavailable(f"the smoke job ended {outcome.state.value}")
        try:
            raw = await self._storage.get_bytes(
                env_id, f"smoke/{iteration}/result.json", max_bytes=MAX_RESULT_BYTES
            )
        except ValueError:
            raw = None
        if not raw:
            raise SmokeUnavailable("the smoke job succeeded without a result")
        result = await ctx.run_cpu(_parse, raw, nonce)
        if result.duration_s == 0.0:
            result = result.model_copy(update={"duration_s": time.monotonic() - started})
        return result

    async def run_branches(
        self, env_id: str, variants: list[dict[str, bytes]], argv: list[str], timeout_s: int
    ) -> list[SmokeResult]:
        return await run_variants(self, env_id, variants, argv, timeout_s)

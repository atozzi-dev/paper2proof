"""PROVISIONING: validate packages (policy), then a CPU job with allowlisted egress prepares
the overlay, the repository archive and the datasets in the run bucket (execution-contracts §3-5).
The manifest is injected (read-only, <= 64 KiB) instead of being written to the bucket."""

import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable

from app.domain.discovery import Discovery
from app.domain.errors import P2PError
from app.domain.events import EventType
from app.domain.models import ErrorCode, PackageReq, Plan, RunStatus, StepName
from app.execution.backend import JobKind, JobSpec, JobState, VolumeMount
from app.execution.config import ExecutionConfig
from app.execution.naming import job_name
from app.execution.package_policy import PackagePolicy, PolicyContext, parse_repo_requirements
from app.execution.run_storage import RunStorage
from app.models.pricing import CloudPrices
from app.orchestrator.context import RunContext
from app.orchestrator.cost_estimate import PROVISION_PLATFORM, PROVISION_PRESET
from app.orchestrator.fsm import StepResult
from app.orchestrator.steps.common import output_of
from app.orchestrator.steps.jobs import run_job
from app.storage.artifacts import ArtifactStore

WATCHDOG_S = 20 * 60  # execution-contracts §8
SMOKE_ROWS = 512


class ProvisioningStep:
    name = StepName.PROVISIONING

    def __init__(
        self,
        *,
        policy: PackagePolicy,
        storage: RunStorage,
        artifacts: ArtifactStore,
        config: ExecutionConfig,
        cloud: CloudPrices,
        poll_s: float = 15,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._policy = policy
        self._storage = storage
        self._artifacts = artifacts
        self._config = config
        self._cloud = cloud
        self._poll_s = poll_s
        self._clock = clock
        self._sleep = sleep

    async def _json(self, key: str) -> dict[str, object]:
        data: dict[str, object] = await asyncio.to_thread(self._artifacts.get_json, key)
        return data

    async def _repo_requirements(self, ctx: RunContext) -> set[str]:
        """Requirement files the provisioner found in the clone during an earlier attempt."""
        try:
            report = await self._json(f"runs/{ctx.run_id}/provision_report.json")
        except FileNotFoundError:
            return set()
        files = report.get("requirement_files")
        return parse_repo_requirements(files) if isinstance(files, dict) else set()

    async def _packages(self, ctx: RunContext, plan: Plan) -> list[PackageReq]:
        repo_requirements = await self._repo_requirements(ctx)
        planned = await self._policy.validate(
            plan.packages, PolicyContext(repo_requirements=repo_requirements)
        )
        for rejected in planned.rejected:
            why = f"{rejected.package.name}: {rejected.reason.value} {rejected.detail}"
            await ctx.emit(EventType.WARNING, {"code": "PACKAGE_REJECTED", "message": why.strip()})
        packages = planned.accepted
        if ctx.attempt > 1:  # back from SMOKE_TESTING with a request_packages call
            request = await self._json(await output_of(ctx, StepName.SMOKE_TESTING))
            raw = request.get("requested_packages")
            requested = [PackageReq.model_validate(p) for p in raw] if isinstance(raw, list) else []
            decision = await self._policy.validate(
                requested,
                PolicyContext(repo_requirements=repo_requirements, requests_so_far=ctx.attempt - 2),
            )
            if requested and not decision.accepted:
                reasons = ", ".join(
                    f"{d.package.name}: {d.reason.value}" for d in decision.rejected
                )
                raise P2PError(
                    ErrorCode.PACKAGE_REJECTED, f"requested packages rejected ({reasons})"
                )
            packages = [*packages, *decision.accepted]
        return packages

    async def run(self, ctx: RunContext) -> StepResult:
        plan = Plan.model_validate(
            (await self._json(await output_of(ctx, StepName.PLANNING)))["plan"]
        )
        discovery = Discovery.model_validate(
            await self._json(await output_of(ctx, StepName.DISCOVERING))
        )
        packages = await self._packages(ctx, plan)
        manifest = {
            "schema": "p2p.provision/v1",
            "run_id": ctx.run_id,
            "packages": [p.name + (p.specifier or "") for p in packages],
            "repo": {"url": discovery.repo.url, "ref": None} if discovery.repo else None,
            "datasets": [d.model_dump(mode="json") for d in plan.datasets],
            "smoke_subset": (
                {"dataset": plan.datasets[0].id, "rows": SMOKE_ROWS} if plan.datasets else None
            ),
            "hf_token_secret": None,
        }
        bucket = await ctx.jobs.ensure_run_bucket(ctx.run_id)
        await ctx.jobs.ensure_gateway_up()
        spec = JobSpec(
            name=job_name(JobKind.PROVISION, ctx.run_id, ctx.attempt),
            kind=JobKind.PROVISION,
            image_digest_ref=self._config.provisioner_image,
            platform=PROVISION_PLATFORM,
            preset=PROVISION_PRESET,
            disk_gib=50,
            subnet_id=self._config.subnet_provision,
            volumes=[
                VolumeMount(source=bucket, mount_path="/mnt/run", mode="rw"),
                VolumeMount(source="p2p-cache", mount_path="/mnt/cache", mode="rw"),
            ],
            inject_files={"/etc/p2p/provision.json": json.dumps(manifest).encode()},
            env={"PYTHONUNBUFFERED": "1"},
            preemptible=False,
            watchdog_s=WATCHDOG_S,
        )
        outcome = await run_job(
            ctx,
            spec,
            usd_per_hour=self._cloud.hourly(PROVISION_PLATFORM, PROVISION_PRESET),
            poll_s=self._poll_s,
            clock=self._clock,
            sleep=self._sleep,
        )
        if outcome.run_cancelled:
            return StepResult(next=RunStatus.CANCELLED)
        if outcome.timed_out:
            raise P2PError(ErrorCode.TIMEOUT, "provisioning took longer than 20 minutes")
        if outcome.state is not JobState.SUCCEEDED:
            error = await self._storage.get_bytes(
                ctx.run_id, "staging/provision_error.json", max_bytes=8192
            )
            reason = "provisioning failed"
            if error:  # the provisioner's own message: jobs hold no secrets
                with contextlib.suppress(ValueError, AttributeError):
                    reason = str(json.loads(error).get("error", reason))[:500]
            raise P2PError(ErrorCode.PROVISION_FAILED, reason)
        report = await self._storage.get_bytes(
            ctx.run_id, "staging/provision_report.json", max_bytes=512 * 1024
        )
        key = f"runs/{ctx.run_id}/provision_report.json"
        try:
            parsed = json.loads(report) if report else {}
        except ValueError:
            parsed = {}
        await asyncio.to_thread(
            self._artifacts.put_json, key, parsed if isinstance(parsed, dict) else {}
        )
        return StepResult(next=StepName.SMOKE_TESTING, output_ref=key)

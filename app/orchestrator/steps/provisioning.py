"""PROVISIONING: validate packages (policy), then a CPU job with allowlisted egress prepares
the overlay, the repository archive and the datasets in the run bucket (execution-contracts §3-5).
The manifest is injected (read-only, <= 64 KiB) instead of being written to the bucket."""

import asyncio
import contextlib
import json
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

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
from app.orchestrator.steps.jobs import run_job, utcnow
from app.storage.artifacts import ArtifactStore

WATCHDOG_S = 20 * 60  # execution-contracts §8
SMOKE_ROWS = 512
RUNNER_PYTHON = "3.12"  # what the runner image runs: overlays are only valid for that
RUNNER_PLATFORM = "x86_64-manylinux_2_28"
STAGING_OUTPUTS = ("staging/provision_error.json", "staging/provision_report.json")
DEADLINE_MARGIN = timedelta(minutes=2)  # the provisioner writes its error file before the cancel
MAX_REPORT_BYTES = 512 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_DATASET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}(?:__[A-Za-z0-9][A-Za-z0-9_.-]{0,95})?$")
_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")


def parse_report(raw: bytes, run_id: str) -> dict[str, Any] | None:
    """The provision report was written next to untrusted inputs (repo, datasets): strict
    shape, or nothing. Pure, for the CPU pool."""
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or data.get("schema") != "p2p.provision-report/v1":
        return None
    if data.get("run_id") != run_id or not isinstance(data.get("overlay_cache_hit"), bool):
        return None
    for field in ("overlay_cache_key", "overlay_sha256"):
        value = data.get(field)
        if not isinstance(value, str) or not _SHA256.fullmatch(value):
            return None
    files = data.get("requirement_files", {})
    if not isinstance(files, dict) or len(files) > 10:
        return None
    if not all(
        isinstance(k, str) and _FILE.fullmatch(k) and isinstance(v, str) for k, v in files.items()
    ):
        return None
    commit, lock = data.get("repo_commit"), data.get("lock_sha256")
    snapshot = data.get("repo_files_sha256")
    datasets = data.get("datasets")
    return {  # whitelisted fields only: later steps and bundles read this artifact
        "schema": "p2p.provision-report/v1",
        "run_id": run_id,
        "overlay_cache_key": data["overlay_cache_key"],
        "overlay_cache_hit": data["overlay_cache_hit"],
        "overlay_sha256": data["overlay_sha256"],
        "lock_sha256": lock if isinstance(lock, str) and _SHA256.fullmatch(lock) else None,
        "repo": data.get("repo") is True,
        "repo_commit": commit if isinstance(commit, str) and _COMMIT.fullmatch(commit) else None,
        "requirement_files": files,
        "datasets": [
            d
            for d in (datasets if isinstance(datasets, list) else [])[:3]
            if isinstance(d, str) and _DATASET.fullmatch(d)
        ],
        "smoke": data.get("smoke") is True,
        "repo_files_sha256": (
            snapshot if isinstance(snapshot, str) and _SHA256.fullmatch(snapshot) else None
        ),
    }


def _error_message(raw: bytes | None) -> str:
    reason = "provisioning failed"
    if raw:  # the provisioner's own message: jobs hold no secrets
        with contextlib.suppress(ValueError, AttributeError, RecursionError):
            reason = str(json.loads(raw).get("error", reason))[:500]
    return reason


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
        clock: Callable[[], datetime] = utcnow,
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

    async def _repo_requirements(self, ctx: RunContext, discovery: Discovery) -> set[str]:
        """Requirement files the provisioner found in the clone during an earlier attempt. Only
        an official repository (linked by the authors) can exempt names from the typosquat and
        age checks: a repository found by search could be anybody's."""
        if discovery.repo is None or not discovery.repo.official:
            return set()
        try:
            report = await self._json(f"runs/{ctx.run_id}/provision_report.json")
        except FileNotFoundError:
            return set()
        raw = report.get("requirement_files")
        if not isinstance(raw, dict):
            return set()
        files = {str(k): v for k, v in raw.items() if isinstance(v, str)}
        return await ctx.run_cpu(parse_repo_requirements, files)

    async def _packages(
        self, ctx: RunContext, plan: Plan, discovery: Discovery
    ) -> list[PackageReq]:
        repo_requirements = await self._repo_requirements(ctx, discovery)
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
        packages = await self._packages(ctx, plan, discovery)
        policy = self._policy.config
        runner_digest = self._config.runner_image.rsplit("@", 1)[1]
        provisioner_digest = self._config.provisioner_image.rsplit("@", 1)[1]
        name = job_name(JobKind.PROVISION, ctx.run_id, ctx.attempt)
        bucket = await ctx.jobs.ensure_run_bucket(ctx.run_id)
        await ctx.jobs.ensure_gateway_up()  # may cold-start the VM: anchor the deadline after it
        existing = await ctx.store.get_job(name)
        listed = [t for h, _, t in await ctx.jobs.list_jobs(name) if h.name == name]
        if existing is not None:
            submitted = existing.created_at
        elif listed:  # submitted before a crash, never recorded
            submitted = listed[0]
        else:
            submitted = self._clock()
        deadline = submitted + timedelta(seconds=WATCHDOG_S) - DEADLINE_MARGIN
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
            "cache_salt": (
                f"{runner_digest}+{provisioner_digest}+py{RUNNER_PYTHON}+{RUNNER_PLATFORM}"
            ),
            "policy": {"denylist": sorted(policy.denylist), "popular": sorted(policy.popular)},
            "trusted_overlays": await ctx.store.trusted_overlays(),
            "deadline_unix": deadline.timestamp(),
        }
        if existing is None and not listed:  # a fresh job: earlier outputs are stale
            for key in STAGING_OUTPUTS:
                await self._storage.delete(ctx.run_id, key)
        spec = JobSpec(
            name=name,
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
            started=submitted,
        )
        if outcome.run_cancelled:
            return StepResult(next=RunStatus.CANCELLED)
        if outcome.timed_out:
            raise P2PError(ErrorCode.TIMEOUT, "provisioning took longer than 20 minutes")
        if outcome.state is not JobState.SUCCEEDED:
            try:
                error = await self._storage.get_bytes(
                    ctx.run_id, "staging/provision_error.json", max_bytes=8192
                )
            except ValueError:
                error = None
            raise P2PError(ErrorCode.PROVISION_FAILED, _error_message(error))
        try:
            raw = await self._storage.get_bytes(
                ctx.run_id, "staging/provision_report.json", max_bytes=MAX_REPORT_BYTES
            )
        except ValueError:
            raw = None
        report = await ctx.run_cpu(parse_report, raw, ctx.run_id) if raw else None
        if report is None:
            raise P2PError(ErrorCode.PROVISION_FAILED, "the provision job left no valid report")
        if not report["overlay_cache_hit"]:  # built by this job: trusted from now on
            await ctx.store.trust_overlay(
                report["overlay_cache_key"], report["overlay_sha256"], ctx.run_id
            )
        key = f"runs/{ctx.run_id}/provision_report.json"
        await asyncio.to_thread(self._artifacts.put_json, key, report)
        return StepResult(next=StepName.SMOKE_TESTING, output_ref=key)

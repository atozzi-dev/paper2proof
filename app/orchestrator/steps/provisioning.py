"""PROVISIONING: validate packages (policy), then two CPU jobs with allowlisted egress prepare the
run bucket, one after the other (execution-contracts §3-5). The provision job locks and packs the
overlay: it is the only job that writes the shared cache, and its inputs are the package list and
PyPI wheels. The fetch job clones the repository and downloads the datasets (untrusted bytes:
parquet parsing, git) without the cache mount. The overlay digest is read and trusted before the
fetch job exists, so nothing those bytes can do reaches the cache or the trust table (A-06).
Manifests are injected (read-only, <= 64 KiB) instead of being written to the bucket."""

import asyncio
import contextlib
import json
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from app.domain.discovery import Discovery
from app.domain.errors import P2PError
from app.domain.events import EventType
from app.domain.models import ErrorCode, PackageReq, Plan, PlanMode, RunStatus, StepName
from app.execution.backend import CACHE_BUCKET, JobKind, JobSpec, JobState, VolumeMount
from app.execution.config import ExecutionConfig
from app.execution.naming import job_name, run_slug
from app.execution.package_policy import PackagePolicy, PolicyContext, parse_repo_requirements
from app.execution.run_storage import RunStorage
from app.models.pricing import CloudPrices
from app.orchestrator.context import RunContext
from app.orchestrator.cost_estimate import PROVISION_PLATFORM, PROVISION_PRESET
from app.orchestrator.fsm import StepResult
from app.orchestrator.steps.common import output_of
from app.orchestrator.steps.jobs import run_job, stop_jobs, utcnow
from app.storage.artifacts import ArtifactStore

WATCHDOG_S = 20 * 60  # both jobs together (execution-contracts §8)
MIN_FETCH_WORK_S = 300  # a cold start plus some work before the deadline, or a timeout now
SMOKE_ROWS = 512
RUNNER_PYTHON = "3.12"  # what the runner image runs: overlays are only valid for that
RUNNER_PLATFORM = "x86_64-manylinux_2_28"
ERROR_FILE = "staging/provision_error.json"
PACKAGES_REPORT, FETCH_REPORT = "staging/packages_report.json", "staging/fetch_report.json"
DEADLINE_MARGIN = timedelta(minutes=2)  # the provisioner writes its error file before the cancel
MAX_REPORT_BYTES = 512 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_DATASET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}(?:__[A-Za-z0-9][A-Za-z0-9_.-]{0,95})?$")
_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")

Parser = Callable[[bytes, str], "dict[str, Any] | None"]


def _load(raw: bytes, run_id: str, schema: str) -> dict[str, Any] | None:
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or data.get("schema") != schema:
        return None
    return data if data.get("run_id") == run_id else None


def parse_packages_report(raw: bytes, run_id: str) -> dict[str, Any] | None:
    """The provision job's report: the overlay digest that may become trusted. Strict shape, or
    nothing; whitelisted fields only. Pure, for the CPU pool."""
    data = _load(raw, run_id, "p2p.packages-report/v1")
    if data is None or not isinstance(data.get("overlay_cache_hit"), bool):
        return None
    for field in ("overlay_cache_key", "overlay_sha256"):
        if _hex(data.get(field)) is None:
            return None
    return {
        "overlay_cache_key": data["overlay_cache_key"],
        "overlay_cache_hit": data["overlay_cache_hit"],
        "overlay_sha256": data["overlay_sha256"],
        "lock_sha256": _hex(data.get("lock_sha256")),
    }


def parse_fetch_report(raw: bytes, run_id: str) -> dict[str, Any] | None:
    """The fetch job's report, written next to untrusted inputs (repo, datasets): strict shape,
    or nothing; whitelisted fields only, never an overlay field. Pure, for the CPU pool."""
    data = _load(raw, run_id, "p2p.fetch-report/v1")
    if data is None:
        return None
    files = data.get("requirement_files", {})
    if not isinstance(files, dict) or len(files) > 10:
        return None
    if not all(
        isinstance(k, str) and _FILE.fullmatch(k) and isinstance(v, str) for k, v in files.items()
    ):
        return None
    commit, archives = data.get("repo_commit"), data.get("data_archives")
    datasets = data.get("datasets")
    return {  # later steps and bundles read these (through the provision report artifact)
        "repo": data.get("repo") is True,
        "repo_commit": commit if isinstance(commit, str) and _COMMIT.fullmatch(commit) else None,
        "requirement_files": files,
        "datasets": [
            d
            for d in (datasets if isinstance(datasets, list) else [])[:3]
            if isinstance(d, str) and _DATASET.fullmatch(d)
        ],
        "smoke": data.get("smoke") is True,
        "repo_files_sha256": _hex(data.get("repo_files_sha256")),
        "repo_sha256": _hex(data.get("repo_sha256")),
        "data_archives": [
            {"name": a["name"], "sha256": a["sha256"]}
            for a in (archives if isinstance(archives, list) else [])[:4]
            if isinstance(a, dict)
            and isinstance(a.get("name"), str)
            and _DATASET.fullmatch(a["name"])
            and _hex(a.get("sha256"))
        ],
    }


def _hex(value: object) -> str | None:
    return value if isinstance(value, str) and _SHA256.fullmatch(value) else None


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
        smoke = f"p2p-smoke-{run_slug(ctx.run_id)}-"  # left by a restart: it mounts the bucket rw
        if not await stop_jobs(
            ctx, smoke, poll_s=self._poll_s, sleep=self._sleep, clock=self._clock
        ):
            raise P2PError(ErrorCode.TIMEOUT, "an earlier smoke job did not stop")
        packages = await self._packages(ctx, plan, discovery)
        bucket = await ctx.jobs.ensure_run_bucket(ctx.run_id)
        await ctx.jobs.ensure_gateway_up()  # may cold-start the VM: anchor the deadline after it
        built_key = f"runs/{ctx.run_id}/packages_report_{ctx.attempt}.json"
        built = await self._stored(built_key)
        if built is None:  # a stored copy means the fetch job may be running: never re-read
            built = await self._build_overlay(ctx, packages, bucket)
            if built is None:
                return StepResult(next=RunStatus.CANCELLED)
            await asyncio.to_thread(self._artifacts.put_json, built_key, built)
        overlay = await self._validated(ctx, built, parse_packages_report)
        deadline_unix = built.get("deadline_unix")
        if isinstance(deadline_unix, bool) or not isinstance(deadline_unix, int | float):
            raise P2PError(
                ErrorCode.PROVISION_FAILED, "the stored provisioning deadline is invalid"
            )
        deadline = datetime.fromtimestamp(deadline_unix, UTC)
        fetch_key = f"runs/{ctx.run_id}/fetch_report.json"
        # The repository and the datasets don't change between attempts (the plan doesn't):
        # a later attempt, or a resumed one, uses the copy stored by this step.
        fetched = await self._stored(fetch_key)
        if fetched is None:
            fetched = await self._fetch(ctx, plan, discovery, bucket, deadline)
            if fetched is None:
                return StepResult(next=RunStatus.CANCELLED)
            await asyncio.to_thread(self._artifacts.put_json, fetch_key, fetched)
        files = await self._validated(ctx, fetched, parse_fetch_report)
        report = {"schema": "p2p.provision-report/v1", "run_id": ctx.run_id, **overlay, **files}
        key = f"runs/{ctx.run_id}/provision_report.json"
        await asyncio.to_thread(self._artifacts.put_json, key, report)
        return StepResult(next=StepName.SMOKE_TESTING, output_ref=key)

    async def _stored(self, key: str) -> dict[str, Any] | None:
        """A report this step stored earlier, or None."""
        try:
            data: dict[str, Any] = await self._json(key)
        except FileNotFoundError:
            return None
        return data

    async def _validated(
        self, ctx: RunContext, document: dict[str, Any], parse: Parser
    ) -> dict[str, Any]:
        """The whitelisted fields of a stored report; an invalid one fails closed (it is never
        replaced by a fresh read of the bucket)."""
        fields = await ctx.run_cpu(parse, json.dumps(document).encode(), ctx.run_id)
        if fields is None:
            raise P2PError(ErrorCode.PROVISION_FAILED, "a stored provisioning report is invalid")
        return fields

    async def _build_overlay(
        self, ctx: RunContext, packages: list[PackageReq], bucket: str
    ) -> dict[str, Any] | None:
        """The provision job: the overlay digest that may become trusted. Its report is read
        back from the run bucket, so no other job of the run may be running meanwhile: smoke
        jobs were stopped, leftover fetch and exec jobs are stopped here, and this run's fetch
        job is submitted only after the digest is stored. None when the run was cancelled."""
        for kind in (JobKind.FETCH, JobKind.EXEC):  # exec can't run yet: a cheap extra guard
            prefix = f"p2p-{kind.value}-{run_slug(ctx.run_id)}-"
            if not await stop_jobs(
                ctx, prefix, poll_s=self._poll_s, sleep=self._sleep, clock=self._clock
            ):
                raise P2PError(ErrorCode.TIMEOUT, f"an earlier {kind.value} job did not stop")
        policy = self._policy.config
        runner_digest = self._config.runner_image.rsplit("@", 1)[1]
        provisioner_digest = self._config.provisioner_image.rsplit("@", 1)[1]
        name = job_name(JobKind.PROVISION, ctx.run_id, ctx.attempt)
        submitted, fresh = await self._submitted(ctx, name)
        deadline = submitted + timedelta(seconds=WATCHDOG_S) - DEADLINE_MARGIN
        manifest = {
            "schema": "p2p.provision/v2",
            "phase": "packages",
            "run_id": ctx.run_id,
            "packages": [p.name + (p.specifier or "") for p in packages],
            "cache_salt": (
                f"{runner_digest}+{provisioner_digest}+py{RUNNER_PYTHON}+{RUNNER_PLATFORM}"
            ),
            "policy": {"denylist": sorted(policy.denylist), "popular": sorted(policy.popular)},
            "trusted_overlays": await ctx.store.trusted_overlays(),
            "deadline_unix": deadline.timestamp(),
        }
        volumes = [
            VolumeMount(source=bucket, mount_path="/mnt/run", mode="rw"),
            VolumeMount(source=CACHE_BUCKET, mount_path="/mnt/cache", mode="rw"),
        ]
        spec = self._spec(name, JobKind.PROVISION, volumes, manifest, WATCHDOG_S)
        report = await self._run_phase(
            ctx, spec, submitted, fresh, deadline, PACKAGES_REPORT, parse_packages_report
        )
        if report is None:
            return None
        if not report["overlay_cache_hit"]:  # built by this job: trusted from now on
            await ctx.store.trust_overlay(
                report["overlay_cache_key"], report["overlay_sha256"], ctx.run_id
            )
        return {
            "schema": "p2p.packages-report/v1",
            "run_id": ctx.run_id,
            **report,
            "deadline_unix": deadline.timestamp(),  # the fetch job shares the budget
        }

    async def _fetch(
        self, ctx: RunContext, plan: Plan, discovery: Discovery, bucket: str, deadline: datetime
    ) -> dict[str, Any] | None:
        """The fetch job, with what is left of the provisioning budget. Only the run bucket is
        mounted: what the repository or a dataset could do stays in this run. None when the
        run was cancelled."""
        name = job_name(JobKind.FETCH, ctx.run_id, ctx.attempt)
        submitted, fresh = await self._submitted(ctx, name)
        left_s = int((deadline + DEADLINE_MARGIN - submitted).total_seconds())
        if fresh and (deadline - submitted).total_seconds() < MIN_FETCH_WORK_S:
            # never a billed cold start that can only end at its deadline
            raise P2PError(ErrorCode.TIMEOUT, "provisioning took longer than 20 minutes")
        manifest = {
            "schema": "p2p.provision/v2",
            "phase": "fetch",
            "run_id": ctx.run_id,
            "repo": (  # a from-scratch plan never uses it: no clone, no 1 GiB failure
                {"url": discovery.repo.url, "ref": None}
                if discovery.repo and plan.mode is PlanMode.REPO_FIRST
                else None
            ),
            "datasets": [d.model_dump(mode="json") for d in plan.datasets],
            "smoke_subset": (
                {"dataset": plan.datasets[0].id, "rows": SMOKE_ROWS} if plan.datasets else None
            ),
            "hf_token_secret": None,
            "deadline_unix": deadline.timestamp(),
        }
        volumes = [VolumeMount(source=bucket, mount_path="/mnt/run", mode="rw")]
        spec = self._spec(name, JobKind.FETCH, volumes, manifest, max(left_s, 1))
        report = await self._run_phase(
            ctx, spec, submitted, fresh, deadline, FETCH_REPORT, parse_fetch_report
        )
        if report is None:
            return None
        return {"schema": "p2p.fetch-report/v1", "run_id": ctx.run_id, **report}

    async def _submitted(self, ctx: RunContext, name: str) -> tuple[datetime, bool]:
        """When the job was first submitted, and whether it is a new one."""
        existing = await ctx.store.get_job(name)
        if existing is not None:
            return existing.created_at, False
        listed = [t for h, _, t in await ctx.jobs.list_jobs(name) if h.name == name]
        if listed:  # submitted before a crash, never recorded
            return listed[0], False
        return self._clock(), True

    def _spec(
        self,
        name: str,
        kind: JobKind,
        volumes: list[VolumeMount],
        manifest: dict[str, Any],
        watchdog_s: int,
    ) -> JobSpec:
        return JobSpec(
            name=name,
            kind=kind,
            image_digest_ref=self._config.provisioner_image,
            platform=PROVISION_PLATFORM,
            preset=PROVISION_PRESET,
            disk_gib=50,
            subnet_id=self._config.subnet_provision,
            volumes=volumes,
            inject_files={"/etc/p2p/provision.json": json.dumps(manifest).encode()},
            env={"PYTHONUNBUFFERED": "1"},
            preemptible=False,
            watchdog_s=watchdog_s,
        )

    async def _run_phase(
        self,
        ctx: RunContext,
        spec: JobSpec,
        submitted: datetime,
        fresh: bool,
        deadline: datetime,
        report_key: str,
        parse: Parser,
    ) -> dict[str, Any] | None:
        """Run one job of the provisioner image and return its validated report."""
        if fresh:  # earlier outputs are stale
            for key in (ERROR_FILE, report_key):
                await self._storage.delete(ctx.run_id, key)
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
            return None
        if outcome.timed_out:
            raise P2PError(ErrorCode.TIMEOUT, "provisioning took longer than 20 minutes")
        if outcome.state is not JobState.SUCCEEDED:
            try:
                error = await self._storage.get_bytes(ctx.run_id, ERROR_FILE, max_bytes=8192)
            except ValueError:
                error = None
            if self._clock() >= deadline:  # the provisioner gave up at its deadline (our clock)
                raise P2PError(
                    ErrorCode.TIMEOUT, f"provisioning reached its deadline: {_error_message(error)}"
                )
            raise P2PError(ErrorCode.PROVISION_FAILED, _error_message(error))
        try:
            raw = await self._storage.get_bytes(ctx.run_id, report_key, max_bytes=MAX_REPORT_BYTES)
        except ValueError:
            raw = None
        report = await ctx.run_cpu(parse, raw, ctx.run_id) if raw else None
        if report is None:
            what = "provision" if spec.kind is JobKind.PROVISION else "fetch"
            raise P2PError(ErrorCode.PROVISION_FAILED, f"the {what} job left no valid report")
        return report

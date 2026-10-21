"""JobBackend on Nebius AI Cloud: Serverless Jobs, per-run buckets and the egress gateway VM, all
in project p2p-sandbox with the sa-job-launcher identity (docs/specs/execution-contracts.md §8-§9).

Every Nebius SDK call of the execution plane is in this module, behind small client protocols, so
the fixes after the spikes stay here. Assumptions not yet verified are marked:
[S-4] job API semantics, [S-5] gateway and subnets, [S-11] buckets and mounts.
"""

import asyncio
import contextlib
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from nebius.aio.request_status import RequestStatus
from nebius.aio.service_error import RequestError
from nebius.api.nebius.ai.v1 import (
    CancelJobRequest,
    CreateJobRequest,
    DeleteJobRequest,
    FollowsSpotPriceSpec,
    GetJobByNameRequest,
    GetJobRequest,
    Job,
    JobServiceClient,
    JobStatus,
    ListJobsRequest,
    ListJobsResponse,
    PricingModelSpec,
)
from nebius.api.nebius.ai.v1 import JobSpec as SdkJobSpec
from nebius.api.nebius.common.v1 import ResourceMetadata
from nebius.api.nebius.compute.v1 import (
    DiskSpec,
    GetInstanceRequest,
    Instance,
    InstanceServiceClient,
    InstanceStatus,
    StartInstanceRequest,
    StopInstanceRequest,
)
from nebius.api.nebius.storage.v1 import (
    Bucket,
    BucketServiceClient,
    BucketSpec,
    BucketStatus,
    CreateBucketRequest,
    DeleteBucketRequest,
    GetBucketByNameRequest,
    ListBucketsRequest,
    ListBucketsResponse,
)
from nebius.sdk import SDK
from pydantic import SecretStr

from app.execution.backend import CapacityError, JobHandle, JobSpec, JobState, QuotaExceeded
from app.execution.naming import bucket_name
from app.execution.s3 import S3Client, S3RunStorage, empty_bucket, make_s3_client

log = logging.getLogger(__name__)

GIB = 1024**3
PLATFORM_TIMEOUT = timedelta(hours=1)  # the minimum the platform allows (§8)
RUN_BUCKET_PREFIX = "p2p-run-"
JOB_PREFIX = "p2p-"
RUN_BUCKET_MAX_BYTES = 200 * GIB  # caps what untrusted code can store in a run bucket [S-11]
PAGE_SIZE = 100
MAX_PAGES = 1000  # a listing that never ends is a bug, not a project


class JobBackendError(RuntimeError):
    """A Nebius request failed in a way the orchestrator can't recover by retrying."""


class GatewayUnavailable(JobBackendError):
    """The egress gateway VM could not be brought to RUNNING."""


def _next_token(token: str, seen: set[str], what: str) -> str:
    if token in seen or len(seen) >= MAX_PAGES:
        raise JobBackendError(f"{what}: listing does not end (page token repeated or too many)")
    seen.add(token)
    return token


# --- SDK seams: the subset of each service client this module uses --------------------------


class Operation(Protocol):
    @property
    def resource_id(self) -> str: ...
    def successful(self) -> bool: ...
    def done(self) -> bool: ...
    def status(self) -> RequestStatus | None: ...
    def wait(self) -> Awaitable[None]: ...


class JobsClient(Protocol):
    def get(self, request: GetJobRequest) -> Awaitable[Job]: ...
    def get_by_name(self, request: GetJobByNameRequest) -> Awaitable[Job]: ...
    def list(self, request: ListJobsRequest) -> Awaitable[ListJobsResponse]: ...
    def create(self, request: CreateJobRequest) -> Awaitable[Operation]: ...
    def cancel(self, request: CancelJobRequest) -> Awaitable[Operation]: ...
    def delete(self, request: DeleteJobRequest) -> Awaitable[Operation]: ...


class BucketsClient(Protocol):
    def get_by_name(self, request: GetBucketByNameRequest) -> Awaitable[Bucket]: ...
    def list(self, request: ListBucketsRequest) -> Awaitable[ListBucketsResponse]: ...
    def create(self, request: CreateBucketRequest) -> Awaitable[Operation]: ...
    def delete(self, request: DeleteBucketRequest) -> Awaitable[Operation]: ...


class InstancesClient(Protocol):
    def get(self, request: GetInstanceRequest) -> Awaitable[Instance]: ...
    def start(self, request: StartInstanceRequest) -> Awaitable[Operation]: ...
    def stop(self, request: StopInstanceRequest) -> Awaitable[Operation]: ...


# gRPC status codes by name (grpc ships no type stubs).
NOT_FOUND, ALREADY_EXISTS, FAILED_PRECONDITION, RESOURCE_EXHAUSTED = (
    "NOT_FOUND",
    "ALREADY_EXISTS",
    "FAILED_PRECONDITION",
    # A RESOURCE_EXHAUSTED job creation is CapacityError (retried, then the fallback GPU
    # platform), unless it shows a quota other than GPUs that can never fit: QuotaExceeded
    # (S5/S6 hit compute.instance.non-gpu.vcpu = 0, which no wait fixes). See _refusal().
    "RESOURCE_EXHAUSTED",
)
# A quota as a failed create operation's message names it (S6: "compute.instance.non-gpu.vcpu
# (limit 0, requested 2)"); only the first KiB of the message is read.
_QUOTA_IN_MESSAGE = re.compile(
    r"(?<![.\w-])((?:compute|storage|vpc|ai|mk8s|registry)(?:\.[a-z0-9_-]{1,40}){1,6})"
    r" ?\(limit (\d{1,12}), requested (\d{1,12})\)"
)
_QUOTA_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,99}")  # what may reach the public run error
# quota families that are never GPUs: only these can be final (a GPU quota at 0 on one
# platform may still leave the fallback platform)
_NON_GPU_PARTS = frozenset({"non-gpu", "disk", "filesystem"})
_NON_GPU_NAMES = frozenset({"compute.instance.count"})
_MESSAGE_CHARS = 1024
_MAX_QUOTAS = 5
_TRANSIENT = frozenset({"too_many_requests", "not_enough_resources"})


def _code(err: RequestError) -> str:
    return str(err.status.code.name)


def _never_fits(name: str, limit: str) -> bool:
    """A quota that can't hold any job until an operator raises it: a non-GPU family at limit
    0. A nonzero limit may free up: whether "requested" includes what is in use is not known
    [S-4], so it is never compared."""
    if not _QUOTA_NAME.fullmatch(name):
        return False
    non_gpu = name in _NON_GPU_NAMES or not _NON_GPU_PARTS.isdisjoint(name.split("."))
    return non_gpu and limit.isascii() and limit.isdigit() and int(limit) == 0


def _final_quotas(status: object) -> list[str] | None:
    """The non-GPU quotas at limit 0 this refusal names, or None when it may pass (a GPU
    quota, a nonzero limit, "not enough resources", rate limiting, or nothing readable).
    Structured QuotaFailure violations of a request error first; a failed create operation
    carries no parsed service errors, so then its message."""
    found: list[tuple[str, str, str]] = []
    errors = list(getattr(status, "service_errors", None) or [])
    for error in errors:
        details = error.details
        if details is None:
            continue
        if details.field in _TRANSIENT:
            return None
        if details.field == "quota_failure":
            found += [
                (str(v.quota), str(v.limit), str(v.requested)) for v in details.value.violations
            ]
    if not errors:
        message = str(getattr(status, "message", None) or "")[:_MESSAGE_CHARS]
        if "quota" in message.lower():
            found = _QUOTA_IN_MESSAGE.findall(message)
    final = [name for name, limit, _ in found if _never_fits(name, limit)]
    return list(dict.fromkeys(final))[:_MAX_QUOTAS] or None


def _refusal(spec: JobSpec, status: object) -> CapacityError | QuotaExceeded:
    """What a RESOURCE_EXHAUSTED creation means: a quota other than GPUs that can never fit is
    final (QuotaExceeded, an operator must raise it); anything else may pass (CapacityError:
    retried, then the fallback platform)."""
    quotas = _final_quotas(status)
    if quotas:
        return QuotaExceeded(spec.name, quotas)
    return CapacityError(f"{spec.name}: no capacity on {spec.platform}")


def _aware(value: datetime | None) -> datetime:
    if value is None:  # never seen in practice; "now" keeps the reaper from acting on it early
        return datetime.now(UTC)
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


# --- JobSpec translation ----------------------------------------------------------------------

_STATES: dict[JobStatus.State, JobState] = {
    JobStatus.State.PROVISIONING: JobState.PENDING,
    JobStatus.State.IMAGE_PULLING: JobState.STARTING,
    JobStatus.State.STARTING: JobState.STARTING,
    JobStatus.State.RUNNING: JobState.RUNNING,
    JobStatus.State.CANCELLING: JobState.RUNNING,  # still running until CANCELLED
    JobStatus.State.COMPLETED: JobState.SUCCEEDED,
    JobStatus.State.FAILED: JobState.FAILED,
    JobStatus.State.ERROR: JobState.FAILED,
    JobStatus.State.CANCELLED: JobState.CANCELLED,
}  # [S-4] how a preempted job ends; DELETING and unknown values map to UNKNOWN (not terminal)


def job_state(job: Job) -> JobState:
    return _STATES.get(job.status.state, JobState.UNKNOWN)


_MODES = {"ro": SdkJobSpec.VolumeMount.Mode.READ_ONLY, "rw": SdkJobSpec.VolumeMount.Mode.READ_WRITE}
# A job mounts a bucket by its ID: the CLI and the API read a name such as "p2p-run-..." as a
# source of type "p2p" and refuse it (spikes S-5 and S-11, 2026-10-21).
BUCKET_ID = re.compile(r"^storagebucket-[a-z0-9-]+$")


def to_sdk_job_spec(spec: JobSpec, bucket_ids: Mapping[str, str]) -> SdkJobSpec:
    """Every JobSpec field, and nothing that widens the job: no command or args override (the
    image ENTRYPOINT is the runner), no public IP, ports, SSH keys or registry/S3 credentials,
    no platform restarts (retries are the orchestrator's). `bucket_ids` maps each volume's
    bucket name to its ID, looked up in the job's project (`NebiusJobBackend.submit`)."""
    spec = JobSpec.model_validate(spec.model_dump())  # never trust a model_construct'ed spec
    sources = []
    for volume in spec.volumes:
        bucket_id = bucket_ids.get(volume.source, "")
        if not BUCKET_ID.fullmatch(bucket_id):
            raise ValueError(f"no bucket ID for the mount of {volume.source}")
        sources.append(bucket_id)
    sdk = SdkJobSpec(
        image=spec.image_digest_ref,  # pulled with the job identity, no registry login [S-5]
        platform=spec.platform,
        preset=spec.preset,
        disk=SdkJobSpec.DiskSpec(
            size_bytes=spec.disk_gib * GIB, type=DiskSpec.DiskType.NETWORK_SSD
        ),  # [S-4] disk type
        subnet_id=spec.subnet_id,
        public_ip=False,
        volumes=[
            # Mounted by bucket ID with the job's own identity, never with a key [S-5].
            SdkJobSpec.VolumeMount(source=source, container_path=v.mount_path, mode=_MODES[v.mode])
            for source, v in zip(sources, spec.volumes, strict=True)
        ],
        injected_files=[
            SdkJobSpec.FileInjection(container_path=path, content=content)
            for path, content in spec.inject_files.items()
        ],
        environment_variables=[
            SdkJobSpec.EnvironmentVariable(name=name, value=value)
            for name, value in spec.env.items()
        ],
        preemptible=spec.preemptible,
        restart_attempts=0,  # restart policy "never" [S-4]
        timeout=PLATFORM_TIMEOUT,
    )
    if spec.preemptible:  # the CLI pairs --preemptible with --follows-spot-price [S-4]
        sdk.pricing_model = PricingModelSpec(follows_spot_price=FollowsSpotPriceSpec())
    check_no_credentials(sdk)
    return sdk


def check_no_credentials(sdk: SdkJobSpec) -> None:
    """Invariant 4 in code, not only in tests: no S3 key on a mount, no secret reference in the
    environment, no registry credentials. If S-11 shows mounts need a key, it must be a
    per-bucket key, never sa-job-launcher's (editor on the whole sandbox project)."""
    registry = sdk.registry_credentials
    if registry.username or registry.password or registry.mysterybox_secret_version:
        raise ValueError("jobs never receive credentials: registry credentials set")
    for volume in sdk.volumes:
        if volume.s3_config is not None:  # allowlist: we never set it, whatever it may carry
            raise ValueError(f"jobs never receive credentials: mount {volume.container_path}")
    for variable in sdk.environment_variables:
        if variable.mysterybox_secret.secret_id or variable.mysterybox_secret.version_id:
            raise ValueError(f"jobs never receive credentials: env {variable.name}")


# --- the backend ------------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(UTC)


class NebiusJobBackend:
    def __init__(
        self,
        *,
        jobs: JobsClient,
        buckets: BucketsClient,
        instances: InstancesClient,
        s3: S3Client,
        project_id: str,
        gateway_instance_id: str | None,
        clock: Callable[[], datetime] = _utcnow,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        create_wait_s: float = 30.0,
        op_timeout_s: float = 300.0,
        gateway_poll_s: float = 5.0,
        gateway_timeout_s: float = 300.0,
        gateway_settle_s: float = 30.0,
    ) -> None:
        self._jobs = jobs
        self._buckets = buckets
        self._instances = instances
        self._s3 = s3
        self._project = project_id
        self._gateway = gateway_instance_id
        self._clock = clock
        self._sleep = sleep
        self._create_wait_s = create_wait_s
        self._op_timeout_s = op_timeout_s
        self._gateway_poll_s = gateway_poll_s
        self._gateway_timeout_s = gateway_timeout_s
        self._gateway_settle_s = gateway_settle_s  # proxy and nftables up after boot [S-5]
        self._gateway_lock = asyncio.Lock()
        self._gateway_active_at = clock()  # after a restart, wait a full idle period

    @classmethod
    def from_sdk(
        cls, sdk: SDK, s3: S3Client, *, project_id: str, gateway_instance_id: str | None
    ) -> "NebiusJobBackend":
        return cls(
            jobs=JobServiceClient(sdk),
            buckets=BucketServiceClient(sdk),
            instances=InstanceServiceClient(sdk),
            s3=s3,
            project_id=project_id,
            gateway_instance_id=gateway_instance_id,
        )

    async def _finish(self, op: Operation, what: str) -> None:
        try:
            await asyncio.wait_for(op.wait(), self._op_timeout_s)
        except TimeoutError as err:
            raise JobBackendError(f"{what}: operation still running") from err
        if not op.successful():
            raise JobBackendError(f"{what}: operation failed")

    # --- jobs ---------------------------------------------------------------------------------

    async def _job_by_name(self, name: str) -> Job | None:
        try:
            return await self._jobs.get_by_name(
                GetJobByNameRequest(parent_id=self._project, name=name)
            )
        except RequestError as err:
            if _code(err) == NOT_FOUND:
                return None
            raise

    async def submit(self, spec: JobSpec) -> JobHandle:
        existing = await self._job_by_name(spec.name)
        if existing is not None:
            return JobHandle(name=spec.name, nebius_id=existing.metadata.id)
        request = CreateJobRequest(
            metadata=ResourceMetadata(parent_id=self._project, name=spec.name),
            spec=to_sdk_job_spec(spec, await self._bucket_ids(spec)),
        )
        try:
            op = await self._jobs.create(request)
        except RequestError as err:
            if _code(err) == RESOURCE_EXHAUSTED:
                await self._discard_refused(spec, "")
                raise _refusal(spec, err.status) from None
            if _code(err) != ALREADY_EXISTS:
                raise
            existing = await self._job_by_name(spec.name)  # a concurrent submit won
            if existing is None:
                raise JobBackendError(f"{spec.name}: exists but can't be read") from err
            return JobHandle(name=spec.name, nebius_id=existing.metadata.id)
        # [S-4] whether the create operation ends at creation or with the job: wait a little
        # for an early failure, then go on with the id (status() polls the job itself).
        try:
            await asyncio.wait_for(op.wait(), self._create_wait_s)
        except TimeoutError:
            pass
        else:
            if not op.successful():
                status = op.status()
                if status is not None and status.code.name == RESOURCE_EXHAUSTED:
                    await self._discard_refused(spec, op.resource_id)
                    raise _refusal(spec, status)
                raise JobBackendError(f"{spec.name}: job creation failed")
        job_id = op.resource_id
        if not job_id:
            found = await self._job_by_name(spec.name)
            if found is None:
                raise JobBackendError(f"{spec.name}: created but not found")
            job_id = found.metadata.id
        log.info("job submitted", extra={"job": spec.name, "nebius_id": job_id})
        return JobHandle(name=spec.name, nebius_id=job_id)

    async def _bucket_ids(self, spec: JobSpec) -> dict[str, str]:
        """The ID of each mounted bucket, looked up by name in the job's project only: a bucket
        of the same name elsewhere is never mounted. A missing bucket fails before any job is
        created."""
        ids: dict[str, str] = {}
        for volume in spec.volumes:
            bucket = await self._bucket_by_name(volume.source)
            if bucket is None:
                raise JobBackendError(
                    f"{spec.name}: bucket {volume.source} not found in the project"
                )
            if (bucket.metadata.parent_id, bucket.metadata.name) != (self._project, volume.source):
                raise JobBackendError(
                    f"{spec.name}: the lookup of {volume.source} returned another bucket"
                )
            self._usable(bucket)  # a bucket being deleted is never mounted
            ids[volume.source] = bucket.metadata.id
        return ids

    async def _discard_refused(self, spec: JobSpec, job_id: str) -> None:
        """CapacityError and QuotaExceeded promise that no job was created: the caller records
        nothing and may try again under the same name, on the fallback platform too. A job
        object left by the refused creation [S-4] would be outside the ledger and the reaper's
        watch, and every retry would re-attach to it. So delete what is left and confirm it is
        gone; when that can't be confirmed, fail with JobBackendError (the step fails, the
        reaper finds the job by name)."""
        ids = {job_id} if job_id else set()
        try:
            found = await self._job_by_name(spec.name)
            if found is not None:
                ids.add(found.metadata.id)
            left = sorted(ids)
            for left_id in left:
                await self._delete_job(left_id, spec.name)
            gone = [await self._job_gone(left_id) for left_id in left]
            if not all(gone) or await self._job_by_name(spec.name) is not None:
                raise JobBackendError(f"{spec.name}: still there after deletion")
        except (RequestError, JobBackendError) as err:
            raise JobBackendError(
                f"{spec.name}: refused (capacity or quota), and its job may remain"
            ) from err
        if left:
            log.info("refused job deleted", extra={"job": spec.name, "nebius_ids": left})

    async def _delete_job(self, job_id: str, name: str) -> None:
        try:  # [S-4] whether a job must be terminal before it can be deleted
            op = await self._jobs.cancel(CancelJobRequest(id=job_id))
        except RequestError as err:
            if _code(err) == NOT_FOUND:
                return
            if _code(err) != FAILED_PRECONDITION:  # FAILED_PRECONDITION: already finished
                raise
        else:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(op.wait(), self._op_timeout_s)
        try:
            op = await self._jobs.delete(DeleteJobRequest(id=job_id))
        except RequestError as err:
            if _code(err) == NOT_FOUND:
                return
            raise
        await self._finish(op, f"{name}: deletion of {job_id}")

    async def _job_gone(self, job_id: str) -> bool:
        try:
            await self._jobs.get(GetJobRequest(id=job_id))
        except RequestError as err:
            if _code(err) == NOT_FOUND:
                return True
            raise
        return False

    async def status(self, handle: JobHandle) -> JobState:
        try:
            job = await self._jobs.get(GetJobRequest(id=handle.nebius_id))
        except RequestError as err:
            if _code(err) == NOT_FOUND:
                return JobState.UNKNOWN
            raise
        if job.metadata.name != handle.name:
            raise JobBackendError(f"job {handle.nebius_id} does not match {handle.name}")
        return job_state(job)

    async def cancel(self, handle: JobHandle) -> None:
        try:
            op = await self._jobs.cancel(CancelJobRequest(id=handle.nebius_id))
        except RequestError as err:
            if _code(err) in (NOT_FOUND, FAILED_PRECONDITION):
                return  # gone, or already finished
            raise
        with contextlib.suppress(TimeoutError):  # callers poll status() until it is terminal
            await asyncio.wait_for(op.wait(), self._op_timeout_s)
        log.info("job cancelled", extra={"job": handle.name})

    async def list_jobs(self, prefix: str) -> list[tuple[JobHandle, JobState, datetime]]:
        found: list[tuple[JobHandle, JobState, datetime]] = []
        token, seen = "", set[str]()
        while True:
            page = await self._jobs.list(
                ListJobsRequest(parent_id=self._project, page_size=PAGE_SIZE, page_token=token)
            )
            for job in page.items:
                if job.metadata.name.startswith(prefix):
                    handle = JobHandle(name=job.metadata.name, nebius_id=job.metadata.id)
                    found.append((handle, job_state(job), _aware(job.metadata.created_at)))
            token = page.next_page_token
            if not token:
                return found
            _next_token(token, seen, "jobs")

    # --- buckets ------------------------------------------------------------------------------

    async def _bucket_by_name(self, name: str) -> Bucket | None:
        try:
            return await self._buckets.get_by_name(
                GetBucketByNameRequest(parent_id=self._project, name=name)
            )
        except RequestError as err:
            if _code(err) == NOT_FOUND:
                return None
            raise

    @staticmethod
    def _usable(bucket: Bucket) -> str:
        if bucket.status.state == BucketStatus.State.SCHEDULED_FOR_DELETION:
            raise JobBackendError(f"bucket {bucket.metadata.name} is scheduled for deletion")
        return bucket.metadata.name

    async def ensure_run_bucket(self, run_id: str) -> str:
        name = bucket_name(run_id)
        existing = await self._bucket_by_name(name)
        if existing is not None:
            return self._usable(existing)
        request = CreateBucketRequest(
            metadata=ResourceMetadata(parent_id=self._project, name=name),
            spec=BucketSpec(max_size_bytes=RUN_BUCKET_MAX_BYTES),
        )
        try:
            op = await self._buckets.create(request)
        except RequestError as err:
            if _code(err) != ALREADY_EXISTS:
                raise
            existing = await self._bucket_by_name(name)
            if existing is None:
                raise JobBackendError(f"bucket {name}: exists but can't be read") from err
            return self._usable(existing)
        await self._finish(op, f"bucket {name}")
        log.info("run bucket created", extra={"bucket": name})
        return name

    async def delete_run_bucket(self, run_id: str) -> None:
        """Empty the bucket through S3, then delete it. Callers decide when this is allowed
        (the reaper: run terminal, bundle stored, no job running)."""
        name = bucket_name(run_id)
        bucket = await self._bucket_by_name(name)
        if bucket is None or bucket.status.state == BucketStatus.State.SCHEDULED_FOR_DELETION:
            return
        if not await empty_bucket(self._s3, name):
            raise JobBackendError(f"bucket {name} is not empty yet: deletion resumes later")
        try:
            op = await self._buckets.delete(DeleteBucketRequest(id=bucket.metadata.id))
        except RequestError as err:
            if _code(err) == NOT_FOUND:
                return
            raise
        await self._finish(op, f"bucket {name} deletion")  # [S-11] soft delete and purge delay
        log.info("run bucket deleted", extra={"bucket": name})

    async def list_run_buckets(self) -> list[tuple[str, datetime]]:
        found: list[tuple[str, datetime]] = []
        token, seen = "", set[str]()
        while True:
            page = await self._buckets.list(
                ListBucketsRequest(parent_id=self._project, page_size=PAGE_SIZE, page_token=token)
            )
            for bucket in page.items:
                name = bucket.metadata.name
                deleting = bucket.status.state == BucketStatus.State.SCHEDULED_FOR_DELETION
                if name.startswith(RUN_BUCKET_PREFIX) and not deleting:
                    found.append((name, _aware(bucket.metadata.created_at)))
            token = page.next_page_token
            if not token:
                return found
            _next_token(token, seen, "buckets")

    # --- gateway ------------------------------------------------------------------------------

    async def _gateway_state(self, instance_id: str) -> InstanceStatus.InstanceState:
        instance = await self._instances.get(GetInstanceRequest(id=instance_id))
        return instance.status.state

    async def ensure_gateway_up(self) -> None:
        """Start the gateway VM if it is stopped and wait until it runs. Fail-closed: jobs in the
        provision/exec subnets have no egress without it [S-5]."""
        if self._gateway is None:
            return  # not managed on demand (always on)
        async with self._gateway_lock:
            self._gateway_active_at = self._clock()
            deadline = self._clock() + timedelta(seconds=self._gateway_timeout_s)
            started = False
            vm = InstanceStatus.InstanceState
            while True:
                state = await self._gateway_state(self._gateway)
                if state == vm.RUNNING:
                    break
                if state in (vm.ERROR, vm.DELETING, vm.UNSPECIFIED):
                    raise GatewayUnavailable(f"gateway VM is {state.name}")
                if state == vm.STOPPED and not started:
                    await self._finish(
                        await self._instances.start(StartInstanceRequest(id=self._gateway)),
                        "gateway start",
                    )
                    started = True
                    log.info("gateway VM starting")
                    continue
                if self._clock() >= deadline:
                    raise GatewayUnavailable(f"gateway VM not RUNNING (still {state.name})")
                await self._sleep(self._gateway_poll_s)
            if started:  # RUNNING is the VM, not yet squid and nftables from cloud-init [S-5]
                await self._sleep(self._gateway_settle_s)
            self._gateway_active_at = self._clock()

    async def gateway_idle_check(self, idle_minutes: int = 30) -> bool:
        """Stop the gateway VM when no p2p job has been running and nobody asked for it for
        `idle_minutes`. The caller (the reaper) also checks that no run is in a step that needs
        it. True when it stopped the VM."""
        if self._gateway is None:
            return False
        # Listed before the lock, so a slow listing never holds up ensure_gateway_up; one that
        # runs meanwhile moves _gateway_active_at, which is read under the lock.
        jobs = await self.list_jobs(JOB_PREFIX)
        async with self._gateway_lock:
            now = self._clock()
            if any(not state.is_terminal for _, state, _ in jobs):
                self._gateway_active_at = now
                return False
            if now - self._gateway_active_at < timedelta(minutes=idle_minutes):
                return False
            if await self._gateway_state(self._gateway) != InstanceStatus.InstanceState.RUNNING:
                return False
            await self._finish(
                await self._instances.stop(StopInstanceRequest(id=self._gateway)), "gateway stop"
            )
            log.info("gateway VM stopped (idle)", extra={"idle_minutes": idle_minutes})
            return True


# --- composition --------------------------------------------------------------------------------


@dataclass(frozen=True)
class NebiusExecution:
    backend: NebiusJobBackend
    storage: S3RunStorage


def _endpoint_url(host: str) -> str:
    return host if host.startswith(("https://", "http://")) else f"https://{host}"


def build_nebius_execution(
    *,
    project_id: str,
    s3_endpoint: str,
    region: str,
    s3_key_id: str,
    s3_secret: SecretStr,
    auth_key_file: Path,
    gateway_instance_id: str | None,
) -> NebiusExecution:
    """The JobBackend and RunStorage of prod, with the sa-job-launcher identity: the Nebius SDK
    authenticates with its authorized key file, S3 with its access key. Every value comes from
    the composition root (settings); nothing is read from the environment here."""
    required = {"project_id": project_id, "s3_endpoint": s3_endpoint, "s3_key_id": s3_key_id}
    missing = [name for name, value in required.items() if not value]
    if not s3_secret.get_secret_value():
        missing.append("s3_secret")
    if not str(auth_key_file) or str(auth_key_file) == "." or not auth_key_file.is_file():
        missing.append("auth_key_file (an existing file)")
    if missing:
        raise ValueError(f"Nebius execution needs: {', '.join(missing)}")
    s3 = make_s3_client(_endpoint_url(s3_endpoint), region, s3_key_id, s3_secret)
    sdk = SDK(credentials_file_name=auth_key_file, user_agent_prefix="paper2proof")
    backend = NebiusJobBackend.from_sdk(
        sdk, s3, project_id=project_id, gateway_instance_id=gateway_instance_id
    )
    return NebiusExecution(backend=backend, storage=S3RunStorage(s3))

"""NebiusJobBackend against fake SDK service clients that speak the real SDK message types.

Nothing here talks to Nebius: the fakes record every request and answer like the services do
(get-by-name, NOT_FOUND, ALREADY_EXISTS, long-running operations, paginated lists).
"""

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from grpc import StatusCode
from moto import mock_aws
from nebius.aio.service_error import RequestError, RequestStatusExtended
from nebius.api.nebius.ai.v1 import (
    CancelJobRequest,
    CreateJobRequest,
    DeleteJobRequest,
    GetJobByNameRequest,
    GetJobRequest,
    Job,
    JobStatus,
    ListJobsRequest,
    ListJobsResponse,
)
from nebius.api.nebius.ai.v1 import JobSpec as SdkJobSpec
from nebius.api.nebius.common.v1 import ResourceMetadata
from nebius.api.nebius.compute.v1 import (
    DiskSpec,
    GetInstanceRequest,
    Instance,
    InstanceStatus,
    StartInstanceRequest,
    StopInstanceRequest,
)
from nebius.api.nebius.storage.v1 import (
    Bucket,
    BucketStatus,
    CreateBucketRequest,
    DeleteBucketRequest,
    GetBucketByNameRequest,
    ListBucketsRequest,
    ListBucketsResponse,
)
from pydantic import SecretStr

from app.execution import nebius_backend
from app.execution.backend import (
    CapacityError,
    JobHandle,
    JobKind,
    JobSpec,
    JobState,
    VolumeMount,
)
from app.execution.naming import bucket_name, job_name
from app.execution.nebius_backend import (
    GatewayUnavailable,
    JobBackendError,
    NebiusJobBackend,
    build_nebius_execution,
    check_no_credentials,
    to_sdk_job_spec,
)
from app.execution.s3 import S3RunStorage, make_s3_client

RUN = "r_01jb7z00000000000000000000"
OTHER = "r_01jb7z00000000000000000009"
PROJECT = "project-sandbox"
GATEWAY = "computeinstance-gw"
DIGEST = "cr.eu-north1.nebius.cloud/reg/p2p-runner@sha256:" + "a" * 64
ENDPOINT = "https://storage.eu-north1.example.test"
T0 = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
State = JobStatus.State
VmState = InstanceStatus.InstanceState


def rpc_error(code: StatusCode) -> RequestError:
    return RequestError(
        RequestStatusExtended(
            code=code, message=code.name, details=[], service_errors=[], request_id="", trace_id=""
        )
    )


@dataclass
class FakeOp:
    resource_id: str
    ok: bool = True
    finished: bool = True
    waited: int = 0
    code: StatusCode | None = None  # why a failed operation failed

    def successful(self) -> bool:
        return self.ok

    def status(self) -> Any:
        if not self.finished:
            return None
        return SimpleNamespace(code=StatusCode.OK if self.ok else self.code or StatusCode.UNKNOWN)

    def done(self) -> bool:
        return self.finished

    async def wait(self, interval: float = 1) -> None:
        self.waited += 1
        if not self.finished:
            await asyncio.sleep(3600)


@dataclass
class FakeJobs:
    jobs: dict[str, Job] = field(default_factory=dict)  # by id
    requests: list[Any] = field(default_factory=list)
    create_error: StatusCode | None = None
    create_op: FakeOp | None = None
    create_leaves_job: bool = True  # [S-4] whether a refused creation leaves a job object
    delete_error: StatusCode | None = None
    delete_keeps_job: bool = False  # the delete operation succeeds, the job is still listed
    page_size: int = 2

    def add(self, name: str, state: Any, created: datetime = T0) -> Job:
        job_id = f"aijob-{len(self.jobs) + 1}"
        job = Job(
            metadata=ResourceMetadata(id=job_id, parent_id=PROJECT, name=name, created_at=created),
            status=JobStatus(state=state),
        )
        self.jobs[job_id] = job
        return job

    async def get_by_name(self, request: GetJobByNameRequest) -> Job:
        self.requests.append(request)
        for job in self.jobs.values():
            if job.metadata.name == request.name and job.metadata.parent_id == request.parent_id:
                return job
        raise rpc_error(StatusCode.NOT_FOUND)

    async def get(self, request: GetJobRequest) -> Job:
        self.requests.append(request)
        if request.id not in self.jobs:
            raise rpc_error(StatusCode.NOT_FOUND)
        return self.jobs[request.id]

    async def create(self, request: CreateJobRequest) -> FakeOp:
        self.requests.append(request)
        if self.create_error is not None:
            if self.create_error is not StatusCode.ALREADY_EXISTS and self.create_leaves_job:
                self.add(request.metadata.name, State.PROVISIONING)  # refused, yet left behind
            raise rpc_error(self.create_error)
        if self.create_op is not None and not self.create_op.ok and not self.create_leaves_job:
            return self.create_op
        job = self.add(request.metadata.name, State.PROVISIONING)
        return self.create_op or FakeOp(job.metadata.id)

    async def delete(self, request: DeleteJobRequest) -> FakeOp:
        self.requests.append(request)
        if self.delete_error is not None:
            raise rpc_error(self.delete_error)
        if request.id not in self.jobs:
            raise rpc_error(StatusCode.NOT_FOUND)
        if not self.delete_keeps_job:
            del self.jobs[request.id]
        return FakeOp(request.id)

    def named(self, name: str) -> list[Job]:
        return [job for job in self.jobs.values() if job.metadata.name == name]

    async def cancel(self, request: CancelJobRequest) -> FakeOp:
        self.requests.append(request)
        if request.id not in self.jobs:
            raise rpc_error(StatusCode.NOT_FOUND)
        self.jobs[request.id].status.state = State.CANCELLED
        return FakeOp(request.id)

    async def list(self, request: ListJobsRequest) -> ListJobsResponse:
        self.requests.append(request)
        items = list(self.jobs.values())
        start = int(request.page_token or 0)
        end = start + self.page_size
        return ListJobsResponse(
            items=items[start:end], next_page_token=str(end) if end < len(items) else ""
        )


@dataclass
class FakeBuckets:
    buckets: dict[str, Bucket] = field(default_factory=dict)  # by name
    requests: list[Any] = field(default_factory=list)
    s3: Any = None  # moto client: buckets created here also exist in S3

    def add(self, name: str, created: datetime = T0, state: Any = None) -> Bucket:
        bucket = Bucket(
            metadata=ResourceMetadata(
                id=f"storagebucket-{name}", parent_id=PROJECT, name=name, created_at=created
            ),
            status=BucketStatus(state=state or BucketStatus.State.ACTIVE),
        )
        self.buckets[name] = bucket
        if self.s3 is not None:
            self.s3.create_bucket(
                Bucket=name, CreateBucketConfiguration={"LocationConstraint": "eu-north1"}
            )
        return bucket

    async def get_by_name(self, request: GetBucketByNameRequest) -> Bucket:
        self.requests.append(request)
        bucket = self.buckets.get(request.name)
        if bucket is None or bucket.metadata.parent_id != request.parent_id:
            raise rpc_error(StatusCode.NOT_FOUND)
        return bucket

    async def create(self, request: CreateBucketRequest) -> FakeOp:
        self.requests.append(request)
        if request.metadata.name in self.buckets:
            raise rpc_error(StatusCode.ALREADY_EXISTS)
        bucket = self.add(request.metadata.name)
        return FakeOp(bucket.metadata.id)

    async def delete(self, request: DeleteBucketRequest) -> FakeOp:
        self.requests.append(request)
        for name, bucket in list(self.buckets.items()):
            if bucket.metadata.id == request.id:
                if self.s3 is not None:
                    assert self.s3.list_objects_v2(Bucket=name)["KeyCount"] == 0, "not empty"
                    self.s3.delete_bucket(Bucket=name)
                del self.buckets[name]
                return FakeOp(request.id)
        raise rpc_error(StatusCode.NOT_FOUND)

    async def list(self, request: ListBucketsRequest) -> ListBucketsResponse:
        self.requests.append(request)
        return ListBucketsResponse(items=list(self.buckets.values()))


@dataclass
class FakeInstances:
    state: Any = VmState.STOPPED
    requests: list[Any] = field(default_factory=list)
    polls_to_running: int = 2  # get() calls in STARTING before RUNNING

    async def get(self, request: GetInstanceRequest) -> Instance:
        self.requests.append(request)
        if self.state == VmState.STARTING:
            self.polls_to_running -= 1
            if self.polls_to_running <= 0:
                self.state = VmState.RUNNING
        elif self.state == VmState.STOPPING:
            self.state = VmState.STOPPED
        return Instance(
            metadata=ResourceMetadata(id=request.id, name="p2p-egress-gw"),
            status=InstanceStatus(state=self.state),
        )

    async def start(self, request: StartInstanceRequest) -> FakeOp:
        self.requests.append(request)
        self.state = VmState.STARTING
        return FakeOp(request.id)

    async def stop(self, request: StopInstanceRequest) -> FakeOp:
        self.requests.append(request)
        self.state = VmState.STOPPED
        return FakeOp(request.id)


class Clock:
    def __init__(self) -> None:
        self.now = T0
        self.slept: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)


@dataclass
class World:
    backend: NebiusJobBackend
    jobs: FakeJobs
    buckets: FakeBuckets
    instances: FakeInstances
    clock: Clock
    s3: Any


def make_world(s3: Any) -> World:
    jobs, buckets, instances, clock = FakeJobs(), FakeBuckets(s3=s3), FakeInstances(), Clock()
    backend = NebiusJobBackend(
        jobs=jobs,
        buckets=buckets,
        instances=instances,
        s3=s3,
        project_id=PROJECT,
        gateway_instance_id=GATEWAY,
        clock=clock,
        sleep=clock.sleep,
        create_wait_s=0.05,
    )
    return World(backend, jobs, buckets, instances, clock, s3)


@pytest.fixture
def world() -> World:
    return make_world(s3=None)  # no S3 call expected


@pytest.fixture
def world_s3(monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    """Buckets created through the fake SDK also exist in moto's S3."""
    monkeypatch.setenv("MOTO_S3_CUSTOM_ENDPOINTS", ENDPOINT)
    with mock_aws():
        yield make_world(make_s3_client(ENDPOINT, "eu-north1", "key-id", SecretStr("key-secret")))


def spec(**overrides: object) -> JobSpec:
    data: dict[str, object] = {
        "name": job_name(JobKind.EXEC, RUN, 1),
        "kind": JobKind.EXEC,
        "image_digest_ref": DIGEST,
        "platform": "gpu-l40s-a",
        "preset": "1gpu-8vcpu-32gb",
        "disk_gib": 100,
        "subnet_id": "vpcsubnet-exec",
        "volumes": [
            VolumeMount(source=bucket_name(RUN), mount_path="/mnt/run", mode="rw"),
            VolumeMount(source="p2p-cache", mount_path="/mnt/cache", mode="ro"),
        ],
        "inject_files": {"/etc/p2p/manifest.json": b'{"schema": "p2p.exec/v1"}'},
        "env": {"PYTHONUNBUFFERED": "1", "P2P_RUN_ID": RUN},
        "preemptible": True,
        "watchdog_s": 2100,
    }
    data.update(overrides)
    return JobSpec.model_validate(data)


# --- JobSpec translation ----------------------------------------------------------------------


def test_sdk_spec_honours_every_field() -> None:
    sdk = to_sdk_job_spec(spec())
    assert sdk.image == DIGEST
    assert (sdk.platform, sdk.preset) == ("gpu-l40s-a", "1gpu-8vcpu-32gb")
    assert sdk.disk.size_bytes == 100 * 1024**3
    assert sdk.disk.type == DiskSpec.DiskType.NETWORK_SSD
    assert sdk.subnet_id == "vpcsubnet-exec"
    assert [(v.source, v.container_path, v.mode) for v in sdk.volumes] == [
        (bucket_name(RUN), "/mnt/run", SdkJobSpec.VolumeMount.Mode.READ_WRITE),
        ("p2p-cache", "/mnt/cache", SdkJobSpec.VolumeMount.Mode.READ_ONLY),
    ]
    assert [(f.container_path, f.content) for f in sdk.injected_files] == [
        ("/etc/p2p/manifest.json", b'{"schema": "p2p.exec/v1"}')
    ]
    assert {e.name: e.value for e in sdk.environment_variables} == {
        "PYTHONUNBUFFERED": "1",
        "P2P_RUN_ID": RUN,
    }
    assert sdk.preemptible is True
    assert sdk.pricing_model.follows_spot_price is not None
    assert sdk.restart_attempts == 0
    assert sdk.timeout == timedelta(hours=1)


def test_sdk_spec_never_overrides_the_entrypoint_nor_opens_the_job() -> None:
    sdk = to_sdk_job_spec(spec())
    assert sdk.container_command == ""
    assert sdk.args == ""
    assert sdk.working_dir == ""
    assert sdk.public_ip is False
    assert list(sdk.ports) == []
    assert list(sdk.ssh_authorized_keys) == []
    assert not sdk.registry_credentials.username and not sdk.registry_credentials.password
    for volume in sdk.volumes:  # mounted with the job's own identity, never with keys
        assert volume.s3_config is None or volume.s3_config.credentials is None


def test_on_demand_spec_has_no_spot_pricing() -> None:
    sdk = to_sdk_job_spec(spec(preemptible=False))
    assert sdk.preemptible is False
    assert sdk.pricing_model.follows_spot_price is None


def test_sdk_spec_rejects_a_spec_built_without_validation() -> None:
    """model_construct skips the least-privilege validators: the adapter re-validates."""
    unsafe = JobSpec.model_construct(**{**dict(spec()), "env": {"NEBIUS_API_KEY": "x"}})
    with pytest.raises(ValueError, match=r"credentials|not allowed"):
        to_sdk_job_spec(unsafe)


# --- submit / status / cancel / list ------------------------------------------------------------


async def test_submit_creates_the_job_in_the_sandbox_project(world: World) -> None:
    handle = await world.backend.submit(spec())
    created = [r for r in world.jobs.requests if isinstance(r, CreateJobRequest)]
    assert len(created) == 1
    assert created[0].metadata.parent_id == PROJECT
    assert created[0].metadata.name == spec().name
    assert created[0].spec.image == DIGEST
    assert handle == JobHandle(name=spec().name, nebius_id="aijob-1")


async def test_submit_is_get_by_name_first(world: World) -> None:
    existing = world.jobs.add(spec().name, State.RUNNING)
    handle = await world.backend.submit(spec())
    assert handle.nebius_id == existing.metadata.id
    assert isinstance(world.jobs.requests[0], GetJobByNameRequest)
    assert world.jobs.requests[0].parent_id == PROJECT
    assert not [r for r in world.jobs.requests if isinstance(r, CreateJobRequest)]


async def test_submit_twice_creates_once(world: World) -> None:
    first = await world.backend.submit(spec())
    second = await world.backend.submit(spec())
    assert first == second
    assert len([r for r in world.jobs.requests if isinstance(r, CreateJobRequest)]) == 1


async def test_submit_race_already_exists_returns_the_existing_job(world: World) -> None:
    world.jobs.create_error = StatusCode.ALREADY_EXISTS
    original_get = world.jobs.get_by_name
    calls = 0

    async def appears_after_first_lookup(request: GetJobByNameRequest) -> Job:
        nonlocal calls
        calls += 1
        if calls == 2:
            world.jobs.add(spec().name, State.STARTING)
        return await original_get(request)

    world.jobs.get_by_name = appears_after_first_lookup  # type: ignore[method-assign]
    handle = await world.backend.submit(spec())
    assert handle.name == spec().name


async def test_submit_failed_operation_raises(world: World) -> None:
    world.jobs.create_op = FakeOp("aijob-x", ok=False)
    with pytest.raises(JobBackendError, match=spec().name):
        await world.backend.submit(spec())


def _deleted(world: World) -> list[str]:
    return [r.id for r in world.jobs.requests if isinstance(r, DeleteJobRequest)]


async def test_submit_without_capacity_raises_capacity_error(world: World) -> None:
    world.jobs.create_error = StatusCode.RESOURCE_EXHAUSTED  # GPU quota, "not enough resources"
    world.jobs.create_leaves_job = False
    with pytest.raises(CapacityError, match=spec().platform):
        await world.backend.submit(spec())
    assert _deleted(world) == []  # nothing was left behind, nothing to clean up


async def test_a_create_operation_failing_for_capacity_raises_capacity_error(world: World) -> None:
    world.jobs.create_op = FakeOp("", ok=False, code=StatusCode.RESOURCE_EXHAUSTED)
    world.jobs.create_leaves_job = False
    with pytest.raises(CapacityError, match=spec().name):
        await world.backend.submit(spec())
    assert _deleted(world) == []


async def test_a_job_left_by_a_refused_create_operation_is_deleted_first(world: World) -> None:
    """[S-4] the failed operation may still name a job: it is removed before CapacityError, so
    the retry (or the fallback platform) creates its own job instead of re-attaching to it."""
    world.jobs.create_op = FakeOp("aijob-1", ok=False, code=StatusCode.RESOURCE_EXHAUSTED)
    with pytest.raises(CapacityError, match=spec().name):
        await world.backend.submit(spec())
    assert _deleted(world) == ["aijob-1"]
    assert world.jobs.named(spec().name) == []
    world.jobs.create_op = None
    fallback = spec(platform="gpu-l40s-d", preset="1gpu-16vcpu-96gb")
    handle = await world.backend.submit(fallback)
    assert world.jobs.jobs[handle.nebius_id].metadata.name == fallback.name
    created = [r for r in world.jobs.requests if isinstance(r, CreateJobRequest)]
    assert created[-1].spec.platform == "gpu-l40s-d"


async def test_a_job_found_by_name_after_a_refused_create_is_deleted(world: World) -> None:
    world.jobs.create_op = FakeOp("", ok=False, code=StatusCode.RESOURCE_EXHAUSTED)
    with pytest.raises(CapacityError):
        await world.backend.submit(spec())
    assert _deleted(world) == ["aijob-1"]
    assert world.jobs.named(spec().name) == []


async def test_a_job_left_by_a_refused_create_call_is_deleted(world: World) -> None:
    world.jobs.create_error = StatusCode.RESOURCE_EXHAUSTED
    with pytest.raises(CapacityError):
        await world.backend.submit(spec())
    assert world.jobs.named(spec().name) == []


async def test_a_refused_job_that_can_not_be_deleted_is_a_backend_error(world: World) -> None:
    """Not a capacity verdict: the job may still run, unrecorded, so the step fails instead."""
    world.jobs.create_op = FakeOp("aijob-1", ok=False, code=StatusCode.RESOURCE_EXHAUSTED)
    world.jobs.delete_error = StatusCode.INTERNAL
    with pytest.raises(JobBackendError, match=spec().name) as raised:
        await world.backend.submit(spec())
    assert not isinstance(raised.value, CapacityError)


async def test_a_refused_job_still_there_after_deletion_is_a_backend_error(world: World) -> None:
    world.jobs.create_op = FakeOp("aijob-1", ok=False, code=StatusCode.RESOURCE_EXHAUSTED)
    world.jobs.delete_keeps_job = True
    with pytest.raises(JobBackendError, match=spec().name):
        await world.backend.submit(spec())


async def test_submit_does_not_wait_for_a_long_running_create(world: World) -> None:
    """[S-4] if the create operation lasts as long as the job, the handle still comes back."""
    world.jobs.create_op = FakeOp("aijob-1", finished=False)
    handle = await asyncio.wait_for(world.backend.submit(spec()), timeout=5)
    assert handle.nebius_id == "aijob-1"


@pytest.mark.parametrize(
    ("sdk_state", "expected"),
    [
        (State.STATE_UNSPECIFIED, JobState.UNKNOWN),
        (State.PROVISIONING, JobState.PENDING),
        (State.IMAGE_PULLING, JobState.STARTING),
        (State.STARTING, JobState.STARTING),
        (State.RUNNING, JobState.RUNNING),
        (State.CANCELLING, JobState.RUNNING),
        (State.DELETING, JobState.UNKNOWN),
        (State.COMPLETED, JobState.SUCCEEDED),
        (State.FAILED, JobState.FAILED),
        (State.ERROR, JobState.FAILED),
        (State.CANCELLED, JobState.CANCELLED),
    ],
)
async def test_status_maps_every_state(world: World, sdk_state: Any, expected: JobState) -> None:
    job = world.jobs.add(spec().name, sdk_state)
    handle = JobHandle(name=spec().name, nebius_id=job.metadata.id)
    assert await world.backend.status(handle) is expected


async def test_status_of_a_vanished_job_is_unknown(world: World) -> None:
    handle = JobHandle(name=spec().name, nebius_id="aijob-gone")
    assert await world.backend.status(handle) is JobState.UNKNOWN


async def test_status_refuses_a_handle_naming_another_job(world: World) -> None:
    job = world.jobs.add(job_name(JobKind.EXEC, OTHER, 1), State.RUNNING)
    with pytest.raises(JobBackendError, match="does not match"):
        await world.backend.status(JobHandle(name=spec().name, nebius_id=job.metadata.id))


async def test_cancel(world: World) -> None:
    job = world.jobs.add(spec().name, State.RUNNING)
    handle = JobHandle(name=spec().name, nebius_id=job.metadata.id)
    await world.backend.cancel(handle)
    assert await world.backend.status(handle) is JobState.CANCELLED


@pytest.mark.parametrize("code", [StatusCode.NOT_FOUND, StatusCode.FAILED_PRECONDITION])
async def test_cancel_of_a_gone_or_finished_job_is_a_no_op(world: World, code: StatusCode) -> None:
    async def refuse(request: CancelJobRequest) -> FakeOp:
        raise rpc_error(code)

    world.jobs.cancel = refuse  # type: ignore[method-assign]
    await world.backend.cancel(JobHandle(name=spec().name, nebius_id="aijob-1"))


async def test_cancel_propagates_other_errors(world: World) -> None:
    async def refuse(request: CancelJobRequest) -> FakeOp:
        raise rpc_error(StatusCode.PERMISSION_DENIED)

    world.jobs.cancel = refuse  # type: ignore[method-assign]
    with pytest.raises(RequestError):
        await world.backend.cancel(JobHandle(name=spec().name, nebius_id="aijob-1"))


async def test_list_jobs_stops_on_a_repeated_page_token(world: World) -> None:
    async def looping(request: ListJobsRequest) -> ListJobsResponse:
        return ListJobsResponse(items=[], next_page_token="same")

    world.jobs.list = looping  # type: ignore[method-assign]
    with pytest.raises(JobBackendError, match="page token"):
        await world.backend.list_jobs("p2p-")


def test_credentials_in_an_sdk_spec_are_refused() -> None:
    """Defence in depth: whatever builds the SDK spec, a job never gets a key (invariant 4)."""
    sdk = to_sdk_job_spec(spec())
    sdk.volumes[0].s3_config = SdkJobSpec.VolumeMount.S3Config(
        credentials=SdkJobSpec.VolumeMount.S3Config.S3Credentials(access_key_id="k")
    )
    with pytest.raises(ValueError, match="credentials"):
        check_no_credentials(sdk)
    sdk = to_sdk_job_spec(spec())
    sdk.volumes[0].s3_config = SdkJobSpec.VolumeMount.S3Config(endpoint="https://x")
    with pytest.raises(ValueError, match="credentials"):  # any s3_config: allowlist
        check_no_credentials(sdk)
    sdk = to_sdk_job_spec(spec())
    sdk.environment_variables[0].mysterybox_secret = SdkJobSpec.MysteryBoxSecretRef(secret_id="x")
    with pytest.raises(ValueError, match="credentials"):
        check_no_credentials(sdk)
    sdk = to_sdk_job_spec(spec())
    sdk.registry_credentials = SdkJobSpec.RegistryCredentials(password="p")
    with pytest.raises(ValueError, match="credentials"):
        check_no_credentials(sdk)
    check_no_credentials(to_sdk_job_spec(spec()))


async def test_list_jobs_pages_and_filters_by_prefix(world: World) -> None:
    a = world.jobs.add(job_name(JobKind.EXEC, RUN, 1), State.COMPLETED, T0)
    world.jobs.add("someone-elses-job", State.RUNNING)
    b = world.jobs.add(job_name(JobKind.EXEC, RUN, 2), State.RUNNING, T0 + timedelta(minutes=5))
    world.jobs.add(job_name(JobKind.PROVISION, RUN, 1), State.COMPLETED)
    listed = await world.backend.list_jobs(f"p2p-exec-{RUN[2:]}-")
    assert listed == [
        (JobHandle(name=a.metadata.name, nebius_id=a.metadata.id), JobState.SUCCEEDED, T0),
        (
            JobHandle(name=b.metadata.name, nebius_id=b.metadata.id),
            JobState.RUNNING,
            T0 + timedelta(minutes=5),
        ),
    ]
    pages = [r for r in world.jobs.requests if isinstance(r, ListJobsRequest)]
    assert len(pages) == 2
    assert all(p.parent_id == PROJECT for p in pages)


# --- buckets -----------------------------------------------------------------------------------


async def test_ensure_run_bucket_creates_once(world: World) -> None:
    assert await world.backend.ensure_run_bucket(RUN) == bucket_name(RUN)
    assert await world.backend.ensure_run_bucket(RUN) == bucket_name(RUN)
    created = [r for r in world.buckets.requests if isinstance(r, CreateBucketRequest)]
    assert len(created) == 1
    assert created[0].metadata.parent_id == PROJECT
    assert created[0].spec.max_size_bytes > 0


async def test_ensure_run_bucket_race_already_exists(world: World) -> None:
    original_get = world.buckets.get_by_name
    calls = 0

    async def appears_after_first_lookup(request: GetBucketByNameRequest) -> Bucket:
        nonlocal calls
        calls += 1
        if calls == 1:
            world.buckets.add(bucket_name(RUN))
            raise rpc_error(StatusCode.NOT_FOUND)
        return await original_get(request)

    world.buckets.get_by_name = appears_after_first_lookup  # type: ignore[method-assign]
    assert await world.backend.ensure_run_bucket(RUN) == bucket_name(RUN)


async def test_ensure_run_bucket_refuses_a_bucket_scheduled_for_deletion(world: World) -> None:
    world.buckets.add(bucket_name(RUN), state=BucketStatus.State.SCHEDULED_FOR_DELETION)
    with pytest.raises(JobBackendError, match="deletion"):
        await world.backend.ensure_run_bucket(RUN)


async def test_delete_run_bucket_empties_it_first(world_s3: World) -> None:
    world = world_s3
    await world.backend.ensure_run_bucket(RUN)
    world.s3.put_object(Bucket=bucket_name(RUN), Key="results/1/results.json", Body=b"{}")
    await world.backend.delete_run_bucket(RUN)
    assert bucket_name(RUN) not in world.buckets.buckets
    deletes = [r for r in world.buckets.requests if isinstance(r, DeleteBucketRequest)]
    assert [d.id for d in deletes] == [f"storagebucket-{bucket_name(RUN)}"]


async def test_delete_run_bucket_waits_until_it_is_empty(
    world_s3: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = world_s3
    await world.backend.ensure_run_bucket(RUN)

    async def not_yet(client: Any, bucket: str, **kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(nebius_backend, "empty_bucket", not_yet)
    with pytest.raises(JobBackendError, match="not empty"):
        await world.backend.delete_run_bucket(RUN)
    assert bucket_name(RUN) in world.buckets.buckets


async def test_delete_of_a_missing_bucket_is_a_no_op(world: World) -> None:
    await world.backend.delete_run_bucket(RUN)


async def test_list_run_buckets_only_lists_live_run_buckets(world: World) -> None:
    world.buckets.add(bucket_name(RUN), created=T0)
    world.buckets.add("p2p-cache")
    world.buckets.add(bucket_name(OTHER), state=BucketStatus.State.SCHEDULED_FOR_DELETION)
    assert await world.backend.list_run_buckets() == [(bucket_name(RUN), T0)]


# --- gateway -----------------------------------------------------------------------------------


async def test_ensure_gateway_up_starts_a_stopped_vm_and_waits(world: World) -> None:
    await world.backend.ensure_gateway_up()
    assert world.instances.state == VmState.RUNNING
    starts = [r for r in world.instances.requests if isinstance(r, StartInstanceRequest)]
    assert [s.id for s in starts] == [GATEWAY]
    assert world.clock.slept  # polled until RUNNING, then let the proxy come up


async def test_ensure_gateway_up_on_a_running_vm_does_nothing(world: World) -> None:
    world.instances.state = VmState.RUNNING
    await world.backend.ensure_gateway_up()
    assert not [r for r in world.instances.requests if isinstance(r, StartInstanceRequest)]
    assert world.clock.slept == []


async def test_ensure_gateway_up_waits_for_a_stopping_vm(world: World) -> None:
    world.instances.state = VmState.STOPPING
    await world.backend.ensure_gateway_up()
    assert world.instances.state == VmState.RUNNING


async def test_ensure_gateway_up_times_out(world: World) -> None:
    world.instances.polls_to_running = 10**6
    with pytest.raises(GatewayUnavailable, match="not RUNNING"):
        await world.backend.ensure_gateway_up()


async def test_ensure_gateway_up_fails_on_a_broken_vm(world: World) -> None:
    world.instances.state = VmState.ERROR
    with pytest.raises(GatewayUnavailable, match="ERROR"):
        await world.backend.ensure_gateway_up()


async def test_gateway_idle_check_stops_an_idle_vm(world: World) -> None:
    world.instances.state = VmState.RUNNING
    await world.backend.ensure_gateway_up()
    world.clock.now += timedelta(minutes=29)
    assert await world.backend.gateway_idle_check(30) is False
    world.clock.now += timedelta(minutes=2)
    assert await world.backend.gateway_idle_check(30) is True
    assert world.instances.state == VmState.STOPPED
    assert [r.id for r in world.instances.requests if isinstance(r, StopInstanceRequest)] == [
        GATEWAY
    ]


async def test_gateway_idle_check_keeps_the_vm_while_a_job_runs(world: World) -> None:
    world.instances.state = VmState.RUNNING
    world.jobs.add(job_name(JobKind.PROVISION, RUN, 1), State.RUNNING)
    world.clock.now += timedelta(hours=2)
    assert await world.backend.gateway_idle_check(30) is False
    assert world.instances.state == VmState.RUNNING
    world.jobs.jobs["aijob-1"].status.state = State.COMPLETED
    world.clock.now += timedelta(minutes=29)  # the running job counted as activity
    assert await world.backend.gateway_idle_check(30) is False
    world.clock.now += timedelta(minutes=2)
    assert await world.backend.gateway_idle_check(30) is True


async def test_gateway_idle_check_on_a_stopped_vm(world: World) -> None:
    world.clock.now += timedelta(hours=1)
    assert await world.backend.gateway_idle_check(30) is False
    assert not [r for r in world.instances.requests if isinstance(r, StopInstanceRequest)]


async def test_unmanaged_gateway_is_left_alone(world: World) -> None:
    backend = NebiusJobBackend(
        jobs=world.jobs,
        buckets=world.buckets,
        instances=world.instances,
        s3=world.s3,
        project_id=PROJECT,
        gateway_instance_id=None,
        clock=world.clock,
        sleep=world.clock.sleep,
    )
    await backend.ensure_gateway_up()
    world.clock.now += timedelta(hours=2)
    assert await backend.gateway_idle_check(30) is False
    assert world.instances.requests == []


# --- composition --------------------------------------------------------------------------------


def test_build_wires_the_backend_and_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[dict[str, Any]] = []

    class FakeSdk:  # the real SDK needs certifi-win32 on Windows; channels open lazily anyway
        def __init__(self, **kwargs: Any) -> None:
            built.append(kwargs)

    monkeypatch.setattr(nebius_backend, "SDK", FakeSdk)
    key_file = tmp_path / "credentials.json"
    key_file.write_text("{}", encoding="utf-8")
    execution = build_nebius_execution(
        project_id=PROJECT,
        s3_endpoint="storage.eu-north1.example.test",
        region="eu-north1",
        s3_key_id="key-id",
        s3_secret=SecretStr("s3-secret-value"),
        auth_key_file=key_file,
        gateway_instance_id=GATEWAY,
    )
    assert built == [{"credentials_file_name": key_file, "user_agent_prefix": "paper2proof"}]
    assert isinstance(execution.backend, NebiusJobBackend)
    assert isinstance(execution.storage, S3RunStorage)
    assert "s3-secret-value" not in repr(execution)


@pytest.mark.parametrize("missing", ["project_id", "s3_endpoint", "s3_key_id"])
def test_build_refuses_empty_values(tmp_path: Path, missing: str) -> None:
    args: dict[str, Any] = {
        "project_id": PROJECT,
        "s3_endpoint": "https://storage.example.test",
        "region": "eu-north1",
        "s3_key_id": "key-id",
        "s3_secret": SecretStr("s3-secret-value"),
        "auth_key_file": tmp_path / "credentials.json",
        "gateway_instance_id": None,
    }
    args[missing] = ""
    with pytest.raises(ValueError, match=missing) as excinfo:
        build_nebius_execution(**args)
    assert "s3-secret-value" not in str(excinfo.value)


@pytest.mark.parametrize("key_file", ["", "missing.json"])
def test_build_needs_the_key_file(tmp_path: Path, key_file: str) -> None:
    with pytest.raises(ValueError, match="auth_key_file"):
        build_nebius_execution(
            project_id=PROJECT,
            s3_endpoint="https://storage.example.test",
            region="eu-north1",
            s3_key_id="key-id",
            s3_secret=SecretStr("s3-secret-value"),
            auth_key_file=tmp_path / key_file if key_file else Path(key_file),
            gateway_instance_id=None,
        )


def test_build_refuses_plain_http(tmp_path: Path) -> None:
    key_file = tmp_path / "credentials.json"
    key_file.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="https"):
        build_nebius_execution(
            project_id=PROJECT,
            s3_endpoint="http://storage.example.test",
            region="eu-north1",
            s3_key_id="key-id",
            s3_secret=SecretStr("s3-secret-value"),
            auth_key_file=key_file,
            gateway_instance_id=None,
        )

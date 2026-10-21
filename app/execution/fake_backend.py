"""In-memory JobBackend for tests and the `test` profile. Mirrors the get-by-name semantics."""

from collections.abc import Callable
from datetime import UTC, datetime

from app.execution.backend import (
    CapacityError,
    JobHandle,
    JobSpec,
    JobState,
    QuotaExceeded,
)
from app.execution.naming import bucket_name


class FakeJobBackend:
    def __init__(
        self,
        final_state: JobState = JobState.SUCCEEDED,
        on_submit: Callable[[JobSpec], None] | None = None,
    ) -> None:
        self.final_state = final_state
        self.on_submit = on_submit  # simulates what the job writes (e.g. into FakeRunStorage)
        self.cancelled: list[str] = []
        self.jobs: dict[str, tuple[JobHandle, JobState, datetime, JobSpec]] = {}
        self.buckets: set[str] = set()
        self.bucket_created: dict[str, datetime] = {}
        self.submit_calls = 0
        self.created = 0
        self.gateway_running = False
        # platform -> how many more new jobs it refuses for lack of capacity
        self.no_capacity: dict[str, int] = {}
        self.quota_exceeded: list[str] = []  # non-GPU quotas used up: every new job is refused

    async def ensure_run_bucket(self, run_id: str) -> str:
        name = bucket_name(run_id)
        self.buckets.add(name)
        self.bucket_created.setdefault(name, datetime.now(UTC))
        return name

    async def delete_run_bucket(self, run_id: str) -> None:
        self.buckets.discard(bucket_name(run_id))
        self.bucket_created.pop(bucket_name(run_id), None)

    async def submit(self, spec: JobSpec) -> JobHandle:
        self.submit_calls += 1
        existing = self.jobs.get(spec.name)
        if existing is not None:
            return existing[0]
        if self.quota_exceeded:
            raise QuotaExceeded(spec.name, list(self.quota_exceeded))
        if self.no_capacity.get(spec.platform, 0) > 0:
            self.no_capacity[spec.platform] -= 1
            raise CapacityError(f"no capacity on {spec.platform} for {spec.preset}")
        self.created += 1
        handle = JobHandle(name=spec.name, nebius_id=f"fakejob-{self.created}")
        self.jobs[spec.name] = (handle, self.final_state, datetime.now(UTC), spec)
        if self.on_submit is not None:
            self.on_submit(spec)
        return handle

    async def status(self, handle: JobHandle) -> JobState:
        entry = self.jobs.get(handle.name)
        return entry[1] if entry else JobState.UNKNOWN

    async def cancel(self, handle: JobHandle) -> None:
        entry = self.jobs.get(handle.name)
        if entry is not None:
            self.jobs[handle.name] = (entry[0], JobState.CANCELLED, entry[2], entry[3])
            self.cancelled.append(handle.name)

    async def ensure_gateway_up(self) -> None:
        self.gateway_running = True

    async def gateway_idle_check(self, idle_minutes: int = 30) -> bool:
        stopped, self.gateway_running = self.gateway_running, False
        return stopped

    async def list_jobs(self, prefix: str) -> list[tuple[JobHandle, JobState, datetime]]:
        return [(h, s, t) for name, (h, s, t, _) in self.jobs.items() if name.startswith(prefix)]

    async def list_run_buckets(self) -> list[tuple[str, datetime]]:
        return [(name, self.bucket_created[name]) for name in sorted(self.buckets)]

"""In-memory JobBackend for tests and the `test` profile. Mirrors the get-by-name semantics."""

from datetime import UTC, datetime

from app.execution.backend import JobHandle, JobSpec, JobState
from app.execution.naming import bucket_name


class FakeJobBackend:
    def __init__(self, final_state: JobState = JobState.SUCCEEDED) -> None:
        self.final_state = final_state
        self.jobs: dict[str, tuple[JobHandle, JobState, datetime, JobSpec]] = {}
        self.buckets: set[str] = set()
        self.submit_calls = 0
        self.created = 0
        self.gateway_running = False

    async def ensure_run_bucket(self, run_id: str) -> str:
        name = bucket_name(run_id)
        self.buckets.add(name)
        return name

    async def delete_run_bucket(self, run_id: str) -> None:
        self.buckets.discard(bucket_name(run_id))

    async def submit(self, spec: JobSpec) -> JobHandle:
        self.submit_calls += 1
        existing = self.jobs.get(spec.name)
        if existing is not None:
            return existing[0]
        self.created += 1
        handle = JobHandle(name=spec.name, nebius_id=f"fakejob-{self.created}")
        self.jobs[spec.name] = (handle, self.final_state, datetime.now(UTC), spec)
        return handle

    async def status(self, handle: JobHandle) -> JobState:
        entry = self.jobs.get(handle.name)
        return entry[1] if entry else JobState.UNKNOWN

    async def cancel(self, handle: JobHandle) -> None:
        entry = self.jobs.get(handle.name)
        if entry is not None:
            self.jobs[handle.name] = (entry[0], JobState.CANCELLED, entry[2], entry[3])

    async def ensure_gateway_up(self) -> None:
        self.gateway_running = True

    async def gateway_idle_check(self, idle_minutes: int = 30) -> None:
        self.gateway_running = False

    async def list_jobs(self, prefix: str) -> list[tuple[JobHandle, JobState, datetime]]:
        return [(h, s, t) for name, (h, s, t, _) in self.jobs.items() if name.startswith(prefix)]

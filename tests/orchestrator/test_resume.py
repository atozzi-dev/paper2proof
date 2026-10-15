import pytest

from app.domain.models import RunStatus, StepName
from app.execution.backend import JobKind, JobSpec, VolumeMount
from app.execution.fake_backend import FakeJobBackend
from app.execution.naming import bucket_name, job_name
from app.orchestrator.context import RunContext
from app.orchestrator.fsm import StepResult
from app.storage.store import Store
from tests.orchestrator.conftest import RUN, OrchestratorFactory, make_steps, new_run


class ProcessKilled(BaseException):
    """Simulates the process dying mid-step: not an Exception, so the FSM can't handle it."""


class SubmitThenCrashOnce:
    name = StepName.EXECUTING

    def __init__(self) -> None:
        self.crashed = False
        self.handles: list[str] = []

    async def run(self, ctx: RunContext) -> StepResult:
        spec = JobSpec(
            name=job_name(JobKind.EXEC, ctx.run_id, ctx.attempt),
            kind=JobKind.EXEC,
            image_digest_ref="cr.eu-north1.nebius.cloud/reg/p2p-runner@sha256:" + "b" * 64,
            platform="gpu-l40s-a",
            preset="1gpu-8vcpu-32gb",
            disk_gib=100,
            subnet_id="vpcsubnet-exec",
            volumes=[VolumeMount(source=bucket_name(ctx.run_id), mount_path="/mnt/run", mode="rw")],
            inject_files={},
            env={},
            preemptible=True,
            watchdog_s=2100,
        )
        handle = await ctx.jobs.submit(spec)
        self.handles.append(handle.nebius_id)
        if not self.crashed:
            self.crashed = True
            raise ProcessKilled
        return StepResult(next=StepName.ANALYZING)


async def test_resume_does_not_duplicate_jobs(
    store: Store, make_orchestrator: OrchestratorFactory
) -> None:
    await new_run(store)
    jobs = FakeJobBackend()
    executing = SubmitThenCrashOnce()
    steps = make_steps()
    steps[StepName.EXECUTING] = executing  # type: ignore[assignment]

    with pytest.raises(ProcessKilled):
        await (await make_orchestrator(steps, jobs).submit(RUN))
    run = await store.get_run(RUN)
    assert run is not None and run.status is RunStatus.RUNNING  # left mid-flight

    # "Restart": a new orchestrator over the same DB and the same (remote) job backend.
    tasks = await make_orchestrator(steps, jobs).resume_all()
    assert [await t for t in tasks] == [RunStatus.COMPLETED]
    assert jobs.submit_calls == 2
    assert jobs.created == 1  # same deterministic name -> existing handle reused
    assert executing.handles[0] == executing.handles[1]
    executing_rows = [s for s in await store.get_steps(RUN) if s.name is StepName.EXECUTING]
    assert [(s.attempt, s.status) for s in executing_rows] == [(1, "SUCCEEDED")]


async def test_resume_all_starts_queued_runs_and_skips_finished_ones(
    store: Store, make_orchestrator: OrchestratorFactory
) -> None:
    queued = await new_run(store, "r_01jb7z00000000000000000002")
    done = await new_run(store, "r_01jb7z00000000000000000003")
    await store.set_run_status(done, RunStatus.COMPLETED)
    tasks = await make_orchestrator(make_steps()).resume_all()
    assert [await t for t in tasks] == [RunStatus.COMPLETED]
    run = await store.get_run(queued)
    assert run is not None and run.status is RunStatus.COMPLETED

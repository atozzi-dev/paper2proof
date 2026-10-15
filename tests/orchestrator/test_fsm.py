import asyncio

from app.domain.errors import P2PError
from app.domain.events import ErrorPayload, EventType, RunStatusPayload, StepFinishedPayload
from app.domain.models import ErrorCode, RunStatus, StepName
from app.orchestrator.budget import BudgetExceeded, KillSwitchActive
from app.orchestrator.context import RunContext
from app.orchestrator.fsm import StepResult
from app.storage.store import Store
from tests.orchestrator.conftest import (
    HAPPY,
    RUN,
    FakeStep,
    OrchestratorFactory,
    goes_to,
    make_steps,
    new_run,
)


async def _events(store: Store, run_id: str = RUN) -> list[tuple[EventType, object]]:
    return [(e.type, e.payload) for e in await store.events_after(run_id, 0)]


async def test_happy_path_walks_every_step_and_completes(
    store: Store, make_orchestrator: OrchestratorFactory
) -> None:
    await new_run(store)
    status = await (await make_orchestrator(make_steps()).submit(RUN))
    assert status is RunStatus.COMPLETED
    run = await store.get_run(RUN)
    assert run is not None and run.status is RunStatus.COMPLETED
    steps = await store.get_steps(RUN)
    assert [s.name for s in steps] == list(HAPPY)
    assert all(s.status == "SUCCEEDED" and s.attempt == 1 for s in steps)
    assert steps[0].output_ref == "ref-INGESTING-1"
    events = await _events(store)
    statuses = [p.status for t, p in events if isinstance(p, RunStatusPayload)]
    assert statuses == [RunStatus.RUNNING, RunStatus.COMPLETED]
    started = [t for t, _ in events if t is EventType.STEP_STARTED]
    finished = [p for _, p in events if isinstance(p, StepFinishedPayload)]
    assert len(started) == len(finished) == len(HAPPY)
    assert all(f.ok for f in finished)


async def test_approval_path_and_cancellation(
    store: Store, make_orchestrator: OrchestratorFactory
) -> None:
    await new_run(store)
    steps = make_steps(
        {
            StepName.PLANNING: goes_to(StepName.AWAITING_APPROVAL),
            StepName.AWAITING_APPROVAL: lambda ctx: StepResult(
                next=RunStatus.CANCELLED, error_code=ErrorCode.APPROVAL_TIMEOUT
            ),
        }
    )
    status = await (await make_orchestrator(steps).submit(RUN))
    assert status is RunStatus.CANCELLED
    run = await store.get_run(RUN)
    assert run is not None and run.error_code is ErrorCode.APPROVAL_TIMEOUT


async def test_third_package_request_fails_the_run(
    store: Store, make_orchestrator: OrchestratorFactory
) -> None:
    await new_run(store)
    steps = make_steps({StepName.SMOKE_TESTING: goes_to(StepName.PROVISIONING)})
    status = await (await make_orchestrator(steps).submit(RUN))
    assert status is RunStatus.FAILED
    run = await store.get_run(RUN)
    assert run is not None and run.error_code is ErrorCode.PACKAGE_REJECTED
    assert steps[StepName.PROVISIONING].calls == [1, 2, 3]  # initial + 2 allowed requests
    assert steps[StepName.SMOKE_TESTING].calls == [1, 2, 3]


async def test_smoke_testing_may_loop_on_itself(
    store: Store, make_orchestrator: OrchestratorFactory
) -> None:
    await new_run(store)
    steps = make_steps(
        {
            StepName.SMOKE_TESTING: lambda ctx: StepResult(
                next=StepName.SMOKE_TESTING if ctx.attempt < 3 else StepName.EXECUTING
            )
        }
    )
    assert await (await make_orchestrator(steps).submit(RUN)) is RunStatus.COMPLETED
    assert steps[StepName.SMOKE_TESTING].calls == [1, 2, 3]


def _raise(exc: BaseException) -> FakeStep:
    def behaviour(ctx: RunContext) -> StepResult:
        raise exc

    return FakeStep(StepName.PLANNING, behaviour)


async def test_budget_exceeded_ends_the_run_with_the_limit_name(
    store: Store, make_orchestrator: OrchestratorFactory
) -> None:
    await new_run(store)
    steps = make_steps()
    steps[StepName.PLANNING] = _raise(BudgetExceeded("inference_usd"))
    assert await (await make_orchestrator(steps).submit(RUN)) is RunStatus.BUDGET_EXCEEDED
    run = await store.get_run(RUN)
    assert run is not None and run.error_code is ErrorCode.BUDGET_EXCEEDED
    errors = [p for _, p in await _events(store) if isinstance(p, ErrorPayload)]
    assert errors[-1].code is ErrorCode.BUDGET_EXCEEDED
    assert "inference_usd" in errors[-1].message
    planning = [s for s in await store.get_steps(RUN) if s.name is StepName.PLANNING]
    assert planning[0].status == "FAILED"


async def test_kill_switch_fails_the_run(
    store: Store, make_orchestrator: OrchestratorFactory
) -> None:
    await new_run(store)
    steps = make_steps()
    steps[StepName.PLANNING] = _raise(KillSwitchActive())
    assert await (await make_orchestrator(steps).submit(RUN)) is RunStatus.FAILED
    run = await store.get_run(RUN)
    assert run is not None and run.error_code is ErrorCode.KILL_SWITCH


async def test_expected_errors_keep_their_code(
    store: Store, make_orchestrator: OrchestratorFactory
) -> None:
    await new_run(store)
    steps = make_steps()
    steps[StepName.PLANNING] = _raise(P2PError(ErrorCode.NO_CLAIMS, "no numeric claims found"))
    assert await (await make_orchestrator(steps).submit(RUN)) is RunStatus.FAILED
    errors = [p for _, p in await _events(store) if isinstance(p, ErrorPayload)]
    assert (errors[-1].code, errors[-1].message) == (ErrorCode.NO_CLAIMS, "no numeric claims found")


async def test_unhandled_exception_becomes_internal_error_without_traceback(
    store: Store, make_orchestrator: OrchestratorFactory
) -> None:
    await new_run(store)
    steps = make_steps()
    steps[StepName.PLANNING] = _raise(RuntimeError("boom at /secret/path line 42"))
    assert await (await make_orchestrator(steps).submit(RUN)) is RunStatus.FAILED
    run = await store.get_run(RUN)
    assert run is not None and run.error_code is ErrorCode.INTERNAL_ERROR
    errors = [p for _, p in await _events(store) if isinstance(p, ErrorPayload)]
    assert "secret" not in errors[-1].message
    assert "Traceback" not in errors[-1].message


async def test_illegal_transition_is_rejected(
    store: Store, make_orchestrator: OrchestratorFactory
) -> None:
    await new_run(store)
    steps = make_steps({StepName.INGESTING: goes_to(StepName.EXECUTING)})
    assert await (await make_orchestrator(steps).submit(RUN)) is RunStatus.FAILED
    run = await store.get_run(RUN)
    assert run is not None and run.error_code is ErrorCode.INTERNAL_ERROR
    assert steps[StepName.EXECUTING].calls == []


async def test_concurrent_runs_are_capped(
    store: Store, make_orchestrator: OrchestratorFactory
) -> None:
    running = 0
    peak = 0
    gate = asyncio.Event()

    class Slow:
        name = StepName.INGESTING

        async def run(self, ctx: RunContext) -> StepResult:
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await gate.wait()
            running -= 1
            return StepResult(next=RunStatus.FAILED, error_code=ErrorCode.INGEST_FAILED)

    steps = make_steps()
    steps[StepName.INGESTING] = Slow()  # type: ignore[assignment]
    orchestrator = make_orchestrator(steps)
    run_ids = [f"r_01jb7z0000000000000000000{i}" for i in range(5)]
    for run_id in run_ids:
        await new_run(store, run_id)
    tasks = [await orchestrator.submit(run_id) for run_id in run_ids]
    await asyncio.sleep(0.2)
    assert peak == 2  # global.concurrent_runs in config/budgets.yaml
    gate.set()
    assert set(await asyncio.gather(*tasks)) == {RunStatus.FAILED}


async def test_terminal_runs_are_not_restarted(
    store: Store, make_orchestrator: OrchestratorFactory
) -> None:
    await new_run(store)
    await store.set_run_status(RUN, RunStatus.CANCELLED)
    steps = make_steps()
    assert await (await make_orchestrator(steps).submit(RUN)) is RunStatus.CANCELLED
    assert steps[StepName.INGESTING].calls == []


async def test_cpu_bound_work_runs_in_the_pool(store: Store) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from app.execution.fake_backend import FakeJobBackend
    from app.orchestrator.budget import Budget, load_budget_limits
    from tests.orchestrator.conftest import ROOT

    limits = load_budget_limits(ROOT / "config" / "budgets.yaml")

    def ctx(pool: ThreadPoolExecutor | None) -> RunContext:
        return RunContext(
            run_id=RUN,
            step=StepName.INGESTING,
            attempt=1,
            store=store,
            budget=Budget(store, limits, env="test"),
            limits=limits,
            jobs=FakeJobBackend(),
            cpu_pool=pool,
        )

    with ThreadPoolExecutor(1) as pool:
        assert await ctx(pool).run_cpu(sum, [1, 2, 3]) == 6
    import pytest

    with pytest.raises(RuntimeError, match="no CPU pool"):
        await ctx(None).run_cpu(sum, [1])

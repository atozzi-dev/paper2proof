"""Traces and metrics recorded by the orchestrator, the model-call recorder and the budget."""

from pathlib import Path

import pytest
from opentelemetry.trace import StatusCode
from pydantic import SecretStr

from app import main
from app.domain.errors import P2PError
from app.domain.models import ErrorCode, RunStatus, StepName
from app.models.client import Role, StoreCallRecorder
from app.observability.tracing import run_trace_id
from app.orchestrator.budget import Budget, SpendKind, load_budget_limits
from app.settings import Settings
from app.storage.store import Store
from tests.observability.fakes import Telemetry
from tests.orchestrator.conftest import HAPPY, RUN, OrchestratorFactory, make_steps, new_run

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def telemetry(monkeypatch: pytest.MonkeyPatch) -> Telemetry:
    return Telemetry(monkeypatch)


async def test_a_run_is_one_trace_with_a_span_per_step(
    store: Store, make_orchestrator: OrchestratorFactory, telemetry: Telemetry
) -> None:
    await new_run(store)
    status = await (await make_orchestrator(make_steps()).submit(RUN))
    assert status is RunStatus.COMPLETED
    spans = telemetry.spans()
    assert [s.name for s in spans] == [f"step {name.value}" for name in HAPPY]
    assert {s.context.trace_id for s in spans if s.context} == {run_trace_id(RUN)}
    first = dict(spans[0].attributes or {})
    assert first == {"run_id": RUN, "step": "INGESTING", "attempt": 1}
    assert telemetry.points("p2p_runs_total") == {(("status", "COMPLETED"),): 1}
    assert telemetry.points("p2p_runs_active") == {(): 0}
    durations = telemetry.points("p2p_step_duration_seconds")
    assert durations[(("step", "VERDICT"),)] == 1 and len(durations) == len(HAPPY)


async def test_a_failing_step_marks_its_span_with_the_error_code_only(
    store: Store, make_orchestrator: OrchestratorFactory, telemetry: Telemetry
) -> None:
    def explode(ctx: object) -> object:
        raise P2PError(ErrorCode.NO_CLAIMS, "paper text: secret details")

    await new_run(store)
    steps = make_steps({StepName.EXTRACTING_CLAIMS: explode})  # type: ignore[dict-item]
    assert await (await make_orchestrator(steps).submit(RUN)) is RunStatus.FAILED
    span = telemetry.spans("step EXTRACTING_CLAIMS")[0]
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == "NO_CLAIMS"
    assert span.events == ()  # no exception text leaves the process in a span
    assert telemetry.points("p2p_runs_total") == {(("status", "FAILED"),): 1}


async def test_model_calls_are_traced_and_counted(store: Store, telemetry: Telemetry) -> None:
    await new_run(store)
    await StoreCallRecorder(store).record(
        run_id=RUN,
        step="PLANNING",
        role=Role.PLANNER,
        model_id="vendor/model-x",
        tokens_in=100,
        tokens_out=20,
        latency_ms=40,
        cost_usd=0.01,
        ratelimit_remaining=5000,
        prompt_version="abc",
    )
    (span,) = telemetry.spans("model.call")
    assert span.context is not None and span.context.trace_id == run_trace_id(RUN)
    assert dict(span.attributes or {})["cost_usd"] == 0.01
    tokens = telemetry.points("p2p_model_tokens_total")
    assert tokens[(("dir", "out"), ("model", "vendor/model-x"), ("role", "planner"))] == 20
    assert telemetry.points("p2p_ratelimit_remaining_tokens") == {
        (("model", "vendor/model-x"),): 5000
    }


async def test_committed_spend_is_counted(store: Store, telemetry: Telemetry) -> None:
    await new_run(store)
    budget = Budget(store, load_budget_limits(ROOT / "config" / "budgets.yaml"), env="test")
    reservation = await budget.check_and_reserve(RUN, SpendKind.CLOUD_USD, 1.0)
    await budget.commit(reservation, 0.4)
    gpu = await budget.check_and_reserve(RUN, SpendKind.GPU_MINUTES, 2.0)
    await budget.commit(gpu, 1.5)
    assert telemetry.points("p2p_cost_usd_total") == {(("kind", "cloud"),): 0.4}
    assert telemetry.points("p2p_gpu_seconds_total") == {(): 90.0}


def test_the_observability_key_is_a_known_secret() -> None:
    settings = Settings(observability_static_key=SecretStr("o" * 30))
    assert "o" * 30 in main.known_secrets(settings)

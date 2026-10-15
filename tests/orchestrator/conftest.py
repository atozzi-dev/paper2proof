"""Fake steps and an orchestrator factory for FSM tests."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from app.domain.models import RunStatus, StepName
from app.execution.fake_backend import FakeJobBackend
from app.orchestrator.budget import Budget, load_budget_limits
from app.orchestrator.context import RunContext
from app.orchestrator.fsm import Orchestrator, Step, StepResult
from app.storage.store import Store

ROOT = Path(__file__).resolve().parents[2]
RUN = "r_01jb7z00000000000000000000"

HAPPY: dict[StepName, StepName | RunStatus] = {
    StepName.INGESTING: StepName.EXTRACTING_CLAIMS,
    StepName.EXTRACTING_CLAIMS: StepName.DISCOVERING,
    StepName.DISCOVERING: StepName.PLANNING,
    StepName.PLANNING: StepName.PROVISIONING,
    StepName.PROVISIONING: StepName.SMOKE_TESTING,
    StepName.SMOKE_TESTING: StepName.EXECUTING,
    StepName.EXECUTING: StepName.ANALYZING,
    StepName.ANALYZING: StepName.VERDICT,
    StepName.VERDICT: RunStatus.COMPLETED,
}

Behaviour = Callable[[RunContext], StepResult]


@dataclass
class FakeStep:
    name: StepName
    behaviour: Behaviour
    calls: list[int] = field(default_factory=list)  # attempts seen

    async def run(self, ctx: RunContext) -> StepResult:
        self.calls.append(ctx.attempt)
        return self.behaviour(ctx)


def goes_to(target: StepName | RunStatus) -> Behaviour:
    return lambda ctx: StepResult(next=target, output_ref=f"ref-{ctx.step}-{ctx.attempt}")


def make_steps(overrides: Mapping[StepName, Behaviour] | None = None) -> dict[StepName, FakeStep]:
    steps = {name: FakeStep(name, goes_to(target)) for name, target in HAPPY.items()}
    steps[StepName.AWAITING_APPROVAL] = FakeStep(
        StepName.AWAITING_APPROVAL, goes_to(StepName.PROVISIONING)
    )
    for name, behaviour in (overrides or {}).items():
        steps[name] = FakeStep(name, behaviour)
    return steps


OrchestratorFactory = Callable[..., Orchestrator]


@pytest.fixture
def make_orchestrator(store: Store) -> OrchestratorFactory:
    def factory(steps: Mapping[StepName, Step], jobs: FakeJobBackend | None = None) -> Orchestrator:
        limits = load_budget_limits(ROOT / "config" / "budgets.yaml")
        return Orchestrator(
            store,
            Budget(store, limits, env="test"),
            steps,
            limits=limits,
            jobs=jobs or FakeJobBackend(),
        )

    return factory


async def new_run(store: Store, run_id: str = RUN) -> str:
    await store.create_run(run_id, "2106.09685", created_by=None, claim_hint=None, budget={})
    return run_id

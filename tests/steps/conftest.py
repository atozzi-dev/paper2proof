"""Helpers to run a single step against a real Store with a previous step's output in place."""

import itertools
from pathlib import Path

from app.domain.models import StepName
from app.execution.fake_backend import FakeJobBackend
from app.orchestrator.budget import Budget, load_budget_limits
from app.orchestrator.context import RunContext
from app.storage.store import Store

ROOT = Path(__file__).resolve().parents[2]
RUN = "r_01jb7z00000000000000000000"
LLM_FIXTURES = ROOT / "tests" / "fixtures" / "llm"


async def ctx_at(
    store: Store,
    step: StepName,
    *,
    previous: list[tuple[StepName, str | None]],
    claim_hint: str | None = None,
    arxiv_id: str = "0000.00001",
) -> RunContext:
    """Create the run, walk it through `previous` steps (with their output refs) and return
    the context of `step`, which is left RUNNING (attempt 1)."""
    await store.create_run(RUN, arxiv_id, created_by=None, claim_hint=claim_hint, budget={})
    chain = [*previous, (step, None)]
    await store.begin_run(RUN, chain[0][0], 1)
    for (name, ref), (next_name, _) in itertools.pairwise(chain):
        await store.transition(RUN, name, 1, "SUCCEEDED", output_ref=ref, next_step=(next_name, 1))
    limits = load_budget_limits(ROOT / "config" / "budgets.yaml")
    return RunContext(
        run_id=RUN,
        step=step,
        attempt=1,
        store=store,
        budget=Budget(store, limits, env="test"),
        limits=limits,
        jobs=FakeJobBackend(),
        cpu_pool=None,
    )

"""Helpers to run a single step against a real Store with a previous step's output in place."""

import itertools
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.domain.models import StepName
from app.execution.fake_backend import FakeJobBackend
from app.orchestrator.budget import Budget, load_budget_limits
from app.orchestrator.context import RunContext
from app.storage.store import Store

ROOT = Path(__file__).resolve().parents[2]
RUN = "r_01jb7z00000000000000000000"
LLM_FIXTURES = ROOT / "tests" / "fixtures" / "llm"
CPU_POOL = ThreadPoolExecutor(max_workers=2)  # the app uses a process pool; threads suffice here


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
    seen: dict[StepName, int] = {}
    attempts = []
    for name, _ in chain:  # repeated steps get increasing attempts, like the FSM
        seen[name] = seen.get(name, 0) + 1
        attempts.append(seen[name])
    await store.begin_run(RUN, chain[0][0], attempts[0])
    for i, ((name, ref), (next_name, _)) in enumerate(itertools.pairwise(chain)):
        await store.transition(
            RUN,
            name,
            attempts[i],
            "SUCCEEDED",
            output_ref=ref,
            next_step=(next_name, attempts[i + 1]),
        )
    limits = load_budget_limits(ROOT / "config" / "budgets.yaml")
    return RunContext(
        run_id=RUN,
        step=step,
        attempt=attempts[-1],
        store=store,
        budget=Budget(store, limits, env="test"),
        limits=limits,
        jobs=FakeJobBackend(),
        cpu_pool=CPU_POOL,
    )

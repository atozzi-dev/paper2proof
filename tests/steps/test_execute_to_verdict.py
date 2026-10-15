"""ANALYZING and VERDICT on fakes: untrusted results in, a deterministic verdict out."""

import json
from pathlib import Path
from typing import Any

import pytest

from app.domain.errors import P2PError
from app.domain.events import VerdictReadyPayload
from app.domain.models import (
    Claim,
    ClaimType,
    ErrorCode,
    Plan,
    PlanMode,
    ReasonCode,
    RunStatus,
    Scale,
    StepName,
    VerdictKind,
)
from app.execution.run_storage import FakeRunStorage
from app.models.fake import FakeModelClient
from app.orchestrator.context import RunContext
from app.orchestrator.steps.analyzing import AnalyzingStep
from app.orchestrator.steps.verdict import VerdictStep
from app.storage.artifacts import LocalArtifactStore
from app.storage.store import Store
from tests.steps.conftest import LLM_FIXTURES, ROOT, RUN, ctx_at

FIXTURES = ROOT / "tests" / "fixtures" / "contracts"
EXECUTION_KEY = f"runs/{RUN}/execution_1.json"
PLAN_KEY = f"runs/{RUN}/plan.json"
CLAIM = Claim(
    id="c1",
    text="Ours beats ResNet-18 by 1.7 points on CIFAR-10",
    claim_type=ClaimType.RELATIVE,
    metric="top1_accuracy",
    higher_is_better=True,
    dataset="CIFAR-10",
    setting="200 epochs",
    reported_value=None,
    baseline_name="ResNet-18",
    claimed_delta=1.7,
    source_ref="Table 2",
)


FULL = {"full": True}


def _manifest(full: bool = False) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / "manifest_valid.json").read_text())
    if full:
        data["scale"] = FULL
    return data


def _results(values: list[float], baseline: list[float], **changes: Any) -> bytes:
    data: dict[str, Any] = json.loads((FIXTURES / "results_valid.json").read_text())
    data["per_seed"] = [{"seed": i, "value": v, "ok": True} for i, v in enumerate(values)]
    data["baseline"]["per_seed"] = [{"seed": i, "value": v} for i, v in enumerate(baseline)]
    data["exit_reason"] = "completed"
    data.update(changes)
    return json.dumps(data).encode()


async def _setup(
    store: Store, tmp_path: Path, results: bytes, step: StepName, full: bool = False
) -> tuple[RunContext, LocalArtifactStore, FakeRunStorage]:
    artifacts = LocalArtifactStore(tmp_path)
    plan = Plan(
        mode=PlanMode.FROM_SCRATCH, claim_ids=["c1"], claim_types={"c1": ClaimType.RELATIVE},
        scale=Scale.REDUCED, seeds=[0, 1, 2], gpu_minutes_est=10,
        notes=["Smaller model than the paper."],
    )  # fmt: skip
    artifacts.put_json(PLAN_KEY, {"plan": plan.model_dump(mode="json"), "est_cost_usd": 0.1})
    artifacts.put_json(
        EXECUTION_KEY,
        {
            "manifest": _manifest(full),
            "results_key": "results/1/results.json",
            "job_state": "SUCCEEDED",
        },
    )
    storage = FakeRunStorage()
    storage.objects[(RUN, "results/1/results.json")] = results
    previous: list[tuple[StepName, str | None]] = [
        (StepName.INGESTING, "i"),
        (StepName.EXTRACTING_CLAIMS, "c"),
        (StepName.DISCOVERING, "d"),
        (StepName.PLANNING, PLAN_KEY),
        (StepName.PROVISIONING, "p"),
        (StepName.SMOKE_TESTING, "s"),
        (StepName.EXECUTING, EXECUTION_KEY),
    ]
    if step is StepName.VERDICT:
        previous.append((StepName.ANALYZING, None))
    ctx = await ctx_at(store, step, previous=previous)
    await store.save_claims(RUN, [CLAIM], selected={"c1"})
    return ctx, artifacts, storage


async def _verdict(
    store: Store, tmp_path: Path, results: bytes, case: str = "honest", full: bool = False
) -> tuple[Any, list[Any]]:
    ctx, artifacts, storage = await _setup(store, tmp_path, results, StepName.ANALYZING, full)
    analyzed = await AnalyzingStep(artifacts, storage).run(ctx)
    assert analyzed.next is StepName.VERDICT and analyzed.output_ref is not None
    await store.transition(
        RUN, StepName.ANALYZING, 1, "SUCCEEDED", output_ref=analyzed.output_ref,
        next_step=(StepName.VERDICT, 1),
    )  # fmt: skip
    vctx = RunContext(**{**ctx.__dict__, "step": StepName.VERDICT})
    step = VerdictStep(FakeModelClient(LLM_FIXTURES, case=case), artifacts)
    result = await step.run(vctx)
    assert result.next is RunStatus.COMPLETED
    events = [e.payload for e in await store.events_after(RUN, 0)]
    return await store.get_verdict(RUN, "c1"), events


async def test_a_negative_reduced_scale_result_is_inconclusive(
    store: Store, tmp_path: Path
) -> None:
    # REDUCED scale + clearly negative delta -> INCONCLUSIVE(REDUCED_SCALE_CONTRADICTS)
    verdict, events = await _verdict(
        store, tmp_path, _results([0.90, 0.91, 0.90], [0.92, 0.93, 0.92])
    )
    assert verdict is not None
    assert verdict.kind is VerdictKind.INCONCLUSIVE
    assert verdict.reason is ReasonCode.REDUCED_SCALE_CONTRADICTS
    ready = next(p for p in events if isinstance(p, VerdictReadyPayload))
    assert "Limitations" in ready.explanation_md
    assert "10% of the data" in ready.explanation_md


async def test_a_contradicting_judge_is_replaced_by_the_template(
    store: Store, tmp_path: Path
) -> None:
    _, events = await _verdict(
        store, tmp_path, _results([0.90, 0.91, 0.90], [0.92, 0.93, 0.92]), case="contradicting"
    )
    ready = next(p for p in events if isinstance(p, VerdictReadyPayload))
    assert "REPRODUCED. The method clearly" not in ready.explanation_md
    assert ready.explanation_md.startswith("Inconclusive (reduced scale contradicts)")


async def test_a_timed_out_run_is_inconclusive_for_budget(store: Store, tmp_path: Path) -> None:
    raw = _results([0.95], [0.90], exit_reason="budget_timeout")
    verdict, _ = await _verdict(store, tmp_path, raw)
    assert verdict is not None
    assert (verdict.kind, verdict.reason) == (VerdictKind.INCONCLUSIVE, ReasonCode.BUDGET)


@pytest.mark.parametrize(
    "raw",
    [
        b"x" * (300 * 1024),
        _results([0.9, 0.9, 0.9], [0.8, 0.8, 0.8], run_id="r_01jb7z00000000000000000009"),
    ],
    ids=["300KB", "foreign-run"],
)
async def test_broken_results_are_contract_errors(store: Store, tmp_path: Path, raw: bytes) -> None:
    ctx, artifacts, storage = await _setup(store, tmp_path, raw, StepName.ANALYZING)
    with pytest.raises(P2PError) as err:
        await AnalyzingStep(artifacts, storage).run(ctx)
    assert err.value.code is ErrorCode.CONTRACT_ERROR


async def test_a_full_scale_failure_is_not_reproduced_in_the_judges_words(
    store: Store, tmp_path: Path
) -> None:
    raw = _results([0.90, 0.91, 0.90], [0.92, 0.93, 0.92], scale=FULL)
    verdict, events = await _verdict(store, tmp_path, raw, full=True)
    assert verdict is not None and verdict.kind is VerdictKind.NOT_REPRODUCED
    ready = next(p for p in events if isinstance(p, VerdictReadyPayload))
    assert ready.explanation_md.startswith("Not reproduced under our conditions. The method was")
    assert "full scale" in ready.explanation_md

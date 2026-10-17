"""The conditions a verdict was reached under, exposed from what the run stores (invariant 10):
the plan, the exec manifest, the analysis of the results and how the GPU job ran."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.api.main import AppState
from app.domain.models import ClaimType, Measurements, Plan, PlanMode, Scale, StepName
from app.storage.artifacts import LocalArtifactStore
from app.storage.store import Store
from app.verdict.results import Analysis
from tests.api.conftest import login

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "tests" / "fixtures" / "contracts" / "manifest_valid.json"


async def _run(client: httpx.AsyncClient) -> str:
    await login(client)
    response = await client.post("/api/runs", json={"arxiv_id": "2106.09685", "claim_hint": None})
    run_id: str = response.json()["run_id"]
    return run_id


def _plan() -> dict[str, Any]:
    plan = Plan(
        mode=PlanMode.FROM_SCRATCH, claim_ids=["c1"], claim_types={"c1": ClaimType.RELATIVE},
        scale=Scale.REDUCED, seeds=[0, 1, 2], gpu_minutes_est=10,
        notes=["Smaller model than the paper."],
    )  # fmt: skip
    return {"plan": plan.model_dump(mode="json"), "est_cost_usd": 0.1}


def _manifest(run_id: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(MANIFEST.read_text(encoding="utf-8"))
    data["run_id"] = run_id
    return data


async def _store_run(
    store: Store,
    artifacts: LocalArtifactStore,
    run_id: str,
    *,
    hardware: dict[str, Any] | None,
    analysis: Any = None,
) -> None:
    prefix = f"runs/{run_id}"
    artifacts.put_json(f"{prefix}/plan.json", _plan())
    execution: dict[str, Any] = {
        "manifest": _manifest(run_id),
        "results_key": "results/1/results.json",
        "exit_reason": None,
        "job_state": "SUCCEEDED",
    }
    if hardware is not None:
        execution["hardware"] = hardware
    artifacts.put_json(f"{prefix}/execution_1.json", execution)
    if analysis is None:
        analysis = {
            "analysis": Analysis(
                exit_reason="completed",
                measurements=Measurements(
                    scale=Scale.REDUCED, per_seed=[0.9, 0.91], baseline_per_seed=[0.8, 0.81]
                ),
                used_seeds=[0, 1],
                wall_clock_s=1200.0,
                gpu="NVIDIA L40S",
            ).model_dump(mode="json"),
            "manifest": _manifest(run_id),
            "log_summary": None,
        }
    artifacts.put_json(f"{prefix}/analysis_1.json", analysis)
    steps = [
        (StepName.PLANNING, f"{prefix}/plan.json"),
        (StepName.EXECUTING, f"{prefix}/execution_1.json"),
        (StepName.ANALYZING, f"{prefix}/analysis_1.json"),
    ]
    await store.begin_run(run_id, steps[0][0], 1)
    following_steps = [s for s, _ in steps[1:]] + [StepName.VERDICT]
    for (step, ref), following in zip(steps, following_steps, strict=True):
        await store.transition(
            run_id, step, 1, "SUCCEEDED", output_ref=ref, next_step=(following, 1)
        )


GPU = {"platform": "gpu-l40s-a", "preset": "1gpu-8vcpu-32gb", "preemptible": True}


@pytest.fixture
def artifacts(state: AppState, tmp_path: Path) -> LocalArtifactStore:
    state.artifacts = LocalArtifactStore(tmp_path / "artifacts")
    return state.artifacts


async def test_the_run_view_carries_the_conditions_of_its_verdict(
    client: httpx.AsyncClient, store: Store, artifacts: LocalArtifactStore
) -> None:
    run_id = await _run(client)
    await _store_run(store, artifacts, run_id, hardware=GPU)
    body = (await client.get(f"/api/runs/{run_id}")).json()
    assert body["conditions"] == {
        "attempt": 1,
        "scale": {"scale": "REDUCED", "fraction_of_data": 0.1, "epochs": 5, "paper_epochs": 200},
        "seeds_planned": [0, 1, 2],
        "seeds_used": [0, 1],
        "hardware": {**GPU, "gpu": "NVIDIA L40S"},
        "code": "FROM_SCRATCH",
        "deviations": ["Smaller model than the paper."],
        "exit_reason": "completed",
    }
    assert "runs/" not in json.dumps(body)  # storage keys stay internal


async def test_no_conditions_before_the_results_are_analyzed(
    client: httpx.AsyncClient, artifacts: LocalArtifactStore
) -> None:
    run_id = await _run(client)
    assert (await client.get(f"/api/runs/{run_id}")).json()["conditions"] is None


async def test_a_job_that_did_not_record_its_hardware_reports_none(
    client: httpx.AsyncClient, store: Store, artifacts: LocalArtifactStore
) -> None:
    run_id = await _run(client)
    await _store_run(store, artifacts, run_id, hardware=None)
    hardware = (await client.get(f"/api/runs/{run_id}")).json()["conditions"]["hardware"]
    assert hardware == {"platform": None, "preset": None, "preemptible": None, "gpu": "NVIDIA L40S"}


async def test_an_unreadable_analysis_never_breaks_the_run_view(
    client: httpx.AsyncClient, store: Store, artifacts: LocalArtifactStore
) -> None:
    run_id = await _run(client)
    await _store_run(store, artifacts, run_id, hardware=GPU, analysis={"analysis": "garbage"})
    response = await client.get(f"/api/runs/{run_id}")
    assert response.status_code == 200
    assert response.json()["conditions"] is None


async def test_without_an_artifact_store_there_are_no_conditions(
    client: httpx.AsyncClient, store: Store, tmp_path: Path
) -> None:
    run_id = await _run(client)
    await _store_run(store, LocalArtifactStore(tmp_path / "elsewhere"), run_id, hardware=GPU)
    assert (await client.get(f"/api/runs/{run_id}")).json()["conditions"] is None

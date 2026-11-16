from typing import get_args

from app.domain.events import JobKindName, JobLogPayload, JobStatusPayload, PlanReadyPayload
from app.domain.models import ClaimType, ExecTarget, Plan, PlanMode, Scale
from app.execution.backend import JobKind


def test_job_kind_names_are_exactly_the_job_kinds() -> None:
    assert set(get_args(JobKindName)) == {kind.value for kind in JobKind}


def test_the_fetch_job_has_its_own_event_kind() -> None:
    status = JobStatusPayload(kind="fetch", name="p2p-fetch-x-1", state="RUNNING")
    assert status.kind == JobKind.FETCH.value
    assert JobLogPayload(kind="fetch", lines=["ok"]).kind == "fetch"


def _plan() -> Plan:
    return Plan(
        mode=PlanMode.FROM_SCRATCH,
        claim_ids=["c1"],
        claim_types={"c1": ClaimType.ABSOLUTE},
        scale=Scale.REDUCED,
        seeds=[0, 1, 2],
        gpu_minutes_est=20,
    )


def test_plan_ready_from_before_the_gpu_target_still_validates() -> None:
    payload = PlanReadyPayload(plan=_plan(), est_cost_usd=0.2, needs_approval=False)
    assert payload.target is ExecTarget.CPU
    assert (payload.cpu_plan, payload.est_cost_max_usd, payload.gpu_skipped) == (None, None, None)


def test_plan_ready_carries_the_gpu_target_and_its_fallback() -> None:
    payload = PlanReadyPayload(
        plan=_plan(),
        est_cost_usd=0.9,
        needs_approval=True,
        target=ExecTarget.GPU,
        cpu_plan=_plan(),
        est_cost_max_usd=1.7,
    )
    assert payload.model_dump(mode="json")["target"] == "gpu"

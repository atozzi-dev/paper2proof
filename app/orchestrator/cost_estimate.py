"""Deterministic cost estimate of a plan (shown to the user; drives auto-approval)."""

from app.domain.models import Plan
from app.models.pricing import CloudPrices

EXEC_PLATFORM, EXEC_PRESET = "gpu-l40s-a", "1gpu-8vcpu-32gb"  # execution-contracts §8
PROVISION_PLATFORM, PROVISION_PRESET = "cpu-d3", "4vcpu-16gb"
PROVISION_MINUTES = 20  # provisioning watchdog: the worst case we pay for


def estimate_plan_cost(
    plan: Plan, cloud: CloudPrices, *, inference_so_far: float, preemptible: bool
) -> float:
    """Inference already spent + GPU minutes of the plan + one provisioning job (on demand)."""
    gpu = (
        plan.gpu_minutes_est
        / 60
        * cloud.hourly(EXEC_PLATFORM, EXEC_PRESET, preemptible=preemptible)
    )
    provision = PROVISION_MINUTES / 60 * cloud.hourly(PROVISION_PLATFORM, PROVISION_PRESET)
    return round(inference_so_far + gpu + provision, 4)

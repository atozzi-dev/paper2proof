"""Deterministic cost estimate of a plan (shown to the user; drives auto-approval)."""

from app.domain.models import Plan
from app.models.pricing import CloudPrices

EXEC_PLATFORM, EXEC_PRESET = "gpu-l40s-a", "1gpu-8vcpu-32gb"  # execution-contracts §8
# Where the exec job goes when gpu-l40s-a has no capacity (execution-contracts §8).
EXEC_FALLBACK_PLATFORM, EXEC_FALLBACK_PRESET = "gpu-l40s-d", "1gpu-16vcpu-96gb"
PROVISION_PLATFORM, PROVISION_PRESET = "cpu-d3", "4vcpu-16gb"
PROVISION_MINUTES = 20  # provisioning watchdog (provision + fetch job): the worst case we pay


def estimate_plan_cost(
    plan: Plan, cloud: CloudPrices, *, inference_so_far: float, preemptible: bool
) -> float:
    """Inference already spent + GPU minutes of the plan + the provisioning jobs (on demand;
    one after the other, 20 minutes in all)."""
    gpu = (
        plan.gpu_minutes_est
        / 60
        * cloud.hourly(EXEC_PLATFORM, EXEC_PRESET, preemptible=preemptible)
    )
    provision = PROVISION_MINUTES / 60 * cloud.hourly(PROVISION_PLATFORM, PROVISION_PRESET)
    return round(inference_so_far + gpu + provision, 4)

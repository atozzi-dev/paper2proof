"""Deterministic cost estimate of a plan (shown to the user; drives auto-approval)."""

from app.domain.models import Plan
from app.execution.config import EXEC_FALLBACK, EXEC_PLATFORM, EXEC_PRESET
from app.models.pricing import CloudPrices

EXEC_FALLBACK_PLATFORM, EXEC_FALLBACK_PRESET = EXEC_FALLBACK
__all__ = [
    "EXEC_FALLBACK_PLATFORM",
    "EXEC_FALLBACK_PRESET",
    "EXEC_PLATFORM",
    "EXEC_PRESET",
    "PROVISION_PLATFORM",
    "PROVISION_PRESET",
    "estimate_plan_cost",
]
PROVISION_PLATFORM, PROVISION_PRESET = "cpu-d3", "4vcpu-16gb"
PROVISION_MINUTES = 20  # provisioning watchdog (provision + fetch job): the worst case we pay


def estimate_plan_cost(
    plan: Plan,
    cloud: CloudPrices,
    *,
    inference_so_far: float,
    preemptible: bool,
    exec_on: tuple[str, str] = (EXEC_PLATFORM, EXEC_PRESET),
) -> float:
    """Inference already spent + the plan's experiment minutes on `exec_on` (the configured exec
    platform and preset) + the provisioning jobs (on demand; one after the other, 20 minutes)."""
    gpu = plan.gpu_minutes_est / 60 * cloud.hourly(*exec_on, preemptible=preemptible)
    provision = PROVISION_MINUTES / 60 * cloud.hourly(PROVISION_PLATFORM, PROVISION_PRESET)
    return round(inference_so_far + gpu + provision, 4)

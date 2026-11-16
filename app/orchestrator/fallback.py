"""The GPU fallback's traces in a run (A-47, GPU when needed): where EXECUTING records that a
GPU run ran its CPU plan, and the package requests that leaves to count. No step imports here:
the FSM reads it too."""

from collections.abc import Sequence

from app.domain.models import StepName
from app.storage.store import StepRecord


def fallback_key(run_id: str) -> str:
    """runs/<id>/gpu_fallback.json: a GpuFallback, written once by EXECUTING; it is also that
    EXECUTING attempt's output_ref, so the steps' records alone tell that it happened."""
    return f"runs/{run_id}/gpu_fallback.json"


def fell_back(steps: Sequence[StepRecord]) -> bool:
    """Whether EXECUTING sent this run back to PROVISIONING on its CPU plan."""
    return any(
        s.name is StepName.EXECUTING
        and s.status == "SUCCEEDED"
        and s.output_ref is not None
        and s.output_ref.endswith("/gpu_fallback.json")
        for s in steps
    )


def package_requests(steps: Sequence[StepRecord], provisioning_attempts: int) -> int:
    """How many of the run's first `provisioning_attempts` PROVISIONING attempts were package
    requests: every return to PROVISIONING but the one the GPU fallback starts. Counted when
    that attempt has started (from SMOKE_TESTING, and after the fallback, it has)."""
    return max(provisioning_attempts - 1 - int(fell_back(steps)), 0)

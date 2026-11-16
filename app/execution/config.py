"""Execution-plane settings: image digests and subnets (filled from infra outputs, A-03/H-04)."""

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

DIGEST_REF = r"^[a-z0-9.\-/:]+@sha256:[0-9a-f]{64}$"  # invariant 8: images by digest only
EXEC_PLATFORM, EXEC_PRESET = "gpu-l40s-a", "1gpu-8vcpu-32gb"  # execution-contracts §8
# Where the exec job goes when gpu-l40s-a has no capacity (execution-contracts §8).
EXEC_FALLBACK = ("gpu-l40s-d", "1gpu-16vcpu-96gb")


def has_time_target(path: Path, image: str) -> bool:
    """Whether this runner image's digest is listed as built with the exec manifest's time
    target (config/runner_images.yaml, A-46). A missing file lists nothing."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return False
    listed = data.get("time_target") if isinstance(data, dict) else None
    return isinstance(listed, list) and image.rsplit("@", 1)[-1] in listed


class ExecutionConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provisioner_image: str = Field(pattern=DIGEST_REF)
    runner_image: str = Field(pattern=DIGEST_REF)
    runner_cpu_image: str = Field(pattern=DIGEST_REF)
    subnet_provision: str = Field(min_length=1)
    subnet_exec: str = Field(min_length=1)
    preemptible_gpu: bool  # P2P_GPU_PREEMPTIBLE: explicit, never defaulted (no for demos)
    # where smoke jobs run: a CPU platform, or the GPU one in the hybrid deploy (no CPU quota)
    smoke_platform: str = Field(default="cpu-d3", min_length=1)
    smoke_preset: str = Field(default="4vcpu-16gb", min_length=1)
    # where the exec job runs: the GPU platform with its fallback, or Sandboxes (hybrid deploy
    # without GPU capacity: no fallback, docs/specs/hybrid-deploy.md)
    exec_platform: str = Field(default=EXEC_PLATFORM, min_length=1)
    exec_preset: str = Field(default=EXEC_PRESET, min_length=1)
    exec_fallback: tuple[str, str] | None = EXEC_FALLBACK

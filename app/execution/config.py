"""Execution-plane settings: image digests and subnets (filled from infra outputs, A-03/H-04)."""

from pydantic import BaseModel, ConfigDict, Field

DIGEST_REF = r"^[a-z0-9.\-/:]+@sha256:[0-9a-f]{64}$"  # invariant 8: images by digest only


class ExecutionConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provisioner_image: str = Field(pattern=DIGEST_REF)
    runner_image: str = Field(pattern=DIGEST_REF)
    runner_cpu_image: str = Field(pattern=DIGEST_REF)
    subnet_provision: str = Field(min_length=1)
    subnet_exec: str = Field(min_length=1)
    preemptible_gpu: bool  # P2P_GPU_PREEMPTIBLE: explicit, never defaulted (no for demos)

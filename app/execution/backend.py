"""Job backend contract (docs/specs/execution-contracts.md §9).

Implementations: NebiusJobBackend (nebius_backend.py, A-20) and FakeJobBackend (fake_backend.py).
"""

import re
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_INJECT_BYTES = 64 * 1024
# Jobs never receive credentials (invariant 4): refuse env names that look like one.
_SECRET_ENV = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)", re.IGNORECASE)


class JobKind(StrEnum):
    PROVISION = "prov"
    EXEC = "exec"
    SMOKE = "smoke"


class JobState(StrEnum):
    PENDING = "PENDING"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"

    @property
    def is_terminal(self) -> bool:
        return self in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED)


class VolumeMount(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str = Field(min_length=1, max_length=63)  # bucket name
    mount_path: str = Field(pattern=r"^/mnt/[a-z0-9/_-]+$")
    mode: Literal["ro", "rw"]


class JobSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^p2p-(prov|exec|smoke)-[0-9a-z]{26}-[0-9]{1,4}$")
    kind: JobKind
    image_digest_ref: str = Field(pattern=r"^[a-z0-9.\-/:]+@sha256:[0-9a-f]{64}$")  # invariant 8
    platform: str
    preset: str
    disk_gib: int = Field(ge=10, le=500)
    subnet_id: str = Field(min_length=1)
    volumes: list[VolumeMount] = Field(max_length=4)
    inject_files: dict[str, bytes]  # container path -> content (each <= 64 KiB)
    env: dict[str, str]  # never secrets
    preemptible: bool
    watchdog_s: int = Field(gt=0)

    @field_validator("volumes")
    @classmethod
    def _unique_mounts(cls, volumes: list[VolumeMount]) -> list[VolumeMount]:
        paths = [v.mount_path for v in volumes]
        if len(set(paths)) != len(paths):
            raise ValueError("duplicate mount paths")
        return volumes

    @field_validator("inject_files")
    @classmethod
    def _small_absolute_files(cls, files: dict[str, bytes]) -> dict[str, bytes]:
        for path, content in files.items():
            if not path.startswith("/") or ".." in path:
                raise ValueError(f"inject path must be absolute: {path!r}")
            if len(content) > MAX_INJECT_BYTES:
                raise ValueError(f"injected file too large: {path!r}")
        return files

    @field_validator("env")
    @classmethod
    def _no_secrets(cls, env: dict[str, str]) -> dict[str, str]:
        bad = [k for k in env if _SECRET_ENV.search(k)]
        if bad:
            raise ValueError(f"jobs never receive credentials: {bad}")
        return env


class JobHandle(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    nebius_id: str


class JobBackend(Protocol):
    async def ensure_run_bucket(self, run_id: str) -> str: ...
    async def delete_run_bucket(self, run_id: str) -> None: ...
    async def submit(self, spec: JobSpec) -> JobHandle: ...  # get-by-name first
    async def status(self, handle: JobHandle) -> JobState: ...
    async def cancel(self, handle: JobHandle) -> None: ...
    async def ensure_gateway_up(self) -> None: ...  # start the VM if stopped, wait until ready
    async def gateway_idle_check(self, idle_minutes: int = 30) -> None: ...
    async def list_jobs(self, prefix: str) -> list[tuple[JobHandle, JobState, datetime]]: ...

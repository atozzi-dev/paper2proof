"""Job backend contract (docs/specs/execution-contracts.md §9).

Implementations: NebiusJobBackend (nebius_backend.py, A-20) and FakeJobBackend (fake_backend.py).
"""

import re
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_INJECT_BYTES = 64 * 1024
MAX_INJECT_FILES = 8
MAX_INJECT_TOTAL = 256 * 1024
INJECT_PREFIX = "/etc/p2p/"
CACHE_BUCKET = "p2p-cache"
CACHE_MOUNT = "/mnt/cache"
# Jobs run untrusted code: they get an allowlisted environment and never credentials
# (invariant 4). Names are allowlisted; names and values are also screened for secrets.
_ENV_ALLOWED = frozenset(
    {
        "PYTHONUNBUFFERED",
        "PYTHONHASHSEED",
        "PYTHONDONTWRITEBYTECODE",
        "HF_HUB_OFFLINE",
        "HF_DATASETS_OFFLINE",
        "TRANSFORMERS_OFFLINE",
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "CUDA_VISIBLE_DEVICES",
        "TZ",
    }
)
_ENV_NAME = re.compile(r"^P2P_[A-Z0-9_]{1,60}$")
_SECRET_NAME = re.compile(
    r"KEY|TOKEN|SECRET|PASS|PWD|AUTH|CREDENTIAL|PRIVATE|CERT|DSN|COOKIE|SESSION|BEARER",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(
    r"://[^/@\s]+@|-----BEGIN|\b(?:sk-|tvly-|hf_|ghp_|AKIA)[A-Za-z0-9_-]{8,}"
    r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"  # JWTs (IAM and OIDC tokens)
)


class JobKind(StrEnum):
    PROVISION = "prov"  # packages only: the one job that writes the overlay cache
    FETCH = "fetch"  # repository and datasets (untrusted bytes): never mounts the cache
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

    name: str = Field(pattern=r"^p2p-(prov|fetch|exec|smoke)-[0-9a-z]{26}-[0-9]{1,4}$")
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
    def _small_files_under_etc_p2p(cls, files: dict[str, bytes]) -> dict[str, bytes]:
        if len(files) > MAX_INJECT_FILES:
            raise ValueError(f"at most {MAX_INJECT_FILES} injected files")
        if sum(len(c) for c in files.values()) > MAX_INJECT_TOTAL:
            raise ValueError("injected files too large in total")
        for path, content in files.items():
            if "\x00" in path or "\\" in path or ".." in path:
                raise ValueError(f"invalid inject path: {path!r}")
            if not path.startswith(INJECT_PREFIX):
                raise ValueError(f"inject paths must be under {INJECT_PREFIX}: {path!r}")
            if len(content) > MAX_INJECT_BYTES:
                raise ValueError(f"injected file too large: {path!r}")
        return files

    @field_validator("env")
    @classmethod
    def _allowlisted_env(cls, env: dict[str, str]) -> dict[str, str]:
        for name, value in env.items():
            if name not in _ENV_ALLOWED and not _ENV_NAME.fullmatch(name):
                raise ValueError(f"env var not allowed in jobs: {name}")
            if _SECRET_NAME.search(name) or _SECRET_VALUE.search(value):
                raise ValueError(f"jobs never receive credentials: {name}")
        return env

    @model_validator(mode="after")
    def _consistent_and_least_privilege(self) -> "JobSpec":
        kind_in_name, slug = self.name.split("-")[1:3]
        if kind_in_name != self.kind.value:
            raise ValueError("job name prefix does not match its kind")
        own_bucket = f"p2p-run-{slug}"
        for volume in self.volumes:
            if volume.source == CACHE_BUCKET:
                if volume.mount_path != CACHE_MOUNT:  # the provisioner's phases rely on it
                    raise ValueError(f"the cache mounts only at {CACHE_MOUNT}")
                if self.kind is JobKind.FETCH:
                    raise ValueError("the fetch job never mounts the cache")
                if volume.mode == "rw" and self.kind is not JobKind.PROVISION:
                    raise ValueError("only the provisioner may write the cache")
            elif volume.source != own_bucket:
                raise ValueError(f"jobs may mount only {own_bucket} and {CACHE_BUCKET}")
        return self


class JobHandle(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    nebius_id: str


class CapacityError(Exception):
    """submit(): the platform has no capacity for the job's platform and preset right now (GPU
    quota used up, "not enough resources"). The job was not created (execution-contracts §8)."""


class JobBackend(Protocol):
    async def ensure_run_bucket(self, run_id: str) -> str: ...
    async def delete_run_bucket(self, run_id: str) -> None: ...
    async def submit(self, spec: JobSpec) -> JobHandle: ...  # get-by-name first
    async def status(self, handle: JobHandle) -> JobState: ...
    async def cancel(self, handle: JobHandle) -> None: ...
    async def ensure_gateway_up(self) -> None: ...  # start the VM if stopped, wait until ready
    async def gateway_idle_check(self, idle_minutes: int = 30) -> bool: ...  # True: stopped it
    async def list_jobs(self, prefix: str) -> list[tuple[JobHandle, JobState, datetime]]: ...
    async def list_run_buckets(self) -> list[tuple[str, datetime]]: ...  # (name, created_at)

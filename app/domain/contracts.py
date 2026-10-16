"""Pydantic mirrors of the runner contracts in schemas/manifest.schema.json and
schemas/results.schema.json. tests/contracts keeps both sides in agreement."""

import re
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.models import ClaimType

RUN_ID_PATTERN = r"^r_[0-9a-z]{26}$"
ENTRYPOINT_PATTERN = r"^workspace/[A-Za-z0-9_./-]+\.py$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
ARCHIVE_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,200}$"
# --seed/--out/--smoke (and argparse prefixes), short clusters with -s/-o, @file
RUNNER_FLAG = re.compile(r"^(--(s|se|see|seed|sm|smo|smok|smoke|o|ou|out)(=.*)?$|-[A-Za-z]*[so]|@)")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ScaleSpec(_Strict):
    full: bool
    fraction_of_data: float | None = Field(default=None, gt=0, le=1)
    epochs: int | None = Field(default=None, ge=1)
    paper_epochs: int | None = Field(default=None, ge=1)


class DataArchive(_Strict):
    name: str = Field(pattern=ARCHIVE_NAME_PATTERN)
    sha256: str = Field(pattern=SHA256_PATTERN)

    @field_validator("name")
    @classmethod
    def _not_dots(cls, name: str) -> str:
        if set(name) == {"."}:
            raise ValueError("invalid archive name")
        return name


class ArchiveDigests(_Strict):
    """sha256 of every archive the runner extracts, taken from the validated provision report
    (never from the shared bucket): the runner checks each one while extracting."""

    overlay: str = Field(pattern=SHA256_PATTERN)
    repo: str | None = Field(pattern=SHA256_PATTERN)
    data: list[DataArchive] = Field(max_length=3)


class ManifestPaths(_Strict):
    overlay: str
    repo: str | None
    data_dir: str
    workspace: str
    cache: str
    results: str


class ExecManifest(_Strict):
    """p2p.manifest/v1: injected into the exec job at /etc/p2p/manifest.json (<= 64 KiB)."""

    schema_: Literal["p2p.manifest/v1"] = Field(alias="schema")
    run_id: str = Field(pattern=RUN_ID_PATTERN)
    attempt: int = Field(ge=1, le=5)
    image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    entrypoint: str = Field(pattern=ENTRYPOINT_PATTERN)
    args: list[str] = Field(max_length=20)
    seeds: list[int] = Field(min_length=1, max_length=20)
    budget_seconds: int = Field(ge=60, le=1800)
    paths: ManifestPaths
    claim_ref: str
    metric: str = Field(max_length=64)
    higher_is_better: bool
    claim_type: ClaimType
    baseline_name: str | None = Field(max_length=128)
    scale: ScaleSpec
    archives: ArchiveDigests

    @field_validator("entrypoint")
    @classmethod
    def _no_traversal(cls, value: str) -> str:
        if ".." in value:
            raise ValueError("entrypoint must not contain '..'")
        return value

    @field_validator("args")
    @classmethod
    def _short_args(cls, args: list[str]) -> list[str]:
        if any(len(a) > 200 for a in args):
            raise ValueError("each arg must be at most 200 characters")
        if any(RUNNER_FLAG.match(a) for a in args):
            raise ValueError("args may not set --seed, --out or --smoke")
        return args

    @field_validator("seeds")
    @classmethod
    def _unique_seeds(cls, seeds: list[int]) -> list[int]:
        if len(set(seeds)) != len(seeds) or any(s < 0 for s in seeds):
            raise ValueError("seeds must be unique non-negative integers")
        return seeds

    @model_validator(mode="after")
    def _relative_needs_baseline(self) -> Self:
        if self.claim_type is ClaimType.RELATIVE and not self.baseline_name:
            raise ValueError("RELATIVE claims need baseline_name")
        return self


class SeedResult(_Strict):
    seed: int
    value: float | None
    ok: bool | None = None

    @model_validator(mode="after")
    def _null_only_when_failed(self) -> Self:
        if self.value is None and self.ok is not False:
            raise ValueError("value may be null only when ok is false")
        return self


class BaselineSeed(_Strict):
    seed: int
    value: float | None


class Baseline(_Strict):
    name: str = Field(max_length=128)
    per_seed: list[BaselineSeed] = Field(max_length=20)


class Results(_Strict):
    """p2p.results/v1: written by the runner. Untrusted: see execution-contracts §7.1."""

    schema_: Literal["p2p.results/v1"] = Field(alias="schema")
    run_id: str = Field(pattern=RUN_ID_PATTERN)
    attempt: int = Field(ge=1)
    claim_ref: str
    claim_type: ClaimType
    metric: str = Field(max_length=64)
    higher_is_better: bool
    scale: ScaleSpec
    per_seed: list[SeedResult] = Field(max_length=20)
    baseline: Baseline | None = None
    wall_clock_s: float = Field(ge=0)
    gpu: str = Field(max_length=128)
    exit_reason: Literal["completed", "experiment_error", "budget_timeout", "contract_error"]

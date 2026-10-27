"""Pydantic mirrors of the runner contracts in schemas/manifest.schema.json and
schemas/results.schema.json. tests/contracts keeps both sides in agreement."""

import re
from typing import Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from app.domain.models import ClaimType

RUN_ID_PATTERN = r"^r_[0-9a-z]{26}$"
ENTRYPOINT_PATTERN = r"^workspace/[A-Za-z0-9_./-]+\.py$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
ARCHIVE_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,200}$"
# A metrics.json key: plain characters, never starting with a dot or a slash.
METRIC_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_./-]{0,63}$"
MAX_CLAIMS_PER_JOB = 5
# --seed/--out/--smoke (and argparse prefixes), short clusters with -s/-o, @file
RUNNER_FLAG = re.compile(r"^(--(s|se|see|seed|sm|smo|smok|smoke|o|ou|out)(=.*)?$|-[A-Za-z]*[so]|@)")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ScaleSpec(_Strict):
    full: bool
    fraction_of_data: float | None = Field(default=None, gt=0, le=1)
    epochs: int | None = Field(default=None, ge=1)
    paper_epochs: int | None = Field(default=None, ge=1)

    @model_serializer(mode="wrap")
    def _omit_unset(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """Unset fields are left out, never null: the manifest and results schemas type them
        as numbers, and the runner echoes the manifest's scale into results.json."""
        data: dict[str, Any] = handler(self)
        return {key: value for key, value in data.items() if value is not None}


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


class ClaimSpec(_Strict):
    """One claim the job measures: where its value is in metrics.json (`metric_key`, and
    `baseline_key` for a RELATIVE claim's baseline), and how the verdict reads it."""

    claim_ref: str = Field(min_length=1, max_length=64)
    claim_type: ClaimType
    metric: str = Field(min_length=1, max_length=64)  # the claim's metric (tolerances)
    higher_is_better: bool
    metric_key: str = Field(pattern=METRIC_KEY_PATTERN)
    baseline_name: str | None = Field(max_length=128)
    baseline_key: str | None = Field(pattern=METRIC_KEY_PATTERN)

    @model_validator(mode="after")
    def _baseline_for_relative_only(self) -> Self:
        if self.claim_type is ClaimType.RELATIVE:
            if not self.baseline_name or self.baseline_key is None:
                raise ValueError("RELATIVE claims need baseline_name and baseline_key")
            if self.baseline_key == self.metric_key:  # a delta of 0 by construction
                raise ValueError("baseline_key must differ from metric_key")
        elif self.baseline_name is not None or self.baseline_key is not None:
            raise ValueError("ABSOLUTE claims have no baseline")
        return self


def _unique_refs(refs: list[str]) -> None:
    if len(set(refs)) != len(refs):
        raise ValueError("claim_ref must be unique")


class ExecManifest(_Strict):
    """p2p.manifest/v2: injected into the exec job at /etc/p2p/manifest.json (<= 64 KiB)."""

    schema_: Literal["p2p.manifest/v2"] = Field(alias="schema")
    run_id: str = Field(pattern=RUN_ID_PATTERN)
    attempt: int = Field(ge=1, le=5)
    image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    entrypoint: str = Field(pattern=ENTRYPOINT_PATTERN)
    args: list[str] = Field(max_length=20)
    seeds: list[int] = Field(min_length=1, max_length=20)
    budget_seconds: int = Field(ge=60, le=1800)
    paths: ManifestPaths
    claims: list[ClaimSpec] = Field(min_length=1, max_length=MAX_CLAIMS_PER_JOB)
    scale: ScaleSpec
    archives: ArchiveDigests
    workspace_sha256: str = Field(pattern=SHA256_PATTERN)  # of the tar at paths.workspace

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
    def _one_spec_per_claim(self) -> Self:
        _unique_refs([c.claim_ref for c in self.claims])
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
    key: str = Field(pattern=METRIC_KEY_PATTERN)
    per_seed: list[BaselineSeed] = Field(max_length=20)


class ClaimResults(_Strict):
    claim_ref: str = Field(min_length=1, max_length=64)
    claim_type: ClaimType
    metric: str = Field(min_length=1, max_length=64)
    metric_key: str = Field(pattern=METRIC_KEY_PATTERN)
    higher_is_better: bool
    per_seed: list[SeedResult] = Field(max_length=20)
    baseline: Baseline | None


class Results(_Strict):
    """p2p.results/v2: written by the runner. Untrusted: see execution-contracts §7.1."""

    schema_: Literal["p2p.results/v2"] = Field(alias="schema")
    run_id: str = Field(pattern=RUN_ID_PATTERN)
    attempt: int = Field(ge=1)
    scale: ScaleSpec
    claims: list[ClaimResults] = Field(min_length=1, max_length=MAX_CLAIMS_PER_JOB)
    wall_clock_s: float = Field(ge=0)
    gpu: str = Field(max_length=128)
    exit_reason: Literal["completed", "experiment_error", "budget_timeout", "contract_error"]

    @model_validator(mode="after")
    def _one_result_per_claim(self) -> Self:
        _unique_refs([c.claim_ref for c in self.claims])
        return self

"""Shared domain types (docs/specs/verdict-engine.md, architecture §6.1). Defined once, reused."""

from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- Verdict engine types (docs/specs/verdict-engine.md) ---------------------------------


class ClaimType(StrEnum):
    ABSOLUTE = "ABSOLUTE"
    RELATIVE = "RELATIVE"


class Scale(StrEnum):
    FULL = "FULL"
    REDUCED = "REDUCED"


class VerdictKind(StrEnum):
    REPRODUCED = "REPRODUCED"
    REPRODUCED_AT_REDUCED_SCALE = "REPRODUCED_AT_REDUCED_SCALE"
    NOT_REPRODUCED = "NOT_REPRODUCED"
    INCONCLUSIVE = "INCONCLUSIVE"


class ReasonCode(StrEnum):
    BUDGET = "BUDGET"
    CAPACITY = "CAPACITY"
    MISSING_DATA = "MISSING_DATA"
    RUNTIME_ERROR = "RUNTIME_ERROR"
    UNDERSPECIFIED_CLAIM = "UNDERSPECIFIED_CLAIM"
    INSUFFICIENT_SEEDS = "INSUFFICIENT_SEEDS"
    HIGH_VARIANCE = "HIGH_VARIANCE"
    REDUCED_SCALE_ABSOLUTE = "REDUCED_SCALE_ABSOLUTE"
    REDUCED_SCALE_CONTRADICTS = "REDUCED_SCALE_CONTRADICTS"
    IMPLAUSIBLE_GAP = "IMPLAUSIBLE_GAP"  # ABSOLUTE, more than implausible_ratio times off
    NOT_MEASURED = "NOT_MEASURED"  # the experiment does not measure this claim (code-set)


class Claim(_Strict):
    id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1)
    claim_type: ClaimType
    metric: str = Field(min_length=1, max_length=64)
    higher_is_better: bool
    dataset: str
    setting: str
    reported_value: float | None  # required for ABSOLUTE; None -> UNDERSPECIFIED_CLAIM
    reported_std: float | None = None
    baseline_name: str | None = None  # required for RELATIVE
    claimed_delta: float | None = None  # RELATIVE; None means "just better" (treated as 0)
    source_ref: str  # "Table 2", "Sec 4.1", "Fig 3"


class Measurements(_Strict):
    scale: Scale
    per_seed: list[float]
    baseline_per_seed: list[float] | None = None  # same length and seed order as per_seed

    @model_validator(mode="after")
    def _baseline_aligned(self) -> Self:
        if self.baseline_per_seed is not None and len(self.baseline_per_seed) != len(self.per_seed):
            raise ValueError("baseline_per_seed must have the same length as per_seed")
        return self


class VerdictConfig(_Strict):
    tol_rel: float = Field(default=0.02, ge=0)
    tol_abs: dict[str, float] = Field(default_factory=dict)  # per metric name
    default_tol_abs: float = Field(default=0.0, ge=0)
    rho: float = Field(default=0.5, ge=0)
    min_seeds: int = Field(default=3, ge=2)
    confidence: float = Field(default=0.95, gt=0, lt=1)
    # ABSOLUTE: a mean more than this many times off the paper's value (same sign) is a likely
    # broken experiment or unit mismatch, never NOT_REPRODUCED. Defaulted so that configs
    # written before the rule still load.
    # None disables the implausible-gap rule; finite only (verdict.json is strict JSON)
    implausible_ratio: float | None = Field(default=3.0, gt=1, allow_inf_nan=False)


class Verdict(_Strict):
    kind: VerdictKind
    reason: ReasonCode | None  # set iff kind == INCONCLUSIVE
    n_seeds: int = Field(ge=0)
    mean: float | None
    std: float | None
    ci_low: float | None
    ci_high: float | None
    tolerance: dict[str, float]  # {"w": ..., "v": ...} or {"rho": ..., "delta": ...}

    @model_validator(mode="after")
    def _reason_iff_inconclusive(self) -> Self:
        if (self.reason is not None) != (self.kind is VerdictKind.INCONCLUSIVE):
            raise ValueError("reason must be set if and only if kind is INCONCLUSIVE")
        return self


# --- Orchestration types (architecture §6.1) ----------------------------------------------


class StepName(StrEnum):
    INGESTING = "INGESTING"
    EXTRACTING_CLAIMS = "EXTRACTING_CLAIMS"
    DISCOVERING = "DISCOVERING"
    PLANNING = "PLANNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    PROVISIONING = "PROVISIONING"
    SMOKE_TESTING = "SMOKE_TESTING"
    EXECUTING = "EXECUTING"
    ANALYZING = "ANALYZING"
    VERDICT = "VERDICT"


class RunStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"

    @property
    def is_terminal(self) -> bool:
        return self not in (RunStatus.QUEUED, RunStatus.RUNNING)


class ErrorCode(StrEnum):
    SCHEMA_ERROR = "SCHEMA_ERROR"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    KILL_SWITCH = "KILL_SWITCH"
    CAPACITY = "CAPACITY"
    TIMEOUT = "TIMEOUT"
    PROVISION_FAILED = "PROVISION_FAILED"
    SMOKE_EXHAUSTED = "SMOKE_EXHAUSTED"
    PACKAGE_REJECTED = "PACKAGE_REJECTED"
    RUNTIME_ERROR = "RUNTIME_ERROR"
    CONTRACT_ERROR = "CONTRACT_ERROR"
    INGEST_FAILED = "INGEST_FAILED"
    NO_CLAIMS = "NO_CLAIMS"
    APPROVAL_TIMEOUT = "APPROVAL_TIMEOUT"
    INTERNAL_ERROR = "INTERNAL_ERROR"  # unhandled exception inside a step


class PlanMode(StrEnum):
    REPO_FIRST = "REPO_FIRST"
    FROM_SCRATCH = "FROM_SCRATCH"


class PackageReq(_Strict):
    """A package as requested (Planner or Coder). Kept raw: the package policy (A-19) judges it."""

    name: str = Field(min_length=1, max_length=200)
    specifier: str | None = Field(default=None, max_length=100)


class DatasetReq(_Strict):
    kind: Literal["hf"]
    id: str = Field(min_length=1, max_length=200)
    split: str | None = None
    max_bytes: int = Field(default=2_000_000_000, gt=0, le=10_000_000_000)


class Plan(_Strict):
    mode: PlanMode
    claim_ids: list[str] = Field(min_length=1)
    claim_types: dict[str, ClaimType]
    scale: Scale
    seeds: list[int] = Field(min_length=1, max_length=20)
    packages: list[PackageReq] = Field(default_factory=list)
    datasets: list[DatasetReq] = Field(default_factory=list)
    entrypoint_hint: str | None = None
    gpu_minutes_est: float = Field(ge=0)
    notes: list[str] = Field(default_factory=list)

    @field_validator("seeds")
    @classmethod
    def _unique_seeds(cls, seeds: list[int]) -> list[int]:
        if len(set(seeds)) != len(seeds) or any(s < 0 for s in seeds):
            raise ValueError("seeds must be unique non-negative integers")
        return seeds

    @model_validator(mode="after")
    def _types_for_selected_claims(self) -> Self:
        if set(self.claim_types) != set(self.claim_ids):
            raise ValueError("claim_types must have exactly one entry per claim id")
        return self

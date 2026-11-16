"""Shared domain types (docs/specs/verdict-engine.md, architecture §6.1). Defined once, reused."""

from datetime import datetime
from enum import StrEnum
from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
    model_validator,
)


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
    BASELINE_MISMATCH = "BASELINE_MISMATCH"  # RELATIVE: our baseline did not reach the paper's
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
    implausible_ratio: float | None = Field(default=2.0, gt=1, allow_inf_nan=False)
    # RELATIVE at full scale: a baseline worse than the paper's own by more than this many
    # tolerance bands is not the paper's experiment (verdict-engine.md, Baseline
    # calibration). None disables the rule; defaulted like implausible_ratio.
    # At least 1: a narrower band would call a baseline inside the verdict's own tolerance
    # worse than the paper's.
    calibration_factor: float | None = Field(default=2.0, ge=1, allow_inf_nan=False)


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


class SetupKind(StrEnum):
    NUMBER = "NUMBER"  # compared as numbers
    CHOICE = "CHOICE"  # words (an optimizer, a protocol): matched by the Coder's declaration


class SetupItem(_Strict):
    """One condition of the experiment that the paper states (the Planner lists them; code
    compares). Every text field is model text read from the paper: untrusted, bounded."""

    # Read leniently (PLANNING cuts the text to 80 characters, fills `planned` and renumbers
    # the ids): a long value must not invalidate the whole plan.
    id: str = Field(min_length=1, max_length=40)  # code renumbers them: "s1", "s2", ...
    name: str = Field(min_length=1, max_length=300)
    kind: SetupKind
    paper: str = Field(min_length=1, max_length=300)  # the value as the paper writes it
    # What this run is planned to use; None is the paper's value (PLANNING fills it in), so
    # the Planner writes it only to plan something else, never to say the same in other words.
    planned: str | None = Field(default=None, max_length=300)
    source_ref: str = Field(default="", max_length=300)  # where the paper says so


class SetupRecord(_Strict):
    """What the run used for one SetupItem, as SMOKE_TESTING recorded it at finish
    (smoke_result.json `setup`). Code writes `matches_paper`: for a number by comparing it
    with the paper's, for a choice from the Coder's own declaration."""

    id: str = Field(min_length=1, max_length=8)
    name: str = Field(min_length=1, max_length=80)
    kind: SetupKind
    paper: str = Field(min_length=1, max_length=80)
    planned: str = Field(min_length=1, max_length=80)
    used: str = Field(min_length=1, max_length=80)  # the value the Coder declared
    where: str = Field(min_length=1, max_length=200)  # the file it is written in
    matches_paper: StrictBool


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
    # Lenient bound: PLANNING keeps the first usable ones (setup_check.MAX_SETUP_ITEMS).
    setup: list[SetupItem] = Field(default_factory=list, max_length=40)

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


class ExecTarget(StrEnum):
    """Where a run's experiment executes (A-47): a Token Factory Sandbox on CPU, or one GPU."""

    CPU = "cpu"
    GPU = "gpu"


class PlanArtifact(_Strict):
    """runs/<id>/plan.json, PLANNING's output. A GPU target carries its worst-case cost (the
    whole GPU allowance) and, with P2P_EXEC_BACKEND=auto, the CPU plan it falls back to when no
    GPU starts in time; a CPU target has neither. The two-key plan.json of runs planned before
    A-47 reads as CPU."""

    plan: Plan
    est_cost_usd: float = Field(ge=0)
    target: ExecTarget = ExecTarget.CPU
    cpu_plan: Plan | None = None
    est_cost_max_usd: float | None = Field(default=None, ge=0)
    gpu_skipped: str | None = Field(default=None, max_length=300)  # why a GPU was not used

    @model_validator(mode="after")
    def _fallback_only_for_gpu(self) -> Self:
        gpu = self.target is ExecTarget.GPU
        if gpu != (self.est_cost_max_usd is not None) or (not gpu and self.cpu_plan is not None):
            raise ValueError(
                "a GPU target has a maximum cost (and, but for the designed deploy, a "
                "CPU fallback plan); a CPU target neither"
            )
        return self


class FallbackReason(StrEnum):
    """Why a GPU run ran its CPU plan instead (A-47)."""

    NO_CAPACITY = "NO_CAPACITY"
    QUEUE_TIMEOUT = "QUEUE_TIMEOUT"
    QUOTA = "QUOTA"
    PLATFORM_ERROR = "PLATFORM_ERROR"
    DAILY_CAP = "DAILY_CAP"


class GpuFallback(_Strict):
    """runs/<id>/gpu_fallback.json: written once by EXECUTING when the GPU job never ran."""

    reason: FallbackReason
    message: str = Field(min_length=1, max_length=300)
    at: datetime
    gpu_attempt: int = Field(ge=1)

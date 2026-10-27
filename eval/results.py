"""Result files of ReproBench-mini (architecture §18), written to eval/results/*.json by the
runners and read by eval/report.py. Shared types only: no I/O, no model calls."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.models import ErrorCode, ReasonCode, RunStatus, VerdictKind

RESULT_FILES = {
    "set_a": "set_a.json",
    "set_b": "set_b.json",
    "set_c": "set_c.json",
    "ablation": "ablation.json",
}


class _Result(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- scores ----------------------------------------------------------------------------------


class ExtractionScore(_Result):
    matched: int = Field(ge=0)
    predicted: int = Field(ge=0)
    truth: int = Field(ge=0)
    precision: float = Field(ge=0, le=1)
    recall: float = Field(ge=0, le=1)
    f1: float = Field(ge=0, le=1)


class ClaimOutcome(_Result):
    """One ground-truth claim of set A and what the pipeline said about it."""

    truth_id: str
    planted: bool  # a false claim planted on purpose
    expected: VerdictKind
    predicted_id: str | None  # the pipeline claim matched to it (None: not extracted)
    predicted: VerdictKind | None  # None: no verdict (not matched, not selected, run failed)
    reason: ReasonCode | None = None

    @property
    def correct(self) -> bool:
        return self.predicted == self.expected

    @property
    def false_accusation(self) -> bool:
        """A true claim reported as not reproduced: the error that matters most (§18.2)."""
        return not self.planted and self.predicted is VerdictKind.NOT_REPRODUCED


class VerdictScore(_Result):
    claims: int = Field(ge=0)
    correct: int = Field(ge=0)
    accuracy: float | None  # None when there are no claims
    false_accusations: int = Field(ge=0)
    missing: int = Field(ge=0)  # claims with no verdict at all
    inconclusive: int = Field(ge=0)
    judged: int = Field(ge=0)  # claims with a decisive verdict (not INCONCLUSIVE, not missing)
    judged_correct: int = Field(ge=0)
    coverage: float | None  # judged / claims: how much of the paper the pipeline could test
    judged_accuracy: float | None  # correct among the judged: None when nothing was judged


class CostSummary(_Result):
    total_usd: float  # of the runs that reported a cost
    runs_without_cost: int = Field(ge=0)  # not started, or no cost.update event
    median_usd_per_claim: float | None
    median_duration_s: float | None


# --- sets A and B: full pipeline -----------------------------------------------------------


class RunInfo(_Result):
    run_id: str | None
    status: RunStatus | None  # None: the run could not be started or followed
    error_code: ErrorCode | None = None
    error: str | None = None  # the harness's own failure (exception type), not provider text
    cost_usd: float | None = None
    duration_s: float | None = None


class CaseA(_Result):
    name: str
    arxiv_id: str
    run: RunInfo
    extraction: ExtractionScore
    claims: list[ClaimOutcome]


class SetAResult(_Result):
    kind: Literal["set_a"] = "set_a"
    generated_at: datetime
    cases: list[CaseA]
    verdicts: VerdictScore
    extraction: ExtractionScore
    cost: CostSummary


class VerdictOutcome(_Result):
    claim_id: str
    kind: VerdictKind
    reason: ReasonCode | None = None


class CaseB(_Result):
    arxiv_id: str
    run: RunInfo
    verdicts: list[VerdictOutcome]


class SetBResult(_Result):
    kind: Literal["set_b"] = "set_b"
    generated_at: datetime
    cases: list[CaseB]
    by_kind: dict[VerdictKind, int]
    by_reason: dict[ReasonCode, int]  # INCONCLUSIVE reasons
    inconclusive_rate: float | None  # INCONCLUSIVE verdicts / verdicts; target <= 0.40
    failed_runs: int = Field(ge=0)
    cost: CostSummary


# --- set C: extraction only (Batch API) and the routing ablation ----------------------------

Modality = Literal["text", "table", "figure"]


class CaseC(_Result):
    arxiv_id: str
    extraction: ExtractionScore
    recall_by_modality: dict[Modality, float]
    error: str | None = None  # no usable Reader output (batch error, schema error, missing)


class SetCResult(_Result):
    kind: Literal["set_c"] = "set_c"
    generated_at: datetime
    arm: str  # "routed" or "all-ultra"
    models: dict[str, str]  # role -> resolved model ID used for this arm
    batch_id: str | None
    batch_status: str | None
    cases: list[CaseC]
    extraction: ExtractionScore
    recall_by_modality: dict[Modality, float]
    tokens_in: int = Field(ge=0)
    tokens_out: int = Field(ge=0)
    cost_usd: float | None  # priced usage at the batch price
    charged_usd: float = Field(ge=0)  # what the budget was charged


class AblationArm(_Result):
    arm: str
    models: dict[str, str]
    extraction: ExtractionScore
    cost_usd: float | None


class AblationResult(_Result):
    kind: Literal["ablation"] = "ablation"
    generated_at: datetime
    papers: int = Field(ge=0)
    arms: list[AblationArm]


class EvalResults(_Result):
    """Everything in eval/results/ (a set not run yet is None)."""

    set_a: SetAResult | None = None
    set_b: SetBResult | None = None
    set_c: SetCResult | None = None
    ablation: AblationResult | None = None

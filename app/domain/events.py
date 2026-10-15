"""SSE event payloads (docs/specs/api-and-events.md) and the stored/streamed envelope."""

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.models import Claim, ErrorCode, Plan, RunStatus, StepName, Verdict

MAX_LINES = 50
JobKindName = Literal["prov", "exec", "smoke"]  # values of app.execution.backend.JobKind


class EventType(StrEnum):
    RUN_STATUS = "run.status"
    STEP_STARTED = "step.started"
    STEP_FINISHED = "step.finished"
    CLAIM_EXTRACTED = "claim.extracted"
    PLAN_READY = "plan.ready"
    APPROVAL_REQUIRED = "approval.required"
    MODEL_CALL = "model.call"
    SANDBOX_OUTPUT = "sandbox.output"
    JOB_STATUS = "job.status"
    JOB_LOG = "job.log"
    EGRESS_BLOCKED = "egress.blocked"
    VERDICT_READY = "verdict.ready"
    COST_UPDATE = "cost.update"
    WARNING = "warning"
    ERROR = "error"


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunStatusPayload(_Payload):
    status: RunStatus
    error_code: ErrorCode | None = None


class StepStartedPayload(_Payload):
    step: StepName
    attempt: int = Field(ge=1)


class StepFinishedPayload(_Payload):
    step: StepName
    attempt: int = Field(ge=1)
    ok: bool
    error_code: ErrorCode | None = None
    duration_ms: int = Field(ge=0)


class ClaimExtractedPayload(_Payload):
    claim: Claim


class PlanReadyPayload(_Payload):
    plan: Plan
    est_cost_usd: float = Field(ge=0)
    needs_approval: bool


class ApprovalRequiredPayload(_Payload):
    expires_at: datetime


class ModelCallPayload(_Payload):
    role: str  # app.models.client.Role value
    model_id: str
    tokens_in: int = Field(ge=0)
    tokens_out: int = Field(ge=0)
    latency_ms: int = Field(ge=0)
    cost_usd: float | None  # None when the model has no price in config/pricing.yaml


class SandboxOutputPayload(_Payload):
    iteration: int = Field(ge=0)
    branch: int | None = None
    stream: Literal["stdout", "stderr"]
    lines: list[str] = Field(max_length=MAX_LINES)


class JobStatusPayload(_Payload):
    kind: JobKindName
    name: str
    state: str


class JobLogPayload(_Payload):
    kind: JobKindName
    lines: list[str] = Field(max_length=MAX_LINES)


class EgressBlockedPayload(_Payload):
    host: str
    subnet: str


class VerdictReadyPayload(_Payload):
    claim_id: str
    verdict: Verdict
    explanation_md: str


class CostUpdatePayload(_Payload):
    inference_usd: float = Field(ge=0)
    cloud_usd: float = Field(ge=0)
    total_usd: float = Field(ge=0)


class WarningPayload(_Payload):
    """Non-fatal condition shown in the timeline (e.g. DISCOVERY_DEGRADED, ABSOLUTE at REDUCED)."""

    code: str = Field(min_length=1, max_length=64)
    message: str = Field(max_length=2000)


class ErrorPayload(_Payload):
    """User-visible error. Never a stack trace."""

    code: ErrorCode
    message: str = Field(max_length=2000)


EventPayload = (
    RunStatusPayload
    | StepStartedPayload
    | StepFinishedPayload
    | ClaimExtractedPayload
    | PlanReadyPayload
    | ApprovalRequiredPayload
    | ModelCallPayload
    | SandboxOutputPayload
    | JobStatusPayload
    | JobLogPayload
    | EgressBlockedPayload
    | VerdictReadyPayload
    | CostUpdatePayload
    | WarningPayload
    | ErrorPayload
)

PAYLOAD_MODELS: dict[EventType, type[_Payload]] = {
    EventType.RUN_STATUS: RunStatusPayload,
    EventType.STEP_STARTED: StepStartedPayload,
    EventType.STEP_FINISHED: StepFinishedPayload,
    EventType.CLAIM_EXTRACTED: ClaimExtractedPayload,
    EventType.PLAN_READY: PlanReadyPayload,
    EventType.APPROVAL_REQUIRED: ApprovalRequiredPayload,
    EventType.MODEL_CALL: ModelCallPayload,
    EventType.SANDBOX_OUTPUT: SandboxOutputPayload,
    EventType.JOB_STATUS: JobStatusPayload,
    EventType.JOB_LOG: JobLogPayload,
    EventType.EGRESS_BLOCKED: EgressBlockedPayload,
    EventType.VERDICT_READY: VerdictReadyPayload,
    EventType.COST_UPDATE: CostUpdatePayload,
    EventType.WARNING: WarningPayload,
    EventType.ERROR: ErrorPayload,
}


class EventEnvelope(BaseModel):
    """One stored event; `id` is the monotonic SSE id used for Last-Event-ID resumption."""

    model_config = ConfigDict(extra="forbid")

    id: int = Field(ge=1)
    run_id: str = Field(pattern=r"^r_[0-9a-z]{26}$")
    ts: datetime
    type: EventType
    payload: EventPayload

    @model_validator(mode="before")
    @classmethod
    def _payload_matches_type(cls, data: Any) -> Any:
        # Validate the payload with the model for its declared type, so a structurally similar
        # payload of another event type can never be accepted by the union.
        if isinstance(data, dict) and "type" in data and "payload" in data:
            model = PAYLOAD_MODELS[EventType(data["type"])]
            payload = data["payload"]
            if not isinstance(payload, model):
                payload = model.model_validate(payload)
            data = {**data, "payload": payload}
        return data

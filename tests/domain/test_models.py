from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.domain.events import EventEnvelope, EventType, RunStatusPayload, WarningPayload
from app.domain.models import (
    Claim,
    ClaimType,
    DatasetReq,
    ErrorCode,
    Measurements,
    PackageReq,
    Plan,
    PlanMode,
    ReasonCode,
    RunStatus,
    Scale,
    StepName,
    Verdict,
    VerdictConfig,
    VerdictKind,
)

ROOT = Path(__file__).resolve().parents[2]


def _claim(**overrides: object) -> Claim:
    data: dict[str, object] = {
        "id": "c1",
        "text": "Our model reaches 94.1% top-1 on CIFAR-10.",
        "claim_type": ClaimType.ABSOLUTE,
        "metric": "top1_accuracy",
        "higher_is_better": True,
        "dataset": "cifar10",
        "setting": "ResNet-18, 200 epochs",
        "reported_value": 0.941,
        "source_ref": "Table 2",
    }
    data.update(overrides)
    return Claim.model_validate(data)


def test_absolute_claim_without_reported_value_is_constructible() -> None:
    claim = _claim(reported_value=None)
    assert claim.reported_value is None
    assert claim.claim_type is ClaimType.ABSOLUTE


def test_claim_rejects_empty_metric() -> None:
    with pytest.raises(ValidationError):
        _claim(metric="")


def test_claim_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        _claim(verdict="REPRODUCED")


def test_measurements_require_aligned_baseline() -> None:
    Measurements(scale=Scale.FULL, per_seed=[0.9, 0.91], baseline_per_seed=[0.8, 0.81])
    with pytest.raises(ValidationError, match="same length"):
        Measurements(scale=Scale.FULL, per_seed=[0.9, 0.91], baseline_per_seed=[0.8])


def test_verdict_config_matches_config_file() -> None:
    raw = yaml.safe_load((ROOT / "config" / "verdict.yaml").read_text(encoding="utf-8"))
    cfg = VerdictConfig.model_validate(raw)
    assert cfg.min_seeds == 3
    assert cfg.tol_abs["top1_accuracy"] == 0.005


def test_verdict_config_rejects_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        VerdictConfig.model_validate({"tol_rel": 0.02, "tolerance": 1})


def _verdict(kind: VerdictKind, reason: ReasonCode | None) -> Verdict:
    return Verdict(
        kind=kind,
        reason=reason,
        n_seeds=3,
        mean=0.9,
        std=0.01,
        ci_low=0.88,
        ci_high=0.92,
        tolerance={"w": 0.02, "v": 0.941},
    )


def test_verdict_reason_set_iff_inconclusive() -> None:
    _verdict(VerdictKind.INCONCLUSIVE, ReasonCode.HIGH_VARIANCE)
    _verdict(VerdictKind.REPRODUCED, None)
    with pytest.raises(ValidationError):
        _verdict(VerdictKind.INCONCLUSIVE, None)
    with pytest.raises(ValidationError):
        _verdict(VerdictKind.NOT_REPRODUCED, ReasonCode.BUDGET)


def test_error_codes_cover_the_roadmap_list() -> None:
    required = {
        "SCHEMA_ERROR",
        "MODEL_UNAVAILABLE",
        "BUDGET_EXCEEDED",
        "KILL_SWITCH",
        "CAPACITY",
        "TIMEOUT",
        "PROVISION_FAILED",
        "SMOKE_EXHAUSTED",
        "PACKAGE_REJECTED",
        "RUNTIME_ERROR",
        "CONTRACT_ERROR",
        "INGEST_FAILED",
        "NO_CLAIMS",
    }
    assert required <= {code.value for code in ErrorCode}


def test_run_status_terminal_states() -> None:
    terminal = {s for s in RunStatus if s.is_terminal}
    assert terminal == {
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.BUDGET_EXCEEDED,
    }


def test_step_names_follow_the_fsm() -> None:
    assert [s.value for s in StepName] == [
        "INGESTING",
        "EXTRACTING_CLAIMS",
        "DISCOVERING",
        "PLANNING",
        "AWAITING_APPROVAL",
        "PROVISIONING",
        "SMOKE_TESTING",
        "EXECUTING",
        "ANALYZING",
        "VERDICT",
    ]


def _plan(**overrides: object) -> Plan:
    data: dict[str, object] = {
        "mode": PlanMode.REPO_FIRST,
        "claim_ids": ["c1"],
        "claim_types": {"c1": ClaimType.RELATIVE},
        "scale": Scale.REDUCED,
        "seeds": [0, 1, 2],
        "packages": [PackageReq(name="timm", specifier="==1.0.9")],
        "datasets": [DatasetReq(kind="hf", id="uoft-cs/cifar10", split="train")],
        "entrypoint_hint": "train.py",
        "gpu_minutes_est": 12.5,
        "notes": [],
    }
    data.update(overrides)
    return Plan.model_validate(data)


def test_plan_accepts_a_well_formed_plan() -> None:
    plan = _plan()
    assert plan.datasets[0].max_bytes == 2_000_000_000


@pytest.mark.parametrize(
    "overrides",
    [
        {"seeds": []},
        {"seeds": [1, 1, 2]},
        {"gpu_minutes_est": -1},
        {"claim_ids": []},
        {"claim_types": {"c2": ClaimType.ABSOLUTE}},
    ],
)
def test_plan_rejects_malformed_plans(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _plan(**overrides)


def test_package_req_keeps_raw_names_for_the_policy() -> None:
    # Syntax checks belong to the package policy (A-19), which must see what the Coder asked for.
    assert PackageReq(name="git+https://evil/x").name == "git+https://evil/x"


def test_event_envelope_parses_payload_by_type() -> None:
    env = EventEnvelope.model_validate(
        {
            "id": 1,
            "run_id": "r_01jb7z00000000000000000000",
            "ts": datetime(2026, 10, 15, tzinfo=UTC),
            "type": "run.status",
            "payload": {"status": "FAILED", "error_code": "NO_CLAIMS"},
        }
    )
    assert isinstance(env.payload, RunStatusPayload)
    assert env.payload.error_code is ErrorCode.NO_CLAIMS


def test_event_envelope_rejects_payload_of_another_type() -> None:
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(
            {
                "id": 2,
                "run_id": "r_01jb7z00000000000000000000",
                "ts": datetime(2026, 10, 15, tzinfo=UTC),
                "type": "step.started",
                "payload": {"status": "FAILED"},
            }
        )


def test_every_event_type_has_a_payload_model() -> None:
    from app.domain.events import PAYLOAD_MODELS

    assert set(PAYLOAD_MODELS) == set(EventType)
    assert EventType.WARNING in PAYLOAD_MODELS
    assert PAYLOAD_MODELS[EventType.WARNING] is WarningPayload


def test_error_event_messages_are_bounded() -> None:
    from app.domain.events import ErrorPayload

    with pytest.raises(ValidationError):
        ErrorPayload(code=ErrorCode.RUNTIME_ERROR, message="x" * 2001)


def test_sandbox_output_is_capped_at_50_lines() -> None:
    from app.domain.events import SandboxOutputPayload

    with pytest.raises(ValidationError):
        SandboxOutputPayload(iteration=1, stream="stdout", lines=["l"] * 51)

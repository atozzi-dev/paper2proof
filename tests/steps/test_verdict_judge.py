"""What VERDICT sends the Judge: a code-written Facts block with the numbers, their sign and
their unit, so the prose cannot misread them (production eval, 2026-10-29: "the CI for the
reduction is [-1.176, -1.166], which encompasses the paper's claimed reduction of 1.164")."""

import json
from pathlib import Path

from app.domain.models import (
    Claim,
    ClaimType,
    Measurements,
    Plan,
    PlanMode,
    RunStatus,
    Scale,
    StepName,
)
from app.models.client import Role
from app.models.fake import FakeModelClient
from app.models.prompt_files import load_prompt
from app.orchestrator.steps.verdict import VerdictStep
from app.storage.artifacts import LocalArtifactStore
from app.storage.store import Store
from app.verdict.results import Analysis, ClaimAnalysis
from tests.steps.conftest import LLM_FIXTURES, ROOT, RUN, ctx_at

MANIFEST = ROOT / "tests" / "fixtures" / "contracts" / "manifest_valid.json"
ANALYSIS_KEY = f"runs/{RUN}/analysis_1.json"
PLAN_KEY = f"runs/{RUN}/plan.json"
SMOKE_KEY = f"runs/{RUN}/smoke_result.json"
RMSE = Claim(
    id="c1",
    text="Boosted trees reduce the RMSE of linear regression by 1.164",
    claim_type=ClaimType.RELATIVE,
    metric="rmse",
    higher_is_better=False,
    dataset="California housing",
    setting="default",
    reported_value=None,
    baseline_name="linear regression",
    claimed_delta=-1.164,
    source_ref="Table 1",
)
ACCURACY = Claim(
    id="c1",
    text="Ours reaches 94.1% top-1 on CIFAR-10",
    claim_type=ClaimType.ABSOLUTE,
    metric="top1_accuracy",
    higher_is_better=True,
    dataset="CIFAR-10",
    setting="200 epochs",
    reported_value=94.1,
    source_ref="Table 2",
)


async def _judge_prompt(
    store: Store,
    tmp_path: Path,
    claim: Claim,
    per_seed: list[float],
    baseline: list[float] | None,
    unit: str,
) -> str:
    """One full-scale claim through VERDICT, from a stored analysis; the Judge's prompt."""
    artifacts = LocalArtifactStore(tmp_path)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest["scale"] = {"full": True}
    measurements = Measurements(scale=Scale.FULL, per_seed=per_seed, baseline_per_seed=baseline)
    analysis = Analysis(
        exit_reason="completed",
        claims={claim.id: ClaimAnalysis(measurements=measurements, used_seeds=[0, 1, 2])},
        gpu="cpu",
    )
    artifacts.put_json(
        ANALYSIS_KEY, {"analysis": analysis.model_dump(mode="json"), "manifest": manifest}
    )
    plan = Plan(
        mode=PlanMode.FROM_SCRATCH, claim_ids=[claim.id],
        claim_types={claim.id: claim.claim_type}, scale=Scale.FULL, seeds=[0, 1, 2],
        gpu_minutes_est=10,
    )  # fmt: skip
    artifacts.put_json(PLAN_KEY, {"plan": plan.model_dump(mode="json"), "est_cost_usd": 0.1})
    measure = {"claim_id": claim.id, "key": "method/m", "baseline_key": None, "unit": unit}
    artifacts.put_json(
        SMOKE_KEY, {"entrypoint": "workspace/train.py", "args": [], "measures": [measure]}
    )
    previous: list[tuple[StepName, str | None]] = [
        (StepName.INGESTING, "i"), (StepName.EXTRACTING_CLAIMS, "c"),
        (StepName.DISCOVERING, "d"), (StepName.PLANNING, PLAN_KEY),
        (StepName.PROVISIONING, "p"), (StepName.SMOKE_TESTING, SMOKE_KEY),
        (StepName.EXECUTING, "e"), (StepName.ANALYZING, ANALYSIS_KEY),
    ]  # fmt: skip
    ctx = await ctx_at(store, StepName.VERDICT, previous=previous)
    await store.save_claims(RUN, [claim], selected={claim.id})
    models = FakeModelClient(LLM_FIXTURES, case="honest")
    result = await VerdictStep(models, artifacts).run(ctx)
    assert result.next is RunStatus.COMPLETED
    [(role, messages)] = models.requests
    assert role is Role.JUDGE
    return str(messages[0].content)


def _facts(prompt: str) -> str:
    start = prompt.index("Facts (")
    return prompt[start : prompt.index("Limitations (", start)]


async def test_a_lower_is_better_difference_reaches_the_judge_with_its_direction(
    store: Store, tmp_path: Path
) -> None:
    prompt = await _judge_prompt(
        store, tmp_path, RMSE, [3.100, 3.105, 3.110], [4.266, 4.276, 4.286], unit="raw"
    )
    facts = _facts(prompt)
    assert "<untrusted_document" not in facts  # written by code, from numbers only
    assert "the method minus the baseline" in facts
    assert "Lower is better for this metric, so a negative difference favours the method" in facts
    assert "mean difference -1.171, 95% CI -1.183 to -1.159" in facts
    assert "the method was lower than the baseline by 1.171, which is better" in facts
    assert "The paper claims a difference of -1.164: the method lower, so better, by 1.164" in (
        facts
    )
    # numbers only from the Facts: the raw verdict numbers are not sent
    assert "ci_low" not in prompt and "-1.1834" not in prompt
    assert '"kind": "REPRODUCED"' in prompt
    assert "only from the Facts" in prompt


async def test_an_absolute_claim_reaches_the_judge_with_its_unit_and_window(
    store: Store, tmp_path: Path
) -> None:
    """The script wrote fractions, the paper says percent: the Facts say what was converted."""
    prompt = await _judge_prompt(
        store, tmp_path, ACCURACY, [0.940, 0.941, 0.942], None, unit="fraction"
    )
    facts = _facts(prompt)
    assert "the method's value of the metric itself, in percent (0-100)" in facts
    assert "multiplied by 100 to match the paper's percent" in facts
    assert "Higher is better for this metric." in facts
    assert "mean 94.1, 95% CI 93.85 to 94.35" in facts
    assert "a mean of 92.22 or higher counts as matching the paper" in facts


def test_the_judge_prompt_version_moved_with_its_text() -> None:
    assert load_prompt("judge_explain").version == "2"
    assert "{{facts}}" in load_prompt("judge_explain").body

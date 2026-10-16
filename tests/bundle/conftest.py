"""A finished run on fakes: every artifact and bucket object a Proof Bundle reads."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from app.bundle.build import Bundler
from app.bundle.sign import FakeSigner, Signer
from app.bundle.store import ArtifactBundleStore
from app.domain.models import Claim, ClaimType, Plan, PlanMode, Scale, StepName
from app.domain.paper import IngestedPaper, Section
from app.execution.run_storage import FakeRunStorage
from app.execution.smoke import pack_with_digest
from app.orchestrator.context import RunContext
from app.storage.artifacts import LocalArtifactStore
from app.storage.store import Store
from tests.steps.conftest import ROOT, RUN, ctx_at

RUNNER = "registry.example/p2p/p2p-runner@sha256:" + "1" * 64
PAPER_TEXT = "We propose a secret sauce that beats every baseline by a wide margin."
KNOWN_SECRET = "s3cr3t-admin-token-value-0123456789"
WORKSPACE = {"train.py": b"import json\nprint('train')\n", "lib/util.py": b"X = 1\n"}
LOCK = b"numpy==2.1.0 --hash=sha256:abc\n"
CLAIM = Claim(
    id="c1",
    text="Ours beats ResNet-18 by 1.7 points on CIFAR-10",
    claim_type=ClaimType.RELATIVE,
    metric="top1_accuracy",
    higher_is_better=True,
    dataset="CIFAR-10",
    setting="200 epochs",
    reported_value=None,
    baseline_name="ResNet-18",
    claimed_delta=1.7,
    source_ref="Table 2",
)
OTHER = CLAIM.model_copy(update={"id": "c2", "text": "Second claim", "source_ref": "Table 3"})
CLOCK = datetime(2026, 10, 25, 10, 0, tzinfo=UTC)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class Run:
    ctx: RunContext
    artifacts: LocalArtifactStore
    storage: FakeRunStorage
    bundles: ArtifactBundleStore
    workspace_tar: bytes

    def bundler(self, signer: Signer | None = None, **kwargs: Any) -> Bundler:
        return Bundler(
            artifacts=self.artifacts,
            storage=self.storage,
            bundles=self.bundles,
            signer=signer,
            runner_image=RUNNER,
            known_secrets=(KNOWN_SECRET,),
            commit="0123abc",
            clock=lambda: CLOCK,
            **kwargs,
        )

    def put(self, key: str, data: bytes) -> None:
        self.storage.objects[(RUN, key)] = data


def exec_manifest(workspace_sha256: str) -> dict[str, Any]:
    path = ROOT / "tests" / "fixtures" / "contracts" / "manifest_valid.json"
    data: dict[str, Any] = json.loads(path.read_text())
    data["workspace_sha256"] = workspace_sha256
    return data


async def make_run(store: Store, tmp_path: Path) -> Run:
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    prefix = f"runs/{RUN}"
    paper = IngestedPaper(
        arxiv_id="2106.09685",
        sha256="d" * 64,
        title="A Paper",
        sections=[Section(title="Method", text=PAPER_TEXT)],
        tables=[],
        source_kind="pdf",
    )
    artifacts.put_json(f"{prefix}/ingested.json", paper.model_dump(mode="json"))
    plan = Plan(
        mode=PlanMode.FROM_SCRATCH, claim_ids=["c1"], claim_types={"c1": ClaimType.RELATIVE},
        scale=Scale.REDUCED, seeds=[0, 1, 2], gpu_minutes_est=10, notes=["Smaller model."],
    )  # fmt: skip
    artifacts.put_json(f"{prefix}/plan.json", {"plan": plan.model_dump(mode="json")})
    archive, digest = pack_with_digest(WORKSPACE)
    manifest = exec_manifest(digest)
    artifacts.put_json(
        f"{prefix}/provision_report.json",
        {
            "schema": "p2p.provision-report/v1",
            "run_id": RUN,
            "overlay_sha256": "a" * 64,
            "lock_sha256": sha(LOCK),
            "data_archives": [{"name": "uoft-cs__cifar10", "sha256": "b" * 64}],
        },
    )
    artifacts.put_json(
        f"{prefix}/smoke_result.json",
        {
            "entrypoint": "workspace/train.py",
            "args": ["--epochs", "5"],
            "smoke_metrics": {"top1_accuracy": 0.5},
            "files": sorted(WORKSPACE),
            "workspace_sha256": digest,
            "smoke_runs": 2,
        },
    )
    artifacts.put_json(f"{prefix}/exec_manifest_1.json", manifest)
    artifacts.put_json(
        f"{prefix}/execution_1.json",
        {"manifest": manifest, "results_key": "results/1/results.json", "exit_reason": None,
         "job_state": "SUCCEEDED"},
    )  # fmt: skip
    measurements = {"scale": "REDUCED", "per_seed": [0.95, 0.96], "baseline_per_seed": [0.9, 0.9]}
    artifacts.put_json(
        f"{prefix}/analysis_1.json",
        {
            "analysis": {"exit_reason": "completed", "measurements": measurements,
                         "used_seeds": [0, 1], "wall_clock_s": 12.0, "gpu": "L40S"},
            "manifest": manifest,
            "log_summary": "Two seeds finished.",
        },
    )  # fmt: skip
    artifacts.put_json(
        f"{prefix}/verdict.json",
        {
            "claim": CLAIM.model_dump(mode="json"),
            "attempt": 1,
            "verdict": {"kind": "REPRODUCED_AT_REDUCED_SCALE", "reason": None, "n_seeds": 2,
                        "mean": 0.055, "std": 0.007, "ci_low": 0.0, "ci_high": 0.1,
                        "tolerance": {"tol_rel": 0.02}},
            "measurements": measurements,
            "config": {"tol_rel": 0.02},
            "config_sha256": "c" * 64,
            "explanation_md": "It held up.\n\n### Limitations\n\n- Reduced scale.",
            "limitations": "- Reduced scale.",
        },
    )  # fmt: skip
    storage = FakeRunStorage()
    run = Run(
        ctx=await ctx_at(
            store, StepName.VERDICT, previous=[(StepName.INGESTING, "i")], arxiv_id="2106.09685"
        ),
        artifacts=artifacts,
        storage=storage,
        bundles=ArtifactBundleStore(artifacts),
        workspace_tar=archive,
    )
    run.put("staging/workspace.tar", archive)
    run.put("staging/lock.txt", LOCK)
    run.put("results/1/results.json", b'{"exit_reason": "completed", "per_seed": []}')
    run.put("results/1/stdout.log", b"epoch 1\nepoch 2\n")
    run.put("results/1/stderr.log", b"")
    run.put("results/1/env.json", b'{"gpu": "L40S", "pip_freeze": ["numpy==2.1.0"]}')
    await store.save_claims(RUN, [CLAIM, OTHER], selected={"c1"})
    await store.set_paper_sha256(RUN, "d" * 64)
    await store.record_model_call(
        RUN, "PLANNING", "planner", "nvidia/nemotron-ultra", 1000, 200, 900, 0.01,
        prompt_version="1",
    )  # fmt: skip
    await store.record_model_call(
        RUN, "VERDICT", "judge", "nvidia/nemotron-super", 500, 100, 400, 0.002,
        prompt_version="1",
    )  # fmt: skip
    await store.record_job(RUN, "exec", "p2p-exec-01jb7z00000000000000000000-1", "j1", "b")
    await store.update_job(
        "p2p-exec-01jb7z00000000000000000000-1", "SUCCEEDED", gpu_seconds=600, cost_usd=0.3
    )
    await store.add_ledger_entry(RUN, date(2026, 10, 25), "inference_usd", 0.012)
    await store.add_ledger_entry(RUN, date(2026, 10, 25), "cloud_usd", 0.3)
    return run


@pytest.fixture
async def run(store: Store, tmp_path: Path) -> Run:
    return await make_run(store, tmp_path)


@pytest.fixture
def signer() -> FakeSigner:
    return FakeSigner(key_id="test-key")

"""Runner entrypoint, pure Python: temp dirs and a fake experiment script (no docker, no GPU)."""

import importlib.util
import json
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
RESULTS_SCHEMA = json.loads((ROOT / "schemas" / "results.schema.json").read_text(encoding="utf-8"))


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "p2p_entrypoint", ROOT / "images/runner/entrypoint.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["p2p_entrypoint"] = module
    spec.loader.exec_module(module)
    return module


entrypoint = _load()

SCRIPT = """
import argparse, json, os, sys, time
p = argparse.ArgumentParser()
p.add_argument("--seed", type=int); p.add_argument("--out")
p.add_argument("--smoke", action="store_true")
p.add_argument("--mode", default="ok")
a = p.parse_args()
print(f"training seed {a.seed}", flush=True)
if a.mode == "fail" and a.seed == 1:
    raise SystemExit("boom on seed 1")
if a.mode == "slow":
    time.sleep(30)
if a.mode == "chatty":
    for i in range(8):
        print(f"step {i}", flush=True); time.sleep(0.1)
os.makedirs(a.out, exist_ok=True)
metrics = {"top1_accuracy": 0.90 + a.seed / 100, "baseline/top1_accuracy": 0.88 + a.seed / 100}
if a.mode == "nan":
    metrics["top1_accuracy"] = float("nan")
json.dump(metrics, open(os.path.join(a.out, "metrics.json"), "w"))
"""


def _setup(
    tmp_path: Path, *, mode: str = "ok", budget: int = 600, **overrides: Any
) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "train.py").write_text(SCRIPT)
    results = tmp_path / "results"
    manifest = {
        "schema": "p2p.manifest/v1",
        "run_id": "r_01jb7z00000000000000000000",
        "attempt": 1,
        "image_digest": "sha256:" + "0" * 64,
        "entrypoint": "workspace/train.py",
        "args": ["--mode", mode],
        "seeds": [0, 1, 2],
        "budget_seconds": budget,
        "paths": {
            "overlay": str(tmp_path / "overlay.tar.zst"),
            "repo": None,
            "data_dir": str(tmp_path / "data"),
            "workspace": str(workspace),
            "cache": str(tmp_path / "cache"),
            "results": str(results),
        },
        "claim_ref": "c1",
        "metric": "top1_accuracy",
        "higher_is_better": True,
        "claim_type": "RELATIVE",
        "baseline_name": "resnet18",
        "scale": {"full": False, "fraction_of_data": 0.1, "epochs": 2, "paper_epochs": 200},
    }
    manifest.update(overrides)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    return path, results


def _run(manifest: Path, tmp_path: Path, **kwargs: Any) -> int:
    code: int = entrypoint.run(
        manifest,
        work=tmp_path / "work",
        extract=lambda archive, dest: dest.mkdir(parents=True, exist_ok=True),
        gpu_name=lambda: "NVIDIA L40S",
        **kwargs,
    )
    return code


def _results(results: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((results / "results.json").read_text())
    errors = list(Draft202012Validator(RESULTS_SCHEMA).iter_errors(data))
    assert not errors, errors
    return data


def test_invalid_manifest_is_a_contract_error(tmp_path: Path) -> None:
    manifest, _ = _setup(tmp_path, entrypoint="../etc/passwd.py")
    assert _run(manifest, tmp_path) == 30
    manifest.write_text("{not json")
    assert _run(manifest, tmp_path) == 30


def test_three_seeds_produce_valid_results(tmp_path: Path) -> None:
    manifest, results = _setup(tmp_path)
    assert _run(manifest, tmp_path) == 0
    data = _results(results)
    assert [s["seed"] for s in data["per_seed"]] == [0, 1, 2]
    assert [s["value"] for s in data["per_seed"]] == pytest.approx([0.90, 0.91, 0.92])
    assert all(s["ok"] for s in data["per_seed"])
    assert data["exit_reason"] == "completed"
    assert data["gpu"] == "NVIDIA L40S"
    assert (results / "seeds" / "1" / "metrics.json").is_file()
    assert (results / "stdout.log").read_text().count("training seed") == 3


def test_relative_claims_pick_up_the_baseline(tmp_path: Path) -> None:
    manifest, results = _setup(tmp_path)
    _run(manifest, tmp_path)
    baseline = _results(results)["baseline"]
    assert baseline["name"] == "resnet18"
    assert [b["value"] for b in baseline["per_seed"]] == pytest.approx([0.88, 0.89, 0.90])
    assert [b["seed"] for b in baseline["per_seed"]] == [0, 1, 2]


def test_a_failing_seed_is_an_experiment_error(tmp_path: Path) -> None:
    manifest, results = _setup(tmp_path, mode="fail")
    assert _run(manifest, tmp_path) == 10
    data = _results(results)
    seed1 = next(s for s in data["per_seed"] if s["seed"] == 1)
    assert seed1 == {"seed": 1, "value": None, "ok": False}
    assert data["exit_reason"] == "experiment_error"


def test_non_finite_metrics_count_as_failed_seeds(tmp_path: Path) -> None:
    manifest, results = _setup(tmp_path, mode="nan")
    assert _run(manifest, tmp_path) == 10
    assert all(s["ok"] is False for s in _results(results)["per_seed"])


def test_budget_timeout(tmp_path: Path) -> None:
    manifest, results = _setup(tmp_path, mode="slow", budget=60)
    started = time.monotonic()
    code = _run(manifest, tmp_path, budget_override_s=1.0, kill_grace_s=0.5)
    assert code == 20
    assert time.monotonic() - started < 15
    assert _results(results)["exit_reason"] == "budget_timeout"


def test_live_log_is_flushed_while_running(tmp_path: Path) -> None:
    manifest, results = _setup(tmp_path, mode="chatty")
    seen: list[str] = []

    def on_flush(text: str) -> None:
        seen.append(text)

    assert _run(manifest, tmp_path, flush_interval_s=0.05, on_flush=on_flush) == 0
    assert any("step" in s for s in seen[:-1])  # flushed before the end, not only at exit
    live = (results / "live.log").read_text()
    assert len(live.splitlines()) <= 200


from tests.contracts.test_schemas import MANIFEST_MUTATIONS, _fixture  # noqa: E402


@pytest.mark.parametrize(
    ("label", "mutate"), MANIFEST_MUTATIONS, ids=[m[0] for m in MANIFEST_MUTATIONS]
)
def test_stdlib_validator_agrees_with_the_schema(label: str, mutate: Any) -> None:
    import copy

    entrypoint.validate(_fixture("manifest_valid.json"))  # the valid fixture passes
    data = copy.deepcopy(_fixture("manifest_valid.json"))
    mutate(data)
    with pytest.raises(entrypoint.ContractError):
        entrypoint.validate(data)

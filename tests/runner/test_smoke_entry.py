"""Image-side smoke runner (CPU smoke jobs, the Sandboxes fallback). Pure Python, temp dirs."""

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUN = "r_01jb7z00000000000000000000"


def _load() -> ModuleType:
    sys.path.insert(0, str(ROOT / "images/runner"))  # smoke.py reuses entrypoint helpers
    spec = importlib.util.spec_from_file_location("p2p_smoke", ROOT / "images/runner/smoke.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["p2p_smoke"] = module
    spec.loader.exec_module(module)
    return module


smoke = _load()

SCRIPT = """
import argparse, json, os, sys, time
p = argparse.ArgumentParser()
p.add_argument("--out"); p.add_argument("--smoke", action="store_true")
p.add_argument("--mode", default="ok")
a = p.parse_args()
print("data dir", os.environ["P2P_DATA_DIR"], flush=True)
if a.mode == "crash":
    raise SystemExit("boom")
if a.mode == "slow":
    time.sleep(30)
os.makedirs(a.out, exist_ok=True)
json.dump({"acc": 0.5, "junk": "x"}, open(os.path.join(a.out, "metrics.json"), "w"))
"""


def _setup(tmp_path: Path, mode: str = "ok", **overrides: Any) -> tuple[Path, Path]:
    mnt = tmp_path / "mnt"
    workspace = mnt / "smoke" / "3" / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "train.py").write_text(SCRIPT)
    manifest = {
        "schema": "p2p.smoke/v1",
        "run_id": RUN,
        "iteration": 3,
        "argv": ["python", "train.py", "--out", "out", "--smoke", "--mode", mode],
        "timeout_s": 60,
    }
    manifest.update(overrides)
    path = tmp_path / "smoke.json"
    path.write_text(json.dumps(manifest))
    return path, mnt


def _run(path: Path, mnt: Path, tmp_path: Path, **kwargs: Any) -> dict[str, Any]:
    code = smoke.run(
        path,
        mnt=mnt,
        work=tmp_path / "work",
        extract=lambda archive, dest: dest.mkdir(parents=True, exist_ok=True),
        **kwargs,
    )
    assert code == 0  # the job succeeds whenever it could write a result
    result: dict[str, Any] = json.loads((mnt / "smoke" / "3" / "result.json").read_text())
    return result


def test_a_passing_script_reports_its_metrics(tmp_path: Path) -> None:
    path, mnt = _setup(tmp_path)
    result = _run(path, mnt, tmp_path)
    assert result["exit_code"] == 0
    assert result["metrics"] == {"acc": 0.5, "junk": "x"}  # filtered by the control plane
    assert "data dir" in result["stdout"]


def test_a_crash_is_reported_not_raised(tmp_path: Path) -> None:
    path, mnt = _setup(tmp_path, mode="crash")
    result = _run(path, mnt, tmp_path)
    assert result["exit_code"] != 0
    assert "boom" in result["stderr"]
    assert result["metrics"] is None


def test_timeouts_are_enforced(tmp_path: Path) -> None:
    path, mnt = _setup(tmp_path, mode="slow")
    result = _run(path, mnt, tmp_path, timeout_override_s=1.0)
    assert result["exit_code"] == -1
    assert "timed out" in result["stderr"]


@pytest.mark.parametrize(
    "override",
    [
        {"argv": ["bash", "-c", "id"]},
        {"argv": ["python", "../escape.py"]},
        {"argv": ["python", "-c", "print(1)"]},
        {"timeout_s": 10_000},
        {"iteration": "3/../../x"},
        {"run_id": "../../etc"},
        {"schema": "other"},
    ],
)
def test_invalid_manifests_are_contract_errors(tmp_path: Path, override: dict[str, Any]) -> None:
    path, mnt = _setup(tmp_path, **override)
    code = smoke.run(path, mnt=mnt, work=tmp_path / "work", extract=lambda a, d: None)
    assert code == 30
    assert not (mnt / "smoke" / "3" / "result.json").exists()

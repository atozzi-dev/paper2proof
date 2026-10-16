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
if a.mode == "longlines":
    print("y" * 50_000, flush=True)
    print("z" * 5_000, file=sys.stderr, flush=True)
    sys.stdout.buffer.write(b"\\xff\\xfe not utf-8\\n"); sys.stdout.flush()
os.makedirs(a.out, exist_ok=True)
metrics = {"top1_accuracy": 0.90 + a.seed / 100, "baseline/top1_accuracy": 0.88 + a.seed / 100}
if a.mode == "nan":
    metrics["top1_accuracy"] = float("nan")
if a.mode == "huge":
    metrics = {"top1_accuracy": 10 ** 400, "baseline/top1_accuracy": 0.8}
if a.mode == "typed":
    metrics = {"top1_accuracy": [True, "0.93", 0.9][a.seed], "baseline/top1_accuracy": 0.8}
json.dump(metrics, open(os.path.join(a.out, "metrics.json"), "w"))
"""


def _setup(
    tmp_path: Path, *, mode: str = "ok", budget: int = 600, **overrides: Any
) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    (tmp_path / "overlay.tar.zst").write_bytes(b"")  # extracted by the fake `extract`
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
        "archives": {"overlay": "a" * 64, "repo": None, "data": []},
    }
    manifest.update(overrides)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    return path, results


EXTRACTED: list[tuple[str, str]] = []


def _extract(archive: Path, dest: Path, sha256: str) -> None:
    EXTRACTED.append((archive.name, sha256))
    dest.mkdir(parents=True, exist_ok=True)


def _run(manifest: Path, tmp_path: Path, **kwargs: Any) -> int:
    kwargs.setdefault("extract", _extract)
    code: int = entrypoint.run(
        manifest,
        work=tmp_path / "work",
        **{"gpu_name": lambda: "NVIDIA L40S", **kwargs},
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


def test_type_errors_in_the_manifest_are_contract_errors(tmp_path: Path) -> None:
    manifest, _ = _setup(tmp_path, seeds=[[1], [2]])
    assert _run(manifest, tmp_path) == 30
    manifest, _ = _setup(tmp_path / "b", scale={"full": False, "extra": 1})
    assert _run(manifest, tmp_path / "b") == 30


def test_a_missing_overlay_or_a_broken_archive_is_a_contract_error(tmp_path: Path) -> None:
    manifest, results = _setup(tmp_path)
    (tmp_path / "overlay.tar.zst").unlink()
    assert _run(manifest, tmp_path) == 30
    assert _results(results)["exit_reason"] == "contract_error"

    def broken(archive: Path, dest: Path, sha256: str) -> None:
        raise entrypoint.ContractError("archive member escapes")

    manifest, results = _setup(tmp_path / "b")
    assert _run(manifest, tmp_path / "b", extract=broken) == 30
    data = _results(results)
    assert data["exit_reason"] == "contract_error"
    assert all(s["ok"] is False for s in data["per_seed"])


def test_metric_values_must_be_real_numbers(tmp_path: Path) -> None:
    manifest, results = _setup(tmp_path, mode="typed")
    assert _run(manifest, tmp_path) == 10
    data = _results(results)
    assert [s["ok"] for s in data["per_seed"]] == [False, False, True]
    assert [b["value"] for b in data["baseline"]["per_seed"]] == [None, None, 0.8]


def test_output_is_bounded_and_decoded_leniently(tmp_path: Path) -> None:
    manifest, results = _setup(tmp_path, mode="longlines")
    assert _run(manifest, tmp_path, log_cap_chars=60_000) == 0
    assert all(len(line) <= 2000 for line in (results / "live.log").read_text().splitlines())
    stdout = (results / "stdout.log").read_text(encoding="utf-8")
    assert len(stdout) <= 60_000 + 200
    assert "[log truncated]" in stdout


def test_results_are_written_after_every_seed(tmp_path: Path) -> None:
    manifest, results = _setup(tmp_path)
    seen: list[int] = []

    def on_seed(done: int) -> None:
        seen.append(sum(s["ok"] for s in _results(results)["per_seed"]))

    assert _run(manifest, tmp_path, on_seed=on_seed) == 0
    assert seen == [1, 2, 3]


def test_termination_writes_partial_results(tmp_path: Path) -> None:
    manifest, results = _setup(tmp_path)

    def on_seed(done: int) -> None:
        if done == 1:
            raise entrypoint.Terminated

    assert _run(manifest, tmp_path, on_seed=on_seed) == 20
    data = _results(results)
    assert data["exit_reason"] == "budget_timeout"
    assert [s["ok"] for s in data["per_seed"]] == [True, False, False]


@pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX")
def test_a_seed_timeout_kills_its_whole_process_tree(tmp_path: Path) -> None:
    marker = tmp_path / "child-alive"
    spawner = f"""
import subprocess, sys, time
child = "import time; time.sleep(3); open({str(marker)!r}, 'w').write('x')"
subprocess.Popen([sys.executable, "-c", child])
time.sleep(30)
"""
    workspace = tmp_path / "workspace"
    manifest, _ = _setup(tmp_path, mode="slow")
    (workspace / "train.py").write_text(spawner)
    assert _run(manifest, tmp_path, budget_override_s=1.0, kill_grace_s=0.5) == 20
    time.sleep(4)
    assert not marker.exists()


def _tar(members: list[tuple[str, bytes | None, str | None]]) -> Any:
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, data, link in members:
            info = tarfile.TarInfo(name)
            if link is not None:
                info.type, info.linkname = tarfile.SYMTYPE, link
                tar.addfile(info)
            else:
                assert data is not None
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    buffer.seek(0)
    return buffer


def test_safe_extraction_skips_links_and_rejects_escapes(tmp_path: Path) -> None:
    dest = tmp_path / "out"
    entrypoint.extract_stream(
        _tar([("a/ok.txt", b"hi", None), ("a/link", None, "/etc/passwd")]),
        dest,
        max_bytes=1000,
        max_members=10,
    )
    assert (dest / "a" / "ok.txt").read_bytes() == b"hi"
    assert not (dest / "a" / "link").exists()
    for bad in (
        [("../evil.txt", b"x", None)],
        [("/abs.txt", b"x", None)],
        [(f"f{i}", b"x", None) for i in range(11)],
        [("big", b"x" * 1001, None)],
    ):
        with pytest.raises(entrypoint.ContractError):
            entrypoint.extract_stream(_tar(bad), tmp_path / "bad", max_bytes=1000, max_members=10)


def test_a_huge_integer_metric_fails_the_seed_not_the_runner(tmp_path: Path) -> None:
    manifest, results = _setup(tmp_path, mode="huge")
    assert _run(manifest, tmp_path) == 10
    assert all(s["ok"] is False for s in _results(results)["per_seed"])


def test_termination_during_extraction_writes_results(tmp_path: Path) -> None:
    def interrupted(archive: Path, dest: Path, sha256: str) -> None:
        raise entrypoint.Terminated

    manifest, results = _setup(tmp_path)
    assert _run(manifest, tmp_path, extract=interrupted) == 20
    assert _results(results)["exit_reason"] == "budget_timeout"


def test_only_listed_archives_are_extracted_with_their_digests(tmp_path: Path) -> None:
    archives = {
        "overlay": "a" * 64,
        "repo": None,
        "data": [{"name": "cifar", "sha256": "c" * 64}],
    }
    manifest, _ = _setup(tmp_path, archives=archives)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "cifar.tar.zst").write_bytes(b"")
    (tmp_path / "data" / "planted.tar.zst").write_bytes(b"")
    EXTRACTED.clear()
    assert _run(manifest, tmp_path) == 0
    assert EXTRACTED == [("overlay.tar.zst", "a" * 64), ("cifar.tar.zst", "c" * 64)]


def test_a_listed_archive_that_is_missing_is_a_contract_error(tmp_path: Path) -> None:
    archives = {"overlay": "a" * 64, "repo": None, "data": [{"name": "cifar", "sha256": "c" * 64}]}
    manifest, results = _setup(tmp_path, archives=archives)
    assert _run(manifest, tmp_path) == 30
    assert _results(results)["exit_reason"] == "contract_error"


def _tar_bytes() -> bytes:
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo("hello.txt")
        info.size = 2
        tar.addfile(info, io.BytesIO(b"hi"))
    return buffer.getvalue()


PASSTHROUGH = [
    sys.executable,
    "-c",
    "import sys,shutil;shutil.copyfileobj(sys.stdin.buffer,sys.stdout.buffer)",
]


def test_extraction_verifies_the_digest_of_what_it_read(tmp_path: Path) -> None:
    import hashlib

    archive = tmp_path / "a.tar.zst"
    archive.write_bytes(_tar_bytes())
    good = hashlib.sha256(archive.read_bytes()).hexdigest()
    entrypoint.tar_extract(archive, tmp_path / "ok", good, decompress=PASSTHROUGH)
    assert (tmp_path / "ok" / "hello.txt").read_bytes() == b"hi"
    with pytest.raises(entrypoint.ContractError, match="digest"):
        entrypoint.tar_extract(archive, tmp_path / "bad", "0" * 64, decompress=PASSTHROUGH)
    assert not (tmp_path / "bad").exists()


@pytest.mark.parametrize("args", [["--seed", "5"], ["--sm"], ["--out=/tmp/x"]])
def test_runner_owned_flags_in_args_are_contract_errors(tmp_path: Path, args: list[str]) -> None:
    manifest, _ = _setup(tmp_path, args=args)
    assert _run(manifest, tmp_path) == 30


def test_termination_while_probing_the_gpu_writes_results(tmp_path: Path) -> None:
    def interrupted() -> str:
        raise entrypoint.Terminated

    manifest, results = _setup(tmp_path)
    assert _run(manifest, tmp_path, gpu_name=interrupted) == 20
    assert _results(results)["exit_reason"] == "budget_timeout"


@pytest.mark.parametrize(
    "args", [["-s", "1"], ["-o", "x"], ["-vs0"], ["-xo/tmp/x"], ["@flags.txt"]]
)
def test_short_and_file_forms_of_runner_flags_are_rejected(tmp_path: Path, args: list[str]) -> None:
    manifest, _ = _setup(tmp_path, args=args)
    assert _run(manifest, tmp_path) == 30


def test_negative_numbers_are_ordinary_arguments(tmp_path: Path) -> None:
    manifest, _ = _setup(tmp_path, args=["--mode", "ok", "--lr-exp", "-3"])
    assert _run(manifest, tmp_path) != 30

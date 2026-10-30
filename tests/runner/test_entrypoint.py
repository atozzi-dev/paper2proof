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
if a.mode == "devzero":  # an endless "file": the runner must not read it
    os.symlink("/dev/zero", os.path.join(a.out, "metrics.json")); raise SystemExit(0)
if a.mode == "fifo":  # a read that would block until the watchdog
    os.mkfifo(os.path.join(a.out, "metrics.json")); raise SystemExit(0)
metrics = {"top1_accuracy": 0.90 + a.seed / 100, "baseline/top1_accuracy": 0.88 + a.seed / 100}
if a.mode == "nan":
    metrics["top1_accuracy"] = float("nan")
if a.mode == "huge":
    metrics = {"top1_accuracy": 10 ** 400, "baseline/top1_accuracy": 0.8}
if a.mode == "silent":
    raise SystemExit(0)  # exits 0 without writing metrics.json
if a.mode == "typed":
    metrics = {"top1_accuracy": [True, "0.93", 0.9][a.seed], "baseline/top1_accuracy": 0.8}
json.dump(metrics, open(os.path.join(a.out, "metrics.json"), "w"))
"""


def _workspace_tar(files: dict[str, str]) -> bytes:
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, text in sorted(files.items()):
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _setup(
    tmp_path: Path,
    *,
    mode: str = "ok",
    budget: int = 600,
    files: dict[str, str] | None = None,
    **overrides: Any,
) -> tuple[Path, Path]:
    import hashlib

    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "overlay.tar.zst").write_bytes(b"")  # extracted by the fake `extract`
    workspace = _workspace_tar(files or {"train.py": SCRIPT})
    (tmp_path / "workspace.tar").write_bytes(workspace)
    results = tmp_path / "results"
    manifest = {
        "schema": "p2p.manifest/v2",
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
            "workspace": str(tmp_path / "workspace.tar"),
            "cache": str(tmp_path / "cache"),
            "results": str(results),
        },
        "claims": [RELATIVE_C1],
        "scale": {"full": False, "fraction_of_data": 0.1, "epochs": 2, "paper_epochs": 200},
        "archives": {"overlay": "a" * 64, "repo": None, "data": []},
        "workspace_sha256": hashlib.sha256(workspace).hexdigest(),
    }
    manifest.update(overrides)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    return path, results


RELATIVE_C1: dict[str, Any] = {
    "claim_ref": "c1",
    "claim_type": "RELATIVE",
    "metric": "top1_accuracy",
    "higher_is_better": True,
    "metric_key": "top1_accuracy",
    "baseline_name": "resnet18",
    "baseline_key": "baseline/top1_accuracy",
}
ABSOLUTE_C2: dict[str, Any] = {
    "claim_ref": "c2",
    "claim_type": "ABSOLUTE",
    "metric": "top1_accuracy",
    "higher_is_better": True,
    "metric_key": "top1_accuracy",
    "baseline_name": None,
    "baseline_key": None,
}

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
    assert [s["seed"] for s in data["claims"][0]["per_seed"]] == [0, 1, 2]
    assert [s["value"] for s in data["claims"][0]["per_seed"]] == pytest.approx([0.90, 0.91, 0.92])
    assert all(s["ok"] for s in data["claims"][0]["per_seed"])
    assert data["exit_reason"] == "completed"
    assert data["gpu"] == "NVIDIA L40S"
    assert (results / "seeds" / "1" / "metrics.json").is_file()
    assert (results / "stdout.log").read_text().count("training seed") == 3


def test_relative_claims_pick_up_the_baseline(tmp_path: Path) -> None:
    manifest, results = _setup(tmp_path)
    _run(manifest, tmp_path)
    baseline = _results(results)["claims"][0]["baseline"]
    assert baseline["name"] == "resnet18"
    assert [b["value"] for b in baseline["per_seed"]] == pytest.approx([0.88, 0.89, 0.90])
    assert [b["seed"] for b in baseline["per_seed"]] == [0, 1, 2]


def test_a_failing_seed_is_an_experiment_error(tmp_path: Path) -> None:
    manifest, results = _setup(tmp_path, mode="fail")
    assert _run(manifest, tmp_path) == 10
    data = _results(results)
    seed1 = next(s for s in data["claims"][0]["per_seed"] if s["seed"] == 1)
    assert seed1 == {"seed": 1, "value": None, "ok": False}
    assert data["exit_reason"] == "experiment_error"


def test_non_finite_metrics_count_as_failed_seeds(tmp_path: Path) -> None:
    manifest, results = _setup(tmp_path, mode="nan")
    assert _run(manifest, tmp_path) == 10
    assert all(s["ok"] is False for s in _results(results)["claims"][0]["per_seed"])


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
    assert all(s["ok"] is False for s in data["claims"][0]["per_seed"])


def test_metric_values_must_be_real_numbers(tmp_path: Path) -> None:
    manifest, results = _setup(tmp_path, mode="typed")
    assert _run(manifest, tmp_path) == 10
    data = _results(results)
    assert [s["ok"] for s in data["claims"][0]["per_seed"]] == [False, False, True]
    assert [b["value"] for b in data["claims"][0]["baseline"]["per_seed"]] == [None, None, 0.8]


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
        seen.append(sum(s["ok"] for s in _results(results)["claims"][0]["per_seed"]))

    assert _run(manifest, tmp_path, on_seed=on_seed) == 0
    assert seen == [1, 2, 3]


def test_results_say_budget_timeout_until_the_last_seed(tmp_path: Path) -> None:
    # a job stopped from outside (preemption, SIGKILL) must never look completed
    manifest, results = _setup(tmp_path)
    reasons: list[str] = []

    def on_seed(done: int) -> None:
        reasons.append(_results(results)["exit_reason"])

    assert _run(manifest, tmp_path, on_seed=on_seed) == 0
    assert reasons == ["budget_timeout", "budget_timeout", "budget_timeout"]
    assert _results(results)["exit_reason"] == "completed"


def test_the_workspace_runs_from_its_verified_archive(tmp_path: Path) -> None:
    helper = "VALUE = 0.5\n"
    script = SCRIPT.replace("import argparse,", "import helper, argparse,")
    manifest, _ = _setup(tmp_path, files={"train.py": script, "helper.py": helper})
    assert _run(manifest, tmp_path) == 0
    assert (tmp_path / "work" / "workspace" / "helper.py").read_text() == helper


def test_a_workspace_archive_that_does_not_match_is_a_contract_error(tmp_path: Path) -> None:
    manifest, results = _setup(tmp_path)
    (tmp_path / "workspace.tar").write_bytes(
        _workspace_tar({"train.py": SCRIPT, "sitecustomize.py": "raise SystemExit(7)"})
    )
    assert _run(manifest, tmp_path) == 30
    assert _results(results)["exit_reason"] == "contract_error"


def test_metrics_planted_before_a_seed_are_ignored(tmp_path: Path) -> None:
    manifest, results = _setup(tmp_path, mode="silent")
    planted = results / "seeds" / "0"
    planted.mkdir(parents=True)
    (planted / "metrics.json").write_text('{"top1_accuracy": 0.99}')
    assert _run(manifest, tmp_path) == 10
    assert all(s["ok"] is False for s in _results(results)["claims"][0]["per_seed"])


def test_termination_writes_partial_results(tmp_path: Path) -> None:
    manifest, results = _setup(tmp_path)

    def on_seed(done: int) -> None:
        if done == 1:
            raise entrypoint.Terminated

    assert _run(manifest, tmp_path, on_seed=on_seed) == 20
    data = _results(results)
    assert data["exit_reason"] == "budget_timeout"
    assert [s["ok"] for s in data["claims"][0]["per_seed"]] == [True, False, False]


@pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX")
def test_a_seed_timeout_kills_its_whole_process_tree(tmp_path: Path) -> None:
    marker = tmp_path / "child-alive"
    spawner = f"""
import subprocess, sys, time
child = "import time; time.sleep(3); open({str(marker)!r}, 'w').write('x')"
subprocess.Popen([sys.executable, "-c", child])
time.sleep(30)
"""
    manifest, _ = _setup(tmp_path, mode="slow", files={"train.py": spawner})
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
    assert all(s["ok"] is False for s in _results(results)["claims"][0]["per_seed"])


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


def test_a_huge_tail_after_the_tar_is_rejected_without_reading_it_all(tmp_path: Path) -> None:
    import hashlib

    archive = tmp_path / "a.tar.zst"
    archive.write_bytes(_tar_bytes() + b"\0" * (3 << 20))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    with pytest.raises(entrypoint.ContractError, match="after the end"):
        entrypoint.tar_extract(archive, tmp_path / "tail", digest, decompress=PASSTHROUGH)
    assert not (tmp_path / "tail").exists()


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


def test_intermediate_results_never_report_an_ending(tmp_path: Path) -> None:
    manifest, results = _setup(tmp_path, mode="fail")
    reasons: list[str] = []

    def on_seed(done: int) -> None:
        reasons.append(_results(results)["exit_reason"])

    assert _run(manifest, tmp_path, on_seed=on_seed) == 10
    assert reasons == ["budget_timeout"] * 3
    assert _results(results)["exit_reason"] == "experiment_error"


def test_an_unreadable_workspace_archive_is_a_contract_error(tmp_path: Path) -> None:
    import hashlib

    manifest, results = _setup(tmp_path)
    junk = b"not a tar" * 100
    (tmp_path / "workspace.tar").write_bytes(junk)
    data = json.loads(manifest.read_text())
    data["workspace_sha256"] = hashlib.sha256(junk).hexdigest()
    manifest.write_text(json.dumps(data))
    assert _run(manifest, tmp_path) == 30
    assert _results(results)["exit_reason"] == "contract_error"


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_a_planted_seed_symlink_is_removed(tmp_path: Path) -> None:
    manifest, results = _setup(tmp_path, mode="silent")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "metrics.json").write_text('{"top1_accuracy": 0.99}')
    (results / "seeds").mkdir(parents=True)
    (results / "seeds" / "0").symlink_to(elsewhere, target_is_directory=True)
    assert _run(manifest, tmp_path) == 10
    assert all(s["ok"] is False for s in _results(results)["claims"][0]["per_seed"])
    assert (elsewhere / "metrics.json").exists()  # the link went, not what it pointed to


def test_one_job_measures_every_claim_from_the_same_seeds(tmp_path: Path) -> None:
    """Two claims read the same metrics.json: the script runs once per seed, not per claim."""
    manifest, results = _setup(tmp_path, claims=[RELATIVE_C1, ABSOLUTE_C2])
    assert _run(manifest, tmp_path) == 0
    data = _results(results)
    assert [c["claim_ref"] for c in data["claims"]] == ["c1", "c2"]
    for claim in data["claims"]:
        assert [s["value"] for s in claim["per_seed"]] == pytest.approx([0.90, 0.91, 0.92])
    assert data["claims"][0]["baseline"]["key"] == "baseline/top1_accuracy"
    assert data["claims"][1]["baseline"] is None
    assert len(list((results / "seeds").iterdir())) == 3  # one script run per seed


def test_a_claim_whose_key_is_missing_fails_alone(tmp_path: Path) -> None:
    """A completed script that never writes a claim's key: that claim has no values, the
    other claims are untouched and the job still completes."""
    absent = {**ABSOLUTE_C2, "metric_key": "forest/top1_accuracy"}
    manifest, results = _setup(tmp_path, claims=[RELATIVE_C1, absent])
    assert _run(manifest, tmp_path) == 0
    data = _results(results)
    assert all(s["ok"] for s in data["claims"][0]["per_seed"])
    assert [(s["value"], s["ok"]) for s in data["claims"][1]["per_seed"]] == [(None, False)] * 3
    assert data["exit_reason"] == "completed"


def test_a_seed_with_no_claim_value_is_an_experiment_error(tmp_path: Path) -> None:
    absent = {**ABSOLUTE_C2, "metric_key": "forest/top1_accuracy"}
    manifest, results = _setup(tmp_path, claims=[absent])
    assert _run(manifest, tmp_path) == 10
    assert _results(results)["exit_reason"] == "experiment_error"


@pytest.mark.parametrize(
    "claims",
    [
        [],
        [RELATIVE_C1, RELATIVE_C1],
        [{**RELATIVE_C1, "baseline_key": None}],
        [{**ABSOLUTE_C2, "baseline_key": "baseline/top1_accuracy"}],
        [{**ABSOLUTE_C2, "metric_key": "../top1_accuracy"}],
        [ABSOLUTE_C2] * 6,
        [{**RELATIVE_C1, "baseline_key": "top1_accuracy"}],
    ],
    ids=["none", "duplicate", "relative-no-key", "absolute-key", "bad-key", "too-many", "same-key"],
)
def test_an_invalid_claim_list_is_a_contract_error(
    tmp_path: Path, claims: list[dict[str, Any]]
) -> None:
    manifest, _ = _setup(tmp_path, claims=claims)
    assert _run(manifest, tmp_path) == 30


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks and FIFOs")
@pytest.mark.parametrize("mode", ["devzero", "fifo"])
def test_a_metrics_file_that_is_not_a_regular_file_fails_the_seed(
    tmp_path: Path, mode: str
) -> None:
    manifest, results = _setup(tmp_path, mode=mode, budget=60)
    started = time.monotonic()
    assert _run(manifest, tmp_path) == 10  # every seed failed, the runner did not hang
    assert time.monotonic() - started < 30
    assert _results(results)["exit_reason"] == "experiment_error"


def test_a_termination_between_two_claims_never_repeats_a_seed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SIGTERM can land while one seed's values are being recorded claim by claim."""
    manifest, results = _setup(tmp_path, claims=[RELATIVE_C1, ABSOLUTE_C2])
    real, calls = entrypoint._metric, []

    def metric(metrics: Any, key: str) -> Any:
        calls.append(key)
        if len(calls) == 5:  # seed 1: c1 and its baseline recorded, c2 not yet
            raise entrypoint.Terminated
        return real(metrics, key)

    monkeypatch.setattr(entrypoint, "_metric", metric)
    assert _run(manifest, tmp_path) == 20
    data = _results(results)
    for claim in data["claims"]:
        assert [s["seed"] for s in claim["per_seed"]] == [0, 1, 2]
    baseline = data["claims"][0]["baseline"]["per_seed"]
    assert [s["seed"] for s in baseline] == [0, 1, 2]
    assert [s["ok"] for s in data["claims"][1]["per_seed"]] == [True, False, False]


def test_a_missing_baseline_key_keeps_the_claims_values(tmp_path: Path) -> None:
    claim = {**RELATIVE_C1, "baseline_key": "forest/top1_accuracy"}
    manifest, results = _setup(tmp_path, claims=[claim])
    assert _run(manifest, tmp_path) == 0
    data = _results(results)["claims"][0]
    assert all(s["ok"] for s in data["per_seed"])
    assert [s["value"] for s in data["baseline"]["per_seed"]] == [None] * 3


def test_a_claim_measured_on_some_seeds_only_still_completes(tmp_path: Path) -> None:
    """mode typed: top1_accuracy is a real number on seed 2 only; the baseline key on all."""
    other = {**ABSOLUTE_C2, "metric_key": "baseline/top1_accuracy"}
    manifest, results = _setup(tmp_path, mode="typed", claims=[RELATIVE_C1, other])
    assert _run(manifest, tmp_path) == 0
    c1, c2 = _results(results)["claims"]
    assert [s["ok"] for s in c1["per_seed"]] == [False, False, True]
    assert all(s["ok"] for s in c2["per_seed"])


def _extract_with_package(archive: Path, dest: Path, sha256: str) -> None:
    """The overlay holds one installed package, as the provisioner's pip --target leaves it."""
    _extract(archive, dest, sha256)
    if archive.name == "overlay.tar.zst":
        info = dest / "demo_pkg-1.2.3.dist-info"
        info.mkdir(parents=True)
        (info / "METADATA").write_text("Metadata-Version: 2.1\nName: demo-pkg\nVersion: 1.2.3\n")


def test_env_json_records_the_packages_driver_and_image(tmp_path: Path) -> None:
    """execution-contracts §3: what the experiment ran with, for the Proof Bundle."""
    manifest, results = _setup(tmp_path)
    code = _run(manifest, tmp_path, extract=_extract_with_package, gpu_driver=lambda: "550.54.15")
    assert code == 0
    env = json.loads((results / "env.json").read_text())
    assert env["image_digest"] == "sha256:" + "0" * 64 and env["gpu"] == "NVIDIA L40S"
    assert env["driver"] == "550.54.15"
    assert "demo-pkg==1.2.3" in env["packages"]  # the overlay, as the script imports it
    assert any(p.startswith("pytest==") for p in env["packages"])  # the base environment
    assert env["packages"] == sorted(env["packages"], key=str.lower)
    assert env["packages_truncated"] is False


def test_the_package_list_is_read_before_any_untrusted_code_runs(tmp_path: Path) -> None:
    """A script that plants metadata in the overlay never changes what env.json reports."""
    plant = SCRIPT.replace(
        "os.makedirs(a.out, exist_ok=True)\n",
        "os.makedirs(a.out, exist_ok=True)\n"
        "d = os.path.join(os.environ['PYTHONPATH'].split(os.pathsep)[0], 'fake-9.9.dist-info')\n"
        "os.makedirs(d, exist_ok=True)\n"
        "open(os.path.join(d, 'METADATA'), 'w').write('Name: fake\\nVersion: 9.9\\n')\n",
    )
    manifest, results = _setup(tmp_path, files={"train.py": plant})
    assert _run(manifest, tmp_path, gpu_driver=lambda: None) == 0
    env = json.loads((results / "env.json").read_text())
    assert not any(p.startswith("fake==") for p in env["packages"])
    assert env["driver"] is None


def test_installed_packages_are_bounded(tmp_path: Path) -> None:
    for i in range(5):
        info = tmp_path / f"p{i}-1.0.dist-info"
        info.mkdir()
        (info / "METADATA").write_text(f"Name: p{i}\nVersion: 1.0\n")
    # each entry is 7 characters plus quotes and a comma in env.json: three fit in 31 bytes
    assert entrypoint.installed_packages([tmp_path], max_bytes=31) == (
        ["p0==1.0", "p1==1.0", "p2==1.0"],
        True,
    )
    assert entrypoint.installed_packages([tmp_path]) == (
        [f"p{i}==1.0" for i in range(5)],
        False,
    )


def test_an_inventory_failure_never_fails_the_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """env.json is a diagnostic: a broken metadata scan leaves an empty list, nothing more."""

    def broken(path: Any = None) -> Any:
        raise RuntimeError("metadata scan failed")

    monkeypatch.setattr(entrypoint.importlib.metadata, "distributions", broken)
    manifest, results = _setup(tmp_path)
    assert _run(manifest, tmp_path, gpu_driver=lambda: None) == 0
    env = json.loads((results / "env.json").read_text())
    assert env["packages"] == [] and env["packages_truncated"] is False


def _dist_info(root: Path, dirname: str, metadata: bytes) -> None:
    info = root / dirname
    info.mkdir()
    (info / "METADATA").write_bytes(metadata)


def test_only_well_formed_names_and_versions_are_listed(tmp_path: Path) -> None:
    """Wheel authors write these strings: env.json is signed, so control characters, folded
    headers and markup never get in."""
    _dist_info(tmp_path, "good-1.0.dist-info", b"Name: good\nVersion: 1.0\n")
    _dist_info(tmp_path, "nul-1.0.dist-info", b"Name: a\x00b\nVersion: 1.0\n")
    _dist_info(tmp_path, "fold-1.0.dist-info", b"Name: foo\n bar\nVersion: 1.0\n")
    _dist_info(tmp_path, "tag-1.0.dist-info", b"Name: x\nVersion: <script>1\n")
    _dist_info(tmp_path, "junk-1.0.dist-info", bytes(range(256)))
    assert entrypoint.installed_packages([tmp_path]) == (["good==1.0"], False)


def test_an_oversized_metadata_file_is_skipped_unread(tmp_path: Path) -> None:
    _dist_info(tmp_path, "big-1.0.dist-info", b"Name: big\nVersion: 1.0\n" + b"x" * (2 << 20))
    _dist_info(tmp_path, "small-1.0.dist-info", b"Name: small\nVersion: 1.0\n")
    assert entrypoint.installed_packages([tmp_path]) == (["small==1.0"], False)


def test_the_scan_stops_after_its_distribution_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(entrypoint, "MAX_DISTRIBUTIONS", 2)
    for i in range(4):
        _dist_info(tmp_path, f"p{i}-1.0.dist-info", f"Name: p{i}\nVersion: 1.0\n".encode())
    packages, truncated = entrypoint.installed_packages([tmp_path])
    assert len(packages) == 2 and truncated

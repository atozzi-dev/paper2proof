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


NONCE = "a1" * 16


def workspace_tar(files: dict[str, bytes], links: dict[str, str] | None = None) -> bytes:
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for name, target in (links or {}).items():
            info = tarfile.TarInfo(name)
            info.type, info.linkname = tarfile.SYMTYPE, target
            tar.addfile(info)
    return buffer.getvalue()


def _setup(
    tmp_path: Path,
    mode: str = "ok",
    tar: bytes | None = None,
    overlay: bool = True,
    **overrides: Any,
) -> tuple[Path, Path]:
    import hashlib

    mnt = tmp_path / "mnt"
    out = mnt / "smoke" / "3"
    out.mkdir(parents=True)
    tar = tar if tar is not None else workspace_tar({"train.py": SCRIPT.encode()})
    (out / "workspace.tar").write_bytes(tar)
    (out / "workspace").mkdir()  # planted files next to the archive are never used
    (out / "workspace" / "sitecustomize.py").write_text("raise SystemExit('planted')")
    if overlay:
        (mnt / "staging").mkdir(parents=True)
        (mnt / "staging" / "overlay.tar.zst").write_bytes(b"")
    manifest = {
        "schema": "p2p.smoke/v1",
        "run_id": RUN,
        "iteration": 3,
        "argv": ["python", "train.py", "--out", "out", "--smoke", "--mode", mode],
        "timeout_s": 60,
        "workspace_sha256": hashlib.sha256(tar).hexdigest(),
        "nonce": NONCE,
        "archives": {"overlay": "a" * 64, "repo": None, "data": []},
    }
    manifest.update(overrides)
    path = tmp_path / "smoke.json"
    path.write_text(json.dumps(manifest))
    return path, mnt


EXTRACTED: list[tuple[str, str]] = []


def _extract(archive: Path, dest: Path, sha256: str) -> None:
    EXTRACTED.append((archive.name, sha256))
    dest.mkdir(parents=True, exist_ok=True)


def _run(path: Path, mnt: Path, tmp_path: Path, **kwargs: Any) -> dict[str, Any]:
    code = smoke.run(
        path,
        mnt=mnt,
        work=tmp_path / "work",
        extract=_extract,
        **kwargs,
    )
    assert code == 0  # the job succeeds whenever it could write a result
    result: dict[str, Any] = json.loads((mnt / "smoke" / "3" / "result.json").read_text())
    return result


def test_a_passing_script_reports_its_metrics(tmp_path: Path) -> None:
    path, mnt = _setup(tmp_path)
    result = _run(path, mnt, tmp_path)
    assert result["exit_code"] == 0
    assert result["metrics"] == {"acc": 0.5}  # flat numeric values only
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
        {"workspace_sha256": "not-hex"},
        {"nonce": "x"},
        {"archives": {"overlay": "x", "repo": None, "data": []}},
        {"archives": None},
    ],
)
def test_invalid_manifests_are_contract_errors(tmp_path: Path, override: dict[str, Any]) -> None:
    path, mnt = _setup(tmp_path, **override)
    code = smoke.run(path, mnt=mnt, work=tmp_path / "work", extract=lambda a, d, s: None)
    assert code == 30
    assert not (mnt / "smoke" / "3" / "result.json").exists()


def test_the_result_echoes_the_nonce_and_planted_files_are_ignored(tmp_path: Path) -> None:
    path, mnt = _setup(tmp_path)
    result = _run(path, mnt, tmp_path)
    assert result["nonce"] == NONCE
    assert result["exit_code"] == 0
    assert not (tmp_path / "work" / "workspace" / "sitecustomize.py").exists()


@pytest.mark.parametrize("case", ["digest", "overlay", "escape"])
def test_setup_problems_still_produce_a_result(tmp_path: Path, case: str) -> None:
    if case == "digest":
        path, mnt = _setup(tmp_path, workspace_sha256="0" * 64)
    elif case == "overlay":
        path, mnt = _setup(tmp_path, overlay=False)
    else:
        path, mnt = _setup(tmp_path, tar=workspace_tar({"../evil.py": b"x"}))
    result = _run(path, mnt, tmp_path)
    assert result["exit_code"] == -1
    assert result["stderr"].startswith("setup error")
    assert result["nonce"] == NONCE
    assert not (tmp_path / "evil.py").exists()


def test_symlinks_in_the_workspace_are_not_extracted(tmp_path: Path) -> None:
    tar = workspace_tar({"train.py": SCRIPT.encode()}, links={"secret.txt": "/etc/passwd"})
    path, mnt = _setup(tmp_path, tar=tar)
    _run(path, mnt, tmp_path)
    assert not (tmp_path / "work" / "workspace" / "secret.txt").exists()


def test_results_stay_small_with_non_ascii_output(tmp_path: Path) -> None:
    script = (
        "import sys\n"
        "for _ in range(4000): print('\u2588' * 100)\n"
        "for _ in range(4000): print('\u2588' * 100, file=sys.stderr)\n"
    )
    path, mnt = _setup(tmp_path, tar=workspace_tar({"train.py": script.encode()}))
    _run(path, mnt, tmp_path)
    assert (mnt / "smoke" / "3" / "result.json").stat().st_size < 200 * 1024


def test_only_flat_numeric_metrics_are_reported(tmp_path: Path) -> None:
    script = (
        "import json, os, sys\n"
        "out = sys.argv[sys.argv.index('--out') + 1]\n"
        "os.makedirs(out, exist_ok=True)\n"
        "json.dump({'acc': 0.5, 'curve': [1e15] * 2000, 'nested': {'a': 1}, 'flag': True},\n"
        "          open(os.path.join(out, 'metrics.json'), 'w'))\n"
    )
    path, mnt = _setup(tmp_path, tar=workspace_tar({"train.py": script.encode()}))
    result = _run(path, mnt, tmp_path)
    assert result["metrics"] == {"acc": 0.5}


def test_staging_archives_are_extracted_with_their_manifest_digests(tmp_path: Path) -> None:
    archives = {
        "overlay": "a" * 64,
        "repo": "b" * 64,
        "data": [{"name": "smoke", "sha256": "c" * 64}],
    }
    path, mnt = _setup(tmp_path, archives=archives)
    (mnt / "staging" / "repo.tar.zst").write_bytes(b"")
    (mnt / "staging" / "data").mkdir()
    (mnt / "staging" / "data" / "smoke.tar.zst").write_bytes(b"")
    EXTRACTED.clear()
    _run(path, mnt, tmp_path)
    assert EXTRACTED == [
        ("overlay.tar.zst", "a" * 64), ("repo.tar.zst", "b" * 64), ("smoke.tar.zst", "c" * 64),
    ]  # fmt: skip


def test_an_unlisted_repo_archive_is_ignored(tmp_path: Path) -> None:
    path, mnt = _setup(tmp_path)  # repo digest null
    (mnt / "staging" / "repo.tar.zst").write_bytes(b"planted")
    EXTRACTED.clear()
    _run(path, mnt, tmp_path)
    assert [name for name, _ in EXTRACTED] == ["overlay.tar.zst"]

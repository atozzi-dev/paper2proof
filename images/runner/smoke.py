"""Smoke runner for CPU smoke jobs (the Sandboxes fallback, execution-contracts §8/§10).
Standard library only; same image as the exec entrypoint (p2p-runner-cpu).

1. Read /etc/p2p/smoke.json and validate it (exit 30 on any contract problem).
2. Extract the overlay, repository and smoke data from /mnt/run/staging to local disk; extract
   the Coder's workspace from /mnt/run/smoke/<iteration>/workspace.tar after checking its sha256
   against the manifest (regular files only: nothing else in the bucket is trusted).
3. Run `python <script> [args]` in the workspace with a timeout (the job has no internet).
4. Write /mnt/run/smoke/<iteration>/result.json: exit code, output tails, duration, metrics read
   from out/metrics.json and the manifest's nonce. The control plane validates it as untrusted
   (app/execution/smoke.py). A setup problem is a result too (exit code -1, "setup error").
Exit 0 whenever a result was written: a failing script is a result, not a job failure.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))  # works under `python -I` too
from entrypoint import (
    EXIT_CONTRACT,
    RUN_ID,
    SHA256,
    ContractError,
    LiveLog,
    Terminated,
    extract_stream,
    run_seed,
    tar_extract,
)

SCHEMA = "p2p.smoke/v1"
SCRIPT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,199}\.py$")
MAX_ARGS = 32
MAX_TIMEOUT_S = 240
TAIL_BYTES = 48 * 1024  # per stream, as encoded in result.json (< 256 KiB in total)
METRICS = "out/metrics.json"
NONCE = re.compile(r"^[0-9a-f]{32}$")
MAX_WORKSPACE_BYTES = 16 * 1024 * 1024


class SmokeContractError(Exception):
    pass


def validate(manifest: Any) -> dict[str, Any]:
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        raise SmokeContractError("unsupported smoke manifest")
    if not isinstance(manifest.get("run_id"), str) or not RUN_ID.fullmatch(manifest["run_id"]):
        raise SmokeContractError("invalid run_id")
    iteration = manifest.get("iteration")
    if not isinstance(iteration, int) or isinstance(iteration, bool) or not 1 <= iteration < 10_000:
        raise SmokeContractError("invalid iteration")
    timeout = manifest.get("timeout_s")
    if (
        not isinstance(timeout, int)
        or isinstance(timeout, bool)
        or not 1 <= timeout <= MAX_TIMEOUT_S
    ):
        raise SmokeContractError("invalid timeout")
    argv = manifest.get("argv")
    if not isinstance(argv, list) or not 2 <= len(argv) <= MAX_ARGS:
        raise SmokeContractError("argv must be: python <script.py> [args]")
    if not all(isinstance(a, str) for a in argv) or argv[0] != "python":
        raise SmokeContractError("argv must be: python <script.py> [args]")
    script = argv[1]
    if not SCRIPT.fullmatch(script) or ".." in script.split("/") or "//" in script:
        raise SmokeContractError("invalid script path")
    if any(len(a) > 200 or any(ord(c) < 32 for c in a) for a in argv[2:]):
        raise SmokeContractError("invalid argument")
    digest, nonce = manifest.get("workspace_sha256"), manifest.get("nonce")
    if not isinstance(digest, str) or not SHA256.fullmatch(digest):
        raise SmokeContractError("invalid workspace digest")
    if not isinstance(nonce, str) or not NONCE.fullmatch(nonce):
        raise SmokeContractError("invalid nonce")
    archives = manifest.get("archives")
    if not isinstance(archives, dict) or set(archives) != {"overlay", "repo", "data"}:
        raise SmokeContractError("invalid archives")
    if not isinstance(archives["overlay"], str) or not SHA256.fullmatch(archives["overlay"]):
        raise SmokeContractError("invalid overlay digest")
    repo = archives["repo"]
    if repo is not None and (not isinstance(repo, str) or not SHA256.fullmatch(repo)):
        raise SmokeContractError("invalid repo digest")
    data = archives["data"]
    if not isinstance(data, list) or len(data) > 1:
        raise SmokeContractError("invalid data archives")
    for entry in data:
        if not isinstance(entry, dict) or entry.get("name") != "smoke":
            raise SmokeContractError("smoke jobs only extract the smoke subset")
        if not isinstance(entry.get("sha256"), str) or not SHA256.fullmatch(entry["sha256"]):
            raise SmokeContractError("invalid smoke digest")
    return manifest


class Tail:
    """Keeps only the last characters written (bounded memory, like a ring buffer)."""

    def __init__(self, limit: int) -> None:
        self._chunks: collections.deque[str] = collections.deque()
        self._size = 0
        self._limit = limit
        self._lock = threading.Lock()

    def write(self, text: str) -> None:
        with self._lock:
            self._chunks.append(text)
            self._size += len(text)
            while self._size - len(self._chunks[0]) >= self._limit:
                self._size -= len(self._chunks.popleft())

    def flush(self) -> None:
        return None

    def value(self) -> str:
        with self._lock:
            return "".join(self._chunks)


def _encoded_tail(text: str, limit: int = TAIL_BYTES) -> str:
    """The longest suffix whose JSON encoding fits in `limit` bytes."""
    while text and len(json.dumps(text, ensure_ascii=False).encode()) > limit:
        text = text[len(text) // 4 or 1 :]
    return text


def _extract_workspace(archive: Path, dest: Path, digest: str) -> None:
    if not archive.is_file() or archive.stat().st_size > MAX_WORKSPACE_BYTES:
        raise ContractError("workspace archive missing or too large")
    with archive.open("rb") as handle:
        data = handle.read(MAX_WORKSPACE_BYTES + 1)
    if len(data) > MAX_WORKSPACE_BYTES:
        raise ContractError("workspace archive too large")
    if hashlib.sha256(data).hexdigest() != digest:
        raise ContractError("workspace archive does not match the manifest digest")
    import io

    shutil.rmtree(dest, ignore_errors=True)
    extract_stream(io.BytesIO(data), dest, max_bytes=MAX_WORKSPACE_BYTES, max_members=1000)


def _read_metrics(path: Path) -> dict[str, float] | None:
    """Flat, finite, numeric top-level values only (the control plane filters them again):
    nested or huge values would bloat result.json past what the control plane reads."""
    if not path.is_file() or path.stat().st_size > 64 * 1024:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    metrics: dict[str, float] = {}
    for key, value in list(data.items())[:100]:
        if not isinstance(key, str) or len(key) > 100 or type(value) not in (int, float):
            continue
        try:
            number = float(value)
        except OverflowError:
            continue
        if math.isfinite(number):
            metrics[key] = number
    return metrics


def run(
    manifest_path: Path,
    *,
    mnt: Path,
    work: Path,
    extract: Callable[[Path, Path, str], None] = tar_extract,
    timeout_override_s: float | None = None,
    kill_grace_s: float = 5.0,
) -> int:
    try:
        manifest = validate(json.loads(manifest_path.read_text(encoding="utf-8")))
    except (OSError, ValueError, RecursionError, SmokeContractError) as err:
        print(f"CONTRACT ERROR: {err}", file=sys.stderr)
        return EXIT_CONTRACT
    out_dir = mnt / "smoke" / str(manifest["iteration"])
    staging = mnt / "staging"
    overlay, repo, data, workspace = (work / n for n in ("overlay", "repo", "data", "workspace"))

    def write_result(result: dict[str, Any]) -> int:
        result["nonce"] = manifest["nonce"]
        out_dir.mkdir(parents=True, exist_ok=True)
        tmp = out_dir / "result.json.tmp"
        tmp.write_text(
            json.dumps(result, ensure_ascii=False), encoding="utf-8", errors="backslashreplace"
        )
        tmp.replace(out_dir / "result.json")
        shutil.rmtree(work / "home", ignore_errors=True)
        return 0

    started = time.monotonic()
    try:
        digests = manifest["archives"]  # from the validated provision report, not the bucket
        if not (staging / "overlay.tar.zst").is_file():
            raise ContractError("overlay archive missing")
        extract(staging / "overlay.tar.zst", overlay, digests["overlay"])
        if digests["repo"] is not None:
            if not (staging / "repo.tar.zst").is_file():
                raise ContractError("repository archive missing")
            extract(staging / "repo.tar.zst", repo, digests["repo"])
        for entry in digests["data"]:
            if not (staging / "data" / "smoke.tar.zst").is_file():
                raise ContractError("smoke data archive missing")
            extract(staging / "data" / "smoke.tar.zst", data / "smoke", entry["sha256"])
        _extract_workspace(out_dir / "workspace.tar", workspace, manifest["workspace_sha256"])
        (work / "home").mkdir(parents=True, exist_ok=True)
    except (ContractError, OSError, subprocess.SubprocessError, Terminated) as err:
        reason = "terminated" if isinstance(err, Terminated) else str(err)[:500]
        return write_result(
            {"exit_code": -1, "stdout": "", "stderr": f"setup error: {reason}",
             "duration_s": round(time.monotonic() - started, 3), "metrics": None}
        )  # fmt: skip
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(work / "home"),
        "PYTHONPATH": os.pathsep.join(str(p) for p in (overlay, repo, workspace)),
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
        "P2P_DATA_DIR": str(data),
        "P2P_SMOKE": "1",
        "HF_HUB_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
    }
    argv = [sys.executable, *manifest["argv"][1:]]
    timeout = timeout_override_s if timeout_override_s is not None else manifest["timeout_s"]
    stdout, stderr = Tail(TAIL_BYTES), Tail(TAIL_BYTES)
    terminated = False
    with LiveLog(work / "live.log", 10.0, None) as live:
        try:
            code, timed_out = run_seed(
                argv, env=env, cwd=workspace, budget_s=timeout, kill_grace_s=kill_grace_s,
                stdout=stdout, stderr=stderr, live=live,
            )  # fmt: skip
        except Terminated:
            code, timed_out, terminated = -1, True, True
    err_text = stderr.value()
    if timed_out:
        err_text += (
            "\nsmoke run terminated" if terminated else f"\nsmoke run timed out after {timeout:g} s"
        )
    return write_result(
        {
            "exit_code": -1 if timed_out else code,
            "stdout": _encoded_tail(stdout.value()),
            "stderr": _encoded_tail(err_text),
            "duration_s": round(time.monotonic() - started, 3),
            "metrics": None if timed_out or code != 0 else _read_metrics(workspace / METRICS),
        }
    )


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - exercised in the image
    parser = argparse.ArgumentParser(description="Paper2Proof smoke runner")
    parser.add_argument("--manifest", default="/etc/p2p/smoke.json")
    parser.add_argument("--mnt", default="/mnt/run")
    parser.add_argument("--work", default="/work")
    args = parser.parse_args(argv)
    return run(Path(args.manifest), mnt=Path(args.mnt), work=Path(args.work))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

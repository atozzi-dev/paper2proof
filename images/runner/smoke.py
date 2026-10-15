"""Smoke runner for CPU smoke jobs (the Sandboxes fallback, execution-contracts §8/§10).
Standard library only; same image as the exec entrypoint (p2p-runner-cpu).

1. Read /etc/p2p/smoke.json and validate it (exit 30 on any contract problem).
2. Extract the overlay, repository and smoke data from /mnt/run/staging to local disk; copy the
   Coder's workspace from /mnt/run/smoke/<iteration>/workspace.
3. Run `python <script> [args]` in the workspace with a timeout (the job has no internet).
4. Write /mnt/run/smoke/<iteration>/result.json: exit code, output tails, duration, metrics read
   from out/metrics.json. The control plane validates it as untrusted (app/execution/smoke.py).
Exit 0 whenever a result was written: a failing script is a result, not a job failure.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from entrypoint import EXIT_CONTRACT, RUN_ID, LiveLog, run_seed, tar_extract

SCHEMA = "p2p.smoke/v1"
SCRIPT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,199}\.py$")
MAX_ARGS = 32
MAX_TIMEOUT_S = 240
TAIL_CHARS = 64 * 1024
METRICS = "out/metrics.json"


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
    return manifest


def _read_metrics(path: Path) -> dict[str, Any] | None:
    if not path.is_file() or path.stat().st_size > 64 * 1024:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None  # filtered again by the control plane


def run(
    manifest_path: Path,
    *,
    mnt: Path,
    work: Path,
    extract: Callable[[Path, Path], None] = tar_extract,
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
    for archive, dest in (
        (staging / "overlay.tar.zst", overlay),
        (staging / "repo.tar.zst", repo),
        (staging / "data" / "smoke.tar.zst", data / "smoke"),
    ):
        if archive.is_file():
            extract(archive, dest)
    shutil.rmtree(workspace, ignore_errors=True)
    shutil.copytree(out_dir / "workspace", workspace, symlinks=False)
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(work / "home"),
        "PYTHONPATH": os.pathsep.join(str(p) for p in (overlay, repo, workspace)),
        "PYTHONUNBUFFERED": "1",
        "P2P_DATA_DIR": str(data),
        "P2P_SMOKE": "1",
        "HF_HUB_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
    }
    argv = [sys.executable, *manifest["argv"][1:]]
    timeout = timeout_override_s if timeout_override_s is not None else manifest["timeout_s"]
    stdout, stderr = io.StringIO(), io.StringIO()
    started = time.monotonic()
    with LiveLog(work / "live.log", 10.0, None) as live:
        code, timed_out = run_seed(
            argv, env=env, cwd=workspace, budget_s=timeout, kill_grace_s=kill_grace_s,
            stdout=stdout, stderr=stderr, live=live,
        )  # fmt: skip
    err_text = stderr.getvalue()
    if timed_out:
        err_text += f"\nsmoke run timed out after {timeout:g} s"
    result = {
        "exit_code": -1 if timed_out else code,
        "stdout": stdout.getvalue()[-TAIL_CHARS:],
        "stderr": err_text[-TAIL_CHARS:],
        "duration_s": round(time.monotonic() - started, 3),
        "metrics": None if timed_out or code != 0 else _read_metrics(workspace / METRICS),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
    shutil.rmtree(work / "home", ignore_errors=True)
    return 0


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - exercised in the image
    parser = argparse.ArgumentParser(description="Paper2Proof smoke runner")
    parser.add_argument("--manifest", default="/etc/p2p/smoke.json")
    parser.add_argument("--mnt", default="/mnt/run")
    parser.add_argument("--work", default="/work")
    args = parser.parse_args(argv)
    return run(Path(args.manifest), mnt=Path(args.mnt), work=Path(args.work))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

"""Runner entrypoint for EXEC jobs (docs/specs/execution-contracts.md §7). Standard library only.

1. Read /etc/p2p/manifest.json and validate it (exit 30 on any contract problem).
2. Extract overlay, repository and datasets to local disk under /work.
3. Run the experiment script once per seed within the remaining budget
   (SIGTERM at the deadline, SIGKILL 30 s later); write seeds/<seed>/metrics.json.
4. Aggregate results.json; mirror the last 200 output lines to live.log every 10 s.
Exit codes: 0 completed, 10 experiment error, 20 budget timeout, 30 contract error.
"""

from __future__ import annotations

import argparse
import collections
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

EXIT_OK, EXIT_EXPERIMENT, EXIT_BUDGET, EXIT_CONTRACT = 0, 10, 20, 30
REASONS = {
    EXIT_OK: "completed",
    EXIT_EXPERIMENT: "experiment_error",
    EXIT_BUDGET: "budget_timeout",
    EXIT_CONTRACT: "contract_error",
}
LIVE_LINES = 200
ENTRYPOINT = re.compile(r"^workspace/[A-Za-z0-9_./-]+\.py$")
RUN_ID = re.compile(r"^r_[0-9a-z]{26}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
MANIFEST_KEYS = {
    "schema", "run_id", "attempt", "image_digest", "entrypoint", "args", "seeds", "budget_seconds",
    "paths", "claim_ref", "metric", "higher_is_better", "claim_type", "baseline_name", "scale",
}  # fmt: skip
PATH_KEYS = {"overlay", "repo", "data_dir", "workspace", "cache", "results"}


class ContractError(Exception):
    pass


def validate(manifest: Any) -> dict[str, Any]:
    """Mirror of schemas/manifest.schema.json (the image doesn't ship jsonschema)."""

    def need(ok: bool, what: str) -> None:
        if not ok:
            raise ContractError(f"invalid manifest: {what}")

    need(isinstance(manifest, dict) and set(manifest) == MANIFEST_KEYS, "keys")
    m: dict[str, Any] = manifest
    need(m["schema"] == "p2p.manifest/v1", "schema")
    need(isinstance(m["run_id"], str) and bool(RUN_ID.fullmatch(m["run_id"])), "run_id")
    need(type(m["attempt"]) is int and 1 <= m["attempt"] <= 5, "attempt")
    need(
        isinstance(m["image_digest"], str) and bool(DIGEST.fullmatch(m["image_digest"])),
        "image_digest",
    )
    entry = m["entrypoint"]
    need(
        isinstance(entry, str) and bool(ENTRYPOINT.fullmatch(entry)) and ".." not in entry,
        "entrypoint",
    )
    args = m["args"]
    need(
        isinstance(args, list)
        and len(args) <= 20
        and all(isinstance(a, str) and len(a) <= 200 for a in args),
        "args",
    )
    seeds = m["seeds"]
    need(
        isinstance(seeds, list) and 1 <= len(seeds) <= 20 and len(set(seeds)) == len(seeds)
        and all(type(s) is int and s >= 0 for s in seeds),
        "seeds",
    )  # fmt: skip
    need(type(m["budget_seconds"]) is int and 60 <= m["budget_seconds"] <= 1800, "budget_seconds")
    paths = m["paths"]
    need(isinstance(paths, dict) and set(paths) == PATH_KEYS, "paths")
    need(all(isinstance(paths[k], str) for k in PATH_KEYS - {"repo"}), "paths")
    need(paths["repo"] is None or isinstance(paths["repo"], str), "paths.repo")
    need(isinstance(m["metric"], str) and 0 < len(m["metric"]) <= 64, "metric")
    need(isinstance(m["higher_is_better"], bool), "higher_is_better")
    need(m["claim_type"] in ("ABSOLUTE", "RELATIVE"), "claim_type")
    need(m["claim_type"] == "ABSOLUTE" or bool(m["baseline_name"]), "baseline_name")
    need(isinstance(m["scale"], dict) and isinstance(m["scale"].get("full"), bool), "scale")
    return m


def tar_extract(archive: Path, dest: Path) -> None:  # pragma: no cover - needs GNU tar in the image
    dest.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "tar",
            "--zstd",
            "-xf",
            str(archive),
            "-C",
            str(dest),
            "--no-same-owner",
            "--no-same-permissions",
        ],
        check=True,
        timeout=900,
    )


def nvidia_gpu_name() -> str:  # pragma: no cover - exercised in the GPU image
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=20, check=True,
        )  # fmt: skip
        return out.stdout.strip().splitlines()[0][:128] or "unknown"
    except (OSError, subprocess.SubprocessError, IndexError):
        return "cpu"


class LiveLog:
    """Keeps the last LIVE_LINES lines of all output and flushes them every interval."""

    def __init__(
        self, path: Path, interval_s: float, on_flush: Callable[[str], None] | None
    ) -> None:
        self._lines: collections.deque[str] = collections.deque(maxlen=LIVE_LINES)
        self._lock = threading.Lock()
        self._path = path
        self._interval = interval_s
        self._on_flush = on_flush
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def add(self, line: str) -> None:
        with self._lock:
            self._lines.append(line.rstrip("\n"))

    def flush(self) -> None:
        with self._lock:
            text = "\n".join(self._lines) + ("\n" if self._lines else "")
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self._path)
        if self._on_flush:
            self._on_flush(text)

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            self.flush()

    def __enter__(self) -> LiveLog:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join()
        self.flush()


def _pump(stream: Any, sink: Any, live: LiveLog) -> None:
    for line in iter(stream.readline, ""):
        sink.write(line)
        sink.flush()
        live.add(line)


def run_seed(
    argv: Sequence[str], *, env: dict[str, str], cwd: Path, budget_s: float, kill_grace_s: float,
    stdout: Any, stderr: Any, live: LiveLog,
) -> tuple[int, bool]:  # fmt: skip
    """Run one seed; returns (exit code, timed out)."""
    proc = subprocess.Popen(
        list(argv),
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    pumps = [
        threading.Thread(target=_pump, args=(proc.stdout, stdout, live), daemon=True),
        threading.Thread(target=_pump, args=(proc.stderr, stderr, live), daemon=True),
    ]
    for t in pumps:
        t.start()
    try:
        code = proc.wait(timeout=max(budget_s, 0.0))
        timed_out = False
    except subprocess.TimeoutExpired:
        proc.terminate()  # SIGTERM
        try:
            proc.wait(timeout=kill_grace_s)
        except subprocess.TimeoutExpired:
            proc.kill()  # SIGKILL
            proc.wait()
        code, timed_out = -1, True
    for t in pumps:
        t.join(timeout=5)
    return code, timed_out


def _read_metric(path: Path, key: str) -> float | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        value = float(data[key])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return value if math.isfinite(value) else None


def run(
    manifest_path: Path,
    *,
    work: Path,
    extract: Callable[[Path, Path], None] = tar_extract,
    gpu_name: Callable[[], str] = nvidia_gpu_name,
    flush_interval_s: float = 10.0,
    on_flush: Callable[[str], None] | None = None,
    budget_override_s: float | None = None,
    kill_grace_s: float = 30.0,
) -> int:
    started = time.monotonic()
    try:
        manifest = validate(json.loads(manifest_path.read_text(encoding="utf-8")))
    except (OSError, ValueError, ContractError) as err:
        print(f"CONTRACT ERROR: {err}", file=sys.stderr)
        return EXIT_CONTRACT
    paths = manifest["paths"]
    results = Path(paths["results"])
    results.mkdir(parents=True, exist_ok=True)
    workspace = Path(paths["workspace"])
    script = workspace / manifest["entrypoint"].removeprefix("workspace/")
    if not script.is_file():
        print("CONTRACT ERROR: entrypoint not found in the workspace", file=sys.stderr)
        return EXIT_CONTRACT

    overlay, repo, data = work / "overlay", work / "repo", work / "data"
    if Path(paths["overlay"]).is_file():
        extract(Path(paths["overlay"]), overlay)
    if paths["repo"] and Path(paths["repo"]).is_file():
        extract(Path(paths["repo"]), repo)
    data_dir = Path(paths["data_dir"])
    if data_dir.is_dir():
        for archive in sorted(data_dir.glob("*.tar.zst")):
            extract(archive, data / archive.name.removesuffix(".tar.zst"))
    run_dir = work / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(work / "home"),
        "PYTHONPATH": os.pathsep.join(str(p) for p in (overlay, repo, workspace)),
        "PYTHONUNBUFFERED": "1",
        "P2P_DATA_DIR": str(data),
        "HF_HUB_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
    }
    budget = float(
        budget_override_s if budget_override_s is not None else manifest["budget_seconds"]
    )
    metric, relative = manifest["metric"], manifest["claim_type"] == "RELATIVE"
    per_seed, baseline, status = [], [], EXIT_OK
    with (
        (results / "stdout.log").open("w", encoding="utf-8") as out,
        (results / "stderr.log").open("w", encoding="utf-8") as err,
        LiveLog(results / "live.log", flush_interval_s, on_flush) as live,
    ):
        for seed in manifest["seeds"]:
            remaining = budget - (time.monotonic() - started)
            seed_dir = results / "seeds" / str(seed)
            if remaining <= 0:
                status = EXIT_BUDGET
                per_seed.append({"seed": seed, "value": None, "ok": False})
                baseline.append({"seed": seed, "value": None})
                continue
            argv = [
                sys.executable,
                str(script),
                "--seed",
                str(seed),
                "--out",
                str(seed_dir),
                *manifest["args"],
            ]
            code, timed_out = run_seed(
                argv, env=env, cwd=run_dir, budget_s=remaining, kill_grace_s=kill_grace_s,
                stdout=out, stderr=err, live=live,
            )  # fmt: skip
            value = _read_metric(seed_dir / "metrics.json", metric) if code == 0 else None
            if timed_out:
                status = EXIT_BUDGET
            elif value is None and status == EXIT_OK:
                status = EXIT_EXPERIMENT
            per_seed.append({"seed": seed, "value": value, "ok": value is not None})
            base = (
                _read_metric(seed_dir / "metrics.json", f"baseline/{metric}") if relative else None
            )
            baseline.append({"seed": seed, "value": base})

    report = {
        "schema": "p2p.results/v1",
        "run_id": manifest["run_id"],
        "attempt": manifest["attempt"],
        "claim_ref": manifest["claim_ref"],
        "claim_type": manifest["claim_type"],
        "metric": metric,
        "higher_is_better": manifest["higher_is_better"],
        "scale": manifest["scale"],
        "per_seed": per_seed,
        "baseline": {"name": manifest["baseline_name"], "per_seed": baseline} if relative else None,
        "wall_clock_s": round(time.monotonic() - started, 3),
        "gpu": gpu_name(),
        "exit_reason": REASONS[status],
    }
    (results / "results.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    (results / "env.json").write_text(
        json.dumps(
            {"python": sys.version, "image_digest": manifest["image_digest"], "gpu": report["gpu"]}
        ),
        encoding="utf-8",
    )
    shutil.rmtree(work / "home", ignore_errors=True)
    return status


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - exercised in the image
    parser = argparse.ArgumentParser(description="Paper2Proof runner entrypoint")
    parser.add_argument("--manifest", default="/etc/p2p/manifest.json")
    parser.add_argument("--work", default="/work")
    args = parser.parse_args(argv)
    return run(Path(args.manifest), work=Path(args.work))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

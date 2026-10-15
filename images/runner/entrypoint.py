"""Runner entrypoint for EXEC jobs (docs/specs/execution-contracts.md §7). Standard library only.

1. Read /etc/p2p/manifest.json and validate it (exit 30 on any contract problem).
2. Extract overlay, repository and datasets to local disk under /work (no links, no escapes,
   size and member caps).
3. Run the experiment script once per seed, in its own process group, within the remaining
   budget (SIGTERM to the group at the deadline, SIGKILL 30 s later); whatever the seed left
   behind is killed before the next one starts.
4. Rewrite results.json atomically after every seed; mirror the last 200 output lines to
   live.log every 10 s. SIGTERM to the runner writes partial results (budget_timeout).
Exit codes: 0 completed, 10 experiment error, 20 budget timeout, 30 contract error.
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath
from typing import IO, Any

EXIT_OK, EXIT_EXPERIMENT, EXIT_BUDGET, EXIT_CONTRACT = 0, 10, 20, 30
REASONS = {
    EXIT_OK: "completed",
    EXIT_EXPERIMENT: "experiment_error",
    EXIT_BUDGET: "budget_timeout",
    EXIT_CONTRACT: "contract_error",
}
LIVE_LINES = 200
LIVE_LINE_CHARS = 2000
READ_LINE_CHARS = 65536
LOG_CAP_CHARS = 20 * 1024 * 1024
MAX_ARCHIVE_BYTES = 40 * 2**30
MAX_ARCHIVE_MEMBERS = 500_000
ENTRYPOINT = re.compile(r"^workspace/[A-Za-z0-9_./-]+\.py$")
RUN_ID = re.compile(r"^r_[0-9a-z]{26}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
MANIFEST_KEYS = {
    "schema", "run_id", "attempt", "image_digest", "entrypoint", "args", "seeds", "budget_seconds",
    "paths", "claim_ref", "metric", "higher_is_better", "claim_type", "baseline_name", "scale",
}  # fmt: skip
PATH_KEYS = {"overlay", "repo", "data_dir", "workspace", "cache", "results"}
SCALE_KEYS = {"full", "fraction_of_data", "epochs", "paper_epochs"}
PASSTHROUGH_ENV = re.compile(
    r"^(LD_LIBRARY_PATH|TZ|CUDA_[A-Z0-9_]+|NVIDIA_[A-Z0-9_]+|NCCL_[A-Z0-9_]+)$"
)


class ContractError(Exception):
    pass


class Terminated(BaseException):
    """SIGTERM from the platform (watchdog or cancel): stop and write what we have."""


def validate(manifest: Any) -> dict[str, Any]:
    """Mirror of schemas/manifest.schema.json (the image doesn't ship jsonschema)."""
    try:
        return _validate(manifest)
    except (TypeError, KeyError, AttributeError) as err:
        raise ContractError(f"invalid manifest: {type(err).__name__}") from None


def _short_str(value: Any, limit: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit


def _validate(manifest: Any) -> dict[str, Any]:
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
        isinstance(seeds, list) and 1 <= len(seeds) <= 20
        and all(type(s) is int and s >= 0 for s in seeds) and len(set(seeds)) == len(seeds),
        "seeds",
    )  # fmt: skip
    need(type(m["budget_seconds"]) is int and 60 <= m["budget_seconds"] <= 1800, "budget_seconds")
    paths = m["paths"]
    need(isinstance(paths, dict) and set(paths) == PATH_KEYS, "paths")
    need(all(isinstance(paths[k], str) for k in PATH_KEYS - {"repo"}), "paths")
    need(paths["repo"] is None or isinstance(paths["repo"], str), "paths.repo")
    need(_short_str(m["claim_ref"], 128), "claim_ref")
    need(_short_str(m["metric"], 64), "metric")
    need(isinstance(m["higher_is_better"], bool), "higher_is_better")
    need(m["claim_type"] in ("ABSOLUTE", "RELATIVE"), "claim_type")
    need(m["baseline_name"] is None or _short_str(m["baseline_name"], 128), "baseline_name")
    need(m["claim_type"] == "ABSOLUTE" or bool(m["baseline_name"]), "baseline_name")
    scale = m["scale"]
    need(isinstance(scale, dict) and set(scale) <= SCALE_KEYS, "scale")
    need(isinstance(scale.get("full"), bool), "scale.full")
    return m


def extract_stream(stream: IO[bytes], dest: Path, *, max_bytes: int, max_members: int) -> None:
    """Extract a tar stream: regular files and directories only (links and devices are
    skipped), no absolute paths or `..`, bounded total size and member count."""
    dest.mkdir(parents=True, exist_ok=True)
    total = members = 0
    with tarfile.open(fileobj=stream, mode="r|") as tar:
        for member in tar:
            members += 1
            if members > max_members:
                raise ContractError("archive has too many members")
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts:
                raise ContractError(f"archive member escapes: {member.name[:200]}")
            if not (member.isfile() or member.isdir()):
                continue  # symlinks, hardlinks, devices: never extracted
            total += max(member.size, 0)
            if total > max_bytes:
                raise ContractError("archive larger than its cap")
            tar.extract(member, dest, filter="data")


def tar_extract(archive: Path, dest: Path) -> None:  # pragma: no cover - needs zstd in the image
    proc = subprocess.Popen(["zstd", "-dc", "--", str(archive)], stdout=subprocess.PIPE)
    if proc.stdout is None:
        raise ContractError("zstd produced no stream")
    try:
        extract_stream(
            proc.stdout, dest, max_bytes=MAX_ARCHIVE_BYTES, max_members=MAX_ARCHIVE_MEMBERS
        )
    except tarfile.TarError as err:
        raise ContractError(f"unreadable archive {archive.name}: {err}") from None
    finally:
        proc.stdout.close()
        if proc.wait(timeout=60) not in (0, None):
            raise ContractError(f"could not decompress {archive.name}")


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
            self._lines.append(line.rstrip("\n")[:LIVE_LINE_CHARS])

    def flush(self) -> None:
        with self._lock:
            text = "\n".join(self._lines) + ("\n" if self._lines else "")
        try:
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(self._path)
        except OSError as err:  # a slow or full mount must not kill the run
            print(f"live.log flush failed: {err}", file=sys.stderr)
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


class CappedLog:
    """A text sink that stops writing after `cap` characters (one marker line instead)."""

    def __init__(self, sink: IO[str], cap: int) -> None:
        self._sink = sink
        self._left = cap
        self._lock = threading.Lock()

    def write(self, text: str) -> None:
        with self._lock:
            if self._left <= 0:
                return
            if len(text) > self._left:
                text = text[: self._left] + "\n[log truncated]\n"
                self._left = 0
            else:
                self._left -= len(text)
            self._sink.write(text)

    def flush(self) -> None:
        self._sink.flush()


def _pump(stream: Any, sink: Any, live: LiveLog) -> None:
    for line in iter(lambda: stream.readline(READ_LINE_CHARS), ""):
        sink.write(line)
        sink.flush()
        live.add(line)


def _kill_group(proc: subprocess.Popen[str], sig: int) -> None:
    if hasattr(os, "killpg"):  # POSIX: the seed and everything it spawned
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, sig)
    elif proc.poll() is None:
        proc.kill() if sig != signal.SIGTERM else proc.terminate()


def run_seed(
    argv: Sequence[str], *, env: dict[str, str], cwd: Path, budget_s: float, kill_grace_s: float,
    stdout: Any, stderr: Any, live: LiveLog,
) -> tuple[int, bool]:  # fmt: skip
    """Run one seed in its own process group; returns (exit code, timed out)."""
    proc = subprocess.Popen(
        list(argv),
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        start_new_session=True,
    )
    pumps = [
        threading.Thread(target=_pump, args=(proc.stdout, stdout, live), daemon=True),
        threading.Thread(target=_pump, args=(proc.stderr, stderr, live), daemon=True),
    ]
    for t in pumps:
        t.start()
    timed_out = False
    try:
        try:
            code = proc.wait(timeout=max(budget_s, 0.0))
        except subprocess.TimeoutExpired:
            _kill_group(proc, signal.SIGTERM)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=kill_grace_s)
            code, timed_out = -1, True
    finally:  # leftovers (DataLoader workers, daemons) never outlive their seed
        _kill_group(proc, getattr(signal, "SIGKILL", signal.SIGTERM))
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        for t in pumps:
            t.join(timeout=5)
    return code, timed_out


def _read_metric(path: Path, key: str) -> float | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        value = data[key]
    except (OSError, ValueError, RecursionError, KeyError, TypeError):
        return None
    if type(value) not in (int, float):  # no bools, no numeric strings
        return None
    return float(value) if math.isfinite(value) else None


def _child_env(
    work: Path, overlay: Path, repo: Path, workspace: Path, data: Path
) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if PASSTHROUGH_ENV.fullmatch(k)}  # CUDA visibility
    env.update(
        {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(work / "home"),
            "PYTHONPATH": os.pathsep.join(str(p) for p in (overlay, repo, workspace)),
            "PYTHONUNBUFFERED": "1",
            "P2P_DATA_DIR": str(data),
            "HF_HUB_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
        }
    )
    return env


def run(
    manifest_path: Path,
    *,
    work: Path,
    extract: Callable[[Path, Path], None] = tar_extract,
    gpu_name: Callable[[], str] = nvidia_gpu_name,
    flush_interval_s: float = 10.0,
    on_flush: Callable[[str], None] | None = None,
    on_seed: Callable[[int], None] | None = None,
    budget_override_s: float | None = None,
    kill_grace_s: float = 30.0,
    log_cap_chars: int = LOG_CAP_CHARS,
) -> int:
    started = time.monotonic()
    try:
        manifest = validate(json.loads(manifest_path.read_text(encoding="utf-8")))
    except (OSError, ValueError, RecursionError, ContractError) as err:
        print(f"CONTRACT ERROR: {err}", file=sys.stderr)
        return EXIT_CONTRACT
    paths = manifest["paths"]
    results = Path(paths["results"])
    results.mkdir(parents=True, exist_ok=True)
    metric, relative = manifest["metric"], manifest["claim_type"] == "RELATIVE"
    per_seed: list[dict[str, Any]] = []
    baseline: list[dict[str, Any]] = []
    gpu = "unknown"

    def write_results(status: int) -> None:
        done = {s["seed"] for s in per_seed}
        missing = [s for s in manifest["seeds"] if s not in done]
        report = {
            "schema": "p2p.results/v1",
            "run_id": manifest["run_id"],
            "attempt": manifest["attempt"],
            "claim_ref": manifest["claim_ref"],
            "claim_type": manifest["claim_type"],
            "metric": metric,
            "higher_is_better": manifest["higher_is_better"],
            "scale": manifest["scale"],
            "per_seed": per_seed + [{"seed": s, "value": None, "ok": False} for s in missing],
            "baseline": (
                {
                    "name": manifest["baseline_name"],
                    "per_seed": baseline + [{"seed": s, "value": None} for s in missing],
                }
                if relative
                else None
            ),
            "wall_clock_s": round(time.monotonic() - started, 3),
            "gpu": gpu,
            "exit_reason": REASONS[status],
        }
        tmp = results / "results.json.tmp"
        tmp.write_text(json.dumps(report, indent=1), encoding="utf-8")
        tmp.replace(results / "results.json")

    workspace = Path(paths["workspace"])
    script = workspace / manifest["entrypoint"].removeprefix("workspace/")
    overlay, repo, data = work / "overlay", work / "repo", work / "data"
    try:
        if not script.is_file():
            raise ContractError("entrypoint not found in the workspace")
        if not Path(paths["overlay"]).is_file():
            raise ContractError("overlay archive missing")
        extract(Path(paths["overlay"]), overlay)
        if paths["repo"]:
            if not Path(paths["repo"]).is_file():
                raise ContractError("repository archive missing")
            extract(Path(paths["repo"]), repo)
        data_dir = Path(paths["data_dir"])
        if data_dir.is_dir():
            for archive in sorted(data_dir.glob("*.tar.zst")):
                extract(archive, data / archive.name.removesuffix(".tar.zst"))
    except (ContractError, OSError, subprocess.SubprocessError) as err:
        print(f"CONTRACT ERROR: {err}", file=sys.stderr)
        write_results(EXIT_CONTRACT)
        return EXIT_CONTRACT

    run_dir, logs, home = work / "run", work / "logs", work / "home"
    for directory in (run_dir, logs, home):
        directory.mkdir(parents=True, exist_ok=True)
    env = _child_env(work, overlay, repo, workspace, data)
    gpu = gpu_name()
    budget = float(
        budget_override_s if budget_override_s is not None else manifest["budget_seconds"]
    )
    status = EXIT_OK
    # Logs go to local disk while running (the mount is slow) and are copied at the end.
    with (
        (logs / "stdout.log").open("w", encoding="utf-8") as out_file,
        (logs / "stderr.log").open("w", encoding="utf-8") as err_file,
        LiveLog(results / "live.log", flush_interval_s, on_flush) as live,
    ):
        out, err = CappedLog(out_file, log_cap_chars), CappedLog(err_file, log_cap_chars)
        try:
            for seed in manifest["seeds"]:
                remaining = budget - (time.monotonic() - started)
                seed_dir = results / "seeds" / str(seed)
                if remaining <= 0:
                    status = EXIT_BUDGET
                    break
                argv = [
                    sys.executable, str(script), "--seed", str(seed), "--out", str(seed_dir),
                    *manifest["args"],
                ]  # fmt: skip
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
                base = None
                if relative and value is not None:
                    base = _read_metric(seed_dir / "metrics.json", f"baseline/{metric}")
                baseline.append({"seed": seed, "value": base})
                write_results(status)
                if on_seed is not None:
                    on_seed(len(per_seed))
                if timed_out:
                    break
        except Terminated:
            status = EXIT_BUDGET
            print("TERMINATED: writing partial results", file=sys.stderr)
    if status == EXIT_OK and len(per_seed) < len(manifest["seeds"]):
        status = EXIT_BUDGET
    write_results(status)
    for name in ("stdout.log", "stderr.log"):
        shutil.copyfile(logs / name, results / name)
    (results / "env.json").write_text(
        json.dumps({"python": sys.version, "image_digest": manifest["image_digest"], "gpu": gpu}),
        encoding="utf-8",
    )
    shutil.rmtree(home, ignore_errors=True)
    return status


def _on_sigterm(signum: int, frame: object) -> None:  # pragma: no cover - exercised in the image
    raise Terminated


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - exercised in the image
    parser = argparse.ArgumentParser(description="Paper2Proof runner entrypoint")
    parser.add_argument("--manifest", default="/etc/p2p/manifest.json")
    parser.add_argument("--work", default="/work")
    parser.add_argument("--smoke-manifest", default="/etc/p2p/smoke.json")
    args = parser.parse_args(argv)
    signal.signal(signal.SIGTERM, _on_sigterm)
    if Path(args.smoke_manifest).is_file():  # CPU smoke job (same image, execution-contracts §8)
        import smoke

        return smoke.run(Path(args.smoke_manifest), mnt=Path("/mnt/run"), work=Path(args.work))
    return run(Path(args.manifest), work=Path(args.work))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

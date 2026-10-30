"""Runner entrypoint for EXEC jobs (docs/specs/execution-contracts.md §7). Standard library only.

1. Read /etc/p2p/manifest.json and validate it (exit 30 on any contract problem).
2. Extract the workspace, overlay, repository and datasets to local disk under /work (no
   links, no escapes, size and member caps), each checked against its manifest digest: nothing
   else on the shared bucket is run or imported.
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
import functools
import hashlib
import importlib.metadata
import io
import json
import math
import os
import re
import shutil
import signal
import stat
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
MAX_WORKSPACE_BYTES = 16 * 1024 * 1024
MAX_TAIL_BYTES = 1 << 20  # after the tar end marker (legitimate padding is a few KiB)
ENTRYPOINT = re.compile(r"^workspace/[A-Za-z0-9_./-]+\.py$")
RUN_ID = re.compile(r"^r_[0-9a-z]{26}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
MANIFEST_KEYS = {
    "schema", "run_id", "attempt", "image_digest", "entrypoint", "args", "seeds", "budget_seconds",
    "paths", "claims", "scale", "archives", "workspace_sha256",
}  # fmt: skip
CLAIM_KEYS = {
    "claim_ref", "claim_type", "metric", "higher_is_better", "metric_key", "baseline_name",
    "baseline_key",
}  # fmt: skip
METRIC_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]{0,63}$")
MAX_CLAIMS = 5
SHA256 = re.compile(r"^[0-9a-f]{64}$")
PATH_KEYS = {"overlay", "repo", "data_dir", "workspace", "cache", "results"}
SCALE_KEYS = {"full", "fraction_of_data", "epochs", "paper_epochs"}
ARCHIVE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,200}$")
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


def _runner_flag(arg: str) -> bool:
    """--seed, --out and --smoke belong to the runner (argparse also accepts prefixes)."""
    if arg.startswith("--"):
        name = arg[2:].split("=", 1)[0]
        return bool(name) and any(f.startswith(name) for f in ("seed", "out", "smoke"))
    if arg.startswith("@"):  # argparse fromfile_prefix_chars
        return True
    return bool(re.match(r"-[A-Za-z]*[so]", arg))  # -s, -o and clusters like -vs0


def _short_str(value: Any, limit: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit


def _metric_key(value: Any) -> bool:
    return isinstance(value, str) and bool(METRIC_KEY.fullmatch(value))


def _validate(manifest: Any) -> dict[str, Any]:
    def need(ok: bool, what: str) -> None:
        if not ok:
            raise ContractError(f"invalid manifest: {what}")

    need(isinstance(manifest, dict) and set(manifest) == MANIFEST_KEYS, "keys")
    m: dict[str, Any] = manifest
    need(m["schema"] == "p2p.manifest/v2", "schema")
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
    need(not any(_runner_flag(a) for a in args), "args set a runner flag")
    claims = m["claims"]
    need(isinstance(claims, list) and 1 <= len(claims) <= MAX_CLAIMS, "claims")
    for claim in claims:
        need(isinstance(claim, dict) and set(claim) == CLAIM_KEYS, "claims keys")
        need(_short_str(claim["claim_ref"], 64), "claim_ref")
        need(_short_str(claim["metric"], 64), "metric")
        need(isinstance(claim["higher_is_better"], bool), "higher_is_better")
        need(claim["claim_type"] in ("ABSOLUTE", "RELATIVE"), "claim_type")
        need(_metric_key(claim["metric_key"]), "metric_key")
        if claim["claim_type"] == "RELATIVE":
            need(_short_str(claim["baseline_name"], 128), "baseline_name")
            need(_metric_key(claim["baseline_key"]), "baseline_key")
            need(claim["baseline_key"] != claim["metric_key"], "baseline_key is the metric_key")
        else:
            need(claim["baseline_name"] is None and claim["baseline_key"] is None, "baseline")
    refs = [c["claim_ref"] for c in claims]
    need(len(set(refs)) == len(refs), "claim_ref must be unique")
    scale = m["scale"]
    need(isinstance(scale, dict) and set(scale) <= SCALE_KEYS, "scale")
    need(isinstance(scale.get("full"), bool), "scale.full")
    archives = m["archives"]
    need(isinstance(archives, dict) and set(archives) == {"overlay", "repo", "data"}, "archives")
    need(
        isinstance(archives["overlay"], str) and bool(SHA256.fullmatch(archives["overlay"])),
        "overlay",
    )
    repo = archives["repo"]
    need(repo is None or (isinstance(repo, str) and bool(SHA256.fullmatch(repo))), "archives.repo")
    data = archives["data"]
    need(isinstance(data, list) and len(data) <= 3, "archives.data")
    for entry in data:
        need(isinstance(entry, dict) and set(entry) == {"name", "sha256"}, "archives.data")
        name, digest = entry["name"], entry["sha256"]
        need(
            isinstance(name, str) and bool(ARCHIVE_NAME.fullmatch(name)) and set(name) != {"."},
            "archives.data.name",
        )
        need(isinstance(digest, str) and bool(SHA256.fullmatch(digest)), "archives.data.sha256")
    workspace = m["workspace_sha256"]
    need(isinstance(workspace, str) and bool(SHA256.fullmatch(workspace)), "workspace_sha256")
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


def _feed(archive: Path, sink: IO[bytes], digest: Any) -> None:
    """Read the archive once: every byte is hashed and handed to the decompressor."""
    try:
        with archive.open("rb") as source:
            for chunk in iter(lambda: source.read(1 << 20), b""):
                digest.update(chunk)
                sink.write(chunk)
    except (BrokenPipeError, ValueError):
        pass  # the extractor stopped early; its own error explains why
    finally:
        with contextlib.suppress(OSError):
            sink.close()


def tar_extract(
    archive: Path, dest: Path, sha256: str, *, decompress: Sequence[str] = ("zstd", "-dc")
) -> None:
    """Extract while hashing the very bytes read (no window for a swapped file): a digest
    other than the manifest's discards everything extracted."""
    proc = subprocess.Popen(list(decompress), stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    if proc.stdout is None or proc.stdin is None:
        raise ContractError("the decompressor produced no stream")
    digest = hashlib.sha256()
    feeder = threading.Thread(target=_feed, args=(archive, proc.stdin, digest), daemon=True)
    feeder.start()
    try:
        extract_stream(
            proc.stdout, dest, max_bytes=MAX_ARCHIVE_BYTES, max_members=MAX_ARCHIVE_MEMBERS
        )
        drained = 0  # the digest must cover the whole file, but a tail is never kept
        for chunk in iter(lambda: proc.stdout.read(1 << 16), b""):
            drained += len(chunk)
            if drained > MAX_TAIL_BYTES:
                raise ContractError(f"{archive.name} has data after the end of its tar")
    except tarfile.TarError as err:
        proc.kill()
        shutil.rmtree(dest, ignore_errors=True)
        raise ContractError(f"unreadable archive {archive.name}: {err}") from None
    except BaseException:
        proc.kill()  # the real reason is the exception in flight, not the decompressor's status
        shutil.rmtree(dest, ignore_errors=True)
        raise
    finally:
        feeder.join(timeout=60)
        proc.stdout.close()
        status = proc.wait(timeout=60)
    if status != 0:
        shutil.rmtree(dest, ignore_errors=True)
        raise ContractError(f"could not decompress {archive.name}")
    if digest.hexdigest() != sha256:
        shutil.rmtree(dest, ignore_errors=True)
        raise ContractError(f"{archive.name} does not match its manifest digest")


def extract_workspace(archive: Path, dest: Path, digest: str) -> None:
    """The Coder's workspace: a small plain tar read once, hashed, then extracted."""
    if not archive.is_file() or archive.stat().st_size > MAX_WORKSPACE_BYTES:
        raise ContractError("workspace archive missing or too large")
    with archive.open("rb") as handle:
        data = handle.read(MAX_WORKSPACE_BYTES + 1)
    if len(data) > MAX_WORKSPACE_BYTES:
        raise ContractError("workspace archive too large")
    if hashlib.sha256(data).hexdigest() != digest:
        raise ContractError("workspace archive does not match the manifest digest")
    shutil.rmtree(dest, ignore_errors=True)
    try:
        extract_stream(io.BytesIO(data), dest, max_bytes=MAX_WORKSPACE_BYTES, max_members=1000)
    except tarfile.TarError as err:
        raise ContractError(f"unreadable workspace archive: {err}") from None


def _clear(path: Path) -> None:
    """Remove what an earlier job may have left at `path` (a link goes, not its target)."""
    if path.is_symlink():
        path.unlink()
    else:
        shutil.rmtree(path, ignore_errors=True)


@functools.cache
def _nvidia_query() -> tuple[str, str | None]:  # pragma: no cover - exercised in the GPU image
    """GPU name and driver in one nvidia-smi call (each call can take seconds on a cold node)."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=20, check=True,
        )  # fmt: skip
        name, _, driver = out.stdout.strip().splitlines()[0].rpartition(",")
        return (name.strip()[:128] or "unknown"), (driver.strip()[:64] or None)
    except (OSError, subprocess.SubprocessError, IndexError):
        return "cpu", None


def nvidia_gpu_name() -> str:  # pragma: no cover - exercised in the GPU image
    return _nvidia_query()[0]


def nvidia_driver_version() -> str | None:  # pragma: no cover - exercised in the GPU image
    return _nvidia_query()[1]


MAX_PACKAGES_BYTES = 128 * 1024  # env.json must stay under the bundle's 256 KiB per-file cap
MAX_DISTRIBUTIONS = 20_000  # an overlay could ship any number of *.dist-info directories
MAX_METADATA_BYTES = 1 << 20
PACKAGE_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?")  # PEP 508
PACKAGE_VERSION = re.compile(r"[A-Za-z0-9.+!_-]{1,50}")  # PEP 440 and local versions


def _metadata_too_big(dist: importlib.metadata.Distribution) -> bool:
    """Path distributions only (the only kind on these paths): METADATA is never read whole
    when it is over the cap. `_path` is the dist-info directory."""
    base = getattr(dist, "_path", None)
    if not isinstance(base, Path):
        return False
    for name in ("METADATA", "PKG-INFO"):
        try:
            if (base / name).stat().st_size > MAX_METADATA_BYTES:
                return True
        except OSError:
            continue
    return False


def installed_packages(
    paths: Sequence[Path], *, max_bytes: int = MAX_PACKAGES_BYTES
) -> tuple[list[str], bool]:
    """`name==version` of every distribution on `paths`, the first found winning (as for an
    import), sorted; metadata is only read, never imported. Cut to `max_bytes` as written in
    env.json (the flag says so). A diagnostic: any failure gives an empty list, never an
    error (Terminated, a BaseException, still stops the job)."""
    seen: dict[str, str] = {}
    cut = False
    try:
        for index, dist in enumerate(
            importlib.metadata.distributions(path=[str(p) for p in paths])
        ):
            if index >= MAX_DISTRIBUTIONS:
                cut = True
                break
            if _metadata_too_big(dist):
                continue
            try:
                meta = dist.metadata  # read and parsed once
                name, version = meta["Name"], meta["Version"]
            except (OSError, ValueError, KeyError):  # unreadable metadata: skip it
                continue
            # written by wheel authors and signed in the bundle: plain names and versions only
            if not (
                isinstance(name, str)
                and isinstance(version, str)
                and PACKAGE_NAME.fullmatch(name)
                and PACKAGE_VERSION.fullmatch(version)
            ):
                continue
            key = re.sub(r"[-_.]+", "-", name).lower()
            if key not in seen:
                seen[key] = f"{name}=={version}"
    except Exception as err:  # the inventory must never fail the job
        print(f"package inventory failed: {type(err).__name__}", file=sys.stderr)
        return [], False
    kept: list[str] = []
    size = 0
    for entry in sorted(seen.values(), key=str.lower):
        size += len(json.dumps(entry)) + 1  # as written: quoted, escaped, with a separator
        if size > max_bytes:
            return kept, True
        kept.append(entry)
    return kept, cut


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


MAX_METRICS_BYTES = 64 * 1024


def _read_metrics(path: Path) -> dict[str, Any] | None:
    """The seed's metrics.json, read once for every claim; None if missing or unusable. The
    untrusted script owns the file: a symlink, a FIFO or a device is never followed, opened
    blocking or read past the cap (a link to /dev/zero would read forever)."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_METRICS_BYTES:
            return None
        chunks: list[bytes] = []
        size = 0
        while size <= MAX_METRICS_BYTES:  # the file may grow after fstat: bound the read
            chunk = os.read(fd, MAX_METRICS_BYTES + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if size > MAX_METRICS_BYTES:
            return None
        data = json.loads(b"".join(chunks).decode("utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    finally:
        os.close(fd)
    return data if isinstance(data, dict) else None


def _metric(metrics: dict[str, Any] | None, key: str) -> float | None:
    value = metrics.get(key) if metrics is not None else None
    if isinstance(value, bool) or not isinstance(value, int | float):  # no strings, no nesting
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


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
            "PYTHONIOENCODING": "utf-8",
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
    extract: Callable[[Path, Path, str], None] = tar_extract,
    gpu_name: Callable[[], str] = nvidia_gpu_name,
    gpu_driver: Callable[[], str | None] = nvidia_driver_version,
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
    claims = manifest["claims"]
    seeds_done: list[int] = []
    values: dict[str, list[dict[str, Any]]] = {c["claim_ref"]: [] for c in claims}
    baselines: dict[str, list[dict[str, Any]]] = {c["claim_ref"]: [] for c in claims}
    gpu = "unknown"

    def padded(entries: list[dict[str, Any]], empty: dict[str, Any]) -> list[dict[str, Any]]:
        """Each list padded from its own seeds: a SIGTERM between two claims of one seed
        leaves the lists uneven, and a seed must never appear twice."""
        done = {e["seed"] for e in entries}
        return entries + [{"seed": s, **empty} for s in manifest["seeds"] if s not in done]

    def claim_results(claim: dict[str, Any]) -> dict[str, Any]:
        ref, relative = claim["claim_ref"], claim["claim_type"] == "RELATIVE"
        return {
            "claim_ref": ref,
            "claim_type": claim["claim_type"],
            "metric": claim["metric"],
            "metric_key": claim["metric_key"],
            "higher_is_better": claim["higher_is_better"],
            "per_seed": padded(values[ref], {"value": None, "ok": False}),
            "baseline": (
                {
                    "name": claim["baseline_name"],
                    "key": claim["baseline_key"],
                    "per_seed": padded(baselines[ref], {"value": None}),
                }
                if relative
                else None
            ),
        }

    def write_results(status: int) -> None:
        report = {
            "schema": "p2p.results/v2",
            "run_id": manifest["run_id"],
            "attempt": manifest["attempt"],
            "scale": manifest["scale"],
            "claims": [claim_results(c) for c in claims],
            "wall_clock_s": round(time.monotonic() - started, 3),
            "gpu": gpu,
            "exit_reason": REASONS[status],
        }
        tmp = results / "results.json.tmp"
        tmp.write_text(json.dumps(report, indent=1), encoding="utf-8")
        tmp.replace(results / "results.json")

    workspace = work / "workspace"
    script = workspace / manifest["entrypoint"].removeprefix("workspace/")
    overlay, repo, data = work / "overlay", work / "repo", work / "data"
    try:
        extract_workspace(Path(paths["workspace"]), workspace, manifest["workspace_sha256"])
        if not script.is_file():
            raise ContractError("entrypoint not found in the workspace")
        digests = manifest["archives"]
        if not Path(paths["overlay"]).is_file():
            raise ContractError("overlay archive missing")
        extract(Path(paths["overlay"]), overlay, digests["overlay"])
        if paths["repo"]:
            if not Path(paths["repo"]).is_file() or digests["repo"] is None:
                raise ContractError("repository archive or its digest missing")
            extract(Path(paths["repo"]), repo, digests["repo"])
        for entry in digests["data"]:  # exactly the listed archives: nothing else is trusted
            archive = Path(paths["data_dir"]) / f"{entry['name']}.tar.zst"
            if not archive.is_file():
                raise ContractError(f"data archive {entry['name']} missing")
            extract(archive, data / entry["name"], entry["sha256"])
        gpu = gpu_name()  # nvidia-smi can take seconds: SIGTERM here still writes results
        driver = gpu_driver()
        # read before any untrusted code runs: a script could plant metadata in the overlay
        packages, truncated = installed_packages([overlay, *(Path(p) for p in sys.path if p)])
    except (ContractError, OSError, subprocess.SubprocessError) as err:
        print(f"CONTRACT ERROR: {err}", file=sys.stderr)
        write_results(EXIT_CONTRACT)
        return EXIT_CONTRACT
    except Terminated:
        print("TERMINATED during setup: writing results", file=sys.stderr)
        write_results(EXIT_BUDGET)
        return EXIT_BUDGET

    run_dir, logs, home = work / "run", work / "logs", work / "home"
    for directory in (run_dir, logs, home):
        directory.mkdir(parents=True, exist_ok=True)
    env = _child_env(work, overlay, repo, workspace, data)
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
                _clear(seed_dir)  # only this seed's own output counts
                argv = [
                    sys.executable, str(script), "--seed", str(seed), "--out", str(seed_dir),
                    *manifest["args"],
                ]  # fmt: skip
                code, timed_out = run_seed(
                    argv, env=env, cwd=run_dir, budget_s=remaining, kill_grace_s=kill_grace_s,
                    stdout=out, stderr=err, live=live,
                )  # fmt: skip
                metrics = _read_metrics(seed_dir / "metrics.json") if code == 0 else None
                measured = False
                for claim in claims:
                    ref = claim["claim_ref"]
                    value = _metric(metrics, claim["metric_key"])
                    values[ref].append({"seed": seed, "value": value, "ok": value is not None})
                    measured = measured or value is not None
                    if claim["claim_type"] == "RELATIVE":
                        base = (
                            _metric(metrics, claim["baseline_key"]) if value is not None else None
                        )
                        baselines[ref].append({"seed": seed, "value": base})
                seeds_done.append(seed)
                if timed_out:
                    status = EXIT_BUDGET
                elif not measured and status == EXIT_OK:  # a claim missing alone is its own
                    status = EXIT_EXPERIMENT
                write_results(EXIT_BUDGET)  # not done yet: a stopped job never shows an ending
                if on_seed is not None:
                    on_seed(len(seeds_done))
                if timed_out:
                    break
        except Terminated:
            status = EXIT_BUDGET
            print("TERMINATED: writing partial results", file=sys.stderr)
    if status == EXIT_OK and len(seeds_done) < len(manifest["seeds"]):
        status = EXIT_BUDGET
    write_results(status)
    for name in ("stdout.log", "stderr.log"):
        shutil.copyfile(logs / name, results / name)
    (results / "env.json").write_text(
        json.dumps(
            {
                "python": sys.version,
                "image_digest": manifest["image_digest"],
                "gpu": gpu,
                "driver": driver,
                "packages": packages,
                "packages_truncated": truncated,
            }
        ),
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
        sys.path.insert(0, str(Path(__file__).resolve().parent))  # works under `python -I` too
        import smoke

        return smoke.run(Path(args.smoke_manifest), mnt=Path("/mnt/run"), work=Path(args.work))
    return run(Path(args.manifest), work=Path(args.work))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

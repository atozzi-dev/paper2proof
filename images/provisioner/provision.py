"""Provisioner: runs in the PROVISION job (CPU, allowlisted egress, no secrets).

Reads the manifest injected at /etc/p2p/provision.json (written by deterministic orchestrator
code), then:
- locks the extra packages with hashes against the runner image's constraints (wheels only,
  never sdists), checks every transitive dependency against the denylist and typosquat list,
  and installs only what the runner image doesn't already have into an overlay;
- shallow-clones the official repository (no hooks, no submodules, no LFS) and records its
  commit; nothing from the repository is executed;
- downloads datasets as parquet archives after checking their size, builds the smoke subset
  from the downloaded files (no dataset loading scripts, ever);
- writes everything to /mnt/run/staging/ (and verified overlays to the shared cache).

Every external command is an argv list built here (never a shell string), with a minimal
environment and a timeout bounded by the job's deadline. Standard library only, except
huggingface_hub and pyarrow imported lazily for datasets.
Usage: python provision.py [--manifest PATH] [--staging DIR] [--cache DIR] [--work DIR]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple, Protocol

SCHEMA = "p2p.provision/v1"
RUN_ID = re.compile(r"^r_[0-9a-z]{26}$")
_OP = r"(?:===|==|~=|!=|<=|>=|<|>)"
_VER = r"[A-Za-z0-9.*+!_-]{1,40}"
PACKAGE = re.compile(
    rf"^[A-Za-z0-9](?:[A-Za-z0-9._-]{{0,98}}[A-Za-z0-9])?(?:{_OP}{_VER}(?:,{_OP}{_VER})*)?$"
)
REPO_URL = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
GIT_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
HF_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}(?:/[A-Za-z0-9][A-Za-z0-9_.-]{0,95})?$")
SPLIT = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
SALT = re.compile(r"^[A-Za-z0-9_.:+-]{1,300}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
LOCK_NAME = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==", re.MULTILINE)
MAX_PACKAGES = 30
MAX_DATASETS = 3
MAX_DATASET_BYTES = 20 * 2**30
MAX_SMOKE_ROWS = 100_000
MAX_SMOKE_BYTES = 50 * 2**20
MAX_REPO_BYTES = 2**30
MAX_REQUIREMENT_FILE = 64 * 1024
MAX_REQUIREMENT_FILES = 10
MAX_REQUIREMENT_CHARS = 128 * 1024  # all files together, in the report
MAX_TRUSTED_OVERLAYS = 300
SNAPSHOT_SUFFIXES = (".py", ".md", ".txt", ".rst", ".yaml", ".yml", ".json", ".cfg", ".toml",
                     ".ini", ".sh", ".ipynb")  # fmt: skip
SNAPSHOT_PART = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}$")
MAX_SNAPSHOT_FILE = 200 * 1024
MAX_SNAPSHOT_FILES = 400
MAX_SNAPSHOT_BYTES = int(3.4 * 1024 * 1024)  # encoded; the control plane reads <= 4 MiB
COMMAND_TIMEOUT_S = 900
DEFAULT_DEADLINE_S = 1080  # 18 of the 20 watchdog minutes: leave time to write the error file
PYTHON_VERSION = "3.12"
PYTHON_PLATFORM = "x86_64-manylinux_2_28"
PYPI_INDEX = "https://pypi.org/simple"
REQUIREMENT_FILES = re.compile(
    r"^(requirements[\w.-]*\.txt|pyproject\.toml|setup\.cfg|environment\.ya?ml)$"
)
ALLOWED_ENV = frozenset(
    {"PATH", "HOME", "LANG", "GIT_TERMINAL_PROMPT", "GIT_LFS_SKIP_SMUDGE", "GIT_CONFIG_NOSYSTEM",
     "UV_CACHE_DIR", "UV_PYTHON_DOWNLOADS", "UV_NO_CONFIG", "HF_HOME", "HF_HUB_DISABLE_TELEMETRY",
     "PYTHONDONTWRITEBYTECODE"}
)  # fmt: skip


class ProvisionError(Exception):
    """A clear, user-facing reason the environment could not be prepared."""


@dataclass(frozen=True)
class Dataset:
    id: str
    split: str | None
    max_bytes: int


@dataclass(frozen=True)
class Manifest:
    run_id: str
    packages: list[str]
    repo_url: str | None
    repo_ref: str | None
    datasets: list[Dataset] = field(default_factory=list)
    smoke_dataset: str | None = None
    smoke_rows: int = 0
    cache_salt: str = ""
    denylist: frozenset[str] = frozenset()
    popular: frozenset[str] = frozenset()
    trusted_overlays: dict[str, str] = field(default_factory=dict)  # cache key -> sha256
    deadline_unix: float | None = None  # set by the control plane from the job's submission


@dataclass(frozen=True)
class Paths:
    staging: Path
    cache: Path
    work: Path
    constraints: Path = Path("/opt/p2p/runner-constraints.txt")  # baked into the image (A-06)


class Runner(Protocol):
    def __call__(
        self, argv: Sequence[str], *, env: dict[str, str], timeout: float, cwd: Path | None = None
    ) -> str: ...


Fetcher = Callable[[str, "str | None", Path, int], None]  # download a split's parquet files
SmokeBuilder = Callable[[Path, Path, int], None]  # first N rows of downloaded parquet files


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def levenshtein(a: str, b: str, cap: int = 3) -> int:
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def _fail(message: str) -> ProvisionError:
    return ProvisionError(message)


def validate_manifest(data: Any) -> Manifest:
    """Defence in depth: the orchestrator validated these already (package policy)."""
    try:
        return _validate(data)
    except ProvisionError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError) as err:
        raise _fail(f"malformed manifest ({type(err).__name__})") from None


def _validate(data: Any) -> Manifest:
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise _fail("unsupported manifest schema")
    run_id = str(data.get("run_id", ""))
    if not RUN_ID.fullmatch(run_id):
        raise _fail("invalid run_id")
    packages = [str(p).replace(" ", "") for p in data.get("packages") or []]
    if len(packages) > MAX_PACKAGES or not all(PACKAGE.fullmatch(p) for p in packages):
        raise _fail("invalid package list (plain names and version specifiers only)")
    repo = data.get("repo")
    repo_url = repo_ref = None
    if repo is not None:
        if not isinstance(repo, dict):
            raise _fail("invalid repo")
        repo_url = str(repo.get("url", ""))
        raw_ref = repo.get("ref")
        repo_ref = None if raw_ref is None else str(raw_ref)
        if not REPO_URL.fullmatch(repo_url):
            raise _fail("repository must be a plain https://github.com/<owner>/<repo> URL")
        if repo_ref is not None and (not GIT_REF.fullmatch(repo_ref) or ".." in repo_ref):
            raise _fail("invalid git ref")
    raw_datasets = data.get("datasets") or []
    if len(raw_datasets) > MAX_DATASETS:
        raise _fail(f"at most {MAX_DATASETS} datasets")
    datasets = []
    for entry in raw_datasets:
        if not isinstance(entry, dict) or entry.get("kind") != "hf":
            raise _fail("only Hugging Face datasets are supported")
        if not HF_ID.fullmatch(str(entry.get("id", ""))):
            raise _fail("invalid dataset id")
        split = entry.get("split")
        if split is not None and not SPLIT.fullmatch(str(split)):
            raise _fail("invalid dataset split (a plain split name, no slicing)")
        max_bytes = int(entry["max_bytes"])
        if not 0 < max_bytes <= MAX_DATASET_BYTES:
            raise _fail("invalid dataset size cap")
        datasets.append(Dataset(id=entry["id"], split=split, max_bytes=max_bytes))
    if sum(d.max_bytes for d in datasets) > MAX_DATASET_BYTES:
        raise _fail("datasets over the total size cap")
    smoke = data.get("smoke_subset")
    smoke_dataset, smoke_rows = None, 0
    if smoke is not None:
        smoke_dataset, smoke_rows = str(smoke.get("dataset", "")), int(smoke.get("rows", 0))
        if not HF_ID.fullmatch(smoke_dataset) or not 1 <= smoke_rows <= MAX_SMOKE_ROWS:
            raise _fail("invalid smoke subset")
    if data.get("hf_token_secret") is not None:
        raise _fail("gated datasets are not supported: jobs never receive tokens")
    salt = data.get("cache_salt")
    if not isinstance(salt, str) or not SALT.fullmatch(salt):
        raise _fail("invalid cache salt")
    trusted = data.get("trusted_overlays") or {}
    if not isinstance(trusted, dict) or len(trusted) > MAX_TRUSTED_OVERLAYS:
        raise _fail("invalid trusted overlays")
    for key, digest in trusted.items():
        if not (isinstance(key, str) and SHA256.fullmatch(key)):
            raise _fail("invalid trusted overlays")
        if not (isinstance(digest, str) and SHA256.fullmatch(digest)):
            raise _fail("invalid trusted overlays")
    deadline = data.get("deadline_unix")
    if deadline is not None and (
        isinstance(deadline, bool)
        or not isinstance(deadline, int | float)
        or not math.isfinite(deadline)
    ):
        raise _fail("invalid deadline")
    policy = data.get("policy") or {}
    return Manifest(
        run_id=run_id,
        packages=packages,
        repo_url=repo_url,
        repo_ref=repo_ref,
        datasets=datasets,
        smoke_dataset=smoke_dataset,
        smoke_rows=smoke_rows,
        cache_salt=salt,
        denylist=frozenset(normalize(str(n)) for n in policy.get("denylist", [])),
        popular=frozenset(normalize(str(n)) for n in policy.get("popular", [])),
        trusted_overlays=dict(trusted),
        deadline_unix=None if deadline is None else float(deadline),
    )


def build_env(paths: Paths) -> dict[str, str]:
    """Minimal environment: nothing inherited except PATH; caches on local disk, never on the
    shared mount (uv and huggingface trust their own cache metadata)."""
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(paths.work / "home"),
        "LANG": "C.UTF-8",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_LFS_SKIP_SMUDGE": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "UV_CACHE_DIR": str(paths.work / "uv-cache"),
        "UV_PYTHON_DOWNLOADS": "never",
        "UV_NO_CONFIG": "1",
        "HF_HOME": str(paths.work / "hf"),
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def subprocess_runner(  # pragma: no cover - exercised in the image
    argv: Sequence[str], *, env: dict[str, str], timeout: float, cwd: Path | None = None
) -> str:
    result = subprocess.run(
        list(argv), env=env, cwd=cwd, check=True, capture_output=True, text=True, timeout=timeout
    )
    return result.stdout


class Clock:
    """Each command gets min(COMMAND_TIMEOUT_S, time left before the deadline). The deadline
    is wall-clock, set by the control plane from the job's submission (queueing and image pull
    included), so the error file is written before the watchdog cancels the job."""

    def __init__(self, deadline_unix: float) -> None:
        self.end = deadline_unix

    def left(self) -> float:
        return self.end - time.time()

    def timeout(self) -> float:
        left = self.left()
        if left <= 1:
            raise _fail("provisioning deadline reached")
        return min(COMMAND_TIMEOUT_S, left)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _explain(err: subprocess.SubprocessError, what: str) -> ProvisionError:
    if isinstance(err, subprocess.TimeoutExpired):
        return _fail(f"timed out while {what}")
    if not isinstance(err, subprocess.CalledProcessError):
        return _fail(f"{what} failed")
    stderr = (err.stderr or "").lower()
    print(f"{what} stderr (tail):\n{(err.stderr or '')[-4000:]}", file=sys.stderr)  # no secrets
    if any(s in stderr for s in ("no wheel", "only-binary", "sdist", "no matching distribution")):
        return _fail(
            f"{what}: a requested package has no wheel for this platform (sdists are never built)"
        )
    if "no solution found" in stderr or "conflict" in stderr:
        return _fail(f"{what}: the requested versions conflict")
    return _fail(f"{what} failed (exit {err.returncode})")


def _pack(source: Path, out: Path, runner: Runner, env: dict[str, str], clock: Clock) -> str:
    """Pack on local disk first, then copy once to the destination (FUSE mounts are slow).
    Returns the sha256 of the local archive: never read back from the shared mount."""
    local = source.parent / f".{out.name}.local"
    runner(
        [
            "tar",
            "-I",
            "zstd -T0",
            "--exclude=.cache",
            "--sort=name",
            "--mtime=@0",
            "--owner=0",
            "--group=0",
            "--numeric-owner",
            "-cf",
            str(local),
            "-C",
            str(source),
            ".",
        ],
        env=env,
        timeout=clock.timeout(),
    )
    digest = _sha256(local)
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(local, out)
    local.unlink(missing_ok=True)
    return digest


def _lock_names(lock: str) -> list[str]:
    return [normalize(m.group(1)) for m in LOCK_NAME.finditer(lock)]


def _check_transitive(manifest: Manifest, lock: str) -> None:
    """Every resolved dependency, not only the requested names, passes denylist + typosquat."""
    requested = {normalize(re.split(_OP, p)[0]) for p in manifest.packages}
    for name in _lock_names(lock):
        if name in manifest.denylist:
            raise _fail(f"dependency {name} is not allowed")
        if name in requested or name in manifest.popular:
            continue
        close = [p for p in manifest.popular if levenshtein(name, p) <= 2]
        if close:
            raise _fail(f"dependency {name} looks like a typosquat of {sorted(close)[0]}")


def _filter_lock(lock: str, constraints_text: str) -> str:
    """Drop lock entries the runner image already provides (pinned in its constraints)."""
    provided = set(_lock_names(constraints_text))
    blocks = re.split(r"\n(?=[A-Za-z0-9])", lock.strip())
    keep = [
        b for b in blocks
        if b and (m := LOCK_NAME.match(b)) and normalize(m.group(1)) not in provided
    ]  # fmt: skip
    return "\n".join(keep) + ("\n" if keep else "")


def _publish_to_cache(overlay: Path, cached: Path, digest: str, run_id: str) -> None:
    """Atomic publish (temp names, rename once complete): survives kills and concurrent runs."""
    cached.parent.mkdir(parents=True, exist_ok=True)
    key = cached.name.removesuffix(".tar.zst")
    tmp = cached.with_name(f".tmp-{run_id}-{key}.tar.zst")
    tmp_sidecar = cached.with_name(f".tmp-{run_id}-{key}.sha256")
    shutil.copyfile(overlay, tmp)
    tmp_sidecar.write_text(digest, encoding="utf-8")
    tmp.replace(cached)
    tmp_sidecar.replace(cached.with_name(f"{key}.sha256"))


def lock_and_overlay(
    manifest: Manifest, paths: Paths, runner: Runner, env: dict[str, str], clock: Clock
) -> tuple[str, bool, str]:
    if not paths.constraints.is_file():  # fail closed: never build a full ML stack overlay
        raise _fail("runner constraints file missing from the provisioner image")
    constraints_text = paths.constraints.read_text(encoding="utf-8")
    work = paths.work / "overlay"
    work.mkdir(parents=True, exist_ok=True)
    requirements_in = paths.work / "requirements.in"
    requirements_in.write_text("".join(f"{p}\n" for p in manifest.packages), encoding="utf-8")
    lock = paths.staging / "lock.txt"
    if manifest.packages:
        try:
            runner(
                ["uv", "pip", "compile", "--no-config", "--index-url", PYPI_INDEX,
                 "--only-binary=:all:", "--generate-hashes", "--no-header",
                 "--python-version", PYTHON_VERSION, "--python-platform", PYTHON_PLATFORM,
                 "-c", str(paths.constraints), "-o", str(lock), str(requirements_in)],
                env=env,
                timeout=clock.timeout(),
            )  # fmt: skip
        except subprocess.SubprocessError as err:
            raise _explain(err, "locking packages") from None
    else:
        lock.write_text("", encoding="utf-8")
    full_lock = lock.read_text(encoding="utf-8")
    install_text = _filter_lock(full_lock, constraints_text)
    _check_transitive(manifest, install_text)  # what the image provides is trusted, not judged
    install_lock = paths.work / "lock.install.txt"
    install_lock.write_text(install_text, encoding="utf-8")

    constraints_sha = hashlib.sha256(constraints_text.encode()).hexdigest()
    salted = f"{install_text}\n{manifest.cache_salt}\n{constraints_sha}"
    key = hashlib.sha256(salted.encode()).hexdigest()
    cached = paths.cache / "overlays" / f"{key}.tar.zst"
    overlay = paths.staging / "overlay.tar.zst"
    expected = manifest.trusted_overlays.get(key)
    if expected is not None and cached.is_file():  # the digest comes from the control plane,
        local = paths.work / "overlay-cached.tar.zst"  # never from the shared bucket
        shutil.copyfile(cached, local)
        if _sha256(local) == expected:
            shutil.copyfile(local, overlay)
            return key, True, expected
    if install_lock.read_text(encoding="utf-8").strip():
        try:
            runner(
                ["uv", "pip", "install", "--no-config", "--index-url", PYPI_INDEX,
                 "--only-binary=:all:", "--require-hashes", "--no-deps",
                 "--python-version", PYTHON_VERSION, "--python-platform", PYTHON_PLATFORM,
                 "-r", str(install_lock), "--target", str(work)],
                env=env,
                timeout=clock.timeout(),
            )  # fmt: skip
        except subprocess.SubprocessError as err:
            raise _explain(err, "installing packages") from None
    digest = _pack(work, overlay, runner, env, clock)
    _publish_to_cache(overlay, cached, digest, manifest.run_id)
    return key, False, digest


def _size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file() and not p.is_symlink())


class RepoResult(NamedTuple):
    requirement_files: dict[str, str]
    commit: str | None
    snapshot_sha256: str | None
    archive_sha256: str | None


def clone_repo(
    manifest: Manifest, paths: Paths, runner: Runner, env: dict[str, str], clock: Clock
) -> RepoResult:
    """Shallow clone; nothing from the repository is executed. Returns the text of its
    requirement files (parsed by the control plane) and the resolved commit."""
    if manifest.repo_url is None:
        return RepoResult({}, None, None, None)
    dest = paths.work / "repo"
    branch = ["--branch", manifest.repo_ref] if manifest.repo_ref else []  # None: default branch
    try:
        runner(
            ["git", "-c", "protocol.file.allow=never", "-c", "core.hooksPath=/dev/null",
             "-c", "advice.detachedHead=false", "clone", "--depth", "1", "--single-branch",
             "--no-tags", *branch, "--", manifest.repo_url, str(dest)],
            env=env,
            timeout=clock.timeout(),
        )  # fmt: skip
        commit = runner(
            ["git", "-C", str(dest), "rev-parse", "HEAD"], env=env, timeout=clock.timeout()
        )
    except subprocess.TimeoutExpired:
        raise _fail("timed out while cloning the repository") from None
    except subprocess.CalledProcessError as err:
        raise _fail(f"cloning the repository failed (exit {err.returncode})") from None
    if _size(dest) > MAX_REPO_BYTES:
        raise _fail("the repository is larger than 1 GiB")
    files: dict[str, str] = {}
    budget = MAX_REQUIREMENT_CHARS  # the whole report must stay well under 512 KiB
    for path in sorted(dest.glob("*")):
        if len(files) >= MAX_REQUIREMENT_FILES:
            break
        if path.is_symlink() or not path.is_file() or not REQUIREMENT_FILES.match(path.name):
            continue
        if path.stat().st_size <= MAX_REQUIREMENT_FILE:
            text = path.read_text(encoding="utf-8", errors="replace")
            cost = len(json.dumps(text, ensure_ascii=False))  # as it lands in the report
            if cost > budget:
                break
            files[path.name] = text
            budget -= cost
    archive_sha = _pack(dest, paths.staging / "repo.tar.zst", runner, env, clock)
    snapshot_sha = _write_snapshot(dest, paths.staging / "repo_files.json")
    resolved = commit.strip()
    return RepoResult(
        files,
        resolved if re.fullmatch(r"[0-9a-f]{40}", resolved) else None,
        snapshot_sha,
        archive_sha,
    )


def _snapshot_order(rel: str) -> tuple[int, str]:
    """Sources and top-level docs first: assets and notebooks never push out `src/`."""
    name = rel.rsplit("/", 1)[-1]
    if (rel.count("/") == 0 and REQUIREMENT_FILES.match(name)) or name.lower().startswith("readme"):
        return 0, rel
    return (1 if name.endswith(".py") else 2), rel


def _write_snapshot(repo: Path, out: Path) -> str:
    """Readable text files of the clone for the Coder's read_file (the control plane never
    unpacks repository archives). Regular files only, plain path segments, a budget on the
    encoded size; written without following links. Returns the sha256 of the bytes written."""
    candidates = []
    for path in repo.rglob("*"):
        rel = path.relative_to(repo).as_posix()
        parts = rel.split("/")
        if parts[0] == ".git" or path.is_symlink() or not path.is_file() or len(parts) > 8:
            continue
        if not path.name.endswith(SNAPSHOT_SUFFIXES):
            continue
        if not all(SNAPSHOT_PART.fullmatch(p) for p in parts):
            continue
        if path.stat().st_size <= MAX_SNAPSHOT_FILE:
            candidates.append(rel)
    files: dict[str, str] = {}
    budget = MAX_SNAPSHOT_BYTES
    for rel in sorted(candidates, key=_snapshot_order):
        text = (repo / rel).read_text(encoding="utf-8", errors="replace")
        cost = (
            len(json.dumps(rel).encode()) + len(json.dumps(text, ensure_ascii=False).encode()) + 4
        )
        if cost > budget:
            continue
        files[rel] = text
        budget -= cost
        if len(files) >= MAX_SNAPSHOT_FILES:
            break
    data = json.dumps({"files": files}, ensure_ascii=False).encode("utf-8", "backslashreplace")
    out.unlink(missing_ok=True)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    return hashlib.sha256(data).hexdigest()


def split_files(names: Sequence[str], split: str) -> list[str]:
    """Parquet files of a split: a path segment named after it, or a file named
    `<split>.parquet` / `<split>-*.parquet` (never a substring: `test` is not `contest`)."""
    wanted = []
    for name in names:
        parts = name.split("/")
        if not name.endswith(".parquet"):
            continue
        file = parts[-1]
        if split in parts[:-1] or file == f"{split}.parquet" or file.startswith(f"{split}-"):
            wanted.append(name)
    return wanted


def hf_fetch(  # pragma: no cover - runs in the image
    dataset_id: str, split: str | None, dest: Path, max_bytes: int
) -> None:
    """Download only the split's parquet files, after checking their total size."""
    from huggingface_hub import HfApi, snapshot_download

    split_name = split or "train"
    info = HfApi().dataset_info(dataset_id, files_metadata=True)
    by_name = {s.rfilename: s for s in info.siblings or []}
    wanted = [by_name[n] for n in split_files(list(by_name), split_name)]
    if not wanted:
        raise _fail(f"dataset {dataset_id} has no parquet files for split {split_name}")
    total = sum(s.size or 0 for s in wanted)
    if total > max_bytes:
        raise _fail(f"dataset {dataset_id} split {split_name} is {total} bytes, over its cap")
    dest.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(dest).free < 3 * total:  # raw files + archive + headroom
        raise _fail("not enough local disk for the dataset")
    snapshot_download(
        repo_id=dataset_id,
        repo_type="dataset",
        local_dir=dest,
        allow_patterns=[s.rfilename for s in wanted],
    )


def parquet_smoke(source: Path, dest: Path, rows: int) -> None:  # pragma: no cover - in image
    """First `rows` rows of the downloaded parquet files: no dataset code is ever executed."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    dest.mkdir(parents=True, exist_ok=True)
    tables, taken = [], 0
    for path in sorted(source.rglob("*.parquet")):
        for batch in pq.ParquetFile(path).iter_batches(batch_size=rows - taken):
            tables.append(pa.Table.from_batches([batch]))
            taken += batch.num_rows
            break  # one batch per file is enough: never load a whole shard
        if taken >= rows:
            break
    if not tables:
        raise _fail("no parquet rows for the smoke subset")
    pq.write_table(pa.concat_tables(tables), dest / "smoke.parquet")


def fetch_datasets(
    manifest: Manifest,
    paths: Paths,
    runner: Runner,
    env: dict[str, str],
    clock: Clock,
    fetch: Fetcher,
    smoke: SmokeBuilder,
) -> list[dict[str, str]]:
    """Returns [{name, sha256}] for each archive written under staging/data/."""
    archives: list[dict[str, str]] = []
    names = []
    for dataset in manifest.datasets:
        clock.timeout()
        name = dataset.id.replace("/", "__")
        dest = paths.work / "data" / name
        fetch(dataset.id, dataset.split, dest, dataset.max_bytes)
        if _size(dest) > dataset.max_bytes:
            raise _fail(f"dataset {dataset.id} is larger than its {dataset.max_bytes}-byte cap")
        digest = _pack(dest, paths.staging / "data" / f"{name}.tar.zst", runner, env, clock)
        archives.append({"name": name, "sha256": digest})
        names.append(name)
    if manifest.smoke_dataset:
        source = paths.work / "data" / manifest.smoke_dataset.replace("/", "__")
        if not source.is_dir():
            raise _fail("the smoke subset must come from one of the plan's datasets")
        dest = paths.work / "smoke"
        smoke(source, dest, manifest.smoke_rows)
        if _size(dest) > MAX_SMOKE_BYTES:
            raise _fail("smoke subset over 50 MB: lower smoke_subset.rows")
        digest = _pack(dest, paths.staging / "data" / "smoke.tar.zst", runner, env, clock)
        archives.append({"name": "smoke", "sha256": digest})
    for name in names:  # packed (and the smoke subset built): free the local disk
        shutil.rmtree(paths.work / "data" / name, ignore_errors=True)
    return archives


def _clean_staging(staging: Path) -> None:
    """Outputs of an earlier attempt must never be mistaken for this one's. Smoke jobs share
    the bucket: a planted symlink is removed itself, never followed."""
    for name in (
        "provision_error.json",
        "provision_report.json",
        "overlay.tar.zst",
        "repo.tar.zst",
        "lock.txt",
        "repo_files.json",
    ):
        (staging / name).unlink(missing_ok=True)
    data = staging / "data"
    if data.is_symlink():
        data.unlink()
    else:
        shutil.rmtree(data, ignore_errors=True)


def provision(
    manifest: Manifest,
    paths: Paths,
    runner: Runner,
    fetch: Fetcher,
    smoke: SmokeBuilder,
    *,
    deadline_s: float = DEFAULT_DEADLINE_S,
) -> dict[str, Any]:
    """`deadline_s` applies only when the manifest carries no control-plane deadline."""
    clock = Clock(
        manifest.deadline_unix if manifest.deadline_unix is not None else time.time() + deadline_s
    )
    clock.timeout()
    if paths.staging.is_symlink():  # planted by a smoke job sharing the bucket
        paths.staging.unlink()
    for directory in (paths.staging, paths.cache, paths.work, paths.work / "home"):
        directory.mkdir(parents=True, exist_ok=True)
    _clean_staging(paths.staging)
    env = build_env(paths)
    cache_key, cache_hit, overlay_sha = lock_and_overlay(manifest, paths, runner, env, clock)
    repo = clone_repo(manifest, paths, runner, env, clock)
    archives = fetch_datasets(manifest, paths, runner, env, clock, fetch, smoke)
    report = {
        "schema": "p2p.provision-report/v1",
        "run_id": manifest.run_id,
        "lock_sha256": _sha256(paths.staging / "lock.txt"),
        "overlay_cache_key": cache_key,
        "overlay_cache_hit": cache_hit,
        "overlay_sha256": overlay_sha,
        "repo": manifest.repo_url is not None,
        "repo_commit": repo.commit,
        "requirement_files": repo.requirement_files,
        "repo_files_sha256": repo.snapshot_sha256,  # of the bytes written, not read back
        "datasets": [a["name"] for a in archives if a["name"] != "smoke"],
        "repo_sha256": repo.archive_sha256,
        "data_archives": archives,
        "smoke": manifest.smoke_dataset is not None,
    }
    (paths.staging / "provision_report.json").write_text(
        json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8"
    )
    return report


def deadline_expired(staging: Path) -> None:
    """Write the error and leave at once: raising would wait for in-process download threads
    (huggingface_hub joins its pool) until the watchdog cancels the job without a message."""
    try:
        if staging.is_symlink():
            staging.unlink()
        staging.mkdir(parents=True, exist_ok=True)
        with (staging / "provision_error.json").open("w", encoding="utf-8") as handle:
            handle.write(json.dumps({"error": "provisioning deadline reached"}))
            handle.flush()
            os.fsync(handle.fileno())
        print("PROVISION FAILED: provisioning deadline reached", file=sys.stderr, flush=True)
    except OSError:
        pass  # a full or broken mount must not keep us waiting: the watchdog still reports it
    finally:
        os._exit(1)


def _arm_deadline(manifest: Manifest, staging: Path) -> None:  # pragma: no cover - POSIX
    """In-process work (HF downloads, parquet, hashing) is stopped at the deadline too."""
    if manifest.deadline_unix is None or not hasattr(signal, "setitimer"):
        return
    signal.signal(signal.SIGALRM, lambda signum, frame: deadline_expired(staging))
    signal.setitimer(signal.ITIMER_REAL, max(manifest.deadline_unix - time.time(), 1.0))


def _disarm_deadline() -> None:  # pragma: no cover - POSIX
    if hasattr(signal, "setitimer"):
        signal.setitimer(signal.ITIMER_REAL, 0)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Paper2Proof provisioner")
    parser.add_argument("--manifest", default="/etc/p2p/provision.json")
    parser.add_argument("--staging", default="/mnt/run/staging")
    parser.add_argument("--cache", default="/mnt/cache")
    parser.add_argument("--work", default="/work")
    args = parser.parse_args(argv)
    paths = Paths(staging=Path(args.staging), cache=Path(args.cache), work=Path(args.work))
    try:
        manifest = validate_manifest(json.loads(Path(args.manifest).read_text(encoding="utf-8")))
        _arm_deadline(manifest, paths.staging)
        os.environ.update(build_env(paths))  # in-process HF downloads use local caches too
        try:
            provision(manifest, paths, subprocess_runner, hf_fetch, parquet_smoke)
        finally:
            _disarm_deadline()  # a late alarm must not turn a finished run into a failure
    except Exception as err:  # any failure becomes a readable error file, never a bare crash
        message = (
            str(err)
            if isinstance(err, ProvisionError)
            else f"provisioning failed: {type(err).__name__}"
        )
        paths.staging.mkdir(parents=True, exist_ok=True)
        (paths.staging / "provision_error.json").write_text(
            json.dumps({"error": message}), encoding="utf-8"
        )
        print(f"PROVISION FAILED: {message}", file=sys.stderr)
        return 1
    print("PROVISION OK")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

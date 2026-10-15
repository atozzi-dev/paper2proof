"""Provisioner: runs in the PROVISION job (CPU, allowlisted egress, no secrets).

Reads the manifest injected at /etc/p2p/provision.json (written by deterministic orchestrator
code), then: locks the extra packages with hashes (wheels only, never sdists), installs them into
an overlay, shallow-clones the official repository without running anything from it, downloads
datasets as archives, builds the smoke subset, and writes everything to /mnt/run/staging/.

Every external command is an argv list built here (never a shell string) with a minimal
environment. Standard library only; huggingface libraries are imported lazily for datasets.
Usage: python provision.py [--manifest PATH] [--staging DIR] [--cache DIR] [--work DIR]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

SCHEMA = "p2p.provision/v1"
RUN_ID = re.compile(r"^r_[0-9a-z]{26}$")
PACKAGE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?"
    r"(?:(?:===|==|~=|!=|<=|>=|<|>)[A-Za-z0-9.*+!_-]{1,40}"
    r"(?:,(?:===|==|~=|!=|<=|>=|<|>)[A-Za-z0-9.*+!_-]{1,40})*)?$"
)
REPO_URL = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
GIT_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
HF_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}(?:/[A-Za-z0-9][A-Za-z0-9_.-]{0,95})?$")
SPLIT = re.compile(r"^[A-Za-z0-9_.\[\]:%-]{1,64}$")
MAX_PACKAGES = 30
MAX_SMOKE_ROWS = 100_000
MAX_SMOKE_BYTES = 50 * 1024 * 1024
MAX_REQUIREMENT_FILE = 64 * 1024
REQUIREMENT_FILES = re.compile(
    r"^(requirements[\w.-]*\.txt|pyproject\.toml|setup\.cfg|environment\.ya?ml)$"
)
ALLOWED_ENV = frozenset(
    {"PATH", "HOME", "LANG", "GIT_TERMINAL_PROMPT", "UV_CACHE_DIR", "HF_HOME",
     "HF_HUB_DISABLE_TELEMETRY", "PYTHONDONTWRITEBYTECODE", "SSL_CERT_FILE"}
)  # fmt: skip
COMMAND_TIMEOUT_S = 900


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


@dataclass(frozen=True)
class Paths:
    staging: Path
    cache: Path
    work: Path


class Runner(Protocol):
    def __call__(
        self, argv: Sequence[str], *, env: dict[str, str], cwd: Path | None = None
    ) -> None: ...


DatasetFetcher = Callable[[str, "str | None", Path, "int | None"], None]


def _fail(message: str) -> ProvisionError:
    return ProvisionError(message)


def validate_manifest(data: Any) -> Manifest:
    """Defence in depth: the orchestrator validated these already (package policy)."""
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
        repo_url = str(repo.get("url", ""))
        raw_ref = repo.get("ref")
        repo_ref = None if raw_ref is None else str(raw_ref)
        if not REPO_URL.fullmatch(repo_url):
            raise _fail("repository must be a plain https://github.com/<owner>/<repo> URL")
        if repo_ref is not None and (not GIT_REF.fullmatch(repo_ref) or ".." in repo_ref):
            raise _fail("invalid git ref")
    datasets = []
    for entry in data.get("datasets") or []:
        if entry.get("kind") != "hf" or not HF_ID.fullmatch(str(entry.get("id", ""))):
            raise _fail("only Hugging Face datasets with a plain id are supported")
        split = entry.get("split")
        if split is not None and not SPLIT.fullmatch(str(split)):
            raise _fail("invalid dataset split")
        datasets.append(Dataset(id=entry["id"], split=split, max_bytes=int(entry["max_bytes"])))
    smoke = data.get("smoke_subset")
    smoke_dataset, smoke_rows = None, 0
    if smoke is not None:
        smoke_dataset, smoke_rows = str(smoke.get("dataset", "")), int(smoke.get("rows", 0))
        if not HF_ID.fullmatch(smoke_dataset) or not 1 <= smoke_rows <= MAX_SMOKE_ROWS:
            raise _fail("invalid smoke subset")
    if data.get("hf_token_secret") is not None:
        raise _fail("gated datasets are not supported: jobs never receive tokens")
    return Manifest(
        run_id=run_id,
        packages=packages,
        repo_url=repo_url,
        repo_ref=repo_ref,
        datasets=datasets,
        smoke_dataset=smoke_dataset,
        smoke_rows=smoke_rows,
    )


def build_env(paths: Paths) -> dict[str, str]:
    """Minimal environment for every subprocess: nothing inherited except PATH."""
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(paths.work / "home"),
        "LANG": "C.UTF-8",
        "GIT_TERMINAL_PROMPT": "0",
        "UV_CACHE_DIR": str(paths.cache / "uv"),
        "HF_HOME": str(paths.cache / "hf"),
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def subprocess_runner(argv: Sequence[str], *, env: dict[str, str], cwd: Path | None = None) -> None:
    subprocess.run(  # noqa: S603 (argv list built by this module, never a shell)
        list(argv),
        env=env,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT_S,
    )


def _pack(source: Path, out: Path, runner: Runner, env: dict[str, str]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    runner(["tar", "--zstd", "-cf", str(out), "-C", str(source), "."], env=env)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _explain(err: subprocess.CalledProcessError, what: str) -> ProvisionError:
    stderr = (err.stderr or "").lower()
    if any(s in stderr for s in ("no wheel", "only-binary", "sdist", "no matching distribution")):
        return _fail(
            f"{what}: a requested package has no wheel for this platform (sdists are never built)"
        )
    if "no solution found" in stderr or "conflict" in stderr:
        return _fail(f"{what}: the requested versions conflict")
    return _fail(f"{what} failed (exit {err.returncode})")


def lock_and_overlay(
    manifest: Manifest, paths: Paths, runner: Runner, env: dict[str, str]
) -> tuple[str, bool]:
    work = paths.work / "overlay"
    work.mkdir(parents=True, exist_ok=True)
    requirements_in = paths.work / "requirements.in"
    requirements_in.write_text("".join(f"{p}\n" for p in manifest.packages), encoding="utf-8")
    lock = paths.staging / "lock.txt"
    if manifest.packages:
        try:
            runner(
                ["uv", "pip", "compile", "--only-binary=:all:", "--generate-hashes", "--no-header",
                 "-o", str(lock), str(requirements_in)],
                env=env,
            )  # fmt: skip
        except subprocess.CalledProcessError as err:
            raise _explain(err, "locking packages") from None
    else:
        lock.write_text("", encoding="utf-8")
    lock_sha = _sha256(lock)
    cached = paths.cache / "overlays" / f"{lock_sha}.tar.zst"
    overlay = paths.staging / "overlay.tar.zst"
    if cached.is_file():
        shutil.copyfile(cached, overlay)
        return lock_sha, True
    if manifest.packages:
        try:
            runner(
                ["uv", "pip", "install", "--only-binary=:all:", "--require-hashes", "--no-deps",
                 "-r", str(lock), "--target", str(work)],
                env=env,
            )  # fmt: skip
        except subprocess.CalledProcessError as err:
            raise _explain(err, "installing packages") from None
    _pack(work, overlay, runner, env)
    cached.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(overlay, cached)
    return lock_sha, False


def clone_repo(
    manifest: Manifest, paths: Paths, runner: Runner, env: dict[str, str]
) -> dict[str, str]:
    """Shallow clone at a ref; nothing from the repository is executed. Returns the text of its
    requirement files (the control plane parses them for the package policy)."""
    if manifest.repo_url is None:
        return {}
    dest = paths.work / "repo"
    branch = ["--branch", manifest.repo_ref] if manifest.repo_ref else []  # None: default branch
    try:
        runner(
            ["git", "-c", "protocol.file.allow=never", "-c", "core.hooksPath=/dev/null",
             "-c", "advice.detachedHead=false", "clone", "--depth", "1", "--single-branch",
             "--no-tags", *branch, "--", manifest.repo_url, str(dest)],
            env=env,
        )  # fmt: skip
    except subprocess.CalledProcessError as err:
        raise _fail(f"cloning the repository failed (exit {err.returncode})") from None
    files = {}
    for path in sorted(dest.glob("*")):
        if path.is_symlink() or not path.is_file() or not REQUIREMENT_FILES.match(path.name):
            continue
        if path.stat().st_size <= MAX_REQUIREMENT_FILE:
            files[path.name] = path.read_text(encoding="utf-8", errors="replace")
    _pack(dest, paths.staging / "repo.tar.zst", runner, env)
    return files


def hf_fetch(
    dataset_id: str, split: str | None, dest: Path, rows: int | None
) -> None:  # pragma: no cover
    """Default dataset fetcher (runs only inside the provisioner image)."""
    if rows is None:
        from huggingface_hub import snapshot_download

        snapshot_download(repo_id=dataset_id, repo_type="dataset", local_dir=dest)
        return
    from datasets import load_dataset

    dest.mkdir(parents=True, exist_ok=True)
    subset = load_dataset(dataset_id, split=f"{split or 'train'}[:{rows}]")
    subset.to_parquet(str(dest / "smoke.parquet"))


def _size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file() and not p.is_symlink())


def fetch_datasets(
    manifest: Manifest, paths: Paths, runner: Runner, env: dict[str, str], fetch: DatasetFetcher
) -> list[str]:
    names = []
    for dataset in manifest.datasets:
        name = dataset.id.replace("/", "__")
        dest = paths.work / "data" / name
        fetch(dataset.id, dataset.split, dest, None)
        if _size(dest) > dataset.max_bytes:
            raise _fail(f"dataset {dataset.id} is larger than its {dataset.max_bytes}-byte cap")
        _pack(dest, paths.staging / "data" / f"{name}.tar.zst", runner, env)
        names.append(name)
    if manifest.smoke_dataset:
        dest = paths.work / "data" / "smoke"
        split = next((d.split for d in manifest.datasets if d.id == manifest.smoke_dataset), None)
        fetch(manifest.smoke_dataset, split, dest, manifest.smoke_rows)
        if _size(dest) > MAX_SMOKE_BYTES:
            raise _fail("smoke subset over 50 MB: lower smoke_subset.rows")
        _pack(dest, paths.staging / "data" / "smoke.tar.zst", runner, env)
    return names


def provision(
    manifest: Manifest, paths: Paths, runner: Runner, fetch: DatasetFetcher
) -> dict[str, Any]:
    for directory in (paths.staging, paths.cache, paths.work, paths.work / "home"):
        directory.mkdir(parents=True, exist_ok=True)
    env = build_env(paths)
    lock_sha, cache_hit = lock_and_overlay(manifest, paths, runner, env)
    requirement_files = clone_repo(manifest, paths, runner, env)
    datasets = fetch_datasets(manifest, paths, runner, env, fetch)
    report = {
        "schema": "p2p.provision-report/v1",
        "run_id": manifest.run_id,
        "lock_sha256": lock_sha,
        "overlay_cache_hit": cache_hit,
        "repo": manifest.repo_url is not None,
        "requirement_files": requirement_files,
        "datasets": datasets,
        "smoke": manifest.smoke_dataset is not None,
    }
    (paths.staging / "provision_report.json").write_text(
        json.dumps(report, indent=1), encoding="utf-8"
    )
    return report


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - exercised in the image
    parser = argparse.ArgumentParser(description="Paper2Proof provisioner")
    parser.add_argument("--manifest", default="/etc/p2p/provision.json")
    parser.add_argument("--staging", default="/mnt/run/staging")
    parser.add_argument("--cache", default="/mnt/cache")
    parser.add_argument("--work", default="/work")
    args = parser.parse_args(argv)
    paths = Paths(staging=Path(args.staging), cache=Path(args.cache), work=Path(args.work))
    try:
        manifest = validate_manifest(json.loads(Path(args.manifest).read_text(encoding="utf-8")))
        provision(manifest, paths, subprocess_runner, hf_fetch)
    except (ProvisionError, OSError, ValueError, subprocess.TimeoutExpired) as err:
        paths.staging.mkdir(parents=True, exist_ok=True)
        message = (
            str(err)
            if isinstance(err, ProvisionError)
            else f"provisioning failed: {type(err).__name__}"
        )
        (paths.staging / "provision_error.json").write_text(
            json.dumps({"error": message}), encoding="utf-8"
        )
        print(f"PROVISION FAILED: {message}", file=sys.stderr)
        return 1
    print("PROVISION OK")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

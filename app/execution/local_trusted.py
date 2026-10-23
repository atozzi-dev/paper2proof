"""LocalTrustedExecutor: smoke runs in local docker, for development only.

The single exception to invariant 1: it refuses to exist unless `P2P_ENV=dev`, and refuses to
prepare a run unless `config/canary_allowlist.yaml` binds the paper's sha256 to the arXiv ID
the run was submitted as (papers we wrote).
Even then the container has no network, a read-only root, no capabilities and a non-root user.
"""

import asyncio
import json
import os
import re
import shutil
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import yaml

from app.execution.config import DIGEST_REF
from app.execution.naming import run_slug
from app.execution.smoke import (
    SMOKE_METRICS,
    SmokeResult,
    parse_metrics,
    run_variants,
    validate_argv,
    validate_timeout,
    validate_workspace,
)

DockerRunner = Callable[[Sequence[str], float], Awaitable[tuple[int, str, str]]]


@dataclass(frozen=True)
class PaperRef:
    """What a run's paper is: the arXiv ID it was submitted as and the sha256 INGESTING hashed."""

    arxiv_id: str
    sha256: str


PaperLookup = Callable[[str], Awaitable[PaperRef | None]]


class _RunRecord(Protocol):
    @property
    def arxiv_id(self) -> str: ...

    @property
    def paper_sha256(self) -> str | None: ...


class _Runs(Protocol):
    async def get_run(self, run_id: str) -> _RunRecord | None: ...


def store_paper_lookup(runs: _Runs) -> PaperLookup:
    """The run's paper as submitted (its arXiv ID) and as ingested (the sha256 INGESTING
    computed); None until INGESTING has hashed it. The one lookup both local executors use."""

    async def lookup(run_id: str) -> PaperRef | None:
        run = await runs.get_run(run_id)
        if run is None or run.paper_sha256 is None:
            return None
        return PaperRef(arxiv_id=run.arxiv_id, sha256=run.paper_sha256)

    return lookup


def is_canary(canaries: Mapping[str, str], paper: PaperRef | None) -> bool:
    """A canary only under the arXiv ID its hash is bound to: the hash alone is not enough."""
    return paper is not None and canaries.get(paper.sha256.lower()) == paper.arxiv_id


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CANARY_ID = re.compile(r"^0000\.\d{5}$", re.ASCII)  # month 00: never a real arXiv paper


class LocalTrustedRefused(Exception):
    """Not dev, or not a canary paper: arbitrary paper code never runs on this machine."""


def load_canaries(path: Path) -> dict[str, str]:
    """{e-print sha256: canary arXiv ID}. A file of another shape is an error; an entry
    without a canary ID (0000.NNNNN) and a sha256, both plain strings, binds nothing."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    papers = data.get("papers") if isinstance(data, dict) else None
    if not isinstance(papers, list):
        raise ValueError(f"{path}: the canary allowlist must be a mapping with a `papers` list")
    canaries: dict[str, str] = {}
    for entry in papers:
        if not isinstance(entry, dict):
            continue
        digest, arxiv_id = entry.get("sha256"), entry.get("arxiv_id")
        if not (isinstance(digest, str) and isinstance(arxiv_id, str)):
            continue
        if _SHA256.fullmatch(digest.lower()) and _CANARY_ID.fullmatch(arxiv_id):
            canaries[digest.lower()] = arxiv_id
    return canaries


async def _tail(stream: asyncio.StreamReader | None, cap: int = 64 * 1024) -> bytes:
    """Read a stream to the end keeping only its last `cap` bytes (bounded memory)."""
    kept = b""
    while stream is not None and (chunk := await stream.read(65536)):
        kept = (kept + chunk)[-cap:]
    return kept


async def docker_cli(argv: Sequence[str], timeout_s: float) -> tuple[int, str, str]:
    """Run the docker CLI (argv list, no shell); kill the container on timeout."""
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(
            asyncio.gather(_tail(proc.stdout), _tail(proc.stderr)), timeout_s
        )
        await proc.wait()
    except TimeoutError:
        name = next((a.removeprefix("--name=") for a in argv if a.startswith("--name=")), None)
        try:
            if name is not None:  # a container run: stop the container, not only the CLI
                killer = await asyncio.create_subprocess_exec("docker", "kill", name)
                await killer.wait()
        finally:
            proc.kill()
            await proc.wait()
        return -1, "", f"docker timed out after {timeout_s:g} s"
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


def _read_metrics(workspace: Path, relative: str) -> dict[str, float] | None:
    """The container owns the workspace: never follow a link it planted to a host file."""
    path = workspace
    for part in relative.split("/"):
        path = path / part
        if path.is_symlink():
            return None
    if not path.is_file() or path.stat().st_size > 64 * 1024:
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as handle:
            data = handle.read(64 * 1024 + 1)
        return parse_metrics(json.loads(data))
    except (OSError, ValueError, RecursionError):
        return None


class LocalTrustedExecutor:
    def __init__(
        self,
        *,
        env: str,
        canaries: Mapping[str, str],
        paper: PaperLookup,
        image: str,
        work_root: Path,
        docker: DockerRunner = docker_cli,
    ) -> None:
        if env != "dev":
            raise LocalTrustedRefused("LocalTrustedExecutor runs only with P2P_ENV=dev")
        if not re.fullmatch(DIGEST_REF, image):
            raise ValueError("the smoke image must be referenced by digest")
        self._canaries = canaries
        self._paper = paper
        self._image = image
        self._root = work_root
        self._docker = docker
        self._runs = 0
        self._approved: set[str] = set()  # run ids whose paper passed the canary check

    async def prepare(self, run_id: str, files: dict[str, bytes]) -> str:
        if not is_canary(self._canaries, await self._paper(run_id)):
            raise LocalTrustedRefused("not a canary paper: its code never runs locally")
        base = self._root / run_slug(run_id)
        shutil.rmtree(base / "base", ignore_errors=True)
        self._write(base / "base", validate_workspace(files))
        self._approved.add(run_id)
        return run_id

    @staticmethod
    def _write(dest: Path, files: dict[str, bytes]) -> None:
        for path, data in files.items():
            target = dest / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

    async def run(
        self, env_id: str, argv: list[str], files: dict[str, bytes], timeout_s: int
    ) -> SmokeResult:
        if env_id not in self._approved:
            raise LocalTrustedRefused("prepare() with a canary paper must come first")
        argv = validate_argv(argv)
        validate_timeout(timeout_s)
        files = validate_workspace(files)
        self._runs += 1
        slug = run_slug(env_id)
        workspace = self._root / slug / f"run-{self._runs}"
        shutil.rmtree(workspace, ignore_errors=True)
        shutil.copytree(self._root / slug / "base", workspace)
        self._write(workspace, files)
        validate_workspace(
            {
                str(p.relative_to(workspace).as_posix()): b""
                for p in workspace.rglob("*")
                if p.is_file()
            }
        )
        command = [
            "docker", "run", "--rm", f"--name=p2p-local-{slug}-{self._runs}",
            "--network=none", "--read-only", "--tmpfs=/tmp:rw,size=512m",
            "--pids-limit=256", "--memory=4g", "--cpus=2", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--user=10001:10001",
            "-v", f"{workspace.resolve()}:/workspace:rw", "-w", "/workspace",
            "--entrypoint=python", self._image, *argv[1:],
        ]  # fmt: skip
        started = time.monotonic()
        code, stdout, stderr = await self._docker(command, timeout_s)
        metrics = _read_metrics(workspace, SMOKE_METRICS)
        return SmokeResult(
            exit_code=code,
            stdout=stdout[-64 * 1024 :],
            stderr=stderr[-64 * 1024 :],
            duration_s=time.monotonic() - started,
            metrics=metrics,
        )

    async def run_branches(
        self, env_id: str, variants: list[dict[str, bytes]], argv: list[str], timeout_s: int
    ) -> list[SmokeResult]:
        return await run_variants(self, env_id, variants, argv, timeout_s)

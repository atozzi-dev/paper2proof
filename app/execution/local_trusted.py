"""LocalTrustedExecutor: smoke runs in local docker, for development only.

The single exception to invariant 1: it refuses to exist unless `P2P_ENV=dev`, and refuses to
prepare a run unless the paper's sha256 is in `config/canary_allowlist.yaml` (papers we wrote).
Even then the container has no network, a read-only root, no capabilities and a non-root user.
"""

import asyncio
import json
import re
import shutil
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

import yaml

from app.execution.config import DIGEST_REF
from app.execution.naming import run_slug
from app.execution.smoke import (
    SMOKE_METRICS,
    SmokeResult,
    parse_metrics,
    run_variants,
    validate_argv,
    validate_workspace,
)

DockerRunner = Callable[[Sequence[str], float], Awaitable[tuple[int, str, str]]]
PaperHash = Callable[[str], Awaitable[str | None]]
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class LocalTrustedRefused(Exception):
    """Not dev, or not a canary paper: arbitrary paper code never runs on this machine."""


def load_canaries(path: Path) -> frozenset[str]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    papers = data.get("papers") or []
    return frozenset(str(p).lower() for p in papers if _SHA256.fullmatch(str(p).lower()))


async def docker_cli(argv: Sequence[str], timeout_s: float) -> tuple[int, str, str]:
    """Run the docker CLI (argv list, no shell); kill the container on timeout."""
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout_s)
    except TimeoutError:
        name = next(a.removeprefix("--name=") for a in argv if a.startswith("--name="))
        killer = await asyncio.create_subprocess_exec("docker", "kill", name)
        await killer.wait()
        proc.kill()
        await proc.wait()
        return -1, "", f"smoke run timed out after {timeout_s:g} s"
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


class LocalTrustedExecutor:
    def __init__(
        self,
        *,
        env: str,
        canaries: frozenset[str],
        paper_sha256: PaperHash,
        image: str,
        work_root: Path,
        docker: DockerRunner = docker_cli,
    ) -> None:
        if env != "dev":
            raise LocalTrustedRefused("LocalTrustedExecutor runs only with P2P_ENV=dev")
        if not re.fullmatch(DIGEST_REF, image):
            raise ValueError("the smoke image must be referenced by digest")
        self._canaries = canaries
        self._paper_sha256 = paper_sha256
        self._image = image
        self._root = work_root
        self._docker = docker
        self._runs = 0

    async def prepare(self, run_id: str, files: dict[str, bytes]) -> str:
        digest = await self._paper_sha256(run_id)
        if digest is None or digest.lower() not in self._canaries:
            raise LocalTrustedRefused("not a canary paper: its code never runs locally")
        base = self._root / run_slug(run_id)
        shutil.rmtree(base / "base", ignore_errors=True)
        self._write(base / "base", validate_workspace(files))
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
        argv = validate_argv(argv)
        files = validate_workspace(files)
        self._runs += 1
        slug = run_slug(env_id)
        workspace = self._root / slug / f"run-{self._runs}"
        shutil.rmtree(workspace, ignore_errors=True)
        shutil.copytree(self._root / slug / "base", workspace)
        self._write(workspace, files)
        command = [
            "docker", "run", "--rm", f"--name=p2p-local-{slug}-{self._runs}",
            "--network=none", "--read-only", "--tmpfs=/tmp:rw,size=512m",
            "--pids-limit=256", "--memory=4g", "--cpus=2", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--user=10001:10001",
            "-v", f"{workspace.resolve()}:/workspace:rw", "-w", "/workspace",
            self._image, *argv,
        ]  # fmt: skip
        started = time.monotonic()
        code, stdout, stderr = await self._docker(command, timeout_s)
        metrics_file = workspace / SMOKE_METRICS
        metrics = None
        if metrics_file.is_file() and metrics_file.stat().st_size <= 64 * 1024:
            try:
                metrics = parse_metrics(json.loads(metrics_file.read_text(encoding="utf-8")))
            except ValueError:
                metrics = None
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

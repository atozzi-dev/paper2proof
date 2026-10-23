"""Smoke executors (docs/specs/execution-contracts.md §9-§10): the Coder's script runs on a
small data subset, with no network, somewhere that is never the control plane (invariant 1).

Everything here treats workspace files, argv and results as untrusted: the Coder chose them and
untrusted code produced the results.
"""

import asyncio
import hashlib
import io
import math
import re
import tarfile
from collections.abc import Mapping
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

MAX_BRANCHES = 2
MAX_FILES = 200
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_WORKSPACE_BYTES = 8 * 1024 * 1024
MAX_ARGS = 32
MAX_OUTPUT_CHARS = 64 * 1024
MAX_TIMEOUT_S = 240  # a smoke run's own limit (spec: smoke scripts take <= 120 s)
SMOKE_METRICS = "out/metrics.json"  # the Coder's smoke argv uses `--out out`
_PATH_PART = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}$")
_RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$", re.IGNORECASE)
_SCRIPT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,199}\.py$")
_METRIC = re.compile(r"^[A-Za-z0-9_./-]{1,100}$")


class SmokeUnavailable(Exception):
    """The executor itself failed (API down, quota, not configured): not the Coder's fault."""


class SmokeContractError(Exception):
    """Workspace, argv or result outside the contract."""


class SmokeResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    metrics: dict[str, float] | None


class SmokeExecutor(Protocol):
    async def prepare(self, run_id: str, files: dict[str, bytes]) -> str: ...
    async def run(
        self, env_id: str, argv: list[str], files: dict[str, bytes], timeout_s: int
    ) -> SmokeResult: ...
    async def run_branches(
        self, env_id: str, variants: list[dict[str, bytes]], argv: list[str], timeout_s: int
    ) -> list[SmokeResult]: ...


def validate_path(path: str) -> str:
    parts = path.split("/")
    if not path or len(parts) > 8 or not all(_PATH_PART.fullmatch(p) for p in parts):
        raise SmokeContractError(f"invalid workspace path: {path!r}")
    if any(p.endswith(".") or _RESERVED.match(p) for p in parts):  # also safe on Windows hosts
        raise SmokeContractError(f"invalid workspace path: {path!r}")
    return path


def validate_timeout(timeout_s: int) -> int:
    if isinstance(timeout_s, bool) or not 1 <= timeout_s <= MAX_TIMEOUT_S:
        raise SmokeContractError(f"smoke timeout must be 1..{MAX_TIMEOUT_S} s")
    return timeout_s


def pack_with_digest(files: Mapping[str, bytes]) -> tuple[bytes, str]:
    """The archive and its sha256, both computed in the CPU pool (invariant 12)."""
    archive = pack_workspace(files)
    return archive, hashlib.sha256(archive).hexdigest()


def pack_workspace(files: Mapping[str, bytes]) -> bytes:
    """Deterministic plain tar of a validated workspace (pure: runs in the CPU pool)."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path, data in sorted(validate_workspace(files).items()):
            info = tarfile.TarInfo(path)
            info.size, info.mode, info.mtime = len(data), 0o644, 0
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def validate_workspace(files: Mapping[str, bytes]) -> dict[str, bytes]:
    if len(files) > MAX_FILES:
        raise SmokeContractError(f"more than {MAX_FILES} workspace files")
    total = 0
    for path, data in files.items():
        validate_path(path)
        if len(data) > MAX_FILE_BYTES:
            raise SmokeContractError(f"{path} is larger than {MAX_FILE_BYTES} bytes")
        total += len(data)
    if total > MAX_WORKSPACE_BYTES:
        raise SmokeContractError("workspace larger than 8 MiB")
    return dict(files)


def validate_argv(argv: list[str]) -> list[str]:
    """`python <workspace script> [args]`: no interpreter flags, no other programs."""
    if len(argv) < 2 or argv[0] != "python" or len(argv) > MAX_ARGS:
        raise SmokeContractError("argv must be: python <script.py> [args] (at most 32)")
    script = argv[1]
    if not _SCRIPT.fullmatch(script) or ".." in script.split("/") or "//" in script:
        raise SmokeContractError(f"invalid script path: {script!r}")
    for arg in argv[2:]:
        if len(arg) > 200 or any(ord(c) < 32 for c in arg):
            raise SmokeContractError("invalid argument")
    return list(argv)


def _text(value: Any) -> str:
    return value[-MAX_OUTPUT_CHARS:] if isinstance(value, str) else ""


def _finite(value: Any) -> float | None:
    """A real number that fits a float, else None (an integer can be too large for one)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def parse_metrics(raw: Any) -> dict[str, float] | None:
    if not isinstance(raw, dict):
        return None
    return {
        k: number
        for k, v in raw.items()
        if isinstance(k, str) and _METRIC.fullmatch(k) and (number := _finite(v)) is not None
    }


def parse_smoke_result(raw: Any) -> SmokeResult:
    """A result produced next to untrusted code: strict types, capped text, finite metrics."""
    if not isinstance(raw, dict):
        raise SmokeContractError("smoke result is not an object")
    code = raw.get("exit_code")
    if not isinstance(code, int) or isinstance(code, bool):
        raise SmokeContractError("smoke result has no integer exit_code")
    duration = raw.get("duration_s")
    seconds = 0.0
    if isinstance(duration, int | float) and math.isfinite(duration):
        seconds = max(0.0, float(duration))
    return SmokeResult(
        exit_code=code,
        stdout=_text(raw.get("stdout")),
        stderr=_text(raw.get("stderr")),
        duration_s=seconds,
        metrics=parse_metrics(raw.get("metrics")),
    )


async def run_variants(
    executor: SmokeExecutor,
    env_id: str,
    variants: list[dict[str, bytes]],
    argv: list[str],
    timeout_s: int,
) -> list[SmokeResult]:
    """Shared `run_branches`: at most MAX_BRANCHES variants, concurrently."""
    if not 1 <= len(variants) <= MAX_BRANCHES:
        raise SmokeContractError(f"1 to {MAX_BRANCHES} branches")
    return list(await asyncio.gather(*(executor.run(env_id, argv, v, timeout_s) for v in variants)))


class FakeSmokeExecutor:
    """Scripted results, in order (tests and the `test` profile)."""

    def __init__(self, results: list[SmokeResult]) -> None:
        self.results = list(results)
        self.prepared: list[str] = []
        self.runs: list[tuple[str, list[str], dict[str, bytes]]] = []
        self.calls = 0

    async def prepare(self, run_id: str, files: dict[str, bytes]) -> str:
        self.prepared.append(run_id)
        return run_id

    async def run(
        self, env_id: str, argv: list[str], files: dict[str, bytes], timeout_s: int
    ) -> SmokeResult:
        self.calls += 1
        self.runs.append((env_id, argv, files))
        if not self.results:
            raise SmokeUnavailable("no scripted result left")
        return self.results.pop(0)

    async def run_branches(
        self, env_id: str, variants: list[dict[str, bytes]], argv: list[str], timeout_s: int
    ) -> list[SmokeResult]:
        return await run_variants(self, env_id, variants, argv, timeout_s)


class FallbackSmokeExecutor:
    """Primary executor (Sandboxes) until `threshold` consecutive SmokeUnavailable errors, then
    the fallback (CPU smoke jobs) for the rest of the run. Env ids are the run id; each
    executor's own env is prepared lazily from the files given to `prepare`."""

    def __init__(
        self, primary: SmokeExecutor, fallback: SmokeExecutor, *, threshold: int = 3
    ) -> None:
        self._executors = [primary, fallback]
        self._threshold = threshold
        self._errors = 0
        self._index = 0
        self._files: dict[str, dict[str, bytes]] = {}
        self._envs: dict[tuple[int, str], str] = {}

    @property
    def active(self) -> SmokeExecutor:
        return self._executors[self._index]

    def _failed(self) -> None:
        self._errors += 1
        if self._errors >= self._threshold and self._index == 0:
            self._index, self._errors = 1, 0

    async def _env(self, run_id: str) -> str:
        key = (self._index, run_id)
        if key not in self._envs:
            self._envs[key] = await self.active.prepare(run_id, self._files[run_id])
        return self._envs[key]

    async def prepare(self, run_id: str, files: dict[str, bytes]) -> str:
        self._files[run_id] = dict(files)
        try:
            await self._env(run_id)
        except SmokeUnavailable:
            self._failed()
            if self._index == 0:
                raise
            await self._env(run_id)
        return run_id

    async def run(
        self, env_id: str, argv: list[str], files: dict[str, bytes], timeout_s: int
    ) -> SmokeResult:
        try:
            result = await self.active.run(await self._env(env_id), argv, files, timeout_s)
        except SmokeUnavailable:
            self._failed()
            raise
        self._errors = 0
        return result

    async def run_branches(
        self, env_id: str, variants: list[dict[str, bytes]], argv: list[str], timeout_s: int
    ) -> list[SmokeResult]:
        try:
            results = await self.active.run_branches(
                await self._env(env_id), variants, argv, timeout_s
            )
        except SmokeUnavailable:
            self._failed()
            raise
        self._errors = 0
        return results

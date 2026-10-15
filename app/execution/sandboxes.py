"""SandboxesSmokeExecutor: Token Factory Sandboxes, networking disabled, disposable instances.

Request and response shapes follow spike S-3 (`spikes/s3_sandboxes.py`); check
`docs/spikes/RESULTS.md` before relying on them. Pending S-3: uploading the overlay and the smoke
data, and the prepare-with-save checkpoint (execution-contracts §10 step 2). Until then each run
starts from the imported runner-cpu image with the workspace inlined in the command, and
anything that doesn't fit raises SmokeUnavailable so the fallback (CPU smoke jobs) takes over.
"""

import asyncio
import base64
import io
import json
import secrets
import tarfile
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from pydantic import SecretStr

from app.execution.smoke import (
    SMOKE_METRICS,
    SmokeResult,
    SmokeUnavailable,
    parse_metrics,
    run_variants,
    validate_argv,
    validate_timeout,
    validate_workspace,
)
from app.orchestrator.budget import Budget, SpendKind

SANDBOXES_API = "https://api.tokenfactory.nebius.com/sandboxes"
NON_TERMINAL = {"PENDING", "ASSIGNED", "EXECUTING"}
MAX_INLINE_BYTES = 256 * 1024  # base64 workspace inlined in the command
MAX_RAW_BYTES = 1024 * 1024
POLL_S = 2.0
WORKDIR = "/tmp/ws"  # noqa: S108 - inside the disposable sandbox, not here


def _quote(arg: str) -> str:
    return "'" + arg.replace("'", "'\"'\"'") + "'"


def _pack(files: dict[str, bytes]) -> str:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for path, data in sorted(files.items()):
            info = tarfile.TarInfo(path)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return base64.b64encode(buffer.getvalue()).decode()


def _decode(stream: Any) -> str:
    if not isinstance(stream, dict):
        return ""
    value = str(stream.get("value") or "")
    if stream.get("encoding") == "base64":
        try:
            return base64.b64decode(value).decode("utf-8", errors="replace")
        except ValueError:
            return ""
    return value


class SandboxesSmokeExecutor:
    def __init__(
        self,
        *,
        api_key: SecretStr,
        project_id: str,
        base_image: str | None,
        budget: Budget,
        usd_per_hour: float,
        http: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._key = api_key
        self._project = project_id
        self._image = base_image
        self._budget = budget
        self._usd_per_hour = usd_per_hour
        self._http = http
        self._sleep = sleep
        self._base: dict[str, dict[str, bytes]] = {}

    async def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        headers = {
            "Authorization": f"Bearer {self._key.get_secret_value()}",
            "Project": self._project,
        }
        client = self._http or httpx.AsyncClient(timeout=60)
        try:
            response = await client.request(
                method, f"{SANDBOXES_API}{path}", json=body, headers=headers
            )
        except httpx.HTTPError as err:
            raise SmokeUnavailable(f"Sandboxes unreachable: {type(err).__name__}") from None
        finally:
            if self._http is None:
                await client.aclose()
        if response.status_code not in (200, 201):
            raise SmokeUnavailable(f"Sandboxes returned HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError:
            raise SmokeUnavailable("Sandboxes returned a malformed body") from None

    async def prepare(self, run_id: str, files: dict[str, bytes]) -> str:
        if not self._image:
            raise SmokeUnavailable("no Sandboxes image imported for this runner-cpu digest")
        self._base[run_id] = validate_workspace(files)
        return run_id

    def _command(self, payload: str, argv: list[str], marker: str) -> str:
        return "\n".join(
            [
                "set -e",
                f"mkdir -p {WORKDIR} && cd {WORKDIR}",
                f"printf '%s' '{payload}' | base64 -d | tar -xzf - -C {WORKDIR} --no-same-owner",
                "set +e",
                " ".join(_quote(a) for a in argv),
                "code=$?",
                f"echo {marker}",
                f"cat {SMOKE_METRICS} 2>/dev/null",
                "exit $code",
            ]
        )

    async def run(
        self, env_id: str, argv: list[str], files: dict[str, bytes], timeout_s: int
    ) -> SmokeResult:
        argv = validate_argv(argv)
        validate_timeout(timeout_s)
        workspace = validate_workspace({**self._base.get(env_id, {}), **validate_workspace(files)})
        if sum(len(d) for d in workspace.values()) > MAX_RAW_BYTES:
            raise SmokeUnavailable("workspace too large to inline in a Sandboxes command")
        payload = await asyncio.to_thread(_pack, workspace)
        if len(payload) > MAX_INLINE_BYTES:
            raise SmokeUnavailable("workspace too large to inline in a Sandboxes command")
        marker = f"P2P-METRICS-{secrets.token_hex(8)}"
        reserved = self._usd_per_hour * (timeout_s + 120) / 3600
        reservation = await self._budget.check_and_reserve(env_id, SpendKind.CLOUD_USD, reserved)
        started = time.monotonic()
        finished = False  # an abandoned instance keeps running (and billing) up to its timeout
        try:
            spawn = await self._call(
                "POST",
                "/v1/instances",
                {
                    "image": self._image,
                    "command": self._command(payload, argv, marker),
                    "shell": True,
                    "networking": {"enabled": False},
                    "disposable": True,
                    "timeout": timeout_s,
                },
            )
            op_id = spawn.get("uuid") if isinstance(spawn, dict) else None
            if not isinstance(op_id, str) or not op_id.isascii() or "/" in op_id:
                raise SmokeUnavailable("Sandboxes returned no operation id")
            op: Any = {}
            for _ in range(int((timeout_s + 120) / POLL_S) + 1):
                op = await self._call("GET", f"/v1/operations/{op_id}")
                if not isinstance(op, dict) or op.get("status") not in NON_TERMINAL:
                    break
                await self._sleep(POLL_S)
            else:
                raise SmokeUnavailable("Sandboxes operation did not finish in time")
            finished = True
        finally:
            elapsed = time.monotonic() - started
            await self._budget.commit(
                reservation, self._usd_per_hour * elapsed / 3600 if finished else reserved
            )
        result = ((op.get("metadata") or {}).get("result") or {}) if isinstance(op, dict) else {}
        code = (result.get("state") or {}).get("exit_code") if isinstance(result, dict) else None
        if not isinstance(code, int) or isinstance(code, bool):
            status = op.get("status") if isinstance(op, dict) else None
            raise SmokeUnavailable(
                f"Sandboxes operation ended without an exit code ({status!s:.32})"
            )
        stdout, _, tail = _decode(result.get("stdout")).partition(f"\n{marker}\n")
        tail = tail[: 64 * 1024]
        try:
            metrics = parse_metrics(json.loads(tail)) if tail.strip() else None
        except (ValueError, RecursionError):
            metrics = None
        return SmokeResult(
            exit_code=code,
            stdout=stdout[-64 * 1024 :],
            stderr=_decode(result.get("stderr"))[-64 * 1024 :],
            duration_s=elapsed,
            metrics=metrics,
        )

    async def run_branches(
        self, env_id: str, variants: list[dict[str, bytes]], argv: list[str], timeout_s: int
    ) -> list[SmokeResult]:
        return await run_variants(self, env_id, variants, argv, timeout_s)

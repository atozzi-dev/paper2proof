"""SandboxesJobBackend: smoke and exec jobs in Token Factory Sandboxes, for the hybrid deploy
without GPU capacity (docs/specs/hybrid-deploy.md, "Experiments in Sandboxes").

A job spec runs as the Nebius job would: the same runner-cpu image (imported into Sandboxes), the
same injected manifest, the run bucket's inputs at the same paths, no network. Sandboxes mounts no
bucket, so the inputs are uploaded to its content-addressed files API and mounted read-only, and
the outputs come back as a harvest on the instance's stdout (images/runner/harvest.py), written to
the run bucket before the job is reported terminal. Sandboxes has no job names: a host-side index
(one file per job name) keeps creation get-by-name first (invariant 9) and lets the reaper list
the jobs. Everything read back was written next to untrusted code: it is parsed as data only.
"""

import asyncio
import base64
import binascii
import contextlib
import hashlib
import io
import json
import logging
import os
import re
import secrets
import stat
import tarfile
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import Executor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr, ValidationError

from app.domain.contracts import ArchiveDigests, ExecManifest
from app.execution.backend import (
    CapacityError,
    JobHandle,
    JobKind,
    JobSpec,
    JobState,
    JobStatusUnavailable,
)
from app.execution.hybrid import open_no_follow
from app.execution.naming import bucket_name, run_slug
from app.execution.nebius_backend import InputUploadError, JobBackendError
from app.execution.run_storage import RunStorage
from app.execution.s3 import check_run_key
from app.execution.sandboxes import SANDBOXES_API

log = logging.getLogger(__name__)


class _Transient(InputUploadError):
    """An upload the gateway or the service failed (429, 5xx, transport): worth a resend."""


SANDBOX_PLATFORM = "sandbox"  # the judged hardware the bundle records (config/pricing.yaml)
# what an instance gets, as a preset (priced in config/pricing.yaml): 4 vCPU, ~3.8 GiB (S14)
SANDBOX_PRESET = "4vcpu-4gb"
KINDS = frozenset({JobKind.SMOKE, JobKind.EXEC})
RUN_MOUNT = "/mnt/run"
JOB_UID = 10001
MAX_INSTANCE_S = 3600  # instance_max_timeout (spike S3)
INSTANCE_SLACK_S = 120  # the instance's own timeout past the watchdog: never the first to fire
HOST_MARGIN_S = 60  # SIGTERM this long before the host's watchdog: the 30 s grace and the harvest
# end before the control plane may cancel the instance
OUTPUT_CAP = 10 * 1024 * 1024  # truncate_output_at: the API's maximum per stream
MAX_STORAGE_INPUT = 64 * 1024 * 1024  # a workspace from RunStorage (the runner caps it at 16 MiB)
MAX_LOCAL_INPUT = 12 * 2**30  # one archive from the local copy: the instance's layer limit (S3)
INSTANCE_LOG_BYTES = 64 * 1024  # the stderr tail kept next to the results (untrusted text)
LIST_PAGE = 1000
MAX_PAGES = 10  # a lookup past this, or a page seen twice, is an outage
CHUNK = 1 << 20
UPLOAD_BACKOFF_S = (5.0, 15.0)  # between the attempts at an upload (3 in all)
UPLOAD_TOTAL_S = 300.0  # all attempts: the upload counts against the job's queue cap
_DROPPED = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)
_TRANSIENT = {429, 500, 502, 503, 504}  # the gateway or the service, not the request
LOOKBACK = timedelta(minutes=5)  # a lost spawn is looked for from its intent minus this
STALE_INTENT = timedelta(minutes=10)  # an intent with no operation after this never spawned
KiB = 1024
# what images/runner/harvest.py sends; JSON keeps cap + 1 bytes (still "too large" to a reader)
CAPS = {
    "exec": {
        "results.json": 256 * KiB,
        "env.json": 256 * KiB,
        "live.log": 512 * KiB,
        "stdout.log": 2048 * KiB,
        "stderr.log": 2048 * KiB,
    },
    "smoke": {"result.json": 256 * KiB},
}
_OUT_DIR = re.compile(r"^/mnt/run/[a-z0-9][a-z0-9/_.-]{0,120}$")
_NAME = re.compile(r"^p2p-(smoke|exec)-[0-9a-z]{26}-[0-9]{1,4}$")
_MARKER = re.compile(r"^[0-9a-f]{32}$")
_DROP = "--reuid=10001 --regid=10001 --clear-groups"
_STATES = {
    "PENDING": JobState.PENDING,
    "ASSIGNED": JobState.PENDING,
    "EXECUTING": JobState.RUNNING,
}
_DONE = frozenset({"SUCCESS", "SUCCEEDED", "FAILED"})


def job_command(*, kind: JobKind, out_dir: str, watchdog_s: int, marker: str) -> str:
    """The instance's shell command (as root, networking disabled): the image's entrypoint as
    uid 10001 with SIGTERM at the watchdog, every uid 10001 process killed after it, then the
    harvest as uid 10001. The runner's own output stays in a root-only file (its tail goes to
    stderr): stdout carries the harvest only."""
    if not _OUT_DIR.fullmatch(out_dir) or ".." in out_dir or not _MARKER.fullmatch(marker):
        raise ValueError("unsafe output directory or marker")
    harvest = "exec" if kind is JobKind.EXEC else "smoke"
    term_s = max(watchdog_s - HOST_MARGIN_S, 1)
    return "\n".join(
        [
            "set -u",
            # root-only, never seen by uid 10001; exit 40/41: the instance itself was unusable
            "logdir=/root/p2p",
            'install -d -m 0700 "$logdir" 2>/dev/null || logdir="$(mktemp -d)" || exit 40',
            f"mkdir -p {out_dir} && chown {JOB_UID}:{JOB_UID} {out_dir} && chmod 0750 {out_dir}"
            " || exit 41",
            # its own session, no stdin: nothing it starts holds the instance's stdout
            f"timeout --preserve-status -k 30 -s TERM {term_s} setpriv {_DROP} "
            "--no-new-privs setsid -w /opt/venv/bin/python -I /opt/p2p/entrypoint.py "
            '< /dev/null > "$logdir/runner.log" 2>&1',
            "code=$?",
            f"setpriv {_DROP} --no-new-privs /bin/sh -c 'kill -KILL -1' 2>/dev/null",
            f"setpriv {_DROP} --no-new-privs /opt/venv/bin/python -I /opt/p2p/harvest.py "
            f"--dir {out_dir} --kind {harvest} --marker {marker}",
            'tail -c 65536 "$logdir/runner.log" >&2',
            'exit "$code"',
        ]
    )


def parse_harvest(stdout: str, marker: str, kind: str) -> dict[str, bytes] | None:
    """The files of the last harvest between `<marker>` and `<marker>-END` (it is printed
    after every uid 10001 process was killed): allowed names, regular members, within their
    caps; None without a readable harvest. Pure (CPU pool)."""
    caps = CAPS[kind]
    start = f"\n{marker}\n"
    _, found, rest = stdout.rpartition(start)
    if not found:
        return None
    payload, ended, _ = rest.partition(f"\n{marker}-END\n")
    if not ended:
        return None
    try:
        raw = base64.b64decode(payload.strip(), validate=True)
        files: dict[str, bytes] = {}
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tar:
            for member in tar:
                cap = caps.get(member.name)
                if cap is None or not member.isreg() or member.name in files:
                    continue
                if member.size > (cap if member.name.endswith(".log") else cap + 1):
                    continue
                extracted = tar.extractfile(member)
                if extracted is not None:
                    files[member.name] = extracted.read(member.size)
    except (binascii.Error, ValueError, tarfile.TarError, EOFError):
        return None
    return files


class _Index:
    """One JSON file per job name, written atomically (host-side, never seen by a job)."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def _path(self, name: str) -> Path:
        if not _NAME.fullmatch(name):
            raise ValueError(f"not a Sandboxes job name: {name!r}")
        return self._root / f"{name}.json"

    def get(self, name: str) -> dict[str, Any] | None:
        try:
            data = json.loads(self._path(name).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except ValueError:  # torn by a crash despite the fsync: no usable entry
            log.warning("unreadable sandbox job index entry", extra={"job": name})
            return None
        return data if isinstance(data, dict) else None

    def put(self, name: str, entry: dict[str, Any]) -> None:
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._root.chmod(0o700)
        path = self._path(name)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(entry))
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)

    def drop(self, name: str) -> None:
        self._path(name).unlink(missing_ok=True)

    def names(self, prefix: str) -> list[str]:
        if not self._root.is_dir():
            return []
        return sorted(
            p.stem
            for p in self._root.glob("p2p-*.json")
            if p.stem.startswith(prefix) and _NAME.fullmatch(p.stem)
        )


def _text(stream: Any) -> str:
    """An instance stream (ascii or base64), decoded leniently."""
    if not isinstance(stream, dict):
        return ""
    value = str(stream.get("value") or "")
    if stream.get("encoding") == "base64":
        try:
            return base64.b64decode(value).decode("utf-8", "replace")
        except (binascii.Error, ValueError):
            return ""
    return value


def _json_dict(response: httpx.Response, what: str) -> dict[str, Any]:
    """The answer's JSON object; a malformed one is an error, never an internal crash."""
    try:
        body = response.json()
    except ValueError:
        raise JobBackendError(f"{what}: the answer is not JSON") from None
    if not isinstance(body, dict):
        raise JobBackendError(f"{what}: the answer is not a JSON object")
    return body


def _hash_fd(fd: int, size: int) -> str:
    """sha256 of the first `size` bytes of `fd` from its start (blocking: call it in a thread)."""
    os.lseek(fd, 0, os.SEEK_SET)
    digest, left = hashlib.sha256(), size
    while left > 0:
        chunk = os.read(fd, min(CHUNK, left))
        if not chunk:
            break
        digest.update(chunk)
        left -= len(chunk)
    return digest.hexdigest()


class SandboxesJobBackend:
    """Smoke and exec jobs in Sandboxes; the hybrid backend routes those two kinds here and
    keeps buckets, provision and fetch jobs elsewhere."""

    def __init__(
        self,
        *,
        api_key: SecretStr,
        project_id: str,
        runner_image: str,
        image_uuid: str,
        storage: RunStorage,
        local_buckets: Path,
        index_dir: Path,
        cpu_pool: Executor | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        http: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._key = api_key
        self._project = project_id
        self._image = runner_image  # invariant 8: the only digest these jobs may run
        self._image_uuid = image_uuid
        self._storage = storage
        self._local = local_buckets
        self._index = _Index(index_dir)
        self._pool = cpu_pool
        self._clock = clock
        self._http = http
        self._sleep = sleep
        self._hashes: dict[tuple[int, int, int, int], str] = {}  # (dev, ino, size, mtime) -> sha
        self._locks: dict[str, asyncio.Lock] = {}

    def bind_cpu_pool(self, pool: Executor | None) -> None:
        """The runtime's process pool, for parsing harvests (invariant 12)."""
        self._pool = pool

    # --- HTTP ----------------------------------------------------------------------------

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(60, write=900))
        return self._http

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {self._key.get_secret_value()}",
            "Project": self._project,
            **kwargs.pop("headers", {}),
        }
        return await self._client().request(
            method, f"{SANDBOXES_API}{path}", headers=headers, **kwargs
        )

    async def _get_op(self, uuid: str) -> dict[str, Any]:
        try:
            response = await self._request("GET", f"/v1/operations/{uuid}")
        except httpx.HTTPError as err:
            raise JobStatusUnavailable(f"Sandboxes unreachable: {type(err).__name__}") from None
        if response.status_code != 200:
            raise JobStatusUnavailable(f"Sandboxes operation: HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError:
            raise JobStatusUnavailable("Sandboxes returned a malformed operation") from None
        if not isinstance(body, dict):
            raise JobStatusUnavailable("Sandboxes returned a malformed operation")
        return body

    # --- inputs --------------------------------------------------------------------------

    async def _known(self, sha: str) -> str | None:
        try:
            response = await self._request("GET", f"/v1/files/{sha}")
        except _DROPPED as err:
            raise _Transient(f"Sandboxes files: {type(err).__name__}") from None
        except httpx.HTTPError as err:
            raise JobBackendError(f"Sandboxes files: {type(err).__name__}") from None
        if response.status_code == 404:
            return None
        if response.status_code in _TRANSIENT:
            raise _Transient(f"Sandboxes files: HTTP {response.status_code}")
        if response.status_code != 200:
            raise JobBackendError(f"Sandboxes files: HTTP {response.status_code}")
        uuid = _json_dict(response, "Sandboxes files").get("uuid")
        return str(uuid) if uuid else None

    async def _send(
        self, content: bytes | AsyncIterator[bytes], sha: str, size: int | None = None
    ) -> str:
        headers = {"Content-Type": "application/octet-stream"}
        if size is not None:  # a stream is sent with its length, never chunked
            headers["Content-Length"] = str(size)
        try:
            response = await self._request("POST", "/v1/files", content=content, headers=headers)
        except _DROPPED as err:
            raise _Transient(f"Sandboxes upload: {type(err).__name__}") from None
        except httpx.HTTPError as err:
            raise JobBackendError(f"Sandboxes upload: {type(err).__name__}") from None
        if response.status_code in _TRANSIENT:
            raise _Transient(f"Sandboxes upload: HTTP {response.status_code}")
        if response.status_code not in (200, 201):
            raise JobBackendError(f"Sandboxes upload: HTTP {response.status_code}")
        body = _json_dict(response, "Sandboxes upload")
        if body.get("sha256") != sha or not body.get("uuid"):
            raise JobBackendError("Sandboxes stored another sha256 than the one sent")
        return str(body["uuid"])

    async def _retrying(self, send: Callable[[], Awaitable[str]], sha: str) -> str:
        """An upload, sent again after a transient failure (production, 2026-10-29: a 502 on a
        large overlay). Content-addressed, so a resend is safe, and a 502 can hide a file that
        was stored: it is looked up before each new attempt."""
        try:
            async with asyncio.timeout(UPLOAD_TOTAL_S):
                for wait in UPLOAD_BACKOFF_S:
                    try:
                        return await send()
                    except _Transient:
                        log.warning("Sandboxes upload failed, retrying", extra={"sha256": sha})
                    await self._sleep(wait)
                    try:
                        found = await self._known(sha)
                    except JobBackendError:  # the same outage: send again, it is safe
                        found = None
                    if found:
                        return found
                return await send()
        except TimeoutError:
            raise InputUploadError(
                f"Sandboxes upload: no success within {UPLOAD_TOTAL_S:g} s"
            ) from None

    async def _upload_bytes(self, data: bytes) -> str:
        sha = await asyncio.to_thread(lambda: hashlib.sha256(data).hexdigest())
        return await self._known(sha) or await self._retrying(lambda: self._send(data, sha), sha)

    async def _upload_local(self, fd: int) -> str:
        info = os.fstat(fd)
        if info.st_size > MAX_LOCAL_INPUT:
            raise JobBackendError(f"a run input is larger than {MAX_LOCAL_INPUT} bytes")
        key = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        cached = self._hashes.get(key)  # an overlay is sent once, then every smoke iteration
        sha = cached if cached else await asyncio.to_thread(_hash_fd, fd, info.st_size)
        self._hashes[key] = sha
        found = await self._known(sha)
        if found:
            return found

        async def send() -> str:  # each attempt reads the file again, from its start
            sent = hashlib.sha256()

            async def chunks() -> AsyncIterator[bytes]:
                os.lseek(fd, 0, os.SEEK_SET)
                left = info.st_size
                while left > 0:
                    chunk = await asyncio.to_thread(os.read, fd, min(CHUNK, left))
                    if not chunk:  # shorter than hashed: never a transient failure
                        raise JobBackendError("a run input changed during its upload")
                    sent.update(chunk)
                    left -= len(chunk)
                    yield chunk

            uuid = await self._send(chunks(), sha, info.st_size)
            if sent.hexdigest() != sha:  # rewritten between the hash and the send
                raise JobBackendError("a run input changed during its upload")
            return uuid

        return await self._retrying(send, sha)

    def _open_local(self, run_id: str, key: str) -> int | None:
        """The local run copy's file for `key`, never through a link; None if it isn't there."""
        parts = tuple(check_run_key(key).split("/"))
        try:
            fd = open_no_follow(self._local / bucket_name(run_id), parts)
        except OSError:
            return None
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return None
        return fd

    async def _input(self, run_id: str, key: str) -> str | None:
        """The uploaded file for a run key: the local copy for what the local jobs wrote, else
        RunStorage (the workspace the control plane wrote). None when neither has it."""
        fd = await asyncio.to_thread(self._open_local, run_id, key)
        if fd is not None:
            try:
                return await self._upload_local(fd)
            finally:
                os.close(fd)
        try:
            data = await self._storage.get_bytes(run_id, key, max_bytes=MAX_STORAGE_INPUT)
        except ValueError:
            raise JobBackendError(f"{key} is too large to send to Sandboxes") from None
        if data is None:
            log.warning("run input missing", extra={"run_id": run_id, "key": key})
            return None
        return await self._upload_bytes(data)

    def _plan(self, spec: JobSpec) -> tuple[list[str], str]:
        """The run keys the job reads and its output directory, from the injected manifest
        (built by our code; validated again here)."""
        if spec.kind is JobKind.EXEC:
            try:
                manifest = ExecManifest.model_validate_json(
                    spec.inject_files["/etc/p2p/manifest.json"]
                )
            except (KeyError, ValidationError):
                raise ValueError("an exec job needs its manifest") from None
            paths = manifest.paths
            wanted = [paths.overlay, paths.workspace]
            if paths.repo:
                wanted.append(paths.repo)
            wanted += [f"{paths.data_dir}/{a.name}.tar.zst" for a in manifest.archives.data]
            out_dir = paths.results
        else:
            try:
                smoke = json.loads(spec.inject_files["/etc/p2p/smoke.json"])
                archives = ArchiveDigests.model_validate(smoke["archives"])
                iteration = int(smoke["iteration"])
            except (KeyError, TypeError, ValueError, ValidationError):
                raise ValueError("a smoke job needs its manifest") from None
            staging = f"{RUN_MOUNT}/staging"
            wanted = [f"{staging}/overlay.tar.zst", f"{RUN_MOUNT}/smoke/{iteration}/workspace.tar"]
            if archives.repo is not None:
                wanted.append(f"{staging}/repo.tar.zst")
            if archives.data:
                wanted.append(f"{staging}/data/smoke.tar.zst")
            out_dir = f"{RUN_MOUNT}/smoke/{iteration}"
        keys = []
        for path in [*wanted, out_dir]:
            if not path.startswith(f"{RUN_MOUNT}/"):
                raise ValueError(f"not under {RUN_MOUNT}: {path!r}")
            keys.append(check_run_key(path.removeprefix(f"{RUN_MOUNT}/")))
        return keys[:-1], out_dir

    # --- JobBackend ----------------------------------------------------------------------

    def _check(self, spec: JobSpec) -> None:
        if spec.kind not in KINDS:
            raise ValueError(f"Sandboxes runs smoke and exec jobs only, not {spec.kind.value}")
        if spec.image_digest_ref != self._image:
            raise ValueError("Sandboxes runs the imported runner-cpu image only")
        if spec.watchdog_s + INSTANCE_SLACK_S > MAX_INSTANCE_S:
            raise ValueError(f"the watchdog does not fit the {MAX_INSTANCE_S} s instance limit")

    async def submit(self, spec: JobSpec) -> JobHandle:
        self._check(spec)
        run_id = "r_" + spec.name.split("-")[2]
        lock = self._locks.setdefault(spec.name, asyncio.Lock())
        async with lock:
            entry = await asyncio.to_thread(self._index.get, spec.name)
            if entry and entry.get("op"):
                return JobHandle(name=spec.name, nebius_id=str(entry["op"]))
            if entry:  # an intent: the control plane may have died during its spawn
                found = await self._find(spec.name, entry)
                if found:
                    await asyncio.to_thread(self._index.put, spec.name, {**entry, "op": found})
                    return JobHandle(name=spec.name, nebius_id=found)
            keys, out_dir = self._plan(spec)
            files: dict[str, dict[str, Any]] = {}
            for key in keys:
                uuid = await self._input(run_id, key)
                if uuid is not None:
                    files[f"{RUN_MOUNT}/{key}"] = self._mount(uuid)
            for path, content in spec.inject_files.items():
                files[path] = self._mount(await self._upload_bytes(content))
            entry = {
                "name": spec.name,
                "kind": spec.kind.value,
                "op": None,
                "created_at": self._clock().isoformat(),
                "marker": secrets.token_hex(16),
                "out_dir": out_dir,
                "state": None,
            }
            await asyncio.to_thread(self._index.put, spec.name, entry)
            op = await self._spawn(spec, entry, files)
            await asyncio.to_thread(self._index.put, spec.name, {**entry, "op": op})
        log.info("sandbox job submitted", extra={"job": spec.name, "operation": op})
        return JobHandle(name=spec.name, nebius_id=op)

    @staticmethod
    def _mount(uuid: str) -> dict[str, Any]:
        """Root-owned and read-only: uid 10001 reads it, never chmods or rewrites it."""
        return {"uuid": uuid, "uid": 0, "gid": 0, "mode": "0444"}

    async def _spawn(
        self, spec: JobSpec, entry: dict[str, Any], files: dict[str, dict[str, Any]]
    ) -> str:
        body = {
            "image": self._image_uuid,
            "command": job_command(
                kind=spec.kind,
                out_dir=entry["out_dir"],
                watchdog_s=spec.watchdog_s,
                marker=entry["marker"],
            ),
            "shell": True,
            "uid": 0,
            "gid": 0,
            "cwd": "/work",
            "env": {**spec.env, "P2P_JOB_NAME": spec.name},
            "networking": {"enabled": False},
            "disposable": True,
            "timeout": spec.watchdog_s + INSTANCE_SLACK_S,
            "truncate_output_at": OUTPUT_CAP,
            "files": files,
        }
        # The intent is dropped only when the spawn certainly did not happen (no connection,
        # or a 4xx refusal). After a timeout, a 5xx or an unreadable reply the instance may
        # exist: the intent stays, and the next submit looks it up before spawning again.
        try:
            response = await self._request("POST", "/v1/instances", json=body)
        except (httpx.ConnectError, httpx.ConnectTimeout) as err:
            await asyncio.to_thread(self._index.drop, spec.name)
            raise JobBackendError(
                f"{spec.name}: Sandboxes unreachable: {type(err).__name__}"
            ) from None
        except httpx.HTTPError as err:
            raise JobBackendError(f"{spec.name}: spawn unconfirmed: {type(err).__name__}") from None
        code = response.status_code
        if 400 <= code < 500:
            await asyncio.to_thread(self._index.drop, spec.name)
            if code == 429:  # the namespace's concurrency limit
                raise CapacityError(f"{spec.name}: Sandboxes instance limit reached")
            raise JobBackendError(f"{spec.name}: Sandboxes refused the spawn: HTTP {code}")
        try:
            uuid = response.json().get("uuid") if code in (200, 201) else None
        except (ValueError, AttributeError):
            uuid = None
        if not isinstance(uuid, str) or not uuid:
            raise JobBackendError(f"{spec.name}: spawn unconfirmed: HTTP {code}")
        return uuid

    async def _find(self, name: str, entry: dict[str, Any]) -> str | None:
        """The operation an intent spawned, looked up by the name in its env; None if there
        is none (it was never spawned)."""
        try:
            since = datetime.fromisoformat(str(entry["created_at"])) - LOOKBACK
        except (KeyError, ValueError):
            raise JobStatusUnavailable("an index entry without its creation time") from None
        seen: set[str] = set()
        for number in range(MAX_PAGES):
            page = await self._operations(since, number * LIST_PAGE)
            if (found := await self._named(page, name)) is not None:
                return found
            if len(page) < LIST_PAGE:
                return None
            first = str(page[0].get("uuid")) if isinstance(page[0], dict) else ""
            if first in seen:  # the API ignored the offset: never read that as "not found"
                break
            seen.add(first)
        raise JobStatusUnavailable("Sandboxes operations: no last page in sight")

    async def _operations(self, since: datetime, offset: int) -> list[Any]:
        try:
            response = await self._request(
                "GET",
                "/v1/operations",
                params={
                    "kind": "instance",
                    "since": str(int(since.timestamp())),
                    "limit": LIST_PAGE,
                    "offset": offset,
                },
            )
            if response.status_code != 200:  # never "not found": that would spawn twice
                raise JobStatusUnavailable(f"Sandboxes operations: HTTP {response.status_code}")
            listed = response.json()
        except (httpx.HTTPError, ValueError) as err:
            raise JobStatusUnavailable(f"Sandboxes operations: {type(err).__name__}") from None
        if isinstance(listed, dict):
            listed = listed.get("operations") or listed.get("items") or []
        if not isinstance(listed, list):
            raise JobStatusUnavailable("Sandboxes returned a malformed operation list")
        return listed

    async def _named(self, listed: list[Any], name: str) -> str | None:
        for item in listed:
            uuid = item.get("uuid") if isinstance(item, dict) else None
            if not isinstance(uuid, str):
                continue
            meta = item.get("metadata")  # when the list carries it: no call per operation
            if not isinstance(meta, dict) or "env" not in meta:
                meta = (await self._get_op(uuid)).get("metadata") or {}
            env = meta.get("env") if isinstance(meta, dict) else None
            if isinstance(env, dict) and env.get("P2P_JOB_NAME") == name:
                return uuid
        return None

    async def status(self, handle: JobHandle) -> JobState:
        lock = self._locks.setdefault(handle.name, asyncio.Lock())
        async with lock:  # two polls never harvest the same job at once
            entry = await asyncio.to_thread(self._index.get, handle.name)
            if entry and entry.get("state"):
                return JobState(entry["state"])
            op = await self._get_op(handle.nebius_id)
            status = op.get("status")
            if status in _STATES:
                return _STATES[status]
            if status == "CANCELLED":  # its partial results, if it got to harvest them
                await self._harvest(handle, entry, op)
                state = JobState.CANCELLED
            elif status in _DONE:
                state = await self._harvest(handle, entry, op)
            else:
                return JobState.UNKNOWN
            if entry:
                await asyncio.to_thread(
                    self._index.put, handle.name, {**entry, "state": state.value}
                )
            return state

    async def _harvest(
        self, handle: JobHandle, entry: dict[str, Any] | None, op: dict[str, Any]
    ) -> JobState:
        """Write the job's outputs to the run bucket; SUCCEEDED only for exit code 0 with a
        harvest (FAILED otherwise, partial results kept)."""
        result = (op.get("metadata") or {}).get("result") or {}
        state = result.get("state") or {}
        code = state.get("exit_code")
        text = _text(result.get("stdout"))
        if entry is None or not _MARKER.fullmatch(str(entry.get("marker"))):
            return JobState.FAILED  # pragma: no cover - every submission writes its entry
        kind = "exec" if entry.get("kind") == JobKind.EXEC.value else "smoke"
        loop = asyncio.get_running_loop()
        files = await loop.run_in_executor(self._pool, parse_harvest, text, entry["marker"], kind)
        ok = op.get("status") in ("SUCCESS", "SUCCEEDED") and code == 0
        ok = ok and not state.get("timed_out")
        if not ok:  # numbers only: the output is untrusted text (kept below, never logged)
            log.warning(
                "sandbox job failed: exit code %s, timed out %s, operation %s",
                code,
                bool(state.get("timed_out")),
                str(op.get("status"))[:16],
                extra={"job": handle.name},
            )
        if not files:  # None or empty: the job wrote nothing the run can use
            log.warning("sandbox job left no harvest", extra={"job": handle.name})
            return JobState.FAILED
        run_id = "r_" + handle.name.split("-")[2]
        prefix = check_run_key(str(entry["out_dir"]).removeprefix(f"{RUN_MOUNT}/"))
        stderr = _text(result.get("stderr"))[-INSTANCE_LOG_BYTES:].encode("utf-8", "replace")
        outputs = {**files, **({"instance.log": stderr} if stderr and not ok else {})}
        try:
            for name, data in outputs.items():
                await self._storage.put_bytes(run_id, f"{prefix}/{name}", data)
        except Exception as err:  # the bucket, not the job: ask again at the next poll
            raise JobStatusUnavailable(f"harvest not written: {type(err).__name__}") from None
        return JobState.SUCCEEDED if ok else JobState.FAILED

    async def cancel(self, handle: JobHandle) -> None:
        try:
            response = await self._request("DELETE", f"/v1/operations/{handle.nebius_id}")
        except httpx.HTTPError as err:
            raise JobStatusUnavailable(f"Sandboxes cancel: {type(err).__name__}") from None
        if response.status_code not in (200, 202, 204, 404, 409):  # gone or already done
            raise JobStatusUnavailable(f"Sandboxes cancel: HTTP {response.status_code}")

    async def list_jobs(self, prefix: str) -> list[tuple[JobHandle, JobState, datetime]]:
        """Every indexed job named `prefix*` with its state (UNKNOWN during an outage); an
        intent never spawned is forgotten once stale."""
        listed = []
        for name in await asyncio.to_thread(self._index.names, prefix):
            entry = await asyncio.to_thread(self._index.get, name)
            if entry is None:  # pragma: no cover - removed between the listing and the read
                continue
            created = datetime.fromisoformat(str(entry["created_at"]))
            if not entry.get("op"):  # an unconfirmed spawn: adopt it if it exists
                try:
                    found = await self._find(name, entry)
                except JobStatusUnavailable:
                    continue
                if found is None:
                    if self._clock() - created > STALE_INTENT:
                        await asyncio.to_thread(self._index.drop, name)
                    continue
                entry = {**entry, "op": found}
                await asyncio.to_thread(self._index.put, name, entry)
            handle = JobHandle(name=name, nebius_id=str(entry["op"]))
            try:
                state = await self.status(handle)
            except JobStatusUnavailable:
                state = JobState.UNKNOWN
            listed.append((handle, state, created))
        return listed

    async def forget(self, run_id: str) -> None:
        """Drop the run's index entries (its bucket is being deleted)."""
        slug = run_slug(run_id)
        for kind in KINDS:
            for name in await asyncio.to_thread(self._index.names, f"p2p-{kind.value}-{slug}-"):
                with contextlib.suppress(OSError):
                    await asyncio.to_thread(self._index.drop, name)

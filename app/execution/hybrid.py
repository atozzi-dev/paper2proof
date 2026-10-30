"""The hybrid deploy (docs/specs/hybrid-deploy.md): provision and fetch jobs run in local docker
on the control-plane host, and what they wrote reaches the Nebius run bucket the GPU jobs mount.

The local run directory was written by the provisioner image (our code) from untrusted sources
(PyPI wheels, a repository clone, parquet files): the upload never follows a link, sends only
regular files, and refuses a copy over its bounds before anything is sent."""

import asyncio
import errno
import hashlib
import logging
import os
import stat
from collections.abc import Callable
from concurrent.futures import Executor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO, TYPE_CHECKING, BinaryIO, Protocol, cast

from botocore.exceptions import BotoCoreError, ClientError

from app.execution.backend import (
    JobBackend,
    JobHandle,
    JobKind,
    JobSpec,
    JobState,
    JobStatusUnavailable,
)
from app.execution.naming import bucket_name
from app.execution.run_storage import MAX_READ_BYTES, RunStorage
from app.execution.s3 import check_run_key

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mypy_boto3_s3 import S3Client
else:
    S3Client = object

log = logging.getLogger(__name__)
MAX_UPLOAD_FILES = 10_000
MAX_UPLOAD_BYTES = 30 * 2**30
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_BINARY = getattr(os, "O_BINARY", 0)


class UploadRefused(RuntimeError):
    """The local run copy cannot be uploaded as it is: the job that wrote it fails."""


# POSIX: every component is opened relative to its parent with O_NOFOLLOW, so a directory a
# still-running job swaps for a link between the scan and the upload is never followed. Windows
# (dev only; the hybrid deploy is a Linux host) checks paths instead.
_DIR_FD = os.open in os.supports_dir_fd and os.stat in os.supports_dir_fd
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
# what a link, or a file or directory swapped or removed under the walk, raises
_SWAPPED = frozenset({errno.ELOOP, errno.ENOTDIR, errno.ENOENT, errno.EMLINK})
CHUNK = 1 << 20


@dataclass(frozen=True)
class _Entry:
    key: str
    parts: tuple[str, ...]
    dev: int
    ino: int
    size: int


def _open_dir(parent: int | None, name: str) -> int:
    flags = os.O_RDONLY | _DIRECTORY | _NOFOLLOW
    return os.open(name, flags, dir_fd=parent) if parent is not None else os.open(name, flags)


def _scan(root: Path) -> list[_Entry]:
    """Every regular file under `root`; links, FIFOs and devices are skipped, never followed.
    Bounded before anything is sent."""
    found: list[_Entry] = []
    total = 0

    def add(parts: tuple[str, ...], info: os.stat_result) -> None:
        nonlocal total
        key = "/".join(parts)
        try:
            check_run_key(key)
        except ValueError:
            raise UploadRefused(f"not a safe object key: {key!r}") from None
        found.append(_Entry(key, parts, info.st_dev, info.st_ino, info.st_size))
        total += info.st_size
        if len(found) > MAX_UPLOAD_FILES:
            raise UploadRefused(f"the run copy has more than {MAX_UPLOAD_FILES} files")
        if total > MAX_UPLOAD_BYTES:
            raise UploadRefused(f"the run copy has more than {MAX_UPLOAD_BYTES} bytes")

    def walk_fd(fd: int, prefix: tuple[str, ...]) -> None:
        for name in sorted(os.listdir(fd)):
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = _open_dir(fd, name)
                try:
                    walk_fd(child, (*prefix, name))
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                add((*prefix, name), info)

    def walk_path(folder: Path, prefix: tuple[str, ...]) -> None:
        with os.scandir(folder) as entries:
            for entry in sorted(entries, key=lambda e: e.name):
                if entry.is_symlink() or entry.is_junction():
                    continue
                info = Path(entry.path).lstat()  # with the inode (scandir has none on Windows)
                if stat.S_ISDIR(info.st_mode):
                    walk_path(Path(entry.path), (*prefix, entry.name))
                elif stat.S_ISREG(info.st_mode):
                    add((*prefix, entry.name), info)

    try:
        if _DIR_FD:
            fd = _open_dir(None, str(root))
            try:
                walk_fd(fd, ())
            finally:
                os.close(fd)
        else:
            walk_path(root, ())
    except OSError as err:
        if err.errno in _SWAPPED:  # the copy changed under the scan: refused, never retried
            raise UploadRefused("the run copy changed while it was scanned") from None
        raise
    return found


def _open(root: Path, entry: _Entry) -> int:
    """The scanned file, reached without following any link; refused if it is not the file
    the scan saw (another inode, another size: something rewrote it in between)."""
    try:
        return _open_checked(root, entry)
    except OSError as err:
        if err.errno in _SWAPPED:  # a link where the scan saw a directory or a file
            raise UploadRefused(f"{entry.key} changed after the scan") from None
        raise


def open_no_follow(root: Path, parts: tuple[str, ...]) -> int:
    """`root/parts...` opened read-only without following a link at any component below
    `root` (OSError otherwise, ELOOP or ENOTDIR on POSIX). The caller checks what it got."""
    flags = os.O_RDONLY | _NOFOLLOW | _NONBLOCK | _BINARY
    if _DIR_FD:
        fd = _open_dir(None, str(root))
        try:
            for part in parts[:-1]:
                child = _open_dir(fd, part)
                os.close(fd)
                fd = child
            return os.open(parts[-1], flags, dir_fd=fd)
        finally:
            os.close(fd)
    path = root.joinpath(*parts)
    if any(p.is_symlink() for p in [path, *path.parents] if p != root and root in p.parents):
        raise OSError(errno.ELOOP, "a link in the run copy", str(path))
    return os.open(path, flags)


def _open_checked(root: Path, entry: _Entry) -> int:
    file = open_no_follow(root, entry.parts)
    info = os.fstat(file)
    if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino, info.st_size) != (
        entry.dev,
        entry.ino,
        entry.size,
    ):
        os.close(file)
        raise UploadRefused(f"{entry.key} changed after the scan")
    return file


class _Bounded:
    """Reads at most `size` bytes: a file that grows after the check is sent as checked."""

    def __init__(self, handle: BinaryIO, size: int) -> None:
        self._handle = handle
        self._left = size
        self.digest = hashlib.sha256()  # of what was actually read (and sent)

    def read(self, n: int = -1) -> bytes:
        want = self._left if n is None or n < 0 else min(n, self._left)
        data = self._handle.read(want) if want > 0 else b""
        self._left -= len(data)
        self.digest.update(data)
        return data


def _sha256(handle: BinaryIO, size: int) -> str:
    digest = hashlib.sha256()
    reader = _Bounded(handle, size)
    while chunk := reader.read(CHUNK):
        digest.update(chunk)
    return digest.hexdigest()


def _remote_sha256(client: S3Client, bucket: str, key: str) -> str | None:
    """The sha256 we stored with the object, None when it is not there."""
    try:
        head = client.head_object(Bucket=bucket, Key=key)
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return None
        raise
    value = head.get("Metadata", {}).get("sha256")
    return str(value) if value else None


def upload_run_dir(client: S3Client, bucket: str, root: Path) -> int:
    """Upload the local run copy to `bucket` under the same keys; an object already there with
    the same sha256 is skipped (a resumed step, a poll after success). Returns the files sent.
    Blocking: call it in a thread."""
    sent = 0
    for entry in _scan(root):
        remote = _remote_sha256(client, bucket, entry.key)
        fd = _open(root, entry)
        with os.fdopen(fd, "rb", buffering=0) as handle:  # every read reaches the file
            local = _sha256(handle, entry.size)
            if remote == local:
                continue
            handle.seek(0)
            reader = _Bounded(handle, entry.size)
            client.upload_fileobj(
                cast("IO[bytes]", reader), bucket, entry.key,
                ExtraArgs={"Metadata": {"sha256": local}},
            )  # fmt: skip
            if reader.digest.hexdigest() != local:  # rewritten in place between hash and send
                client.delete_object(Bucket=bucket, Key=entry.key)
                raise UploadRefused(f"{entry.key} changed during the upload")
        sent += 1
    return sent


LOCAL_KINDS = frozenset({JobKind.PROVISION, JobKind.FETCH})
_PREFIXES = {kind: f"p2p-{kind.value}-" for kind in JobKind}
Upload = Callable[[str, Path], int]  # (bucket, local run directory) -> files sent; blocking


class SandboxJobs(Protocol):
    """The Sandboxes job backend (app/execution/sandbox_jobs.py): smoke and exec jobs only."""

    async def submit(self, spec: JobSpec) -> JobHandle: ...
    async def status(self, handle: JobHandle) -> JobState: ...
    async def cancel(self, handle: JobHandle) -> None: ...
    async def list_jobs(self, prefix: str) -> list[tuple[JobHandle, JobState, datetime]]: ...
    async def forget(self, run_id: str) -> None: ...
    def bind_cpu_pool(self, pool: Executor | None) -> None: ...


def _kind(name: str) -> JobKind:
    for kind, prefix in _PREFIXES.items():
        if name.startswith(prefix):
            return kind
    raise ValueError(f"not a job name: {name!r}")


class HybridJobBackend:
    """Provision and fetch jobs on this host (`local`, a provisioner-only DevDockerBackend),
    exec and smoke jobs in Sandboxes (`sandbox`, the default) or on Nebius (`remote`). A local
    job is SUCCEEDED only once its output is in the Nebius run bucket. There is no gateway: a
    Sandboxes job has no network, a GPU job only the exec subnet's route
    (docs/specs/hybrid-deploy.md). Buckets are always the Nebius ones and their local copies."""

    def __init__(
        self,
        *,
        local: JobBackend,
        remote: JobBackend,
        local_buckets: Path,
        upload: Upload,
        sandbox: SandboxJobs | None = None,
    ) -> None:
        self._local = local
        self._remote = remote
        self._sandbox = sandbox
        self._buckets = local_buckets
        self._upload = upload
        self._uploaded: set[str] = set()  # job names whose output is in the run bucket
        self._locks: dict[str, asyncio.Lock] = {}

    def bind_cpu_pool(self, pool: Executor | None) -> None:
        if self._sandbox is not None:
            self._sandbox.bind_cpu_pool(pool)

    def _for(self, name: str) -> JobBackend | SandboxJobs:
        if _kind(name) in LOCAL_KINDS:
            return self._local
        return self._sandbox or self._remote

    async def ensure_run_bucket(self, run_id: str) -> str:
        await self._local.ensure_run_bucket(run_id)
        return await self._remote.ensure_run_bucket(run_id)

    async def delete_run_bucket(self, run_id: str) -> None:
        await self._remote.delete_run_bucket(run_id)
        await self._local.delete_run_bucket(run_id)
        if self._sandbox is not None:
            await self._sandbox.forget(run_id)

    async def submit(self, spec: JobSpec) -> JobHandle:
        return await self._for(spec.name).submit(spec)

    async def status(self, handle: JobHandle) -> JobState:
        state = await self._for(handle.name).status(handle)
        if _kind(handle.name) not in LOCAL_KINDS:
            return state
        if state is JobState.FAILED:  # its error report says why: best effort, once
            await self._report_failure(handle)
            return state
        if state is not JobState.SUCCEEDED:
            return state
        lock = self._locks.setdefault(handle.name, asyncio.Lock())
        async with lock:  # two polls never upload the same copy at once
            if handle.name in self._uploaded:
                return state
            bucket = bucket_name("r_" + handle.name.split("-")[2])
            try:
                sent = await asyncio.to_thread(self._upload, bucket, self._buckets / bucket)
            except UploadRefused as err:
                log.warning("run copy not uploaded: %s", err, extra={"job": handle.name})
                return JobState.FAILED
            except (OSError, ClientError, BotoCoreError) as err:  # unreachable: ask again later
                raise JobStatusUnavailable(f"upload of {bucket}: {type(err).__name__}") from None
            self._uploaded.add(handle.name)
            log.info("run copy uploaded", extra={"job": handle.name, "files": sent})
        return state

    async def _report_failure(self, handle: JobHandle) -> None:
        if handle.name in self._uploaded:
            return
        self._uploaded.add(handle.name)
        bucket = bucket_name("r_" + handle.name.split("-")[2])
        try:
            await asyncio.to_thread(self._upload, bucket, self._buckets / bucket)
        except (UploadRefused, OSError, ClientError, BotoCoreError) as err:
            log.warning("failed job's copy not uploaded: %s", type(err).__name__)

    async def cancel(self, handle: JobHandle) -> None:
        await self._for(handle.name).cancel(handle)

    async def ensure_gateway_up(self) -> None:
        return None  # no gateway VM: the exec subnet's route has no next hop (fail-closed)

    async def gateway_idle_check(self, idle_minutes: int = 30) -> bool:
        return False

    async def list_jobs(self, prefix: str) -> list[tuple[JobHandle, JobState, datetime]]:
        sandboxed = await self._sandbox.list_jobs(prefix) if self._sandbox is not None else []
        return [
            *await self._local.list_jobs(prefix),
            *await self._remote.list_jobs(prefix),
            *sandboxed,
        ]

    async def list_run_buckets(self) -> list[tuple[str, datetime]]:
        """Both copies, by name, with the older creation time (the reaper's age check)."""
        merged: dict[str, datetime] = {}
        for name, created in [
            *await self._remote.list_run_buckets(),
            *await self._local.list_run_buckets(),
        ]:
            merged[name] = min(created, merged.get(name, created))
        return sorted(merged.items())


class HybridRunStorage:
    """The Nebius run bucket for every read and write (what the GPU jobs mount); a delete also
    removes the key from the local copy, or the next upload would bring the old file back."""

    def __init__(self, remote: RunStorage, local: RunStorage) -> None:
        self._remote = remote
        self._local = local

    async def get_bytes(
        self, run_id: str, key: str, *, max_bytes: int = MAX_READ_BYTES
    ) -> bytes | None:
        return await self._remote.get_bytes(run_id, key, max_bytes=max_bytes)

    async def put_bytes(self, run_id: str, key: str, data: bytes) -> None:
        await self._remote.put_bytes(run_id, key, data)

    async def delete(self, run_id: str, key: str) -> None:
        await self._local.delete(run_id, key)
        await self._remote.delete(run_id, key)

"""The hybrid deploy (docs/specs/hybrid-deploy.md): provision and fetch jobs run in local docker
on the control-plane host, and what they wrote reaches the Nebius run bucket the GPU jobs mount.

The local run directory was written by the provisioner image (our code) from untrusted sources
(PyPI wheels, a repository clone, parquet files): the upload never follows a link, sends only
regular files, and refuses a copy over its bounds before anything is sent."""

import asyncio
import logging
import os
import stat
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

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


def _files(root: Path) -> list[tuple[str, Path, int]]:
    """(key, path, size) of every regular file under `root`; links, FIFOs and devices are
    skipped, never followed. Bounded before anything is sent."""
    found: list[tuple[str, Path, int]] = []
    total = 0
    pending = [(root, "")]
    while pending:
        folder, prefix = pending.pop()
        with os.scandir(folder) as entries:
            for entry in entries:
                key = f"{prefix}{entry.name}"
                if entry.is_symlink() or entry.is_junction():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append((Path(entry.path), key + "/"))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue  # a FIFO, a socket, a device
                try:
                    check_run_key(key)
                except ValueError:
                    raise UploadRefused(f"not a safe object key: {key!r}") from None
                size = entry.stat(follow_symlinks=False).st_size
                found.append((key, Path(entry.path), size))
                total += size
                if len(found) > MAX_UPLOAD_FILES:
                    raise UploadRefused(f"the run copy has more than {MAX_UPLOAD_FILES} files")
                if total > MAX_UPLOAD_BYTES:
                    raise UploadRefused(f"the run copy has more than {MAX_UPLOAD_BYTES} bytes")
    return sorted(found)


def _remote_size(client: S3Client, bucket: str, key: str) -> int | None:
    try:
        return int(client.head_object(Bucket=bucket, Key=key)["ContentLength"])
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def upload_run_dir(client: S3Client, bucket: str, root: Path) -> int:
    """Upload the local run copy to `bucket` under the same keys; a key already there with the
    same size is skipped (a resumed step, a poll after success). Returns the files sent.
    Blocking: call it in a thread."""
    sent = 0
    for key, path, size in _files(root):
        if _remote_size(client, bucket, key) == size:
            continue
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW | _NONBLOCK | _BINARY)
        with os.fdopen(fd, "rb") as handle:  # the file checked above, never a swapped link
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise UploadRefused(f"{key} is no longer a regular file")
            client.upload_fileobj(handle, bucket, key)  # multipart for large files
        sent += 1
    return sent


LOCAL_KINDS = frozenset({JobKind.PROVISION, JobKind.FETCH})
_PREFIXES = {kind: f"p2p-{kind.value}-" for kind in JobKind}
Upload = Callable[[str, Path], int]  # (bucket, local run directory) -> files sent; blocking


def _kind(name: str) -> JobKind:
    for kind, prefix in _PREFIXES.items():
        if name.startswith(prefix):
            return kind
    raise ValueError(f"not a job name: {name!r}")


class HybridJobBackend:
    """Provision and fetch jobs on this host (`local`, a provisioner-only DevDockerBackend),
    exec and smoke jobs on Nebius (`remote`). A local job is SUCCEEDED only once its output is
    in the Nebius run bucket. There is no gateway: exec isolation comes from the exec subnet's
    route (docs/specs/hybrid-deploy.md)."""

    def __init__(
        self, *, local: JobBackend, remote: JobBackend, local_buckets: Path, upload: Upload
    ) -> None:
        self._local = local
        self._remote = remote
        self._buckets = local_buckets
        self._upload = upload
        self._uploaded: set[str] = set()  # job names whose output is in the run bucket
        self._locks: dict[str, asyncio.Lock] = {}

    def _for(self, name: str) -> JobBackend:
        return self._local if _kind(name) in LOCAL_KINDS else self._remote

    async def ensure_run_bucket(self, run_id: str) -> str:
        await self._local.ensure_run_bucket(run_id)
        return await self._remote.ensure_run_bucket(run_id)

    async def delete_run_bucket(self, run_id: str) -> None:
        await self._remote.delete_run_bucket(run_id)
        await self._local.delete_run_bucket(run_id)

    async def submit(self, spec: JobSpec) -> JobHandle:
        return await self._for(spec.name).submit(spec)

    async def status(self, handle: JobHandle) -> JobState:
        state = await self._for(handle.name).status(handle)
        if state is not JobState.SUCCEEDED or _kind(handle.name) not in LOCAL_KINDS:
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

    async def cancel(self, handle: JobHandle) -> None:
        await self._for(handle.name).cancel(handle)

    async def ensure_gateway_up(self) -> None:
        return None  # no gateway VM: the exec subnet's route has no next hop (fail-closed)

    async def gateway_idle_check(self, idle_minutes: int = 30) -> bool:
        return False

    async def list_jobs(self, prefix: str) -> list[tuple[JobHandle, JobState, datetime]]:
        return [*await self._local.list_jobs(prefix), *await self._remote.list_jobs(prefix)]

    async def list_run_buckets(self) -> list[tuple[str, datetime]]:
        """Both copies, by name, with the older creation time (the reaper's age check)."""
        merged: dict[str, datetime] = {}
        for name, created in [
            *await self._remote.list_run_buckets(),
            *await self._local.list_run_buckets(),
        ]:
            merged[name] = min(created, merged.get(name, created))
        return sorted(merged.items())

"""The hybrid deploy (docs/specs/hybrid-deploy.md): provision and fetch jobs run in local docker
on the control-plane host, and what they wrote reaches the Nebius run bucket the GPU jobs mount.

The local run directory was written by the provisioner image (our code) from untrusted sources
(PyPI wheels, a repository clone, parquet files): the upload never follows a link, sends only
regular files, and refuses a copy over its bounds before anything is sent."""

import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING

from botocore.exceptions import ClientError

from app.execution.s3 import check_run_key

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mypy_boto3_s3 import S3Client
else:
    S3Client = object

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

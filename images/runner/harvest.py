"""Harvest of a Sandboxes job's outputs (docs/specs/hybrid-deploy.md, "Experiments in Sandboxes").
Standard library only; runs as uid 10001 after the job's processes were killed.

A Sandboxes instance mounts no bucket, so the files the job would have written to the run bucket
travel back on the instance's stdout: a base64 tar of the allowed files of one directory, between
`<marker>` and `<marker>-END` lines. Files are opened without following links and must be
regular. JSON files keep their first cap + 1 bytes (a reader still sees "too large"), logs their
last cap bytes. Everything here was written next to untrusted code: the control plane parses it
with the same rules.
"""

from __future__ import annotations

import argparse
import base64
import io
import os
import re
import stat
import sys
import tarfile
from collections.abc import Sequence
from pathlib import Path

MARKER = re.compile(r"^[0-9a-f]{32}$")
KiB = 1024
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
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_POSIX = os.open in os.supports_dir_fd


def _read(fd: int, size: int, cap: int, *, tail: bool) -> bytes:
    if tail and size > cap:
        os.lseek(fd, size - cap, os.SEEK_SET)
    limit = cap if tail else cap + 1
    chunks, total = [], 0
    while total < limit:
        chunk = os.read(fd, min(1 << 20, limit - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def _collect(directory: Path, caps: dict[str, int]) -> dict[str, bytes]:
    found: dict[str, bytes] = {}
    if not _POSIX and (directory.is_symlink() or not directory.is_dir()):
        return found  # dev machines without dir_fd (Windows): the image is POSIX
    try:
        dir_fd = os.open(directory, os.O_RDONLY | _DIRECTORY | _NOFOLLOW) if _POSIX else None
    except OSError:
        return found
    try:
        for name, cap in caps.items():
            path = name if dir_fd is not None else directory / name
            try:
                fd = os.open(path, os.O_RDONLY | _NOFOLLOW | _NONBLOCK, dir_fd=dir_fd)
            except OSError:
                continue  # missing, a link, or not openable: never followed
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    continue
                found[name] = _read(fd, info.st_size, cap, tail=name.endswith(".log"))
            finally:
                os.close(fd)
    finally:
        if dir_fd is not None:
            os.close(dir_fd)
    return found


def harvest(directory: Path, kind: str, marker: str) -> str:
    """The text to print: `\\n<marker>\\n<base64 tar>\\n<marker>-END\\n`."""
    if not MARKER.fullmatch(marker):
        raise ValueError("the marker must be 32 lowercase hex characters")
    caps = CAPS.get(kind)
    if caps is None:
        raise ValueError(f"unknown kind: {kind!r}")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name, data in _collect(directory, caps).items():
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o644, 0
            tar.addfile(info, io.BytesIO(data))
    payload = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"\n{marker}\n{payload}\n{marker}-END\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Paper2Proof Sandboxes harvest")
    parser.add_argument("--dir", required=True)
    parser.add_argument("--kind", required=True)
    parser.add_argument("--marker", required=True)
    args = parser.parse_args(argv)
    try:
        text = harvest(Path(args.dir), args.kind, args.marker)
    except ValueError as err:
        print(f"harvest: {err}", file=sys.stderr)
        return 2
    sys.stdout.write(text)
    sys.stdout.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

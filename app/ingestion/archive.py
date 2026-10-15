"""Safe, in-memory unpacking of arXiv e-print sources. Nothing is written to disk and nothing
is executed: members are read as bytes after the path and type checks."""

import io
import tarfile
import zlib
from dataclasses import dataclass, field
from typing import Literal

MAX_BYTES = 50 * 1024 * 1024
MAX_FILES = 2000


class ArchiveError(ValueError):
    """The source is malformed, hostile or over the caps."""


@dataclass(frozen=True)
class SourceFiles:
    kind: Literal["latex", "pdf"]
    files: dict[str, bytes] = field(default_factory=dict)
    pdf: bytes | None = None


def _safe_name(name: str) -> str:
    if (
        not name
        or name.startswith(("/", "\\"))
        or "\\" in name
        or (len(name) > 1 and name[1] == ":")
        or ".." in name.split("/")
        or "\x00" in name
    ):
        raise ArchiveError(f"unsafe path in archive: {name!r}")
    return name.removeprefix("./")


def _gunzip_capped(data: bytes, max_bytes: int) -> bytes:
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        out = decompressor.decompress(data, max_bytes + 1)
    except zlib.error as err:
        raise ArchiveError("corrupt gzip data") from err
    if len(out) > max_bytes or decompressor.unconsumed_tail:
        raise ArchiveError("source too large")
    return out


def _from_tar(raw: bytes, max_bytes: int, max_files: int) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    total = 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tar:
        for member in tar:
            name = _safe_name(member.name)
            if member.isdir():
                continue
            if not member.isreg():  # symlinks, hardlinks, devices, fifos
                raise ArchiveError(f"non-regular file in archive: {name!r}")
            if len(files) >= max_files:
                raise ArchiveError(f"too many files in archive (max {max_files})")
            total += member.size
            if total > max_bytes:
                raise ArchiveError("source too large")
            extracted = tar.extractfile(member)
            files[name] = extracted.read() if extracted else b""
    return files


def extract_source(
    data: bytes, *, max_bytes: int = MAX_BYTES, max_files: int = MAX_FILES
) -> SourceFiles:
    if data.startswith(b"%PDF"):
        return SourceFiles(kind="pdf", pdf=data)
    if not data.startswith(b"\x1f\x8b"):
        raise ArchiveError("unrecognised source format")
    raw = _gunzip_capped(data, max_bytes)
    if tarfile.is_tarfile(io.BytesIO(raw)):
        try:
            return SourceFiles(kind="latex", files=_from_tar(raw, max_bytes, max_files))
        except tarfile.TarError as err:
            raise ArchiveError("corrupt tar archive") from err
    if raw.startswith(b"%PDF"):
        return SourceFiles(kind="pdf", pdf=raw)
    return SourceFiles(kind="latex", files={"main.tex": raw})

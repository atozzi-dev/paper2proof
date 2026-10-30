"""Verify a Proof Bundle (docs/specs/proof-bundle.md §5). Everything in the bundle is untrusted:
it is read in memory with caps, never extracted, and only the public key decides.

    uv run python -m app.bundle.verify proof-bundle-<run_id>.tar.gz [--public-key PEM]
"""

import argparse
import gzip
import hashlib
import io
import json
import re
import sys
import tarfile
import zlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.bundle.sign import check_signature

ROOT = "bundle"
MANIFEST = "MANIFEST.json"
SIGNATURE = "SIGNATURE"
MAX_MEMBERS = 512
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_BYTES = 300 * 1024 * 1024
DEFAULT_PUBLIC_KEY = Path(__file__).resolve().parents[2] / "docs" / "bundle-public-key.pem"
_PATH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*(/[A-Za-z0-9_][A-Za-z0-9_.-]*){0,7}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


MAX_STREAM_BYTES = MAX_TOTAL_BYTES + 16 * 1024 * 1024  # file data + tar/PAX headers


class BundleError(Exception):
    """The archive itself is unreadable or unsafe."""


class _Bounded(io.RawIOBase):
    """Decompressed bytes, capped: a gzip bomb or a huge PAX header stops at the cap."""

    def __init__(self, inner: gzip.GzipFile, limit: int) -> None:
        self._inner = inner
        self._left = limit

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        view = memoryview(buffer).cast("B")
        chunk = self._inner.read(min(len(view), self._left + 1))
        if len(chunk) > self._left:
            raise BundleError(f"more than {MAX_STREAM_BYTES} bytes once decompressed")
        self._left -= len(chunk)
        view[: len(chunk)] = chunk
        return len(chunk)


def printable(text: str) -> str:
    """Bundle text shown on a terminal: no control characters (ANSI, CR, bidi)."""
    return "".join(c if c.isprintable() else "?" for c in text)


def safe_path(path: str) -> bool:
    return bool(_PATH.fullmatch(path)) and ".." not in path.split("/") and len(path) <= 255


def read_archive(data: bytes) -> dict[str, bytes]:
    """Members of a bundle tar.gz, relative to bundle/. Regular files only, no duplicates."""
    files: dict[str, bytes] = {}
    total = 0
    try:
        stream = _Bounded(gzip.GzipFile(fileobj=io.BytesIO(data)), MAX_STREAM_BYTES)
        with tarfile.open(fileobj=io.BufferedReader(stream), mode="r|") as tar:
            for count, info in enumerate(tar, start=1):
                if count > MAX_MEMBERS:
                    raise BundleError(f"more than {MAX_MEMBERS} members")
                if not info.isreg():
                    raise BundleError(f"{info.name}: not a regular file")
                prefix, _, rel = info.name.partition("/")
                if prefix != ROOT or not safe_path(rel):
                    raise BundleError(f"{info.name}: outside {ROOT}/ or an unsafe path")
                if rel in files:
                    raise BundleError(f"{info.name}: duplicate member")
                total += info.size
                if total > MAX_TOTAL_BYTES:
                    raise BundleError(f"more than {MAX_TOTAL_BYTES} bytes")
                member = tar.extractfile(info)
                if member is None:  # pragma: no cover - regular files always have data
                    raise BundleError(f"{info.name}: unreadable")
                files[rel] = member.read(info.size + 1)
                if len(files[rel]) != info.size:
                    raise BundleError(f"{info.name}: truncated")
    except (tarfile.TarError, OSError, EOFError, zlib.error, gzip.BadGzipFile) as err:
        raise BundleError(f"not a readable tar.gz: {type(err).__name__}") from None
    if not files:
        raise BundleError("empty archive")
    return files


def read_directory(root: Path) -> dict[str, bytes]:
    """An extracted bundle/ directory (or its parent). No links, same caps as the archive."""
    if not (root / MANIFEST).is_file() and (root / ROOT / MANIFEST).is_file():
        root = root / ROOT
    files: dict[str, bytes] = {}
    total = 0
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise BundleError(f"{rel}: symbolic link")
        if path.is_dir():
            continue
        if not path.is_file() or not safe_path(rel):
            raise BundleError(f"{rel}: not a regular file or an unsafe path")
        if len(files) >= MAX_MEMBERS:
            raise BundleError(f"more than {MAX_MEMBERS} files")
        with path.open("rb") as handle:  # bounded: sizes can change after a stat
            data = handle.read(MAX_TOTAL_BYTES - total + 1)
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise BundleError(f"more than {MAX_TOTAL_BYTES} bytes")
        files[rel] = data
    return files


def read_bundle(path: Path) -> dict[str, bytes]:
    if path.is_dir():
        return read_directory(path)
    if path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise BundleError(f"larger than {MAX_ARCHIVE_BYTES} bytes")
    return read_archive(path.read_bytes())


def _listed(manifest: Any) -> tuple[dict[str, Mapping[str, Any]], list[str]]:
    problems: list[str] = []
    listed = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(listed, dict):
        return {}, [f"{MANIFEST}: no files table"]
    entries: dict[str, Mapping[str, Any]] = {}
    for path, entry in listed.items():
        if not isinstance(path, str) or not safe_path(path) or path in (MANIFEST, SIGNATURE):
            problems.append(f"{path}: not a valid bundle path")
        elif not isinstance(entry, dict) or not _SHA256.fullmatch(str(entry.get("sha256"))):
            problems.append(f"{path}: no valid sha256 in {MANIFEST}")
        else:
            entries[path] = entry
    return entries, problems


def verify_files(
    files: Mapping[str, bytes], public_key_pem: bytes | None, *, allow_unsigned: bool = False
) -> list[str]:
    """Problems, one per line and each starting with the file it is about; [] when valid."""
    raw = files.get(MANIFEST)
    if raw is None:
        return [f"{MANIFEST}: missing"]
    try:
        manifest: Any = json.loads(raw)
    except (ValueError, RecursionError):
        return [f"{MANIFEST}: not JSON"]
    entries, problems = _listed(manifest)
    for path, entry in sorted(entries.items()):
        data = files.get(path)
        if data is None:
            problems.append(f"{path}: missing")
        elif entry.get("bytes") != len(data) or hashlib.sha256(data).hexdigest() != entry["sha256"]:
            problems.append(f"{path}: content differs from {MANIFEST} (sha256 mismatch)")
    for path in sorted(set(files) - set(entries) - {MANIFEST, SIGNATURE}):
        problems.append(f"{path}: not listed in {MANIFEST}")
    signature = files.get(SIGNATURE)
    if signature is None:
        if not allow_unsigned:
            problems.append(f"{SIGNATURE}: missing (unsigned bundle)")
    elif public_key_pem is None:
        problems.append(f"{SIGNATURE}: no public key to check it with")
    else:
        problem = check_signature(signature, raw, public_key_pem)
        if problem is not None:
            problems.append(f"{SIGNATURE}: {problem}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a Paper2Proof Proof Bundle.")
    parser.add_argument("bundle", type=Path, help="proof-bundle-<run_id>.tar.gz or bundle/ dir")
    parser.add_argument("--public-key", type=Path, default=DEFAULT_PUBLIC_KEY)
    parser.add_argument(
        "--allow-unsigned", action="store_true", help="accept a bundle without SIGNATURE"
    )
    args = parser.parse_args(argv)
    try:
        files = read_bundle(args.bundle)
    except (BundleError, OSError) as err:
        print(f"ERROR {args.bundle}: {err}", file=sys.stderr)
        return 2
    key: bytes | None = None
    if args.public_key.is_file():
        key = args.public_key.read_bytes()
    elif SIGNATURE in files:
        print(f"ERROR no public key at {args.public_key}", file=sys.stderr)
        return 2
    problems = verify_files(files, key, allow_unsigned=args.allow_unsigned)
    for problem in problems:
        print(f"FAIL {printable(problem)}")
    if problems:
        return 1
    signed = "signed" if SIGNATURE in files else "UNSIGNED"
    print(f"OK {args.bundle}: {len(files)} files match MANIFEST.json ({signed})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

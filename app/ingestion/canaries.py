"""Canary papers (eval set A, A-29) served under their `0000.000NN` arXiv IDs.

Month 00 does not exist on arXiv, so these IDs never shadow a real paper. A canary is served
only when the sha256 of its e-print is in config/canary_allowlist.yaml: a changed paper.tex is
never ingested as a canary. Everything else goes to arXiv. Used by the A-25 end-to-end runs,
eval set A and the H-09 demo canaries; the canaries' jobs run like any other run's (Nebius jobs
in prod, the dev backends in dev).
"""

import hashlib
import logging
import re
import struct
import zlib
from pathlib import Path
from typing import Protocol

import yaml

from app.execution.local_trusted import load_canaries
from app.ingestion.arxiv import ArxivMetadata

log = logging.getLogger(__name__)

CANARY_ARXIV_ID = re.compile(r"^0000\.\d{5}$")  # a subset of app.ingestion.arxiv.ARXIV_ID
_GZIP_HEADER = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff"  # no flags, mtime 0, OS unknown
_STORED_MAX = 0xFFFF


def gzip_stored(data: bytes) -> bytes:
    """gzip made of stored (uncompressed) deflate blocks: byte-identical for every zlib build."""
    chunks = [data[i : i + _STORED_MAX] for i in range(0, len(data), _STORED_MAX)] or [b""]
    out = bytearray(_GZIP_HEADER)
    for i, chunk in enumerate(chunks):
        out.append(1 if i == len(chunks) - 1 else 0)  # BFINAL on the last block, BTYPE=00
        out += struct.pack("<HH", len(chunk), len(chunk) ^ 0xFFFF) + chunk
    return bytes(out + struct.pack("<II", zlib.crc32(data), len(data) & 0xFFFFFFFF))


def eprint(paper_tex: Path) -> bytes:
    """A canary's arXiv-style e-print: paper.tex with LF line endings, gzip_stored."""
    return gzip_stored(paper_tex.read_bytes().replace(b"\r\n", b"\n"))


class PaperSource(Protocol):
    async def metadata(self, arxiv_id: str) -> ArxivMetadata: ...
    async def source(self, arxiv_id: str) -> bytes: ...


class CanaryPapers:
    """The allowlisted canaries, read once at startup (no file I/O on the event loop)."""

    def __init__(self, papers: dict[str, tuple[str, bytes]]) -> None:
        self._papers = papers  # arxiv_id -> (title, e-print bytes)

    def __contains__(self, arxiv_id: object) -> bool:
        return arxiv_id in self._papers

    def __len__(self) -> int:
        return len(self._papers)

    def _get(self, arxiv_id: str) -> tuple[str, bytes]:
        try:
            return self._papers[arxiv_id]
        except KeyError:
            raise LookupError(f"not a canary arXiv ID: {arxiv_id}") from None

    async def metadata(self, arxiv_id: str) -> ArxivMetadata:
        return ArxivMetadata(arxiv_id=arxiv_id, title=self._get(arxiv_id)[0])

    async def source(self, arxiv_id: str) -> bytes:
        return self._get(arxiv_id)[1]


def load_canary_papers(root: Path, allowlist: Path) -> CanaryPapers:
    """Every `<root>/<name>/{truth.yaml,paper.tex}` whose e-print hash is allowlisted."""
    allowed = load_canaries(allowlist) if allowlist.is_file() else frozenset()
    papers: dict[str, tuple[str, bytes]] = {}
    for truth_path in sorted(root.glob("*/truth.yaml")) if root.is_dir() else []:
        paper = truth_path.with_name("paper.tex")
        try:
            truth = yaml.safe_load(truth_path.read_text(encoding="utf-8")) or {}
            arxiv_id, title = str(truth["arxiv_id"]), str(truth["title"])
            data = eprint(paper)
        except (OSError, KeyError, TypeError, yaml.YAMLError):
            log.warning("canary skipped: unreadable", extra={"canary": truth_path.parent.name})
            continue
        if not CANARY_ARXIV_ID.fullmatch(arxiv_id):
            log.warning("canary skipped: not a 0000.000NN ID", extra={"arxiv_id": arxiv_id})
            continue
        if hashlib.sha256(data).hexdigest() not in allowed:
            log.warning("canary skipped: not in the allowlist", extra={"arxiv_id": arxiv_id})
            continue
        papers[arxiv_id] = (title, data)
    return CanaryPapers(papers)


class PaperRouter:
    """INGESTING's paper source: canary IDs from the canaries, every other ID from arXiv."""

    def __init__(self, arxiv: PaperSource, canaries: CanaryPapers) -> None:
        self._arxiv = arxiv
        self._canaries = canaries

    def _source_for(self, arxiv_id: str) -> PaperSource:
        return self._canaries if arxiv_id in self._canaries else self._arxiv

    async def metadata(self, arxiv_id: str) -> ArxivMetadata:
        return await self._source_for(arxiv_id).metadata(arxiv_id)

    async def source(self, arxiv_id: str) -> bytes:
        return await self._source_for(arxiv_id).source(arxiv_id)

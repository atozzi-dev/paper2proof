"""Canary papers served under their 0000.000NN arXiv IDs (A-25 end-to-end runs, eval set A, the
H-09 demo canaries): the bytes are the allowlisted e-prints, every other ID goes to arXiv."""

import hashlib
import shutil
from pathlib import Path

import pytest

from app.execution.local_trusted import load_canaries
from app.ingestion.arxiv import ArxivMetadata
from app.ingestion.canaries import CanaryPapers, PaperRouter, load_canary_papers
from eval.canaries import build

ROOT = Path(__file__).resolve().parents[2]
CANARIES = ROOT / "eval" / "canaries"
ALLOWLIST = ROOT / "config" / "canary_allowlist.yaml"


class FakeArxiv:
    def __init__(self) -> None:
        self.asked: list[str] = []

    async def metadata(self, arxiv_id: str) -> ArxivMetadata:
        self.asked.append(arxiv_id)
        return ArxivMetadata(arxiv_id=arxiv_id, title="From arXiv")

    async def source(self, arxiv_id: str) -> bytes:
        self.asked.append(arxiv_id)
        return b"arxiv bytes"


def _papers(root: Path = CANARIES) -> CanaryPapers:
    return load_canary_papers(root, ALLOWLIST)


async def test_every_canary_is_served_with_its_allowlisted_bytes() -> None:
    papers = _papers()
    allowed = load_canaries(ALLOWLIST)
    for canary in build.discover():
        data = await papers.source(canary.truth.arxiv_id)
        assert data == build.source_bytes(canary)  # the very bytes the allowlist hashes
        assert hashlib.sha256(data).hexdigest() in allowed
        meta = await papers.metadata(canary.truth.arxiv_id)
        assert meta.title == canary.truth.title
    assert len(papers) == len(build.discover()) == 10


async def test_an_unknown_canary_id_is_not_served() -> None:
    with pytest.raises(LookupError):
        await _papers().source("0000.99999")


def test_a_changed_paper_is_never_served(tmp_path: Path) -> None:
    shutil.copytree(CANARIES / "digits-logreg", tmp_path / "digits-logreg")
    paper = tmp_path / "digits-logreg" / "paper.tex"
    paper.write_text(paper.read_text(encoding="utf-8") + "\n% tampered\n", encoding="utf-8")
    assert "0000.00001" not in _papers(tmp_path)


def test_a_missing_canary_directory_serves_nothing(tmp_path: Path) -> None:
    assert len(_papers(tmp_path / "absent")) == 0


async def test_the_router_sends_canary_ids_to_the_canaries_and_the_rest_to_arxiv() -> None:
    arxiv = FakeArxiv()
    router = PaperRouter(arxiv, _papers())
    canary = build.discover()[0]
    assert await router.source(canary.truth.arxiv_id) == build.source_bytes(canary)
    assert (await router.metadata(canary.truth.arxiv_id)).title == canary.truth.title
    assert arxiv.asked == []
    assert await router.source("2106.09685") == b"arxiv bytes"
    assert (await router.metadata("2106.09685")).title == "From arXiv"
    assert arxiv.asked == ["2106.09685", "2106.09685"]


async def test_a_month_00_id_that_is_no_canary_still_goes_to_arxiv() -> None:
    arxiv = FakeArxiv()  # arXiv answers 404 for month 00: INGESTING then fails as for any ID
    assert await PaperRouter(arxiv, _papers()).source("0000.99999") == b"arxiv bytes"
    assert arxiv.asked == ["0000.99999"]


def test_eval_and_app_build_the_e_print_the_same_way() -> None:
    from app.ingestion import canaries

    assert build.gzip_stored is canaries.gzip_stored


async def test_a_versioned_canary_id_goes_to_arxiv() -> None:
    arxiv = FakeArxiv()
    assert await PaperRouter(arxiv, _papers()).source("0000.00001v2") == b"arxiv bytes"
    assert arxiv.asked == ["0000.00001v2"]


def test_the_hash_ingesting_records_is_the_allowlisted_one() -> None:
    """INGESTING hashes the bytes it is given; DISCOVERING skips discovery for those hashes."""
    allowed = load_canaries(ALLOWLIST)
    for canary in build.discover():
        assert hashlib.sha256(build.source_bytes(canary)).hexdigest() in allowed

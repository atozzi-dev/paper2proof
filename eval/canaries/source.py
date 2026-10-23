"""A dev PaperSource that serves the canary e-prints under their `0000.000NN` arXiv IDs.

Month 00 does not exist on arXiv, so these IDs never shadow a real paper. The bytes are the
ones config/canary_allowlist.yaml binds to these IDs, but this source does not check it. The
app serves them through app/ingestion/canaries.py (PaperRouter), which does; this dev-only
source remains for offline eval tooling.
"""

from app.ingestion.arxiv import ArxivMetadata
from eval.canaries import build


class CanaryPaperSource:
    def __init__(self, *, env: str) -> None:
        if env != "dev":
            raise RuntimeError("CanaryPaperSource serves canary papers only with P2P_ENV=dev")
        self._canaries = {c.truth.arxiv_id: c for c in build.discover()}

    def _get(self, arxiv_id: str) -> build.Canary:
        canary = self._canaries.get(arxiv_id)
        if canary is None:
            raise KeyError(f"not a canary arXiv ID: {arxiv_id!r}")
        return canary

    async def metadata(self, arxiv_id: str) -> ArxivMetadata:
        return ArxivMetadata(arxiv_id=arxiv_id, title=self._get(arxiv_id).truth.title)

    async def source(self, arxiv_id: str) -> bytes:
        return build.source_bytes(self._get(arxiv_id))

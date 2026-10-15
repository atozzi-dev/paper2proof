"""PDF fallback when no LaTeX source is available (pypdf; PyMuPDF is AGPL and excluded)."""

import hashlib
import io
import re

from pypdf import PdfReader

from app.domain.paper import IngestedPaper, Section

_REFERENCES = re.compile(r"\b(?:Table|Tab\.|Figure|Fig\.)\s*\d+", re.IGNORECASE)


def _page_texts(pdf: bytes) -> list[str]:
    reader = PdfReader(io.BytesIO(pdf))
    return [(page.extract_text() or "").strip() for page in reader.pages]


def parse_pdf(pdf: bytes, *, arxiv_id: str, title: str | None) -> IngestedPaper:
    return IngestedPaper(
        arxiv_id=arxiv_id,
        sha256=hashlib.sha256(pdf).hexdigest(),
        title=title,
        sections=[Section(title=f"Page {i}", text=t) for i, t in enumerate(_page_texts(pdf), 1)],
        tables=[],  # numbers in PDF tables come from the Vision role on rasterized pages
        source_kind="pdf",
    )


def pages_with_tables_or_figures(pdf: bytes) -> list[int]:
    """1-based pages that mention 'Table N' or 'Figure N' (candidates for rasterization)."""
    return [i for i, text in enumerate(_page_texts(pdf), 1) if _REFERENCES.search(text)]

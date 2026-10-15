"""Rasterize selected PDF pages for the Vision role (pypdfium2, Apache-2.0/BSD)."""

import io
from dataclasses import dataclass

import pypdfium2 as pdfium

MAX_PAGES = 20
DPI = 150


@dataclass(frozen=True)
class RenderedPage:
    page: int  # 1-based
    png: bytes


def rasterize_pages(pdf: bytes, pages: list[int], dpi: int = DPI) -> list[RenderedPage]:
    document = pdfium.PdfDocument(pdf)
    try:
        rendered = []
        for number in sorted(set(pages))[:MAX_PAGES]:
            if not 1 <= number <= len(document):
                continue
            image = document[number - 1].render(scale=dpi / 72).to_pil()
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            rendered.append(RenderedPage(page=number, png=buffer.getvalue()))
        return rendered
    finally:
        document.close()

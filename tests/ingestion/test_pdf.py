import io

from PIL import Image

from app.ingestion.pdf import pages_with_tables_or_figures, parse_pdf
from app.ingestion.raster import MAX_PAGES, rasterize_pages
from tests.ingestion.helpers import minimal_pdf

PDF = minimal_pdf(
    ["Introduction text", "Table 1: accuracy 94.1", "Conclusion", "See Figure 2 here"]
)


def test_pdf_text_is_extracted_per_page() -> None:
    paper = parse_pdf(PDF, arxiv_id="0000.00001", title="T")
    assert paper.source_kind == "pdf"
    assert [s.title for s in paper.sections] == ["Page 1", "Page 2", "Page 3", "Page 4"]
    assert "accuracy 94.1" in paper.sections[1].text
    assert paper.tables == []


def test_pages_with_tables_or_figures() -> None:
    assert pages_with_tables_or_figures(PDF) == [2, 4]


def test_rasterize_returns_150_dpi_pngs() -> None:
    images = rasterize_pages(PDF, [2, 4])
    assert [i.page for i in images] == [2, 4]
    png = Image.open(io.BytesIO(images[0].png))
    assert png.format == "PNG"
    assert abs(png.size[0] - 1275) <= 1 and abs(png.size[1] - 1650) <= 1  # 8.5x11 in, 150 dpi


def test_rasterize_caps_the_page_count() -> None:
    many = minimal_pdf([f"Table {i}" for i in range(MAX_PAGES + 5)])
    assert len(rasterize_pages(many, list(range(1, MAX_PAGES + 6)))) == MAX_PAGES

"""The ingested paper: produced by INGESTING (A-15), consumed by claim extraction (A-16)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Section(_Frozen):
    title: str
    text: str


class Table(_Frozen):
    number: int  # 1-based order of appearance ("Table 2" in the paper's numbering)
    label: str | None  # \label{...}
    caption: str
    latex: str  # the raw tabular block (untrusted)
    rows: list[list[str]]  # best-effort split on \\ and &


class PageImage(_Frozen):
    page: int  # 1-based
    key: str  # artifact key of the PNG (150 dpi)


class IngestedPaper(_Frozen):
    arxiv_id: str
    sha256: str  # of the downloaded source (tarball or PDF)
    title: str | None
    sections: list[Section]
    tables: list[Table]
    page_images: list[PageImage] = Field(default_factory=list)
    source_kind: Literal["latex", "pdf"]

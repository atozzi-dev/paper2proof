import pytest

from app.ingestion.archive import ArchiveError, extract_source
from tests.ingestion.helpers import gzip_single, make_tar, minimal_pdf


def test_extracts_a_normal_tarball_in_memory() -> None:
    files = extract_source(make_tar({"main.tex": b"x", "sec/a.tex": b"y"}))
    assert files.kind == "latex"
    assert files.files == {"main.tex": b"x", "sec/a.tex": b"y"}


@pytest.mark.parametrize(
    "archive",
    [
        make_tar({"../x.tex": b"x"}),
        make_tar({"a/../../x.tex": b"x"}),
        make_tar({"/etc/x.tex": b"x"}),
        make_tar({"main.tex": b"x"}, symlink="link.tex"),
        make_tar({"main.tex": b"x"}, hardlink="hard.tex"),
        make_tar({"C:\\evil.tex": b"x"}),
    ],
    ids=["dotdot", "nested-dotdot", "absolute", "symlink", "hardlink", "drive"],
)
def test_hostile_archives_are_rejected(archive: bytes) -> None:
    with pytest.raises(ArchiveError):
        extract_source(archive)


def test_file_count_cap() -> None:
    with pytest.raises(ArchiveError, match="files"):
        extract_source(make_tar({f"f{i}.tex": b"x" for i in range(11)}), max_files=10)


def test_size_cap() -> None:
    with pytest.raises(ArchiveError, match="large"):
        extract_source(make_tar({"main.tex": b"x" * 2000}), max_bytes=1000)


def test_gzip_bomb_is_capped() -> None:
    with pytest.raises(ArchiveError, match="large"):
        extract_source(gzip_single(b"\\documentclass{article}" + b" " * 5000), max_bytes=1000)


def test_single_gzipped_tex_becomes_main_tex() -> None:
    files = extract_source(gzip_single(b"\\documentclass{article}\\begin{document}x"))
    assert files.kind == "latex"
    assert list(files.files) == ["main.tex"]


def test_pdf_is_recognised() -> None:
    files = extract_source(minimal_pdf(["hello"]))
    assert files.kind == "pdf"
    assert files.pdf is not None and files.pdf.startswith(b"%PDF")


def test_garbage_is_rejected() -> None:
    with pytest.raises(ArchiveError):
        extract_source(b"not an archive")

import time

from app.ingestion.title import MAX_TITLE_CHARS, display_title


def test_the_arxiv_title_wins_and_its_line_breaks_go() -> None:
    meta = "Deep  Residual\n  Learning for Image Recognition"
    assert display_title(meta, "Something else") == "Deep Residual Learning for Image Recognition"


def test_the_source_title_is_used_without_its_latex() -> None:
    raw = r"Do Kernel Machines Need \emph{Feature Scaling}?\\ Evidence from \textsc{Breast~Cancer}"
    assert (
        display_title(None, raw)
        == "Do Kernel Machines Need Feature Scaling? Evidence from Breast Cancer"
    )


def test_math_and_escaped_characters_read_as_text() -> None:
    assert display_title(r"$k$-NN \& PCA on {D}igits", None) == "k-NN & PCA on Digits"


def test_control_format_and_bidi_characters_are_dropped() -> None:
    title = display_title("Title‮ reversed\x1b[31m red​", None)
    assert title is not None
    assert not any(ch in title for ch in "‮\x1b​")
    assert title == "Title reversed[31m red"


def test_a_long_title_is_cut_on_a_word() -> None:
    title = display_title("word " * 200, None)
    assert title is not None
    assert len(title) <= MAX_TITLE_CHARS
    assert title.endswith("…")
    assert "wor…" not in title


def test_no_title_or_only_markup_gives_none() -> None:
    assert display_title(None, None) is None
    assert display_title("   ", r"\maketitle") is None


def test_isolates_and_marks_are_dropped_and_blank_titles_fall_back() -> None:
    title = display_title("\u2066Left\u2069 \u200eand\u200f \u2067right\u2069", None)
    assert title == "Left and right"
    blank = "\u3164\u115f\u2800\u034f\ufe0f"
    assert display_title(blank, None) is None
    assert display_title(blank, "Source Title") == "Source Title"


def test_a_hostile_title_is_cleaned_fast() -> None:
    # spaces after a command, then balanced nesting: 3.2 s before the bounds (security review)
    nested = r"\a" + " " * 2000 + "x " + r"\a{" * 500 + "x" + "}" * 500
    long_arxiv = (r"\b{" * 2000 + "y" + "}" * 2000) * 10  # the arXiv title has no length cap
    started = time.perf_counter()
    display_title(None, nested)
    display_title(long_arxiv, None)
    assert time.perf_counter() - started < 0.2

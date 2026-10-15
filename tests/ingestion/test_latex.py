from app.ingestion.archive import extract_source
from app.ingestion.latex import MAX_DEPTH, MAX_FLATTEN_CHARS, find_main, flatten, parse_latex
from tests.ingestion.helpers import canary_tar


def test_canary_paper_parses_into_sections_and_tables() -> None:
    files = extract_source(canary_tar()).files
    paper = parse_latex(files, arxiv_id="0000.00001", title=None)
    assert paper.title == "Logistic Regression on Digits Is Enough"
    assert [s.title for s in paper.sections] == ["Abstract", "Introduction", "Method", "Results"]
    assert "96.1\\% accuracy" in paper.sections[0].text
    assert "must disappear" not in " ".join(s.text for s in paper.sections)
    assert len(paper.tables) == 2
    main = paper.tables[0]
    assert (main.label, main.number) == ("tab:main", 1)
    assert main.caption.startswith("Test accuracy on digits")
    assert main.rows[:3] == [
        ["Model", "Accuracy"],
        ["Logistic regression", "96.1"],
        ["k-NN (k=3)", "98.2"],
    ]
    assert paper.tables[1].label == "tab:ablation"
    assert paper.source_kind == "latex"


def test_find_main_prefers_the_documentclass_file() -> None:
    files = {"aux.tex": b"\\section{A}", "paper.tex": b"\\documentclass{article}\\begin{document}"}
    assert find_main(files) == "paper.tex"


def test_flatten_resolves_input_and_include_with_and_without_extension() -> None:
    files = {
        "main.tex": b"A \\input{b} C \\include{d.tex} E",
        "b.tex": b"B",
        "d.tex": b"D",
    }
    assert flatten(files, "main.tex") == "A B C D E"


def test_flatten_stops_at_max_depth() -> None:
    files = {f"f{i}.tex": f"L{i} \\input{{f{i + 1}}}".encode() for i in range(10)}
    text = flatten(files, "f0.tex")
    assert f"L{MAX_DEPTH}" in text
    assert f"L{MAX_DEPTH + 1}" not in text
    assert "[input depth limit]" in text


def test_self_include_terminates() -> None:
    assert "[input depth limit]" in flatten({"main.tex": b"x \\input{main}"}, "main.tex")


def test_missing_inputs_are_marked_not_fatal() -> None:
    assert "[missing input: nope]" in flatten({"main.tex": b"x \\input{nope}"}, "main.tex")


def test_input_paths_cannot_escape() -> None:
    files = {"main.tex": b"\\input{../../etc/passwd}", "etc/passwd": b"root"}
    assert "root" not in flatten(files, "main.tex")


def test_flatten_caps_the_output_of_a_fan_out() -> None:
    # every level inputs the next one 6 times: 6**5 copies of the leaf without a cap
    files = {f"f{i}.tex": (rf"\input{{f{i + 1}}} " * 6).encode() for i in range(5)}
    files["f5.tex"] = b"x" * 1000
    text = flatten(files, "f0.tex")
    assert len(text) <= MAX_FLATTEN_CHARS + 100
    assert "[input size limit]" in text

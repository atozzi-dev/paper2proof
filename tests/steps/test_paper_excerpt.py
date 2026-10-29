"""The paper text the Planner and the Coder see: the sections that describe the experiment
first, capped (production, 2026-10-29: without the paper they guessed the sample size, the
noise and the epochs, and a planted false claim came out Reproduced)."""

from app.domain.paper import IngestedPaper, Section
from app.orchestrator.steps.common import paper_excerpt


def _paper(*sections: tuple[str, str]) -> IngestedPaper:
    return IngestedPaper(
        arxiv_id="0000.00008",
        sha256="0" * 64,
        title="t",
        sections=[Section(title=t, text=x) for t, x in sections],
        tables=[],
        source_kind="latex",
    )


def test_the_experiment_sections_come_first() -> None:
    paper = _paper(
        ("Introduction", "Boosting is old."),
        ("Related work", "Many papers."),
        ("Experimental setup", "2000 samples, noise 1.0, random_state 0."),
        ("Results", "RMSE 1.300."),
    )
    text = paper_excerpt(paper, 10_000)
    assert text.index("Experimental setup") < text.index("Introduction")
    assert text.index("Results") < text.index("Related work")
    assert "2000 samples, noise 1.0, random_state 0." in text


def test_the_excerpt_is_capped_without_losing_the_setup() -> None:
    paper = _paper(("Introduction", "x" * 5000), ("Training details", "300 epochs of Adam."))
    text = paper_excerpt(paper, 200)
    assert len(text) <= 200 and "300 epochs of Adam." in text


def test_a_paper_without_sections_has_an_empty_excerpt() -> None:
    assert paper_excerpt(_paper(), 1000) == ""

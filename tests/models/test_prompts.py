"""What the prompts tell the models about data: jobs have no network, so a dataset bundled with
scikit-learn or generated in code needs no download and no planned dataset (eval set A run 2:
the Planner invented Hugging Face ids for scikit-learn's digits and wine)."""

from app.models.prompt_files import load_prompt


def test_the_reader_prompt_version_moved_with_its_text() -> None:
    assert load_prompt("reader_extract_claims").version == "2"


def test_the_planner_plans_no_dataset_for_bundled_or_generated_data() -> None:
    body = load_prompt("planner_experiment_plan").body
    assert "load_digits" in body and "make_" in body
    assert "plan no dataset" in body


def test_the_coder_loads_bundled_data_in_code_and_has_a_smoke_subset_only_when_planned() -> None:
    body = load_prompt("coder_system").body
    assert "load_digits" in body and "make_" in body
    assert "only when the plan has datasets" in body


def test_prompt_versions_moved_with_their_text() -> None:
    assert load_prompt("planner_experiment_plan").version == "9"
    assert load_prompt("coder_system").version == "12"


def test_the_coder_is_told_which_key_is_the_method_and_that_seeds_are_the_runner_s() -> None:
    """Super VL swapped method and baseline, and looped over seeds inside its script so every
    seed gave the same value (production, 2026-10-30)."""
    body = load_prompt("coder_system").body
    assert "the value of the method the claim is about" in body
    assert "never loop over seeds" in body


def test_the_coder_uses_the_function_a_claim_names() -> None:
    """KAN (production, 2026-10-30): a claim on Jacobian elliptic functions measured on
    exp(sin(pi x) + y^2), a toy formula from the same paper, under the elliptic name."""
    body = load_prompt("coder_system").body
    assert "scipy.special" in body and "never another formula under its name" in body


def test_the_planner_lists_the_paper_s_conditions_and_what_it_plans_for_each() -> None:
    """Production, 2026-10-30: "scale FULL" was a label; a KAN run trained 200 steps under it."""
    body = load_prompt("planner_experiment_plan").body
    assert "- setup:" in body and "`planned`" in body and "`paper`" in body
    assert "never seeds" in body

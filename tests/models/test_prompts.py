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
    assert load_prompt("planner_experiment_plan").version == "6"
    assert load_prompt("coder_system").version == "6"

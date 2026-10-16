"""The popular-package list generator (A-19): deterministic, offline on a recorded snapshot.

tests/fixtures/pypi/top-pypi-packages.min.json is the real dataset of 2026-10-01 cut to its
first 8 rows (boto3 ... charset-normalizer)."""

import json
from pathlib import Path
from typing import Any

import pytest

from app.execution.package_policy import normalize
from tests.ci import ROOT, load_script

gen = load_script("scripts/gen_pypi_top.py")
FIXTURE = ROOT / "tests" / "fixtures" / "pypi" / "top-pypi-packages.min.json"
FIRST_FIVE = ["boto3", "packaging", "typing-extensions", "idna", "urllib3"]


def _data(*rows: tuple[str, int]) -> dict[str, Any]:
    return {
        "last_update": "2026-10-01 12:40:51",
        "rows": [{"download_count": n, "project": p} for p, n in rows],
    }


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("tests never reach the network")

    monkeypatch.setattr(gen.urllib.request, "urlopen", refuse)


def test_the_top_names_follow_the_download_ranking() -> None:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert gen.top_names(data, 5) == FIRST_FIVE


def test_ranking_does_not_depend_on_source_order_and_ties_break_by_name() -> None:
    data = _data(("zeta", 5), ("alpha", 10), ("beta", 5), ("gamma", 1))
    assert gen.top_names(data, 3) == ["alpha", "beta", "zeta"]


def test_names_are_pep503_normalized_and_deduplicated() -> None:
    data = _data(("Typing_Extensions", 9), ("typing-extensions", 8), ("PyYAML", 7), ("x", 1))
    assert gen.top_names(data, 2) == ["typing-extensions", "pyyaml"]


def test_a_snapshot_shorter_than_the_count_is_an_error() -> None:
    with pytest.raises(ValueError, match="only 8"):
        gen.top_names(json.loads(FIXTURE.read_text(encoding="utf-8")), 9)


@pytest.mark.parametrize(
    "data",
    [
        [],
        {"rows": "boto3"},
        {"rows": [{"project": "boto3"}]},
        {"rows": [{"project": "boto3", "download_count": "9"}]},
        {"rows": [{"project": "boto3", "download_count": True}]},
        {"rows": [{"project": "https://evil.example/pkg", "download_count": 9}]},
        {"rows": [{"project": "bad name", "download_count": 9}]},
        {"rows": [{"project": "-dash", "download_count": 9}]},
        {"rows": [{"project": 7, "download_count": 9}]},
    ],
)
def test_malformed_snapshots_are_rejected(data: Any) -> None:
    with pytest.raises(ValueError):
        gen.top_names(data, 1)


def _generate(tmp_path: Path, curated: str, *extra: str) -> str:
    (tmp_path / "curated.txt").write_text(curated, encoding="utf-8")
    out = tmp_path / "out.txt"
    gen.main(
        ["--source", str(FIXTURE), "--curated", str(tmp_path / "curated.txt"),
         "--out", str(out), *extra]
    )  # fmt: skip
    return out.read_text(encoding="utf-8")


def test_the_output_is_the_sorted_union_of_the_top_names_and_the_curated_list(
    tmp_path: Path,
) -> None:
    text = _generate(tmp_path, "# ML staples\ntimm\nEinops\n\nboto3\n", "--count", "5")
    lines = text.splitlines()
    assert all(line.startswith("# ") for line in lines[:3])
    assert "snapshot 2026-10-01 12:40:51" in text
    names = [line for line in lines if not line.startswith("#")]
    assert names == sorted([*FIRST_FIVE, "einops", "timm"])


def test_the_output_is_byte_identical_across_runs(tmp_path: Path) -> None:
    first = _generate(tmp_path, "timm\n", "--count", "8")
    assert _generate(tmp_path, "timm\n", "--count", "8") == first
    assert first.endswith("\n") and "\r" not in first


def test_the_snapshot_date_cannot_inject_lines(tmp_path: Path) -> None:
    data = _data(("boto3", 9))
    data["last_update"] = "2026\nevil-package"
    assert "\nevil-package" not in gen.render(gen.top_names(data, 1), [], data, 1)


def test_the_committed_list_keeps_every_curated_name() -> None:
    """config/pypi_top500.txt is generated: sorted, normalized, the curated list inside."""
    names = gen.read_names((ROOT / "config" / "pypi_top500.txt").read_text(encoding="utf-8"))
    curated = gen.read_names((ROOT / "config" / "pypi_curated.txt").read_text(encoding="utf-8"))
    assert names == sorted(set(names)) and names == [normalize(n) for n in names]
    assert {normalize(n) for n in curated} <= set(names)
    assert len(names) >= 500

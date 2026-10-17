"""CODEOWNERS covers the files that decide which paper code may run on a developer machine
(invariant 1: the canary allowlist and the canary papers it hashes)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CODEOWNERS = ROOT / ".github" / "CODEOWNERS"


def _rules() -> dict[str, list[str]]:
    rules: dict[str, list[str]] = {}
    for line in CODEOWNERS.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            pattern, *owners = line.split()
            rules[pattern] = owners
    return rules


def test_the_canary_allowlist_and_papers_need_a_code_owner_review() -> None:
    rules = _rules()
    for pattern in ("/config/canary_allowlist.yaml", "/eval/canaries/", "/.github/CODEOWNERS"):
        assert rules.get(pattern), f"{pattern} has no code owner"
        assert all(owner.startswith("@") for owner in rules[pattern])


def test_no_catch_all_rule_hides_the_specific_ones() -> None:
    """The last matching pattern wins: a later `*` would override the canary rules."""
    patterns = list(_rules())
    assert "*" not in patterns[patterns.index("/config/canary_allowlist.yaml") :]

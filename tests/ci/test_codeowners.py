"""CODEOWNERS covers the files that decide which paper code may run on a developer machine
(invariant 1: the canary allowlist, the canary papers it hashes, and the code that wires and
enforces it)."""

import fnmatch
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CODEOWNERS = ROOT / ".github" / "CODEOWNERS"

GUARDED = (
    "/config/canary_allowlist.yaml",
    "/eval/canaries/",
    "/app/execution/local_trusted.py",
    "/app/execution/dev_docker.py",
    "/app/execution/smoke_factory.py",
    "/app/ingestion/canaries.py",  # which bytes a 0000.NNNNN ID gets
    "/app/orchestrator/steps/ingesting.py",  # computes the hash the gates compare
    "/app/orchestrator/steps/discovering.py",  # keeps third-party repos out of canary runs
    "/app/main.py",  # wires the allowlist, the paper lookup and the prod refusals
    "/tests/ci/test_codeowners.py",
    "/.github/CODEOWNERS",
)


def _rules(text: str) -> list[tuple[str, list[str]]]:
    """(pattern, owners) in file order: the last matching rule wins, as on GitHub."""
    rules = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            pattern, *owners = line.split()
            rules.append((pattern, owners))
    return rules


def _matches(pattern: str, path: str) -> bool:
    """gitignore-style matching, enough for CODEOWNERS: an anchored or floating pattern, a
    trailing `/` for a directory, `*` within a segment and `**` across segments."""
    path = path.lstrip("/")
    anchored = pattern.startswith("/") or "/" in pattern.rstrip("/")
    body = pattern.strip("/")
    if pattern in ("*", "**", "/**"):
        return True
    regex = re.escape(body).replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
    prefix = "" if anchored else "(?:.*/)?"
    if pattern.endswith("/") or "*" not in body:
        regex += "(?:/.*)?"  # a directory (or a file) and everything below it
    return re.fullmatch(prefix + regex, path) is not None or fnmatch.fnmatch(path, body)


def _owners_of(text: str, path: str) -> tuple[str, list[str]] | None:
    winner = None
    for pattern, owners in _rules(text):
        if _matches(pattern, path):
            winner = (pattern, owners)
    return winner


def _guarded_paths() -> list[str]:
    return [p.rstrip("/") + ("/x.py" if p.endswith("/") else "") for p in GUARDED]


def test_every_guarded_path_ends_on_its_own_rule_with_an_owner() -> None:
    text = CODEOWNERS.read_text(encoding="utf-8")
    for guarded, path in zip(GUARDED, _guarded_paths(), strict=True):
        winner = _owners_of(text, path)
        assert winner is not None, f"{guarded} has no code owner"
        pattern, owners = winner
        assert pattern == guarded, f"{guarded} is overridden by the later rule {pattern}"
        assert owners and all(owner.startswith("@") for owner in owners), guarded


def test_a_later_broad_or_ownerless_rule_would_be_caught() -> None:
    text = CODEOWNERS.read_text(encoding="utf-8")
    for extra in ("* @someone", "/app/ @someone", "/** @someone", "app/", "/app/main.py"):
        winner = _owners_of(text + f"\n{extra}\n", "/app/main.py")
        assert winner is not None and (winner[0] != "/app/main.py" or not winner[1]), extra

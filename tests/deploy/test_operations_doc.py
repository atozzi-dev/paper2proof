"""docs/OPERATIONS.md: the rotate-after-judging checklist names every secret the deploy loads
and every secret the workflows read, so a new secret can't be forgotten at rotation time."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "OPERATIONS.md"


def _section(title: str) -> str:
    text = DOC.read_text(encoding="utf-8")
    match = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    assert match, f"docs/OPERATIONS.md has no '## {title}' section"
    return match.group(1)


def _endpoint_secrets() -> set[str]:
    script = (ROOT / "deploy" / "endpoint.sh").read_text(encoding="utf-8")
    listed = re.search(r"SECRET_VARS=\(([^)]*)\)", script)
    assert listed
    added = re.findall(r"SECRET_VARS\+=\(([^)]*)\)", script)
    return {*listed.group(1).split(), *" ".join(added).split()}


def _workflow_secrets() -> set[str]:
    names: set[str] = set()
    for workflow in (ROOT / ".github" / "workflows").glob("*.yml"):
        names |= set(re.findall(r"secrets\.([A-Z][A-Z0-9_]*)", workflow.read_text("utf-8")))
    return names


def test_every_deployed_and_ci_secret_has_rotation_steps() -> None:
    checklist = _section("Rotate after judging")
    secrets = _endpoint_secrets() | _workflow_secrets()
    assert {"NEBIUS_API_KEY", "ADMIN_TOKEN", "OBSERVABILITY_STATIC_KEY", "P2P_SMOKE_INVITE"} <= (
        secrets
    )
    missing = sorted(name for name in secrets if f"`{name}`" not in checklist)
    assert not missing, f"no rotation steps for {missing}"


def test_the_runbook_sections_exist() -> None:
    for title in ("Invites", "Gallery curation", "Takedown", "Reliability runbook"):
        _section(title)

"""Static guards for infra/: invariant 4 (no secret in Terraform code or state) and IAM scope.

Secret values are loaded out of band (deploy/secrets.sh). Terraform creates only the
SecretStash containers, and S3 access keys deliver their secret to SecretStash instead
of the API response, so neither the code nor the state ever holds a secret value.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
INFRA = ROOT / "infra"
PROJECT_IAM = INFRA / "terraform" / "modules" / "project_iam"

# Anything that would put a secret payload into Terraform code or state.
FORBIDDEN = {
    "secret version resource": re.compile(r"mysterybox_v1_secret_version"),
    "inline secret version": re.compile(r"\bsecret_version\b"),
    "secret payload": re.compile(r"\b(payload|string_value|binary_value)\s*="),
    "access key secret read": re.compile(r'\bstatus\.secret\b(?!_)|status\[\s*"secret"\s*\]'),
    "secret payload data source": re.compile(r"mysterybox_v1_secret_payload"),
    "ephemeral access key secret": re.compile(r"iam_v2_access_key_secret"),
}


def _terraform_files() -> list[Path]:
    return sorted(
        p
        for pattern in ("*.tf", "*.tf.json")
        for p in INFRA.rglob(pattern)
        if ".terraform" not in p.parts
    )


def _strip_comments(text: str) -> str:
    """Drop HCL comments so documentation can name what is forbidden."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"(?m)(#|(?<!:)//).*$", "", text)


def _code(path: Path) -> str:
    return _strip_comments(path.read_text(encoding="utf-8"))


def _blocks(text: str, header: re.Pattern[str]) -> list[str]:
    """Return the body of every block whose header matches, by brace counting."""
    bodies = []
    for match in header.finditer(text):
        start = text.index("{", match.end() - 1)
        depth = 0
        for i in range(start, len(text)):
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            if depth == 0:
                bodies.append(text[start : i + 1])
                break
    return bodies


def _resource_bodies(resource_type: str) -> list[str]:
    header = re.compile(rf'resource\s+"{resource_type}"\s+"[^"]+"\s*\{{')
    return [body for path in _terraform_files() for body in _blocks(_code(path), header)]


def test_infra_has_terraform_code() -> None:
    assert _terraform_files(), (
        "infra/ has no Terraform files: the guards below would pass vacuously"
    )


@pytest.mark.parametrize("name", sorted(FORBIDDEN))
def test_no_secret_payload_in_terraform(name: str) -> None:
    offenders = [
        str(path.relative_to(ROOT))
        for path in _terraform_files()
        if FORBIDDEN[name].search(_code(path))
    ]
    assert not offenders, f"{name} found in {offenders}"


def test_access_keys_deliver_their_secret_to_secretstash() -> None:
    bodies = _resource_bodies("nebius_iam_v2_access_key")
    assert bodies, "no nebius_iam_v2_access_key found: update this guard if they moved"
    for body in bodies:
        assert re.search(r'secret_delivery_mode\s*=\s*"MYSTERY_BOX"', body), body


def test_no_tfvars_or_state_in_the_repository() -> None:
    """Tracked or addable files only: a local, gitignored prod.auto.tfvars is expected."""
    git = shutil.which("git")
    assert git, "git is needed to list the repository's files"
    files = subprocess.run(  # noqa: S603 (fixed argv)
        [git, "ls-files", "--cached", "--others", "--exclude-standard", "infra"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    leaked = [f for f in files if re.search(r"\.tfstate(\.backup)?$|\.tfvars(\.json)?$", f)]
    assert not leaked, leaked


def test_no_access_permit_on_the_tenant() -> None:
    bodies = _resource_bodies("nebius_iam_v1_access_permit")
    assert bodies
    for body in bodies:
        assert not re.search(r"resource_id\s*=\s*var\.tenant_id\b", body), body


def test_api_runtime_has_no_role_on_the_control_project() -> None:
    """Its S3 key must not reach p2p-tfstate; bucket permits give it its two buckets only."""
    bodies = [
        body
        for body in _resource_bodies("nebius_iam_v1_access_permit")
        if "nebius_iam_v1_group.api_runtime.id" in body
    ]
    assert bodies
    for body in bodies:
        assert "control_project_id" not in body, body


def test_github_oidc_subject_pins_environment_and_branch() -> None:
    main = _code(PROJECT_IAM / "main.tf")
    variables = _code(PROJECT_IAM / "variables.tf")
    assert (
        '"repo:${var.github_repo}:environment:${var.github_environment}:ref:${var.github_ref}"'
        in main
    )
    assert re.search(r'variable "github_ref" \{[^}]*default\s*=\s*"refs/heads/main"', variables)


def test_payload_viewer_only_on_the_app_secret() -> None:
    """Payload access is the one role editor does not imply: grant it on p2p-secrets alone."""
    bodies = [
        body
        for body in _resource_bodies("nebius_iam_v1_access_permit")
        if "mysterybox.payload-viewer" in body
    ]
    assert bodies
    for body in bodies:
        assert re.search(r"resource_id\s*=\s*var\.secrets_id\b", body), body

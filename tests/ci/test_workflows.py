"""Static policy checks on .github/workflows (A-05): supply chain, permissions, secrets, cost.

Nothing here talks to GitHub. actionlint (make lint) checks syntax and expressions; these tests
check the project rules actionlint cannot know about.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
EXPECTED = ["ci.yml", "deploy.yml", "smoke-cron.yml"]

SHA_PINNED = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}$")
USES_LINE = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+)(.*)$")
PERMISSION_LEVELS = {"read", "write", "none"}

# The only write scopes any job may hold, by (workflow, job).
WRITE_ALLOWLIST = {
    ("deploy.yml", "deploy"): {"id-token"},
    ("smoke-cron.yml", "report"): {"issues"},
}
# Third-party scanners never share a job with the Nebius OIDC token.
SCANNER_ACTIONS = ("aquasecurity/", "anchore/")
# Steps that restore a cache other runs could have written.
CACHE_INPUTS = {"cache", "enable-cache"}


def _files() -> list[Path]:
    return sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])


def _load(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path.name} is not a mapping"
    # YAML 1.1 reads the bare key `on` as the boolean True.
    if True in data:
        data["on"] = data.pop(True)
    return data


def _jobs(path: Path) -> dict[str, dict[str, Any]]:
    jobs = _load(path)["jobs"]
    assert isinstance(jobs, dict)
    return jobs


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return list(job.get("steps", []))


def _triggers(path: Path) -> dict[str, Any]:
    on = _load(path)["on"]
    if isinstance(on, str):
        return {on: None}
    if isinstance(on, list):
        return dict.fromkeys(on)
    assert isinstance(on, dict)
    return on


ALL = [p.name for p in _files()]
JOBS = [(p.name, name) for p in _files() for name in _jobs(p)]


def _job(workflow: str, name: str) -> dict[str, Any]:
    return _jobs(WORKFLOWS / workflow)[name]


@pytest.mark.parametrize("name", EXPECTED)
def test_expected_workflow_exists(name: str) -> None:
    assert (WORKFLOWS / name).is_file(), f"missing .github/workflows/{name}"


@pytest.mark.parametrize("name", ALL)
def test_every_action_is_pinned_by_commit_sha(name: str) -> None:
    for lineno, line in enumerate((WORKFLOWS / name).read_text(encoding="utf-8").splitlines(), 1):
        match = USES_LINE.match(line)
        if not match:
            continue
        ref, rest = match.group(1), match.group(2)
        if ref.startswith("./"):
            continue
        if ref.startswith("docker://"):
            assert "@sha256:" in ref, f"{name}:{lineno} docker action not pinned by digest"
            continue
        assert SHA_PINNED.match(ref), f"{name}:{lineno} {ref} is not pinned to a full commit SHA"
        assert re.search(r"#\s*v\d", rest), f"{name}:{lineno} pinned SHA needs a '# vX.Y.Z' comment"


@pytest.mark.parametrize("name", ALL)
def test_no_privileged_or_cross_workflow_triggers(name: str) -> None:
    triggers = _triggers(WORKFLOWS / name)
    assert "pull_request_target" not in triggers
    assert "workflow_run" not in triggers


@pytest.mark.parametrize("name", ALL)
def test_workflow_grants_nothing_by_default(name: str) -> None:
    assert _load(WORKFLOWS / name).get("permissions") == {}, (
        f"{name}: set top-level `permissions: {{}}` and grant scopes per job"
    )


@pytest.mark.parametrize(("workflow", "job"), JOBS)
def test_every_job_declares_least_privilege_permissions(workflow: str, job: str) -> None:
    permissions = _job(workflow, job).get("permissions")
    assert isinstance(permissions, dict), f"{workflow}:{job} must declare a permissions mapping"
    assert set(permissions.values()) <= PERMISSION_LEVELS
    writes = {scope for scope, level in permissions.items() if level == "write"}
    allowed = WRITE_ALLOWLIST.get((workflow, job), set())
    assert writes <= allowed, f"{workflow}:{job} has write scopes {sorted(writes - allowed)}"


def test_id_token_only_in_the_main_only_prod_deploy_job() -> None:
    holders = [
        (wf, job)
        for wf, job in JOBS
        if _job(wf, job).get("permissions", {}).get("id-token") == "write"
    ]
    assert holders == [("deploy.yml", "deploy")]
    deploy = _job("deploy.yml", "deploy")
    environment = deploy.get("environment")
    env_name = environment.get("name") if isinstance(environment, dict) else environment
    assert env_name == "prod", "the OIDC subject is pinned to environment:prod"
    assert "github.ref == 'refs/heads/main'" in str(deploy.get("if", ""))


def test_deploy_runs_only_from_main_and_never_concurrently() -> None:
    triggers = _triggers(WORKFLOWS / "deploy.yml")
    assert set(triggers) <= {"push", "workflow_dispatch"}
    assert triggers["push"]["branches"] == ["main"]
    concurrency = _job("deploy.yml", "deploy")["concurrency"]
    assert concurrency["cancel-in-progress"] is False


@pytest.mark.parametrize(("workflow", "job"), JOBS)
def test_run_scripts_never_interpolate_expressions(workflow: str, job: str) -> None:
    # `${{ }}` inside run: is pasted into the script before bash parses it (script injection,
    # secrets in the process list). Pass values through `env:` instead.
    for step in _steps(_job(workflow, job)):
        assert "${{" not in str(step.get("run", "")), f"{workflow}:{job}: {step.get('name')}"


@pytest.mark.parametrize(("workflow", "job"), JOBS)
def test_checkout_does_not_persist_the_token(workflow: str, job: str) -> None:
    for step in _steps(_job(workflow, job)):
        if str(step.get("uses", "")).startswith("actions/checkout@"):
            assert step.get("with", {}).get("persist-credentials") is False


@pytest.mark.parametrize(("workflow", "job"), JOBS)
def test_every_job_has_a_timeout(workflow: str, job: str) -> None:
    assert isinstance(_job(workflow, job).get("timeout-minutes"), int)


@pytest.mark.parametrize(("workflow", "job"), JOBS)
def test_privileged_jobs_restore_no_cache(workflow: str, job: str) -> None:
    spec = _job(workflow, job)
    privileged = (
        "write" in spec.get("permissions", {}).values()
        or "environment" in spec
        or "secrets." in yaml.safe_dump(spec)
    )
    if not privileged:
        return
    for step in _steps(spec):
        uses = str(step.get("uses", ""))
        assert not uses.startswith("actions/cache"), f"{workflow}:{job} restores a cache"
        inputs = step.get("with", {}) or {}
        for key in CACHE_INPUTS & set(inputs):
            assert str(inputs[key]).lower() in {"false", ""}, f"{workflow}:{job}: {uses} {key}"


@pytest.mark.parametrize(("workflow", "job"), JOBS)
def test_secrets_only_in_jobs_behind_an_environment(workflow: str, job: str) -> None:
    spec = _job(workflow, job)
    text = yaml.safe_dump(spec)
    if re.search(r"secrets\.(?!GITHUB_TOKEN\b)", text):
        assert "environment" in spec, f"{workflow}:{job} reads secrets outside an environment"
    assert "secrets: inherit" not in text


@pytest.mark.parametrize("name", ALL)
def test_no_paid_targets_in_ci(name: str) -> None:
    text = "\n".join(
        str(step.get("run", "")) for job in _jobs(WORKFLOWS / name).values() for step in _steps(job)
    )
    assert not re.search(r"make\s+(test-live|eval)\b", text), f"{name} runs a paid make target"
    assert not re.search(r"-m\s+[\"']?live\b", text), f"{name} runs live tests"


def _step(workflow: str, job: str, name: str) -> dict[str, Any]:
    matches = [s for s in _steps(_job(workflow, job)) if s.get("name") == name]
    assert len(matches) == 1, f"{workflow}:{job} has no single step named {name!r}"
    return matches[0]


def _step_index(job: dict[str, Any], predicate: Any) -> int:
    return next(i for i, step in enumerate(_steps(job)) if predicate(step))


def test_scanners_never_share_a_job_with_the_oidc_token() -> None:
    deploy_uses = [str(s.get("uses", "")) for s in _steps(_job("deploy.yml", "deploy"))]
    assert not [u for u in deploy_uses if u.startswith(SCANNER_ACTIONS)]
    build = yaml.safe_dump(_job("deploy.yml", "build"))
    assert "--severity CRITICAL" in build
    assert "--exit-code 1" in build
    assert "--ignore-unfixed" not in build, "fail on every CRITICAL; exceptions go in .trivyignore"
    assert "syft" in build


def test_the_oidc_job_runs_only_first_party_actions_and_setup_terraform() -> None:
    # Every step of a job with id-token: write can mint the prod OIDC token.
    allowed = ("actions/", "hashicorp/setup-terraform@")
    for step in _steps(_job("deploy.yml", "deploy")):
        uses = str(step.get("uses", ""))
        assert not uses or uses.startswith(allowed), f"third-party action in deploy: {uses}"
        run = str(step.get("run", ""))
        assert "uv sync" not in run and "uv run" not in run, "install only hashed wheels"
    install = str(_step("deploy.yml", "deploy", "Install the Nebius SDK (locked wheels)")["run"])
    for flag in ("--require-hashes", "--only-binary :all:", "--no-deps"):
        assert flag in install


def test_digests_are_fixed_before_any_scanner_runs() -> None:
    build = _job("deploy.yml", "build")
    assert build["outputs"]["digests"] == "${{ steps.build.outputs.digests }}"
    upload = _step_index(build, lambda s: s.get("with", {}).get("name") == "images-oci")
    first_scanner = _step_index(build, lambda s: str(s.get("uses", "")).startswith(SCANNER_ACTIONS))
    assert upload < first_scanner, "upload the archives before installing scanners"
    build_run = str(_step("deploy.yml", "build", "Build the images as OCI archives")["run"])
    assert re.search(r"image=moby/buildkit:v[\d.]+@sha256:[0-9a-f]{64}", build_run), "pin BuildKit"
    deploy = _job("deploy.yml", "deploy")
    assert deploy["env"]["BUILD_DIGESTS"] == "${{ needs.build.outputs.digests }}"
    oci = next(s for s in _steps(build) if s.get("with", {}).get("name") == "images-oci")
    assert oci["with"]["retention-days"] >= 3, "the archives must outlive the approval wait"


def test_deploy_pushes_and_deploys_by_digest() -> None:
    text = yaml.safe_dump(_job("deploy.yml", "deploy"))
    for script in ("deploy/drain.sh", "deploy/endpoint.sh", "deploy/smoke.sh"):
        assert script in text
    assert "--preserve-digests" in text, "push the scanned bytes unchanged"
    assert "BUILD_DIGESTS" in str(_step("deploy.yml", "deploy", "Push the images by digest"))
    assert ":latest" not in (WORKFLOWS / "deploy.yml").read_text(encoding="utf-8")


def test_the_fallback_key_reaches_only_key_mode() -> None:
    env = _step("deploy.yml", "deploy", "Get a Nebius token (OIDC; S-7 pending, fallback key)")[
        "env"
    ]
    assert env["NEBIUS_SA_CREDENTIALS"].startswith("${{ vars.NEBIUS_AUTH_MODE == 'key' &&")
    assert "NEBIUS_SA_CREDENTIALS_SET" in env


def test_rollback_restores_the_previous_digest_set_and_undrains() -> None:
    rollback = _step("deploy.yml", "deploy", "Rollback to the previous deployment")
    assert "failure()" in rollback["if"]
    assert 'P2P_DIGESTS_FILE="$RUNNER_TEMP/previous-digests.json"' in rollback["run"]
    assert "P2P_DIGESTS_FILE" not in rollback.get("env", {})
    assert "^https://" in rollback["run"], "check the URL before sending a credential to it"
    undrain = _step("deploy.yml", "deploy", "Cancel the drain")
    assert "failure()" in undrain["if"] and "steps.drain.outcome" in undrain["if"]
    assert "--cancel" in undrain["run"]


def test_long_deploy_steps_time_out_before_the_job_does() -> None:
    # A job timeout cancels the job and skips the failure() rollback; a step timeout does not.
    deploy = _job("deploy.yml", "deploy")
    for step in _steps(deploy):
        run = str(step.get("run", ""))
        if "bash deploy/" in run or "terraform -chdir" in run or "skopeo copy" in run:
            assert isinstance(step.get("timeout-minutes"), int), step.get("name")
    budget = sum(int(step.get("timeout-minutes", 0)) for step in _steps(deploy))
    setup_minutes = 15  # checkout, downloads, installers, token
    assert budget + setup_minutes <= deploy["timeout-minutes"]


def test_deploy_never_applies_an_unreviewed_terraform_plan() -> None:
    text = yaml.safe_dump(_job("deploy.yml", "deploy"))
    run = str(_step("deploy.yml", "deploy", "Terraform plan, guard, apply")["run"])
    assert 'terraform -chdir="$TF_DIR" plan' in run
    assert "apply -input=false -lock-timeout=5m tfplan" in run, "apply the guarded plan file"
    assert ".github/scripts/tfplan_guard.py" in text
    assert "envs/prod-iam" not in text, "prod-iam is applied by a human only"
    assert "TF_PROD_TFVARS_JSON may only set" in run, "limit the override keys"


def test_smoke_never_gets_the_admin_token() -> None:
    for workflow, job, name in (
        ("deploy.yml", "deploy", "Smoke test"),
        ("smoke-cron.yml", "smoke", "Smoke test"),
    ):
        env = _step(workflow, job, name).get("env", {})
        assert "P2P_ADMIN_TOKEN" not in env
        assert "P2P_SMOKE_INVITE" in env
    assert "P2P_ADMIN_TOKEN" not in (WORKFLOWS / "smoke-cron.yml").read_text(encoding="utf-8")


def test_ci_runs_every_gate_on_pull_requests() -> None:
    triggers = _triggers(WORKFLOWS / "ci.yml")
    assert "pull_request" in triggers
    text = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    for command in (
        "make lint",
        "make test",
        "make tf-check",
        "make licenses",
        "make images",
        "npm run build",
    ):
        assert command in text, f"ci.yml does not run `{command}`"
    assert "--push" not in text and "skopeo" not in text, "CI never pushes images"


def test_smoke_cron_runs_daily_during_judging_and_reports_failures() -> None:
    triggers = _triggers(WORKFLOWS / "smoke-cron.yml")
    assert triggers["schedule"] == [{"cron": "0 7 * * *"}]
    text = (WORKFLOWS / "smoke-cron.yml").read_text(encoding="utf-8")
    assert "2026-10-30" in text and "2026-12-15" in text
    assert "deploy/smoke.sh" in text
    report = _job("smoke-cron.yml", "report")
    assert "failure" in str(report.get("if", ""))
    assert "smoke" in report.get("needs", [])

"""Proof Bundle build: layout, manifest, signature, what never goes in, untrusted bucket inputs."""

import json
import re
import shutil
import subprocess
from typing import Any

import pytest

from app.bundle.build import BundleUnavailable, render_reproduce
from app.bundle.secrets import find_secrets, redact, secret_values
from app.bundle.sign import FakeSigner, SigningError
from app.bundle.store import bundle_key
from app.bundle.verify import read_archive, verify_files
from app.domain.contracts import ExecManifest
from app.settings import Settings
from tests.bundle.conftest import (
    KNOWN_SECRET,
    LOCK,
    PAPER_TEXT,
    RUN,
    RUNNER,
    Run,
    exec_manifest,
    sha,
)

# Fake secrets, split so the repository's own secret hooks don't flag this file.
TVLY = b"tvly-" + b"abcdefghijklmnop1234"
BEARER = b"Bearer " + b"abcdefghijklmnopqrstuvwxyz"

# What the issue asks the scan to catch, written independently of app/bundle/secrets.py.
SECRET_SHAPES = re.compile(
    rb"tvly-[A-Za-z0-9_-]{8,}|Bearer\s+[A-Za-z0-9._~+/=-]{8,}|"
    rb"-----BEGIN [A-Z ]*PRIVATE KEY-----|AKIA[0-9A-Z]{16}|hf_[A-Za-z0-9]{20,}"
)


async def _files(run: Run, signer: FakeSigner | None) -> dict[str, bytes]:
    await run.bundler(signer).build(run.ctx)
    stored = await run.bundles.get(RUN)
    assert stored is not None
    return read_archive(stored)


def _manifest(files: dict[str, bytes]) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(files["MANIFEST.json"])
    return data


async def test_layout_manifest_and_signature(run: Run, signer: FakeSigner) -> None:
    files = await _files(run, signer)
    expected = {
        "MANIFEST.json", "SIGNATURE", "paper/metadata.json", "claims.json", "plan.json",
        "env/provision_report.json", "env/lock.txt", "code/workspace.tar",
        "code/smoke_result.json", "code/exec_manifest_1.json", "results/1/results.json",
        "results/1/stdout.log", "results/1/stderr.log", "results/1/env.json",
        "results/1/execution.json", "results/1/analysis.json", "verdict.json",
        "explanation.md", "cost_receipt.json", "reproduce.sh",
    }  # fmt: skip
    assert set(files) == expected
    assert verify_files(files, (await signer.public_key_pem()).encode()) == []
    manifest = _manifest(files)
    assert manifest["schema"] == "p2p.bundle/v2"
    assert manifest["run_id"] == RUN
    assert manifest["arxiv_id"] == "2106.09685"
    assert manifest["paper_sha256"] == "d" * 64
    assert manifest["config_sha256"] == "c" * 64
    assert manifest["schema"] == "p2p.bundle/v2"
    assert manifest["verdicts"] == {"c1": "REPRODUCED_AT_REDUCED_SCALE"}
    assert manifest["judged_attempt"] == 1
    assert manifest["paper2proof"]["commit"] == "0123abc"
    assert manifest["built_at"] == "2026-10-25T10:00:00Z"
    assert manifest["models"] == {
        "judge": ["nvidia/nemotron-super"],
        "planner": ["nvidia/nemotron-ultra"],
    }
    assert manifest["prompts"]["judge_explain"]["version"] == "2"
    assert re.fullmatch(r"[0-9a-f]{64}", manifest["prompts"]["judge_explain"]["sha256"])
    assert manifest["images"] == {"runner": RUNNER.split("@")[0] + "@sha256:" + "0" * 64}
    assert manifest["omitted"] == [] and manifest["redacted"] == []
    origins = {p: f["origin"] for p, f in manifest["files"].items()}
    assert origins["results/1/stdout.log"] == "run_bucket"
    assert origins["code/workspace.tar"] == "run_bucket"
    assert origins["verdict.json"] == "control_plane"
    assert files["code/workspace.tar"] == run.workspace_tar
    assert files["env/lock.txt"] == LOCK
    signature = json.loads(files["SIGNATURE"])
    assert signature["key_id"] == "test-key"
    assert signature["manifest_sha256"] == sha(files["MANIFEST.json"])


async def test_content_of_the_control_plane_files(run: Run, signer: FakeSigner) -> None:
    files = await _files(run, signer)
    claims = json.loads(files["claims.json"])
    assert [(c["id"], c["selected"]) for c in claims] == [("c1", True), ("c2", False)]
    verdict = json.loads(files["verdict.json"])
    assert verdict["verdicts"][0]["verdict"]["kind"] == "REPRODUCED_AT_REDUCED_SCALE"
    assert verdict["config"] == {"tol_rel": 0.02}
    assert "explanation_md" not in verdict["verdicts"][0]
    assert files["explanation.md"].decode().startswith("## Claim c1\n\nIt held up.")
    receipt = json.loads(files["cost_receipt.json"])
    assert {(m["role"], m["tokens_in"], m["tokens_out"]) for m in receipt["model_calls"]} == {
        ("planner", 1000, 200),
        ("judge", 500, 100),
    }
    assert receipt["jobs"][0]["gpu_seconds"] == 600
    assert receipt["ledger"]["cloud_usd"] == pytest.approx(0.3)
    assert receipt["total_usd"] == pytest.approx(0.312)


async def test_the_paper_itself_never_goes_in(run: Run, signer: FakeSigner) -> None:
    files = await _files(run, signer)
    assert not any(p.endswith((".pdf", ".tex", ".png")) for p in files)
    assert all(PAPER_TEXT.encode() not in data for data in files.values())
    metadata = json.loads(files["paper/metadata.json"])
    assert metadata == {
        "arxiv_id": "2106.09685",
        "sha256": "d" * 64,
        "title": "A Paper",
        "source_kind": "pdf",
        "sections": 1,
        "tables": 0,
    }


async def test_no_file_contains_a_secret(run: Run, signer: FakeSigner) -> None:
    run.put(
        "results/1/stdout.log",
        b"key " + TVLY + b"\nAuthorization: " + BEARER + b"\n",
    )
    run.put("results/1/stderr.log", f"leaked {KNOWN_SECRET}\n".encode())
    run.put("results/1/env.json", b'{"HF": "hf_abcdefghijklmnopqrstuvwxyz0123"}')
    files = await _files(run, signer)
    for path, data in files.items():
        assert not SECRET_SHAPES.search(data), path
        assert KNOWN_SECRET.encode() not in data, path
    manifest = _manifest(files)
    assert manifest["redacted"] == [
        "results/1/env.json",
        "results/1/stderr.log",
        "results/1/stdout.log",
    ]
    assert json.loads(files["results/1/env.json"]) == {"HF": "[REDACTED]"}
    assert verify_files(files, (await signer.public_key_pem()).encode()) == []


async def test_a_secret_in_the_workspace_omits_it(run: Run, signer: FakeSigner) -> None:
    from app.execution.smoke import pack_with_digest

    archive, digest = pack_with_digest({"train.py": b"KEY = '" + TVLY + b"'\n"})
    run.put("staging/workspace.tar", archive)
    run.artifacts.put_json(f"runs/{RUN}/exec_manifest_1.json", exec_manifest(digest))
    files = await _files(run, signer)
    assert "code/workspace.tar" not in files
    assert {"path": "code/workspace.tar", "reason": "secret_pattern"} in _manifest(files)["omitted"]


@pytest.mark.parametrize(
    ("key", "data", "path", "reason"),
    [
        ("staging/workspace.tar", b"tampered", "code/workspace.tar", "digest_mismatch"),
        ("staging/lock.txt", b"numpy==9\n", "env/lock.txt", "digest_mismatch"),
        ("results/1/results.json", b"{not json", "results/1/results.json", "invalid"),
        ("results/1/env.json", b"[1, 2", "results/1/env.json", "invalid"),
        ("results/1/stdout.log", b"x" * (4 * 1024 * 1024 + 1), "results/1/stdout.log",
         "too_large"),
        ("results/1/results.json", b"{}" + b" " * (256 * 1024), "results/1/results.json",
         "too_large"),
        ("results/1/stderr.log", None, "results/1/stderr.log", "missing"),
    ],
    ids=["workspace", "lock", "results-json", "env-json", "big-log", "big-results", "missing"],
)  # fmt: skip
async def test_untrusted_bucket_inputs_are_checked(
    run: Run, signer: FakeSigner, key: str, data: bytes | None, path: str, reason: str
) -> None:
    if data is None:
        run.storage.objects.pop((RUN, key))
    else:
        run.put(key, data)
    files = await _files(run, signer)
    assert path not in files
    assert {"path": path, "reason": reason} in _manifest(files)["omitted"]
    assert verify_files(files, (await signer.public_key_pem()).encode()) == []


async def test_missing_control_plane_artifacts_are_listed(run: Run, signer: FakeSigner) -> None:
    (run.artifacts.root / "runs" / RUN / "smoke_result.json").unlink()
    files = await _files(run, signer)
    assert {"path": "code/smoke_result.json", "reason": "missing"} in _manifest(files)["omitted"]


async def test_no_verdict_means_no_bundle(run: Run, signer: FakeSigner) -> None:
    (run.artifacts.root / "runs" / RUN / "verdict.json").unlink()
    with pytest.raises(BundleUnavailable):
        await run.bundler(signer).build(run.ctx)
    assert await run.bundles.get(RUN) is None


async def test_both_attempts_go_in_and_the_judged_one_is_reproduced(
    run: Run, signer: FakeSigner
) -> None:
    second = {**exec_manifest(sha(run.workspace_tar)), "attempt": 2}
    second["paths"] = {**second["paths"], "results": "/mnt/run/results/2"}
    run.artifacts.put_json(f"runs/{RUN}/exec_manifest_2.json", second)
    run.put("results/2/results.json", b'{"exit_reason": "completed"}')
    doc = run.artifacts.get_json(f"runs/{RUN}/verdict.json")
    run.artifacts.put_json(f"runs/{RUN}/verdict.json", {**doc, "attempt": 2})
    files = await _files(run, signer)
    assert "code/exec_manifest_1.json" in files and "code/exec_manifest_2.json" in files
    assert "results/2/results.json" in files
    assert _manifest(files)["judged_attempt"] == 2
    assert "ATTEMPT=2" in files["reproduce.sh"].decode()


async def test_a_failing_signer_leaves_an_unsigned_bundle(run: Run) -> None:
    class Down(FakeSigner):
        async def sign_digest(self, digest: bytes) -> bytes:
            raise SigningError("kms unavailable")

    outcome = await run.bundler(Down()).build(run.ctx)
    assert outcome.signed is False
    stored = await run.bundles.get(RUN)
    assert stored is not None
    files = read_archive(stored)
    assert "SIGNATURE" not in files
    assert verify_files(files, None, allow_unsigned=True) == []


async def test_without_a_signer_the_bundle_is_unsigned(run: Run) -> None:
    outcome = await run.bundler(None).build(run.ctx)
    assert outcome.signed is False and outcome.key == bundle_key(RUN)


async def test_rebuilding_gives_the_same_archive(run: Run) -> None:
    await run.bundler(None).build(run.ctx)
    first = await run.bundles.get(RUN)
    await run.bundler(None).build(run.ctx)
    assert await run.bundles.get(RUN) == first


def test_reproduce_script_runs_the_runner_by_digest_without_network() -> None:
    manifest = ExecManifest.model_validate(exec_manifest("e" * 64))
    script = render_reproduce(manifest, RUNNER, run_id=RUN).decode()
    image = RUNNER.split("@")[0] + "@sha256:" + "0" * 64
    assert f"RUNNER_IMAGE={image}" in script
    assert "--network none" in script
    # all GPUs when the host has one, CPU otherwise (runs judged in a CPU-only Sandbox)
    assert "if command -v nvidia-smi > /dev/null 2>&1; then gpus=(--gpus all); fi" in script
    assert 'docker run --rm "${gpus[@]}" --network none' in script
    assert "a" * 64 + "  overlay.tar.zst" in script
    assert "b" * 64 + "  data/uoft-cs__cifar10.tar.zst" in script
    assert "e" * 64 + "  workspace.tar" in script
    assert "repo.tar.zst" not in script
    assert "{{" not in script
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck not installed")
    result = subprocess.run(  # noqa: S603 (fixed tool, script from our template)
        [shellcheck, "-s", "bash", "-"], input=script.encode(), capture_output=True, check=False
    )
    assert result.returncode == 0, result.stdout.decode()


def test_reproduce_rejects_unsafe_values() -> None:
    manifest = ExecManifest.model_validate(exec_manifest("e" * 64))
    with pytest.raises(ValueError):
        render_reproduce(manifest, "registry.example/x:latest", run_id=RUN)


def test_secret_scan_and_redaction() -> None:
    known = (b"sup3r-s3cret-value",)
    assert find_secrets(b"nothing here", known) is False
    assert find_secrets(b"-----BEGIN OPENSSH " + b"PRIVATE KEY-----", known) is True
    assert find_secrets(b"AKIA" + b"ABCDEFGHIJKLMNOP", known) is True
    assert find_secrets(b"x sup3r-s3cret-value y", known) is True
    cleaned, changed = redact(b"token=sk-abcdefghijklmnopqrstuvwxyz012345 ok", known)
    assert changed and cleaned == b"token=[REDACTED] ok"
    assert redact(b"plain", known) == (b"plain", False)


# A Nebius IAM access token: "v1." and a long base64url protobuf, split for the secret hooks.
NEBIUS_IAM = b"v1." + b"CmMKHHNlcnZpY2VhY2NvdW50LWUwMGFiY2RlZmdoaWprbG1ub3BxcnN0EhRhdXRo" + b".c2ln"


def test_nebius_iam_tokens_are_found_and_redacted() -> None:
    assert find_secrets(b"NEBIUS_IAM_TOKEN=" + NEBIUS_IAM) is True
    cleaned, changed = redact(b"password " + NEBIUS_IAM + b" end")
    assert changed and cleaned == b"password [REDACTED] end"
    for harmless in (b"torch v1.13.1", b"GET /api/v1.json", b"v1.nebius-access-token-value"):
        assert find_secrets(harmless) is False, harmless


def test_secret_values_come_from_the_settings() -> None:
    settings = Settings(nebius_api_key="n" * 40, tavily_api_key="tvly-" + "t" * 20)
    values = secret_values(settings)
    assert "n" * 40 in values and "tvly-" + "t" * 20 in values
    assert settings.session_key.get_secret_value() in values
    assert all(len(v) >= 8 for v in values)


def test_cpu_work_can_cross_a_process_pool() -> None:
    """The app's CPU pool is a process pool: functions, arguments and results must pickle
    (spawning real workers here breaks under pytest-cov on Windows)."""
    import pickle

    from app.bundle.build import (
        BundleFile,
        Omission,
        Origin,
        Reason,
        assemble,
        pack,
        prompt_versions,
    )
    from app.bundle.sign import check_signature

    def through(value: Any) -> Any:
        return pickle.loads(pickle.dumps(value))  # noqa: S301 (our own bytes)

    files = [BundleFile("a.json", b"{}", Origin.RUN_BUCKET, json=True)]
    fn, args = through((assemble, (files, [Omission("b.log", Reason.MISSING)], {"r": 1}, ())))
    assembled = through(fn(*args))
    assert assembled.omitted == ("b.log",)
    archive = through(through(pack)({**assembled.files, "MANIFEST.json": assembled.manifest}))
    assert set(read_archive(archive)) == {"a.json", "MANIFEST.json"}
    assert "judge_explain" in through(through(prompt_versions)())
    assert through(check_signature) is check_signature


async def test_a_kms_key_other_than_the_published_one_leaves_it_unsigned(run: Run) -> None:
    published = (await FakeSigner().public_key_pem()).encode()
    outcome = await run.bundler(FakeSigner(), public_key_pem=published).build(run.ctx)
    assert outcome.signed is False
    stored = await run.bundles.get(RUN)
    assert stored is not None and "SIGNATURE" not in read_archive(stored)


async def test_the_published_key_is_used_when_it_matches(run: Run, signer: FakeSigner) -> None:
    published = (await signer.public_key_pem()).encode()
    assert (await run.bundler(signer, public_key_pem=published).build(run.ctx)).signed


def test_json_escaped_known_secrets_are_found() -> None:
    known = (b'pa"ss\\word-123',)
    escaped = json.dumps({"k": known[0].decode()}).encode()
    assert find_secrets(escaped, known)
    cleaned, changed = redact(escaped, known)
    assert changed and json.loads(cleaned) == {"k": "[REDACTED]"}


@pytest.mark.parametrize(
    "token",
    [
        b"ghp_" + b"a" * 36,
        b"xoxb-1234567890-abcdef",
        b"AIza" + b"B" * 35,
        b"eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.c2lnbmF0dXJlLXZhbHVl",
    ],
)
def test_more_token_shapes_are_found(token: bytes) -> None:
    assert find_secrets(b"x " + token + b" y")

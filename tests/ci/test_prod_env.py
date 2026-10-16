"""The env-file CI hands to deploy/endpoint.sh (.github/scripts/prod_env.py)."""

import json
from pathlib import Path

import pytest

from tests.ci import load_script

prod_env = load_script(".github/scripts/prod_env.py")

REF = "cr.eu-north1.nebius.cloud/reg/{}@sha256:" + "a" * 64
DIGESTS = {
    "commit": "0" * 40,
    "p2p-api": REF.format("p2p-api"),
    "p2p-provisioner": REF.format("p2p-provisioner"),
    "p2p-runner": REF.format("p2p-runner"),
    "p2p-runner-cpu": REF.format("p2p-runner-cpu"),
}
SETTINGS = "S3_REGION=eu-north1\n# comment\n\nP2P_GPU_PREEMPTIBLE=false\n"


def test_settings_then_the_three_job_images() -> None:
    assert prod_env.render(SETTINGS, DIGESTS) == (
        "S3_REGION=eu-north1\n"
        "# comment\n"
        "\n"
        "P2P_GPU_PREEMPTIBLE=false\n"
        f"P2P_PROVISIONER_IMAGE={DIGESTS['p2p-provisioner']}\n"
        f"P2P_RUNNER_IMAGE={DIGESTS['p2p-runner']}\n"
        f"P2P_RUNNER_CPU_IMAGE={DIGESTS['p2p-runner-cpu']}\n"
    )


@pytest.mark.parametrize(
    "settings",
    ["P2P_RUNNER_IMAGE=evil:latest\n", "  P2P_PROVISIONER_IMAGE=x\n", ""],
)
def test_image_keys_belong_to_ci_and_settings_are_required(settings: str) -> None:
    with pytest.raises(ValueError):
        prod_env.render(settings, DIGESTS)


@pytest.mark.parametrize("bad", ["cr.example/p2p-runner:latest", REF.format("p2p-runner") + "\n"])
def test_job_images_must_be_digest_refs(bad: str) -> None:
    with pytest.raises(ValueError, match="p2p-runner"):
        prod_env.render(SETTINGS, {**DIGESTS, "p2p-runner": bad})


def test_main_reads_the_variable_and_the_digests_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    digests = tmp_path / "digests.json"
    digests.write_text(json.dumps(DIGESTS), encoding="utf-8")
    assert prod_env.main([str(digests)], {"P2P_PROD_ENV": SETTINGS}) == 0
    assert "P2P_RUNNER_CPU_IMAGE=" in capsys.readouterr().out
    assert prod_env.main([str(digests)], {}) == 2
    assert "P2P_PROD_ENV" in capsys.readouterr().err

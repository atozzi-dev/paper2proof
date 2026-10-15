"""Provisioner logic with a fake command runner (no network, no docker). The real-image test
(`pytest -m docker`) lands with the provisioner image in A-06."""

import importlib.util
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "provision", ROOT / "images/provisioner/provision.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["provision"] = module
    spec.loader.exec_module(module)
    return module


provision = _load()
RUN = "r_01jb7z00000000000000000000"


def manifest(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "schema": "p2p.provision/v1",
        "run_id": RUN,
        "packages": ["tabulate==0.9.0", "einops"],
        "repo": {"url": "https://github.com/org/repo", "ref": "v1.0"},
        "datasets": [{"kind": "hf", "id": "uoft-cs/cifar10", "split": "train", "max_bytes": 10**9}],
        "smoke_subset": {"dataset": "uoft-cs/cifar10", "rows": 512},
        "hf_token_secret": None,
    }
    data.update(overrides)
    return data


class FakeRunner:
    """Records argv and fakes the side effects of uv, git and tar."""

    def __init__(self, fail_on: str | None = None, stderr: str = "") -> None:
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self.fail_on = fail_on
        self.stderr = stderr

    def __call__(
        self, argv: Sequence[str], *, env: dict[str, str], cwd: Path | None = None
    ) -> None:
        argv = list(argv)
        self.calls.append((argv, env))
        if self.fail_on and self.fail_on in argv:
            raise subprocess.CalledProcessError(1, argv, output="", stderr=self.stderr)
        if argv[:3] == ["uv", "pip", "compile"]:
            Path(argv[argv.index("-o") + 1]).write_text("tabulate==0.9.0 --hash=sha256:abc\n")
        elif argv[:3] == ["uv", "pip", "install"]:
            target = Path(argv[argv.index("--target") + 1])
            (target / "tabulate").mkdir(parents=True)
        elif argv[0] == "tar":
            Path(argv[argv.index("-cf") + 1]).write_bytes(b"zstd")
        elif "clone" in argv:
            dest = Path(argv[-1])
            dest.mkdir(parents=True)
            (dest / "requirements.txt").write_text("torch\neinops==0.8\n")


def fake_datasets(dataset_id: str, split: str | None, dest: Path, rows: int | None) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "part-0.parquet").write_bytes(b"PAR1")


def _paths(tmp_path: Path) -> Any:
    return provision.Paths(
        staging=tmp_path / "staging", cache=tmp_path / "cache", work=tmp_path / "work"
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"schema": "p2p.provision/v2"},
        {"run_id": "r_BAD"},
        {"packages": ["git+https://github.com/a/b"]},
        {"packages": ["pkg @ https://x/y.whl"]},
        {"packages": ["--index-url=https://evil"]},
        {"packages": [f"p{i}" for i in range(31)]},
        {"repo": {"url": "https://user:pass@github.com/org/repo", "ref": "main"}},
        {"repo": {"url": "https://gitlab.com/org/repo", "ref": "main"}},
        {"repo": {"url": "https://github.com/org/repo", "ref": "--upload-pack=touch /tmp/x"}},
        {"repo": {"url": "https://github.com/org/repo", "ref": "../../x"}},
        {"datasets": [{"kind": "hf", "id": "../etc", "split": "train", "max_bytes": 1}]},
        {"datasets": [{"kind": "url", "id": "https://x", "split": None, "max_bytes": 1}]},
        {"smoke_subset": {"dataset": "uoft-cs/cifar10", "rows": 10**7}},
        {"hf_token_secret": "hf_abcdefghijklmnop"},
    ],
)
def test_manifest_validation_rejects_unsafe_inputs(overrides: dict[str, Any]) -> None:
    with pytest.raises(provision.ProvisionError):
        provision.validate_manifest(manifest(**overrides))


def test_wheels_only_with_hashes(tmp_path: Path) -> None:
    runner = FakeRunner()
    provision.provision(
        provision.validate_manifest(manifest()), _paths(tmp_path), runner, fake_datasets
    )
    argvs = [a for a, _ in runner.calls]
    compile_cmd = next(a for a in argvs if a[:3] == ["uv", "pip", "compile"])
    install_cmd = next(a for a in argvs if a[:3] == ["uv", "pip", "install"])
    assert "--only-binary=:all:" in compile_cmd and "--generate-hashes" in compile_cmd
    assert "--only-binary=:all:" in install_cmd and "--require-hashes" in install_cmd
    requirements_in = Path(compile_cmd[-1]).read_text().splitlines()
    assert requirements_in == ["tabulate==0.9.0", "einops"]


def test_repo_is_cloned_shallow_without_hooks_or_setup(tmp_path: Path) -> None:
    runner = FakeRunner()
    provision.provision(
        provision.validate_manifest(manifest()), _paths(tmp_path), runner, fake_datasets
    )
    clone, env = next((a, e) for a, e in runner.calls if "clone" in a)
    assert clone[:1] == ["git"]
    for flag in (
        "--depth",
        "--single-branch",
        "--no-tags",
        "core.hooksPath=/dev/null",
        "protocol.file.allow=never",
    ):
        assert flag in clone
    assert clone[clone.index("--") + 1] == "https://github.com/org/repo"
    assert "--recurse-submodules" not in clone
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert not any(a[:2] == ["pip", "install"] or "setup.py" in a for a, _ in runner.calls)


def test_subprocess_env_carries_no_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEBIUS_API_KEY", "should-never-leak")
    runner = FakeRunner()
    provision.provision(
        provision.validate_manifest(manifest()), _paths(tmp_path), runner, fake_datasets
    )
    for _, env in runner.calls:
        assert "NEBIUS_API_KEY" not in env
        assert set(env) <= provision.ALLOWED_ENV


def test_outputs_and_report(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    report = provision.provision(
        provision.validate_manifest(manifest()), paths, FakeRunner(), fake_datasets
    )
    for name in (
        "lock.txt",
        "overlay.tar.zst",
        "repo.tar.zst",
        "data/uoft-cs__cifar10.tar.zst",
        "data/smoke.tar.zst",
    ):
        assert (paths.staging / name).is_file(), name
    stored = json.loads((paths.staging / "provision_report.json").read_text())
    assert stored == report
    assert stored["requirement_files"]["requirements.txt"].startswith("torch")
    assert len(stored["lock_sha256"]) == 64


def test_overlay_cache_hit_skips_install(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    provision.provision(provision.validate_manifest(manifest()), paths, FakeRunner(), fake_datasets)
    second = FakeRunner()
    paths2 = provision.Paths(
        staging=tmp_path / "staging2", cache=paths.cache, work=tmp_path / "work2"
    )
    report = provision.provision(
        provision.validate_manifest(manifest()), paths2, second, fake_datasets
    )
    assert report["overlay_cache_hit"] is True
    assert not any(a[:3] == ["uv", "pip", "install"] for a, _ in second.calls)
    assert (paths2.staging / "overlay.tar.zst").is_file()


def test_sdist_only_packages_fail_with_a_clear_error(tmp_path: Path) -> None:
    runner = FakeRunner(
        fail_on="compile", stderr="no wheels with a matching platform tag (sdist only)"
    )
    with pytest.raises(provision.ProvisionError, match="no wheel"):
        provision.provision(
            provision.validate_manifest(manifest()), _paths(tmp_path), runner, fake_datasets
        )


def test_no_packages_no_repo_still_works(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    report = provision.provision(
        provision.validate_manifest(
            manifest(packages=[], repo=None, datasets=[], smoke_subset=None)
        ),
        paths,
        FakeRunner(),
        fake_datasets,
    )
    assert report["repo"] is False
    assert (paths.staging / "overlay.tar.zst").is_file()  # empty overlay, same contract

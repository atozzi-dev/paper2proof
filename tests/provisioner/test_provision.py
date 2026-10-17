"""Provisioner logic with a fake command runner (no network, no docker); the real-image tests
are in tests/images/test_docker.py. Two phases: `packages` (lock, overlay, the shared cache) and
`fetch` (repository and datasets, never the cache)."""

import hashlib
import importlib.util
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

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
LOCK = (
    "tabulate==0.9.0 \\\n    --hash=sha256:aaa\n"
    "torch==2.4.0 \\\n    --hash=sha256:bbb\n"
    "nvidia-cublas-cu12==12.1.3.1 \\\n    --hash=sha256:ccc\n"
)


def manifest(**overrides: Any) -> dict[str, Any]:
    """The provision job's manifest (phase `packages`)."""
    data: dict[str, Any] = {
        "schema": "p2p.provision/v2",
        "phase": "packages",
        "run_id": RUN,
        "packages": ["tabulate==0.9.0", "einops"],
        "cache_salt": "py3.12-x86_64-manylinux_2_28-sha256:" + "d" * 64,
        "policy": {
            "denylist": ["pip", "uv", "setuptools", "wheel"],
            "popular": ["requests", "numpy"],
        },
    }
    data.update(overrides)
    return data


def fetch_manifest(**overrides: Any) -> dict[str, Any]:
    """The fetch job's manifest (phase `fetch`)."""
    data: dict[str, Any] = {
        "schema": "p2p.provision/v2",
        "phase": "fetch",
        "run_id": RUN,
        "repo": {"url": "https://github.com/org/repo", "ref": "v1.0"},
        "datasets": [{"kind": "hf", "id": "uoft-cs/cifar10", "split": "train", "max_bytes": 10**9}],
        "smoke_subset": {"dataset": "uoft-cs/cifar10", "rows": 512},
        "hf_token_secret": None,
    }
    data.update(overrides)
    return data


class FakeRunner:
    """Records argv and fakes the side effects of uv, git and tar."""

    def __init__(self, fail_on: str | None = None, stderr: str = "", lock: str = LOCK) -> None:
        self.calls: list[tuple[list[str], dict[str, str], float]] = []
        self.fail_on = fail_on
        self.stderr = stderr
        self.lock = lock

    def __call__(
        self, argv: Sequence[str], *, env: dict[str, str], timeout: float, cwd: Path | None = None
    ) -> str:
        argv = list(argv)
        self.calls.append((argv, env, timeout))
        if self.fail_on and self.fail_on in argv:
            raise subprocess.CalledProcessError(1, argv, output="", stderr=self.stderr)
        if argv[:3] == ["uv", "pip", "compile"]:
            Path(argv[argv.index("-o") + 1]).write_text(self.lock)
        elif argv[:3] == ["uv", "pip", "install"]:
            target = Path(argv[argv.index("--target") + 1])
            (target / "tabulate").mkdir(parents=True)
        elif argv[0] == "tar":
            Path(argv[argv.index("-cf") + 1]).write_bytes(b"zstd:" + " ".join(argv).encode())
        elif "clone" in argv:
            dest = Path(argv[-1])
            dest.mkdir(parents=True)
            (dest / "requirements.txt").write_text("torch\neinops==0.8\n")
        elif "rev-parse" in argv:
            return "0123456789abcdef0123456789abcdef01234567\n"
        return ""


def fake_fetch(dataset_id: str, split: str | None, dest: Path, max_bytes: int) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "train-00000-of-00001.parquet").write_bytes(b"PAR1")


def fake_smoke(source: Path, dest: Path, rows: int) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "smoke.parquet").write_bytes(b"PAR1-smoke")


def _paths(tmp_path: Path, name: str = "", cache: str = "cache") -> Any:
    constraints = tmp_path / "runner-constraints.txt"
    constraints.write_text("torch==2.4.0\nnumpy==2.1.0\nnvidia-cublas-cu12==12.1.3.1\n")
    return provision.Paths(
        staging=tmp_path / f"staging{name}",
        cache=tmp_path / cache,
        work=tmp_path / f"work{name}",
        constraints=constraints,
    )


def _never(*args: Any) -> None:
    raise AssertionError("the packages phase must never fetch or parse datasets")


def _provision(
    tmp_path: Path, runner: FakeRunner | None = None, name: str = "", **overrides: Any
) -> Any:
    paths = _paths(tmp_path, name)
    report = provision.provision(
        provision.validate_manifest(manifest(**overrides)),
        paths,
        runner or FakeRunner(),
        _never,
        _never,
        deadline_s=1080,
    )
    return report, paths


def _fetch(
    tmp_path: Path, runner: FakeRunner | None = None, name: str = "", **overrides: Any
) -> Any:
    """The fetch phase runs without the cache: its cache path never exists."""
    paths = _paths(tmp_path, name, cache=f"no-cache{name}")
    report = provision.provision(
        provision.validate_manifest(fetch_manifest(**overrides)),
        paths,
        runner or FakeRunner(),
        fake_fetch,
        fake_smoke,
        deadline_s=1080,
    )
    return report, paths


@pytest.mark.parametrize(
    "overrides",
    [
        {"schema": "p2p.provision/v1"},
        {"phase": None},
        {"phase": "everything"},
        {"run_id": "r_BAD"},
        {"packages": ["git+https://github.com/a/b"]},
        {"packages": ["pkg @ https://x/y.whl"]},
        {"packages": ["--index-url=https://evil"]},
        {"packages": [f"p{i}" for i in range(31)]},
        {"cache_salt": ""},
        {"trusted_overlays": {"not-hex": "x"}},
        {"deadline_unix": "soon"},
        {"deadline_unix": float("inf")},
        {"deadline_unix": float("nan")},
        # the fetch phase's inputs never reach the job that writes the cache
        {"repo": {"url": "https://github.com/org/repo", "ref": None}},
        {"repo": None},
        {"datasets": []},
        {"smoke_subset": None},
        {"hf_token_secret": None},
        {"surprise": True},
    ],
)
def test_packages_manifest_validation_rejects_unsafe_inputs(overrides: dict[str, Any]) -> None:
    with pytest.raises(provision.ProvisionError):
        provision.validate_manifest(manifest(**overrides))


@pytest.mark.parametrize(
    "overrides",
    [
        {"schema": "p2p.provision/v1"},
        {"run_id": "r_BAD"},
        {"repo": {"url": "https://user:pass@github.com/org/repo", "ref": "main"}},
        {"repo": {"url": "https://gitlab.com/org/repo", "ref": "main"}},
        {"repo": {"url": "https://github.com/org/repo", "ref": "--upload-pack=touch /tmp/x"}},
        {"repo": {"url": "https://github.com/org/repo", "ref": "../../x"}},
        {"datasets": [{"kind": "hf", "id": "../etc", "split": "train", "max_bytes": 1}]},
        {"datasets": [{"kind": "url", "id": "https://x", "split": None, "max_bytes": 1}]},
        {"datasets": [{"kind": "hf", "id": "a/b", "split": "train[:10%]", "max_bytes": 1}]},
        {"datasets": [{"kind": "hf", "id": "a/b", "split": "train", "max_bytes": -1}]},
        {"datasets": [{"kind": "hf", "id": "a/b", "split": "train", "max_bytes": 30 * 2**30}]},
        {
            "datasets": [
                {"kind": "hf", "id": f"a/b{i}", "split": "train", "max_bytes": 1} for i in range(4)
            ]
        },
        {"smoke_subset": {"dataset": "uoft-cs/cifar10", "rows": 10**7}},
        {"hf_token_secret": "hf_abcdefghijklmnop"},
        {"repo": "not-a-dict"},
        {"datasets": ["not-a-dict"]},
        {"deadline_unix": float("inf")},
        # the job that touches untrusted bytes gets nothing about the cache or the packages
        {"packages": ["einops"]},
        {"packages": []},
        {"trusted_overlays": {}},
        {"cache_salt": "py3.12"},
        {"policy": {"denylist": [], "popular": []}},
    ],
)
def test_fetch_manifest_validation_rejects_unsafe_inputs(overrides: dict[str, Any]) -> None:
    with pytest.raises(provision.ProvisionError):
        provision.validate_manifest(fetch_manifest(**overrides))


def test_wheels_only_with_hashes_constrained_to_the_runner(tmp_path: Path) -> None:
    runner = FakeRunner()
    _, paths = _provision(tmp_path, runner)
    argvs = [a for a, _, _ in runner.calls]
    compile_cmd = next(a for a in argvs if a[:3] == ["uv", "pip", "compile"])
    install_cmd = next(a for a in argvs if a[:3] == ["uv", "pip", "install"])
    for flag in ("--only-binary=:all:", "--generate-hashes", "--no-config", "--python-version"):
        assert flag in compile_cmd
    assert compile_cmd[compile_cmd.index("-c") + 1] == str(paths.constraints)
    assert compile_cmd[compile_cmd.index("--index-url") + 1] == "https://pypi.org/simple"
    for flag in ("--only-binary=:all:", "--require-hashes", "--no-deps", "--no-config"):
        assert flag in install_cmd
    # The runner's torch is PyTorch's CUDA 12.6 build: packages that depend on torch must
    # resolve it from that index (or the constraints have no solution), and nothing else may
    # come from there. The install only fetches what the runner lacks: PyPI alone.
    for cmd in (compile_cmd, install_cmd):
        assert "--index-strategy" not in cmd and "--extra-index-url" not in cmd
    project = Path(compile_cmd[-1])
    assert project.name == "pyproject.toml"
    text = project.read_text()
    assert '"tabulate==0.9.0"' in text and '"torch"' in text
    assert 'torch = { index = "pytorch-cu126" }' in text
    assert 'url = "https://download.pytorch.org/whl/cu126"' in text and "explicit = true" in text
    # Packages already in the runner image are dropped from what gets installed.
    install_lock = Path(install_cmd[install_cmd.index("-r") + 1]).read_text()
    assert "tabulate==0.9.0" in install_lock
    assert "torch" not in install_lock and "nvidia" not in install_lock
    assert (paths.staging / "lock.txt").read_text() == LOCK  # full lock kept for the bundle


def test_transitive_dependencies_are_checked(tmp_path: Path) -> None:
    bad = "tabulate==0.9.0 \\\n    --hash=sha256:a\nreqeusts==1.0 \\\n    --hash=sha256:b\n"
    with pytest.raises(provision.ProvisionError, match="reqeusts"):
        _provision(tmp_path, FakeRunner(lock=bad))
    denied = "tabulate==0.9.0 \\\n    --hash=sha256:a\nsetuptools==70.0 \\\n    --hash=sha256:b\n"
    with pytest.raises(provision.ProvisionError, match="setuptools"):
        _provision(tmp_path, FakeRunner(lock=denied), name="2")


def test_repo_is_cloned_shallow_without_hooks_or_setup(tmp_path: Path) -> None:
    runner = FakeRunner()
    report, _ = _fetch(tmp_path, runner)
    clone, env, _ = next(c for c in runner.calls if "clone" in c[0])
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
    assert env["GIT_LFS_SKIP_SMUDGE"] == "1"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert report["repo_commit"] == "0123456789abcdef0123456789abcdef01234567"
    assert not any("setup.py" in " ".join(a) for a, _, _ in runner.calls)


def test_subprocess_env_is_minimal_and_caches_are_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NEBIUS_API_KEY", "should-never-leak")
    for run in (_provision, _fetch):
        runner = FakeRunner()
        _, paths = run(tmp_path, runner, name=run.__name__)
        assert runner.calls
        for _, env, _ in runner.calls:
            assert "NEBIUS_API_KEY" not in env
            assert set(env) <= provision.ALLOWED_ENV
            assert env["UV_PYTHON_DOWNLOADS"] == "never"
            assert env["UV_CACHE_DIR"].startswith(str(paths.work))  # never on the shared mount
            assert env["HF_HOME"].startswith(str(paths.work))


def test_every_command_is_bounded_by_the_deadline(tmp_path: Path) -> None:
    for run in (_provision, _fetch):
        runner = FakeRunner()
        run(tmp_path, runner, name=run.__name__)
        assert all(0 < timeout <= 900 for _, _, timeout in runner.calls)


def test_packages_outputs_and_report(tmp_path: Path) -> None:
    report, paths = _provision(tmp_path)
    for name in ("lock.txt", "overlay.tar.zst"):
        assert (paths.staging / name).is_file(), name
    for name in ("repo.tar.zst", "repo_files.json", "data", "fetch_report.json"):
        assert not (paths.staging / name).exists(), name
    stored = json.loads((paths.staging / "packages_report.json").read_text())
    assert stored == report
    assert set(stored) == {
        "schema", "run_id", "lock_sha256", "overlay_cache_key", "overlay_cache_hit",
        "overlay_sha256",
    }  # fmt: skip
    assert stored["schema"] == "p2p.packages-report/v1"
    overlay = (paths.staging / "overlay.tar.zst").read_bytes()
    assert stored["overlay_sha256"] == hashlib.sha256(overlay).hexdigest()
    lock = (paths.staging / "lock.txt").read_bytes()
    assert stored["lock_sha256"] == hashlib.sha256(lock).hexdigest()


def test_fetch_outputs_and_report(tmp_path: Path) -> None:
    report, paths = _fetch(tmp_path)
    for name in (
        "repo.tar.zst",
        "repo_files.json",
        "data/uoft-cs__cifar10.tar.zst",
        "data/smoke.tar.zst",
    ):
        assert (paths.staging / name).is_file(), name
    for name in ("lock.txt", "overlay.tar.zst", "packages_report.json"):
        assert not (paths.staging / name).exists(), name
    stored = json.loads((paths.staging / "fetch_report.json").read_text())
    assert stored == report
    assert stored["schema"] == "p2p.fetch-report/v1"
    assert "overlay_sha256" not in stored and "overlay_cache_key" not in stored
    assert stored["requirement_files"]["requirements.txt"].startswith("torch")
    assert stored["datasets"] == ["uoft-cs__cifar10"] and stored["smoke"] is True


def test_the_fetch_phase_refuses_to_run_with_the_cache_mounted(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.cache.mkdir()
    runner = FakeRunner()
    with pytest.raises(provision.ProvisionError, match="cache"):
        provision.provision(
            provision.validate_manifest(fetch_manifest()), paths, runner, fake_fetch,
            fake_smoke, deadline_s=1080,
        )  # fmt: skip
    assert runner.calls == []  # nothing untrusted was touched


def test_the_fetch_phase_never_creates_the_cache(tmp_path: Path) -> None:
    _, paths = _fetch(tmp_path)
    assert not paths.cache.exists()


def test_the_packages_phase_never_touches_the_repository(tmp_path: Path) -> None:
    runner = FakeRunner()
    _provision(tmp_path, runner)  # _never fails if a dataset is fetched or parsed
    assert not any(argv[0] == "git" for argv, _, _ in runner.calls)


def test_stale_outputs_of_a_previous_attempt_are_removed(tmp_path: Path) -> None:
    paths = _paths(tmp_path, cache="no-cache")
    (paths.staging / "data").mkdir(parents=True)
    (paths.staging / "provision_error.json").write_text('{"error": "old"}')
    (paths.staging / "data" / "old-dataset.tar.zst").write_bytes(b"x")
    provision.provision(
        provision.validate_manifest(fetch_manifest()),
        paths,
        FakeRunner(),
        fake_fetch,
        fake_smoke,
        deadline_s=1080,
    )
    assert not (paths.staging / "provision_error.json").exists()
    assert not (paths.staging / "data" / "old-dataset.tar.zst").exists()
    (paths.staging / "provision_error.json").write_text('{"error": "old"}')
    _, packages = _provision(tmp_path)
    assert packages.staging == paths.staging
    assert not (paths.staging / "provision_error.json").exists()


def test_each_phase_keeps_the_other_phase_s_outputs(tmp_path: Path) -> None:
    # a later attempt runs a new provision job and reuses the fetch job's archives
    _, fetched = _fetch(tmp_path)
    _, paths = _provision(tmp_path)
    assert fetched.staging == paths.staging
    for name in ("repo.tar.zst", "repo_files.json", "data/smoke.tar.zst", "fetch_report.json"):
        assert (paths.staging / name).is_file(), name
    paths = provision.Paths(  # a new job: same bucket, fresh local disk
        staging=paths.staging, cache=tmp_path / "no-cache", work=tmp_path / "work-again"
    )
    provision.provision(
        provision.validate_manifest(fetch_manifest()), paths, FakeRunner(), fake_fetch,
        fake_smoke, deadline_s=1080,
    )  # fmt: skip
    for name in ("lock.txt", "overlay.tar.zst", "packages_report.json"):
        assert (paths.staging / name).is_file(), name


def _trusted(report: dict[str, Any]) -> dict[str, str]:
    return {report["overlay_cache_key"]: report["overlay_sha256"]}


def test_overlay_cache_hit_skips_install_and_is_verified(tmp_path: Path) -> None:
    first, _ = _provision(tmp_path)
    second = FakeRunner()
    report, _ = _provision(tmp_path, second, name="2", trusted_overlays=_trusted(first))
    assert report["overlay_cache_hit"] is True
    assert not any(a[:3] == ["uv", "pip", "install"] for a, _, _ in second.calls)
    # A replaced cache entry (and its sidecar) is detected and rebuilt, never served.
    entry = next((tmp_path / "cache" / "overlays").glob("*.tar.zst"))
    entry.write_bytes(b"backdoored")
    entry.with_name(entry.name.replace(".tar.zst", ".sha256")).write_text(
        hashlib.sha256(b"backdoored").hexdigest()
    )
    third = FakeRunner()
    report3, _ = _provision(tmp_path, third, name="3", trusted_overlays=_trusted(first))
    assert report3["overlay_cache_hit"] is False
    assert any(a[:3] == ["uv", "pip", "install"] for a, _, _ in third.calls)
    assert not list((tmp_path / "cache" / "overlays").glob(".tmp-*"))


def test_cache_entries_the_control_plane_does_not_know_are_not_served(tmp_path: Path) -> None:
    _provision(tmp_path)
    report, _ = _provision(tmp_path, name="2")  # nothing trusted
    assert report["overlay_cache_hit"] is False


def test_cache_key_includes_the_salt_and_the_constraints(tmp_path: Path) -> None:
    first, paths = _provision(tmp_path)
    report, _ = _provision(
        tmp_path, name="2", cache_salt="py3.12-other-image", trusted_overlays=_trusted(first)
    )
    assert report["overlay_cache_key"] != first["overlay_cache_key"]
    paths.constraints.write_text("torch==2.5.0\n")
    report3 = provision.provision(
        provision.validate_manifest(manifest(trusted_overlays=_trusted(first))),
        provision.Paths(
            staging=tmp_path / "s3", cache=tmp_path / "cache", work=tmp_path / "w3",
            constraints=paths.constraints,
        ),
        FakeRunner(), _never, _never, deadline_s=1080,
    )  # fmt: skip
    assert report3["overlay_cache_key"] != first["overlay_cache_key"]


def test_image_provided_dependencies_are_not_judged(tmp_path: Path) -> None:
    lock = LOCK + "setuptools==70.0 \\\n    --hash=sha256:d\n"
    paths = _paths(tmp_path)
    paths.constraints.write_text("torch==2.4.0\nsetuptools==70.0\nnvidia-cublas-cu12==12.1.3.1\n")
    provision.provision(
        provision.validate_manifest(manifest()), paths, FakeRunner(lock=lock), _never, _never,
        deadline_s=1080,
    )  # fmt: skip


def test_missing_constraints_fail_closed(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.constraints.unlink()
    with pytest.raises(provision.ProvisionError, match="constraints"):
        provision.provision(
            provision.validate_manifest(manifest()), paths, FakeRunner(), _never, _never,
            deadline_s=1080,
        )  # fmt: skip


def test_install_targets_the_runner_platform_and_archives_are_deterministic(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    _provision(tmp_path, runner)
    install = next(a for a, _, _ in runner.calls if a[:3] == ["uv", "pip", "install"])
    assert install[install.index("--python-version") + 1] == "3.12"
    assert install[install.index("--python-platform") + 1] == "x86_64-manylinux_2_28"
    _fetch(tmp_path, runner)
    tars = [a for a, _, _ in runner.calls if a[0] == "tar"]
    assert len(tars) == 4  # overlay, repo, dataset, smoke subset
    for tar in tars:
        for flag in ("--sort=name", "--mtime=@0", "--owner=0", "--group=0", "--numeric-owner"):
            assert flag in tar


def test_the_control_plane_deadline_is_enforced(tmp_path: Path) -> None:
    for run in (_provision, _fetch):
        with pytest.raises(provision.ProvisionError, match="deadline"):
            run(tmp_path, deadline_unix=1.0)


def test_command_timeouts_have_a_clear_message(tmp_path: Path) -> None:
    class Slow(FakeRunner):
        def __call__(self, argv: Sequence[str], **kwargs: Any) -> str:
            if list(argv)[:3] == ["uv", "pip", "compile"]:
                raise subprocess.TimeoutExpired(list(argv), 900)
            return super().__call__(argv, **kwargs)

    with pytest.raises(provision.ProvisionError, match="timed out while locking packages"):
        _provision(tmp_path, Slow())


def test_requirement_text_in_the_report_is_capped(tmp_path: Path) -> None:
    class BigRepo(FakeRunner):
        def __call__(self, argv: Sequence[str], **kwargs: Any) -> str:
            out = super().__call__(argv, **kwargs)
            if "clone" in argv:
                dest = Path(list(argv)[-1])
                for i in range(5):
                    (dest / f"requirements-{i}.txt").write_text("é" * 60_000)
            return out

    report, paths = _fetch(tmp_path, BigRepo())
    assert sum(len(t) for t in report["requirement_files"].values()) <= 128 * 1024
    assert len((paths.staging / "fetch_report.json").read_bytes()) < 512 * 1024


def test_raw_dataset_files_are_removed_after_packing(tmp_path: Path) -> None:
    _, paths = _fetch(tmp_path)
    assert not list((paths.work / "data").rglob("*.parquet"))


def test_split_files_match_whole_names_only() -> None:
    names = [
        "plain_text/train-00000-of-00001.parquet",
        "plain_text/test-00000-of-00001.parquet",
        "data/contest-00000.parquet",
        "test/0000.parquet",
        "README.md",
    ]
    assert provision.split_files(names, "test") == [
        "plain_text/test-00000-of-00001.parquet",
        "test/0000.parquet",
    ]


def test_sdist_only_packages_fail_with_a_clear_error(tmp_path: Path) -> None:
    runner = FakeRunner(
        fail_on="compile", stderr="no wheels with a matching platform tag (sdist only)"
    )
    with pytest.raises(provision.ProvisionError, match="no wheel"):
        _provision(tmp_path, runner)


def test_no_packages_no_repo_still_works(tmp_path: Path) -> None:
    _, paths = _provision(tmp_path, packages=[])
    assert (paths.staging / "overlay.tar.zst").is_file()  # empty overlay, same contract
    report, _ = _fetch(tmp_path, repo=None, datasets=[], smoke_subset=None)
    assert report["repo"] is False and report["data_archives"] == []


def test_oversized_repo_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provision, "MAX_REPO_BYTES", 5)
    with pytest.raises(provision.ProvisionError, match="repository"):
        _fetch(tmp_path)


def test_main_writes_an_error_file_for_any_failure(tmp_path: Path) -> None:
    bad = tmp_path / "manifest.json"
    bad.write_text(
        json.dumps({"schema": "p2p.provision/v2", "phase": "fetch", "run_id": RUN, "repo": 5})
    )
    staging = tmp_path / "staging"
    code = provision.main(
        [
            "--manifest",
            str(bad),
            "--staging",
            str(staging),
            "--cache",
            str(tmp_path / "c"),
            "--work",
            str(tmp_path / "w"),
        ]
    )
    assert code == 1
    assert "error" in json.loads((staging / "provision_error.json").read_text())


def test_requirement_text_budget_counts_its_json_encoding(tmp_path: Path) -> None:
    class ControlChars(FakeRunner):
        def __call__(self, argv: Sequence[str], **kwargs: Any) -> str:
            out = super().__call__(argv, **kwargs)
            if "clone" in argv:
                dest = Path(list(argv)[-1])
                for i in range(3):
                    (dest / f"requirements-{i}.txt").write_text("\x01" * 60_000)
            return out

    _, paths = _fetch(tmp_path, ControlChars())
    assert len((paths.staging / "fetch_report.json").read_bytes()) < 400 * 1024


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_planted_symlinks_in_staging_are_removed_not_followed(tmp_path: Path) -> None:
    paths = _paths(tmp_path, cache="no-cache")
    paths.staging.mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep")
    (paths.staging / "data").symlink_to(victim, target_is_directory=True)
    (paths.staging / "lock.txt").symlink_to(victim / "keep.txt")
    (paths.staging / "overlay.tar.zst").symlink_to(victim / "keep.txt")
    provision.provision(
        provision.validate_manifest(fetch_manifest()), paths, FakeRunner(), fake_fetch,
        fake_smoke, deadline_s=1080,
    )  # fmt: skip
    assert not (paths.staging / "data").is_symlink()
    _provision(tmp_path)  # same staging directory
    assert (victim / "keep.txt").read_text() == "keep"
    assert not (paths.staging / "lock.txt").is_symlink()
    assert not (paths.staging / "overlay.tar.zst").is_symlink()


def test_the_deadline_handler_writes_the_error_and_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exits: list[int] = []
    monkeypatch.setattr(provision.os, "_exit", exits.append)
    staging = tmp_path / "staging"
    provision.deadline_expired(staging)
    assert exits == [1]
    assert "deadline" in json.loads((staging / "provision_error.json").read_text())["error"]


def test_a_repository_snapshot_is_written_with_its_digest(tmp_path: Path) -> None:
    import hashlib

    class Repo(FakeRunner):
        def __call__(self, argv: Sequence[str], **kwargs: Any) -> str:
            out = super().__call__(argv, **kwargs)
            if "clone" in argv:
                dest = Path(list(argv)[-1])
                (dest / "src").mkdir()
                (dest / "src" / "model.py").write_text("class Net: ...\n")
                (dest / "weights.bin").write_bytes(b"\x00" * 10)
                (dest / "notes.md").write_text("x" * (300 * 1024))  # too large: skipped
            return out

    report, paths = _fetch(tmp_path, Repo())
    raw = (paths.staging / "repo_files.json").read_bytes()
    assert report["repo_files_sha256"] == hashlib.sha256(raw).hexdigest()
    files = json.loads(raw)["files"]
    assert files["src/model.py"] == "class Net: ...\n"
    assert "weights.bin" not in files and "notes.md" not in files


def test_the_deadline_exit_happens_even_when_the_error_cannot_be_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exits: list[int] = []
    monkeypatch.setattr(provision.os, "_exit", exits.append)
    blocked = tmp_path / "staging"
    blocked.write_text("a file where the directory should be")
    provision.deadline_expired(blocked)
    assert exits == [1]


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_a_symlinked_staging_directory_is_replaced(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    for data, phase, cache in ((_never, manifest, "cache"), (fake_fetch, fetch_manifest, "nc")):
        smoke = fake_smoke if data is fake_fetch else _never
        paths = _paths(tmp_path, phase.__name__, cache=cache)
        paths.staging.symlink_to(elsewhere, target_is_directory=True)
        provision.provision(
            provision.validate_manifest(phase()), paths, FakeRunner(), data, smoke,
            deadline_s=1080,
        )  # fmt: skip
        assert not paths.staging.is_symlink()
        assert not list(elsewhere.iterdir())


def test_the_snapshot_budget_counts_its_encoding_and_keeps_sources_first(tmp_path: Path) -> None:
    class Notebooks(FakeRunner):
        def __call__(self, argv: Sequence[str], **kwargs: Any) -> str:
            out = super().__call__(argv, **kwargs)
            if "clone" in argv:
                dest = Path(list(argv)[-1])
                (dest / "a_notebooks").mkdir()
                for i in range(30):
                    (dest / "a_notebooks" / f"n{i}.ipynb").write_text('"\\' * 90_000)
                (dest / "src").mkdir()
                (dest / "src" / "model.py").write_text("class Net: ...\n")
            return out

    _, paths = _fetch(tmp_path, Notebooks())
    raw = (paths.staging / "repo_files.json").read_bytes()
    assert len(raw) <= int(3.5 * 1024 * 1024)
    assert "src/model.py" in json.loads(raw)["files"]


def test_the_report_records_every_archive_digest(tmp_path: Path) -> None:
    import hashlib

    report, paths = _fetch(tmp_path)

    def sha(name: str) -> str:
        return hashlib.sha256((paths.staging / name).read_bytes()).hexdigest()

    assert report["repo_sha256"] == sha("repo.tar.zst")
    assert report["data_archives"] == [
        {"name": "uoft-cs__cifar10", "sha256": sha("data/uoft-cs__cifar10.tar.zst")},
        {"name": "smoke", "sha256": sha("data/smoke.tar.zst")},
    ]


@given(
    st.lists(
        st.from_regex(provision.PACKAGE, fullmatch=True).filter(lambda p: len(p) <= 140),
        max_size=5,
    )
)
def test_any_valid_package_list_becomes_the_same_toml_dependencies(packages: list[str]) -> None:
    import tomllib

    data = tomllib.loads(provision._lock_input(packages))
    assert data["project"]["dependencies"] == [*packages, "torch", "torchvision"]
    assert set(data["tool"]["uv"]["sources"]) == {"torch", "torchvision"}


def test_the_cache_is_published_from_the_local_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the run bucket is shared with untrusted jobs: what lands in the cache never comes from it
    publish = provision._publish_to_cache

    def swapped_first(overlay: Path, cached: Path, digest: str, run_id: str) -> None:
        staging = tmp_path / "staging" / "overlay.tar.zst"
        staging.parent.mkdir(parents=True, exist_ok=True)
        staging.write_bytes(b"swapped by another job")
        publish(overlay, cached, digest, run_id)

    monkeypatch.setattr(provision, "_publish_to_cache", swapped_first)
    report, paths = _provision(tmp_path)
    entry = paths.cache / "overlays" / f"{report['overlay_cache_key']}.tar.zst"
    assert hashlib.sha256(entry.read_bytes()).hexdigest() == report["overlay_sha256"]
    staged = (paths.staging / "overlay.tar.zst").read_bytes()
    assert hashlib.sha256(staged).hexdigest() == report["overlay_sha256"]
    assert not list(paths.work.rglob("*.local"))  # the local archive is cleaned up


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_planted_report_and_error_symlinks_are_not_followed(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.staging.mkdir(parents=True)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    for name in ("packages_report.json", "provision_error.json"):
        (paths.staging / name).symlink_to(victim)
    _provision(tmp_path)
    assert victim.read_text() == "keep"
    assert not (paths.staging / "packages_report.json").is_symlink()


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_an_early_error_is_written_without_following_symlinks(tmp_path: Path) -> None:
    # the job started after its deadline: the error is written before staging is cleaned
    staging = tmp_path / "staging"
    staging.mkdir()
    victim = tmp_path / "cached-overlay.tar.zst"
    victim.write_bytes(b"overlay")
    (staging / "provision_error.json").symlink_to(victim)
    late = tmp_path / "manifest.json"
    late.write_text(json.dumps(manifest(deadline_unix=1.0)))
    argv = ["--manifest", str(late), "--staging", str(staging), "--cache", str(tmp_path / "c"),
            "--work", str(tmp_path / "w")]  # fmt: skip
    assert provision.main(argv) == 1
    assert victim.read_bytes() == b"overlay"
    assert "deadline" in json.loads((staging / "provision_error.json").read_text())["error"]


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_the_deadline_handler_does_not_follow_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(provision.os, "_exit", lambda code: None)
    staging = tmp_path / "staging"
    staging.mkdir()
    victim = tmp_path / "victim"
    victim.write_bytes(b"keep")
    (staging / "provision_error.json").symlink_to(victim)
    provision.deadline_expired(staging)
    assert victim.read_bytes() == b"keep"
    assert "deadline" in json.loads((staging / "provision_error.json").read_text())["error"]


def _transitive(*names: str) -> None:
    data = manifest(
        packages=["einops"],
        policy={"denylist": ["pip"], "popular": ["jax", "tqdm", "requests", "six", "py"]},
    )
    lock = "".join(f"{name}==1.0 \\\n    --hash=sha256:aaa\n" for name in names)
    provision._check_transitive(provision.validate_manifest(data), lock)


@pytest.mark.parametrize("name", ["mlx", "pot", "tfds", "einops", "requests"])
def test_short_real_dependencies_are_not_typosquats(name: str) -> None:
    """The control plane's rule (execution-contracts 4): one edit below 5 characters."""
    _transitive(name)


@pytest.mark.parametrize("name", ["jaxx", "tqmd", "reqeusts", "requessts"])
def test_typosquatted_dependencies_are_refused(name: str) -> None:
    with pytest.raises(provision.ProvisionError, match="typosquat"):
        _transitive(name)

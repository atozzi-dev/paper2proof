"""The built images (A-06), on the local Docker daemon: `make images`, then `make test-docker`.
Skipped when an image is missing. One test downloads a package from PyPI."""

import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from packaging.version import Version

pytestmark = pytest.mark.docker
TAG = "dev"
ALL = ["p2p-runner", "p2p-runner-cpu", "p2p-provisioner", "p2p-api"]
SCHEMAS = Path(__file__).resolve().parents[2] / "schemas"


def _docker(args: list[str], timeout: float = 600) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(  # noqa: S603 - the docker CLI with arguments built by these tests
        ["docker", *args],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _need(*images: str) -> None:
    for image in images:
        try:
            found = _docker(["image", "inspect", f"{image}:{TAG}"], timeout=30).returncode == 0
        except (OSError, subprocess.SubprocessError):
            found = False
        if not found:
            pytest.skip(f"{image}:{TAG} not built (make images)")


def _run(image: str, *argv: str, entrypoint: str, mounts: tuple[str, ...] = ()) -> str:
    _need(image)
    done = _docker(
        ["run", "--rm", "--network", "none", "--entrypoint", entrypoint, *mounts,
         f"{image}:{TAG}", *argv]
    )  # fmt: skip
    assert done.returncode == 0, done.stderr[-2000:]
    return done.stdout


@pytest.mark.parametrize("image", ALL)
def test_images_run_as_p2p(image: str) -> None:
    assert _run(image, "-u", entrypoint="id").strip() == "10001"


def test_the_cpu_runner_imports_its_stack() -> None:
    _run("p2p-runner-cpu", "-c", "import torch, timm, transformers, datasets", entrypoint="python")


@pytest.mark.parametrize("image", ["p2p-runner", "p2p-runner-cpu", "p2p-provisioner"])
def test_runner_constraints_are_root_owned_and_read_only(image: str) -> None:
    out = _run(image, "-c", "%U %a", "/opt/p2p/runner-constraints.txt", entrypoint="stat")
    assert out.split() == ["root", "444"]


def test_constraints_match_the_cpu_runner_s_versions() -> None:
    script = (
        "import importlib.metadata as m\n"
        "lines = open('/opt/p2p/runner-constraints.txt').read().split()\n"
        "pins = dict(line.split('==') for line in lines if '==' in line)\n"
        "for name in ('numpy', 'transformers', 'torch'):\n"
        "    assert m.version(name).split('+')[0] == pins[name], name\n"
    )
    _run("p2p-runner-cpu", "-c", script, entrypoint="python")


def test_the_entrypoint_starts_under_tini_and_rejects_a_missing_manifest() -> None:
    _need("p2p-runner-cpu")
    done = _docker(["run", "--rm", "--network", "none", f"p2p-runner-cpu:{TAG}"], timeout=300)
    assert done.returncode == 30  # contract error: no /etc/p2p/manifest.json
    assert "CONTRACT ERROR" in done.stderr


def test_real_zstd_extraction_round_trip() -> None:
    script = (
        "import hashlib, pathlib, subprocess, sys\n"
        "sys.path.insert(0, '/opt/p2p')\n"
        "import entrypoint\n"
        "src = pathlib.Path('/tmp/src'); (src / 'a').mkdir(parents=True)\n"
        "(src / 'a' / 'x.txt').write_text('hello')\n"
        "subprocess.run(['tar', '-I', 'zstd -T0', '-cf', '/tmp/a.tar.zst', '-C', str(src), '.'],"
        " check=True)\n"
        "archive = pathlib.Path('/tmp/a.tar.zst')\n"
        "digest = hashlib.sha256(archive.read_bytes()).hexdigest()\n"
        "entrypoint.tar_extract(archive, pathlib.Path('/tmp/out'), digest)\n"
        "assert (pathlib.Path('/tmp/out') / 'a' / 'x.txt').read_text() == 'hello'\n"
        "try:\n"
        "    entrypoint.tar_extract(archive, pathlib.Path('/tmp/bad'), '0' * 64)\n"
        "except entrypoint.ContractError:\n"
        "    print('rejected')\n"
    )
    assert _run("p2p-runner-cpu", "-c", script, entrypoint="python").strip() == "rejected"


def test_the_provisioner_packs_deterministically() -> None:
    # provision.py's _pack flags with zstd -T0 (one worker per core): multithreaded zstd
    # output must not depend on the worker count, so hosts with different cores agree
    script = (
        "set -e; mkdir -p /tmp/t/d; echo x > /tmp/t/f\n"
        "head -c 64000000 /dev/urandom > /tmp/t/d/blob\n"
        'for t in 2 4 0; do tar -I "zstd -T$t" --sort=name --mtime=@0 --owner=0 --group=0'
        " --numeric-owner -cf /tmp/$t.tar.zst -C /tmp/t .; done\n"
        "sha256sum /tmp/2.tar.zst /tmp/4.tar.zst /tmp/0.tar.zst | cut -d' ' -f1 | uniq | wc -l\n"
    )
    assert _run("p2p-provisioner", "-c", script, entrypoint="sh").strip() == "1"


def test_the_provisioner_has_safe_pyarrow_git_and_uv() -> None:
    out = _run(
        "p2p-provisioner",
        "-c",
        "import pyarrow, huggingface_hub, shutil; "
        "print(pyarrow.__version__, bool(shutil.which('git')), bool(shutil.which('uv')))",
        entrypoint="python",
    ).split()
    assert Version(out[0]) >= Version("14.0.1") and out[1:] == ["True", "True"]


def test_the_api_image_has_the_app_the_ui_and_litestream() -> None:
    out = _run(
        "p2p-api",
        "-c",
        "import app.api.main, pathlib, shutil; "
        "print(pathlib.Path('web/dist/index.html').is_file(), bool(shutil.which('litestream')))",
        entrypoint="python",
    ).split()
    assert out == ["True", "True"]


def test_a_provisioned_overlay_imports_in_the_cpu_runner(tmp_path: Path) -> None:
    """End to end on real images: the provisioner locks and packs `tabulate` (from PyPI), the
    CPU runner extracts the overlay against its digest and imports it."""
    _need("p2p-provisioner", "p2p-runner-cpu")
    run, cache = tmp_path / "run", tmp_path / "cache"
    for d in (run / "staging", cache):
        d.mkdir(parents=True)
    manifest = {
        "schema": "p2p.provision/v1", "run_id": "r_01jb7z00000000000000000000",
        "packages": ["tabulate", "torchmetrics"], "repo": None, "datasets": [],
        "smoke_subset": None,
        "hf_token_secret": None, "cache_salt": "test", "policy": {"denylist": [], "popular": []},
        "trusted_overlays": {}, "deadline_unix": 4102444800.0,
    }  # fmt: skip
    (tmp_path / "provision.json").write_text(json.dumps(manifest))
    done = _docker(
        ["run", "--rm", "--user", "0:0",  # host bind mounts are root-owned on Docker Desktop
         "-v", f"{run}:/mnt/run", "-v", f"{cache}:/mnt/cache",
         "-v", f"{tmp_path / 'provision.json'}:/etc/p2p/provision.json:ro",
         f"p2p-provisioner:{TAG}"],
        timeout=900,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr[-3000:]
    report = json.loads((run / "staging" / "provision_report.json").read_text())
    overlay = run / "staging" / "overlay.tar.zst"
    assert hashlib.sha256(overlay.read_bytes()).hexdigest() == report["overlay_sha256"]
    script = (
        "import pathlib, subprocess, sys\n"
        "sys.path.insert(0, '/opt/p2p')\n"
        "import entrypoint\n"
        "entrypoint.tar_extract(pathlib.Path('/mnt/run/staging/overlay.tar.zst'),"
        f" pathlib.Path('/tmp/overlay'), {report['overlay_sha256']!r})\n"
        "assert not [p for p in pathlib.Path('/tmp/overlay').iterdir()"
        " if p.name.startswith(('torch', 'nvidia', 'cuda', 'triton'))"
        " and not p.name.startswith('torchmetrics')], 'the runner provides torch'\n"
        "subprocess.run([sys.executable, '-c', 'import tabulate, torchmetrics'], check=True,"
        " env={'PYTHONPATH': '/tmp/overlay'})\n"
    )
    _run("p2p-runner-cpu", "-c", script, entrypoint="python", mounts=("-v", f"{run}:/mnt/run:ro"))


@pytest.mark.parametrize("image", ["p2p-runner", "p2p-runner-cpu", "p2p-provisioner"])
def test_the_image_entrypoint_is_tini(image: str) -> None:
    _need(image)
    done = _docker(
        ["image", "inspect", "--format", "{{json .Config.Entrypoint}}", f"{image}:{TAG}"]
    )
    entrypoint = json.loads(done.stdout)
    assert entrypoint[:4] == ["/usr/bin/tini", "--", "/opt/venv/bin/python", "-I"]


@pytest.mark.parametrize("image", ["p2p-runner", "p2p-runner-cpu", "p2p-provisioner"])
def test_the_environment_is_read_only_and_work_is_writable(image: str) -> None:
    script = (
        "find /opt /usr /bin /sbin /lib /etc -xdev -writable -print 2>/dev/null | head -5; "
        "find / -xdev -perm /6000 -type f -print 2>/dev/null | head -5; "
        "touch /work/x && echo work-ok"
    )
    assert _run(image, "-c", script, entrypoint="sh").split() == ["work-ok"]


CANARY_SCRIPT = """
import argparse, json, os
import canary_overlay  # from the overlay archive: proves it was extracted onto PYTHONPATH
p = argparse.ArgumentParser()
p.add_argument("--seed", type=int, required=True)
p.add_argument("--out", required=True)
p.add_argument("--steps", type=int, default=1)
a = p.parse_args()
os.makedirs(a.out, exist_ok=True)
value = canary_overlay.BASE + a.seed / 100 + a.steps / 1000
with open(os.path.join(a.out, "metrics.json"), "w") as f:
    json.dump({"top1_accuracy": value}, f)
"""


def _tar(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, text in sorted(files.items()):
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o644
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_the_cpu_runner_runs_a_canary_workspace_to_a_valid_results_json(tmp_path: Path) -> None:
    """A-23 done-when: the real EXEC entrypoint, as uid 10001 with no network, extracts a
    workspace and a zstd overlay against their manifest digests, runs every seed and writes
    a results.json that the control plane's schema accepts."""
    _need("p2p-runner-cpu")
    run = tmp_path / "run"
    run.mkdir()
    run.chmod(0o777)
    workspace = _tar({"train.py": CANARY_SCRIPT})
    (run / "workspace.tar").write_bytes(workspace)
    (run / "overlay.tar").write_bytes(_tar({"canary_overlay.py": "BASE = 0.5\n"}))
    packed = _docker(
        ["run", "--rm", "--network", "none", "--entrypoint", "zstd", "-v", f"{run}:/mnt/run",
         f"p2p-runner-cpu:{TAG}", "-q", "--rm", "/mnt/run/overlay.tar"]
    )  # fmt: skip
    assert packed.returncode == 0, packed.stderr[-2000:]
    overlay = hashlib.sha256((run / "overlay.tar.zst").read_bytes()).hexdigest()
    manifest = {
        "schema": "p2p.manifest/v1", "run_id": "r_01jb7z00000000000000000000", "attempt": 1,
        "image_digest": "sha256:" + "0" * 64, "entrypoint": "workspace/train.py",
        "args": ["--steps", "3"], "seeds": [0, 1, 2], "budget_seconds": 120,
        "paths": {"overlay": "/mnt/run/overlay.tar.zst", "repo": None,
                  "data_dir": "/mnt/run/data", "workspace": "/mnt/run/workspace.tar",
                  "cache": "/mnt/cache", "results": "/mnt/run/results"},
        "claim_ref": "c1", "metric": "top1_accuracy", "higher_is_better": True,
        "claim_type": "ABSOLUTE", "baseline_name": None,
        "scale": {"full": False, "fraction_of_data": 0.1, "epochs": 1, "paper_epochs": 90},
        "archives": {"overlay": overlay, "repo": None, "data": []},
        "workspace_sha256": hashlib.sha256(workspace).hexdigest(),
    }  # fmt: skip
    manifest_schema = json.loads((SCHEMAS / "manifest.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator(manifest_schema).validate(manifest)
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    done = _docker(
        ["run", "--rm", "--network", "none", "-v", f"{run}:/mnt/run",
         "-v", f"{tmp_path / 'manifest.json'}:/etc/p2p/manifest.json:ro",
         f"p2p-runner-cpu:{TAG}"],
        timeout=300,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr[-3000:]
    results = json.loads((run / "results" / "results.json").read_text(encoding="utf-8"))
    results_schema = json.loads((SCHEMAS / "results.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator(results_schema).validate(results)
    assert results["exit_reason"] == "completed"
    assert (results["run_id"], results["attempt"], results["gpu"]) == (manifest["run_id"], 1, "cpu")
    assert results["per_seed"] == [
        {"seed": s, "value": pytest.approx(0.5 + s / 100 + 0.003), "ok": True} for s in (0, 1, 2)
    ]

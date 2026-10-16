"""Image build inputs (A-06), checked without Docker: locks, runner constraints, Dockerfiles."""

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[2]
IMAGES = ROOT / "images"
LOCKS = {
    "runner": IMAGES / "runner" / "requirements.lock",
    "runner-cpu": IMAGES / "runner-cpu" / "requirements.lock",
    "provisioner": IMAGES / "provisioner" / "requirements.lock",
}
DOCKERFILES = sorted(IMAGES.glob("*/Dockerfile"))
_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)")


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "runner_constraints", ROOT / "scripts" / "runner_constraints.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["runner_constraints"] = module
    spec.loader.exec_module(module)
    return module


constraints = _load()


def _pins(lock: Path) -> dict[str, str]:
    pins = {}
    for line in lock.read_text(encoding="utf-8").splitlines():
        match = _PIN.match(line)
        if match:
            pins[match.group(1).lower().replace("_", "-")] = match.group(2)
    return pins


@pytest.mark.parametrize("name", sorted(LOCKS))
def test_every_locked_package_is_pinned_with_hashes(name: str) -> None:
    text = LOCKS[name].read_text(encoding="utf-8")
    entries = re.split(
        r"\n(?=[A-Za-z0-9])", text.split("\n", 1)[1] if text.startswith("#") else text
    )
    packages = [e for e in entries if _PIN.match(e)]
    assert packages, f"{name}: empty lock"
    for entry in packages:
        assert "--hash=sha256:" in entry, f"{name}: {entry.splitlines()[0]} has no hash"
    assert not re.search(r"^(-e |git\+|https?://|file:)", text, re.M), "no URLs or editables"


def test_the_provisioner_pins_safe_pyarrow_and_huggingface_hub() -> None:
    pins = _pins(LOCKS["provisioner"])
    assert Version(pins["pyarrow"]) >= Version("14.0.1")  # CVE-2023-47248
    assert "huggingface-hub" in pins


def test_the_runner_has_the_promised_stack_on_both_images() -> None:
    wanted = {
        "torch", "torchvision", "numpy", "scipy", "scikit-learn", "transformers", "datasets",
        "timm", "accelerate", "evaluate", "pandas", "pyarrow",
    }  # fmt: skip
    gpu, cpu = _pins(LOCKS["runner"]), _pins(LOCKS["runner-cpu"])
    assert wanted <= set(gpu) and wanted <= set(cpu)
    assert Version(gpu["pyarrow"]) >= Version("14.0.1")
    for name in wanted:  # same public versions: an overlay locked for one runs on the other
        assert Version(gpu[name]).public == Version(cpu[name]).public, name
    assert cpu["torch"].endswith("+cpu") and gpu["torch"].endswith("+cu126")
    assert not [n for n in gpu if n.startswith("nvidia-") and "13" in gpu[n].split(".")[0]]


def test_runner_constraints_are_public_versions_of_the_lock() -> None:
    lock = LOCKS["runner"].read_text(encoding="utf-8")
    text = constraints.constraints(lock)
    lines = [line for line in text.splitlines() if line and not line.startswith("#")]
    assert lines == sorted(lines)
    assert all(re.fullmatch(r"[a-z0-9][a-z0-9._-]*==[0-9][0-9A-Za-z.!]*", line) for line in lines)
    assert {line.split("==")[0] for line in lines} == set(_pins(LOCKS["runner"]))


def test_local_version_labels_are_dropped() -> None:
    lock = "torch==2.6.0+cpu \\\n    --hash=sha256:" + "a" * 64 + "\nNumPy==2.1.3\n"
    assert constraints.constraints(lock).splitlines()[1:] == ["numpy==2.1.3", "torch==2.6.0"]


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda p: p.parent.name)
def test_dockerfiles_pin_bases_by_digest_and_run_as_p2p(dockerfile: Path) -> None:
    text = dockerfile.read_text(encoding="utf-8")
    bases = re.findall(r"^FROM\s+(\S+)", text, re.M)
    assert bases
    stages = set(re.findall(r"^FROM\s+\S+\s+AS\s+(\S+)", text, re.M | re.I))
    for base in bases:
        assert base in stages or re.search(r"@sha256:[0-9a-f]{64}$", base), base
    assert re.search(r"^USER\s+10001(:10001)?\s*$", text, re.M), "final stage runs as uid 10001"
    assert "PYTHONDONTWRITEBYTECODE=1" in text
    assert ":latest" not in text


@pytest.mark.parametrize("image", ["runner", "runner-cpu"])
def test_runners_start_isolated_under_tini(image: str) -> None:
    text = (IMAGES / image / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(
        r'^ENTRYPOINT \["/usr/bin/tini", "--", "/opt/venv/bin/python", "-I", '
        r'"/opt/p2p/entrypoint.py"\]$',
        text,
        re.M,
    )
    assert "runner-constraints.txt" in text and "0444" in text


@pytest.mark.parametrize("name", sorted(LOCKS))
def test_only_pytorch_s_own_wheels_come_from_its_index(name: str) -> None:
    allowed = re.compile(r"^(torch|torchvision|triton|nvidia-.*|cuda-.*)$")
    current = None
    for line in LOCKS[name].read_text(encoding="utf-8").splitlines():
        match = _PIN.match(line)
        if match:
            current = match.group(1).lower()
        elif "# from https://download.pytorch.org" in line:
            assert current is not None and allowed.match(current), f"{name}: {current}"


@pytest.mark.parametrize("name", sorted(LOCKS))
def test_locks_carry_no_local_paths(name: str) -> None:
    text = LOCKS[name].read_text(encoding="utf-8")
    assert not re.search(r"(?<![A-Za-z])[A-Za-z]:[/\\](?!/)|/tmp/|AppData|/home/", text)


def test_the_api_image_runs_in_prod_mode_with_a_writable_database() -> None:
    text = (IMAGES / "api" / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"P2P_ENV=prod", text)  # the default (test) skips the prod checks
    assert "DB_URL=sqlite+aiosqlite:////var/lib/p2p/p2p.db" in text
    assert "npm ci --ignore-scripts" in text


def test_images_are_built_for_amd64() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    builds = [line for line in makefile.splitlines() if "$(DOCKER) build" in line]
    assert builds and all("--platform linux/amd64" in line for line in builds)

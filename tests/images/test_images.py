"""Image build inputs (A-06), checked without Docker: locks, runner constraints, Dockerfiles."""

import importlib.util
import re
import sys
import tomllib
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


@pytest.mark.parametrize(
    ("image", "script"),
    [("runner", "entrypoint.py"), ("runner-cpu", "entrypoint.py"), ("provisioner", "provision.py")],
)
def test_jobs_start_isolated_under_tini(image: str, script: str) -> None:
    text = (IMAGES / image / "Dockerfile").read_text(encoding="utf-8")
    entrypoint = f'["/usr/bin/tini", "--", "/opt/venv/bin/python", "-I", "/opt/p2p/{script}"]'
    assert f"\nENTRYPOINT {entrypoint}\n" in text
    assert "runner-constraints.txt" in text and "0444" in text


TORCH_URLS = {
    "runner": "https://download.pytorch.org/whl/cu126",
    "runner-cpu": "https://download.pytorch.org/whl/cpu",
}


def _sources(lock: Path) -> dict[str, str]:
    """package -> the index it was locked from (`--emit-index-annotation`)."""
    sources, current = {}, None
    for line in lock.read_text(encoding="utf-8").splitlines():
        match = _PIN.match(line)
        if match:
            current = match.group(1).lower()
        elif line.strip().startswith("# from ") and current is not None:
            sources[current] = line.strip().removeprefix("# from ")
    return sources


@pytest.mark.parametrize("name", sorted(LOCKS))
def test_only_torch_and_torchvision_come_from_pytorch_s_index(name: str) -> None:
    sources = _sources(LOCKS[name])
    assert set(sources) == set(_pins(LOCKS[name])), "every pin records its index"
    expected = {
        pkg: TORCH_URLS[name] if pkg in ("torch", "torchvision") else "https://pypi.org/simple"
        for pkg in sources
    } if name in TORCH_URLS else {pkg: "https://pypi.org/simple" for pkg in sources}  # fmt: skip
    assert sources == expected


def _stack(image: str) -> dict:  # type: ignore[type-arg]
    return tomllib.loads((IMAGES / image / "pyproject.toml").read_text(encoding="utf-8"))


def test_both_runners_list_the_same_packages() -> None:
    assert (
        _stack("runner")["project"]["dependencies"]
        == _stack("runner-cpu")["project"]["dependencies"]
    )


def test_the_provisioner_resolves_torch_from_the_runner_s_index() -> None:
    spec = importlib.util.spec_from_file_location(
        "provision_for_images", IMAGES / "provisioner" / "provision.py"
    )
    assert spec and spec.loader
    provision = importlib.util.module_from_spec(spec)
    sys.modules["provision_for_images"] = provision
    spec.loader.exec_module(provision)
    stack = _stack("runner")
    [index] = stack["tool"]["uv"]["index"]
    assert index["url"] == provision.TORCH_INDEX
    assert set(provision.TORCH_FAMILY) == set(stack["tool"]["uv"]["sources"])


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


def _load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


split_lock = _load_script("split_lock")


@pytest.mark.parametrize("name", ["runner", "runner-cpu"])
def test_the_lock_splits_into_one_install_per_index(name: str) -> None:
    lock = LOCKS[name].read_text(encoding="utf-8")
    parts = split_lock.split(lock)
    assert set(parts) == {"https://pypi.org/simple", TORCH_URLS[name]}
    torch = parts[TORCH_URLS[name]]
    assert re.findall(r"^(\S+)==", torch, re.M) == ["torch", "torchvision"]
    pins = sum(len(re.findall(r"^\S+==", text, re.M)) for text in parts.values())
    assert pins == len(_pins(LOCKS[name]))
    assert all("--hash=sha256:" in text for text in parts.values())


def test_a_lock_entry_without_its_index_is_rejected() -> None:
    with pytest.raises(ValueError, match="annotation"):
        split_lock.split("numpy==2.5.4 \\\n    --hash=sha256:" + "a" * 64 + "\n")


@pytest.mark.parametrize("image", ["runner", "runner-cpu"])
def test_runner_builds_install_each_part_from_its_own_index(image: str) -> None:
    text = (IMAGES / image / "Dockerfile").read_text(encoding="utf-8")
    assert "unsafe-best-match" not in text and "--extra-index-url" not in text
    assert "split_lock.py" in text


def test_dockerignore_keeps_out_every_secret_pattern_of_gitignore() -> None:
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").split()
    for pattern in ("*.pem", "*.key", "*credentials*.json", ".env*", "*.tfvars", "*.tfstate*"):
        assert f"**/{pattern}" in ignore, pattern
    assert ignore.index("**/*.pem") > max(i for i, p in enumerate(ignore) if p.startswith("!"))


@pytest.mark.parametrize("image", ["runner", "runner-cpu", "provisioner"])
def test_job_images_drop_setuid_bits(image: str) -> None:
    text = (IMAGES / image / "Dockerfile").read_text(encoding="utf-8")
    assert "-perm /6000 -type f -exec chmod a-s {} +" in text


def test_the_api_build_never_builds_sdists() -> None:
    assert "UV_NO_BUILD=1" in (IMAGES / "api" / "Dockerfile").read_text(encoding="utf-8")

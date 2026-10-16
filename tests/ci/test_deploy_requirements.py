"""The deploy job installs only the locked nebius SDK closure (.github/deploy-requirements.txt)."""

from typing import Any

import pytest

from tests.ci import load_script

req = load_script(".github/scripts/deploy_requirements.py")


def test_requirements_file_matches_uv_lock() -> None:
    assert req.main(["--check"]) == 0, "regenerate .github/deploy-requirements.txt"


def _lock(*packages: dict[str, Any]) -> dict[str, Any]:
    return {"package": list(packages)}


def _pkg(name: str, deps: tuple[str, ...] = (), wheels: tuple[str, ...] = ("sha256:aa",)):
    return {
        "name": name,
        "version": "1.0",
        "dependencies": [{"name": d} for d in deps],
        "wheels": [{"url": f"https://x/{name}.whl", "hash": h} for h in wheels],
    }


def test_closure_follows_dependencies_only_from_nebius() -> None:
    lock = _lock(_pkg("nebius", ("grpcio",)), _pkg("grpcio", ("six",)), _pkg("six"), _pkg("other"))
    assert [p["name"] for p in req.closure(lock)] == ["grpcio", "nebius", "six"]


def test_render_pins_versions_and_wheel_hashes() -> None:
    text = req.render(_lock(_pkg("nebius", wheels=("sha256:bb", "sha256:aa"))))
    assert text.endswith("nebius==1.0 \\\n    --hash=sha256:aa \\\n    --hash=sha256:bb\n")


def test_environment_markers_are_kept() -> None:
    nebius = _pkg("nebius", ("winonly", "both"))
    nebius["dependencies"][0]["marker"] = "sys_platform == 'win32'"
    other = _pkg("other", ("both",))
    lock = _lock(nebius, _pkg("winonly"), _pkg("both"), other)
    text = req.render(lock)
    assert "winonly==1.0 ; (sys_platform == 'win32') \\\n" in text
    assert "both==1.0 \\\n" in text


def test_a_package_without_wheels_is_refused() -> None:
    with pytest.raises(ValueError, match="no wheel"):
        req.render(_lock(_pkg("nebius", wheels=())))

"""`make dev` on Windows with local docker jobs: uvicorn's reloader forces the selector event
loop there, which cannot start a subprocess, so every docker job failed with
NotImplementedError (2026-11-02). The recipe drops --reload in that case only."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed")


def _dev_commands(tmp_path: Path, **env: str) -> str:
    """The commands `make dev` runs, printed by UV=echo, in a folder with no .env."""
    environ = {k: v for k, v in os.environ.items() if k not in ("OS", "P2P_JOB_BACKEND")}
    result = subprocess.run(  # noqa: S603 - make with a fixed argument list
        ["make", "-s", "-f", str(ROOT / "Makefile"), "dev", "UV=echo"],  # noqa: S607
        cwd=tmp_path,
        env={**environ, **env},
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return result.stdout


def test_dev_reloads_by_default(tmp_path: Path) -> None:
    out = _dev_commands(tmp_path)
    assert "run uvicorn app.main:app --reload --reload-dir app --port 8000" in out


def test_dev_keeps_the_reload_with_docker_jobs_elsewhere(tmp_path: Path) -> None:
    assert "--reload" in _dev_commands(tmp_path, P2P_JOB_BACKEND="docker")


def test_dev_drops_the_reload_for_docker_jobs_on_windows(tmp_path: Path) -> None:
    out = _dev_commands(tmp_path, OS="Windows_NT", P2P_JOB_BACKEND="docker")
    (uvicorn,) = [line for line in out.splitlines() if "uvicorn" in line]
    assert uvicorn.startswith("run uvicorn app.main:app --port 8000") and "--reload" not in uvicorn
    assert "no --reload" in out  # said, not silent


def test_dev_reloads_on_windows_without_docker_jobs(tmp_path: Path) -> None:
    assert "--reload" in _dev_commands(tmp_path, OS="Windows_NT")

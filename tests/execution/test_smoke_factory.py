"""Smoke executor selection: Sandboxes with the job fallback, jobs only, or local (dev)."""

from pathlib import Path

import pytest
from pydantic import SecretStr

from app.domain.models import StepName
from app.execution.config import ExecutionConfig
from app.execution.job_smoke import JobSmokeExecutor
from app.execution.local_trusted import LocalTrustedExecutor, LocalTrustedRefused
from app.execution.run_storage import FakeRunStorage
from app.execution.smoke import FallbackSmokeExecutor
from app.execution.smoke_factory import SmokeOptions, build_smoke_executor, load_sandbox_image
from app.models.pricing import load_cloud_prices
from app.storage.store import Store
from tests.steps.conftest import ROOT, ctx_at

DIGEST = "cr.eu-north1.nebius.cloud/reg/{}@sha256:" + "c" * 64
CFG = ExecutionConfig(
    provisioner_image=DIGEST.format("p2p-provisioner"),
    runner_image=DIGEST.format("p2p-runner"),
    runner_cpu_image=DIGEST.format("p2p-runner-cpu"),
    subnet_provision="vpcsubnet-provision",
    subnet_exec="vpcsubnet-exec",
)


def _options(tmp_path: Path, kind: str, env: str = "test", key: str | None = "k") -> SmokeOptions:
    images = tmp_path / "sandbox_images.yaml"
    images.write_text(f'images:\n  "sha256:{"c" * 64}": img-0001\n')
    return SmokeOptions(
        kind=kind,
        env=env,
        api_key=SecretStr(key) if key else None,
        project_id="project-e00test",
        sandbox_images=images,
        canary_allowlist=ROOT / "config" / "canary_allowlist.yaml",
        work_root=tmp_path / "local",
    )


async def _build(store: Store, tmp_path: Path, options: SmokeOptions) -> object:
    ctx = await ctx_at(store, StepName.SMOKE_TESTING, previous=[(StepName.PROVISIONING, "p")])
    return build_smoke_executor(
        options,
        ctx,
        storage=FakeRunStorage(),
        config=CFG,
        cloud=load_cloud_prices(ROOT / "config" / "pricing.yaml"),
    )


async def test_sandboxes_fall_back_to_jobs(store: Store, tmp_path: Path) -> None:
    smoke = await _build(store, tmp_path, _options(tmp_path, "sandboxes"))
    assert isinstance(smoke, FallbackSmokeExecutor)
    assert isinstance(smoke._executors[1], JobSmokeExecutor)


async def test_without_a_key_sandboxes_are_skipped(store: Store, tmp_path: Path) -> None:
    smoke = await _build(store, tmp_path, _options(tmp_path, "sandboxes", key=None))
    assert isinstance(smoke, JobSmokeExecutor)


async def test_jobs_only(store: Store, tmp_path: Path) -> None:
    assert isinstance(await _build(store, tmp_path, _options(tmp_path, "job")), JobSmokeExecutor)


async def test_local_only_in_dev(store: Store, tmp_path: Path) -> None:
    with pytest.raises(LocalTrustedRefused):
        await _build(store, tmp_path, _options(tmp_path, "local", env="prod"))


async def test_local_in_dev(store: Store, tmp_path: Path) -> None:
    smoke = await _build(store, tmp_path, _options(tmp_path, "local", env="dev"))
    assert isinstance(smoke, LocalTrustedExecutor)


def test_sandbox_images_are_keyed_by_runner_cpu_digest(tmp_path: Path) -> None:
    images = _options(tmp_path, "job").sandbox_images
    assert load_sandbox_image(images, CFG.runner_cpu_image) == "img-0001"
    assert load_sandbox_image(images, DIGEST.format("x").replace("c" * 64, "d" * 64)) is None
    assert load_sandbox_image(ROOT / "config" / "sandbox_images.yaml", CFG.runner_cpu_image) is None

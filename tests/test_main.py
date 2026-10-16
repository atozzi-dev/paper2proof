"""The composition root (app/main.py, A-14): every step wired, fail-fast in prod, models
resolved at startup, interrupted runs resumed."""

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import respx
from alembic import command
from pydantic import SecretStr

from app import main
from app.domain.models import StepName
from app.models.client import Role
from app.models.fake import FakeModelClient
from app.settings import Settings
from tests.conftest import ROOT, alembic_config

LLM = ROOT / "tests" / "fixtures" / "llm"
MODELS = json.loads((LLM / "models_list.json").read_text(encoding="utf-8"))
SECRET_A, SECRET_B = "a" * 40, "b" * 40


def _settings(tmp_path: Path, **values: object) -> Settings:
    db = tmp_path / "p2p.db"
    command.upgrade(alembic_config(db), "head")
    fields: dict[str, object] = {
        "p2p_env": "dev",
        "db_url": f"sqlite+aiosqlite:///{db.as_posix()}",
        "p2p_var_dir": tmp_path / "var",
    }
    fields.update(values)
    return Settings(**fields)  # type: ignore[arg-type]


@pytest.fixture
async def runtime(tmp_path: Path) -> AsyncIterator[main.Runtime]:
    built = main.build_runtime(_settings(tmp_path), models=FakeModelClient(LLM))
    yield built
    await built.close()


async def test_every_fsm_step_is_wired(runtime: main.Runtime) -> None:
    assert set(runtime.steps) == set(StepName)


async def test_the_app_serves_health_and_reports_readiness(tmp_path: Path) -> None:
    runtime = main.build_runtime(_settings(tmp_path))  # dev without an API key
    app = main.create(runtime)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/healthz")).status_code == 200
            ready = await client.get("/readyz")
    assert ready.status_code == 503
    assert ready.json()["checks"] == {"db": True, "models": False, "replication": True}


@respx.mock
async def test_models_are_resolved_at_startup(tmp_path: Path) -> None:
    respx.get("https://api.tokenfactory.nebius.com/v1/models").mock(
        return_value=httpx.Response(200, json=MODELS)
    )
    runtime = main.build_runtime(_settings(tmp_path, nebius_api_key=SecretStr("k" * 20)))
    app = main.create(runtime)
    async with app.router.lifespan_context(app):
        assert runtime.state.models_ready
        assert set(runtime.models.resolved) == set(Role)  # type: ignore[attr-defined]


@respx.mock
async def test_a_routing_failure_stops_a_prod_start(tmp_path: Path) -> None:
    respx.get("https://api.tokenfactory.nebius.com/v1/models").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    runtime = main.build_runtime(
        _settings(tmp_path, nebius_api_key=SecretStr("k" * 20)), adapters=main.dev_adapters
    )
    runtime.settings = runtime.settings.model_copy(update={"p2p_env": "prod"})
    app = main.create(runtime)
    with pytest.raises(main.RouterError):
        async with app.router.lifespan_context(app):
            pass


async def test_interrupted_runs_resume_at_startup(tmp_path: Path) -> None:
    runtime = main.build_runtime(_settings(tmp_path), models=FakeModelClient(LLM))
    await runtime.store.create_run(
        "r_01jb7z00000000000000000000", "2106.09685", created_by=None, claim_hint=None, budget={}
    )
    await runtime.store.begin_run("r_01jb7z00000000000000000000", StepName.INGESTING, 1)
    resumed: list[str] = []

    async def submit(run_id: str) -> None:
        resumed.append(run_id)

    runtime.orchestrator.submit = submit  # type: ignore[method-assign,assignment]
    app = main.create(runtime)
    async with app.router.lifespan_context(app):
        pass
    assert resumed == ["r_01jb7z00000000000000000000"]


def test_prod_needs_the_nebius_adapters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEBIUS_API_KEY", "k" * 20)
    monkeypatch.setenv("SESSION_KEY", SECRET_A)
    monkeypatch.setenv("ADMIN_TOKEN", SECRET_B)
    settings = _settings(tmp_path, p2p_env="prod")
    with pytest.raises(main.ConfigError, match="A-20"):
        main.build_runtime(settings)


@pytest.mark.parametrize("missing", ["p2p_runner_image", "p2p_subnet_exec", "p2p_gpu_preemptible"])
def test_prod_execution_settings_are_required(tmp_path: Path, missing: str) -> None:
    values: dict[str, object] = {
        "p2p_provisioner_image": "cr.example/p2p-provisioner@sha256:" + "a" * 64,
        "p2p_runner_image": "cr.example/p2p-runner@sha256:" + "b" * 64,
        "p2p_runner_cpu_image": "cr.example/p2p-runner-cpu@sha256:" + "c" * 64,
        "p2p_subnet_provision": "vpcsubnet-provision",
        "p2p_subnet_exec": "vpcsubnet-exec",
        "p2p_gpu_preemptible": False,
    }
    del values[missing]
    with pytest.raises(main.ConfigError, match=missing.upper()):
        main.execution_config(_settings(tmp_path, **values), prod=True)


def test_dev_runs_without_execution_settings(tmp_path: Path) -> None:
    config = main.execution_config(_settings(tmp_path), prod=False)
    assert config.runner_image.endswith("@sha256:" + "0" * 64)
    assert config.preemptible_gpu is True


@pytest.mark.parametrize(
    ("env", "replication", "ready"),
    [("prod", "litestream", True), ("prod", None, False), ("dev", None, True)],
)
def test_replication_is_ready_only_under_litestream_in_prod(
    env: str, replication: str | None, ready: bool
) -> None:
    environ = {"P2P_REPLICATION": replication} if replication else {}
    assert main.replication_ready(env, environ, parent=lambda: "litestream") is ready


def test_known_secrets_are_the_configured_values(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path, nebius_api_key=SecretStr("n" * 20), tavily_api_key=SecretStr("t" * 20)
    )
    secrets = main.known_secrets(settings)
    assert "n" * 20 in secrets and "t" * 20 in secrets
    assert settings.session_key.get_secret_value() in secrets
    assert "" not in secrets


@pytest.mark.parametrize(("parent", "ready"), [("litestream", True), ("sh", False), (None, False)])
def test_prod_replication_needs_litestream_as_the_parent_process(
    parent: str | None, ready: bool
) -> None:
    environ = {"P2P_REPLICATION": "litestream"}
    assert main.replication_ready("prod", environ, parent=lambda: parent) is ready


def test_readyz_reports_the_build_commit(tmp_path: Path) -> None:
    runtime = main.build_runtime(_settings(tmp_path, p2p_build_commit="abc123"))
    app = main.create(runtime)

    async def check() -> dict[str, object]:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
                body: dict[str, object] = (await client.get("/readyz")).json()
                return body

    import asyncio

    assert asyncio.run(check())["commit"] == "abc123"


def _prod(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **values: object) -> Settings:
    monkeypatch.setenv("NEBIUS_API_KEY", "k" * 20)
    monkeypatch.setenv("SESSION_KEY", SECRET_A)
    monkeypatch.setenv("ADMIN_TOKEN", SECRET_B)
    execution: dict[str, object] = {
        "p2p_provisioner_image": "cr.example/p2p-provisioner@sha256:" + "a" * 64,
        "p2p_runner_image": "cr.example/p2p-runner@sha256:" + "b" * 64,
        "p2p_runner_cpu_image": "cr.example/p2p-runner-cpu@sha256:" + "c" * 64,
        "p2p_subnet_provision": "vpcsubnet-provision",
        "p2p_subnet_exec": "vpcsubnet-exec",
        "p2p_gpu_preemptible": False,
        "p2p_bundle_key_id": "kms-key",
    }
    return _settings(tmp_path, p2p_env="prod", **{**execution, **values})


def test_prod_refuses_unsigned_bundles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _prod(tmp_path, monkeypatch, p2p_bundle_key_id=None)
    with pytest.raises(main.ConfigError, match="P2P_BUNDLE_KEY_ID"):
        main.build_runtime(settings, adapters=main.dev_adapters)


def test_prod_refuses_the_local_smoke_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _prod(tmp_path, monkeypatch, smoke_executor="local")
    with pytest.raises(main.ConfigError, match="SMOKE_EXECUTOR"):
        main.build_runtime(settings, adapters=main.dev_adapters)


async def test_a_failed_start_closes_the_runtime(tmp_path: Path) -> None:
    runtime = main.build_runtime(_settings(tmp_path), models=FakeModelClient(LLM))
    closed: list[bool] = []
    original = runtime.close

    async def close() -> None:
        closed.append(True)
        await original()

    async def broken() -> list[object]:
        raise RuntimeError("resume failed")

    runtime.close = close  # type: ignore[method-assign]
    runtime.orchestrator.resume_all = broken  # type: ignore[method-assign,assignment]
    app = main.create(runtime)
    with pytest.raises(RuntimeError, match="resume failed"):
        async with app.router.lifespan_context(app):
            pass
    assert closed == [True]

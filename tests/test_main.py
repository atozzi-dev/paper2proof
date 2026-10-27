"""The composition root (app/main.py, A-14): every step wired, fail-fast in prod, models
resolved at startup, interrupted runs resumed."""

import asyncio
import dataclasses
import json
import multiprocessing
import os
import stat
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx
from alembic import command
from pydantic import SecretStr

from app import main
from app.bundle.sign import FakeSigner
from app.domain.models import StepName
from app.execution.dev_docker import DevDockerBackend, LocalRunStorage
from app.execution.fake_backend import FakeJobBackend
from app.execution.hybrid import HybridJobBackend
from app.execution.local_trusted import LocalTrustedRefused
from app.execution.run_storage import FakeRunStorage
from app.models.client import Role
from app.models.fake import FakeModelClient
from app.settings import Settings
from app.storage.artifacts import LocalArtifactStore
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


def test_prod_needs_the_nebius_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEBIUS_API_KEY", "k" * 20)
    monkeypatch.setenv("SESSION_KEY", SECRET_A)
    monkeypatch.setenv("ADMIN_TOKEN", SECRET_B)
    settings = _settings(tmp_path, p2p_env="prod")
    with pytest.raises(main.ConfigError) as err:
        main.build_runtime(settings)
    for name in ("S3_ENDPOINT", "P2P_CONTROL_BUCKET", "JOB_LAUNCHER_S3_SECRET", "RUNTIME_AUTH_KEY"):
        assert name in str(err.value)


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


PROD_STORAGE: dict[str, object] = {
    "s3_endpoint": "https://storage.eu-north1.nebius.cloud",
    "p2p_control_bucket": "p2p-bundles",
    "nebius_sandbox_project_id": "project-sandbox",
    "runtime_s3_key_id": SecretStr("runtime-key-id"),
    "runtime_s3_secret": SecretStr("runtime-secret"),
    "job_launcher_s3_key_id": SecretStr("launcher-key-id"),
    "job_launcher_s3_secret": SecretStr("launcher-secret"),
    "job_launcher_auth_key": SecretStr('{"launcher": "authorized key"}'),
    "runtime_auth_key": SecretStr('{"runtime": "authorized key"}'),
    "gateway_instance_id": "computeinstance-gw",
}


def test_prod_adapters_use_each_identity_for_its_bucket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, dict[str, object]] = {}

    def execution(**kwargs: object) -> object:
        seen["execution"] = kwargs
        key_file = Path(str(kwargs["auth_key_file"]))
        seen["launcher_key"] = {"text": key_file.read_text(), "mode": key_file.stat().st_mode}
        return SimpleNamespace(backend=FakeJobBackend(), storage=FakeRunStorage())

    def artifacts(**kwargs: object) -> object:
        seen["artifacts"] = kwargs
        return LocalArtifactStore(tmp_path / "a")

    def signer(key_file: Path, key_id: str) -> object:
        seen["signer"] = {"text": key_file.read_text(), "key_id": key_id}
        return FakeSigner()

    monkeypatch.setattr(main, "build_nebius_execution", execution)
    monkeypatch.setattr(main.S3ArtifactStore, "connect", artifacts)
    monkeypatch.setattr(main, "kms_signer", signer)
    settings = _prod(tmp_path, monkeypatch, **PROD_STORAGE)
    adapters = main.prod_adapters(settings)

    assert adapters.reap and adapters.signer is not None
    launcher = seen["execution"]
    assert (
        launcher["project_id"] == "project-sandbox" and launcher["s3_key_id"] == "launcher-key-id"
    )
    assert launcher["gateway_instance_id"] == "computeinstance-gw"
    assert seen["launcher_key"]["text"] == '{"launcher": "authorized key"}'
    if os.name != "nt":  # POSIX modes (the container); Windows has none
        assert stat.S_IMODE(int(seen["launcher_key"]["mode"])) & 0o077 == 0  # owner only
    control = seen["artifacts"]
    assert control["bucket"] == "p2p-bundles" and control["key_id"] == "runtime-key-id"
    assert seen["signer"] == {"text": '{"runtime": "authorized key"}', "key_id": "kms-key"}


def test_the_reaper_runs_only_with_real_adapters(tmp_path: Path) -> None:
    started: list[str] = []

    class Reaper:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def run_forever(self) -> None:
            started.append("run")
            try:
                await asyncio.sleep(3600)
            finally:
                started.append("stopped")

    def adapters(settings: Settings) -> main.Adapters:
        return dataclasses.replace(main.dev_adapters(settings), reap=True)

    async def check() -> None:
        original = main.Reaper
        main.Reaper = Reaper  # type: ignore[misc,assignment]
        try:
            runtime = main.build_runtime(_settings(tmp_path), adapters=adapters)
            app = main.create(runtime)
            async with app.router.lifespan_context(app):
                await asyncio.sleep(0)
                assert started == ["run"]
        finally:
            main.Reaper = original  # type: ignore[misc]

    asyncio.run(check())
    assert started == ["run", "stopped"]


def test_dev_has_no_reaper(runtime: main.Runtime) -> None:
    assert runtime.reaper is None


def test_the_api_reads_verdict_conditions_from_the_run_artifacts(runtime: main.Runtime) -> None:
    assert runtime.state.artifacts is runtime.adapters.artifacts


def test_an_existing_key_file_or_link_is_replaced_owner_only(tmp_path: Path) -> None:
    target = tmp_path / "keys" / "k.json"
    target.parent.mkdir()
    target.write_text("old")
    written = main.key_file(SecretStr('{"k": 1}'), None, target)
    assert written == target and target.read_text() == '{"k": 1}'
    if os.name != "nt":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
        elsewhere = tmp_path / "elsewhere.json"
        elsewhere.write_text("x")
        target.unlink()
        target.symlink_to(elsewhere)
        main.key_file(SecretStr('{"k": 2}'), None, target)
        assert elsewhere.read_text() == "x" and not target.is_symlink()


def test_prod_refuses_key_files_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = tmp_path / "launcher.json"
    key.write_text("{}")
    settings = _prod(tmp_path, monkeypatch, **{**PROD_STORAGE, "job_launcher_auth_key_file": key})
    with pytest.raises(main.ConfigError, match="JOB_LAUNCHER_AUTH_KEY_FILE"):
        main.prod_adapters(settings)


def test_nothing_is_written_when_prod_settings_are_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = {**PROD_STORAGE, "p2p_control_bucket": None}
    settings = _prod(tmp_path, monkeypatch, **values)
    with pytest.raises(main.ConfigError, match="P2P_CONTROL_BUCKET"):
        main.prod_adapters(settings)
    assert not (settings.p2p_var_dir / "keys").exists()


def test_prod_needs_the_published_signing_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "PUBLIC_KEY", tmp_path / "missing.pem")
    settings = _prod(tmp_path, monkeypatch)
    with pytest.raises(main.ConfigError, match=r"bundle-public-key.pem"):
        main.build_runtime(settings, adapters=main.dev_adapters)


async def test_the_signing_key_is_probed_at_startup(tmp_path: Path) -> None:
    signer = FakeSigner()
    pinned = (await signer.public_key_pem()).encode()
    other = (await FakeSigner().public_key_pem()).encode()

    def adapters(settings: Settings) -> main.Adapters:
        return dataclasses.replace(main.dev_adapters(settings), signer=signer)

    for key, ok in ((pinned, True), (other, False)):
        runtime = main.build_runtime(_settings(tmp_path / str(ok)), adapters=adapters)
        runtime.pinned_key = key
        app = main.create(runtime)
        if ok:
            async with app.router.lifespan_context(app):
                pass
        else:
            with pytest.raises(main.ConfigError, match="published key"):
                async with app.router.lifespan_context(app):
                    pass


SECRET_FIELDS = sorted(
    name.upper()
    for name, field in Settings.model_fields.items()
    if "SecretStr" in str(field.annotation)
)


def test_every_secret_leaves_the_environment_once_settings_are_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CPU-pool workers parse untrusted papers: they inherit no secret at all, neither the
    authorized keys nor the API keys, the S3 secrets, the session key or the admin token."""
    assert {"NEBIUS_API_KEY", "ADMIN_TOKEN", "RUNTIME_S3_SECRET", "JOB_LAUNCHER_AUTH_KEY"} <= set(
        SECRET_FIELDS
    )
    for name in SECRET_FIELDS:
        monkeypatch.setenv(name, "s" * 40)
    monkeypatch.setenv("session_key", "s" * 40)  # settings read names case-insensitively
    monkeypatch.setenv("LITESTREAM_SECRET_ACCESS_KEY", "l" * 40)  # exported by start.sh
    monkeypatch.setenv("LITESTREAM_ACCESS_KEY_ID", "l" * 20)
    monkeypatch.setenv("S3_ENDPOINT", "https://storage.example")
    settings = Settings()
    main.drop_secret_env()
    left = {name.upper() for name in os.environ}
    assert not left & {*SECRET_FIELDS, "LITESTREAM_SECRET_ACCESS_KEY", "LITESTREAM_ACCESS_KEY_ID"}
    assert os.environ["S3_ENDPOINT"] == "https://storage.example"  # settings stay
    assert settings.admin_token.get_secret_value() == "s" * 40  # already read


def test_authorized_keys_private_part_is_redacted(tmp_path: Path) -> None:
    private = "-----BEGIN " + "PRIVATE KEY-----\nAbC\n-----END"  # no key-like literal
    key = json.dumps({"id": "k", "private-key": private})
    settings = _settings(tmp_path, runtime_auth_key=SecretStr(key))
    secrets = main.known_secrets(settings)
    assert key in secrets and private in secrets


def test_the_cpu_pool_never_forks_a_threaded_process() -> None:
    method = main.pool_start_method()
    assert method in multiprocessing.get_all_start_methods()
    assert method != "fork"  # aiosqlite and asyncio threads are already running


async def test_dev_runs_real_jobs_in_local_docker_when_asked(tmp_path: Path) -> None:
    built = main.dev_adapters(_settings(tmp_path, p2p_job_backend="docker"))
    assert isinstance(built.jobs, DevDockerBackend)
    assert isinstance(built.storage, LocalRunStorage)
    assert isinstance(main.dev_adapters(_settings(tmp_path)).jobs, FakeJobBackend)


def test_the_local_docker_backend_is_never_used_in_prod(tmp_path: Path) -> None:
    settings = _settings(tmp_path, p2p_job_backend="docker")
    with pytest.raises(main.ConfigError, match="P2P_JOB_BACKEND"):
        main.build_runtime(settings.model_copy(update={"p2p_env": "prod"}))


def test_the_local_docker_backend_refuses_the_test_environment(tmp_path: Path) -> None:
    settings = _settings(tmp_path, p2p_job_backend="docker", p2p_env="test")
    with pytest.raises(LocalTrustedRefused):
        main.dev_adapters(settings)


async def test_canary_papers_are_ingested_from_the_repository(runtime: main.Runtime) -> None:
    """A-25 / eval set A / H-09: 0000.000NN IDs are served from eval/canaries, not arXiv."""
    from eval.canaries import build

    step = runtime.steps[StepName.INGESTING]
    canary = build.discover()[0]
    assert await step._papers.source(canary.truth.arxiv_id) == build.source_bytes(canary)  # type: ignore[attr-defined]


# --- hybrid deploy (docs/specs/hybrid-deploy.md) -------------------------------------------

HYBRID: dict[str, object] = {
    **PROD_STORAGE,
    "gateway_instance_id": None,
    "p2p_execution": "hybrid",
    "smoke_executor": "sandboxes",
    "p2p_local_jobs_network": "p2p-local-jobs",
}


def _nebius(seen: dict[str, object]) -> object:
    def execution(**kwargs: object) -> object:
        seen.update(kwargs)
        remote = SimpleNamespace(backend=FakeJobBackend(), storage=FakeRunStorage(), s3=object())
        seen["remote"] = remote
        return remote

    return execution


def test_hybrid_runs_provisioning_here_and_jobs_on_nebius(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(main, "build_nebius_execution", _nebius(seen))
    monkeypatch.setattr(main.S3ArtifactStore, "connect", lambda **_: LocalArtifactStore(tmp_path))
    monkeypatch.setattr(main, "kms_signer", lambda *_: FakeSigner())
    settings = _prod(tmp_path, monkeypatch, **HYBRID, p2p_local_jobs_dir=tmp_path / "jobs")
    adapters = main.prod_adapters(settings)
    assert isinstance(adapters.jobs, HybridJobBackend)
    remote = seen["remote"]
    assert adapters.storage is remote.storage  # type: ignore[attr-defined]  # what jobs mount
    assert seen["gateway_instance_id"] is None
    assert adapters.reap


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"gateway_instance_id": "computeinstance-gw"}, "GATEWAY_INSTANCE_ID"),
        ({"p2p_local_jobs_dir": None}, "P2P_LOCAL_JOBS_DIR"),
        ({"smoke_executor": "job"}, "SMOKE_EXECUTOR"),
        ({"p2p_local_jobs_network": None}, "P2P_LOCAL_JOBS_NETWORK"),
    ],
)
def test_hybrid_refuses_a_gateway_no_local_dir_or_cpu_smoke_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: dict[str, object], error: str
) -> None:
    monkeypatch.setattr(main, "build_nebius_execution", _nebius({}))
    values = {**HYBRID, "p2p_local_jobs_dir": tmp_path / "jobs", **change}
    settings = _prod(tmp_path, monkeypatch, **values)
    with pytest.raises(main.ConfigError, match=error):
        main.prod_adapters(settings)


def test_hybrid_is_for_prod_only(tmp_path: Path) -> None:
    settings = _settings(tmp_path, p2p_execution="hybrid")
    with pytest.raises(main.ConfigError, match="P2P_EXECUTION=hybrid"):
        main.build_runtime(settings, adapters=main.dev_adapters)


def test_hybrid_smoke_jobs_run_on_the_gpu_platform_and_need_no_provision_subnet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _prod(tmp_path, monkeypatch, **HYBRID, p2p_subnet_provision=None)
    config = main.execution_config(settings, prod=True)
    assert (config.smoke_platform, config.smoke_preset) == ("gpu-l40s-a", "1gpu-8vcpu-32gb")
    assert config.subnet_exec == "vpcsubnet-exec"
    with pytest.raises(main.ConfigError, match="P2P_SUBNET_PROVISION"):
        main.execution_config(_prod(tmp_path, monkeypatch, p2p_subnet_provision=None), prod=True)

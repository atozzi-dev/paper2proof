"""Composition root (A-14): builds every service from Settings and serves the API.

`uvicorn app.main:app` (images/api/start.sh) builds the runtime at import (settings fail fast)
and does the slow part in the lifespan: model routing against GET /v1/models (fail-fast in
prod, invariant 5), then resuming every run that was interrupted by a restart.

Adapters (job backend, run storage, artifact store) differ by environment: dev and test use
local and in-memory ones; prod needs the Nebius job backend and S3 storage (A-20) and refuses
to start without them.
"""

import asyncio
import json
import logging
import multiprocessing
import os
import stat
from collections.abc import AsyncIterator, Callable, Mapping
from concurrent.futures import ProcessPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from pydantic import SecretStr

from app import observability
from app.api.deps import AppState
from app.api.main import create_app
from app.bundle.build import Bundler
from app.bundle.sign import NebiusKmsSigner, Signer, sign_manifest
from app.bundle.store import ArtifactBundleStore, BundleStore
from app.discovery.tavily import TavilyClient
from app.domain.errors import P2PError
from app.domain.models import ErrorCode, PackageReq, StepName
from app.execution.backend import JobBackend
from app.execution.config import ExecutionConfig
from app.execution.dev_docker import DevDockerBackend, LocalRunStorage
from app.execution.fake_backend import FakeJobBackend
from app.execution.local_trusted import load_canaries
from app.execution.nebius_backend import build_nebius_execution
from app.execution.package_policy import (
    PackagePolicy,
    PolicyContext,
    PyPIClient,
    load_policy_config,
)
from app.execution.run_storage import FakeRunStorage, RunStorage
from app.execution.s3 import S3ArtifactStore
from app.execution.smoke_factory import SmokeOptions, build_smoke_executor
from app.ingestion.arxiv import ArxivClient
from app.models.client import (
    Completion,
    ModelClient,
    StoreCallRecorder,
    TokenFactoryClient,
    load_models_config,
)
from app.models.pricing import Pricing, load_cloud_prices
from app.models.router import RouterError, fetch_models, resolve_roles
from app.orchestrator.budget import Budget, BudgetLimits, load_budget_limits
from app.orchestrator.fsm import Orchestrator, Step
from app.orchestrator.reaper import Reaper, reaper_rates
from app.orchestrator.steps.analyzing import AnalyzingStep
from app.orchestrator.steps.awaiting_approval import AwaitingApprovalStep
from app.orchestrator.steps.discovering import DiscoveringStep
from app.orchestrator.steps.executing import ExecutingStep
from app.orchestrator.steps.extracting_claims import ExtractingClaimsStep
from app.orchestrator.steps.ingesting import IngestingStep
from app.orchestrator.steps.planning import PlanningStep
from app.orchestrator.steps.provisioning import ProvisioningStep
from app.orchestrator.steps.smoke_testing import SmokeTestingStep
from app.orchestrator.steps.verdict import VerdictStep
from app.settings import Settings, get_settings
from app.storage.artifacts import ArtifactStore, LocalArtifactStore
from app.storage.db import make_engine
from app.storage.store import Store

__all__ = ["ConfigError", "RouterError", "Runtime", "build_runtime", "create"]
log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDER = "local/{}@sha256:" + "0" * 64  # dev/test only: no job ever runs these
PUBLIC_KEY = ROOT / "docs" / "bundle-public-key.pem"


class ConfigError(RuntimeError):
    """The environment can't run this configuration: the app refuses to start."""


@dataclass
class Adapters:
    jobs: JobBackend
    storage: RunStorage
    artifacts: ArtifactStore
    bundles: BundleStore
    signer: Signer | None = None  # None: bundles are left unsigned (BUNDLE_UNSIGNED)
    reap: bool = False  # real jobs and buckets: the reaper runs


def dev_adapters(settings: Settings) -> Adapters:
    """Local files and in-memory fakes: nothing leaves the machine and no job runs. With
    P2P_JOB_BACKEND=docker the real job images run in local docker, for canary papers only."""
    artifacts = LocalArtifactStore(settings.p2p_var_dir / "artifacts")
    bundles = ArtifactBundleStore(artifacts)
    if settings.p2p_job_backend != "docker":
        return Adapters(FakeJobBackend(), FakeRunStorage(), artifacts, bundles)
    store = Store(make_engine(settings.db_url))

    async def paper_sha256(run_id: str) -> str | None:
        run = await store.get_run(run_id)
        return run.paper_sha256 if run else None

    root = settings.p2p_var_dir / "jobs"
    jobs = DevDockerBackend(
        env=settings.p2p_env,
        canaries=load_canaries(settings.config_dir / "canary_allowlist.yaml"),
        paper_sha256=paper_sha256,
        root=root,
    )
    return Adapters(jobs, LocalRunStorage(root), artifacts, bundles)


def key_file(content: SecretStr | None, path: Path | None, target: Path) -> Path | None:
    """An SDK authorized key as a file: the configured file (dev), or the secret's content
    written owner-only to the container's ephemeral disk, never through a planted link."""
    if path is not None:
        return path
    if content is None or not content.get_secret_value():
        return None
    folder = target.parent
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = folder.lstat()
    if not stat.S_ISDIR(info.st_mode) or (hasattr(os, "geteuid") and info.st_uid != os.geteuid()):
        raise ConfigError(f"{folder} must be a directory owned by this process")
    folder.chmod(0o700)
    temporary = folder / f".{target.name}.{os.getpid()}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content.get_secret_value())
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(target)  # replaces a planted link, never writes through it
    finally:
        temporary.unlink(missing_ok=True)
    return target


KEY_ENV = ("JOB_LAUNCHER_AUTH_KEY", "RUNTIME_AUTH_KEY")


def drop_key_env() -> None:
    """The authorized keys leave the environment once Settings has read them: child
    processes (the parsing pool, litestream) never inherit a signing identity."""
    for name in KEY_ENV:
        os.environ.pop(name, None)


def kms_signer(key: Path, key_id: str) -> Signer:  # pragma: no cover - needs the Nebius SDK
    from nebius.sdk import SDK

    return NebiusKmsSigner.from_sdk(
        SDK(credentials_file_name=key, user_agent_prefix="paper2proof"), key_id
    )


def _secret(value: SecretStr | None) -> str:
    return value.get_secret_value() if value is not None else ""


def prod_adapters(settings: Settings) -> Adapters:
    """Nebius jobs and S3 storage. Each identity reaches only its own buckets: sa-job-launcher
    the per-run buckets, sa-api-runtime the control bucket and the KMS signing key."""
    files = {
        "JOB_LAUNCHER_AUTH_KEY_FILE": settings.job_launcher_auth_key_file,
        "RUNTIME_AUTH_KEY_FILE": settings.runtime_auth_key_file,
    }
    refused = [name for name, value in files.items() if value is not None]
    if refused:  # prod keys come from the secret store, never from a file of unknown mode
        raise ConfigError(f"P2P_ENV=prod takes the keys as secrets, not {', '.join(refused)}")
    required = {
        "S3_ENDPOINT": settings.s3_endpoint,
        "P2P_CONTROL_BUCKET": settings.p2p_control_bucket,
        "NEBIUS_SANDBOX_PROJECT_ID": settings.nebius_sandbox_project_id,
        "RUNTIME_S3_KEY_ID": _secret(settings.runtime_s3_key_id),
        "RUNTIME_S3_SECRET": _secret(settings.runtime_s3_secret),
        "JOB_LAUNCHER_S3_KEY_ID": _secret(settings.job_launcher_s3_key_id),
        "JOB_LAUNCHER_S3_SECRET": _secret(settings.job_launcher_s3_secret),
        "JOB_LAUNCHER_AUTH_KEY": _secret(settings.job_launcher_auth_key),
        "RUNTIME_AUTH_KEY": _secret(settings.runtime_auth_key),
        "P2P_BUNDLE_KEY_ID": settings.p2p_bundle_key_id,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ConfigError(f"P2P_ENV=prod needs {', '.join(missing)}")
    keys = settings.p2p_var_dir / "keys"  # written only once everything else is in place
    launcher_key = key_file(settings.job_launcher_auth_key, None, keys / "launcher.json")
    runtime_key = key_file(settings.runtime_auth_key, None, keys / "runtime.json")
    assert settings.s3_endpoint and settings.p2p_control_bucket  # noqa: S101 (checked above)
    assert launcher_key and runtime_key and settings.p2p_bundle_key_id  # noqa: S101
    assert settings.job_launcher_s3_secret and settings.runtime_s3_secret  # noqa: S101
    assert settings.nebius_sandbox_project_id  # noqa: S101
    execution = build_nebius_execution(
        project_id=settings.nebius_sandbox_project_id,
        s3_endpoint=settings.s3_endpoint,
        region=settings.s3_region,
        s3_key_id=_secret(settings.job_launcher_s3_key_id),
        s3_secret=settings.job_launcher_s3_secret,
        auth_key_file=launcher_key,
        gateway_instance_id=settings.gateway_instance_id,
    )
    artifacts = S3ArtifactStore.connect(
        endpoint_url=settings.s3_endpoint,
        region=settings.s3_region,
        bucket=settings.p2p_control_bucket,
        key_id=_secret(settings.runtime_s3_key_id),
        secret=settings.runtime_s3_secret,
    )
    return Adapters(
        execution.backend,
        execution.storage,
        artifacts,
        ArtifactBundleStore(artifacts),
        signer=kms_signer(runtime_key, settings.p2p_bundle_key_id),
        reap=True,
    )


class UnavailableModels:
    """Stands in until the models are routed (dev without an API key): every call fails with
    MODEL_UNAVAILABLE, so a run fails clearly instead of hanging."""

    async def complete(self, *args: object, **kwargs: object) -> Completion:
        raise P2PError(ErrorCode.MODEL_UNAVAILABLE, "models are not routed (no NEBIUS_API_KEY?)")

    async def structured(self, *args: object, **kwargs: object) -> Any:
        raise P2PError(ErrorCode.MODEL_UNAVAILABLE, "models are not routed (no NEBIUS_API_KEY?)")


class _LateModels:
    """The steps' model client: the real one replaces the stand-in once the lifespan has
    routed the models (steps are built before that)."""

    def __init__(self, client: ModelClient) -> None:
        self.client = client

    async def complete(self, *args: Any, **kwargs: Any) -> Completion:
        return await self.client.complete(*args, **kwargs)

    async def structured(self, *args: Any, **kwargs: Any) -> Any:
        return await self.client.structured(*args, **kwargs)


def execution_config(settings: Settings, *, prod: bool) -> ExecutionConfig:
    """Images and subnets of the execution plane: required in prod, placeholders elsewhere."""
    values = {  # ExecutionConfig field -> (environment variable, value)
        "provisioner_image": ("P2P_PROVISIONER_IMAGE", settings.p2p_provisioner_image),
        "runner_image": ("P2P_RUNNER_IMAGE", settings.p2p_runner_image),
        "runner_cpu_image": ("P2P_RUNNER_CPU_IMAGE", settings.p2p_runner_cpu_image),
        "subnet_provision": ("P2P_SUBNET_PROVISION", settings.p2p_subnet_provision),
        "subnet_exec": ("P2P_SUBNET_EXEC", settings.p2p_subnet_exec),
        "preemptible_gpu": ("P2P_GPU_PREEMPTIBLE", settings.p2p_gpu_preemptible),
    }
    missing = [env for env, value in values.values() if value is None]
    if prod and missing:
        raise ConfigError(f"P2P_ENV=prod needs {', '.join(missing)}")
    defaults: dict[str, object] = {
        "provisioner_image": PLACEHOLDER.format("p2p-provisioner"),
        "runner_image": PLACEHOLDER.format("p2p-runner"),
        "runner_cpu_image": PLACEHOLDER.format("p2p-runner-cpu"),
        "subnet_provision": "local-provision",
        "subnet_exec": "local-exec",
        "preemptible_gpu": True,
    }
    return ExecutionConfig.model_validate(
        {k: defaults[k] if v is None else v for k, (_, v) in values.items()}
    )


def _parent_name() -> str | None:
    """The parent process's name (Linux): start.sh execs uvicorn as litestream's child."""
    try:
        return Path(f"/proc/{os.getppid()}/comm").read_text(encoding="utf-8").strip()
    except OSError:
        return None


def replication_ready(
    env: str, environ: Mapping[str, str], *, parent: Callable[[], str | None] = _parent_name
) -> bool:
    """In prod the database is only safe under `litestream replicate`: start.sh says so and
    the parent process must really be litestream (an environment variable alone could be set
    by anyone writing the endpoint's env)."""
    if env != "prod":
        return True
    return environ.get("P2P_REPLICATION") == "litestream" and parent() == "litestream"


def known_secrets(settings: Settings) -> list[str]:
    """Every configured secret value: the Proof Bundle redacts them (invariant 4)."""
    values = [
        settings.nebius_api_key,
        settings.tavily_api_key,
        settings.runtime_s3_key_id,
        settings.runtime_s3_secret,
        settings.job_launcher_s3_key_id,
        settings.job_launcher_s3_secret,
        settings.job_launcher_auth_key,
        settings.runtime_auth_key,
        settings.session_key,
        settings.admin_token,
        settings.observability_static_key,
    ]
    found = [v.get_secret_value() for v in values if v is not None and v.get_secret_value()]
    for key in (settings.job_launcher_auth_key, settings.runtime_auth_key):
        if key is not None:  # the private key alone is redacted too, not only the whole JSON
            found += _private_parts(key.get_secret_value())
    return found


def _private_parts(document: str) -> list[str]:
    try:
        data: Any = json.loads(document)
    except ValueError:
        return []
    parts: list[str] = []
    stack = [data]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for name, value in item.items():
                if isinstance(value, str) and "private" in str(name).lower() and value:
                    parts.append(value)
                else:
                    stack.append(value)
        elif isinstance(item, list):
            stack.extend(item)
    return parts


@dataclass
class Runtime:
    settings: Settings
    store: Store
    budget: Budget
    limits: BudgetLimits
    adapters: Adapters
    models: ModelClient
    steps: Mapping[StepName, Step]
    orchestrator: Orchestrator
    state: AppState
    http: httpx.AsyncClient
    cpu_pool: ProcessPoolExecutor | None
    late: _LateModels
    reaper: Reaper | None = None
    pinned_key: bytes | None = None  # the published signing key (docs/bundle-public-key.pem)
    _reaper_task: "asyncio.Task[None] | None" = None

    async def probe_signing(self) -> None:
        """Sign once at startup and check it under the published key: a wrong key ID, a
        missing permission or a bad authorized key stops the start, not every later bundle."""
        signer = self.adapters.signer
        if signer is None or self.pinned_key is None:
            return
        try:
            await sign_manifest(signer, b"paper2proof startup probe", self.pinned_key)
        except Exception as err:
            raise ConfigError(
                f"bundle signing does not verify under the published key: {type(err).__name__}"
            ) from None

    def start_reaper(self) -> None:
        """One reaper per deployment (one uvicorn worker), only with real jobs and buckets."""
        if self.reaper is not None and self._reaper_task is None:
            self._reaper_task = asyncio.create_task(self.reaper.run_forever(), name="reaper")

    async def route_models(self) -> None:
        """Resolve every role against GET /v1/models (prod: any failure stops the start)."""
        key = self.settings.nebius_api_key
        if key is None or not key.get_secret_value():
            log.warning("no NEBIUS_API_KEY: model calls fail with MODEL_UNAVAILABLE")
            return
        config = load_models_config(self.settings.config_dir / "models.yaml")
        try:
            listed = await fetch_models(self.http, config.provider.base_url, key)
            resolved = resolve_roles(listed, config)
        except (RouterError, httpx.HTTPError):
            if self.settings.p2p_env == "prod":
                raise
            log.exception("model routing failed: model calls fail with MODEL_UNAVAILABLE")
            return
        self.late.client = TokenFactoryClient(
            config,
            resolved,
            Pricing.load(self.settings.config_dir / "pricing.yaml"),
            self.budget,
            StoreCallRecorder(self.store),
            api_key=key,
            http_client=self.http,
        )
        self.models = self.late.client
        self.state.models_ready = True

    async def close(self) -> None:
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            await asyncio.gather(self._reaper_task, return_exceptions=True)
        await self.orchestrator.shutdown()
        await self.http.aclose()
        if self.cpu_pool is not None:
            self.cpu_pool.shutdown(wait=False, cancel_futures=True)
        await self.store.engine.dispose()


def build_runtime(
    settings: Settings,
    *,
    adapters: Callable[[Settings], Adapters] | None = None,
    models: ModelClient | None = None,
    cpu_pool: ProcessPoolExecutor | None = None,
) -> Runtime:
    prod = settings.p2p_env == "prod"
    if prod and settings.p2p_job_backend != "fake":
        raise ConfigError("P2P_JOB_BACKEND=docker is for dev canaries only, never in prod")
    built = (adapters or (prod_adapters if prod else dev_adapters))(settings)
    if prod and settings.smoke_executor == "local":
        raise ConfigError("SMOKE_EXECUTOR=local is for dev canaries only, never in prod")
    if prod and not settings.p2p_bundle_key_id:
        raise ConfigError("P2P_ENV=prod needs P2P_BUNDLE_KEY_ID: Proof Bundles are signed")
    if prod and not PUBLIC_KEY.is_file():
        raise ConfigError(
            "P2P_ENV=prod needs docs/bundle-public-key.pem in the image: bundles are checked "
            "against the published key (export it with app.bundle.sign public-key)"
        )
    config_dir = settings.config_dir
    store = Store(make_engine(settings.db_url))
    limits = load_budget_limits(config_dir / "budgets.yaml")
    budget = Budget(store, limits, env=settings.p2p_env)
    cloud = load_cloud_prices(config_dir / "pricing.yaml")
    execution = execution_config(settings, prod=prod)
    http = httpx.AsyncClient(timeout=30, follow_redirects=False)
    late = _LateModels(models or UnavailableModels())
    lm: ModelClient = late  # delegates the ModelClient protocol
    artifacts, storage = built.artifacts, built.storage
    tavily = TavilyClient(settings.tavily_api_key, http)
    policy = PackagePolicy(load_policy_config(config_dir), PyPIClient(store, http))

    async def package_check(packages: list[PackageReq]) -> list[PackageReq]:
        return (await policy.validate(packages, PolicyContext())).accepted

    smoke_options = SmokeOptions(
        kind=settings.smoke_executor,
        env=settings.p2p_env,
        api_key=settings.nebius_api_key,
        project_id=settings.tf_project_id,
        sandbox_images=config_dir / "sandbox_images.yaml",
        canary_allowlist=config_dir / "canary_allowlist.yaml",
        work_root=settings.p2p_var_dir / "smoke",
    )
    public_key = PUBLIC_KEY.read_bytes() if PUBLIC_KEY.is_file() else None
    bundler = Bundler(
        artifacts=artifacts,
        storage=storage,
        bundles=built.bundles,
        signer=built.signer,
        runner_image=execution.runner_image,
        known_secrets=known_secrets(settings),
        public_key_pem=public_key,
        commit=settings.p2p_build_commit,
    )
    steps: dict[StepName, Step] = {
        StepName.INGESTING: IngestingStep(ArxivClient(http), artifacts),
        StepName.EXTRACTING_CLAIMS: ExtractingClaimsStep(lm, artifacts),
        StepName.DISCOVERING: DiscoveringStep(
            store=store,
            arxiv=ArxivClient(http),
            tavily=tavily,
            models=lm,
            artifacts=artifacts,
            http=http,
        ),
        StepName.PLANNING: PlanningStep(
            lm,
            artifacts,
            cloud,
            preemptible=execution.preemptible_gpu,
            package_check=package_check,
        ),
        StepName.AWAITING_APPROVAL: AwaitingApprovalStep(
            timeout=timedelta(minutes=limits.approval.approval_timeout_min)
        ),
        StepName.PROVISIONING: ProvisioningStep(
            policy=policy, storage=storage, artifacts=artifacts, config=execution, cloud=cloud
        ),
        StepName.SMOKE_TESTING: SmokeTestingStep(
            lm,
            artifacts,
            storage,
            lambda ctx, archives: build_smoke_executor(
                smoke_options,
                ctx,
                archives=archives,
                storage=storage,
                config=execution,
                cloud=cloud,
            ),
            tavily=tavily,
        ),
        StepName.EXECUTING: ExecutingStep(
            artifacts=artifacts, storage=storage, config=execution, cloud=cloud
        ),
        StepName.ANALYZING: AnalyzingStep(artifacts, storage, lm),
        StepName.VERDICT: VerdictStep(
            lm, artifacts, config_path=config_dir / "verdict.yaml", bundler=bundler
        ),
    }
    orchestrator = Orchestrator(
        store, budget, steps, limits=limits, jobs=built.jobs, cpu_pool=cpu_pool
    )
    state = AppState(
        settings=settings,
        store=store,
        budget=budget,
        limits=limits,
        submit=orchestrator.submit,
        bundles=built.bundles,
        replication_ready=replication_ready(settings.p2p_env, os.environ),
        web_dist=ROOT / "web" / "dist",
    )
    return Runtime(
        settings, store, budget, limits, built, lm, steps, orchestrator, state, http, cpu_pool,
        late,
        Reaper(store, built.jobs, built.bundles, budget, rates=reaper_rates(cloud))
        if built.reap
        else None,
        public_key,
    )  # fmt: skip


def create(runtime: Runtime) -> FastAPI:
    """The FastAPI app over a runtime; its lifespan routes models and resumes runs."""
    api = create_app(runtime.state)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            await runtime.route_models()
            await runtime.probe_signing()
            resumed = await runtime.orchestrator.resume_all()
            runtime.start_reaper()
        except BaseException:
            await runtime.close()  # runs already resumed stop; nothing serves half-started
            raise
        log.info("started", extra={"resumed_runs": len(resumed)})
        try:
            yield
        finally:
            runtime.state.draining = True
            await runtime.close()

    api.router.lifespan_context = lifespan
    return api


def pool_start_method() -> str:
    """Never fork a process that already runs threads (aiosqlite, asyncio): forkserver on
    Linux (the endpoint) and macOS, spawn where forkserver doesn't exist (Windows)."""
    methods = multiprocessing.get_all_start_methods()
    return "forkserver" if "forkserver" in methods else "spawn"


@lru_cache(maxsize=1)
def _app() -> FastAPI:  # pragma: no cover - the uvicorn entrypoint (start.sh)
    observability.configure_early()  # JSON and key-shaped redaction even if settings fail
    settings = get_settings()
    observability.configure(settings, known_secrets(settings))
    drop_key_env()
    pool = ProcessPoolExecutor(
        max_workers=2,
        mp_context=multiprocessing.get_context(pool_start_method()),
        initializer=observability.worker_logging,
    )
    return create(build_runtime(settings, cpu_pool=pool))


def __getattr__(name: str) -> FastAPI:  # pragma: no cover - lazy: importing app.main is free
    if name == "app":
        return _app()
    raise AttributeError(name)

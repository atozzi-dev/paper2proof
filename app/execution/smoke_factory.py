"""Which smoke executor a run gets (`P2P_SMOKE_EXECUTOR`):

- `sandboxes`: Token Factory Sandboxes, falling back to CPU smoke jobs after 3 consecutive
  Sandboxes errors (or straight to jobs without a key or an imported image);
- `job`: CPU smoke jobs only (the default until spike S-3 confirms the Sandboxes flow);
- `local`: LocalTrustedExecutor, dev and canary papers only (it refuses anything else).
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import SecretStr

from app.execution.config import ExecutionConfig
from app.execution.job_smoke import SMOKE_PLATFORM, SMOKE_PRESET, JobSmokeExecutor
from app.execution.local_trusted import LocalTrustedExecutor, load_canaries
from app.execution.run_storage import RunStorage
from app.execution.sandboxes import SandboxesSmokeExecutor
from app.execution.smoke import FallbackSmokeExecutor, SmokeExecutor
from app.models.pricing import CloudPrices
from app.orchestrator.context import RunContext

SmokeKind = Literal["sandboxes", "job", "local"]
FALLBACK_AFTER = 3


@dataclass(frozen=True)
class SmokeOptions:
    kind: str
    env: str
    api_key: SecretStr | None
    project_id: str | None
    sandbox_images: Path
    canary_allowlist: Path
    work_root: Path


def load_sandbox_image(path: Path, runner_cpu_image: str) -> str | None:
    """Sandboxes image UUID imported for this runner-cpu digest (config/sandbox_images.yaml)."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    images = data.get("images") or {}
    digest = runner_cpu_image.rsplit("@", 1)[-1]
    value = images.get(digest) if isinstance(images, dict) else None
    return str(value) if value else None


def build_smoke_executor(
    options: SmokeOptions,
    ctx: RunContext,
    *,
    storage: RunStorage,
    config: ExecutionConfig,
    cloud: CloudPrices,
) -> SmokeExecutor:
    jobs = JobSmokeExecutor(ctx, storage=storage, config=config, cloud=cloud)
    if options.kind == "local":

        async def paper_sha256(run_id: str) -> str | None:
            run = await ctx.store.get_run(run_id)
            return run.paper_sha256 if run else None

        return LocalTrustedExecutor(
            env=options.env,
            canaries=load_canaries(options.canary_allowlist),
            paper_sha256=paper_sha256,
            image=config.runner_cpu_image,
            work_root=options.work_root,
        )
    if options.kind == "job":
        return jobs
    if options.kind != "sandboxes":
        raise ValueError(f"unknown smoke executor: {options.kind!r}")
    image = load_sandbox_image(options.sandbox_images, config.runner_cpu_image)
    key = options.api_key
    if key is None or not key.get_secret_value() or not options.project_id or image is None:
        return jobs
    sandboxes = SandboxesSmokeExecutor(
        api_key=key,
        project_id=options.project_id,
        base_image=image,
        budget=ctx.budget,
        usd_per_hour=cloud.hourly(SMOKE_PLATFORM, SMOKE_PRESET),  # proxy until S-3 gives a price
    )
    return FallbackSmokeExecutor(sandboxes, jobs, threshold=FALLBACK_AFTER)

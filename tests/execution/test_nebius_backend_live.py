"""Live check of NebiusJobBackend + S3RunStorage on real Nebius (`pytest -m live`, costs money:
a few minutes of cpu-d3 2vcpu-8gb and an empty bucket). Run only with the human's approval.

The backend never overrides an image's command (the ENTRYPOINT is the runner), so this test needs
an image whose ENTRYPOINT writes the probe file, pushed by the human to the sandbox registry:

    FROM busybox:1.37
    ENTRYPOINT ["sh", "-c", "mkdir -p /mnt/run/results/1 && echo ok > /mnt/run/results/1/ok"]

Environment (names as in .env.example): NEBIUS_SANDBOX_PROJECT_ID, NEBIUS_STORAGE_HOST,
NEBIUS_REGION, JOB_LAUNCHER_AUTH_KEY_FILE, JOB_LAUNCHER_S3_KEY_ID, JOB_LAUNCHER_S3_SECRET,
SUBNET_EXEC_ID and P2P_LIVE_ECHO_IMAGE (that image, by digest).
"""

import asyncio
import os
import time
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.domain.ids import new_run_id
from app.execution.backend import JobKind, JobSpec, JobState, VolumeMount
from app.execution.naming import bucket_name, job_name
from app.execution.nebius_backend import build_nebius_execution

pytestmark = pytest.mark.live

POLL_S = 10
DEADLINE_S = 15 * 60


def _env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        pytest.skip(f"{name} is not set")
    return value


async def test_bucket_job_and_results_round_trip() -> None:
    image = _env("P2P_LIVE_ECHO_IMAGE")
    subnet = _env("SUBNET_EXEC_ID")
    execution = build_nebius_execution(
        project_id=_env("NEBIUS_SANDBOX_PROJECT_ID"),
        s3_endpoint=_env("NEBIUS_STORAGE_HOST"),
        region=os.environ.get("NEBIUS_REGION", "eu-north1"),
        s3_key_id=_env("JOB_LAUNCHER_S3_KEY_ID"),
        s3_secret=SecretStr(_env("JOB_LAUNCHER_S3_SECRET")),
        auth_key_file=Path(_env("JOB_LAUNCHER_AUTH_KEY_FILE")),
        gateway_instance_id=None,  # the gateway is not part of this check
    )
    backend, storage = execution.backend, execution.storage
    run_id = new_run_id()
    spec = JobSpec(
        name=job_name(JobKind.SMOKE, run_id, 1),
        kind=JobKind.SMOKE,
        image_digest_ref=image,
        platform="cpu-d3",
        preset="2vcpu-8gb",
        disk_gib=30,
        subnet_id=subnet,
        volumes=[VolumeMount(source=bucket_name(run_id), mount_path="/mnt/run", mode="rw")],
        inject_files={},
        env={},
        preemptible=False,
        watchdog_s=DEADLINE_S,
    )
    handle = None
    try:
        assert await backend.ensure_run_bucket(run_id) == bucket_name(run_id)
        handle = await backend.submit(spec)
        assert await backend.submit(spec) == handle  # get-by-name: no second job
        started = time.monotonic()
        state = await backend.status(handle)
        while not state.is_terminal and time.monotonic() - started < DEADLINE_S:
            print(f"{time.monotonic() - started:6.0f}s {state}")
            await asyncio.sleep(POLL_S)
            state = await backend.status(handle)
        print(f"final state {state} after {time.monotonic() - started:.0f}s")
        assert state is JobState.SUCCEEDED
        assert await storage.get_bytes(run_id, "results/1/ok") == b"ok\n"
        listed = await backend.list_jobs(spec.name)
        assert [h for h, _, _ in listed] == [handle]
    finally:
        if handle is not None and not (await backend.status(handle)).is_terminal:
            await backend.cancel(handle)
        await backend.delete_run_bucket(run_id)
    assert bucket_name(run_id) not in {name for name, _ in await backend.list_run_buckets()}

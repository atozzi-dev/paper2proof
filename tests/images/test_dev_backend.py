"""DevDockerBackend (A-25 dev) against the local Docker daemon and a real job image: the
container starts isolated, its state comes from `docker inspect`, and a resumed submit finds it.
`make images`, then `make test-docker`."""

import asyncio
import subprocess
from pathlib import Path

import pytest

from app.execution.backend import JobKind, JobSpec, JobState, VolumeMount
from app.execution.dev_docker import DevDockerBackend

pytestmark = pytest.mark.docker
RUN = "r_01jb7z00000000000000000000"
SLUG = RUN[2:]
CANARY = "c" * 64


def _image_id(image: str) -> str:
    done = subprocess.run(  # noqa: S603 - the docker CLI with a fixed argument list
        ["docker", "image", "inspect", "--format", "{{.Id}}", image],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if done.returncode != 0:
        pytest.skip(f"{image} not built (make images)")
    return done.stdout.strip()


async def test_a_job_without_its_manifest_runs_and_fails_in_local_docker(tmp_path: Path) -> None:
    image = _image_id("p2p-runner-cpu:dev")

    async def paper_sha256(run_id: str) -> str | None:
        return CANARY if run_id == RUN else None

    backend = DevDockerBackend(
        env="dev", canaries=frozenset({CANARY}), paper_sha256=paper_sha256, root=tmp_path
    )
    name = f"p2p-smoke-{SLUG}-1"
    spec = JobSpec(
        name=name,
        kind=JobKind.SMOKE,
        image_digest_ref=f"local/p2p-runner-cpu@{image}",
        platform="cpu-d3",
        preset="4vcpu-16gb",
        disk_gib=30,
        subnet_id="local-exec",
        volumes=[VolumeMount(source=f"p2p-run-{SLUG}", mount_path="/mnt/run", mode="rw")],
        inject_files={},
        env={},
        preemptible=False,
        watchdog_s=60,
    )
    await backend.ensure_run_bucket(RUN)
    handle = await backend.submit(spec)
    try:
        for _ in range(60):
            state = await backend.status(handle)
            if state in (JobState.SUCCEEDED, JobState.FAILED):
                break
            await asyncio.sleep(1)
        assert state is JobState.FAILED  # the entrypoint refuses to start without a manifest
        assert await backend.submit(spec) == handle  # get-by-name, no second container
        [(listed, listed_state, _)] = await backend.list_jobs(f"p2p-smoke-{SLUG}-")
        assert (listed, listed_state) == (handle, JobState.FAILED)
    finally:
        await backend._cli("rm", "-f", name)

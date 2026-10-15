import pytest
from pydantic import ValidationError

from app.execution.backend import JobKind, JobSpec, JobState, VolumeMount
from app.execution.fake_backend import FakeJobBackend
from app.execution.naming import job_name

RUN = "r_01jb7z00000000000000000000"
DIGEST = "cr.eu-north1.nebius.cloud/reg/p2p-runner@sha256:" + "a" * 64


def spec(**overrides: object) -> JobSpec:
    data: dict[str, object] = {
        "name": job_name(JobKind.EXEC, RUN, 1),
        "kind": JobKind.EXEC,
        "image_digest_ref": DIGEST,
        "platform": "gpu-l40s-a",
        "preset": "1gpu-8vcpu-32gb",
        "disk_gib": 100,
        "subnet_id": "vpcsubnet-exec",
        "volumes": [VolumeMount(source="p2p-run-x", mount_path="/mnt/run", mode="rw")],
        "inject_files": {"/etc/p2p/manifest.json": b"{}"},
        "env": {"PYTHONUNBUFFERED": "1"},
        "preemptible": True,
        "watchdog_s": 2100,
    }
    data.update(overrides)
    return JobSpec.model_validate(data)


async def test_submit_is_get_by_name_first() -> None:
    backend = FakeJobBackend()
    first = await backend.submit(spec())
    second = await backend.submit(spec())
    assert first == second
    assert backend.submit_calls == 2
    assert backend.created == 1


async def test_status_and_cancel() -> None:
    backend = FakeJobBackend(final_state=JobState.SUCCEEDED)
    handle = await backend.submit(spec())
    assert await backend.status(handle) is JobState.SUCCEEDED
    await backend.cancel(handle)
    assert await backend.status(handle) is JobState.CANCELLED


async def test_run_bucket_is_idempotent() -> None:
    backend = FakeJobBackend()
    assert await backend.ensure_run_bucket(RUN) == await backend.ensure_run_bucket(RUN)
    await backend.delete_run_bucket(RUN)
    assert backend.buckets == set()


@pytest.mark.parametrize(
    "overrides",
    [
        {"image_digest_ref": "cr.eu-north1.nebius.cloud/reg/p2p-runner:latest"},
        {"name": "p2p_exec_bad"},
        {"env": {"NEBIUS_API_KEY": "x"}},
        {"env": {"HF_TOKEN": "x"}},
        {"inject_files": {"/etc/p2p/manifest.json": b"x" * (64 * 1024 + 1)}},
        {"inject_files": {"relative/path": b"{}"}},
        {"volumes": [VolumeMount(source="b", mount_path="/mnt/run", mode="rw")] * 2},
    ],
)
def test_job_spec_rejects_unsafe_specs(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        spec(**overrides)

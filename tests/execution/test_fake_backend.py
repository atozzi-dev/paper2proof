import pytest
from pydantic import ValidationError

from app.execution.backend import JobKind, JobSpec, JobState, VolumeMount
from app.execution.fake_backend import FakeJobBackend
from app.execution.naming import bucket_name, job_name

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
        "volumes": [
            VolumeMount(source=bucket_name(RUN), mount_path="/mnt/run", mode="rw"),
            VolumeMount(source="p2p-cache", mount_path="/mnt/cache", mode="ro"),
        ],
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


async def test_gateway_idle_check_reports_a_stop() -> None:
    backend = FakeJobBackend()
    await backend.ensure_gateway_up()
    assert await backend.gateway_idle_check() is True
    assert await backend.gateway_idle_check() is False


async def test_list_run_buckets_reports_creation_time() -> None:
    backend = FakeJobBackend()
    name = await backend.ensure_run_bucket(RUN)
    [(listed, created)] = await backend.list_run_buckets()
    assert listed == name
    assert created.tzinfo is not None
    await backend.ensure_run_bucket(RUN)  # get-by-name: the creation time is kept
    assert await backend.list_run_buckets() == [(name, created)]


@pytest.mark.parametrize(
    "overrides",
    [
        {"image_digest_ref": "cr.eu-north1.nebius.cloud/reg/p2p-runner:latest"},
        {"name": "p2p_exec_bad"},
        {"env": {"NEBIUS_API_KEY": "x"}},
        {"env": {"HF_TOKEN": "x"}},
        {"inject_files": {"/etc/p2p/manifest.json": b"x" * (64 * 1024 + 1)}},
        {"inject_files": {"relative/path": b"{}"}},
        {"volumes": [VolumeMount(source=bucket_name(RUN), mount_path="/mnt/run", mode="rw")] * 2},
        {"env": {"AUTH_HEADER": "x"}},
        {"env": {"DB_PASSWD": "x"}},
        {"env": {"api_key": "x"}},
        {"env": {"LD_PRELOAD": "/x.so"}},
        {"env": {"PIP_INDEX_URL": "https://evil/simple"}},
        {"env": {"HTTPS_PROXY": "http://proxy"}},
        {"env": {"P2P_DATA": "https://user:pass@host/x"}},
        {"env": {"P2P_DATA": "-----BEGIN " + "PRIVATE KEY-----"}},  # split: secret hook
        {"env": {"P2P_DATA": "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJzYS0xMjMifQ.sig"}},
        {"inject_files": {"/etc/../x": b"{}"}},
        {"inject_files": {"/var/lib/manifest.json": b"{}"}},
        {"inject_files": {"/etc/p2p/a\x00b": b"{}"}},
        {"inject_files": {f"/etc/p2p/f{i}": b"x" for i in range(9)}},
        {"volumes": [VolumeMount(source="p2p-state", mount_path="/mnt/run", mode="rw")]},
        {
            "volumes": [
                VolumeMount(
                    source="p2p-run-01jb7z00000000000000000009", mount_path="/mnt/run", mode="rw"
                )
            ]
        },
        {"volumes": [VolumeMount(source="p2p-cache", mount_path="/mnt/cache", mode="rw")]},
        {"name": job_name(JobKind.SMOKE, RUN, 1)},
    ],
)
def test_job_spec_rejects_unsafe_specs(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        spec(**overrides)


def test_env_allowlist_accepts_runner_variables() -> None:
    env = {
        "PYTHONUNBUFFERED": "1",
        "P2P_RUN_ID": RUN,
        "HF_HUB_OFFLINE": "1",
        "OMP_NUM_THREADS": "8",
    }
    assert spec(env=env).env == env


def test_provisioner_may_write_the_cache() -> None:
    provision = spec(
        name=job_name(JobKind.PROVISION, RUN, 1),
        kind=JobKind.PROVISION,
        volumes=[
            VolumeMount(source=bucket_name(RUN), mount_path="/mnt/run", mode="rw"),
            VolumeMount(source="p2p-cache", mount_path="/mnt/cache", mode="rw"),
        ],
    )
    assert provision.volumes[1].mode == "rw"

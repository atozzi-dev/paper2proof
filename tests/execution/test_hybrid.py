"""The hybrid deploy (docs/specs/hybrid-deploy.md): the control-plane host runs only the
provision and fetch jobs (our provisioner image) in local docker, for any paper; model and
paper code never runs here (invariant 1)."""

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from moto import mock_aws
from pydantic import SecretStr

from app.execution import hybrid
from app.execution.backend import JobKind, JobState, JobStatusUnavailable
from app.execution.dev_docker import DevDockerBackend
from app.execution.fake_backend import FakeJobBackend
from app.execution.hybrid import HybridJobBackend, UploadRefused, upload_run_dir
from app.execution.local_trusted import LocalTrustedRefused
from app.execution.naming import bucket_name
from app.execution.s3 import make_s3_client
from tests.execution.test_dev_docker import RUN, SLUG, Acl, Docker, _run_argv, _spec

PROVISIONER = "cr.eu-north1.nebius.cloud/e00abc/p2p-provisioner@sha256:" + "b" * 64


async def _no_paper(run_id: str) -> None:
    return None  # not a canary: these jobs do not need one


def _local(tmp_path: Path, docker: Docker, env: str = "prod") -> DevDockerBackend:
    return DevDockerBackend(
        env=env, canaries={}, paper=_no_paper, root=tmp_path, docker=docker,
        host_uid=None, acl=Acl(), provisioner_only=PROVISIONER,
    )  # fmt: skip


def _prov(kind: JobKind = JobKind.PROVISION, **changes: object):  # type: ignore[no-untyped-def]
    short = "prov" if kind is JobKind.PROVISION else kind.value
    fields: dict[str, object] = {"name": f"p2p-{short}-{SLUG}-1", "image_digest_ref": PROVISIONER}
    if kind is JobKind.FETCH:
        fields["volumes"] = [v for v in _spec().volumes if v.mount_path == "/mnt/run"]
    return _spec(kind, **{**fields, **changes})


async def test_provision_and_fetch_run_for_any_paper_by_registry_digest(tmp_path: Path) -> None:
    docker = Docker()
    backend = _local(tmp_path, docker)
    await backend.ensure_run_bucket(RUN)
    await backend.submit(_prov())
    run = _run_argv(docker)
    assert run[-1] == PROVISIONER  # pulled and run by its registry digest (invariant 8)
    assert "--network=none" not in run and "--read-only" in run and "--cap-drop=ALL" in run
    await backend.submit(_prov(JobKind.FETCH))


@pytest.mark.parametrize("kind", [JobKind.EXEC, JobKind.SMOKE])
async def test_model_code_never_runs_on_this_host(tmp_path: Path, kind: JobKind) -> None:
    docker = Docker()
    backend = _local(tmp_path, docker)
    with pytest.raises(LocalTrustedRefused, match="provision and fetch"):
        await backend.submit(_spec(kind, image_digest_ref=PROVISIONER))
    assert not [c for c in docker.calls if c[1] == "run"]


async def test_only_the_configured_provisioner_image_runs(tmp_path: Path) -> None:
    docker = Docker()
    backend = _local(tmp_path, docker)
    other = PROVISIONER.replace("b" * 64, "c" * 64)
    with pytest.raises(LocalTrustedRefused, match="provisioner image"):
        await backend.submit(_prov(image_digest_ref=other))
    assert not [c for c in docker.calls if c[1] == "run"]


def test_the_provisioner_only_backend_is_for_the_hybrid_prod_deploy(tmp_path: Path) -> None:
    for env in ("dev", "test"):
        with pytest.raises(LocalTrustedRefused, match="hybrid"):
            _local(tmp_path, Docker(), env=env)


async def test_a_resumed_provision_job_is_found_by_name(tmp_path: Path) -> None:
    docker = Docker()
    backend = _local(tmp_path, docker)
    first = await backend.submit(_prov())
    assert await backend.submit(_prov()) == first
    assert len([c for c in docker.calls if c[1] == "run"]) == 1


# --- upload of the local run copy to the Nebius run bucket --------------------------------

ENDPOINT = "https://storage.eu-north1.example.test"
POSIX = pytest.mark.skipif(os.name == "nt", reason="POSIX links and FIFOs")


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    monkeypatch.setenv("MOTO_S3_CUSTOM_ENDPOINTS", ENDPOINT)
    with mock_aws():
        client = make_s3_client(ENDPOINT, "eu-north1", "key-id", SecretStr("key-secret"))
        client.create_bucket(
            Bucket=bucket_name(RUN),
            CreateBucketConfiguration={"LocationConstraint": "eu-north1"},
        )
        yield client


def _keys(s3: Any) -> dict[str, bytes]:
    listed = s3.list_objects_v2(Bucket=bucket_name(RUN)).get("Contents", [])
    return {
        o["Key"]: s3.get_object(Bucket=bucket_name(RUN), Key=o["Key"])["Body"].read()
        for o in listed
    }


def _run_dir(tmp_path: Path) -> Path:
    root = tmp_path / bucket_name(RUN)
    (root / "staging" / "data").mkdir(parents=True)
    (root / "staging" / "overlay.tar.zst").write_bytes(b"overlay")
    (root / "staging" / "data" / "cifar.tar.zst").write_bytes(b"data")
    (root / "staging" / "provision_report.json").write_bytes(b"{}")
    return root


def test_the_local_run_copy_is_uploaded_under_the_same_keys(tmp_path: Path, s3: Any) -> None:
    sent = upload_run_dir(s3, bucket_name(RUN), _run_dir(tmp_path))
    assert _keys(s3) == {
        "staging/overlay.tar.zst": b"overlay",
        "staging/data/cifar.tar.zst": b"data",
        "staging/provision_report.json": b"{}",
    }
    assert sent == 3


def test_a_second_upload_sends_only_what_changed(tmp_path: Path, s3: Any) -> None:
    root = _run_dir(tmp_path)
    upload_run_dir(s3, bucket_name(RUN), root)
    (root / "staging" / "repo.tar.zst").write_bytes(b"repo")  # the fetch job's output
    assert upload_run_dir(s3, bucket_name(RUN), root) == 1
    assert _keys(s3)["staging/repo.tar.zst"] == b"repo"


@POSIX
def test_links_and_fifos_are_never_followed_or_sent(tmp_path: Path, s3: Any) -> None:
    root = _run_dir(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("host secret")
    (root / "staging" / "link.txt").symlink_to(secret)
    (root / "staging" / "dirlink").symlink_to(tmp_path)
    os.mkfifo(root / "staging" / "fifo")
    upload_run_dir(s3, bucket_name(RUN), root)
    assert set(_keys(s3)) == {
        "staging/overlay.tar.zst",
        "staging/data/cifar.tar.zst",
        "staging/provision_report.json",
    }


def test_a_run_copy_over_its_bounds_is_refused(
    tmp_path: Path, s3: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _run_dir(tmp_path)
    monkeypatch.setattr(hybrid, "MAX_UPLOAD_FILES", 2)
    with pytest.raises(UploadRefused, match="files"):
        upload_run_dir(s3, bucket_name(RUN), root)
    monkeypatch.setattr(hybrid, "MAX_UPLOAD_FILES", 100)
    monkeypatch.setattr(hybrid, "MAX_UPLOAD_BYTES", 10)
    with pytest.raises(UploadRefused, match="bytes"):
        upload_run_dir(s3, bucket_name(RUN), root)
    assert _keys(s3) == {}  # checked before anything is sent


def test_a_name_that_is_not_a_safe_key_is_refused(tmp_path: Path, s3: Any) -> None:
    root = _run_dir(tmp_path)
    (root / "staging" / "bad\x7fname").write_bytes(b"x")
    with pytest.raises(UploadRefused, match="key"):
        upload_run_dir(s3, bucket_name(RUN), root)


# --- HybridJobBackend: provision and fetch here, exec and smoke on Nebius -----------------


class Uploads:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, Path]] = []
        self.error = error

    def __call__(self, bucket: str, root: Path) -> int:
        self.calls.append((bucket, root))
        if self.error is not None:
            raise self.error
        return 1


def _hybrid(
    tmp_path: Path, uploads: Uploads | None = None
) -> tuple[HybridJobBackend, FakeJobBackend, FakeJobBackend, Uploads]:
    local, remote = FakeJobBackend(), FakeJobBackend()
    uploads = uploads or Uploads()
    backend = HybridJobBackend(
        local=local, remote=remote, local_buckets=tmp_path / "buckets", upload=uploads
    )
    return backend, local, remote, uploads


def _job(kind: JobKind) -> Any:
    short = "prov" if kind is JobKind.PROVISION else kind.value
    if kind in (JobKind.PROVISION, JobKind.FETCH):
        return _prov(kind)
    return _spec(kind, name=f"p2p-{short}-{SLUG}-1", platform="gpu-l40s-a")


@pytest.mark.parametrize(
    ("kind", "where"),
    [(JobKind.PROVISION, "local"), (JobKind.FETCH, "local"),
     (JobKind.EXEC, "remote"), (JobKind.SMOKE, "remote")],
)  # fmt: skip
async def test_jobs_are_routed_by_kind(tmp_path: Path, kind: JobKind, where: str) -> None:
    backend, local, remote, _ = _hybrid(tmp_path)
    handle = await backend.submit(_job(kind))
    ran, other = (local, remote) if where == "local" else (remote, local)
    assert handle.name in ran.jobs and not other.jobs
    await backend.cancel(handle)
    assert ran.cancelled == [handle.name]


async def test_a_local_job_succeeds_only_once_its_output_is_in_the_run_bucket(
    tmp_path: Path,
) -> None:
    backend, _, _, uploads = _hybrid(tmp_path)
    handle = await backend.submit(_job(JobKind.PROVISION))
    assert await backend.status(handle) is JobState.SUCCEEDED
    assert uploads.calls == [(bucket_name(RUN), tmp_path / "buckets" / bucket_name(RUN))]
    assert await backend.status(handle) is JobState.SUCCEEDED
    assert len(uploads.calls) == 1  # polled again after success: nothing sent twice


async def test_a_refused_upload_fails_the_job(tmp_path: Path) -> None:
    backend, _, _, _ = _hybrid(tmp_path, Uploads(UploadRefused("too many files")))
    handle = await backend.submit(_job(JobKind.FETCH))
    assert await backend.status(handle) is JobState.FAILED


async def test_an_upload_that_cannot_reach_the_bucket_is_retried_at_the_next_poll(
    tmp_path: Path,
) -> None:
    uploads = Uploads(OSError("connection reset"))
    backend, _, _, _ = _hybrid(tmp_path, uploads)
    handle = await backend.submit(_job(JobKind.PROVISION))
    with pytest.raises(JobStatusUnavailable):
        await backend.status(handle)
    uploads.error = None
    assert await backend.status(handle) is JobState.SUCCEEDED


async def test_a_failed_local_job_uploads_nothing(tmp_path: Path) -> None:
    backend, local, _, uploads = _hybrid(tmp_path)
    local.final_state = JobState.FAILED
    handle = await backend.submit(_job(JobKind.PROVISION))
    assert await backend.status(handle) is JobState.FAILED
    assert uploads.calls == []


async def test_remote_jobs_are_never_uploaded(tmp_path: Path) -> None:
    backend, _, _, uploads = _hybrid(tmp_path)
    handle = await backend.submit(_job(JobKind.EXEC))
    assert await backend.status(handle) is JobState.SUCCEEDED
    assert uploads.calls == []


async def test_both_copies_of_the_run_bucket_are_created_and_deleted(tmp_path: Path) -> None:
    backend, local, remote, _ = _hybrid(tmp_path)
    assert await backend.ensure_run_bucket(RUN) == bucket_name(RUN)
    assert local.buckets == remote.buckets == {bucket_name(RUN)}
    assert [n for n, _ in await backend.list_run_buckets()] == [bucket_name(RUN)]
    await backend.delete_run_bucket(RUN)
    assert local.buckets == remote.buckets == set()


async def test_the_reaper_sees_local_and_remote_jobs_and_there_is_no_gateway(
    tmp_path: Path,
) -> None:
    backend, _, _, _ = _hybrid(tmp_path)
    await backend.submit(_job(JobKind.PROVISION))
    await backend.submit(_job(JobKind.EXEC))
    names = {h.name for h, _, _ in await backend.list_jobs("p2p-")}
    assert names == {f"p2p-prov-{SLUG}-1", f"p2p-exec-{SLUG}-1"}
    await backend.ensure_gateway_up()  # nothing to start: exec isolation is the route
    assert await backend.gateway_idle_check() is False

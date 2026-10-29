"""SandboxesJobBackend against a respx mock of the Sandboxes REST API (docs/specs/hybrid-deploy.md,
"Experiments in Sandboxes"; request shapes from the API reference, to be confirmed by S14)."""

import asyncio
import base64
import hashlib
import io
import json
import os
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from app.execution.backend import (
    CapacityError,
    JobHandle,
    JobKind,
    JobSpec,
    JobState,
    JobStatusUnavailable,
    VolumeMount,
)
from app.execution.nebius_backend import JobBackendError
from app.execution.run_storage import FakeRunStorage
from app.execution.sandbox_jobs import (
    SANDBOX_PLATFORM,
    SandboxesJobBackend,
    job_command,
    parse_harvest,
)
from app.execution.sandboxes import SANDBOXES_API

RUN = "r_01jb7z00000000000000000000"
SLUG = RUN[2:]
BUCKET = f"p2p-run-{SLUG}"
CPU_IMAGE = "cr.example/p2p-runner-cpu@sha256:" + "c" * 64
GPU_IMAGE = "cr.example/p2p-runner@sha256:" + "a" * 64
IMAGE_UUID = "11111111-2222-3333-4444-555555555555"
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
FILES = f"{SANDBOXES_API}/v1/files"
INSTANCES = f"{SANDBOXES_API}/v1/instances"
OPS = f"{SANDBOXES_API}/v1/operations"


def _exec_manifest(*, repo: bool = True, data: tuple[str, ...] = ("mnist",)) -> dict[str, Any]:
    return {
        "schema": "p2p.manifest/v2",
        "run_id": RUN,
        "attempt": 1,
        "image_digest": "sha256:" + "c" * 64,
        "entrypoint": "workspace/main.py",
        "args": [],
        "seeds": [1, 2, 3],
        "budget_seconds": 600,
        "paths": {
            "overlay": "/mnt/run/staging/overlay.tar.zst",
            "repo": "/mnt/run/staging/repo.tar.zst" if repo else None,
            "data_dir": "/mnt/run/staging/data",
            "workspace": "/mnt/run/staging/workspace.tar",
            "cache": "/mnt/cache",
            "results": "/mnt/run/results/1",
        },
        "claims": [
            {
                "claim_ref": "c1",
                "claim_type": "ABSOLUTE",
                "metric": "accuracy",
                "higher_is_better": True,
                "metric_key": "accuracy",
                "baseline_name": None,
                "baseline_key": None,
            }
        ],
        "scale": {"full": False},
        "archives": {
            "overlay": "1" * 64,
            "repo": "2" * 64 if repo else None,
            "data": [{"name": n, "sha256": "3" * 64} for n in data],
        },
        "workspace_sha256": "4" * 64,
    }


def _smoke_manifest(*, repo: bool = False, data: bool = True) -> dict[str, Any]:
    return {
        "schema": "p2p.smoke/v1",
        "run_id": RUN,
        "iteration": 2,
        "argv": ["python", "main.py"],
        "timeout_s": 120,
        "workspace_sha256": "5" * 64,
        "nonce": "6" * 32,
        "archives": {
            "overlay": "1" * 64,
            "repo": "2" * 64 if repo else None,
            "data": [{"name": "smoke", "sha256": "3" * 64}] if data else [],
        },
    }


def _spec(
    kind: JobKind = JobKind.EXEC,
    manifest: dict[str, Any] | None = None,
    image: str = CPU_IMAGE,
    watchdog_s: int = 900,
) -> JobSpec:
    if kind is JobKind.SMOKE:
        name, inject = f"p2p-smoke-{SLUG}-2", "/etc/p2p/smoke.json"
        body = manifest or _smoke_manifest()
    else:
        name, inject = f"p2p-{kind.value}-{SLUG}-1", "/etc/p2p/manifest.json"
        body = manifest or _exec_manifest()
    return JobSpec(
        name=name,
        kind=kind,
        image_digest_ref=image,
        platform=SANDBOX_PLATFORM,
        preset="4vcpu-4gb",
        disk_gib=30,
        subnet_id="sandboxes",
        volumes=[VolumeMount(source=BUCKET, mount_path="/mnt/run", mode="rw")],
        inject_files={inject: json.dumps(body).encode()},
        env={"PYTHONUNBUFFERED": "1"},
        preemptible=False,
        watchdog_s=watchdog_s,
    )


@pytest.fixture
def local(tmp_path: Path) -> Path:
    root = tmp_path / "buckets"
    staging = root / BUCKET / "staging"
    (staging / "data").mkdir(parents=True)
    (staging / "overlay.tar.zst").write_bytes(b"overlay-bytes")
    (staging / "repo.tar.zst").write_bytes(b"repo-bytes")
    (staging / "data" / "mnist.tar.zst").write_bytes(b"mnist-bytes")
    (staging / "data" / "smoke.tar.zst").write_bytes(b"smoke-bytes")
    return root


@pytest.fixture
def storage() -> FakeRunStorage:
    store = FakeRunStorage()
    store.objects[(RUN, "staging/workspace.tar")] = b"workspace-tar"
    store.objects[(RUN, "smoke/2/workspace.tar")] = b"smoke-workspace-tar"
    return store


def _backend(
    tmp_path: Path, local: Path, storage: FakeRunStorage, clock: list[datetime] | None = None
) -> SandboxesJobBackend:
    times = clock if clock is not None else [NOW]
    return SandboxesJobBackend(
        api_key=SecretStr("test-key-not-real"),
        project_id="project-e00test",
        runner_image=CPU_IMAGE,
        image_uuid=IMAGE_UUID,
        storage=storage,
        local_buckets=local,
        index_dir=tmp_path / "sandbox-jobs",
        clock=lambda: times[-1],
        sleep=_no_wait,
    )


SLEPT: list[float] = []


async def _no_wait(seconds: float) -> None:
    SLEPT.append(seconds)


class Api:
    """The Sandboxes API: content-addressed files, instances, operations."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}  # sha256 -> content
        self.spawns: list[dict[str, Any]] = []
        self.ops: dict[str, dict[str, Any]] = {}
        self.uploads = 0
        self.cancelled: list[str] = []
        self.spawn_error: int | None = None  # HTTP status the next spawns answer
        self.stored_sha: str | None = None  # what the upload claims it stored
        self.op_error: int | None = None
        self.cancel_error: int | None = None
        self.list_error: int | None = None
        self.lost_reply: type[Exception] | None = None  # the spawn happens, its reply is lost
        self.upload_errors: list[int] = []  # HTTP status the next uploads answer, in order
        self.stored_despite_error = False  # a 502 from the gateway after the file was stored
        self.posts = 0
        self.upload_drops = 0  # uploads whose connection is dropped
        self.lookup_errors: list[int] = []  # HTTP status the next GET /v1/files answers
        self.lookup_fails_after_an_error = False  # the gateway outage hits the lookup too

    def install(self, router: respx.Router) -> None:
        router.get(url__regex=rf"{FILES}/[0-9a-f]{{64}}$").mock(side_effect=self._file)
        router.post(FILES).mock(side_effect=self._upload)
        router.post(INSTANCES).mock(side_effect=self._spawn)
        router.get(url__regex=rf"{OPS}/[0-9a-f-]+$").mock(side_effect=self._op)
        router.delete(url__regex=rf"{OPS}/[0-9a-f-]+$").mock(side_effect=self._cancel)
        router.get(url__startswith=f"{OPS}?").mock(side_effect=self._list)

    def _uuid(self, sha: str) -> str:
        return f"{sha[:8]}-0000-0000-0000-{sha[8:20]}"

    def _file(self, request: httpx.Request) -> httpx.Response:
        if self.lookup_errors:
            return httpx.Response(self.lookup_errors.pop(0), text="<html>Bad Gateway</html>")
        sha = request.url.path.rsplit("/", 1)[-1]
        if sha not in self.files:
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(200, json={"uuid": self._uuid(sha), "sha256": sha})

    def _upload(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["content-type"] == "application/octet-stream"
        assert "transfer-encoding" not in request.headers  # file APIs often refuse chunked
        assert int(request.headers["content-length"]) == len(request.read())
        assert request.headers["authorization"] == "Bearer test-key-not-real"
        assert request.headers["project"] == "project-e00test"
        data = request.read()
        sha = hashlib.sha256(data).hexdigest()
        self.posts += 1
        if self.upload_drops:
            self.upload_drops -= 1
            raise httpx.ReadTimeout("the gateway dropped the upload", request=request)
        if self.upload_errors:
            if self.stored_despite_error:
                self.files[sha] = data
            if self.lookup_fails_after_an_error:
                self.lookup_errors = [502]  # the lookup right after this failed upload
            return httpx.Response(self.upload_errors.pop(0), text="<html>Bad Gateway</html>")
        self.files[sha] = data
        self.uploads += 1
        claimed = self.stored_sha or sha
        body = {"uuid": self._uuid(sha), "sha256": claimed, "size": len(data)}
        return httpx.Response(201, json=body)

    def content(self, uuid: str) -> bytes:
        return next(data for sha, data in self.files.items() if self._uuid(sha) == uuid)

    def _spawn(self, request: httpx.Request) -> httpx.Response:
        if self.spawn_error:
            return httpx.Response(self.spawn_error, json={"error": "refused"})
        body = json.loads(request.content)
        self.spawns.append(body)
        uuid = f"0000{len(self.spawns):04d}-aaaa-bbbb-cccc-000000000000"
        self.ops[uuid] = {"uuid": uuid, "kind": "instance", "status": "PENDING", "metadata": body}
        if self.lost_reply is not None:
            raise self.lost_reply("the reply was lost")
        return httpx.Response(201, json={"uuid": uuid})

    def finish(
        self, uuid: str, *, exit_code: int = 0, outputs: dict[str, bytes] | None = None,
        status: str = "SUCCESS", marker: str | None = None, stdout: str | None = None,
    ) -> None:  # fmt: skip
        op = self.ops[uuid]
        marker = marker or job_marker(op["metadata"]["command"])
        if stdout is None:
            stdout = "noise\n" + harvest_text(marker, outputs or {})
        op["status"] = status
        op["metadata"] = {
            **op["metadata"],
            "result": {
                "state": {"exit_code": exit_code, "timed_out": False},
                "stdout": {"value": stdout, "encoding": "ascii", "truncated": False},
                "stderr": {"value": "", "encoding": "ascii", "truncated": False},
            },
        }

    def _op(self, request: httpx.Request) -> httpx.Response:
        uuid = request.url.path.rsplit("/", 1)[-1]
        if self.op_error:
            return httpx.Response(self.op_error)
        if uuid not in self.ops:
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(200, json=self.ops[uuid])

    def _cancel(self, request: httpx.Request) -> httpx.Response:
        uuid = request.url.path.rsplit("/", 1)[-1]
        if self.cancel_error:
            return httpx.Response(self.cancel_error)
        self.cancelled.append(uuid)
        if uuid not in self.ops:
            return httpx.Response(404, json={"error": "not found"})
        self.ops[uuid]["status"] = "CANCELLED"
        return httpx.Response(202)

    def _list(self, request: httpx.Request) -> httpx.Response:
        assert request.url.params["kind"] == "instance"
        if self.list_error:
            return httpx.Response(self.list_error)
        return httpx.Response(
            200,
            json=[
                {"uuid": u, "kind": "instance", "status": o["status"]} for u, o in self.ops.items()
            ],
        )


def job_marker(command: str) -> str:
    return command.split("--marker ")[1].split()[0]


def harvest_text(marker: str, outputs: dict[str, bytes], *, link: str | None = None) -> str:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name, data in outputs.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        if link:
            info = tarfile.TarInfo(link)
            info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
            tar.addfile(info)
    payload = base64.b64encode(buffer.getvalue()).decode()
    return f"\n{marker}\n{payload}\n{marker}-END\n"


@pytest.fixture
def api() -> Any:
    fake = Api()
    with respx.mock(assert_all_called=False) as router:
        fake.install(router)
        yield fake


# --- submit -------------------------------------------------------------------------------


async def test_exec_submit_mounts_the_inputs_and_runs_offline_as_the_job_would(
    tmp_path: Path, local: Path, storage: FakeRunStorage, api: Api
) -> None:
    backend = _backend(tmp_path, local, storage)
    handle = await backend.submit(_spec())
    assert handle == JobHandle(name=f"p2p-exec-{SLUG}-1", nebius_id=api.ops and next(iter(api.ops)))
    (body,) = api.spawns
    assert body["image"] == IMAGE_UUID
    assert body["networking"] == {"enabled": False}
    assert body["shell"] is True and body["disposable"] is True
    assert body["uid"] == 0 and body["gid"] == 0
    assert body["env"] == {"PYTHONUNBUFFERED": "1", "P2P_JOB_NAME": f"p2p-exec-{SLUG}-1"}
    assert body["timeout"] == 900 + 120
    assert body["truncate_output_at"] == 10 * 1024 * 1024
    mounted = {path: api.content(f["uuid"]) for path, f in body["files"].items()}
    assert mounted == {
        "/mnt/run/staging/overlay.tar.zst": b"overlay-bytes",
        "/mnt/run/staging/repo.tar.zst": b"repo-bytes",
        "/mnt/run/staging/data/mnist.tar.zst": b"mnist-bytes",
        "/mnt/run/staging/workspace.tar": b"workspace-tar",
        "/etc/p2p/manifest.json": _spec().inject_files["/etc/p2p/manifest.json"],
    }
    assert {(f["uid"], f["gid"], f["mode"]) for f in body["files"].values()} == {(0, 0, "0444")}
    assert "timeout" in body["command"] and "--reuid=10001" in body["command"]
    assert "--kind exec" in body["command"] and "--dir /mnt/run/results/1" in body["command"]


async def test_smoke_submit_mounts_the_smoke_inputs(
    tmp_path: Path, local: Path, storage: FakeRunStorage, api: Api
) -> None:
    await _backend(tmp_path, local, storage).submit(_spec(JobKind.SMOKE))
    (body,) = api.spawns
    mounted = {path: api.content(f["uuid"]) for path, f in body["files"].items()}
    assert mounted == {
        "/mnt/run/staging/overlay.tar.zst": b"overlay-bytes",
        "/mnt/run/staging/data/smoke.tar.zst": b"smoke-bytes",
        "/mnt/run/smoke/2/workspace.tar": b"smoke-workspace-tar",
        "/etc/p2p/smoke.json": _spec(JobKind.SMOKE).inject_files["/etc/p2p/smoke.json"],
    }
    assert "--kind smoke" in body["command"] and "--dir /mnt/run/smoke/2" in body["command"]


async def test_an_input_missing_everywhere_is_not_mounted(
    tmp_path: Path, local: Path, storage: FakeRunStorage, api: Api
) -> None:
    (local / BUCKET / "staging" / "repo.tar.zst").unlink()
    await _backend(tmp_path, local, storage).submit(_spec())
    assert "/mnt/run/staging/repo.tar.zst" not in api.spawns[0]["files"]  # the runner says why


async def test_files_the_namespace_has_are_not_uploaded_again(
    tmp_path: Path, local: Path, storage: FakeRunStorage, api: Api
) -> None:
    backend = _backend(tmp_path, local, storage)
    await backend.submit(_spec(JobKind.SMOKE))
    uploads = api.uploads
    smoke = _smoke_manifest()
    smoke["iteration"] = 3
    storage.objects[(RUN, "smoke/3/workspace.tar")] = b"smoke-workspace-tar"
    spec = _spec(JobKind.SMOKE, smoke).model_copy(update={"name": f"p2p-smoke-{SLUG}-3"})
    await _backend(tmp_path, local, storage).submit(spec)  # a fresh process: lookups only
    assert api.uploads == uploads + 1  # only the new manifest (its iteration differs)


async def test_submit_is_get_by_name_first(
    tmp_path: Path, local: Path, storage: FakeRunStorage, api: Api
) -> None:
    first = await _backend(tmp_path, local, storage).submit(_spec())
    again = await _backend(tmp_path, local, storage).submit(_spec())
    assert again == first and len(api.spawns) == 1


async def test_a_spawn_lost_by_a_crash_is_found_again_by_its_env(
    tmp_path: Path, local: Path, storage: FakeRunStorage, api: Api
) -> None:
    backend = _backend(tmp_path, local, storage)
    first = await backend.submit(_spec())
    index = tmp_path / "sandbox-jobs" / f"{first.name}.json"
    entry = json.loads(index.read_text(encoding="utf-8"))
    entry["op"] = None  # the control plane died between the spawn and the index write
    index.write_text(json.dumps(entry), encoding="utf-8")
    again = await _backend(tmp_path, local, storage).submit(_spec())
    assert again == first and len(api.spawns) == 1


@pytest.mark.parametrize(
    ("kind", "image"),
    [(JobKind.EXEC, GPU_IMAGE), (JobKind.PROVISION, CPU_IMAGE), (JobKind.FETCH, CPU_IMAGE)],
)
async def test_only_runner_cpu_smoke_and_exec_jobs_are_accepted(
    tmp_path: Path, local: Path, storage: FakeRunStorage, api: Api, kind: JobKind, image: str
) -> None:
    with pytest.raises(ValueError, match="Sandboxes"):
        await _backend(tmp_path, local, storage).submit(_spec(kind, image=image))
    assert api.spawns == []


async def test_a_watchdog_past_the_instance_limit_is_refused(
    tmp_path: Path, local: Path, storage: FakeRunStorage, api: Api
) -> None:
    with pytest.raises(ValueError, match="3600"):
        await _backend(tmp_path, local, storage).submit(_spec(watchdog_s=3500))


async def test_too_many_instances_is_no_capacity(
    tmp_path: Path, local: Path, storage: FakeRunStorage, api: Api
) -> None:
    api.spawn_error = 429
    backend = _backend(tmp_path, local, storage)
    with pytest.raises(CapacityError):
        await backend.submit(_spec())
    api.spawn_error = None
    await backend.submit(_spec())  # the refused intent does not block a later submission
    assert len(api.spawns) == 1


@pytest.mark.parametrize("status", [400, 500, 503])
async def test_other_spawn_errors_are_backend_errors(
    tmp_path: Path, local: Path, storage: FakeRunStorage, api: Api, status: int
) -> None:
    api.spawn_error = status
    with pytest.raises(JobBackendError, match=str(status)):
        await _backend(tmp_path, local, storage).submit(_spec())


async def test_a_spawn_whose_reply_was_lost_is_found_again_never_spawned_twice(
    tmp_path: Path, local: Path, storage: FakeRunStorage, api: Api
) -> None:
    backend = _backend(tmp_path, local, storage)
    api.lost_reply = httpx.ReadTimeout
    with pytest.raises(JobBackendError):
        await backend.submit(_spec())
    api.lost_reply = None
    handle = await backend.submit(_spec())  # the intent is kept: looked up by its env first
    assert len(api.spawns) == 1 and handle.nebius_id == next(iter(api.ops))


async def test_a_5xx_spawn_keeps_its_intent_and_a_later_submit_spawns_once(
    tmp_path: Path, local: Path, storage: FakeRunStorage, api: Api
) -> None:
    backend = _backend(tmp_path, local, storage)
    api.spawn_error = 503
    with pytest.raises(JobBackendError):
        await backend.submit(_spec())
    assert (tmp_path / "sandbox-jobs" / f"p2p-exec-{SLUG}-1.json").exists()
    api.spawn_error = None
    await backend.submit(_spec())
    assert len(api.spawns) == 1


async def test_a_spawn_that_never_left_drops_its_intent(
    tmp_path: Path, local: Path, storage: FakeRunStorage, api: Api
) -> None:
    api.lost_reply = httpx.ConnectError  # the connection was never made: nothing was sent
    backend = _backend(tmp_path, local, storage)
    with pytest.raises(JobBackendError):
        await backend.submit(_spec())
    assert not (tmp_path / "sandbox-jobs" / f"p2p-exec-{SLUG}-1.json").exists()


async def test_an_unlistable_lookup_is_unavailable_never_not_found(
    tmp_path: Path, local: Path, storage: FakeRunStorage, api: Api
) -> None:
    backend = _backend(tmp_path, local, storage)
    first = await backend.submit(_spec())
    index = tmp_path / "sandbox-jobs" / f"{first.name}.json"
    index.write_text(
        json.dumps({**json.loads(index.read_text(encoding="utf-8")), "op": None}), encoding="utf-8"
    )
    api.list_error = 400
    with pytest.raises(JobStatusUnavailable):
        await backend.submit(_spec())
    assert len(api.spawns) == 1


async def test_an_upload_the_server_hashes_differently_is_refused(
    tmp_path: Path, local: Path, storage: FakeRunStorage, api: Api
) -> None:
    api.stored_sha = "0" * 64
    with pytest.raises(JobBackendError, match="sha256"):
        await _backend(tmp_path, local, storage).submit(_spec())
    assert api.spawns == []


@pytest.mark.skipif(os.name == "nt", reason="symlinks need POSIX")
async def test_a_link_in_the_local_copy_is_never_followed(
    tmp_path: Path, local: Path, storage: FakeRunStorage, api: Api
) -> None:
    secret = tmp_path / "p2p.env"
    secret.write_bytes(b"SECRET=1")
    overlay = local / BUCKET / "staging" / "overlay.tar.zst"
    overlay.unlink()
    overlay.symlink_to(secret)
    await _backend(tmp_path, local, storage).submit(_spec())
    assert b"SECRET=1" not in api.files.values()
    assert "/mnt/run/staging/overlay.tar.zst" not in api.spawns[0]["files"]


# --- status -------------------------------------------------------------------------------


async def test_states_map_and_outputs_reach_the_run_bucket_before_success(
    tmp_path: Path, local: Path, storage: FakeRunStorage, api: Api
) -> None:
    backend = _backend(tmp_path, local, storage)
    handle = await backend.submit(_spec())
    assert await backend.status(handle) is JobState.PENDING
    api.ops[handle.nebius_id]["status"] = "ASSIGNED"
    assert await backend.status(handle) is JobState.PENDING
    api.ops[handle.nebius_id]["status"] = "EXECUTING"
    assert await backend.status(handle) is JobState.RUNNING
    api.finish(handle.nebius_id, outputs={"results.json": b'{"r": 1}', "live.log": b"x\n"})
    assert await backend.status(handle) is JobState.SUCCEEDED
    assert storage.objects[(RUN, "results/1/results.json")] == b'{"r": 1}'
    assert storage.objects[(RUN, "results/1/live.log")] == b"x\n"
    api.ops.clear()  # terminal: answered from the index, the API is not asked again
    assert await _backend(tmp_path, local, storage).status(handle) is JobState.SUCCEEDED


@pytest.mark.parametrize(
    ("status", "exit_code", "expected"),
    [
        ("SUCCESS", 20, JobState.FAILED),
        ("SUCCEEDED", 0, JobState.SUCCEEDED),
        ("FAILED", 0, JobState.FAILED),
        ("CANCELLED", 0, JobState.CANCELLED),
    ],
)
async def test_terminal_states(
    tmp_path: Path, local: Path, storage: FakeRunStorage, api: Api,
    status: str, exit_code: int, expected: JobState,
) -> None:  # fmt: skip
    backend = _backend(tmp_path, local, storage)
    handle = await backend.submit(_spec())
    api.finish(
        handle.nebius_id, status=status, exit_code=exit_code, outputs={"results.json": b"{}"}
    )
    assert await backend.status(handle) is expected
    # partial results count too, of a job cancelled at the host's watchdog as well
    assert (RUN, "results/1/results.json") in storage.objects


async def test_an_empty_harvest_is_a_failure_with_nothing_written(
    tmp_path: Path, local: Path, storage: FakeRunStorage, api: Api, caplog: pytest.LogCaptureFixture
) -> None:
    backend = _backend(tmp_path, local, storage)
    handle = await backend.submit(_spec())
    api.finish(handle.nebius_id, exit_code=41, outputs={})  # the output dir could not be made
    assert await backend.status(handle) is JobState.FAILED
    assert "exit code 41" in caplog.text
    assert not any(key.startswith("results/") for _, key in storage.objects)


async def test_a_failed_job_keeps_its_instance_log_for_diagnosis(
    tmp_path: Path, local: Path, storage: FakeRunStorage, api: Api
) -> None:
    backend = _backend(tmp_path, local, storage)
    handle = await backend.submit(_spec())
    api.finish(handle.nebius_id, exit_code=30, outputs={"results.json": b"{}"})
    api.ops[handle.nebius_id]["metadata"]["result"]["stderr"]["value"] = "CONTRACT ERROR: x"
    assert await backend.status(handle) is JobState.FAILED
    assert storage.objects[(RUN, "results/1/instance.log")] == b"CONTRACT ERROR: x"


class BrokenStorage(FakeRunStorage):
    async def put_bytes(self, run_id: str, key: str, data: bytes) -> None:
        if key.startswith("results/"):
            raise OSError("bucket unreachable")
        await super().put_bytes(run_id, key, data)


async def test_a_harvest_that_cannot_reach_the_bucket_is_retried_at_the_next_poll(
    tmp_path: Path, local: Path, api: Api
) -> None:
    storage = BrokenStorage()
    storage.objects[(RUN, "staging/workspace.tar")] = b"workspace-tar"
    backend = _backend(tmp_path, local, storage)
    handle = await backend.submit(_spec())
    api.finish(handle.nebius_id, outputs={"results.json": b"{}"})
    with pytest.raises(JobStatusUnavailable):
        await backend.status(handle)
    good = FakeRunStorage()
    assert await _backend(tmp_path, local, good).status(handle) is JobState.SUCCEEDED
    assert good.objects[(RUN, "results/1/results.json")] == b"{}"


async def test_a_corrupt_index_file_is_read_as_no_entry(
    tmp_path: Path, local: Path, storage: FakeRunStorage, api: Api
) -> None:
    backend = _backend(tmp_path, local, storage)
    handle = await backend.submit(_spec())
    (tmp_path / "sandbox-jobs" / f"{handle.name}.json").write_text("{trunc", encoding="utf-8")
    assert await backend.list_jobs("p2p-") == []  # the reaper still sees every other job


async def test_the_reaper_adopts_an_orphan_its_intent_finds(
    tmp_path: Path, local: Path, storage: FakeRunStorage, api: Api
) -> None:
    backend = _backend(tmp_path, local, storage)
    api.lost_reply = httpx.ReadTimeout
    with pytest.raises(JobBackendError):
        await backend.submit(_spec())
    api.lost_reply = None
    ((handle, state, _),) = await backend.list_jobs("p2p-")
    assert handle.nebius_id == next(iter(api.ops)) and state is JobState.PENDING


async def test_a_lookup_outage_never_fails_the_whole_listing(
    tmp_path: Path, local: Path, storage: FakeRunStorage, api: Api
) -> None:
    backend = _backend(tmp_path, local, storage)
    handle = await backend.submit(_spec(JobKind.SMOKE))
    api.spawn_error = 503
    with pytest.raises(JobBackendError):
        await backend.submit(_spec())
    api.list_error = 500
    assert [h for h, _, _ in await backend.list_jobs("p2p-")] == [handle]


async def test_a_smoke_result_lands_under_its_iteration(
    tmp_path: Path, local: Path, storage: FakeRunStorage, api: Api
) -> None:
    backend = _backend(tmp_path, local, storage)
    handle = await backend.submit(_spec(JobKind.SMOKE))
    api.finish(handle.nebius_id, outputs={"result.json": b'{"nonce": "n"}'})
    assert await backend.status(handle) is JobState.SUCCEEDED
    assert storage.objects[(RUN, "smoke/2/result.json")] == b'{"nonce": "n"}'


async def test_a_job_without_a_harvest_fails_and_writes_nothing(
    tmp_path: Path, local: Path, storage: FakeRunStorage, api: Api
) -> None:
    backend = _backend(tmp_path, local, storage)
    handle = await backend.submit(_spec())
    api.finish(handle.nebius_id, stdout="no marker here")
    assert await backend.status(handle) is JobState.FAILED
    assert not any(key.startswith("results/") for _, key in storage.objects)


@pytest.mark.parametrize("status", [404, 500])
async def test_an_unreadable_operation_is_status_unavailable(
    tmp_path: Path, local: Path, storage: FakeRunStorage, api: Api, status: int
) -> None:
    backend = _backend(tmp_path, local, storage)
    handle = await backend.submit(_spec())
    api.op_error = status
    with pytest.raises(JobStatusUnavailable):
        await backend.status(handle)


async def test_an_unknown_operation_status_is_unknown(
    tmp_path: Path, local: Path, storage: FakeRunStorage, api: Api
) -> None:
    backend = _backend(tmp_path, local, storage)
    handle = await backend.submit(_spec())
    api.ops[handle.nebius_id]["status"] = "WEIRD"
    assert await backend.status(handle) is JobState.UNKNOWN


# --- harvest parsing ------------------------------------------------------------------------

MARKER = "cd" * 16


def test_parse_harvest_takes_allowed_regular_files_only() -> None:
    text = "x\n" + harvest_text(
        MARKER, {"results.json": b"{}", "other.txt": b"no", "env.json": b"{}"}, link="live.log"
    )
    assert parse_harvest(text, MARKER, "exec") == {"results.json": b"{}", "env.json": b"{}"}


def test_parse_harvest_drops_oversized_files() -> None:
    big = b"{" * (256 * 1024 + 2)  # harvest.py keeps cap + 1: anything past that is not ours
    text = harvest_text(MARKER, {"results.json": big, "env.json": b"{}"})
    assert parse_harvest(text, MARKER, "exec") == {"env.json": b"{}"}


@pytest.mark.parametrize(
    "text",
    [
        "",
        f"\n{MARKER}\nnot base64!\n{MARKER}-END\n",
        f"\n{MARKER}\n{base64.b64encode(b'not a tar').decode()}\n{MARKER}-END\n",
        f"\n{MARKER}\n",  # truncated: no end line
    ],
)
def test_parse_harvest_without_a_valid_payload_is_none(text: str) -> None:
    assert parse_harvest(text, MARKER, "exec") is None


def test_parse_harvest_uses_the_last_block_only() -> None:
    forged = harvest_text(MARKER, {"result.json": b"forged"})  # printed before the kill
    real = harvest_text(MARKER, {"result.json": b"real"})  # the harvest runs last
    assert parse_harvest(forged + real, MARKER, "smoke") == {"result.json": b"real"}


def test_job_command_drops_privileges_and_harvests_as_uid_10001() -> None:
    command = job_command(kind=JobKind.EXEC, out_dir="/mnt/run/results/1", watchdog_s=900,
                          marker=MARKER)  # fmt: skip
    # 60 s before the host's watchdog: TERM, the 30 s grace and the harvest end before it
    assert "timeout --preserve-status -k 30 -s TERM 840 setpriv" in command
    assert command.count("--reuid=10001 --regid=10001 --clear-groups") == 3
    assert '|| logdir="$(mktemp -d)" || exit 40' in command  # no writable /root: still a log
    assert "setsid -w /opt/venv/bin/python -I /opt/p2p/entrypoint.py < /dev/null" in command
    assert "chmod 0750 /mnt/run/results/1 || exit 41" in command  # setup failures say so
    assert "--no-new-privs" in command
    assert "/opt/p2p/harvest.py --dir /mnt/run/results/1 --kind exec --marker " + MARKER in command
    assert command.rstrip().endswith('exit "$code"')


# --- cancel, list, forget -------------------------------------------------------------------


async def test_cancel_deletes_the_operation_and_ignores_a_missing_one(
    tmp_path: Path, local: Path, storage: FakeRunStorage, api: Api
) -> None:
    backend = _backend(tmp_path, local, storage)
    handle = await backend.submit(_spec())
    await backend.cancel(handle)
    assert api.cancelled == [handle.nebius_id]
    assert await backend.status(handle) is JobState.CANCELLED
    await backend.cancel(
        JobHandle(name=handle.name, nebius_id="99999999-0000-0000-0000-000000000000")
    )


async def test_cancel_errors_are_status_unavailable(
    tmp_path: Path, local: Path, storage: FakeRunStorage, api: Api
) -> None:
    backend = _backend(tmp_path, local, storage)
    handle = await backend.submit(_spec())
    api.cancel_error = 500
    with pytest.raises(JobStatusUnavailable):
        await backend.cancel(handle)


async def test_list_jobs_reads_the_index(
    tmp_path: Path, local: Path, storage: FakeRunStorage, api: Api
) -> None:
    clock = [NOW]
    backend = _backend(tmp_path, local, storage, clock)
    exec_handle = await backend.submit(_spec())
    clock.append(NOW + timedelta(minutes=1))
    smoke_handle = await backend.submit(_spec(JobKind.SMOKE))
    api.ops[smoke_handle.nebius_id]["status"] = "EXECUTING"
    listed = await backend.list_jobs("p2p-")
    assert sorted(listed, key=lambda t: t[0].name) == [
        (exec_handle, JobState.PENDING, NOW),
        (smoke_handle, JobState.RUNNING, NOW + timedelta(minutes=1)),
    ]
    assert [h for h, _, _ in await backend.list_jobs(f"p2p-smoke-{SLUG}-")] == [smoke_handle]


async def test_list_jobs_survives_an_outage_and_drops_stale_intents(
    tmp_path: Path, local: Path, storage: FakeRunStorage, api: Api
) -> None:
    clock = [NOW]
    backend = _backend(tmp_path, local, storage, clock)
    handle = await backend.submit(_spec())
    index = tmp_path / "sandbox-jobs"
    (index / f"p2p-smoke-{SLUG}-9.json").write_text(
        json.dumps({"name": f"p2p-smoke-{SLUG}-9", "op": None, "created_at": NOW.isoformat(),
                    "marker": MARKER, "out_dir": "/mnt/run/smoke/9", "kind": "smoke"}),
        encoding="utf-8",
    )  # fmt: skip
    clock.append(NOW + timedelta(hours=1))
    assert await backend.list_jobs(f"p2p-smoke-{SLUG}-") == []
    assert not (index / f"p2p-smoke-{SLUG}-9.json").exists()  # never spawned: forgotten
    api.op_error = 503
    assert await backend.list_jobs("p2p-") == [(handle, JobState.UNKNOWN, NOW)]


async def test_forget_drops_the_runs_index_entries(
    tmp_path: Path, local: Path, storage: FakeRunStorage, api: Api
) -> None:
    backend = _backend(tmp_path, local, storage)
    await backend.submit(_spec())
    await backend.forget(RUN)
    assert await backend.list_jobs("p2p-") == []


# --- hostile harvests (security review) ---------------------------------------------------


def _raw_harvest(build: Any) -> str:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        build(tar)
    return f"\n{MARKER}\n{base64.b64encode(buffer.getvalue()).decode()}\n{MARKER}-END\n"


def test_a_hardlink_or_a_long_name_is_never_taken() -> None:
    def build(tar: tarfile.TarFile) -> None:
        link = tarfile.TarInfo("results.json")
        link.type, link.linkname = tarfile.LNKTYPE, "/etc/shadow"
        tar.addfile(link)
        long = tarfile.TarInfo("x" * 300 + "/env.json")  # a pax long name, not "env.json"
        long.size = 2
        tar.addfile(long, io.BytesIO(b"{}"))

    assert parse_harvest(_raw_harvest(build), MARKER, "exec") == {}


def test_thousands_of_members_take_only_the_allowed_names_once() -> None:
    def build(tar: tarfile.TarFile) -> None:
        for i in range(3000):
            info = tarfile.TarInfo(f"junk{i}")
            tar.addfile(info, io.BytesIO(b""))
        for content in (b"first", b"second"):
            info = tarfile.TarInfo("results.json")
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))

    assert parse_harvest(_raw_harvest(build), MARKER, "exec") == {"results.json": b"first"}


def test_a_member_whose_body_is_cut_short_is_no_harvest() -> None:
    text = harvest_text(MARKER, {"results.json": b"{" * 4096})
    payload = text.split("\n")[2]
    raw = base64.b64decode(payload)[:1024]  # header kept, body truncated
    cut = f"\n{MARKER}\n{base64.b64encode(raw).decode()}\n{MARKER}-END\n"
    assert parse_harvest(cut, MARKER, "exec") in (None, {})


async def test_a_list_that_ignores_its_offset_never_loops(
    tmp_path: Path, local: Path, storage: FakeRunStorage, api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.execution.sandbox_jobs as module

    monkeypatch.setattr(module, "LIST_PAGE", 1)  # every page is "full": the same one, again
    backend = _backend(tmp_path, local, storage)
    other = await backend.submit(_spec(JobKind.SMOKE))
    assert other.nebius_id
    api.spawn_error = 503
    with pytest.raises(JobBackendError):
        await backend.submit(_spec())
    api.spawn_error = None
    with pytest.raises(JobStatusUnavailable, match="last page"):
        await backend.submit(_spec())  # the exec intent's lookup sees only the smoke op


async def test_a_transient_upload_error_is_retried(
    tmp_path: Path, local: Path, storage: FakeRunStorage, api: Api
) -> None:
    """Production, 2026-10-29: a 502 from the files API on a large overlay failed the run with
    an internal error. A content-addressed upload is safe to send again."""
    SLEPT.clear()
    api.upload_errors = [502, 503]
    await _backend(tmp_path, local, storage).submit(_spec())
    assert len(api.spawns) == 1
    assert len(SLEPT) == 2 and sorted(SLEPT) == SLEPT  # backing off


async def test_an_upload_that_landed_behind_a_502_is_not_sent_again(
    tmp_path: Path, local: Path, storage: FakeRunStorage, api: Api
) -> None:
    api.upload_errors = [502]
    api.stored_despite_error = True
    before = api.posts
    await _backend(tmp_path, local, storage).submit(_spec())
    first_input_posts = 1  # the failed POST; the lookup then finds the stored file
    assert len(api.spawns) == 1
    assert api.posts - before == api.uploads + first_input_posts


async def test_a_lasting_upload_error_fails_the_submit_without_a_spawn(
    tmp_path: Path, local: Path, storage: FakeRunStorage, api: Api
) -> None:
    api.upload_errors = [502] * 10
    with pytest.raises(JobBackendError, match="HTTP 502"):
        await _backend(tmp_path, local, storage).submit(_spec())
    assert api.spawns == []
    assert api.posts == 3  # UPLOAD_ATTEMPTS


async def test_a_refused_upload_is_not_retried(
    tmp_path: Path, local: Path, storage: FakeRunStorage, api: Api
) -> None:
    api.upload_errors = [413]
    with pytest.raises(JobBackendError, match="HTTP 413"):
        await _backend(tmp_path, local, storage).submit(_spec())
    assert api.posts == 1


async def test_a_dropped_upload_connection_is_retried(
    tmp_path: Path, local: Path, storage: FakeRunStorage, api: Api
) -> None:
    api.upload_drops = 1
    await _backend(tmp_path, local, storage).submit(_spec())
    assert len(api.spawns) == 1


async def test_a_lookup_the_gateway_fails_between_attempts_does_not_end_the_retries(
    tmp_path: Path, local: Path, storage: FakeRunStorage, api: Api
) -> None:
    backend = _backend(tmp_path, local, storage)
    api.upload_errors = [502]
    api.lookup_fails_after_an_error = True
    await backend.submit(_spec())
    assert len(api.spawns) == 1


async def test_an_input_that_shrinks_during_its_upload_is_changed_not_retried(
    tmp_path: Path, local: Path, storage: FakeRunStorage, api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.execution import sandbox_jobs

    hash_fd = sandbox_jobs._hash_fd

    def hash_then_truncate(fd: int, size: int) -> str:
        sha = hash_fd(fd, size)
        for path in local.rglob("*"):  # rewritten between the hash and the send
            if path.is_file() and path.stat().st_size:
                os.truncate(path, path.stat().st_size // 2)
        return sha

    monkeypatch.setattr(sandbox_jobs, "_hash_fd", hash_then_truncate)
    with pytest.raises(JobBackendError, match="changed during its upload"):
        await _backend(tmp_path, local, storage).submit(_spec())
    assert api.posts <= 1 and api.spawns == []


async def test_the_retries_have_a_total_deadline(
    tmp_path: Path, local: Path, storage: FakeRunStorage, api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Upload time counts against the job's queue cap and watchdog: the retries are bounded."""
    from app.execution import sandbox_jobs
    from app.execution.nebius_backend import InputUploadError

    monkeypatch.setattr(sandbox_jobs, "UPLOAD_TOTAL_S", 0.05)
    api.upload_errors = [502] * 10
    backend = _backend(tmp_path, local, storage)
    backend._sleep = asyncio.sleep  # a real wait, longer than the deadline
    monkeypatch.setattr(sandbox_jobs, "UPLOAD_BACKOFF_S", (1.0, 1.0))
    with pytest.raises(InputUploadError, match="within"):
        await backend.submit(_spec())
    assert api.spawns == []

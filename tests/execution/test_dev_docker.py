"""DevDockerBackend and LocalRunStorage (A-25 dev): the dev end-to-end runs the real job images
in local docker, for canary papers only, with the job's isolation (invariant 1 exception, the
same gate as LocalTrustedExecutor). Jobs write the run's directory, so storage never follows
what they plant there."""

import asyncio
import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.execution import dev_docker
from app.execution.backend import JobHandle, JobKind, JobSpec, JobState, VolumeMount
from app.execution.dev_docker import DevDockerBackend, LocalRunStorage, local_image
from app.execution.local_trusted import LocalTrustedRefused

RUN = "r_01jb7z00000000000000000000"
SLUG = RUN[2:]
CANARY = "c" * 64
IMAGE = "local/p2p-runner-cpu@sha256:" + "a" * 64
POSIX = pytest.mark.skipif(os.name == "nt", reason="POSIX links and FIFOs")


class Docker:
    """Records docker CLI calls; answers inspect/ps from the containers it ran."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.containers: dict[str, dict[str, object]] = {}
        self.kill_fails = False

    async def __call__(self, argv: Sequence[str], timeout_s: float) -> tuple[int, str, str]:
        argv = list(argv)
        self.calls.append(argv)
        if argv[1] == "run":
            name = next(a.split("=", 1)[1] for a in argv if a.startswith("--name="))
            labels = dict(
                a.split("=", 1)[1].split("=", 1) for a in argv if a.startswith("--label=")
            )
            self.containers[name] = {
                "Id": "cid-" + name,
                "Name": "/" + name,
                "Created": "2026-10-20T10:00:00.123456789Z",
                "Image": argv[-1],
                "Config": {"Labels": labels},
                "State": {"Status": "running", "ExitCode": 0, "StartedAt": _now()},
            }
            return 0, "cid-" + name + "\n", ""
        if argv[1] == "inspect":
            if argv[-1] not in self.containers:
                return 1, "", "No such object"
            return 0, json.dumps([self.containers[argv[-1]]]), ""
        if argv[1] == "kill":
            return (1, "", "daemon not responding") if self.kill_fails else (0, argv[2], "")
        if argv[1] == "ps":
            return 0, "\n".join(self.containers) + "\n", ""
        return 0, "", ""

    def state(self, name: str, **state: object) -> None:
        current = self.containers[name]["State"]
        assert isinstance(current, dict)
        current.update(state)


def _now(delta: timedelta = timedelta()) -> str:
    return (datetime.now(UTC) + delta).strftime("%Y-%m-%dT%H:%M:%S.%f000Z")


def _backend(
    tmp_path: Path, docker: Docker, *, env: str = "dev", paper: str = CANARY
) -> DevDockerBackend:
    async def paper_sha256(run_id: str) -> str | None:
        return paper if run_id == RUN else None

    return DevDockerBackend(
        env=env, canaries=frozenset({CANARY}), paper_sha256=paper_sha256, root=tmp_path,
        docker=docker,
    )  # fmt: skip


def _spec(kind: JobKind = JobKind.EXEC, **changes: object) -> JobSpec:
    fields: dict[str, object] = {
        "name": f"p2p-{kind.value}-{SLUG}-1",
        "kind": kind,
        "image_digest_ref": IMAGE,
        "platform": "cpu-d3",
        "preset": "4vcpu-16gb",
        "disk_gib": 30,
        "subnet_id": "local-exec",
        "volumes": [
            VolumeMount(source=f"p2p-run-{SLUG}", mount_path="/mnt/run", mode="rw"),
            VolumeMount(
                source="p2p-cache",
                mount_path="/mnt/cache",
                mode="rw" if kind is JobKind.PROVISION else "ro",
            ),
        ],
        "inject_files": {"/etc/p2p/manifest.json": b'{"schema": "p2p.manifest/v1"}'},
        "env": {"PYTHONUNBUFFERED": "1"},
        "preemptible": False,
        "watchdog_s": 600,
    }
    fields.update(changes)
    return JobSpec(**fields)  # type: ignore[arg-type]


def _run_argv(docker: Docker) -> list[str]:
    [run] = [c for c in docker.calls if c[1] == "run"]
    return run


def test_it_never_exists_outside_dev(tmp_path: Path) -> None:
    for env in ("prod", "test"):
        with pytest.raises(LocalTrustedRefused):
            _backend(tmp_path, Docker(), env=env)


async def test_only_canary_papers_get_a_job(tmp_path: Path) -> None:
    docker = Docker()
    backend = _backend(tmp_path, docker, paper="d" * 64)
    with pytest.raises(LocalTrustedRefused, match="canary"):
        await backend.submit(_spec())
    assert not [c for c in docker.calls if c[1] == "run"]


async def test_an_exec_job_runs_isolated_without_network(tmp_path: Path) -> None:
    docker = Docker()
    backend = _backend(tmp_path, docker)
    bucket = await backend.ensure_run_bucket(RUN)
    handle = await backend.submit(_spec())
    assert handle == JobHandle(name=f"p2p-exec-{SLUG}-1", nebius_id=f"cid-p2p-exec-{SLUG}-1")
    run = _run_argv(docker)
    for flag in ("--network=none", "--read-only", "--cap-drop=ALL",
                 "--security-opt=no-new-privileges", "--user=10001:10001",
                 "--memory=8g", "--memory-swap=8g", "--log-opt=max-size=10m"):  # fmt: skip
        assert flag in run, flag
    bucket_dir = (tmp_path / "buckets" / bucket).resolve()
    assert f"type=bind,source={bucket_dir},target=/mnt/run" in run
    assert f"type=bind,source={(tmp_path / 'cache').resolve()},target=/mnt/cache,readonly" in run
    assert any(a.endswith(",target=/etc/p2p/manifest.json,readonly") for a in run)
    assert run[-1] == "sha256:" + "a" * 64  # the local image by its id, never a tag
    assert "-e" in run and "PYTHONUNBUFFERED=1" in run


async def test_the_provisioner_gets_the_network_and_a_writable_cache(tmp_path: Path) -> None:
    docker = Docker()
    backend = _backend(tmp_path, docker)
    await backend.submit(_spec(JobKind.PROVISION, name=f"p2p-prov-{SLUG}-1"))
    run = _run_argv(docker)
    assert "--network=none" not in run
    assert f"type=bind,source={(tmp_path / 'cache').resolve()},target=/mnt/cache" in run


async def test_the_fetch_job_gets_the_network_and_no_cache(tmp_path: Path) -> None:
    docker = Docker()
    backend = _backend(tmp_path, docker)
    volumes = [VolumeMount(source=f"p2p-run-{SLUG}", mount_path="/mnt/run", mode="rw")]
    await backend.submit(_spec(JobKind.FETCH, volumes=volumes))
    run = _run_argv(docker)
    assert "--network=none" not in run  # it clones and downloads
    assert not any("target=/mnt/cache" in a for a in run)


async def test_a_fetch_job_with_the_cache_is_refused_even_unvalidated(tmp_path: Path) -> None:
    backend = _backend(tmp_path, Docker())
    volumes = [VolumeMount(source=f"p2p-run-{SLUG}", mount_path="/mnt/run", mode="rw")]
    planted = _spec(JobKind.FETCH, volumes=volumes).model_copy(
        update={
            "volumes": [
                *volumes,
                VolumeMount(source="p2p-cache", mount_path="/mnt/cache", mode="ro"),
            ]
        }
    )
    with pytest.raises(ValueError, match="cache"):
        await backend.submit(planted)


async def test_submit_is_get_by_name(tmp_path: Path) -> None:
    docker = Docker()
    backend = _backend(tmp_path, docker)
    first = await backend.submit(_spec())
    again = await backend.submit(_spec())
    assert first == again
    assert len([c for c in docker.calls if c[1] == "run"]) == 1


@pytest.mark.parametrize(
    "foreign",
    [
        {"Config": {"Labels": {}}},  # not ours
        {"Image": "sha256:" + "b" * 64},  # another image
        {"Config": {"Labels": {"p2p-dev": "1", "p2p-root": "another checkout"}}},
    ],
)
async def test_a_same_named_container_that_is_not_ours_is_never_adopted(
    tmp_path: Path, foreign: dict[str, object]
) -> None:
    docker = Docker()
    backend = _backend(tmp_path, docker)
    handle = await backend.submit(_spec())
    docker.containers[handle.name].update(foreign)
    with pytest.raises(RuntimeError, match="not created by this backend"):
        await backend.submit(_spec())


async def test_specs_built_without_validation_are_still_refused(tmp_path: Path) -> None:
    backend = _backend(tmp_path, Docker())
    other = _spec().model_copy(
        update={"volumes": [VolumeMount(source="p2p-run-other", mount_path="/mnt/run", mode="rw")]}
    )
    with pytest.raises(ValueError, match="mount"):
        await backend.submit(other)
    planted = _spec().model_copy(update={"inject_files": {"/etc/p2p/a,b": b"x"}})
    with pytest.raises(ValueError, match="inject"):
        await backend.submit(planted)


@pytest.mark.parametrize(
    ("state", "code", "expected"),
    [
        ("created", 0, JobState.PENDING),
        ("running", 0, JobState.RUNNING),
        ("exited", 0, JobState.SUCCEEDED),
        ("exited", 10, JobState.FAILED),
        ("dead", 137, JobState.FAILED),
    ],
)
async def test_container_states_map_to_job_states(
    tmp_path: Path, state: str, code: int, expected: JobState
) -> None:
    docker = Docker()
    backend = _backend(tmp_path, docker)
    handle = await backend.submit(_spec())
    docker.state(handle.name, Status=state, ExitCode=code)
    assert await backend.status(handle) is expected


async def test_a_job_past_its_watchdog_is_killed(tmp_path: Path) -> None:
    docker = Docker()
    backend = _backend(tmp_path, docker)
    handle = await backend.submit(_spec(watchdog_s=60))
    assert await backend.status(handle) is JobState.RUNNING
    docker.state(handle.name, StartedAt=_now(-timedelta(seconds=61)))
    assert await backend.status(handle) is JobState.FAILED
    assert ["docker", "kill", handle.name] in docker.calls


async def test_a_missing_container_is_unknown_and_cancel_kills(tmp_path: Path) -> None:
    docker = Docker()
    backend = _backend(tmp_path, docker)
    handle = await backend.submit(_spec())
    await backend.cancel(handle)
    assert ["docker", "kill", handle.name] in docker.calls
    assert await backend.status(JobHandle(name="p2p-exec-x-1", nebius_id="")) is JobState.UNKNOWN


async def test_jobs_are_listed_by_prefix(tmp_path: Path) -> None:
    docker = Docker()
    backend = _backend(tmp_path, docker)
    await backend.submit(_spec())
    listed = await backend.list_jobs(f"p2p-exec-{SLUG}-")
    assert [(h.name, s) for h, s, _ in listed] == [(f"p2p-exec-{SLUG}-1", JobState.RUNNING)]
    assert listed[0][2] == datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
    assert await backend.list_jobs("p2p-smoke-") == []


def test_local_images_are_run_by_id() -> None:
    assert local_image("local/p2p-runner-cpu@sha256:" + "b" * 64) == "sha256:" + "b" * 64
    with pytest.raises(ValueError):
        local_image("local/p2p-runner-cpu:dev")


async def test_run_storage_reads_writes_and_caps(tmp_path: Path) -> None:
    storage = LocalRunStorage(tmp_path)
    await storage.put_bytes(RUN, "results/1/results.json", b"{}")
    assert await storage.get_bytes(RUN, "results/1/results.json") == b"{}"
    await storage.put_bytes(RUN, "results/1/results.json", b"[]")  # replaced
    assert await storage.get_bytes(RUN, "results/1/results.json") == b"[]"
    assert await storage.get_bytes(RUN, "results/1/missing.json") is None
    assert await storage.get_bytes(RUN, "nothing/here.json") is None
    await storage.put_bytes(RUN, "big", b"x" * 100)
    with pytest.raises(ValueError):
        await storage.get_bytes(RUN, "big", max_bytes=10)
    await storage.delete(RUN, "big")
    await storage.delete(RUN, "big")  # already gone
    assert await storage.get_bytes(RUN, "big") is None
    assert [p.name for p in storage.path(RUN, "results/1").iterdir()] == ["results.json"]


@pytest.mark.parametrize("key", ["../outside", "/etc/passwd", "a/../../b", "", "a\\b", "a//b"])
async def test_run_storage_refuses_keys_outside_the_bucket(tmp_path: Path, key: str) -> None:
    with pytest.raises(ValueError):
        await LocalRunStorage(tmp_path).get_bytes(RUN, key)


async def test_a_directory_at_a_key_is_refused(tmp_path: Path) -> None:
    storage = LocalRunStorage(tmp_path)
    await storage.put_bytes(RUN, "results/1/stdout.log", b"ok")
    with pytest.raises(ValueError):
        await storage.get_bytes(RUN, "results/1")
    with pytest.raises(ValueError):
        await storage.get_bytes(RUN, "results/1/stdout.log/x")


async def test_a_file_swapped_after_the_check_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The portable guard (Windows has no dir_fd): what is opened must be what was checked."""
    storage = LocalRunStorage(tmp_path)
    await storage.put_bytes(RUN, "results/1/live.log", b"job output")
    target = storage.path(RUN, "results/1/live.log")
    real_open = os.open

    def swapping_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if Path(str(path)) == target:
            (target.parent / "other").write_bytes(b"host file")  # a new inode, then swapped in
            (target.parent / "other").replace(target)
        return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(dev_docker, "_DIR_FD", False)
    monkeypatch.setattr(dev_docker, "_open", swapping_open)
    with pytest.raises(ValueError, match="changed"):
        await storage.get_bytes(RUN, "results/1/live.log")


@pytest.mark.skipif(os.name != "nt", reason="junctions are Windows links")
async def test_a_junction_is_a_link_too(tmp_path: Path) -> None:
    import _winapi

    storage = LocalRunStorage(tmp_path)
    await storage.put_bytes(RUN, "results/1/stdout.log", b"ok")
    (tmp_path / "secret.txt").write_text("host file")
    _winapi.CreateJunction(str(tmp_path), str(storage.path(RUN, "linked")))  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match="link"):
        await storage.get_bytes(RUN, "linked/secret.txt")
    with pytest.raises(ValueError, match="link"):
        await storage.put_bytes(RUN, "linked/written.txt", b"x")
    with pytest.raises(ValueError, match="link"):
        await storage.delete(RUN, "linked/secret.txt")
    assert (tmp_path / "secret.txt").exists() and not (tmp_path / "written.txt").exists()


@POSIX
async def test_run_storage_never_follows_a_planted_link(tmp_path: Path) -> None:
    storage = LocalRunStorage(tmp_path)
    await storage.put_bytes(RUN, "results/1/stdout.log", b"ok")
    secret = tmp_path / "secret.txt"
    secret.write_text("host file")
    storage.path(RUN, "results/1/results.json").symlink_to(secret)
    with pytest.raises(ValueError, match="link"):
        await storage.get_bytes(RUN, "results/1/results.json")
    storage.path(RUN, "linked").symlink_to(tmp_path, target_is_directory=True)
    for key in ("linked/secret.txt", "linked/new.txt"):
        with pytest.raises(ValueError, match="link"):
            await storage.get_bytes(RUN, key)
        with pytest.raises(ValueError, match="link"):
            await storage.put_bytes(RUN, key, b"x")
        with pytest.raises(ValueError, match="link"):
            await storage.delete(RUN, key)
    assert secret.read_text() == "host file" and not (tmp_path / "new.txt").exists()


@POSIX
async def test_a_planted_temporary_link_is_never_written_through(tmp_path: Path) -> None:
    storage = LocalRunStorage(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("host file")
    folder = storage.path(RUN, "smoke/2")
    folder.mkdir(parents=True)
    for name in (f".workspace.tar.{os.getpid()}.tmp", ".workspace.tar.tmp", "workspace.tar~"):
        (folder / name).symlink_to(secret)
    await storage.put_bytes(RUN, "smoke/2/workspace.tar", b"tar bytes")
    assert secret.read_text() == "host file"
    assert await storage.get_bytes(RUN, "smoke/2/workspace.tar") == b"tar bytes"


@POSIX
async def test_a_planted_fifo_never_blocks(tmp_path: Path) -> None:
    storage = LocalRunStorage(tmp_path)
    await storage.put_bytes(RUN, "results/1/stdout.log", b"ok")
    os.mkfifo(storage.path(RUN, "results/1/live.log"))
    with pytest.raises(ValueError):
        await asyncio.wait_for(storage.get_bytes(RUN, "results/1/live.log"), timeout=5)


async def test_a_failed_watchdog_kill_never_reports_the_job_finished(tmp_path: Path) -> None:
    docker = Docker()
    backend = _backend(tmp_path, docker)
    handle = await backend.submit(_spec(watchdog_s=60))
    docker.state(handle.name, StartedAt=_now(-timedelta(seconds=61)))
    docker.kill_fails = True
    assert await backend.status(handle) is JobState.RUNNING  # polled again, killed again


@pytest.mark.parametrize("path", ["/etc/p2p/.", "/etc/p2p/.."])
async def test_dot_inject_paths_are_refused(tmp_path: Path, path: str) -> None:
    planted = _spec().model_copy(update={"inject_files": {path: b"x"}})
    with pytest.raises(ValueError, match="inject"):
        await _backend(tmp_path, Docker()).submit(planted)


@POSIX
def test_an_entry_gone_before_the_diagnosis_is_still_a_value_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = LocalRunStorage(tmp_path)
    folder = storage.path(RUN, "results")
    folder.parent.mkdir(parents=True)
    folder.write_bytes(b"a file where a directory should be")
    (folder.parent / "live.log").mkdir()

    def gone(*args: object, **kwargs: object) -> os.stat_result:
        raise FileNotFoundError

    before = len(list(Path("/proc/self/fd").iterdir()))
    monkeypatch.setattr(dev_docker, "_stat_at", gone)
    for key in ("results/1/live.log", "live.log"):
        with pytest.raises(ValueError):
            storage._read(RUN, key, 1024)
    assert len(list(Path("/proc/self/fd").iterdir())) == before  # no descriptor leaked


def test_docker_cli_times_out_without_a_container_name() -> None:
    import sys

    from app.execution.local_trusted import docker_cli

    argv = [sys.executable, "-c", "import time; time.sleep(30)"]
    code, _, err = asyncio.run(docker_cli(argv, 0.5))
    assert code == -1 and "timed out" in err


async def test_the_cache_mounts_only_at_its_path_even_unvalidated(tmp_path: Path) -> None:
    backend = _backend(tmp_path, Docker())
    moved = _spec(JobKind.PROVISION).model_copy(
        update={
            "volumes": [
                VolumeMount(source=f"p2p-run-{SLUG}", mount_path="/mnt/run", mode="rw"),
                VolumeMount(source="p2p-cache", mount_path="/mnt/elsewhere", mode="rw"),
            ]
        }
    )
    with pytest.raises(ValueError, match="cache"):
        await backend.submit(moved)

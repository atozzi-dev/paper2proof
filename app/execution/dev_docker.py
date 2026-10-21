"""DevDockerBackend and LocalRunStorage: the dev end-to-end (A-25) runs the real job images in
local docker, with per-run "buckets" as directories under `root`.

Like LocalTrustedExecutor, this is the one exception to invariant 1: it refuses to exist unless
`P2P_ENV=dev`, and refuses every job of a run whose paper's sha256 is not in
`config/canary_allowlist.yaml`. Containers keep the job's isolation: non-root (10001), no
capabilities, no new privileges, a read-only root with private scratch space, capped memory and
logs, a host-side kill at the watchdog, no network except for the provision and fetch jobs
(the provisioner image, our code; locally their egress is not allowlisted as the gateway does
in the cloud), and only the run's own directory and the cache mounted: the cache read-only
except for the provision job, never for the fetch job.
"""

import asyncio
import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from app.execution.backend import CACHE_BUCKET, CACHE_MOUNT, JobHandle, JobKind, JobSpec, JobState
from app.execution.local_trusted import DockerRunner, LocalTrustedRefused, PaperHash, docker_cli
from app.execution.naming import bucket_name
from app.execution.run_storage import MAX_READ_BYTES

_DIGEST = re.compile(r"@(sha256:[0-9a-f]{64})$")
_JOB_NAME = re.compile(r"^p2p-(prov|fetch|exec|smoke)-([0-9a-z]{26})-[0-9]{1,4}$")
_MOUNT_PATH = re.compile(r"^/mnt/[a-z]{1,32}$")
_INJECT_PATH = re.compile(r"^/etc/p2p/(?!\.\.?$)[a-z0-9._-]{1,64}$")
_STATES = {
    "created": JobState.PENDING,
    "restarting": JobState.STARTING,
    "running": JobState.RUNNING,
}
DOCKER_TIMEOUT_S = 120.0
LABEL = "p2p-dev"


def local_image(ref: str) -> str:
    """A local build has no registry digest: dev settings carry `<name>@sha256:<image id>` and
    the container runs from that image id, never from a tag."""
    match = _DIGEST.search(ref)
    if match is None:
        raise ValueError("job images are referenced by digest")
    return match.group(1)


def _when(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)[:19]).replace(tzinfo=UTC)
    except ValueError:
        return None


class DevDockerBackend:
    def __init__(
        self,
        *,
        env: str,
        canaries: frozenset[str],
        paper_sha256: PaperHash,
        root: Path,
        docker: DockerRunner = docker_cli,
    ) -> None:
        if env != "dev":
            raise LocalTrustedRefused("the local docker job backend runs only with P2P_ENV=dev")
        self._canaries = canaries
        self._paper_sha256 = paper_sha256
        self._root = root.resolve()
        if any(c in str(self._root) for c in ',"\n'):
            raise ValueError(f"{self._root}: docker --mount cannot take this path")
        self._root_label = hashlib.sha256(str(self._root).encode()).hexdigest()[:16]
        self._docker = docker

    def _bucket(self, name: str) -> Path:
        return self._root / "buckets" / name

    async def _cli(self, *argv: str) -> tuple[int, str, str]:
        return await self._docker(["docker", *argv], DOCKER_TIMEOUT_S)

    async def ensure_run_bucket(self, run_id: str) -> str:
        name = bucket_name(run_id)
        self._bucket(name).mkdir(parents=True, exist_ok=True)
        return name

    async def delete_run_bucket(self, run_id: str) -> None:
        shutil.rmtree(self._bucket(bucket_name(run_id)), ignore_errors=True)

    async def list_run_buckets(self) -> list[tuple[str, datetime]]:
        folder = self._root / "buckets"
        if not folder.is_dir():
            return []
        return [
            (p.name, datetime.fromtimestamp(p.stat().st_mtime, UTC))
            for p in sorted(folder.iterdir())
            if p.is_dir()
        ]

    async def _inspect(self, name: str) -> dict[str, object] | None:
        code, out, _ = await self._cli("inspect", "--type=container", name)
        if code != 0:
            return None
        try:
            data = json.loads(out)
        except ValueError:
            return None
        return data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else None

    def _labels(self, info: dict[str, object]) -> dict[str, str]:
        config = info.get("Config")
        labels = config.get("Labels") if isinstance(config, dict) else None
        return {str(k): str(v) for k, v in labels.items()} if isinstance(labels, dict) else {}

    def _ours(self, info: dict[str, object]) -> bool:
        labels = self._labels(info)
        return labels.get(LABEL) == "1" and labels.get("p2p-root") == self._root_label

    def _checked(self, spec: JobSpec) -> str:
        """Again here, not only in JobSpec's validators: a spec built with model_construct or
        model_copy skips them, and this backend runs on the developer's machine."""
        match = _JOB_NAME.fullmatch(spec.name)
        if match is None or match.group(1) != spec.kind.value:
            raise ValueError(f"invalid job name: {spec.name!r}")
        run_id = "r_" + match.group(2)
        for volume in spec.volumes:
            cache = volume.source == CACHE_BUCKET
            if not (cache or volume.source == bucket_name(run_id)):
                raise ValueError(f"jobs may mount only their run's bucket and the cache: {volume}")
            if cache and volume.mount_path != CACHE_MOUNT:  # the provisioner's phases rely on it
                raise ValueError(f"the cache mounts only at {CACHE_MOUNT}")
            if cache and spec.kind is JobKind.FETCH:
                raise ValueError("the fetch job never mounts the cache")
            if cache and volume.mode == "rw" and spec.kind is not JobKind.PROVISION:
                raise ValueError("only the provisioner may mount the cache writable")
            if not _MOUNT_PATH.fullmatch(volume.mount_path):
                raise ValueError(f"invalid mount path: {volume.mount_path!r}")
        for path in spec.inject_files:
            if not _INJECT_PATH.fullmatch(path):
                raise ValueError(f"invalid inject path: {path!r}")
        return run_id

    async def submit(self, spec: JobSpec) -> JobHandle:
        run_id = self._checked(spec)
        digest = await self._paper_sha256(run_id)
        if digest is None or digest.lower() not in self._canaries:
            raise LocalTrustedRefused("not a canary paper: its jobs never run on this machine")
        image = local_image(spec.image_digest_ref)
        existing = await self._inspect(spec.name)
        if existing is not None:  # get-by-name: a resumed step finds its container
            if not self._ours(existing) or existing.get("Image") != image:
                raise RuntimeError(f"{spec.name} exists but was not created by this backend")
            return JobHandle(name=spec.name, nebius_id=str(existing.get("Id", "")))
        job = self._root / "jobs" / spec.name
        shutil.rmtree(job, ignore_errors=True)
        (job / "work").mkdir(parents=True)
        (job / "home").mkdir()
        argv = [
            "run", "-d", f"--name={spec.name}", f"--label={LABEL}=1",
            f"--label=p2p-root={self._root_label}", f"--label=p2p-watchdog={spec.watchdog_s}",
            "--user=10001:10001", "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--read-only", "--tmpfs=/tmp:rw,size=1g", "--pids-limit=4096",
            "--memory=8g", "--memory-swap=8g", "--cpus=2",
            # the log caps need json-file: a daemon defaulting to journald refuses max-file
            "--log-driver=json-file", "--log-opt=max-size=10m", "--log-opt=max-file=2",
        ]  # fmt: skip
        mounts = [(job / "work", "/work", "rw"), (job / "home", "/home/p2p", "rw")]
        if spec.kind not in (JobKind.PROVISION, JobKind.FETCH):  # they download (PyPI, repo, HF)
            argv.append("--network=none")
        for volume in spec.volumes:
            cache = volume.source == CACHE_BUCKET
            source = self._root / "cache" if cache else self._bucket(volume.source)
            source.mkdir(parents=True, exist_ok=True)
            mounts.append((source, volume.mount_path, volume.mode))
        for index, (path, content) in enumerate(sorted(spec.inject_files.items())):
            local = job / f"inject-{index}"
            local.write_bytes(content)
            mounts.append((local, path, "ro"))
        for source, target, mode in mounts:
            readonly = ",readonly" if mode == "ro" else ""
            argv += ["--mount", f"type=bind,source={source.resolve()},target={target}{readonly}"]
        for name, value in sorted(spec.env.items()):
            argv += ["-e", f"{name}={value}"]
        argv.append(image)
        code, out, err = await self._cli(*argv)
        if code != 0:
            raise RuntimeError(f"docker run failed for {spec.name}: {err.strip()[-500:]}")
        return JobHandle(name=spec.name, nebius_id=out.strip())

    async def status(self, handle: JobHandle) -> JobState:
        info = await self._inspect(handle.name)
        state = info.get("State") if info is not None else None
        if info is None or not isinstance(state, dict):
            return JobState.UNKNOWN
        status = str(state.get("Status", ""))
        if status == "running" and self._past_watchdog(info, state):
            # the host-side watchdog: nothing runs past its budget, and a job is over only
            # once its container really is (a failed kill is retried at the next poll)
            code, _, _ = await self._cli("kill", handle.name)
            return JobState.FAILED if code == 0 else JobState.RUNNING
        if status in _STATES:
            return _STATES[status]
        if status in ("exited", "dead"):
            return JobState.SUCCEEDED if state.get("ExitCode") == 0 else JobState.FAILED
        return JobState.UNKNOWN

    def _past_watchdog(self, info: dict[str, object], state: dict[str, object]) -> bool:
        started = _when(state.get("StartedAt"))
        watchdog = self._labels(info).get("p2p-watchdog", "")
        if started is None or not watchdog.isdigit():
            return False
        return (datetime.now(UTC) - started).total_seconds() > int(watchdog)

    async def cancel(self, handle: JobHandle) -> None:
        await self._cli("kill", handle.name)

    async def ensure_gateway_up(self) -> None:
        return None  # no gateway locally: the provisioner uses docker's default network

    async def gateway_idle_check(self, idle_minutes: int = 30) -> bool:
        return False

    async def list_jobs(self, prefix: str) -> list[tuple[JobHandle, JobState, datetime]]:
        code, out, _ = await self._cli(
            "ps", "-a", "--filter", f"label={LABEL}=1", "--format", "{{.Names}}"
        )
        names = [n for n in out.split() if n.startswith(prefix)] if code == 0 else []
        listed = []
        for name in sorted(names):
            info = await self._inspect(name)
            if info is None or not self._ours(info):
                continue
            handle = JobHandle(name=name, nebius_id=str(info.get("Id", "")))
            when = _when(info.get("Created")) or datetime.now(UTC)
            listed.append((handle, await self.status(handle), when))
        return listed


# LocalRunStorage. Jobs (untrusted code) write these directories while the control plane reads
# them, so every access refuses links (and Windows junctions), FIFOs and anything not a regular
# file. On POSIX each component is opened relative to its parent with O_NOFOLLOW, so nothing a
# job swaps in between a check and a use is ever followed. Windows has no dir_fd: there the
# chain is checked, and a read must open the very file that was checked (same volume and index).

_DIR_FD = all(f in os.supports_dir_fd for f in (os.open, os.mkdir, os.rename, os.unlink))
_open: Callable[..., int] = os.open  # patched by the race test
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_BINARY = getattr(os, "O_BINARY", 0)
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _key_parts(key: str) -> tuple[str, ...]:
    parts = key.split("/")
    if not key or "\\" in key or "\x00" in key or any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"invalid object key: {key!r}")
    return tuple(parts)


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _REPARSE)


def _lstat(path: Path, key: str) -> os.stat_result | None:
    try:
        info = path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        raise ValueError(f"{key} is unreadable") from exc
    if _is_link(info):
        raise ValueError(f"{key} goes through a link: refused")
    return info


def _stat_at(name: str, fd: int) -> os.stat_result:
    return os.stat(name, dir_fd=fd, follow_symlinks=False)


def _refused(name: str, fd: int, key: str) -> ValueError:
    """Why opening `name` failed; the job may have removed it meanwhile."""
    try:
        link = _is_link(_stat_at(name, fd))
    except OSError:
        link = False
    return ValueError(f"{key} {'goes through a link: refused' if link else 'is unreadable'}")


def _read_fd(descriptor: int, key: str, max_bytes: int, expected: os.stat_result | None) -> bytes:
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode):  # before fdopen, which refuses a directory itself
        os.close(descriptor)
        raise ValueError(f"{key} is not a regular file")
    with os.fdopen(descriptor, "rb") as handle:
        if expected is not None and (info.st_dev, info.st_ino) != (
            expected.st_dev,
            expected.st_ino,
        ):
            raise ValueError(f"{key} changed while it was read: refused")
        if info.st_size > max_bytes:
            raise ValueError(f"{key} is larger than {max_bytes} bytes")
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"{key} is larger than {max_bytes} bytes")
    return data


class LocalRunStorage:
    """RunStorage over `<root>/buckets/p2p-run-<slug>/`."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def _base(self, run_id: str) -> Path:
        return self._root / "buckets" / bucket_name(run_id)

    def path(self, run_id: str, key: str) -> Path:
        return self._base(run_id).joinpath(*_key_parts(key))

    async def get_bytes(
        self, run_id: str, key: str, *, max_bytes: int = MAX_READ_BYTES
    ) -> bytes | None:
        return await asyncio.to_thread(self._read, run_id, key, max_bytes)

    async def put_bytes(self, run_id: str, key: str, data: bytes) -> None:
        await asyncio.to_thread(self._write, run_id, key, data)

    async def delete(self, run_id: str, key: str) -> None:
        await asyncio.to_thread(self._delete, run_id, key)

    # POSIX: a directory descriptor per component, never following a link

    def _parent_fd(self, run_id: str, parts: tuple[str, ...], key: str, create: bool) -> int | None:
        base = self._base(run_id)
        if create:
            base.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(base, os.O_RDONLY | _DIRECTORY)
        except FileNotFoundError:
            return None
        for part in parts[:-1]:
            try:
                if create:
                    with contextlib.suppress(FileExistsError):
                        os.mkdir(part, dir_fd=fd)
                child = os.open(part, os.O_RDONLY | _DIRECTORY | _NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise _refused(part, fd, key) from exc
            finally:
                os.close(fd)
            fd = child
        return fd

    def _read(self, run_id: str, key: str, max_bytes: int) -> bytes | None:
        parts = _key_parts(key)
        if not _DIR_FD:
            return self._read_portable(run_id, parts, key, max_bytes)
        parent = self._parent_fd(run_id, parts, key, create=False)
        if parent is None:
            return None
        try:
            descriptor = _open(parts[-1], os.O_RDONLY | _NOFOLLOW | _NONBLOCK, dir_fd=parent)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise _refused(parts[-1], parent, key) from exc
        finally:
            os.close(parent)
        return _read_fd(descriptor, key, max_bytes, None)

    def _write(self, run_id: str, key: str, data: bytes) -> None:
        parts = _key_parts(key)
        if not _DIR_FD:
            return self._write_portable(run_id, parts, key, data)
        parent = self._parent_fd(run_id, parts, key, create=True)
        if parent is None:  # pragma: no cover - created just above
            raise ValueError(f"{key} cannot be written")
        temporary = f".p2p-{secrets.token_hex(8)}"  # random and O_EXCL: nothing planted to follow
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW
            with os.fdopen(os.open(temporary, flags, 0o644, dir_fd=parent), "wb") as handle:
                handle.write(data)
            try:
                os.rename(temporary, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)
            except OSError as exc:  # a directory at the key
                os.unlink(temporary, dir_fd=parent)
                raise ValueError(f"{key} cannot be written") from exc
        finally:
            os.close(parent)
        return None

    def _delete(self, run_id: str, key: str) -> None:
        parts = _key_parts(key)
        if not _DIR_FD:
            return self._delete_portable(run_id, parts, key)
        parent = self._parent_fd(run_id, parts, key, create=False)
        if parent is None:
            return None
        try:
            os.unlink(parts[-1], dir_fd=parent)  # removes a planted link itself, never its target
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ValueError(f"{key} cannot be deleted") from exc
        finally:
            os.close(parent)
        return None

    # Windows: the checked chain, then the very file that was checked

    def _chain(self, run_id: str, parts: tuple[str, ...], key: str, create: bool) -> Path | None:
        """The parent directory of the key, every component checked; None if it is missing."""
        current = self._base(run_id)
        if create:
            current.mkdir(parents=True, exist_ok=True)
        for part in parts[:-1]:
            current = current / part
            info = _lstat(current, key)
            if info is None and create:
                current.mkdir(exist_ok=True)
                info = _lstat(current, key)
            if info is None:
                return None
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError(f"{key} goes through a file")
        return current

    def _read_portable(
        self, run_id: str, parts: tuple[str, ...], key: str, max_bytes: int
    ) -> bytes | None:
        parent = self._chain(run_id, parts, key, create=False)
        if parent is None:
            return None
        path = parent / parts[-1]
        checked = _lstat(path, key)
        if checked is None:
            return None
        if not stat.S_ISREG(checked.st_mode):
            raise ValueError(f"{key} is not a regular file")
        try:
            descriptor = _open(path, os.O_RDONLY | _BINARY | _NOFOLLOW | _NONBLOCK)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ValueError(f"{key} is unreadable") from exc
        return _read_fd(descriptor, key, max_bytes, checked)

    def _write_portable(self, run_id: str, parts: tuple[str, ...], key: str, data: bytes) -> None:
        parent = self._chain(run_id, parts, key, create=True)
        if parent is None:  # pragma: no cover - created just above
            raise ValueError(f"{key} cannot be written")
        temporary = parent / f".p2p-{secrets.token_hex(8)}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _BINARY | _NOFOLLOW
        try:
            with os.fdopen(os.open(temporary, flags, 0o644), "wb") as handle:
                handle.write(data)
                written = os.fstat(handle.fileno())
            self._chain(run_id, parts, key, create=False)  # still no link on the way
            placed = _lstat(temporary, key)  # and the file is where it was checked
            if placed is None or (placed.st_dev, placed.st_ino) != (
                written.st_dev,
                written.st_ino,
            ):
                raise ValueError(f"{key} changed while it was written: refused")
            existing = _lstat(parent / parts[-1], key)
            if existing is not None and not stat.S_ISREG(existing.st_mode):
                raise ValueError(f"{key} cannot be written")
            temporary.replace(parent / parts[-1])
        except OSError as exc:
            raise ValueError(f"{key} cannot be written") from exc
        finally:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)

    def _delete_portable(self, run_id: str, parts: tuple[str, ...], key: str) -> None:
        # Known Windows-only gap: a junction swapped in after the check could make this delete
        # a same-named file elsewhere (it needs a job able to create junctions through the
        # Docker Desktop bind mount, which POSIX hosts and the cloud never allow).
        parent = self._chain(run_id, parts, key, create=False)
        if parent is None:
            return
        path = parent / parts[-1]
        info = _lstat(path, key)
        if info is None:
            return
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"{key} cannot be deleted")
        path.unlink(missing_ok=True)

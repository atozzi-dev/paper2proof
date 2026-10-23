"""LocalTrustedExecutor: dev only, canary papers only, locked-down docker (never run here)."""

import json
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from app.execution.local_trusted import (
    LocalTrustedExecutor,
    LocalTrustedRefused,
    PaperRef,
    is_canary,
    store_paper_lookup,
)
from app.execution.smoke import SmokeContractError
from app.storage.store import Store

RUN = "r_01jb7z00000000000000000000"
CANARY = "a" * 64
IMAGE = "cr.eu-north1.nebius.cloud/reg/p2p-runner-cpu@sha256:" + "c" * 64


class FakeDocker:
    def __init__(self, exit_code: int = 0, metrics: dict[str, float] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.exit_code = exit_code
        self.metrics = metrics

    async def __call__(self, argv: Sequence[str], timeout_s: float) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        workspace = next(a for a in argv if a.endswith(":/workspace:rw")).split(":/workspace")[0]
        if self.metrics is not None:
            out = Path(workspace) / "out"
            out.mkdir(parents=True, exist_ok=True)
            (out / "metrics.json").write_text(json.dumps(self.metrics))
        return self.exit_code, "stdout here", "stderr here"


async def _paper(run_id: str) -> PaperRef | None:
    return PaperRef(arxiv_id="0000.00001", sha256=CANARY)


def _executor(tmp_path: Path, docker: FakeDocker, env: str = "dev") -> LocalTrustedExecutor:
    return LocalTrustedExecutor(
        env=env,
        canaries={CANARY: "0000.00001"},
        paper=_paper,
        image=IMAGE,
        work_root=tmp_path,
        docker=docker,
    )


@pytest.mark.parametrize("env", ["prod", "test"])
def test_refuses_outside_dev(tmp_path: Path, env: str) -> None:
    with pytest.raises(LocalTrustedRefused):
        _executor(tmp_path, FakeDocker(), env=env)


async def test_refuses_a_paper_that_is_not_a_canary(tmp_path: Path) -> None:
    async def other(run_id: str) -> PaperRef | None:
        return PaperRef(arxiv_id="0000.00001", sha256="b" * 64)

    executor = LocalTrustedExecutor(
        env="dev",
        canaries={CANARY: "0000.00001"},
        paper=other,
        image=IMAGE,
        work_root=tmp_path,
        docker=FakeDocker(),
    )
    with pytest.raises(LocalTrustedRefused):
        await executor.prepare(RUN, {"train.py": b"print(1)"})

    async def unknown(run_id: str) -> PaperRef | None:
        return None

    executor._paper = unknown
    with pytest.raises(LocalTrustedRefused):
        await executor.prepare(RUN, {})


async def test_runs_in_a_locked_down_container(tmp_path: Path) -> None:
    docker = FakeDocker(metrics={"acc": 0.5})
    executor = _executor(tmp_path, docker)
    env = await executor.prepare(RUN, {"train.py": b"print(1)"})
    result = await executor.run(env, ["python", "train.py", "--out", "out", "--smoke"], {}, 60)
    assert result.exit_code == 0
    assert result.metrics == {"acc": 0.5}
    (argv,) = docker.calls
    assert argv[:2] == ["docker", "run"]
    for flag in [
        "--network=none",
        "--read-only",
        "--pids-limit=256",
        "--memory=4g",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--user=10001:10001",
        "--rm",
    ]:
        assert flag in argv
    assert IMAGE in argv
    assert argv[-4:] == ["train.py", "--out", "out", "--smoke"]


async def test_workspace_files_are_confined(tmp_path: Path) -> None:
    executor = _executor(tmp_path, FakeDocker())
    env = await executor.prepare(RUN, {})
    with pytest.raises(SmokeContractError):
        await executor.run(env, ["python", "train.py"], {"../escape.py": b"x"}, 60)
    assert not (tmp_path.parent / "escape.py").exists()


def test_image_must_be_pinned_by_digest(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        LocalTrustedExecutor(
            env="dev",
            canaries={CANARY: "0000.00001"},
            paper=_paper,
            image="p2p-runner-cpu:latest",
            work_root=tmp_path,
            docker=FakeDocker(),
        )


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
async def test_a_symlinked_metrics_file_is_not_followed(tmp_path: Path) -> None:
    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps({"acc": 0.123}))

    class LinkingDocker(FakeDocker):
        async def __call__(self, argv: Sequence[str], timeout_s: float) -> tuple[int, str, str]:
            workspace = next(a for a in argv if a.endswith(":/workspace:rw")).split(":/workspace")[
                0
            ]
            (Path(workspace) / "out").mkdir(parents=True, exist_ok=True)
            (Path(workspace) / "out" / "metrics.json").symlink_to(secret)
            return 0, "", ""

    executor = _executor(tmp_path / "root", LinkingDocker())
    env = await executor.prepare(RUN, {"train.py": b"x"})
    result = await executor.run(env, ["python", "train.py"], {}, 60)
    assert result.metrics is None


async def test_run_requires_a_canary_approved_prepare(tmp_path: Path) -> None:
    executor = _executor(tmp_path, FakeDocker())
    with pytest.raises(LocalTrustedRefused):
        await executor.run(RUN, ["python", "train.py"], {"train.py": b"x"}, 60)


async def test_the_image_entrypoint_is_overridden(tmp_path: Path) -> None:
    docker = FakeDocker()
    executor = _executor(tmp_path, docker)
    env = await executor.prepare(RUN, {"train.py": b"x"})
    await executor.run(env, ["python", "train.py"], {}, 60)
    (argv,) = docker.calls
    assert "--entrypoint=python" in argv
    assert argv[argv.index(IMAGE) + 1 :] == ["train.py"]


async def test_refuses_a_canary_hash_under_another_arxiv_id(tmp_path: Path) -> None:
    """The bytes of a canary submitted as a real arXiv ID never get a local job (A-38)."""

    async def renamed(run_id: str) -> PaperRef | None:
        return PaperRef(arxiv_id="2106.09685", sha256=CANARY)

    executor = LocalTrustedExecutor(
        env="dev",
        canaries={CANARY: "0000.00001"},
        paper=renamed,
        image=IMAGE,
        work_root=tmp_path,
        docker=FakeDocker(),
    )
    with pytest.raises(LocalTrustedRefused):
        await executor.prepare(RUN, {"train.py": b"print(1)"})


async def test_the_store_lookup_gives_the_submitted_id_and_the_ingested_hash(
    store: Store,
) -> None:
    """The one lookup both local executors use (app/main.py, smoke_factory): the gate's input
    comes from the run as submitted and as ingested, never from the allowlist."""
    runs = {
        "r_01jb7z00000000000000000001": ("0000.00001", CANARY),  # a canary: accepted
        "r_01jb7z00000000000000000002": ("2106.09685", CANARY),  # its bytes, another ID
        "r_01jb7z00000000000000000003": ("0000.00001", None),  # not ingested yet
    }
    for run_id, (arxiv_id, digest) in runs.items():
        await store.create_run(run_id, arxiv_id, created_by=None, claim_hint=None, budget={})
        if digest is not None:
            await store.set_paper_sha256(run_id, digest)
    lookup = store_paper_lookup(store)
    canaries = {CANARY: "0000.00001"}
    verdicts = [is_canary(canaries, await lookup(run_id)) for run_id in runs]
    assert verdicts == [True, False, False]
    assert await lookup("r_01jb7z00000000000000000009") is None

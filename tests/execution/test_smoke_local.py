"""LocalTrustedExecutor: dev only, canary papers only, locked-down docker (never run here)."""

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from app.execution.local_trusted import LocalTrustedExecutor, LocalTrustedRefused
from app.execution.smoke import SmokeContractError

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


async def _hash(run_id: str) -> str | None:
    return CANARY


def _executor(tmp_path: Path, docker: FakeDocker, env: str = "dev") -> LocalTrustedExecutor:
    return LocalTrustedExecutor(
        env=env,
        canaries=frozenset({CANARY}),
        paper_sha256=_hash,
        image=IMAGE,
        work_root=tmp_path,
        docker=docker,
    )


@pytest.mark.parametrize("env", ["prod", "test"])
def test_refuses_outside_dev(tmp_path: Path, env: str) -> None:
    with pytest.raises(LocalTrustedRefused):
        _executor(tmp_path, FakeDocker(), env=env)


async def test_refuses_a_paper_that_is_not_a_canary(tmp_path: Path) -> None:
    async def other(run_id: str) -> str | None:
        return "b" * 64

    executor = LocalTrustedExecutor(
        env="dev",
        canaries=frozenset({CANARY}),
        paper_sha256=other,
        image=IMAGE,
        work_root=tmp_path,
        docker=FakeDocker(),
    )
    with pytest.raises(LocalTrustedRefused):
        await executor.prepare(RUN, {"train.py": b"print(1)"})

    async def unknown(run_id: str) -> str | None:
        return None

    executor._paper_sha256 = unknown
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
            canaries=frozenset({CANARY}),
            paper_sha256=_hash,
            image="p2p-runner-cpu:latest",
            work_root=tmp_path,
            docker=FakeDocker(),
        )

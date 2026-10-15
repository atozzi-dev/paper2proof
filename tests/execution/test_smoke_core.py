"""Smoke executor contract: workspace validation, result parsing and the fallback wrapper."""

import contextlib

import pytest

from app.execution.smoke import (
    MAX_BRANCHES,
    FakeSmokeExecutor,
    FallbackSmokeExecutor,
    SmokeContractError,
    SmokeResult,
    SmokeUnavailable,
    parse_smoke_result,
    validate_argv,
    validate_workspace,
)

OK = SmokeResult(exit_code=0, stdout="ok", stderr="", duration_s=1.0, metrics={"acc": 0.5})


@pytest.mark.parametrize(
    "path",
    ["../x.py", "/etc/passwd", "a/../../b.py", "C:/x.py", "a\\b.py", "", "a\x00.py", "./a.py"],
)
def test_workspace_paths_must_stay_inside(path: str) -> None:
    with pytest.raises(SmokeContractError):
        validate_workspace({path: b"x"})


def test_workspace_size_caps() -> None:
    validate_workspace({"train.py": b"x" * 1000, "pkg/model.py": b"y"})
    with pytest.raises(SmokeContractError):
        validate_workspace({f"f{i}.py": b"x" for i in range(201)})
    with pytest.raises(SmokeContractError):
        validate_workspace({"big.py": b"x" * (2 * 1024 * 1024 + 1)})


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["bash", "-c", "id"],
        ["python", "-c", "print(1)"],
        ["python", "../train.py"],
        ["python", "train.sh"],
        ["python", "train.py", "a\x00b"],
        ["python", "train.py", *["--x"] * 40],
    ],
)
def test_argv_is_python_running_a_workspace_script(argv: list[str]) -> None:
    with pytest.raises(SmokeContractError):
        validate_argv(argv)


def test_valid_argv() -> None:
    assert validate_argv(["python", "train.py", "--seed", "0", "--out", "out", "--smoke"]) == [
        "python", "train.py", "--seed", "0", "--out", "out", "--smoke",
    ]  # fmt: skip


def test_untrusted_results_are_validated_and_capped() -> None:
    raw = {
        "exit_code": 1,
        "stdout": "x" * 100_000,
        "stderr": "Traceback",
        "duration_s": 3.5,
        "metrics": {"acc": 0.9, "bad": float("nan"), "text": "no", "baseline/acc": 0.8},
    }
    result = parse_smoke_result(raw)
    assert result.exit_code == 1
    assert len(result.stdout) <= 64 * 1024
    assert result.metrics == {"acc": 0.9, "baseline/acc": 0.8}
    for broken in [None, [], {"exit_code": "0"}, {"exit_code": True}]:
        with pytest.raises(SmokeContractError):
            parse_smoke_result(broken)


class _Broken(FakeSmokeExecutor):
    async def run(
        self, env_id: str, argv: list[str], files: dict[str, bytes], timeout_s: int
    ) -> SmokeResult:
        self.calls += 1
        raise SmokeUnavailable("sandboxes down")


async def test_fallback_after_three_consecutive_errors() -> None:
    primary, fallback = _Broken([]), FakeSmokeExecutor([OK, OK])
    smoke = FallbackSmokeExecutor(primary, fallback, threshold=3)
    env = await smoke.prepare("r_01jb7z00000000000000000000", {"train.py": b"x"})
    argv = ["python", "train.py"]
    for _ in range(3):
        with pytest.raises(SmokeUnavailable):
            await smoke.run(env, argv, {}, 60)
    assert await smoke.run(env, argv, {}, 60) == OK
    assert primary.calls == 3
    assert fallback.prepared == ["r_01jb7z00000000000000000000"]  # prepared lazily on switch
    assert smoke.active is fallback


async def test_a_success_resets_the_error_count() -> None:
    class Flaky(FakeSmokeExecutor):
        async def run(
            self, env_id: str, argv: list[str], files: dict[str, bytes], timeout_s: int
        ) -> SmokeResult:
            self.calls += 1
            if self.calls % 3 == 0:
                return OK
            raise SmokeUnavailable("blip")

    primary = Flaky([])
    smoke = FallbackSmokeExecutor(primary, FakeSmokeExecutor([]), threshold=3)
    env = await smoke.prepare("r_01jb7z00000000000000000000", {})
    for _ in range(6):
        with contextlib.suppress(SmokeUnavailable):
            await smoke.run(env, ["python", "train.py"], {}, 60)
    assert smoke.active is primary


async def test_a_failing_prepare_counts_and_falls_back() -> None:
    class NoPrepare(FakeSmokeExecutor):
        async def prepare(self, run_id: str, files: dict[str, bytes]) -> str:
            raise SmokeUnavailable("pending")

    fallback = FakeSmokeExecutor([OK])
    smoke = FallbackSmokeExecutor(NoPrepare([]), fallback, threshold=1)
    env = await smoke.prepare("r_01jb7z00000000000000000000", {})
    assert await smoke.run(env, ["python", "train.py"], {}, 60) == OK


async def test_branches_are_capped() -> None:
    fake = FakeSmokeExecutor([OK] * 3)
    env = await fake.prepare("r_01jb7z00000000000000000000", {})
    assert len(await fake.run_branches(env, [{}, {}], ["python", "t.py"], 60)) == MAX_BRANCHES
    with pytest.raises(SmokeContractError):
        await fake.run_branches(env, [{}, {}, {}], ["python", "t.py"], 60)

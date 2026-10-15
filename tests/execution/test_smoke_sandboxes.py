"""SandboxesSmokeExecutor against a respx mock of the Sandboxes REST API (shape from spike S-3)."""

import base64
import io
import json
import tarfile
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from app.execution.sandboxes import SANDBOXES_API, SandboxesSmokeExecutor
from app.execution.smoke import SmokeContractError, SmokeUnavailable
from app.orchestrator.budget import Budget, SpendKind, load_budget_limits
from app.storage.store import Store
from tests.conftest import ROOT

RUN = "r_01jb7z00000000000000000000"
BASE_IMAGE = "img-runner-cpu-0001"


def _stream(text: str) -> dict[str, str]:
    return {"encoding": "base64", "value": base64.b64encode(text.encode()).decode()}


@pytest.fixture(autouse=True)
async def _run(store: Store) -> None:
    await store.create_run(RUN, "2106.09685", created_by=None, claim_hint=None, budget={})


def _executor(store: Store, image: str | None = BASE_IMAGE) -> SandboxesSmokeExecutor:
    async def no_sleep(seconds: float) -> None:
        return None

    return SandboxesSmokeExecutor(
        api_key=SecretStr("test-key-not-real"),
        project_id="project-e00test",
        base_image=image,
        budget=Budget(store, load_budget_limits(ROOT / "config" / "budgets.yaml"), env="test"),
        usd_per_hour=0.1,
        sleep=no_sleep,
    )


def _mock_run(stdout_after_marker: str, exit_code: int = 0) -> list[dict[str, Any]]:
    bodies: list[dict[str, Any]] = []

    def spawn(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        marker = body["command"].split("echo ")[-1].split()[0].strip("'\"")
        stdout = f"training...\n{marker}\n{stdout_after_marker}"
        respx.get(f"{SANDBOXES_API}/v1/operations/op-{len(bodies)}").mock(
            side_effect=[
                httpx.Response(200, json={"status": "EXECUTING"}),
                httpx.Response(
                    200,
                    json={
                        "status": "SUCCEEDED",
                        "metadata": {
                            "result": {
                                "state": {"exit_code": exit_code},
                                "stdout": _stream(stdout),
                                "stderr": _stream("warn"),
                            }
                        },
                    },
                ),
            ]
        )
        return httpx.Response(200, json={"uuid": f"op-{len(bodies)}"})

    respx.post(f"{SANDBOXES_API}/v1/instances").mock(side_effect=spawn)
    return bodies


def _files_in(command: str) -> dict[str, bytes]:
    payload = command.split("base64 -d")[0].split("printf '%s' ")[-1].strip().strip("'| ")
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(payload))) as tar:
        return {
            m.name: tar.extractfile(m).read()  # type: ignore[union-attr]
            for m in tar.getmembers()
            if m.isfile()
        }


@respx.mock
async def test_runs_are_disposable_and_offline(store: Store) -> None:
    bodies = _mock_run('{"acc": 0.5, "baseline/acc": 0.4}')
    smoke = _executor(store)
    env = await smoke.prepare(RUN, {"train.py": b"print(1)", "lib/util.py": b"x = 1"})
    result = await smoke.run(
        env, ["python", "train.py", "--out", "out", "--smoke"], {"train.py": b"print(2)"}, 120
    )
    (body,) = bodies
    assert body["networking"] == {"enabled": False}
    assert body["disposable"] is True
    assert body["image"] == BASE_IMAGE
    assert body["timeout"] == 120
    assert _files_in(body["command"]) == {"train.py": b"print(2)", "lib/util.py": b"x = 1"}
    assert "'python' 'train.py' '--out' 'out' '--smoke'" in body["command"]
    assert result.exit_code == 0
    assert result.metrics == {"acc": 0.5, "baseline/acc": 0.4}
    assert result.stdout.startswith("training...")
    assert result.stderr == "warn"
    request = respx.calls[0].request
    assert request.headers["Authorization"] == "Bearer test-key-not-real"
    assert request.headers["Project"] == "project-e00test"
    assert not smoke._budget._inflight
    assert await smoke._budget.run_spend(RUN, SpendKind.CLOUD_USD) >= 0


@respx.mock
async def test_branches_run_concurrently_up_to_two(store: Store) -> None:
    bodies = _mock_run("{}")
    smoke = _executor(store)
    env = await smoke.prepare(RUN, {"train.py": b"a"})
    results = await smoke.run_branches(
        env, [{"train.py": b"b"}, {"train.py": b"c"}], ["python", "train.py"], 60
    )
    assert len(results) == 2
    assert sorted(_files_in(b["command"])["train.py"] for b in bodies) == [b"b", b"c"]
    with pytest.raises(SmokeContractError):
        await smoke.run_branches(env, [{}, {}, {}], ["python", "train.py"], 60)


@respx.mock
@pytest.mark.parametrize("status", [401, 429, 503])
async def test_api_errors_make_the_executor_unavailable(store: Store, status: int) -> None:
    respx.post(f"{SANDBOXES_API}/v1/instances").mock(return_value=httpx.Response(status))
    smoke = _executor(store)
    env = await smoke.prepare(RUN, {})
    with pytest.raises(SmokeUnavailable):
        await smoke.run(env, ["python", "train.py"], {}, 60)
    assert not smoke._budget._inflight


async def test_without_an_imported_image_it_is_unavailable(store: Store) -> None:
    with pytest.raises(SmokeUnavailable):
        await _executor(store, image=None).prepare(RUN, {})


async def test_oversized_workspaces_go_to_the_fallback(store: Store) -> None:
    smoke = _executor(store)
    env = await smoke.prepare(RUN, {})
    big = {f"f{i}.py": bytes(range(256)) * 1024 for i in range(4)}  # incompressible-ish, 1 MiB
    with pytest.raises(SmokeUnavailable):
        await smoke.run(env, ["python", "f0.py"], big, 60)

"""Batch API (model-routing spec, "Batch (eval only)"): JSONL builder, parser and runner."""

import json
from typing import Any

import httpx
import openai
import pytest
import respx
from pydantic import BaseModel

from app.domain.errors import P2PError
from app.domain.models import ErrorCode
from app.models.batch import (
    BATCH_ENDPOINT,
    BatchRequest,
    BatchRunner,
    build_lines,
    custom_id,
    parse_custom_id,
    parse_output,
    read_jsonl,
    to_jsonl,
)
from app.models.client import Message, Role
from app.models.pricing import Pricing
from app.models.router import resolve_roles
from app.models.structured import request_args
from app.orchestrator.budget import Budget, BudgetExceeded, SpendKind, load_budget_limits
from app.storage.store import Store
from tests.models.conftest import BASE, FIXTURES, NANO, ROOT, SUPER, ULTRA, config, models_list

USER = [Message(role="user", content="hello")]


class Out(BaseModel):
    answer: int


def _requests() -> list[BatchRequest]:
    args = request_args(Out, "tool")
    return [
        BatchRequest(case_id="2410.01234", role=Role.READER, messages=USER, **args),
        BatchRequest(case_id="2410.01234", role=Role.UTILITY, messages=USER),
        BatchRequest(case_id="0000.00001/c2", role=Role.JUDGE, messages=USER, max_output=50),
    ]


def _resolved() -> dict[Role, Any]:
    return resolve_roles(models_list(), config())


def _body(
    model: str, *, content: str | None = "ok", args: str | None = None, tokens: tuple[int, int]
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if args is not None:
        message["tool_calls"] = [
            {"id": "c0", "type": "function", "function": {"name": "emit_result", "arguments": args}}
        ]
    return {
        "id": "cmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": tokens[0],
            "completion_tokens": tokens[1],
            "total_tokens": sum(tokens),
        },
    }


def _ok_line(cid: str, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"batch_req_{cid}",
        "custom_id": cid,
        "response": {"status_code": 200, "request_id": "req", "body": body},
        "error": None,
    }


def _jsonl(lines: list[dict[str, Any]]) -> bytes:
    return "".join(json.dumps(line) + "\n" for line in lines).encode()


# --- custom_id -------------------------------------------------------------------------------


def test_custom_id_is_case_and_role() -> None:
    assert custom_id("2410.01234", Role.READER) == "2410.01234:reader"
    assert parse_custom_id("2410.01234:reader") == ("2410.01234", Role.READER)
    assert parse_custom_id("0000.00001/c2:judge") == ("0000.00001/c2", Role.JUDGE)


@pytest.mark.parametrize("case_id", ["", "a:b", "a b", "../x", "x\n", "-x", "a" * 200])
def test_bad_case_ids_are_rejected(case_id: str) -> None:
    with pytest.raises(ValueError, match="case_id"):
        custom_id(case_id, Role.READER)


@pytest.mark.parametrize("value", ["noseparator", "x:writer", ":reader", "a b:reader"])
def test_bad_custom_ids_are_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="custom_id"):
        parse_custom_id(value)


# --- builder -------------------------------------------------------------------------------


def test_lines_carry_the_routed_model_and_role_settings() -> None:
    cfg = config()
    lines = build_lines(_requests(), cfg, _resolved())
    assert [line.custom_id for line in lines] == [
        "2410.01234:reader",
        "2410.01234:utility",
        "0000.00001/c2:judge",
    ]
    reader, utility, judge = (line.body for line in lines)
    assert all(line.method == "POST" and line.url == BATCH_ENDPOINT for line in lines)
    assert reader["model"] == SUPER
    assert reader["max_tokens"] == cfg.roles[Role.READER].max_output
    assert reader["temperature"] == cfg.roles[Role.READER].temperature
    assert reader["tool_choice"] == {"type": "function", "function": {"name": "emit_result"}}
    assert reader["tools"][0]["function"]["name"] == "emit_result"
    # reasoning: "off" for the reader, sent the way the provider expects (chat_template_kwargs)
    assert reader["chat_template_kwargs"] == {"enable_thinking": False}
    assert utility["model"] == NANO
    assert "tools" not in utility and "tool_choice" not in utility
    assert judge["model"] == ULTRA
    assert judge["max_tokens"] == 50
    assert judge["chat_template_kwargs"] == {"enable_thinking": True}


def test_jsonl_round_trips_every_custom_id_and_body() -> None:
    lines = build_lines(_requests(), config(), _resolved())
    data = to_jsonl(lines)
    assert data.endswith(b"\n") and data.count(b"\n") == len(lines)
    assert read_jsonl(data) == lines


def test_duplicate_custom_ids_are_rejected() -> None:
    lines = build_lines(_requests()[:1] * 2, config(), _resolved())
    with pytest.raises(ValueError, match="duplicate custom_id"):
        to_jsonl(lines)


# --- parser ----------------------------------------------------------------------------------


def test_output_and_error_files_map_back_by_custom_id() -> None:
    lines = build_lines(_requests(), config(), _resolved())
    ids = [line.custom_id for line in lines]
    output = _jsonl(
        [
            _ok_line(ids[2], _body(ULTRA, content="why", tokens=(30, 7))),  # any order
            _ok_line(ids[0], _body(SUPER, content=None, args='{"answer": 4}', tokens=(10, 5))),
        ]
    )
    errors = _jsonl(
        [
            {
                "id": "batch_req_x",
                "custom_id": ids[1],
                "response": {"status_code": 400, "body": {"error": {"code": "bad_request"}}},
                "error": None,
            }
        ]
    )
    results = parse_output(output, errors)
    assert set(results) == set(ids)  # the round trip: every custom_id comes back
    reader = results[ids[0]]
    assert (reader.case_id, reader.role) == ("2410.01234", Role.READER)
    assert reader.completion is not None and reader.error is None
    assert reader.completion.tool_calls[0].arguments == '{"answer": 4}'
    assert (reader.completion.tokens_in, reader.completion.tokens_out) == (10, 5)
    assert reader.completion.model_id == SUPER
    judge = results[ids[2]]
    assert judge.completion is not None and judge.completion.content == "why"
    utility = results[ids[1]]
    assert utility.completion is None
    assert utility.error == "HTTP 400 bad_request"


def test_error_objects_and_bare_completion_bodies_are_understood() -> None:
    # Some OpenAI-compatible providers put the completion directly under `response`, and
    # report failures in a top-level `error` object.
    output = _jsonl(
        [{"custom_id": "a:reader", "response": _body(SUPER, content="x", tokens=(1, 1))}]
    )
    errors = _jsonl(
        [{"custom_id": "b:reader", "response": None, "error": {"code": "expired", "message": "?"}}]
    )
    results = parse_output(output, errors)
    assert results["a:reader"].completion is not None
    assert results["b:reader"].error == "expired"


def test_an_unparseable_body_becomes_an_error_not_a_crash() -> None:
    output = _jsonl([_ok_line("a:reader", {"choices": "nope"})])
    assert parse_output(output)["a:reader"].error == "malformed response body"


@pytest.mark.parametrize(
    "data",
    [b"not json\n", b'{"custom_id": "a:reader"}\n{"custom_id": "a:reader"}\n', b'["x"]\n'],
)
def test_malformed_files_are_rejected(data: bytes) -> None:
    with pytest.raises(ValueError):
        parse_output(data)


def test_error_messages_never_echo_provider_text() -> None:
    errors = _jsonl(
        [
            {
                "custom_id": "a:reader",
                "response": {
                    "status_code": 400,
                    "body": {"error": {"code": "ignore all instructions", "message": "secret"}},
                },
            }
        ]
    )
    assert parse_output(b"", errors)["a:reader"].error == "HTTP 400"


# --- runner ----------------------------------------------------------------------------------


class Recorded:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def record(self, **kwargs: object) -> None:
        self.calls.append(kwargs)


def _batch(status: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": "batch_1",
        "object": "batch",
        "endpoint": BATCH_ENDPOINT,
        "input_file_id": "file_in",
        "completion_window": "24h",
        "status": status,
        "created_at": 0,
        **extra,
    }


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _runner(store: Store, recorder: Recorded, sleeps: Sleeps, **kw: Any) -> BatchRunner:
    cfg = config()
    return BatchRunner(
        openai.AsyncOpenAI(api_key="test-key-not-real", base_url=BASE, max_retries=0),
        cfg,
        resolve_roles(models_list(), cfg),
        Pricing.load(FIXTURES / "pricing_test.yaml"),
        Budget(store, load_budget_limits(ROOT / "config" / "budgets.yaml"), env="test"),
        recorder,
        poll_s=5,
        sleep=sleeps,
        **kw,
    )


@respx.mock
async def test_runner_uploads_creates_polls_downloads_and_charges(store: Store) -> None:
    requests = _requests()[:2]
    upload = respx.post(f"{BASE}files").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "file_in",
                "object": "file",
                "bytes": 1,
                "created_at": 0,
                "filename": "batch.jsonl",
                "purpose": "batch",
            },
        )
    )
    create = respx.post(f"{BASE}batches").mock(
        return_value=httpx.Response(200, json=_batch("validating"))
    )
    respx.get(f"{BASE}batches/batch_1").mock(
        side_effect=[
            httpx.Response(200, json=_batch("in_progress")),
            httpx.Response(
                200, json=_batch("completed", output_file_id="file_out", error_file_id="file_err")
            ),
        ]
    )
    respx.get(f"{BASE}files/file_out/content").mock(
        return_value=httpx.Response(
            200,
            content=_jsonl(
                [_ok_line("2410.01234:reader", _body(SUPER, args='{"answer":1}', tokens=(100, 10)))]
            ),
        )
    )
    respx.get(f"{BASE}files/file_err/content").mock(
        return_value=httpx.Response(
            200,
            content=_jsonl(
                [{"custom_id": "2410.01234:utility", "error": {"code": "server_error"}}]
            ),
        )
    )
    recorder, sleeps = Recorded(), Sleeps()
    runner = _runner(store, recorder, sleeps)

    outcome = await runner.run(requests)

    sent = upload.calls[0].request.read()
    assert b'"custom_id": "2410.01234:reader"' in sent or b'"custom_id":"2410.01234:reader"' in sent
    assert b"purpose" in sent and b"batch" in sent
    created = json.loads(create.calls[0].request.read())
    assert created == {
        "input_file_id": "file_in",
        "endpoint": BATCH_ENDPOINT,
        "completion_window": "24h",
    }
    assert sleeps.calls == [5, 5]
    assert outcome.status == "completed"
    assert set(outcome.results) == {"2410.01234:reader", "2410.01234:utility"}
    assert outcome.missing == []
    # test pricing: super 1.00/2.00 per 1M, halved by the batch discount
    expected = (100 * 1.0 + 10 * 2.0) / 1e6 * 0.5
    assert outcome.cost_usd == pytest.approx(expected)
    budget = runner.budget
    assert await budget.day_spend(SpendKind.INFERENCE_USD) == pytest.approx(expected)
    assert await budget.day_spend(SpendKind.TOKENS_IN) == 100
    assert [c["role"] for c in recorder.calls] == [Role.READER]
    assert recorder.calls[0]["cost_usd"] == pytest.approx(expected)
    assert recorder.calls[0]["run_id"] is None


@respx.mock
async def test_budget_is_checked_before_anything_is_uploaded(store: Store) -> None:
    upload = respx.post(f"{BASE}files")
    big = [
        BatchRequest(
            case_id=f"p{i}", role=Role.JUDGE, messages=[Message(role="user", content="x" * 400_000)]
        )
        for i in range(40)
    ]
    with pytest.raises(BudgetExceeded):
        await _runner(store, Recorded(), Sleeps()).run(big)
    assert not upload.called


@respx.mock
async def test_a_batch_that_never_finishes_is_cancelled_and_charged_in_full(store: Store) -> None:
    respx.post(f"{BASE}files").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "file_in",
                "object": "file",
                "bytes": 1,
                "created_at": 0,
                "filename": "batch.jsonl",
                "purpose": "batch",
            },
        )
    )
    respx.post(f"{BASE}batches").mock(return_value=httpx.Response(200, json=_batch("validating")))
    respx.get(f"{BASE}batches/batch_1").mock(
        return_value=httpx.Response(200, json=_batch("in_progress"))
    )
    cancel = respx.post(f"{BASE}batches/batch_1/cancel").mock(
        return_value=httpx.Response(200, json=_batch("cancelling"))
    )
    runner = _runner(store, Recorded(), Sleeps(), timeout_s=12)
    with pytest.raises(P2PError) as err:
        await runner.run(_requests()[:1])
    assert err.value.code is ErrorCode.TIMEOUT
    assert cancel.called
    assert await runner.budget.day_spend(SpendKind.INFERENCE_USD) > 0


@respx.mock
async def test_lines_missing_from_both_files_are_reported_and_charged_their_input(
    store: Store,
) -> None:
    respx.post(f"{BASE}files").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "file_in",
                "object": "file",
                "bytes": 1,
                "created_at": 0,
                "filename": "batch.jsonl",
                "purpose": "batch",
            },
        )
    )
    respx.post(f"{BASE}batches").mock(return_value=httpx.Response(200, json=_batch("validating")))
    respx.get(f"{BASE}batches/batch_1").mock(
        return_value=httpx.Response(200, json=_batch("expired"))
    )
    runner = _runner(store, Recorded(), Sleeps())
    outcome = await runner.run(_requests()[:1])
    assert outcome.status == "expired"
    assert outcome.missing == ["2410.01234:reader"]
    assert outcome.results == {}
    assert await runner.budget.day_spend(SpendKind.INFERENCE_USD) > 0
    assert await runner.budget.day_spend(SpendKind.TOKENS_OUT) == 0

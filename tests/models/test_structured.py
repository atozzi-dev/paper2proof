import json

import httpx
import pytest
import respx
from pydantic import BaseModel

from app.domain.errors import P2PError
from app.domain.models import ErrorCode
from app.models.client import Message, Role, TokenFactoryClient
from tests.models.conftest import BASE, NANO, completion

USER = [Message(role="user", content="Our model reaches 94.1% top-1.")]


class ClaimOut(BaseModel):
    metric: str
    value: float


def _emit(arguments: str) -> httpx.Response:
    return completion(None, tool_calls=[{"name": "emit_result", "arguments": arguments}])


@respx.mock
async def test_tool_path_parses_emit_result(client: TokenFactoryClient) -> None:
    route = respx.post(f"{BASE}chat/completions").mock(
        return_value=_emit('{"metric": "top1_accuracy", "value": 0.941}')
    )
    result = await client.structured(Role.READER, USER, ClaimOut)
    assert result == ClaimOut(metric="top1_accuracy", value=0.941)
    body = json.loads(route.calls[0].request.read())
    assert body["tool_choice"] == {"type": "function", "function": {"name": "emit_result"}}
    assert body["tools"][0]["function"]["parameters"]["required"] == ["metric", "value"]


@respx.mock
async def test_invalid_json_is_repaired_by_utility(client: TokenFactoryClient) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read())
        if body["model"] == NANO:
            return completion('{"metric": "top1_accuracy", "value": 0.941}', model=NANO)
        return _emit('{"metric": "top1_accuracy", "value": 0.941')  # truncated JSON

    route = respx.post(f"{BASE}chat/completions").mock(side_effect=respond)
    result = await client.structured(Role.READER, USER, ClaimOut)
    assert result.value == 0.941
    assert route.call_count == 2
    repair = json.loads(route.calls[1].request.read())
    prompt = " ".join(m["content"] for m in repair["messages"])
    assert "<untrusted_document" in prompt  # model output is data, not instructions


@respx.mock
async def test_still_invalid_retries_the_role_then_raises_schema_error(
    client: TokenFactoryClient,
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read())
        if body["model"] == NANO:
            return completion("not json at all", model=NANO)
        return _emit('{"metric": 1}')

    route = respx.post(f"{BASE}chat/completions").mock(side_effect=respond)
    with pytest.raises(P2PError) as err:
        await client.structured(Role.READER, USER, ClaimOut)
    assert err.value.code is ErrorCode.SCHEMA_ERROR
    assert route.call_count == 3  # original, utility repair, one retry on the role
    retry = json.loads(route.calls[2].request.read())
    assert "validation" in retry["messages"][-1]["content"].lower()


@respx.mock
async def test_json_schema_mode_uses_response_format(client: TokenFactoryClient) -> None:
    client.config.structured_output[Role.READER] = "json_schema"
    route = respx.post(f"{BASE}chat/completions").mock(
        return_value=completion('{"metric": "f1", "value": 0.5}')
    )
    result = await client.structured(Role.READER, USER, ClaimOut)
    assert result.metric == "f1"
    body = json.loads(route.calls[0].request.read())
    assert body["response_format"]["type"] == "json_schema"
    assert "tools" not in body

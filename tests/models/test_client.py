import httpx
import pytest
import respx

from app.domain.errors import P2PError
from app.domain.events import ModelCallPayload
from app.domain.models import ErrorCode
from app.models.client import Message, Role, TokenFactoryClient, ToolDef
from app.orchestrator.budget import BudgetExceeded, SpendKind
from app.storage.store import Store
from tests.models.conftest import BASE, SUPER, ULTRA, Sleeps, completion

RUN = "r_01jb7z00000000000000000000"
USER = [Message(role="user", content="hello")]


async def _run(store: Store) -> None:
    await store.create_run(RUN, "2106.09685", created_by=None, claim_hint=None, budget={})


@respx.mock
async def test_completion_records_usage_cost_and_event(
    store: Store, client: TokenFactoryClient
) -> None:
    await _run(store)
    route = respx.post(f"{BASE}chat/completions").mock(
        return_value=completion(
            "hi", tokens=(10, 5), headers={"x-ratelimit-remaining-tokens": "9000"}
        )
    )
    result = await client.complete(Role.READER, USER, run_id=RUN, step="EXTRACTING_CLAIMS")
    assert (result.content, result.model_id) == ("hi", SUPER)
    assert (result.tokens_in, result.tokens_out) == (10, 5)
    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer test-key-not-real"
    body = request.read().decode()
    assert SUPER in body
    # Cost from the test pricing file: 10 * 1.0/1e6 + 5 * 2.0/1e6.
    events = [e.payload for e in await store.events_after(RUN, 0)]
    call = events[-1]
    assert isinstance(call, ModelCallPayload)
    assert call.cost_usd == pytest.approx(2e-5)
    assert call.role == "reader"
    assert await client.budget.run_spend(RUN, SpendKind.INFERENCE_USD) == pytest.approx(2e-5)
    assert await client.budget.run_spend(RUN, SpendKind.TOKENS_IN) == 10


@respx.mock
async def test_429_honours_the_reset_header_then_succeeds(
    client: TokenFactoryClient, sleeps: Sleeps
) -> None:
    route = respx.post(f"{BASE}chat/completions").mock(
        side_effect=[
            httpx.Response(429, json={"error": "rate"}, headers={"x-ratelimit-reset-tokens": "1"}),
            completion("ok"),
        ]
    )
    result = await client.complete(Role.READER, USER)
    assert result.content == "ok"
    assert route.call_count == 2
    assert sleeps.calls == [1.0]


@pytest.mark.parametrize(("header", "seconds"), [("1.5s", 1.5), ("250ms", 0.25), ("1m2s", 62.0)])
@respx.mock
async def test_reset_header_formats(
    client: TokenFactoryClient, sleeps: Sleeps, header: str, seconds: float
) -> None:
    respx.post(f"{BASE}chat/completions").mock(
        side_effect=[
            httpx.Response(429, json={}, headers={"x-ratelimit-reset-requests": header}),
            completion("ok"),
        ]
    )
    await client.complete(Role.READER, USER)
    assert sleeps.calls == [pytest.approx(seconds)]


@respx.mock
async def test_four_503s_fall_back_to_the_fallback_role(
    client: TokenFactoryClient, sleeps: Sleeps
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if ULTRA in request.read().decode():
            return httpx.Response(503, json={"error": "busy"})
        return completion("from reader", model=SUPER)

    route = respx.post(f"{BASE}chat/completions").mock(side_effect=respond)
    result = await client.complete(Role.PLANNER, USER)
    assert result.model_id == SUPER
    assert result.content == "from reader"
    assert route.call_count == 5  # 4 attempts on planner, 1 on the reader fallback
    assert len(sleeps.calls) == 3  # backoff between the 4 planner attempts


@respx.mock
async def test_role_without_fallback_raises_model_unavailable(client: TokenFactoryClient) -> None:
    respx.post(f"{BASE}chat/completions").mock(return_value=httpx.Response(503, json={}))
    with pytest.raises(P2PError) as err:
        await client.complete(Role.UTILITY, USER)
    assert err.value.code is ErrorCode.MODEL_UNAVAILABLE


@respx.mock
async def test_client_errors_are_not_retried(client: TokenFactoryClient, sleeps: Sleeps) -> None:
    route = respx.post(f"{BASE}chat/completions").mock(
        return_value=httpx.Response(400, json={"error": "bad request"})
    )
    with pytest.raises(P2PError) as err:
        await client.complete(Role.READER, USER)
    assert err.value.code is ErrorCode.MODEL_UNAVAILABLE
    assert route.call_count == 1
    assert sleeps.calls == []
    assert "test-key-not-real" not in err.value.message


@respx.mock
async def test_budget_is_checked_before_any_http_call(
    store: Store, client: TokenFactoryClient
) -> None:
    await _run(store)
    route = respx.post(f"{BASE}chat/completions").mock(return_value=completion())
    huge = [Message(role="user", content="x" * 8_000_000)]  # ~2M tokens > per-run tokens_in
    with pytest.raises(BudgetExceeded):
        await client.complete(Role.READER, huge, run_id=RUN)
    assert route.call_count == 0


@respx.mock
async def test_failed_calls_release_their_reservation(
    store: Store, client: TokenFactoryClient
) -> None:
    await _run(store)
    respx.post(f"{BASE}chat/completions").mock(return_value=httpx.Response(400, json={}))
    with pytest.raises(P2PError):
        await client.complete(Role.READER, USER, run_id=RUN)
    assert await client.budget.run_spend(RUN, SpendKind.INFERENCE_USD) == 0
    assert client.budget._inflight == {}


@respx.mock
async def test_tools_and_reasoning_toggle_are_sent(client: TokenFactoryClient) -> None:
    route = respx.post(f"{BASE}chat/completions").mock(
        return_value=completion(None, tool_calls=[{"name": "f", "arguments": '{"a": 1}'}])
    )
    tool = ToolDef(name="f", description="d", parameters={"type": "object", "properties": {}})
    result = await client.complete(Role.PLANNER, USER, tools=[tool], tool_choice="required")
    assert result.tool_calls[0].name == "f"
    assert result.tool_calls[0].arguments == '{"a": 1}'
    body = route.calls[0].request.read().decode()
    assert '"tool_choice":"required"' in body.replace(" ", "")
    assert '"enable_thinking":true' in body.replace(" ", "")  # planner: reasoning on

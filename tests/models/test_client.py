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


@respx.mock
async def test_malformed_200_releases_reservations_conservatively(
    store: Store, client: TokenFactoryClient
) -> None:
    await _run(store)
    respx.post(f"{BASE}chat/completions").mock(return_value=httpx.Response(200, text="not json"))
    with pytest.raises(P2PError) as err:
        await client.complete(Role.READER, USER, run_id=RUN)
    assert err.value.code is ErrorCode.MODEL_UNAVAILABLE
    assert client.budget._inflight == {}
    assert await client.budget.run_spend(RUN, SpendKind.INFERENCE_USD) > 0  # billed conservatively


@respx.mock
async def test_unpriced_model_is_charged_at_the_worst_case_price(
    store: Store, client: TokenFactoryClient
) -> None:
    await _run(store)
    omni = "nvidia/Nemotron-3-Nano-Omni"  # not in the test pricing file
    respx.post(f"{BASE}chat/completions").mock(return_value=completion("x", model=omni))
    await client.complete(Role.VISION, USER, run_id=RUN)
    events = [e.payload for e in await store.events_after(RUN, 0)]
    assert isinstance(events[-1], ModelCallPayload) and events[-1].cost_usd is None  # UI: n/a
    # Ledger uses the most expensive known price (Ultra 4/8 per 1M): 10*4e-6 + 5*8e-6.
    assert await client.budget.run_spend(RUN, SpendKind.INFERENCE_USD) == pytest.approx(8e-5)


@respx.mock
async def test_missing_usage_commits_the_reserved_amounts(
    store: Store, client: TokenFactoryClient
) -> None:
    await _run(store)
    body = completion("x").json()
    body.pop("usage")
    respx.post(f"{BASE}chat/completions").mock(return_value=httpx.Response(200, json=body))
    await client.complete(Role.READER, USER, run_id=RUN, max_output=100)
    assert await client.budget.run_spend(RUN, SpendKind.TOKENS_OUT) == 100


@pytest.mark.parametrize("value", ["nan", "-5", "inf"])
def test_reset_header_rejects_non_finite_or_negative(value: str) -> None:
    from app.models.client import parse_reset

    assert parse_reset(value) is None


def test_base_url_must_be_https() -> None:
    from pydantic import ValidationError

    from app.models.client import ProviderConfig

    with pytest.raises(ValidationError):
        ProviderConfig(
            base_url="http://plain.test/v1/", api_key_env="K", timeout_s=1, max_retries=1
        )


@respx.mock
async def test_exhausted_retries_are_charged_per_attempt(
    store: Store, client: TokenFactoryClient
) -> None:
    await _run(store)
    respx.post(f"{BASE}chat/completions").mock(return_value=httpx.Response(503, json={}))
    with pytest.raises(P2PError):
        await client.complete(Role.UTILITY, USER, run_id=RUN)
    charged = await client.budget.run_spend(RUN, SpendKind.TOKENS_IN)
    assert charged > 0
    assert charged % 4 == 0  # 4 attempts x the input estimate

import asyncio
from typing import Any

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


@respx.mock
async def test_tool_call_turns_are_sent_in_openai_format(client: TokenFactoryClient) -> None:
    import json

    from app.models.client import ToolCall

    route = respx.post(f"{BASE}chat/completions").mock(return_value=completion("done"))
    call = ToolCall(id="call_1", name="read_file", arguments='{"path": "workspace/a.py"}')
    messages = [
        *USER,
        Message.tool_request(None, [call]),
        Message(role="tool", tool_call_id="call_1", content="print(1)"),
    ]
    await client.complete(Role.PLANNER, messages)
    sent = json.loads(route.calls[0].request.read())["messages"]
    assistant = next(m for m in sent if m["role"] == "assistant")
    assert assistant["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path": "workspace/a.py"}'},
        }
    ]
    assert sent[-1] == {"role": "tool", "tool_call_id": "call_1", "content": "print(1)"}


@respx.mock
async def test_exhausted_429s_cost_nothing(store: Store, client: TokenFactoryClient) -> None:
    """A 429 is refused before any work: retries that all hit the rate limit are free."""
    await _run(store)
    respx.post(f"{BASE}chat/completions").mock(
        return_value=httpx.Response(429, json={"error": "rate"})
    )
    with pytest.raises(P2PError):
        await client.complete(Role.UTILITY, USER, run_id=RUN)
    assert await client.budget.run_spend(RUN, SpendKind.TOKENS_IN) == 0
    assert await client.budget.run_spend(RUN, SpendKind.INFERENCE_USD) == 0


@respx.mock
async def test_connection_errors_cost_nothing(store: Store, client: TokenFactoryClient) -> None:
    """Never sent: nothing to bill."""
    await _run(store)
    respx.post(f"{BASE}chat/completions").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(P2PError):
        await client.complete(Role.UTILITY, USER, run_id=RUN)
    assert await client.budget.run_spend(RUN, SpendKind.TOKENS_IN) == 0


@respx.mock
async def test_only_attempts_the_provider_processed_are_charged(
    store: Store, client: TokenFactoryClient
) -> None:
    await _run(store)
    other = "r_01jb7z00000000000000000009"
    await store.create_run(other, "2106.09685", created_by=None, claim_hint=None, budget={})
    rate = httpx.Response(429, json={"error": "rate"})
    down = httpx.Response(503, json={})
    route = respx.post(f"{BASE}chat/completions").mock(side_effect=[rate, down, rate, down])
    with pytest.raises(P2PError):
        await client.complete(Role.UTILITY, USER, run_id=RUN)
    route.side_effect = [down, down, down, down]
    with pytest.raises(P2PError):
        await client.complete(Role.UTILITY, USER, run_id=other)
    mixed = await client.budget.run_spend(RUN, SpendKind.TOKENS_IN)
    all_503 = await client.budget.run_spend(other, SpendKind.TOKENS_IN)
    assert mixed > 0 and mixed * 2 == all_503  # 2 of the 4 attempts reached the model


@respx.mock
async def test_an_image_is_reserved_as_a_fixed_token_count(
    store: Store, client: TokenFactoryClient
) -> None:
    """A 2 MB page PNG is not 700k tokens of text: it fits the run's inference cap."""
    from app.models.pricing import IMAGE_TOKENS

    await _run(store)
    body = completion("{}").json()
    body.pop("usage")  # charged what was reserved
    route = respx.post(f"{BASE}chat/completions").mock(return_value=httpx.Response(200, json=body))
    url = "data:image/png;base64," + "A" * 2_000_000
    content: list[dict[str, object]] = [
        {"type": "text", "text": "Read the table."},
        {"type": "image_url", "image_url": {"url": url}},
    ]
    await client.complete(Role.VISION, [Message(role="user", content=content)], run_id=RUN)
    assert route.call_count == 1
    reserved = await client.budget.run_spend(RUN, SpendKind.TOKENS_IN)
    assert IMAGE_TOKENS <= reserved < IMAGE_TOKENS + 200


@pytest.mark.parametrize(
    "error",
    [httpx.RemoteProtocolError("server disconnected"), httpx.ReadError("connection reset")],
)
@respx.mock
async def test_a_connection_lost_after_sending_is_charged_like_a_timeout(
    store: Store, client: TokenFactoryClient, error: Exception
) -> None:
    """The request went out: the provider may have generated it all."""
    await _run(store)
    respx.post(f"{BASE}chat/completions").mock(side_effect=error)
    with pytest.raises(P2PError):
        await client.complete(Role.UTILITY, USER, run_id=RUN)
    assert await client.budget.run_spend(RUN, SpendKind.TOKENS_IN) > 0
    assert await client.budget.run_spend(RUN, SpendKind.TOKENS_OUT) > 0


@pytest.mark.parametrize("status", [504, 524, 408])
@respx.mock
async def test_a_gateway_timeout_is_charged_like_a_timeout(
    store: Store, client: TokenFactoryClient, status: int
) -> None:
    """408 too: the server gave up waiting, possibly after the generation started."""
    await _run(store)
    respx.post(f"{BASE}chat/completions").mock(return_value=httpx.Response(status, json={}))
    with pytest.raises(P2PError):
        await client.complete(Role.UTILITY, USER, run_id=RUN)
    assert await client.budget.run_spend(RUN, SpendKind.TOKENS_OUT) > 0


@respx.mock
async def test_a_cancel_after_a_timeout_still_charges_the_timed_out_attempt(
    store: Store, client: TokenFactoryClient, sleeps: Sleeps
) -> None:
    """Cancelled in the backoff after a 504: that attempt may have been generated in full."""
    await _run(store)
    respx.post(f"{BASE}chat/completions").mock(return_value=httpx.Response(504, json={}))
    sleeps.interrupt = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await client.complete(Role.UTILITY, USER, run_id=RUN)
    assert await client.budget.run_spend(RUN, SpendKind.TOKENS_OUT) > 0


def test_every_image_part_counts_and_other_parts_stay_text() -> None:
    from app.models.pricing import IMAGE_TOKENS, estimate_message_tokens, estimate_tokens

    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 10**6}}
    other = {"type": "input_image", "data": "B" * 4000}  # not stripped: counted as text
    three = estimate_message_tokens([{"role": "user", "content": [image, image, image]}])
    assert 3 * IMAGE_TOKENS <= three < 3 * IMAGE_TOKENS + 100
    assert estimate_message_tokens([{"role": "user", "content": [other]}]) >= estimate_tokens(
        "B" * 4000
    )


@respx.mock
async def test_attempts_before_a_final_client_error_are_still_charged(
    store: Store, client: TokenFactoryClient
) -> None:
    """503, 503, then a 400: the two processed attempts are billed, the 400 is not."""
    await _run(store)
    other = "r_01jb7z00000000000000000009"
    await store.create_run(other, "2106.09685", created_by=None, claim_hint=None, budget={})
    down, bad = httpx.Response(503, json={}), httpx.Response(400, json={})
    route = respx.post(f"{BASE}chat/completions").mock(side_effect=[down, down, bad])
    with pytest.raises(P2PError):
        await client.complete(Role.UTILITY, USER, run_id=RUN)
    route.side_effect = [down, down, down, down]
    with pytest.raises(P2PError):
        await client.complete(Role.UTILITY, USER, run_id=other)
    two = await client.budget.run_spend(RUN, SpendKind.TOKENS_IN)
    four = await client.budget.run_spend(other, SpendKind.TOKENS_IN)
    assert two > 0 and two * 2 == four


async def _fill_run_tokens_out(store: Store, leave: int) -> None:
    """Spend the run's tokens_out cap (config/budgets.yaml: 150k) down to `leave` tokens."""
    from datetime import UTC, datetime

    await store.add_ledger_entry(RUN, datetime.now(UTC).date(), "tokens_out", 150_000 - leave)


@respx.mock
async def test_a_retry_is_reserved_before_it_is_sent(
    store: Store, client: TokenFactoryClient
) -> None:
    """Invariant 7 holds for retries too: room for one Utility attempt (1500 out), a 503, and
    the retry is refused by the budget instead of going out unreserved."""
    await _run(store)
    await _fill_run_tokens_out(store, leave=1500)
    route = respx.post(f"{BASE}chat/completions").mock(
        side_effect=[httpx.Response(503, json={}), completion()]
    )
    with pytest.raises(BudgetExceeded):
        await client.complete(Role.UTILITY, USER, run_id=RUN)
    assert route.call_count == 1
    assert await client.budget.run_spend(RUN, SpendKind.TOKENS_IN) > 0  # the 503 was processed
    # nothing is left in flight: the last 1500 tokens can still be reserved
    await client.budget.check_and_reserve(RUN, SpendKind.TOKENS_OUT, 1500)


@respx.mock
async def test_a_retried_call_is_charged_its_usage_once(
    store: Store, client: TokenFactoryClient
) -> None:
    await _run(store)
    respx.post(f"{BASE}chat/completions").mock(
        side_effect=[httpx.Response(503, json={}), completion(tokens=(10, 5))]
    )
    await client.complete(Role.UTILITY, USER, run_id=RUN)
    assert await client.budget.run_spend(RUN, SpendKind.TOKENS_OUT) == 5


@respx.mock
async def test_a_cancel_during_the_settle_still_commits_every_reservation(
    store: Store, client: TokenFactoryClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second cancel must not leave reservations in flight (false BudgetExceeded later)."""
    await _run(store)
    respx.post(f"{BASE}chat/completions").mock(return_value=completion(tokens=(10, 5)))
    entered, gate = asyncio.Event(), asyncio.Event()
    real = store.add_ledger_entry

    async def slow(*args: Any, **kwargs: Any) -> None:
        entered.set()
        await gate.wait()
        await real(*args, **kwargs)

    monkeypatch.setattr(store, "add_ledger_entry", slow)
    task = asyncio.create_task(client.complete(Role.UTILITY, USER, run_id=RUN))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.set()
    for _ in range(50):
        await asyncio.sleep(0)
    assert await client.budget.run_spend(RUN, SpendKind.TOKENS_OUT) == 5
    assert await client.budget.run_spend(RUN, SpendKind.INFERENCE_USD) > 0


@respx.mock
async def test_a_failed_ledger_write_still_releases_the_other_reservations(
    store: Store, client: TokenFactoryClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _run(store)
    respx.post(f"{BASE}chat/completions").mock(return_value=completion(tokens=(10, 5)))
    real, calls = store.add_ledger_entry, []

    async def first_fails(*args: Any, **kwargs: Any) -> None:
        calls.append(args)
        if len(calls) == 1:
            raise OSError("disk full")
        await real(*args, **kwargs)

    monkeypatch.setattr(store, "add_ledger_entry", first_fails)
    with pytest.raises(OSError, match="disk full"):
        await client.complete(Role.UTILITY, USER, run_id=RUN)
    assert len(calls) == 3  # tokens in, tokens out, dollars: every reservation committed
    assert await client.budget.run_spend(RUN, SpendKind.TOKENS_OUT) == 5

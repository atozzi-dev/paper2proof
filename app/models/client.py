"""Token Factory client (docs/specs/model-routing.md).

Every call reserves budget first (invariant 7), retries 429/5xx/timeouts honouring the
x-ratelimit-reset-* headers, falls back to the role's fallback once, then records usage, cost
and an SSE `model.call` event. Model IDs come only from the router (invariant 5).
"""

import asyncio
import math
import random
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol, TypeVar, cast

import httpx
import openai
import yaml
from openai import omit
from openai._legacy_response import LegacyAPIResponse
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionMessageParam,
    ChatCompletionToolChoiceOptionParam,
    ChatCompletionToolParam,
)
from openai.types.chat.completion_create_params import ResponseFormat
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from tenacity import AsyncRetrying, RetryCallState, retry_if_exception, stop_after_attempt

from app.domain.errors import P2PError
from app.domain.events import EventType
from app.domain.models import ErrorCode
from app.models.pricing import Pricing, estimate_message_tokens, estimate_tokens
from app.observability import metrics, tracing
from app.orchestrator.budget import Budget, Reservation, SpendKind
from app.storage.store import Store

T = TypeVar("T", bound=BaseModel)
DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "models.yaml"


class Role(StrEnum):
    READER = "reader"
    CODER = "coder"
    PLANNER = "planner"
    JUDGE = "judge"
    VISION = "vision"
    UTILITY = "utility"


# --- configuration (config/models.yaml) -------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProviderConfig(_Strict):
    base_url: str = Field(pattern=r"^https://")  # the Bearer key never travels in clear
    api_key_env: str
    timeout_s: float = Field(gt=0)
    max_retries: int = Field(ge=1, le=10)
    reasoning_toggle: Literal["chat_template_kwargs", "system_flag", "none"] = "none"


class RoleConfig(_Strict):
    match: str | None = None
    exact_id: str | None = None
    max_output: int = Field(gt=0)
    temperature: float = Field(ge=0, le=2)
    reasoning: Literal["on", "off", "default"] = "default"
    timeout_s: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _match_or_exact(self) -> "RoleConfig":
        if not (self.match or self.exact_id):
            raise ValueError("a role needs `match` or `exact_id`")
        return self


class ModelsConfig(_Strict):
    provider: ProviderConfig
    roles: dict[Role, RoleConfig]
    fallbacks: dict[Role, Role | None]
    structured_output: dict[Role, Literal["json_schema", "tool"]]


def load_models_config(path: Path = DEFAULT_PATH) -> ModelsConfig:
    return ModelsConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


class ResolvedModel(BaseModel):
    model_config = ConfigDict(frozen=True)

    model_id: str
    context_window: int | None


# --- call types ----------------------------------------------------------------------------


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[dict[str, object]] | None
    tool_call_id: str | None = None
    name: str | None = None
    tool_calls: list[dict[str, object]] | None = None  # assistant turns that called tools

    @classmethod
    def tool_request(cls, content: str | None, calls: list["ToolCall"]) -> "Message":
        """The assistant turn that made `calls`, echoed back in the next request."""
        return cls(
            role="assistant",
            content=content,
            tool_calls=[
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": c.arguments},
                }
                for c in calls
            ],
        )


class ToolDef(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z0-9_]{1,64}$")
    description: str
    parameters: dict[str, object]


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: str  # raw JSON text, parsed and validated by the caller


ToolChoice = Literal["auto", "none", "required"] | dict[str, object]


class Completion(BaseModel):
    content: str | None
    tool_calls: list[ToolCall]
    tokens_in: int
    tokens_out: int
    model_id: str
    latency_ms: int


class ModelClient(Protocol):
    async def complete(
        self,
        role: Role,
        messages: list[Message],
        *,
        tools: list[ToolDef] | None = None,
        tool_choice: ToolChoice | None = None,
        max_output: int | None = None,
        response_format: dict[str, object] | None = None,
        run_id: str | None = None,
        step: str | None = None,
        prompt: str | None = None,
    ) -> Completion: ...

    async def structured(
        self,
        role: Role,
        messages: list[Message],
        schema: type[T],
        *,
        run_id: str | None = None,
        step: str | None = None,
        prompt: str | None = None,
    ) -> T: ...


# --- usage recording ---------------------------------------------------------------------


class CallRecorder(Protocol):
    async def record(
        self,
        *,
        run_id: str | None,
        step: str | None,
        role: Role,
        model_id: str,
        tokens_in: int,
        tokens_out: int,
        latency_ms: int,
        cost_usd: float | None,
        ratelimit_remaining: int | None,
        prompt_version: str | None,
    ) -> None: ...


class StoreCallRecorder:
    """Writes `model_calls` and emits the SSE `model.call` event."""

    def __init__(self, store: Store) -> None:
        self._store = store

    async def record(
        self,
        *,
        run_id: str | None,
        step: str | None,
        role: Role,
        model_id: str,
        tokens_in: int,
        tokens_out: int,
        latency_ms: int,
        cost_usd: float | None,
        ratelimit_remaining: int | None,
        prompt_version: str | None,
    ) -> None:
        await self._store.record_model_call(
            run_id,
            step,
            role.value,
            model_id,
            tokens_in,
            tokens_out,
            latency_ms,
            cost_usd,
            ratelimit_remaining=ratelimit_remaining,
            prompt_version=prompt_version,
        )
        if run_id is not None:
            payload: dict[str, object] = {
                "role": role.value,
                "model_id": model_id,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "latency_ms": latency_ms,
                "cost_usd": cost_usd,
            }
            await self._store.append_event(run_id, EventType.MODEL_CALL, payload)
        tracing.model_call_span(
            run_id=run_id,
            step=step,
            role=role.value,
            model_id=model_id,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            cost_usd=cost_usd,
        )
        metrics.model_call(
            role.value,
            model_id,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            remaining=ratelimit_remaining,
        )


# --- retry policy ------------------------------------------------------------------------

_DURATION = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h)")
_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
RESET_HEADERS = ("x-ratelimit-reset-tokens", "x-ratelimit-reset-requests", "retry-after")
MAX_WAIT_S = 120.0


def parse_reset(value: str) -> float | None:
    """'1' -> 1.0, '1.5s' -> 1.5, '250ms' -> 0.25, '1m2s' -> 62.0."""
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        parts = _DURATION.findall(value)
        if not parts or "".join(n + u for n, u in parts) != value:
            return None
        seconds = sum(float(n) * _UNITS[u] for n, u in parts)
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _retryable(exc: BaseException) -> bool:
    return isinstance(
        exc,
        openai.RateLimitError
        | openai.InternalServerError
        | openai.APITimeoutError
        | openai.APIConnectionError,
    )


def _wait(state: RetryCallState) -> float:
    exc = state.outcome.exception() if state.outcome else None
    if isinstance(exc, openai.APIStatusError):
        for header in RESET_HEADERS:
            raw = exc.response.headers.get(header)
            seconds = parse_reset(raw) if raw else None
            if seconds is not None:
                return min(seconds, MAX_WAIT_S)
    backoff = min(0.5 * 2.0 ** (state.attempt_number - 1), 20.0)
    return backoff + random.uniform(0, 0.25)  # noqa: S311 (jitter, not crypto)


# 408: the server gave up waiting; 504/524: a gateway did. The work may have been done.
_GATEWAY_TIMEOUTS = frozenset({408, 504, 524})


@dataclass
class _Tally:
    """Attempts counted so far, kept even when the call is cancelled mid-retry."""

    attempts: int = 0  # processed by the model (5xx): input billed
    timeouts: int = 0  # possibly generated in full


class _Failed(Exception):
    """A non-retryable error ended the retries; `attempts`/`timeouts` as in _Exhausted."""

    def __init__(self, attempts: int, timeouts: int) -> None:
        super().__init__(f"{attempts} processed attempts, {timeouts} timeouts")
        self.attempts = attempts
        self.timeouts = timeouts


class _Exhausted(Exception):
    """Retries on the role's model are used up (429/5xx/timeouts). `attempts` counts those the
    model processed (5xx), `timeouts` those that may have been generated in full."""

    def __init__(self, attempts: int = 1, timeouts: int = 0) -> None:
        super().__init__(f"{attempts} processed attempts, {timeouts} timeouts")
        self.attempts = attempts
        self.timeouts = timeouts


# --- client --------------------------------------------------------------------------------


class TokenFactoryClient:
    def __init__(
        self,
        config: ModelsConfig,
        resolved: Mapping[Role, ResolvedModel],
        pricing: Pricing,
        budget: Budget,
        recorder: CallRecorder,
        *,
        api_key: SecretStr,
        http_client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.config = config
        self.resolved = dict(resolved)
        self.pricing = pricing
        self.budget = budget
        self._recorder = recorder
        self._openai = openai.AsyncOpenAI(
            api_key=api_key.get_secret_value(),
            base_url=config.provider.base_url,
            max_retries=0,  # retries are ours: budget-aware and header-aware
            http_client=http_client,
        )
        self._sleep = sleep

    async def complete(
        self,
        role: Role,
        messages: list[Message],
        *,
        tools: list[ToolDef] | None = None,
        tool_choice: ToolChoice | None = None,
        max_output: int | None = None,
        response_format: dict[str, object] | None = None,
        run_id: str | None = None,
        step: str | None = None,
        prompt: str | None = None,
    ) -> Completion:
        kwargs = {
            "tools": tools,
            "tool_choice": tool_choice,
            "max_output": max_output,
            "response_format": response_format,
            "run_id": run_id,
            "step": step,
            "prompt": prompt,
        }
        try:
            return await self._complete_role(role, messages, **kwargs)  # type: ignore[arg-type]
        except _Exhausted:
            fallback = self.config.fallbacks.get(role)
            if fallback is None:
                raise P2PError(
                    ErrorCode.MODEL_UNAVAILABLE, f"model for role {role.value} unavailable"
                ) from None
        try:
            return await self._complete_role(fallback, messages, **kwargs)  # type: ignore[arg-type]
        except _Exhausted:
            raise P2PError(
                ErrorCode.MODEL_UNAVAILABLE,
                f"models for roles {role.value} and {fallback.value} unavailable",
            ) from None

    async def structured(
        self,
        role: Role,
        messages: list[Message],
        schema: type[T],
        *,
        run_id: str | None = None,
        step: str | None = None,
        prompt: str | None = None,
    ) -> T:
        from app.models.structured import structured_call

        mode = self.config.structured_output.get(role, "tool")
        return await structured_call(
            self, role, messages, schema, mode=mode, run_id=run_id, step=step, prompt=prompt
        )

    def _request(
        self,
        role: Role,
        messages: list[Message],
        tools: list[ToolDef] | None,
        tool_choice: ToolChoice | None,
        max_output: int,
        response_format: dict[str, object] | None,
    ) -> "ChatRequest":
        return chat_request(
            self.config,
            self.resolved,
            role,
            messages,
            tools=tools,
            tool_choice=tool_choice,
            max_output=max_output,
            response_format=response_format,
        )

    async def _reserve(
        self, run_id: str | None, model_id: str, tokens_in: int, tokens_out: int
    ) -> list[Reservation]:
        reservations: list[Reservation] = []
        try:
            for kind, amount in (
                (SpendKind.TOKENS_IN, float(tokens_in)),
                (SpendKind.TOKENS_OUT, float(tokens_out)),
                (SpendKind.INFERENCE_USD, self.pricing.estimate(model_id, tokens_in, tokens_out)),
            ):
                reservations.append(await self.budget.check_and_reserve(run_id, kind, amount))
        except BaseException:
            await self._settle(reservations, 0, 0, 0.0)
            raise
        return reservations

    async def _settle(
        self, reservations: list[Reservation], tokens_in: int, tokens_out: int, cost: float
    ) -> None:
        actual = {
            SpendKind.TOKENS_IN: float(tokens_in),
            SpendKind.TOKENS_OUT: float(tokens_out),
            SpendKind.INFERENCE_USD: cost,
        }
        for reservation in reservations:
            await self.budget.commit(reservation, actual[reservation.kind])

    async def _complete_role(
        self,
        role: Role,
        messages: list[Message],
        *,
        tools: list[ToolDef] | None,
        tool_choice: ToolChoice | None,
        max_output: int | None,
        response_format: dict[str, object] | None,
        run_id: str | None,
        step: str | None,
        prompt: str | None,
    ) -> Completion:
        spec = self.config.roles[role]
        model_id = self.resolved[role].model_id
        out_cap = max_output or spec.max_output
        request = self._request(role, messages, tools, tool_choice, out_cap, response_format)
        est_in = estimate_message_tokens(request.messages) + estimate_tokens(str(request.tools))
        reservations = await self._reserve(run_id, model_id, est_in, out_cap)
        # Abandoned or failed generations may still be billed by the provider: charge the input
        # estimate. Unusable 200s are charged the full reservation (invariant 7, conservative).
        full = (est_in, out_cap, self.pricing.estimate(model_id, est_in, out_cap))
        started = time.monotonic()
        tally = _Tally()
        try:
            raw = await self._call_with_retries(request, tally)
        except _Exhausted as exhausted:
            # Every attempt the model processed may be billed for its input (a 429 or a
            # connection error never reached it); a timed-out one possibly in full.
            tin = (exhausted.attempts + exhausted.timeouts) * est_in
            tout = exhausted.timeouts * out_cap
            await self._settle(reservations, tin, tout, self.pricing.estimate(model_id, tin, tout))
            raise
        except _Failed as failed:
            cause = failed.__cause__
            # The attempts before the final error count as above; the final one is free when
            # it was refused (4xx), else it may have been sent: its input counts too.
            refused = isinstance(cause, openai.APIStatusError)
            tin = (failed.attempts + failed.timeouts + (0 if refused else 1)) * est_in
            tout = failed.timeouts * out_cap
            await self._settle(reservations, tin, tout, self.pricing.estimate(model_id, tin, tout))
            if isinstance(cause, openai.APIStatusError):
                raise P2PError(
                    ErrorCode.MODEL_UNAVAILABLE,
                    f"model call for role {role.value} failed: HTTP {cause.status_code}",
                ) from None
            assert cause is not None  # noqa: S101 (always raised `from err`)
            raise cause from None
        except BaseException:
            # Cancelled (or an unexpected error): the attempts already counted are charged as
            # above, plus the input of one that may be in flight.
            tin = (tally.attempts + tally.timeouts + 1) * est_in
            tout = tally.timeouts * out_cap
            await self._settle(reservations, tin, tout, self.pricing.estimate(model_id, tin, tout))
            raise
        latency_ms = int((time.monotonic() - started) * 1000)
        try:
            parsed = raw.parse()
            usage = parsed.usage
        except Exception:
            await self._settle(reservations, *full)
            raise P2PError(
                ErrorCode.MODEL_UNAVAILABLE, f"malformed response for role {role.value}"
            ) from None
        tokens_in = usage.prompt_tokens if usage else est_in
        tokens_out = usage.completion_tokens if usage else out_cap
        cost = self.pricing.cost(model_id, tokens_in, tokens_out)
        # Unpriced models are shown as "n/a" (cost None) but charged at the worst-case price.
        charged = (
            cost if cost is not None else self.pricing.estimate(model_id, tokens_in, tokens_out)
        )
        await self._settle(reservations, tokens_in, tokens_out, charged)
        remaining = raw.headers.get("x-ratelimit-remaining-tokens")
        await self._recorder.record(
            run_id=run_id,
            step=step,
            role=role,
            model_id=model_id,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            cost_usd=cost,
            ratelimit_remaining=int(remaining) if remaining and remaining.isdigit() else None,
            prompt_version=_prompt_version(prompt),
        )
        message = parsed.choices[0].message if parsed.choices else None
        calls: list[ToolCall] = []
        for call in (message.tool_calls or []) if message else []:
            function = getattr(call, "function", None)  # custom tool calls carry no function
            if function is not None:
                calls.append(ToolCall(id=call.id, name=function.name, arguments=function.arguments))
        return Completion(
            content=message.content if message else None,
            tool_calls=calls,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model_id=model_id,
            latency_ms=latency_ms,
        )

    async def _call_with_retries(
        self, req: "ChatRequest", tally: _Tally
    ) -> LegacyAPIResponse[ChatCompletion]:
        retrying = AsyncRetrying(
            stop=stop_after_attempt(self.config.provider.max_retries),
            wait=_wait,
            retry=retry_if_exception(_retryable),
            reraise=True,
            sleep=self._sleep or asyncio.sleep,
        )
        create = self._openai.chat.completions.with_raw_response.create
        try:
            async for attempt in retrying:
                with attempt:
                    try:
                        return await create(
                            model=req.model,
                            messages=cast("list[ChatCompletionMessageParam]", req.messages),
                            max_tokens=req.max_tokens,
                            temperature=req.temperature,
                            timeout=req.timeout,
                            tools=cast("list[ChatCompletionToolParam]", req.tools) or omit,
                            tool_choice=(
                                cast("ChatCompletionToolChoiceOptionParam", req.tool_choice)
                                if req.tool_choice is not None
                                else omit
                            ),
                            response_format=(
                                cast("ResponseFormat", req.response_format)
                                if req.response_format is not None
                                else omit
                            ),
                            extra_body=req.extra_body or None,
                        )
                    except openai.APITimeoutError:
                        tally.timeouts += 1  # the provider may have generated (and billed) it all
                        raise
                    except openai.APIConnectionError as err:
                        if not isinstance(err.__cause__, httpx.ConnectError):
                            tally.timeouts += 1  # lost after sending: it may have been generated
                        raise  # a connection that never opened sent nothing: not billed
                    except openai.RateLimitError:
                        raise  # refused before any work: nothing billed
                    except openai.APIStatusError as err:
                        if err.status_code in _GATEWAY_TIMEOUTS:
                            tally.timeouts += 1  # the upstream timed out, possibly after generating
                        elif err.status_code >= 500:
                            tally.attempts += 1  # 5xx: the input may have been processed
                        raise  # a 4xx is refused before any work
        except Exception as err:
            if _retryable(err):
                raise _Exhausted(tally.attempts, tally.timeouts) from err
            raise _Failed(tally.attempts, tally.timeouts) from err  # the retries before count
        raise _Exhausted(tally.attempts, tally.timeouts)  # pragma: no cover - returns or raises


@dataclass(frozen=True)
class ChatRequest:
    model: str
    messages: list[dict[str, object]]
    max_tokens: int
    temperature: float
    timeout: float
    tools: list[dict[str, object]]
    tool_choice: ToolChoice | None
    response_format: dict[str, object] | None
    extra_body: dict[str, object]

    def body(self) -> dict[str, object]:
        """The JSON body of POST /chat/completions (the Batch API sends it as is)."""
        body: dict[str, object] = {
            "model": self.model,
            "messages": self.messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        if self.tools:
            body["tools"] = self.tools
        if self.tool_choice is not None:
            body["tool_choice"] = self.tool_choice
        if self.response_format is not None:
            body["response_format"] = self.response_format
        return {**body, **self.extra_body}


def chat_request(
    config: ModelsConfig,
    resolved: Mapping[Role, ResolvedModel],
    role: Role,
    messages: list[Message],
    *,
    tools: list[ToolDef] | None = None,
    tool_choice: ToolChoice | None = None,
    max_output: int | None = None,
    response_format: dict[str, object] | None = None,
) -> ChatRequest:
    """The request for `role`: its routed model, settings and reasoning switch. Shared by the
    live client and the Batch API (app/models/batch.py), so both send the same thing."""
    spec = config.roles[role]
    payload = [m.model_dump(exclude_none=True) for m in messages]
    extra_body: dict[str, object] = {}
    toggle = config.provider.reasoning_toggle
    if spec.reasoning != "default":
        on = spec.reasoning == "on"
        if toggle == "chat_template_kwargs":
            extra_body["chat_template_kwargs"] = {"enable_thinking": on}
        elif toggle == "system_flag":
            payload.insert(0, {"role": "system", "content": "/think" if on else "/no_think"})
    return ChatRequest(
        model=resolved[role].model_id,
        messages=payload,
        max_tokens=max_output or spec.max_output,
        temperature=spec.temperature,
        timeout=spec.timeout_s or config.provider.timeout_s,
        tools=[{"type": "function", "function": t.model_dump()} for t in tools or []],
        tool_choice=tool_choice,
        response_format=response_format,
        extra_body=extra_body,
    )


def _prompt_version(prompt: str | None) -> str | None:
    if prompt is None:
        return None
    from app.models.prompt_files import load_prompt

    try:
        return load_prompt(prompt).version
    except FileNotFoundError:
        return None

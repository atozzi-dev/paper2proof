"""Token Factory Batch API, eval only (docs/specs/model-routing.md, "Batch (eval only)").

Builds a JSONL file with one chat completion per line and `custom_id = <case_id>:<role>`,
uploads it with the Files API, creates a batch, polls it, downloads the output and error files
and maps the results back by `custom_id`. Each line's body is built by the same function the
live client uses (`chat_request`), so a batch sends exactly what a live call would.

The spec leaves the wire format open; this follows the OpenAI Batch API, which Token Factory
documents as compatible: input lines `{custom_id, method, url, body}`, output lines
`{id, custom_id, response: {status_code, request_id, body}, error}`. The parser also accepts a
bare completion under `response` and a top-level `error` object. Unverified against the live
service until the first paid run (docs/FEEDBACK.md).

Budget (invariant 7): the whole batch is reserved before the file is uploaded, at the batch
price (`batch_discount` in config/pricing.yaml), and the actual usage is committed afterwards.
"""

import asyncio
import contextlib
import json
import math
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

import openai
from openai.types import Batch
from openai.types.chat import ChatCompletion, ChatCompletionMessageFunctionToolCall
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.domain.errors import P2PError
from app.domain.models import ErrorCode
from app.models.client import (
    CallRecorder,
    Completion,
    Message,
    ModelsConfig,
    ResolvedModel,
    Role,
    ToolCall,
    ToolChoice,
    ToolDef,
    _prompt_version,
    chat_request,
)
from app.models.pricing import Pricing, estimate_message_tokens, estimate_tokens
from app.orchestrator.budget import Budget, Reservation, SpendKind

BATCH_ENDPOINT: Literal["/v1/chat/completions"] = "/v1/chat/completions"
COMPLETION_WINDOW: Literal["24h"] = "24h"
TERMINAL = frozenset({"completed", "failed", "expired", "cancelled"})
_CASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
_SAFE_CODE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")  # provider error codes we may echo


def custom_id(case_id: str, role: Role) -> str:
    if not _CASE_ID.fullmatch(case_id) or ".." in case_id:
        raise ValueError(f"invalid case_id {case_id!r}: [A-Za-z0-9._/-], at most 100 characters")
    return f"{case_id}:{role.value}"


def parse_custom_id(value: str) -> tuple[str, Role]:
    case_id, sep, role = value.rpartition(":")
    if not sep or role not in {r.value for r in Role}:
        raise ValueError(f"invalid custom_id {value!r}: expected <case_id>:<role>")
    try:
        custom_id(case_id, Role(role))
    except ValueError:
        raise ValueError(f"invalid custom_id {value!r}: bad case_id") from None
    return case_id, Role(role)


# --- builder ---------------------------------------------------------------------------------


def _messages(body: dict[str, object]) -> list[dict[str, object]]:
    """The request body's messages, for the token estimate (images at a fixed count)."""
    messages = body.get("messages")
    if not isinstance(messages, list):
        return []
    return [m for m in messages if isinstance(m, dict)]


class BatchRequest(BaseModel):
    """One chat completion of the batch, as the eval asks for it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    role: Role
    messages: list[Message] = Field(min_length=1)
    tools: list[ToolDef] | None = None
    tool_choice: ToolChoice | None = None
    response_format: dict[str, object] | None = None
    max_output: int | None = Field(default=None, gt=0)
    prompt: str | None = None  # prompt file name, for the prompt version in the ledger

    @field_validator("case_id")
    @classmethod
    def _valid_case_id(cls, value: str) -> str:
        custom_id(value, Role.READER)
        return value

    @property
    def custom_id(self) -> str:
        return custom_id(self.case_id, self.role)


class BatchLine(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    custom_id: str
    method: Literal["POST"] = "POST"
    url: Literal["/v1/chat/completions"] = BATCH_ENDPOINT
    body: dict[str, object]

    @field_validator("custom_id")
    @classmethod
    def _valid_custom_id(cls, value: str) -> str:
        parse_custom_id(value)
        return value


def build_lines(
    requests: Sequence[BatchRequest], config: ModelsConfig, resolved: Mapping[Role, ResolvedModel]
) -> list[BatchLine]:
    return [
        BatchLine(
            custom_id=r.custom_id,
            body=chat_request(
                config,
                resolved,
                r.role,
                r.messages,
                tools=r.tools,
                tool_choice=r.tool_choice,
                max_output=r.max_output,
                response_format=r.response_format,
            ).body(),
        )
        for r in requests
    ]


def to_jsonl(lines: Sequence[BatchLine]) -> bytes:
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        if line.custom_id in seen:
            raise ValueError(f"duplicate custom_id {line.custom_id!r}")
        seen.add(line.custom_id)
        out.append(json.dumps(line.model_dump(mode="json"), ensure_ascii=False) + "\n")
    return "".join(out).encode("utf-8")


def _objects(data: bytes) -> list[dict[str, object]]:
    objects: list[dict[str, object]] = []
    for number, raw in enumerate(data.decode("utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            raise ValueError(f"line {number} is not JSON") from None
        if not isinstance(value, dict):
            raise ValueError(f"line {number} is not a JSON object")
        objects.append(value)
    return objects


def read_jsonl(data: bytes) -> list[BatchLine]:
    return [BatchLine.model_validate(obj) for obj in _objects(data)]


# --- parser ----------------------------------------------------------------------------------


class BatchResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    custom_id: str
    case_id: str
    role: Role
    completion: Completion | None
    usage_known: bool = False  # False: the provider sent no usage (charged at the estimate)
    error: str | None = None  # status and provider error code only, never provider text


def _code(error: object) -> str | None:
    if isinstance(error, dict):
        code = error.get("code") or error.get("type")
        if isinstance(code, str) and _SAFE_CODE.fullmatch(code):
            return code
    return None


def _completion(body: object) -> tuple[Completion, bool] | None:
    try:
        parsed = ChatCompletion.model_validate(body)
    except ValidationError:
        return None
    message = parsed.choices[0].message if parsed.choices else None
    calls = [
        ToolCall(id=c.id, name=c.function.name, arguments=c.function.arguments)
        for c in (message.tool_calls or [] if message else [])
        if isinstance(c, ChatCompletionMessageFunctionToolCall)  # custom tool calls: no function
    ]
    usage = parsed.usage
    completion = Completion(
        content=message.content if message else None,
        tool_calls=calls,
        tokens_in=usage.prompt_tokens if usage else 0,
        tokens_out=usage.completion_tokens if usage else 0,
        model_id=parsed.model,
        latency_ms=0,  # a batch has no per-request latency
    )
    return completion, usage is not None


def _result(obj: Mapping[str, object]) -> BatchResult:
    cid = obj.get("custom_id")
    if not isinstance(cid, str):
        raise ValueError("a result line has no custom_id")
    case_id, role = parse_custom_id(cid)
    response = obj.get("response")
    status, body = None, None
    if isinstance(response, dict):
        if "status_code" in response:
            raw_status = response.get("status_code")
            status = raw_status if isinstance(raw_status, int) else None
            body = response.get("body")
        elif "choices" in response:
            status, body = 200, response
    if status is not None and 200 <= status < 300:
        parsed = _completion(body)
        if parsed is None:
            return _failed(cid, "malformed response body")
        completion, usage_known = parsed
        return BatchResult(
            custom_id=cid,
            case_id=case_id,
            role=role,
            completion=completion,
            usage_known=usage_known,
        )
    if status is not None:
        code = _code(body.get("error") if isinstance(body, dict) else None)
        return _failed(cid, f"HTTP {status}" + (f" {code}" if code else ""))
    return _failed(cid, _code(obj.get("error")) or "batch error")


def _failed(cid: str, error: str) -> BatchResult:
    case_id, role = parse_custom_id(cid)
    return BatchResult(custom_id=cid, case_id=case_id, role=role, completion=None, error=error)


def parse_output(output: bytes, errors: bytes = b"") -> dict[str, BatchResult]:
    """Results of the output and error files by custom_id; a custom_id seen twice is an error."""
    results: dict[str, BatchResult] = {}
    for obj in [*_objects(output), *_objects(errors)]:
        result = _result(obj)
        if result.custom_id in results:
            raise ValueError(f"duplicate custom_id {result.custom_id!r} in the batch results")
        results[result.custom_id] = result
    return results


# --- runner ----------------------------------------------------------------------------------


class BatchOutcome(BaseModel):
    batch_id: str
    status: str
    results: dict[str, BatchResult]
    missing: list[str]  # sent, but in neither file (an expired or cancelled batch)
    tokens_in: int
    tokens_out: int
    cost_usd: float | None  # priced usage at the batch price; None if a model has no price
    charged_usd: float  # what the budget was charged (conservative for unpriced/missing lines)


@dataclass(frozen=True)
class _Estimate:
    model_id: str
    role: Role
    prompt: str | None
    tokens_in: int
    tokens_out: int


class BatchRunner:
    def __init__(
        self,
        client: openai.AsyncOpenAI,
        config: ModelsConfig,
        resolved: Mapping[Role, ResolvedModel],
        pricing: Pricing,
        budget: Budget,
        recorder: CallRecorder,
        *,
        poll_s: float = 60.0,
        timeout_s: float = 26 * 3600.0,  # the 24 h completion window plus slack
        step: str = "EVAL_BATCH",
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._openai = client
        self.config = config
        self.resolved = dict(resolved)
        self.pricing = pricing
        self.budget = budget
        self._recorder = recorder
        self._poll_s = poll_s
        self._timeout_s = timeout_s
        self._step = step
        self._sleep = sleep or asyncio.sleep

    def _price(self, model_id: str, tokens_in: int, tokens_out: int) -> float:
        return self.pricing.estimate(model_id, tokens_in, tokens_out) * self.pricing.batch_discount

    async def _reserve(self, est: Sequence[_Estimate]) -> list[Reservation]:
        tokens_in = sum(e.tokens_in for e in est)
        tokens_out = sum(e.tokens_out for e in est)
        usd = math.fsum(self._price(e.model_id, e.tokens_in, e.tokens_out) for e in est)
        reservations: list[Reservation] = []
        try:
            for kind, amount in (
                (SpendKind.TOKENS_IN, float(tokens_in)),
                (SpendKind.TOKENS_OUT, float(tokens_out)),
                (SpendKind.INFERENCE_USD, usd),
            ):
                reservations.append(await self.budget.check_and_reserve(None, kind, amount))
        except BaseException:
            await self._settle(reservations, 0, 0, 0.0)
            raise
        return reservations

    async def _settle(
        self, reservations: list[Reservation], tokens_in: int, tokens_out: int, usd: float
    ) -> None:
        actual = {
            SpendKind.TOKENS_IN: float(tokens_in),
            SpendKind.TOKENS_OUT: float(tokens_out),
            SpendKind.INFERENCE_USD: usd,
        }
        await self.budget.settle(reservations, actual)

    async def _settle_in_full(self, reservations: list[Reservation]) -> None:
        await self.budget.settle(reservations)

    async def _wait(self, batch: Batch) -> Batch:
        waited = 0.0
        while batch.status not in TERMINAL:
            if waited >= self._timeout_s:
                raise TimeoutError
            await self._sleep(self._poll_s)
            waited += self._poll_s
            batch = await self._openai.batches.retrieve(batch.id)
        return batch

    async def _download(self, file_id: str | None) -> bytes:
        if not file_id:
            return b""
        content = await self._openai.files.content(file_id)
        return content.content

    async def run(self, requests: Sequence[BatchRequest]) -> BatchOutcome:
        if not requests:
            raise ValueError("an empty batch")
        lines = build_lines(requests, self.config, self.resolved)
        data = to_jsonl(lines)  # duplicate custom_ids fail here, before any spend
        estimates: dict[str, _Estimate] = {}
        for request, line in zip(requests, lines, strict=True):
            body = line.body
            estimates[line.custom_id] = _Estimate(
                model_id=self.resolved[request.role].model_id,
                role=request.role,
                prompt=request.prompt,
                tokens_in=estimate_message_tokens(_messages(body))
                + estimate_tokens(str(body.get("tools", []))),
                tokens_out=int(str(body["max_tokens"])),
            )
        reservations = await self._reserve(list(estimates.values()))
        try:
            uploaded = await self._openai.files.create(
                file=("batch.jsonl", data, "application/jsonl"), purpose="batch"
            )
            batch = await self._openai.batches.create(
                input_file_id=uploaded.id,
                endpoint=BATCH_ENDPOINT,
                completion_window=COMPLETION_WINDOW,
            )
        except openai.APIStatusError as err:
            if err.status_code >= 500:  # the batch may exist anyway: charge it in full
                await self._settle_in_full(reservations)
            else:  # rejected (4xx): no batch was created, nothing billed
                await self._settle(reservations, 0, 0, 0.0)
            raise
        except BaseException:
            # A timeout or a dropped connection: the create may have succeeded on the server
            # side. The client has no retries (a retry could start a second, untracked batch),
            # so the one batch that may run is charged in full.
            await self._settle_in_full(reservations)
            raise
        try:
            batch = await self._wait(batch)
            output = await self._download(batch.output_file_id)
            errors = await self._download(batch.error_file_id)
            results = {
                cid: r for cid, r in parse_output(output, errors).items() if cid in estimates
            }
        except BaseException as err:
            # The batch may still run and be billed: cancel it and charge the whole estimate.
            with contextlib.suppress(Exception):  # best effort: the charge below is what matters
                await self._openai.batches.cancel(batch.id)
            await self._settle_in_full(reservations)
            if isinstance(err, TimeoutError):
                raise P2PError(
                    ErrorCode.TIMEOUT, f"batch {batch.id} did not finish in time"
                ) from None
            raise
        return await self._account(batch, results, estimates, reservations)

    async def _account(
        self,
        batch: Batch,
        results: dict[str, BatchResult],
        estimates: Mapping[str, _Estimate],
        reservations: list[Reservation],
    ) -> BatchOutcome:
        missing = [cid for cid in estimates if cid not in results]
        tokens_in = tokens_out = 0
        priced: list[float | None] = []
        charged: list[float] = []
        calls: list[tuple[_Estimate, int, int, float | None]] = []
        for cid in missing:  # possibly processed and billed for its input: charge that
            est = estimates[cid]
            tokens_in += est.tokens_in
            charged.append(self._price(est.model_id, est.tokens_in, 0))
        for cid, result in results.items():
            est = estimates[cid]
            if result.completion is None:
                continue  # a failed request is not billed
            completion = result.completion
            tin, tout = (
                (completion.tokens_in, completion.tokens_out)
                if result.usage_known
                else (est.tokens_in, est.tokens_out)
            )
            tokens_in += tin
            tokens_out += tout
            list_price = self.pricing.cost(est.model_id, tin, tout)
            cost = None if list_price is None else list_price * self.pricing.batch_discount
            priced.append(cost)
            charged.append(cost if cost is not None else self._price(est.model_id, tin, tout))
            calls.append((est, tin, tout, cost))
        total = math.fsum(charged)
        await self._settle(reservations, tokens_in, tokens_out, total)  # before anything can fail
        for est, tin, tout, cost in calls:
            await self._recorder.record(
                run_id=None,
                step=self._step,
                role=est.role,
                model_id=est.model_id,
                tokens_in=tin,
                tokens_out=tout,
                latency_ms=0,
                cost_usd=cost,
                ratelimit_remaining=None,
                prompt_version=_prompt_version(est.prompt),
            )
        return BatchOutcome(
            batch_id=batch.id,
            status=batch.status,
            results=results,
            missing=missing,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=None
            if any(c is None for c in priced)
            else math.fsum(c or 0.0 for c in priced),
            charged_usd=total,
        )

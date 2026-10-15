"""Structured output (model-routing spec): json_schema or a forced `emit_result` tool, then
pydantic validation, one Utility repair, one retry on the original role, else SCHEMA_ERROR."""

import json
import re
from typing import Literal, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from app.domain.errors import P2PError
from app.domain.models import ErrorCode
from app.models.client import Completion, Message, Role, ToolChoice, ToolDef
from app.models.prompt_files import load_prompt
from app.models.untrusted import UNTRUSTED_NOTICE, wrap_untrusted

T = TypeVar("T", bound=BaseModel)
EMIT = "emit_result"
_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


class _Completer(Protocol):
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


class _Unparseable(Exception):
    def __init__(self, raw: str, error: str) -> None:
        super().__init__(error)
        self.raw = raw
        self.error = error


def _strip_fences(text: str) -> str:
    return _FENCE.sub("", text).strip()


def _parse[M: BaseModel](
    completion: Completion, schema: type[M], mode: Literal["json_schema", "tool"]
) -> M:
    if mode == "tool":
        calls = [c for c in completion.tool_calls if c.name == EMIT]
        if not calls:
            raise _Unparseable(completion.content or "", f"no {EMIT} tool call in the response")
        raw = calls[0].arguments
    else:
        raw = _strip_fences(completion.content or "")
    try:
        return schema.model_validate_json(raw)
    except ValidationError as err:
        raise _Unparseable(raw, _describe(err)) from None


_SAFE_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,40}$")


def _describe(err: ValidationError) -> str:
    """Validation errors go back into prompts, so they carry only our vocabulary: pydantic's
    error type and a path whose keys are identifiers. Keys the model invented (extra fields)
    are never echoed: they may carry text injected by the paper (invariant 6)."""
    parts = []
    for e in err.errors()[:10]:
        if e["type"] == "extra_forbidden":
            path = "<unexpected field>"
        else:
            keys = [
                str(p) if isinstance(p, int) or _SAFE_KEY.match(str(p)) else "?" for p in e["loc"]
            ]
            path = ".".join(keys) or "<root>"
        parts.append(f"{path}: {e['type']}")
    return "; ".join(parts)


async def _ask(
    client: _Completer,
    role: Role,
    messages: list[Message],
    schema: type[BaseModel],
    mode: Literal["json_schema", "tool"],
    run_id: str | None,
    step: str | None,
    prompt: str | None,
) -> Completion:
    json_schema = schema.model_json_schema()
    if mode == "tool":
        tool = ToolDef(name=EMIT, description="Return the result.", parameters=json_schema)
        choice: ToolChoice = {"type": "function", "function": {"name": EMIT}}
        return await client.complete(
            role,
            messages,
            tools=[tool],
            tool_choice=choice,
            run_id=run_id,
            step=step,
            prompt=prompt,
        )
    response_format: dict[str, object] = {
        "type": "json_schema",
        "json_schema": {"name": schema.__name__, "schema": json_schema},
    }
    return await client.complete(
        role, messages, response_format=response_format, run_id=run_id, step=step, prompt=prompt
    )


async def structured_call[M: BaseModel](
    client: _Completer,
    role: Role,
    messages: list[Message],
    schema: type[M],
    *,
    mode: Literal["json_schema", "tool"],
    run_id: str | None = None,
    step: str | None = None,
    prompt: str | None = None,
) -> M:
    first = await _ask(client, role, messages, schema, mode, run_id, step, prompt)
    try:
        return _parse(first, schema, mode)
    except _Unparseable as bad:
        failure = bad

    repair = load_prompt("utility_repair_json")
    user = repair.render(
        schema=json.dumps(schema.model_json_schema()),
        error=failure.error,
        broken=wrap_untrusted(failure.raw, source="model-output", max_chars=20_000),
    )
    fixed = await client.complete(
        Role.UTILITY,
        [Message(role="system", content=UNTRUSTED_NOTICE), Message(role="user", content=user)],
        run_id=run_id,
        step=step,
        prompt=repair.name,
    )
    try:
        return schema.model_validate_json(_strip_fences(fixed.content or ""))
    except ValidationError:
        pass

    retry = [
        *messages,
        Message(
            role="user",
            content=(
                f"Your previous output failed validation: {failure.error}. "
                "Return a result that matches the schema exactly."
            ),
        ),
    ]
    second = await _ask(client, role, retry, schema, mode, run_id, step, prompt)
    try:
        return _parse(second, schema, mode)
    except _Unparseable:
        raise P2PError(
            ErrorCode.SCHEMA_ERROR, f"model output did not match {schema.__name__}"
        ) from None

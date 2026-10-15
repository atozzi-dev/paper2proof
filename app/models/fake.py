"""Deterministic ModelClient for tests and the `test` profile.

Fixtures: <root>/<role>/<prompt>/<case>.json, a list of responses consumed in order:
  [{"content": "...", "tool_calls": [{"id": "...", "name": "...", "arguments": "{...}"}]}, ...]
"""

import json
from collections import defaultdict
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from app.models.client import Completion, Message, Role, ToolCall, ToolChoice, ToolDef

T = TypeVar("T", bound=BaseModel)


class FakeModelClient:
    def __init__(self, root: Path, case: str = "default") -> None:
        self.root = root
        self.case = case
        self.calls: list[tuple[Role, str, str]] = []
        self._cursor: dict[tuple[Role, str, str], int] = defaultdict(int)

    def _next(self, role: Role, prompt: str | None) -> Completion:
        name = prompt or "default"
        key = (role, name, self.case)
        path = self.root / role.value / name / f"{self.case}.json"
        if not path.is_file():
            raise FileNotFoundError(f"no LLM fixture {role.value}/{name}/{self.case}.json")
        script = json.loads(path.read_text(encoding="utf-8"))
        index = self._cursor[key]
        if index >= len(script):
            raise IndexError(f"LLM fixture {path.name} exhausted after {len(script)} responses")
        self._cursor[key] += 1
        self.calls.append(key)
        entry = script[index]
        return Completion(
            content=entry.get("content"),
            tool_calls=[ToolCall.model_validate(c) for c in entry.get("tool_calls", [])],
            tokens_in=int(entry.get("tokens_in", 0)),
            tokens_out=int(entry.get("tokens_out", 0)),
            model_id=f"fake/{role.value}",
            latency_ms=0,
        )

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
        return self._next(role, prompt)

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
        completion = self._next(role, prompt)
        if completion.tool_calls:
            return schema.model_validate_json(completion.tool_calls[0].arguments)
        return schema.model_validate_json(completion.content or "")

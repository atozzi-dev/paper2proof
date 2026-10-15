"""Spike S2: Token Factory model IDs, context windows, tool calling, JSON Schema output, reasoning.

Run:   uv run python spikes/s2_models.py [--only super,ultra,nano,omni] [--skip-long]
Needs: NEBIUS_API_KEY (Token Factory key).  Cost: ~40 short calls, well under $0.10.

Prints every model ID, resolves each role in config/models.yaml (exactly one match expected),
then probes Super, Ultra, Nano and Omni: forced tool call, response_format=json_schema,
reasoning toggles (system flag vs chat_template_kwargs), latency and usage of a ~1k-token prompt.
Ends with `VERDICT: PASS|FAIL|PARTIAL`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from spikelib import Report, http, redact, require_env

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE = "https://api.tokenfactory.nebius.com/v1/"
# Families probed in depth, mapped to the role whose `match` finds them in config/models.yaml.
FAMILIES = {"super": "reader", "ultra": "planner", "nano": "utility", "omni": "vision"}
TINY_PNG = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8Dw"
    "HwAFBQIAX8jx0gAAAABJRU5ErkJggg=="
)
WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
            "additionalProperties": False,
        },
    },
}
CLAIM_SCHEMA = {
    "name": "claim",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "metric": {"type": "string"},
            "value": {"type": "number"},
            "higher_is_better": {"type": "boolean"},
        },
        "required": ["metric", "value", "higher_is_better"],
        "additionalProperties": False,
    },
}
CLAIM_TEXT = "Our model reaches 94.1% top-1 accuracy on CIFAR-10. Extract the claim."


class Client:
    def __init__(self, base: str, key: str) -> None:
        self.base = base if base.endswith("/") else base + "/"
        self.headers = {"Authorization": f"Bearer {key}"}

    def chat(self, model: str, messages: list[dict[str, Any]], **extra: Any) -> tuple[Any, Any]:
        body = {"model": model, "messages": messages, "max_tokens": 400, **extra}
        resp = http(
            "POST", self.base + "chat/completions", headers=self.headers, body=body, timeout=300
        )
        return resp, resp.json()


def load_roles() -> dict[str, dict[str, Any]]:
    import yaml  # project dependency; imported lazily so --help works anywhere

    cfg = yaml.safe_load((ROOT / "config" / "models.yaml").read_text(encoding="utf-8"))
    return dict(cfg["roles"])


def message(data: Any) -> dict[str, Any]:
    try:
        return dict(data["choices"][0]["message"])
    except (KeyError, IndexError, TypeError):
        return {}


def usage(data: Any) -> str:
    u = (data or {}).get("usage") or {}
    return f"in={u.get('prompt_tokens')} out={u.get('completion_tokens')}"


def probe_tools(c: Client, model: str, r: Report, fam: str) -> None:
    resp, data = c.chat(
        model,
        [{"role": "user", "content": "What's the weather in Rome? Use the tool."}],
        tools=[WEATHER_TOOL],
        tool_choice={"type": "function", "function": {"name": "get_weather"}},
    )
    calls = message(data).get("tool_calls") or []
    if resp.status == 200 and calls:
        args = calls[0].get("function", {}).get("arguments", "")
        r.fact(f"{fam}: forced tool call OK (args={args!r}, {resp.latency_ms} ms)")
        return
    resp2, data2 = c.chat(
        model,
        [{"role": "user", "content": "What's the weather in Rome? Use the tool."}],
        tools=[WEATHER_TOOL],
        tool_choice="auto",
    )
    calls2 = message(data2).get("tool_calls") or []
    detail = f"forced: HTTP {resp.status} {resp.text(200)!r}; auto: HTTP {resp2.status}"
    if calls2:
        r.partial(f"{fam}: tool calling works only with tool_choice=auto ({detail})")
    else:
        r.partial(f"{fam}: no tool call ({detail})")


def probe_json_schema(c: Client, model: str, r: Report, fam: str) -> None:
    resp, data = c.chat(
        model,
        [{"role": "user", "content": CLAIM_TEXT}],
        response_format={"type": "json_schema", "json_schema": CLAIM_SCHEMA},
    )
    content = message(data).get("content") or ""
    try:
        parsed = json.loads(content)
        ok = set(parsed) == {"metric", "value", "higher_is_better"}
    except (json.JSONDecodeError, TypeError):
        parsed, ok = None, False
    if resp.status == 200 and ok:
        r.fact(f"{fam}: response_format=json_schema OK -> {parsed}")
    else:
        r.partial(
            f"{fam}: json_schema not usable (HTTP {resp.status}, content={content[:120]!r}) "
            "-> structured_output: tool"
        )


def probe_reasoning(c: Client, model: str, r: Report, fam: str) -> None:
    question = [{"role": "user", "content": "Is 391 prime? Answer yes or no."}]
    variants: dict[str, tuple[list[dict[str, Any]], dict[str, Any]]] = {
        "default": (question, {}),
        "system /no_think": ([{"role": "system", "content": "/no_think"}, *question], {}),
        "system /think": ([{"role": "system", "content": "/think"}, *question], {}),
        "system 'detailed thinking off'": (
            [{"role": "system", "content": "detailed thinking off"}, *question],
            {},
        ),
        "chat_template_kwargs enable_thinking=false": (
            question,
            {"chat_template_kwargs": {"enable_thinking": False}},
        ),
        "chat_template_kwargs enable_thinking=true": (
            question,
            {"chat_template_kwargs": {"enable_thinking": True}},
        ),
    }
    for label, (msgs, extra) in variants.items():
        resp, data = c.chat(model, msgs, **extra)
        msg = message(data)
        content = msg.get("content") or ""
        thinking = (
            bool(msg.get("reasoning_content") or msg.get("reasoning")) or "<think>" in content
        )
        r.fact(
            f"{fam}: reasoning[{label}] HTTP {resp.status} thinking={thinking} {usage(data)} "
            f"{resp.latency_ms} ms"
        )


def probe_long_prompt(c: Client, model: str, r: Report, fam: str) -> None:
    filler = " ".join(f"Sentence {i} describes an ablation on a small CNN." for i in range(110))
    resp, data = c.chat(
        model,
        [{"role": "user", "content": filler + "\nSummarize in one sentence."}],
        max_tokens=60,
    )
    limits = {k: v for k, v in resp.headers.items() if k.lower().startswith("x-ratelimit")}
    r.fact(f"{fam}: ~1k-token prompt HTTP {resp.status} {usage(data)} {resp.latency_ms} ms")
    if limits:
        r.fact(f"{fam}: rate-limit headers {limits}")


def probe_vision(c: Client, model: str, r: Report) -> None:
    content = [
        {"type": "text", "text": "What colour is this single pixel? One word."},
        {"type": "image_url", "image_url": {"url": TINY_PNG}},
    ]
    resp, data = c.chat(model, [{"role": "user", "content": content}], max_tokens=20)
    answer = (message(data).get("content") or "")[:60]
    if resp.status == 200:
        r.fact(f"omni: image input accepted -> {answer!r}")
    else:
        r.partial(f"omni: image input rejected HTTP {resp.status} {resp.text(200)!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    parser.add_argument("--only", default="super,ultra,nano,omni", help="families to probe")
    parser.add_argument("--skip-long", action="store_true", help="skip the ~1k-token prompt")
    args = parser.parse_args()

    env = require_env("NEBIUS_API_KEY")
    c = Client(args.base_url, env["NEBIUS_API_KEY"])
    r = Report()

    print("== GET /models")
    resp = http("GET", c.base + "models", headers=c.headers)
    if resp.status != 200:
        r.fail(f"GET /models -> HTTP {resp.status} {resp.text(300)!r}")
        r.summary()
        print("VERDICT: FAIL")
        return 1
    models = (resp.json() or {}).get("data", [])
    for m in models:
        extra = {k: v for k, v in m.items() if "context" in k or "max" in k}
        print(f"  {m.get('id')} {redact(json.dumps(extra)) if extra else ''}")
    r.fact(f"{len(models)} models listed")
    ids = [str(m.get("id")) for m in models]

    print("\n== Role resolution (config/models.yaml)")
    resolved: dict[str, str] = {}
    for role, spec in load_roles().items():
        if spec.get("exact_id"):
            hits = [i for i in ids if i == spec["exact_id"]]
        else:
            hits = [i for i in ids if str(spec["match"]).lower() in i.lower()]
        if len(hits) == 1:
            resolved[role] = hits[0]
            ctx = next((m for m in models if m.get("id") == hits[0]), {})
            ctx_len = {k: v for k, v in ctx.items() if "context" in k}
            r.fact(f"role {role} -> {hits[0]} {ctx_len or '(context length not exposed)'}")
        else:
            r.fail(f"role {role}: match {spec.get('match')!r} hit {len(hits)} models: {hits}")

    for fam in [f.strip() for f in args.only.split(",") if f.strip()]:
        model = resolved.get(FAMILIES.get(fam, ""), "")
        if not model:
            r.fail(f"{fam}: no resolved model, skipping probes")
            continue
        print(f"\n== {fam}: {model}")
        if fam == "omni":
            probe_vision(c, model, r)
        probe_tools(c, model, r, fam)
        probe_json_schema(c, model, r, fam)
        probe_reasoning(c, model, r, fam)
        if not args.skip_long:
            probe_long_prompt(c, model, r, fam)

    verdict = r.summary()
    print(f"VERDICT: {verdict}")
    return 1 if verdict == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main())

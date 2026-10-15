"""Shared helpers for the Python spike scripts. Standard library only.

Never print credentials: `redact()` masks every secret-looking env value before output.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

SECRET_ENV = ("NEBIUS_API_KEY", "TAVILY_API_KEY", "HF_TOKEN", "NEBIUS_IAM_TOKEN")


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes
    latency_ms: int

    def json(self) -> Any:
        try:
            return json.loads(self.body or b"null")
        except json.JSONDecodeError:
            return None

    def text(self, limit: int = 2000) -> str:
        return redact(self.body.decode("utf-8", errors="replace")[:limit])


@dataclass
class Report:
    """Collects facts and the worst outcome seen; prints them at the end."""

    facts: list[str] = field(default_factory=list)
    outcome: str = "PASS"

    def fact(self, line: str) -> None:
        self.facts.append(line)
        print(f"  - {redact(line)}", flush=True)

    def partial(self, line: str) -> None:
        self.fact(f"PARTIAL: {line}")
        if self.outcome == "PASS":
            self.outcome = "PARTIAL"

    def fail(self, line: str) -> None:
        self.fact(f"FAIL: {line}")
        self.outcome = "FAIL"

    def summary(self) -> str:
        print("\nKEY FACTS")
        for line in self.facts:
            print(f"  {redact(line)}")
        return self.outcome


def redact(text: str) -> str:
    for name in SECRET_ENV:
        value = os.environ.get(name, "")
        if len(value) >= 6:
            text = text.replace(value, f"<{name}>")
    return text


def require_env(*names: str) -> dict[str, str]:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        print(f"Missing environment variables: {', '.join(missing)} (see spikes/README.md)")
        print("VERDICT: FAIL")
        sys.exit(2)
    return {n: os.environ[n] for n in names}


def http(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: Any = None,
    timeout: float = 60,
) -> Response:
    """One HTTP call that never raises on HTTP errors (status 0 = transport error)."""
    data = None
    hdrs = {"User-Agent": "paper2proof-spike/0.1", **(headers or {})}
    auth = hdrs.pop("Authorization", None)
    if body is not None:
        data = json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(url, data=data, method=method, headers=hdrs)  # noqa: S310
    if auth:
        # Unredirected: urllib must not forward credentials to a redirect target.
        request.add_unredirected_header("Authorization", auth)
    start = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310
            payload = resp.read()
            status, resp_headers = resp.status, dict(resp.headers.items())
    except urllib.error.HTTPError as err:
        payload, status, resp_headers = err.read(), err.code, dict(err.headers.items())
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        payload, status, resp_headers = str(err).encode(), 0, {}
    return Response(status, resp_headers, payload, int((time.monotonic() - start) * 1000))

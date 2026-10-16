"""JSON logs on stdout with run_id and trace_id, scrubbed of secrets (invariant 4).

The redaction filter sits on the root logger and on its handlers (a logger's own filters never
see records propagated from child loggers): it replaces every configured secret value and
every key-shaped string (app/bundle/secrets.py) in the message, its arguments, extra fields
and exception text (with its chained causes), and blanks fields named like prompts, messages
or paper text at any depth, at every level (invariant 6). The exception travels on as
scrubbed text only: later handlers get `exc_text`, never `exc_info`. The formatter scrubs the
finished line once more. A record that cannot be read or serialized still yields a line and
never raises into the code that logged it.
"""

import dataclasses
import json
import logging
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TextIO

from opentelemetry import trace
from pydantic import BaseModel

from app.bundle.secrets import PATTERNS, known_pattern
from app.observability.tracing import current_run, run_trace_id

REDACTED = "[REDACTED]"
UNREADABLE = "[log record unreadable]"
MAX_TEXT = 8192  # message and exception text, per line
SENSITIVE_PARTS = ("prompt", "paper_text")  # any field whose name contains these
SENSITIVE_NAMES = frozenset({"messages", "content", "completion", "response_text", "paper"})
MAX_DEPTH = 8
UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")
CHATTY_LOGGERS = ("openai", "httpx", "httpcore", "nebius", "grpc", "hpack")  # bodies at DEBUG
_STANDARD = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "message",
    "asctime",
}


class Redactor:
    """Replaces configured secret values (also JSON-escaped) and key-shaped strings."""

    def __init__(self, known: Sequence[str]) -> None:
        self._known = known_pattern([k.encode() for k in known if k])

    def __call__(self, text: str) -> str:
        data = text.encode("utf-8", "surrogatepass")
        if self._known is not None:
            data = self._known.sub(REDACTED.encode(), data)
        cleaned = PATTERNS.sub(REDACTED.encode(), data)
        return cleaned.decode("utf-8", "surrogatepass")


def _sensitive(name: object) -> bool:
    lowered = str(name).lower()
    return lowered in SENSITIVE_NAMES or any(part in lowered for part in SENSITIVE_PARTS)


def _cap(text: str) -> str:
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + " [truncated]"


def _blank(value: object, depth: int = 0) -> object:
    """A JSON-ready copy of `value`: sensitive keys blanked at any depth, models and dataclasses
    walked as dicts, long strings capped, and any other object shown by its type name only
    (its str() could hold anything)."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = dataclasses.asdict(value)
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return _cap(value)
    if depth >= MAX_DEPTH:
        return REDACTED
    if isinstance(value, dict):
        return {
            str(k): REDACTED if _sensitive(k) else _blank(v, depth + 1) for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [_blank(v, depth + 1) for v in value]
    return f"[{type(value).__name__}]"


class RedactionFilter(logging.Filter):
    def __init__(self, redact: Redactor) -> None:
        super().__init__()
        self.redact = redact

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            self._scrub(record)
        except Exception:  # never raise into the caller; keep nothing that was not scrubbed
            record.msg, record.args, record.exc_info, record.exc_text = UNREADABLE, None, None, None
            record.stack_info = None
            for name in [n for n in vars(record) if n not in _STANDARD]:
                delattr(record, name)
        return True

    def _scrub(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # arguments that don't match the format: keep the template only
            message = str(record.msg)
        record.msg, record.args = _cap(self.redact(message)), None
        if record.exc_info:  # the formatted text includes chained causes and notes
            record.exc_text = logging.Formatter().formatException(record.exc_info)
            record.exc_info = None
        if record.exc_text:
            record.exc_text = _cap(self.redact(record.exc_text))
        if record.stack_info:
            record.stack_info = _cap(self.redact(record.stack_info))
        for name, value in list(vars(record).items()):
            if name in _STANDARD:
                continue
            if _sensitive(name):
                setattr(record, name, REDACTED)
            elif isinstance(value, str):
                setattr(record, name, _cap(self.redact(value)))
            else:
                setattr(record, name, _blank(value))


class JsonFormatter(logging.Formatter):
    def __init__(self, redact: Redactor) -> None:
        super().__init__()
        self.redact = redact

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        run_id = getattr(record, "run_id", None) or current_run.get()
        span = trace.get_current_span().get_span_context()
        if span.is_valid:
            entry["trace_id"] = format(span.trace_id, "032x")
            entry["span_id"] = format(span.span_id, "016x")
        elif isinstance(run_id, str):
            entry["trace_id"] = format(run_trace_id(run_id), "032x")
        if run_id:
            entry["run_id"] = run_id
        for name, value in vars(record).items():
            if name not in _STANDARD and name not in entry:
                entry[name] = REDACTED if _sensitive(name) else _blank(value)
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            entry["exc"] = record.exc_text
        if record.stack_info:
            entry["stack"] = record.stack_info
        try:
            line = json.dumps(entry, default=str)
        except (TypeError, ValueError):  # circular or unserializable extras: keep the basics
            basics = ("ts", "level", "logger", "message", "run_id", "trace_id", "span_id", "exc")
            line = json.dumps({k: entry[k] for k in basics if k in entry}, default=str)
        return self.redact(line)


class JsonHandler(logging.StreamHandler[TextIO]):
    """The process's log handler (one, however often logging is configured)."""


def configure_logging(
    redact: Redactor, *, level: int = logging.INFO, stream: TextIO | None = None
) -> None:
    root = logging.getLogger()
    for handler in [h for h in root.handlers if isinstance(h, JsonHandler)]:
        root.removeHandler(handler)
    for old in [f for f in root.filters if isinstance(f, RedactionFilter)]:
        root.removeFilter(old)
    redaction = RedactionFilter(redact)
    handler = JsonHandler(stream or sys.stdout)
    handler.setFormatter(JsonFormatter(redact))
    root.addHandler(handler)
    root.addFilter(redaction)
    for each in root.handlers:  # also any handler installed before this one
        for old in [f for f in each.filters if isinstance(f, RedactionFilter)]:
            each.removeFilter(old)
        each.addFilter(redaction)
    root.setLevel(level)
    for name in UVICORN_LOGGERS:  # uvicorn configured its own handlers before loading the app
        uvicorn = logging.getLogger(name)
        uvicorn.handlers.clear()
        uvicorn.propagate = True
    for name in CHATTY_LOGGERS:  # e.g. OPENAI_LOG=debug would log request bodies
        chatty = logging.getLogger(name)
        chatty.setLevel(max(chatty.level, logging.INFO))


def worker_logging() -> None:
    """CPU-pool initializer: workers log JSON scrubbed of key-shaped strings. The secret values
    are not passed to them (they parse untrusted papers)."""
    configure_logging(Redactor([]))

"""JSON logs with run_id and trace_id, and the redaction filter (invariant 4, A-28)."""

import io
import json
import logging
from collections.abc import Iterator

import pytest

from app.observability import logging as p2p_logging
from app.observability.tracing import current_run, run_trace_id

KNOWN = "tf-key-0123456789abcdef"  # a configured secret value with no recognisable shape
SHAPED = "sk-" + "A" * 32  # an unknown key, caught by its shape
UVICORN = ("uvicorn", "uvicorn.error", "uvicorn.access")


@pytest.fixture
def stream() -> Iterator[io.StringIO]:
    """Configure logging into a buffer; restore the root and uvicorn loggers afterwards."""
    root = logging.getLogger()
    saved = (list(root.handlers), list(root.filters), root.level)
    loggers = [logging.getLogger(name) for name in UVICORN]
    saved_uvicorn = [(list(lg.handlers), lg.propagate) for lg in loggers]
    for lg in loggers:  # what uvicorn's own dictConfig leaves behind before the app loads
        lg.addHandler(logging.StreamHandler(io.StringIO()))
        lg.propagate = False
    buffer = io.StringIO()
    p2p_logging.configure_logging(p2p_logging.Redactor([KNOWN]), stream=buffer)
    yield buffer
    root.handlers[:], root.filters[:] = saved[0], saved[1]
    root.setLevel(saved[2])
    for lg, (handlers, propagate) in zip(loggers, saved_uvicorn, strict=True):
        lg.handlers[:] = handlers
        lg.propagate = propagate


def _lines(buffer: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in buffer.getvalue().splitlines()]


def test_a_fake_api_key_is_removed_from_message_and_args(stream: io.StringIO) -> None:
    log = logging.getLogger("app.anything")
    log.info("calling with %s and %s", KNOWN, SHAPED)
    log.warning(f"inline {KNOWN}")
    text = stream.getvalue()
    assert KNOWN not in text and SHAPED not in text
    assert [line["message"] for line in _lines(stream)] == [
        "calling with [REDACTED] and [REDACTED]",
        "inline [REDACTED]",
    ]


def test_paper_text_and_prompt_fields_are_redacted(stream: io.StringIO) -> None:
    logging.getLogger("app.x").info(
        "step done",
        extra={"paper_text": "We prove that ...", "prompt": "You are a reader", "tokens": 12},
    )
    (line,) = _lines(stream)
    assert line["paper_text"] == "[REDACTED]" and line["prompt"] == "[REDACTED]"
    assert line["tokens"] == 12
    assert "We prove" not in stream.getvalue()


def test_extra_fields_and_exception_text_are_scrubbed(stream: io.StringIO) -> None:
    log = logging.getLogger("app.y")
    try:
        raise ValueError(f"bad key {KNOWN}")
    except ValueError:
        log.exception("failed", extra={"detail": {"auth": f"Bearer {KNOWN}"}})
    text = stream.getvalue()
    assert KNOWN not in text
    (line,) = _lines(stream)
    assert "ValueError: bad key [REDACTED]" in str(line["exc"])


def test_the_filter_is_on_the_root_logger_and_its_handlers(stream: io.StringIO) -> None:
    root = logging.getLogger()
    assert any(isinstance(f, p2p_logging.RedactionFilter) for f in root.filters)
    assert all(
        any(isinstance(f, p2p_logging.RedactionFilter) for f in h.filters) for h in root.handlers
    )


def test_uvicorn_loggers_go_through_the_json_handler(stream: io.StringIO) -> None:
    for name in UVICORN:
        lg = logging.getLogger(name)
        assert lg.handlers == [] and lg.propagate
    access = logging.getLogger("uvicorn.access")
    access.info('%s - "%s %s HTTP/%s" %d', "10.0.0.1", "GET", f"/x?k={KNOWN}", "1.1", 200)
    (line,) = _lines(stream)
    assert line["logger"] == "uvicorn.access" and KNOWN not in stream.getvalue()


def test_lines_carry_run_id_and_the_run_trace_id(stream: io.StringIO) -> None:
    log = logging.getLogger("app.z")
    log.info("no run")
    token = current_run.set("r_01jb7z00000000000000000000")
    try:
        log.info("in a run")
    finally:
        current_run.reset(token)
    log.info("explicit", extra={"run_id": "r_other"})
    outside, inside, explicit = _lines(stream)
    assert "run_id" not in outside and "trace_id" not in outside
    assert inside["run_id"] == "r_01jb7z00000000000000000000"
    assert inside["trace_id"] == format(run_trace_id("r_01jb7z00000000000000000000"), "032x")
    assert explicit["run_id"] == "r_other"
    assert explicit["trace_id"] == format(run_trace_id("r_other"), "032x")
    assert inside["level"] == "INFO" and inside["message"] == "in a run" and "ts" in inside


def test_configuring_twice_keeps_one_handler(stream: io.StringIO) -> None:
    p2p_logging.configure_logging(p2p_logging.Redactor([KNOWN]), stream=stream)
    root = logging.getLogger()
    ours = [h for h in root.handlers if isinstance(h, p2p_logging.JsonHandler)]
    assert len(ours) == 1
    assert sum(isinstance(f, p2p_logging.RedactionFilter) for f in root.filters) == 1


def test_short_values_are_not_treated_as_secrets() -> None:
    redactor = p2p_logging.Redactor(["", "abc"])
    assert redactor("abc def") == "abc def"


def test_sensitive_keys_are_blanked_at_any_depth(stream: io.StringIO) -> None:
    logging.getLogger("app.n").info(
        "request",
        extra={
            "req": {
                "prompt": "<paper body>",
                "messages": [{"role": "user", "content": "We prove that ..."}],
                "model": "vendor/model-x",
            }
        },
    )
    (line,) = _lines(stream)
    assert line["req"] == {
        "prompt": "[REDACTED]",
        "messages": "[REDACTED]",
        "model": "vendor/model-x",
    }
    assert "We prove" not in stream.getvalue() and "paper body" not in stream.getvalue()


def test_an_unreadable_record_never_raises_into_the_caller(stream: io.StringIO) -> None:
    class Broken:
        def __str__(self) -> str:
            raise RuntimeError("no")

    loop: dict[str, object] = {}
    loop["self"] = loop
    log = logging.getLogger("app.broken")
    log.info(Broken())  # type: ignore[arg-type]
    log.info("circular", extra={"loop": loop})
    first, second = _lines(stream)
    assert first["message"] == "[log record unreadable]"
    assert second["message"] == "circular"


def test_secrets_in_chained_exceptions_and_exporter_logs_are_scrubbed(stream: io.StringIO) -> None:
    log = logging.getLogger("app.chain")
    try:
        try:
            raise KeyError(KNOWN)
        except KeyError as inner:
            raise RuntimeError("outer") from inner
    except RuntimeError:
        log.exception("failed")
    logging.getLogger("opentelemetry.exporter.otlp.proto.grpc.exporter").warning(
        "Failed to export, headers %s", {"authorization": f"Bearer {KNOWN}"}
    )
    assert KNOWN not in stream.getvalue()


def test_long_messages_are_capped(stream: io.StringIO) -> None:
    logging.getLogger("app.long").info("x" * 50_000)
    (line,) = _lines(stream)
    assert len(str(line["message"])) <= p2p_logging.MAX_TEXT + 20


def test_chatty_client_loggers_never_log_below_info(stream: io.StringIO) -> None:
    for name in ("openai", "httpx", "httpcore"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.INFO


def test_worker_processes_log_json_without_secrets() -> None:
    root = logging.getLogger()
    saved = (list(root.handlers), list(root.filters), root.level)
    try:
        p2p_logging.worker_logging()
        assert any(isinstance(h, p2p_logging.JsonHandler) for h in root.handlers)
    finally:
        root.handlers[:], root.filters[:] = saved[0], saved[1]
        root.setLevel(saved[2])


def test_object_extras_are_walked_or_hidden(stream: io.StringIO) -> None:
    from dataclasses import dataclass

    from pydantic import BaseModel

    class Req(BaseModel):
        prompt: str
        model: str

    @dataclass
    class Call:
        messages: list[str]
        tokens: int

    logging.getLogger("app.obj").info(
        "objects",
        extra={
            "req": Req(prompt="PAPER-LEAK-1", model="m"),
            "call": Call(["PAPER-LEAK-2"], 3),
            "tags": {"PAPER-LEAK-3"},
            "body": "y" * 50_000,
        },
    )
    (line,) = _lines(stream)
    assert "PAPER-LEAK" not in stream.getvalue()
    assert line["req"] == {"prompt": "[REDACTED]", "model": "m"}
    assert line["call"] == {"messages": "[REDACTED]", "tokens": 3}
    assert line["tags"] == "[set]"
    assert len(str(line["body"])) <= p2p_logging.MAX_TEXT + 20


def test_early_then_full_configuration_keeps_one_handler_and_filter(
    stream: io.StringIO,
) -> None:
    p2p_logging.configure_logging(p2p_logging.Redactor([]), stream=stream)
    p2p_logging.configure_logging(p2p_logging.Redactor([KNOWN]), stream=stream)
    root = logging.getLogger()
    assert sum(isinstance(h, p2p_logging.JsonHandler) for h in root.handlers) == 1
    assert sum(isinstance(f, p2p_logging.RedactionFilter) for f in root.filters) == 1
    logging.getLogger("app.e").info(KNOWN)
    assert KNOWN not in stream.getvalue()

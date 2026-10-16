"""Traces (architecture §14): one trace per run, a span per step and per model call.

Exported over OTLP/gRPC to Nebius Observability Tracing (`write.tracing.<region>.nebius.cloud:
443`, a static key of the `observability` service as Bearer token and the project ID as
`iam-container`; docs.nebius.com/observability/traces/direct-ingest). In `test`, or without an
endpoint, nothing is exported and spans are no-ops.

A run's trace ID is derived from its run ID: the steps of a run land in one trace even across
restarts, and log lines carry the same trace ID when tracing is off. Spans hold IDs, names,
numbers and error codes only: never prompts, paper text or exception messages.
"""

import hashlib
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from urllib.parse import urlsplit

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.id_generator import RandomIdGenerator
from opentelemetry.trace import Span, Status, StatusCode

from app.settings import Settings

NAME = "paper2proof"
TRACING_HOST = re.compile(r"write\.tracing\.[a-z0-9-]+\.nebius\.cloud")  # the key goes here only
_HOST_PORT = re.compile(r"([a-z0-9.-]+)(?::([0-9]{1,5}))?")  # no userinfo, no escapes
current_run: ContextVar[str | None] = ContextVar("p2p_run_id", default=None)
_provider: trace.TracerProvider | None = None  # None: the global provider (tests replace it)


def run_trace_id(run_id: str) -> int:
    """The 128-bit trace ID of a run (never 0, which OpenTelemetry treats as invalid)."""
    return int.from_bytes(hashlib.sha256(run_id.encode()).digest()[:16], "big") or 1


class RunIdGenerator(RandomIdGenerator):
    """Root spans started while a run is current get the run's trace ID."""

    def generate_trace_id(self) -> int:
        run_id = current_run.get()
        return run_trace_id(run_id) if run_id else super().generate_trace_id()


def tracer() -> trace.Tracer:
    return trace.get_tracer(NAME, tracer_provider=_provider)


@contextmanager
def step_span(run_id: str, step: str, attempt: int) -> Iterator[Span]:
    """The span of one step attempt. Exceptions are not recorded: their text may hold paper
    content or secrets, and the step's error code is set by the caller instead."""
    with tracer().start_as_current_span(
        f"step {step}",
        attributes={"run_id": run_id, "step": step, "attempt": attempt},
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        yield span


def fail(span: Span, error_code: str) -> None:
    span.set_status(Status(StatusCode.ERROR, error_code))


def model_call_span(
    *,
    run_id: str | None,
    step: str | None,
    role: str,
    model_id: str,
    tokens_in: int,
    tokens_out: int,
    latency_ms: int,
    cost_usd: float | None,
) -> None:
    """A finished model call as a span, back-dated by its latency (recorded once it is done)."""
    attributes: dict[str, str | int | float] = {
        "role": role,
        "model_id": model_id,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "latency_ms": latency_ms,
    }
    for name, value in (("run_id", run_id), ("step", step), ("cost_usd", cost_usd)):
        if value is not None:
            attributes[name] = value
    end = time.time_ns()
    token = current_run.set(run_id or current_run.get())  # outside a step: still the run's trace
    try:
        span = tracer().start_span("model.call", start_time=end - latency_ms * 1_000_000)
    finally:
        current_run.reset(token)
    span.set_attributes(attributes)
    span.end(end_time=end)


@dataclass(frozen=True)
class ExporterOptions:
    endpoint: str
    insecure: bool
    headers: tuple[tuple[str, str], ...]


def exporter_options(
    settings: Settings, endpoint: str, *, send_key: bool = True
) -> ExporterOptions:
    """Where and how an OTLP/gRPC exporter connects: https://host[:port] only (a plain
    http:// collector in dev), rebuilt from its parts so the dialled target is the checked one.
    The static key goes only to the Nebius Tracing write host, over TLS."""
    url = urlsplit(endpoint)
    plain_dev = url.scheme == "http" and settings.p2p_env == "dev"
    netloc = _HOST_PORT.fullmatch(url.netloc)
    if (
        netloc is None
        or (url.scheme != "https" and not plain_dev)
        or url.path not in ("", "/")
        or url.query
        or url.fragment
    ):  # the endpoint is not echoed: it may carry credentials
        raise ValueError("OTLP endpoint must be https://host[:port], without credentials or path")
    host = netloc.group(1)
    port = int(netloc.group(2) or (443 if url.scheme == "https" else 4317))
    headers: tuple[tuple[str, str], ...] = ()
    key = settings.observability_static_key
    tracing_host = url.scheme == "https" and TRACING_HOST.fullmatch(host) is not None
    if send_key and tracing_host:
        if key is None or not key.get_secret_value():
            raise ValueError("Nebius Tracing needs OBSERVABILITY_STATIC_KEY")
        if not settings.observability_project_id:
            raise ValueError("Nebius Observability needs OBSERVABILITY_PROJECT_ID (iam-container)")
        headers = (
            ("authorization", f"Bearer {key.get_secret_value()}"),
            ("iam-container", settings.observability_project_id),
        )
    return ExporterOptions(
        f"{url.scheme}://{host}:{port}", insecure=url.scheme == "http", headers=headers
    )


def resource(settings: Settings) -> Resource:
    return Resource.create(
        {
            "service.name": NAME,
            "service.version": settings.p2p_build_commit or "unknown",
            "deployment.environment": settings.p2p_env,
        }
    )


def build_tracer_provider(settings: Settings) -> TracerProvider | None:
    endpoint = settings.p2p_traces_endpoint
    if settings.p2p_env == "test" or not endpoint:
        return None
    options = exporter_options(settings, endpoint)
    exporter = OTLPSpanExporter(
        endpoint=options.endpoint, insecure=options.insecure, headers=options.headers
    )
    provider = TracerProvider(resource=resource(settings), id_generator=RunIdGenerator())
    provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider


def configure_tracing(settings: Settings) -> bool:
    """Install the exporting provider for this process; False when tracing stays a no-op."""
    provider = build_tracer_provider(settings)
    if provider is None:
        return False
    trace.set_tracer_provider(provider)
    return True

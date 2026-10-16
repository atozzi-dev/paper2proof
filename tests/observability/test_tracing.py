"""Traces: no-op in test, one trace per run, and where the exporter may send its key."""

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from pydantic import SecretStr

from app.observability import tracing
from app.settings import Settings
from tests.observability.fakes import Telemetry

NEBIUS = "https://write.tracing.eu-north1.nebius.cloud:443"
KEY = "nebius-static-key-0123456789"


def _settings(env: str = "prod", **values: object) -> Settings:
    fields: dict[str, object] = {
        "p2p_env": env,
        "nebius_api_key": SecretStr("n" * 40),
        "session_key": SecretStr("s" * 40),
        "admin_token": SecretStr("a" * 40),
    }
    fields.update(values)
    return Settings(**fields)  # type: ignore[arg-type]


def test_the_tracer_is_a_no_op_in_test() -> None:
    settings = _settings(
        "test", p2p_traces_endpoint=NEBIUS, observability_static_key=SecretStr(KEY)
    )
    assert tracing.build_tracer_provider(settings) is None
    assert tracing.configure_tracing(settings) is False
    assert not isinstance(trace.get_tracer_provider(), TracerProvider)
    with tracing.tracer().start_as_current_span("x") as span:
        assert not span.is_recording()


def test_no_endpoint_means_no_provider() -> None:
    assert tracing.build_tracer_provider(_settings()) is None


def test_a_prod_provider_is_built_with_the_run_id_generator() -> None:
    settings = _settings(
        p2p_traces_endpoint=NEBIUS,
        observability_static_key=SecretStr(KEY),
        observability_project_id="project-e00abc",
    )
    provider = tracing.build_tracer_provider(settings)
    try:
        assert isinstance(provider, TracerProvider)
        assert isinstance(provider.id_generator, tracing.RunIdGenerator)
        assert provider.resource.attributes["service.name"] == "paper2proof"
    finally:
        assert provider is not None
        provider.shutdown()


def test_the_static_key_goes_only_to_nebius_over_tls() -> None:
    settings = _settings(
        p2p_traces_endpoint=NEBIUS,
        observability_static_key=SecretStr(KEY),
        observability_project_id="project-e00abc",
    )
    options = tracing.exporter_options(settings, NEBIUS)
    assert options is not None and not options.insecure
    assert dict(options.headers) == {
        "authorization": f"Bearer {KEY}",
        "iam-container": "project-e00abc",
    }
    other = tracing.exporter_options(settings, "https://collector.example.com:4317")
    assert other is not None and other.headers == ()
    lookalike = tracing.exporter_options(settings, "https://nebius.cloud.evil.example:443")
    assert lookalike is not None and lookalike.headers == ()


@pytest.mark.parametrize(
    ("env", "endpoint", "problem"),
    [
        ("prod", "http://write.tracing.eu-north1.nebius.cloud:443", "https"),
        ("prod", "write.tracing.eu-north1.nebius.cloud:443", "https"),
        ("prod", NEBIUS, "OBSERVABILITY_PROJECT_ID"),
    ],
)
def test_unsafe_or_incomplete_endpoints_are_refused(env: str, endpoint: str, problem: str) -> None:
    settings = _settings(env, observability_static_key=SecretStr(KEY))
    with pytest.raises(ValueError, match=problem):
        tracing.exporter_options(settings, endpoint)


def test_a_plain_http_collector_is_allowed_in_dev_without_the_key() -> None:
    settings = _settings("dev", observability_static_key=SecretStr(KEY))
    options = tracing.exporter_options(settings, "http://localhost:4317")
    assert options is not None and options.insecure and options.headers == ()


def test_every_span_of_a_run_lands_in_the_run_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    telemetry = Telemetry(monkeypatch)
    for run_id in ("r_a", "r_a", "r_b"):
        token = tracing.current_run.set(run_id)
        try:
            with tracing.tracer().start_as_current_span("step"):
                pass
        finally:
            tracing.current_run.reset(token)
    with tracing.tracer().start_as_current_span("outside"):
        pass
    a1, a2, b, outside = telemetry.spans()
    assert a1.context is not None and a2.context is not None and b.context is not None
    assert a1.context.trace_id == a2.context.trace_id == tracing.run_trace_id("r_a")
    assert b.context.trace_id == tracing.run_trace_id("r_b") != a1.context.trace_id
    assert outside.context is not None
    assert outside.context.trace_id not in {a1.context.trace_id, b.context.trace_id}


def test_model_call_spans_carry_usage_and_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    telemetry = Telemetry(monkeypatch)
    tracing.model_call_span(
        run_id="r_a",
        step="PLANNING",
        role="planner",
        model_id="vendor/model-x",
        tokens_in=100,
        tokens_out=20,
        latency_ms=1500,
        cost_usd=None,
    )
    (span,) = telemetry.spans("model.call")
    attributes = dict(span.attributes or {})
    assert attributes == {
        "run_id": "r_a",
        "step": "PLANNING",
        "role": "planner",
        "model_id": "vendor/model-x",
        "tokens_in": 100,
        "tokens_out": 20,
        "latency_ms": 1500,
    }
    assert span.start_time is not None and span.end_time is not None
    assert span.end_time - span.start_time == 1_500_000_000


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://evil.com:443@write.tracing.eu-north1.nebius.cloud",
        "https://u:p@write.tracing.eu-north1.nebius.cloud:443",
        "https://write.tracing.eu-north1.nebius.cloud:443/v1/traces",
        "https://write.tracing.eu-north1.nebius.cloud:443?x=1",
        "https://write.tracing.eu-north1.nebius.cloud:443#f",
        "https://write.tracing.eu-north1.nebius.cloud%2eevil.com:443",
    ],
)
def test_endpoints_with_credentials_paths_or_escapes_are_refused(endpoint: str) -> None:
    settings = _settings(
        observability_static_key=SecretStr(KEY), observability_project_id="project-e00abc"
    )
    with pytest.raises(ValueError, match="host"):
        tracing.exporter_options(settings, endpoint)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://other.nebius.cloud:443",  # another Nebius-hosted name
        "https://write.tracing.eu-north1.nebius.cloud.:443",  # trailing dot
        "https://write.monitoring.eu-north1.nebius.cloud:443",
    ],
)
def test_the_key_is_pinned_to_the_tracing_write_host(endpoint: str) -> None:
    settings = _settings(
        observability_static_key=SecretStr(KEY), observability_project_id="project-e00abc"
    )
    options = tracing.exporter_options(settings, endpoint)
    assert options.headers == ()


def test_the_exporter_dials_a_rebuilt_url_and_metrics_never_get_the_key() -> None:
    settings = _settings(
        observability_static_key=SecretStr(KEY), observability_project_id="project-e00abc"
    )
    options = tracing.exporter_options(settings, "https://write.tracing.eu-north1.nebius.cloud/")
    assert options.endpoint == "https://write.tracing.eu-north1.nebius.cloud:443"
    metrics_options = tracing.exporter_options(settings, NEBIUS, send_key=False)
    assert metrics_options.headers == ()


def test_plain_http_never_carries_the_key_even_in_dev() -> None:
    settings = _settings(
        "dev", observability_static_key=SecretStr(KEY), observability_project_id="project-e00abc"
    )
    options = tracing.exporter_options(settings, "http://write.tracing.eu-north1.nebius.cloud:443")
    assert options.insecure and options.headers == ()


def test_nebius_tracing_without_the_key_is_refused() -> None:
    settings = _settings(observability_project_id="project-e00abc")
    with pytest.raises(ValueError, match="OBSERVABILITY_STATIC_KEY"):
        tracing.exporter_options(settings, NEBIUS)

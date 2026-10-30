"""Metrics of architecture §14, exported over OTLP/gRPC when an endpoint is configured.

Nebius Monitoring has no direct OTLP write for application metrics (it takes Prometheus
remote-write), so P2P_METRICS_ENDPOINT points at an OpenTelemetry Collector that forwards
them; without it the instruments record into a no-op provider. Labels are bounded: status,
step, role, model, token direction and spend kind, never run IDs.
"""

from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

from app.domain.models import RunStatus
from app.observability.tracing import NAME, exporter_options, resource
from app.settings import Settings

EXPORT_INTERVAL_MS = 30_000
COST_KINDS = {"inference_usd": "inference", "cloud_usd": "cloud"}  # SpendKind values
JOB_KINDS = ("prov", "exec", "smoke", "other")  # job name kinds (app/execution/naming.py)
REAPER_ACTIONS = (
    "error",
    "job_cancelled_orphan",
    "job_cancelled_overdue",
    "ledger_reconciled",
    "bucket_kept_no_bundle",
    "bucket_delete_failed",
    "bucket_deleted",
    "gateway_stopped",
)


class Instruments:
    def __init__(self, meter: metrics.Meter) -> None:
        self.runs = meter.create_counter(
            "p2p_runs_total", description="Runs that reached a terminal status"
        )
        self.runs_active = meter.create_up_down_counter(
            "p2p_runs_active", description="Runs being driven or waiting for a slot"
        )
        self.step_duration = meter.create_histogram(
            "p2p_step_duration_seconds", unit="s", description="Duration of one step attempt"
        )
        self.model_tokens = meter.create_counter(
            "p2p_model_tokens_total", description="Tokens per role, model and direction"
        )
        self.cost_usd = meter.create_counter(
            "p2p_cost_usd_total", description="Committed spend in USD per kind"
        )
        self.gpu_seconds = meter.create_counter(
            "p2p_gpu_seconds_total", unit="s", description="Committed GPU time"
        )
        self.ratelimit_remaining = meter.create_gauge(
            "p2p_ratelimit_remaining_tokens", description="x-ratelimit-remaining-tokens"
        )
        self.reaper_actions = meter.create_counter(
            "p2p_reaper_actions_total", description="Reaper actions (job cancels are alerts)"
        )
        self.job_oldest_running = meter.create_gauge(
            "p2p_job_oldest_running_seconds",
            unit="s",
            description="Age of the oldest non-terminal p2p-* job per kind (0: none)",
        )


_current: Instruments | None = None


def instruments() -> Instruments:
    """The process's instruments, registered once on the global meter (a proxy until a
    provider is installed, so instruments made earlier still export)."""
    global _current
    if _current is None:
        _current = Instruments(metrics.get_meter(NAME))
    return _current


def run_driven(delta: int) -> None:
    instruments().runs_active.add(delta)


def run_ended(status: str) -> None:
    instruments().runs.add(1, {"status": status})


def step_finished(step: str, seconds: float) -> None:
    instruments().step_duration.record(seconds, {"step": step})


def model_call(
    role: str, model_id: str, *, tokens_in: int, tokens_out: int, remaining: int | None
) -> None:
    current = instruments()
    current.model_tokens.add(tokens_in, {"role": role, "model": model_id, "dir": "in"})
    current.model_tokens.add(tokens_out, {"role": role, "model": model_id, "dir": "out"})
    if remaining is not None:
        current.ratelimit_remaining.set(remaining, {"model": model_id})


def spend(kind: str, amount: float) -> None:
    """Committed spend from the budget (a SpendKind value); tokens are counted per model call."""
    if kind in COST_KINDS:
        instruments().cost_usd.add(amount, {"kind": COST_KINDS[kind]})
    elif kind == "gpu_minutes":
        instruments().gpu_seconds.add(amount * 60)


def reaper_action(action: str) -> None:
    instruments().reaper_actions.add(1, {"action": action})


def jobs_running(oldest: dict[str, float]) -> None:
    """The reaper's view of running jobs, every pass: a p2p-exec-* job past 45 min alerts."""
    for kind in JOB_KINDS:
        instruments().job_oldest_running.set(oldest.get(kind, 0.0), {"kind": kind})


def prime() -> None:
    """Create every bounded series at 0 once exporting starts: Prometheus' increase() ignores
    a series' first sample, so the first failed run, cloud charge or reaper cancel after a
    restart would otherwise never reach an alert."""
    current = instruments()
    current.runs_active.add(0)
    current.gpu_seconds.add(0)
    for status in RunStatus:
        if status.is_terminal:
            current.runs.add(0, {"status": status.value})
    for kind in COST_KINDS.values():
        current.cost_usd.add(0, {"kind": kind})
    for action in REAPER_ACTIONS:
        current.reaper_actions.add(0, {"action": action})


def build_meter_provider(settings: Settings) -> MeterProvider | None:
    endpoint = settings.p2p_metrics_endpoint
    if settings.p2p_env == "test" or not endpoint:
        return None
    options = exporter_options(settings, endpoint, send_key=False)  # a collector, not Nebius
    exporter = OTLPMetricExporter(
        endpoint=options.endpoint, insecure=options.insecure, headers=options.headers
    )
    reader = PeriodicExportingMetricReader(exporter, export_interval_millis=EXPORT_INTERVAL_MS)
    return MeterProvider(resource=resource(settings), metric_readers=[reader])


def configure_metrics(settings: Settings) -> bool:
    """Install the exporting provider for this process; False when metrics stay a no-op."""
    provider = build_meter_provider(settings)
    if provider is None:
        return False
    metrics.set_meter_provider(provider)
    prime()
    return True

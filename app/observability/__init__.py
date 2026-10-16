"""Logs, traces and metrics (architecture §14, A-28). The composition root calls
`configure` once per process."""

from collections.abc import Sequence
from logging import getLogger  # `logging` below is the submodule app.observability.logging

from app.observability.logging import Redactor, configure_logging, worker_logging
from app.observability.metrics import configure_metrics
from app.observability.tracing import configure_tracing
from app.settings import Settings

log = getLogger(__name__)
__all__ = ["configure", "configure_early", "worker_logging"]


def configure_early() -> None:
    """JSON logs scrubbed of key-shaped strings, before the settings (and their secret values)
    are read: nothing is logged in another format or unscrubbed at startup."""
    configure_logging(Redactor([]))


def configure(settings: Settings, known_secrets: Sequence[str]) -> None:
    """JSON logs scrubbed of `known_secrets` (and of key-shaped strings), then the trace and
    metric exporters when their endpoints are set (never in `test`)."""
    configure_logging(Redactor(known_secrets))
    traces, metrics = configure_tracing(settings), configure_metrics(settings)
    log.info("observability configured", extra={"traces": traces, "metrics": metrics})

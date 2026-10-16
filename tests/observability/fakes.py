"""In-memory traces and metrics: tests read what the app recorded without any exporter."""

from collections.abc import Mapping

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.observability import metrics, tracing


class Telemetry:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.exporter = InMemorySpanExporter()
        provider = TracerProvider(id_generator=tracing.RunIdGenerator())
        provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.reader = InMemoryMetricReader()
        meters = MeterProvider(metric_readers=[self.reader])
        monkeypatch.setattr(tracing, "_provider", provider)
        monkeypatch.setattr(metrics, "_current", metrics.Instruments(meters.get_meter("test")))

    def spans(self, name: str | None = None) -> list[ReadableSpan]:
        found = self.exporter.get_finished_spans()
        return [s for s in found if name is None or s.name == name]

    def points(self, name: str) -> dict[tuple[tuple[str, object], ...], object]:
        """{sorted attributes: value} of one metric (sum, gauge value, or histogram count)."""
        data = self.reader.get_metrics_data()
        found: dict[tuple[tuple[str, object], ...], object] = {}
        for resource in data.resource_metrics if data else []:
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    if metric.name != name:
                        continue
                    for point in metric.data.data_points:
                        attributes: Mapping[str, object] = point.attributes or {}
                        value = getattr(point, "value", None)
                        if value is None:
                            value = getattr(point, "count", None)
                        found[tuple(sorted(attributes.items()))] = value
        return found

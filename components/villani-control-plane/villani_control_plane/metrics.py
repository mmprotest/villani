from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from threading import Lock
from typing import Protocol


class MetricsExporter(Protocol):
    def export(self, metrics: list[dict[str, object]]) -> None: ...


class FakeOTLPExporter:
    def __init__(self) -> None:
        self.exports: list[list[dict[str, object]]] = []

    def export(self, metrics: list[dict[str, object]]) -> None:
        self.exports.append(metrics)


class OTLPHTTPMetricsExporter:
    """Optional real OTLP/HTTP adapter backed by the OpenTelemetry Python SDK."""

    def __init__(self, endpoint: str) -> None:
        try:
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
                OTLPMetricExporter,
            )
            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        except ImportError as error:
            raise RuntimeError("OTLP export requires villani-control-plane[otel]") from error
        exporter = OTLPMetricExporter(endpoint=endpoint)
        self._reader = PeriodicExportingMetricReader(exporter)
        self._provider = MeterProvider(metric_readers=[self._reader])
        self._meter = self._provider.get_meter("villani-control-plane")
        self._instruments: dict[str, object] = {}
        self._previous: dict[MetricKey, float] = {}

    def export(self, metrics: list[dict[str, object]]) -> None:
        for metric in metrics:
            name = str(metric["name"])
            labels = metric.get("labels", {})
            if not isinstance(labels, dict):
                labels = {}
            key = MetricKey(name, tuple(sorted((str(k), str(v)) for k, v in labels.items())))
            value = float(metric["value"])
            instrument = self._instruments.get(name)
            if instrument is None:
                instrument = self._meter.create_up_down_counter(name)
                self._instruments[name] = instrument
            delta = value - self._previous.get(key, 0.0)
            instrument.add(delta, dict(key.labels))
            self._previous[key] = value
        self._provider.force_flush()

    def shutdown(self) -> None:
        self._provider.shutdown()


@dataclass(frozen=True, slots=True)
class MetricKey:
    name: str
    labels: tuple[tuple[str, str], ...]


class StructuredMetrics:
    def __init__(self, exporter: MetricsExporter | None = None) -> None:
        self._values: dict[MetricKey, float] = defaultdict(float)
        self._lock = Lock()
        self.exporter = exporter

    def add(self, name: str, value: float = 1, **labels: str) -> None:
        key = MetricKey(name, tuple(sorted(labels.items())))
        with self._lock:
            self._values[key] += value

    def snapshot(self) -> list[dict[str, object]]:
        with self._lock:
            result = [
                {"name": key.name, "value": value, "labels": dict(key.labels)}
                for key, value in sorted(
                    self._values.items(), key=lambda item: (item[0].name, item[0].labels)
                )
            ]
        if self.exporter:
            self.exporter.export(result)
        return result

    def json(self) -> str:
        return json.dumps({"metrics": self.snapshot()}, sort_keys=True, separators=(",", ":"))


metrics = StructuredMetrics()

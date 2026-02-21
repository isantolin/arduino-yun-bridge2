"""Lightweight prometheus_client test stub.

This stub exists for local/unit test environments where the real
`prometheus_client` package is unavailable.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .core import GaugeMetricFamily, InfoMetricFamily
from .registry import Collector

CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"


class CollectorRegistry:
    def __init__(self) -> None:
        self._collectors: list[Collector] = []

    def register(self, collector: Collector) -> None:
        self._collectors.append(collector)

    def unregister(self, collector: Collector) -> None:
        self._collectors = [existing for existing in self._collectors if existing is not collector]

    @property
    def collectors(self) -> tuple[Collector, ...]:
        return tuple(self._collectors)


class Summary:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self._observations: list[float] = []

    def observe(self, value: float) -> None:
        self._observations.append(float(value))


def _render_metric_name(name: str) -> str:
    return name.strip() or "metric"


def _emit_gauge(metric: GaugeMetricFamily) -> Iterable[str]:
    metric_name = _render_metric_name(metric.name)
    for labels, value in metric.samples:
        if labels:
            rendered_labels = ",".join(f'{key}="{val}"' for key, val in labels.items())
            yield f"{metric_name}{{{rendered_labels}}} {value}"
        else:
            yield f"{metric_name} {value}"


def _emit_info(metric: InfoMetricFamily) -> Iterable[str]:
    metric_name = _render_metric_name(metric.name)
    for labels, value_dict in metric.samples:
        combined = dict(labels)
        combined.update(value_dict)
        rendered_labels = ",".join(f'{key}="{val}"' for key, val in combined.items())
        yield f"{metric_name}{{{rendered_labels}}} 1"


def generate_latest(registry: CollectorRegistry) -> bytes:
    lines: list[str] = []
    for collector in registry.collectors:
        for metric in collector.collect():
            if isinstance(metric, GaugeMetricFamily):
                lines.extend(_emit_gauge(metric))
            elif isinstance(metric, InfoMetricFamily):
                lines.extend(_emit_info(metric))
    return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")


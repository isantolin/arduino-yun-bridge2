"""Core metric family stubs for prometheus_client."""

from __future__ import annotations

from typing import Any, Sequence


class Metric:
    """Base class for all metrics in the stub."""
    def __init__(self, name: str, documentation: str, labels: Sequence[str] | None = None) -> None:
        self.name = name
        self.documentation = documentation
        self.label_names: Sequence[str] = labels or []


class GaugeMetricFamily(Metric):
    def __init__(self, name: str, documentation: str, labels: Sequence[str] | None = None) -> None:
        super().__init__(name, documentation, labels)
        self.samples: list[tuple[dict[str, str], float]] = []

    def add_metric(self, labels: Sequence[str], value: float) -> None:
        label_map: dict[str, str] = {}
        for i, label_val in enumerate(labels):
            label_name = self.label_names[i] if i < len(self.label_names) else f"label{i}"
            label_map[label_name] = str(label_val)
        self.samples.append((label_map, float(value)))


class InfoMetricFamily(Metric):
    def __init__(self, name: str, documentation: str, labels: Sequence[str] | None = None) -> None:
        super().__init__(name, documentation, labels)
        self.samples: list[tuple[dict[str, str], dict[str, Any]]] = []

    def add_metric(self, labels: Sequence[str], value: dict[str, Any]) -> None:
        label_map: dict[str, str] = {}
        for i, label_val in enumerate(labels):
            label_name = self.label_names[i] if i < len(self.label_names) else f"label{i}"
            label_map[label_name] = str(label_val)
        self.samples.append((label_map, value))

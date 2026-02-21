"""Core metric family stubs for prometheus_client."""

from __future__ import annotations

from typing import Any


class GaugeMetricFamily:
    def __init__(self, name: str, documentation: str) -> None:
        self.name = name
        self.documentation = documentation
        self.samples: list[tuple[dict[str, str], float]] = []

    def add_metric(self, labels: tuple[str, ...], value: float) -> None:
        if labels:
            label_map = {f"label{index}": label for index, label in enumerate(labels)}
        else:
            label_map = {}
        self.samples.append((label_map, float(value)))


class InfoMetricFamily:
    def __init__(self, name: str, documentation: str, labels: tuple[str, ...] = ()) -> None:
        self.name = name
        self.documentation = documentation
        self.labels = labels
        self.samples: list[tuple[dict[str, str], dict[str, Any]]] = []

    def add_metric(self, labels: tuple[str, ...], value: dict[str, Any]) -> None:
        label_map = {key: labels[index] for index, key in enumerate(self.labels) if index < len(labels)}
        self.samples.append((label_map, value))


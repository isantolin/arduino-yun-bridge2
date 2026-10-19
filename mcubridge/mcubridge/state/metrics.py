"""Formal native metrics container for McuBridge edge daemon (SIL-2)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

logger = structlog.get_logger("mcubridge.state.metrics")


@dataclass
class CounterMetric:
    """Lightweight deterministic counter primitive."""

    value: int = 0

    def inc(self, amount: int = 1) -> None:
        self.value += amount


@dataclass
class FloatGaugeMetric:
    """Lightweight latency / gauge primitive."""

    value: float = 0.0

    def set(self, val: float) -> None:
        self.value = val

    def observe(self, val: float) -> None:
        self.value = val


@dataclass
class StateMetric:
    """Lightweight discrete state tracking primitive."""

    value: str = "disconnected"

    def state(self, val: str | Any) -> None:
        self.value = str(getattr(val, "value", val))


class LabeledCounter:
    """Lightweight labeled counter collection."""

    def __init__(self) -> None:
        self._counters: dict[str, CounterMetric] = {}

    def labels(self, **kwargs: str) -> CounterMetric:
        key = (
            next(iter(kwargs.values()), "")
            if len(kwargs) == 1
            else ":".join(f"{k}={v}" for k, v in sorted(kwargs.items()))
        )
        if key not in self._counters:
            self._counters[key] = CounterMetric()
        return self._counters[key]

    def items(self) -> list[tuple[str, int]]:
        return [(k, c.value) for k, c in self._counters.items()]


class DaemonMetrics:
    """Formal native metrics container for McuBridge edge daemon without external dependencies."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs

        # CLOUD Metrics
        self.cloud_messages_published = CounterMetric()
        self.cloud_messages_dropped = CounterMetric()

        # Serial Metrics
        self.serial_bytes_sent = CounterMetric()
        self.serial_bytes_received = CounterMetric()
        self.serial_frames_sent = CounterMetric()
        self.serial_frames_received = CounterMetric()
        self.serial_retries = CounterMetric()
        self.serial_failures = CounterMetric()
        self.serial_crc_errors = CounterMetric()
        self.serial_decode_errors = CounterMetric()
        self.serial_latency_ms = FloatGaugeMetric()
        self.rpc_latency_ms = FloatGaugeMetric()

        # System Metrics
        self.unknown_command_count = CounterMetric()
        self.mcu_status_counts = LabeledCounter()
        self.handshake_attempts = CounterMetric()
        self.handshake_successes = CounterMetric()
        self.watchdog_beats = CounterMetric()
        self.uptime_seconds = FloatGaugeMetric()

        # FSM State Metrics
        self.link_state = StateMetric("disconnected")
        self.handshake_state = StateMetric("unsynchronized")

        # Connection/Operation Retry Metrics
        self.retries = LabeledCounter()


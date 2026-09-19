"""Formal native metrics container for McuBridge edge daemon (SIL-2)."""

from __future__ import annotations

from dataclasses import dataclass
import importlib.metadata
from typing import Any


class _ValueGetter:
    """Helper for backward-compatibility with tests inspecting ._value.get()."""

    def __init__(self, val: int | float) -> None:
        self._val = val

    def get(self) -> int | float:
        return self._val


@dataclass
class CounterMetric:
    """Lightweight deterministic counter primitive."""

    _count: int = 0

    def inc(self, amount: int = 1) -> None:
        self._count += amount

    @property
    def value(self) -> int:
        return self._count

    @property
    def _value(self) -> _ValueGetter:
        return _ValueGetter(self._count)


@dataclass
class FloatGaugeMetric:
    """Lightweight latency / gauge primitive."""

    _val: float = 0.0

    def set(self, val: float) -> None:
        self._val = val

    def observe(self, val: float) -> None:
        self._val = val

    @property
    def value(self) -> float:
        return self._val


@dataclass
class StateMetric:
    """Lightweight discrete state tracking primitive."""

    _state: str = "disconnected"

    def state(self, val: str | Any) -> None:
        self._state = str(getattr(val, "value", val))

    @property
    def value(self) -> str:
        return self._state


class LabeledCounter:
    """Lightweight labeled counter collection."""

    def __init__(self) -> None:
        self._counters: dict[str, CounterMetric] = {}

    def labels(self, **kwargs: str) -> CounterMetric:
        key = next(iter(kwargs.values()), "") if len(kwargs) == 1 else ":".join(
            f"{k}={v}" for k, v in sorted(kwargs.items())
        )
        if key not in self._counters:
            self._counters[key] = CounterMetric()
        return self._counters[key]

    def items(self) -> list[tuple[str, int]]:
        return [(k, c.value) for k, c in self._counters.items()]


class InfoMetric:
    """Lightweight build info metadata container."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def info(self, data: dict[str, str]) -> None:
        self.data.update(data)


class DaemonMetrics:
    """Formal native metrics container for McuBridge edge daemon without external dependencies."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs

        # Supervisor Metrics
        self.supervisor_failures = LabeledCounter()

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

        # Info Metric (build metadata — set once at startup)
        self.build_info = InfoMetric()

        # FSM State Metrics
        self.link_state = StateMetric("disconnected")
        self.handshake_state = StateMetric("unsynchronized")

        # Connection/Operation Retry Metrics
        self.retries = LabeledCounter()

        self._set_build_info()

    def _set_build_info(self) -> None:
        """Populate build info from package metadata."""
        try:
            version = importlib.metadata.version("mcubridge")
        except importlib.metadata.PackageNotFoundError:
            version = "dev"
        self.build_info.info({"version": version, "python": "3.13"})

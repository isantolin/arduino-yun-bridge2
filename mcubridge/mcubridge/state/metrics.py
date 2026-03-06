"""Formal metrics container for McuBridge using prometheus_client primitives."""

from __future__ import annotations

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
)


class DaemonMetrics:
    """Formal metrics container using prometheus_client primitives."""

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()

        # MQTT Metrics
        self.mqtt_messages_published = Counter(
            "mcubridge_mqtt_tx_messages_total",
            "Total MQTT messages published",
            ["topic"],
            registry=self.registry,
        )
        self.mqtt_messages_received = Counter(
            "mcubridge_mqtt_rx_messages_total",
            "Total MQTT messages received from broker",
            ["topic"],
            registry=self.registry,
        )

        # Serial Metrics
        self.serial_tx_bytes = Counter(
            "mcubridge_serial_tx_bytes_total",
            "Total bytes sent to MCU",
            registry=self.registry,
        )
        self.serial_rx_bytes = Counter(
            "mcubridge_serial_rx_bytes_total",
            "Total bytes received from MCU",
            registry=self.registry,
        )
        self.serial_tx_frames = Counter(
            "mcubridge_serial_tx_frames_total",
            "Total frames sent to MCU",
            registry=self.registry,
        )
        self.serial_rx_frames = Counter(
            "mcubridge_serial_rx_frames_total",
            "Total frames received from MCU",
            registry=self.registry,
        )
        self.decode_errors = Counter(
            "mcubridge_serial_decode_errors_total",
            "Total frame decoding failures (COBS/Length)",
            registry=self.registry,
        )
        self.mcu_uptime_seconds = Counter(
            "mcubridge_mcu_uptime_seconds_total",
            "MCU uptime estimate",
            registry=self.registry,
        )
        self.bridge_restart_count = Counter(
            "mcubridge_restart_total",
            "Total daemon restarts",
            registry=self.registry,
        )
        self.task_failures = Counter(
            "mcubridge_task_failures_total",
            "Total background task crashes",
            ["task"],
            registry=self.registry,
        )
        self.mcu_free_memory = Gauge(
            "mcubridge_mcu_free_memory_bytes",
            "Reported free heap memory on MCU",
            registry=self.registry,
        )

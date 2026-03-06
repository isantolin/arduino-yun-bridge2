"""Prometheus metrics exporter for the MCU Bridge service."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import msgspec
from mcubridge.protocol.protocol import Command
from mcubridge.protocol.topics import Topic, topic_path
from prometheus_client import Counter, Gauge, Histogram, start_http_server

if TYPE_CHECKING:
    from aiomqtt.message import Message
    from mcubridge.config.settings import RuntimeConfig
    from mcubridge.state.context import RuntimeState

logger = logging.getLogger("mcubridge.metrics")


@dataclass
class BridgeMetrics:
    """Consolidated metrics for the MCU Bridge ecosystem."""

    # Serial I/O
    serial_tx_bytes: Counter = field(
        default_factory=lambda: Counter(
            "mcubridge_serial_tx_bytes_total", "Total bytes sent to MCU"
        )
    )
    serial_rx_bytes: Counter = field(
        default_factory=lambda: Counter(
            "mcubridge_serial_rx_bytes_total", "Total bytes received from MCU"
        )
    )
    serial_tx_frames: Counter = field(
        default_factory=lambda: Counter(
            "mcubridge_serial_tx_frames_total", "Total frames sent to MCU"
        )
    )
    serial_rx_frames: Counter = field(
        default_factory=lambda: Counter(
            "mcubridge_serial_rx_frames_total", "Total frames received from MCU"
        )
    )

    # Frame Quality
    crc_errors: Counter = field(
        default_factory=lambda: Counter(
            "mcubridge_serial_crc_errors_total", "Total CRC32 validation failures"
        )
    )
    decode_errors: Counter = field(
        default_factory=lambda: Counter(
            "mcubridge_serial_decode_errors_total", "Total COBS/Protocol decoding errors"
        )
    )

    # MQTT
    mqtt_messages_received: Counter = field(
        default_factory=lambda: Counter(
            "mcubridge_mqtt_rx_messages_total",
            "Total MQTT messages received from broker",
            ["topic"],
        )
    )
    mqtt_messages_published: Counter = field(
        default_factory=lambda: Counter(
            "mcubridge_mqtt_tx_messages_total",
            "Total MQTT messages published to broker",
            ["topic"],
        )
    )

    # Latency
    rpc_roundtrip_latency: Histogram = field(
        default_factory=lambda: Histogram(
            "mcubridge_rpc_latency_seconds",
            "Time from MQTT command to MCU response",
            ["command"],
        )
    )

    # MCU State
    mcu_free_memory: Gauge = field(
        default_factory=lambda: Gauge(
            "mcubridge_mcu_free_memory_bytes", "Reported free heap memory on MCU"
        )
    )
    mcu_uptime_seconds: Counter = field(
        default_factory=lambda: Counter(
            "mcubridge_mcu_uptime_seconds_total", "MCU uptime estimate"
        )
    )

    # Bridge System
    bridge_restart_count: Counter = field(
        default_factory=lambda: Counter(
            "mcubridge_restart_total", "Total daemon restarts"
        )
    )
    task_failures: Counter = field(
        default_factory=lambda: Counter(
            "mcubridge_task_failures_total", "Total background task crashes", ["task"]
        )
    )

    def record_serial_tx(self, frame_size: int) -> None:
        self.serial_tx_bytes.inc(frame_size)
        self.serial_tx_frames.inc()

    def record_serial_rx(self, frame_size: int) -> None:
        self.serial_rx_bytes.inc(frame_size)
        self.serial_rx_frames.inc()

    def record_mqtt_rx(self, message: Message) -> None:
        route = topic_path(str(message.topic))
        topic_name = route.topic.value if route else "unknown"
        self.mqtt_messages_received.labels(topic=topic_name).inc()

    def record_mqtt_tx(self, topic: str) -> None:
        route = topic_path(topic)
        topic_name = route.topic.value if route else "unknown"
        self.mqtt_messages_published.labels(topic=topic_name).inc()

    def record_rpc_latency(self, command_id: int, duration: float) -> None:
        try:
            cmd_name = Command(command_id).name
        except ValueError:
            cmd_name = f"unknown_{command_id}"
        self.rpc_roundtrip_latency.labels(command=cmd_name).observe(duration)


class MetricsManager:
    """High-level service to manage and expose Prometheus metrics."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.metrics = BridgeMetrics() if enabled else None

    def record_serial_tx(self, frame_size: int) -> None:
        if self.metrics:
            self.metrics.record_serial_tx(frame_size)

    def record_serial_rx(self, frame_size: int) -> None:
        if self.metrics:
            self.metrics.record_serial_rx(frame_size)

    def record_crc_error(self) -> None:
        if self.metrics:
            self.metrics.crc_errors.inc()

    def record_decode_error(self) -> None:
        if self.metrics:
            self.metrics.decode_errors.inc()

    def record_mqtt_rx(self, message: Message) -> None:
        if self.metrics:
            self.metrics.record_mqtt_rx(message)

    def record_mqtt_tx(self, topic: str) -> None:
        if self.metrics:
            self.metrics.record_mqtt_tx(topic)

    def record_rpc_latency(self, command_id: int, duration: float) -> None:
        if self.metrics:
            self.metrics.record_rpc_latency(command_id, duration)

    def update_mcu_memory(self, free_bytes: int) -> None:
        if self.metrics:
            self.metrics.mcu_free_memory.set(free_bytes)

    def record_task_failure(self, task_name: str) -> None:
        if self.metrics:
            self.metrics.task_failures.labels(task=task_name).inc()


class PrometheusExporter:
    """HTTP server providing a Prometheus scrape endpoint."""

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port

    async def run(self) -> None:
        """Expose metrics via HTTP server."""
        logger.info("Starting Prometheus metrics server on %s:%d", self.host, self.port)
        start_http_server(self.port, self.host)
        # Keep task alive
        while True:
            await asyncio.sleep(3600)


async def publish_metrics(
    state: RuntimeState,
    config: RuntimeConfig,
    publish_callback: Any,
) -> None:
    """Periodic task to publish bridge metrics to MQTT."""
    if not config.metrics_enabled:
        return

    interval = config.status_interval
    topic = f"{config.mqtt_topic}/{Topic.METRICS.value}"

    while True:
        await asyncio.sleep(interval)
        try:
            snapshot = state.capture_snapshot()
            payload = msgspec.json.encode(snapshot)
            await publish_callback(topic, payload)
        except Exception as exc:
            logger.error("Failed to publish metrics to MQTT: %s", exc)


async def publish_bridge_snapshots(
    state: RuntimeState,
    config: RuntimeConfig,
    publish_callback: Any,
) -> None:
    """Periodic task to publish full bridge state snapshots."""
    interval = config.bridge_summary_interval
    if interval <= 0:
        return

    topic = f"{config.mqtt_topic}/{Topic.STATUS.value}/summary"

    while True:
        await asyncio.sleep(interval)
        try:
            snapshot = state.capture_snapshot()
            payload = msgspec.json.encode(snapshot)
            await publish_callback(topic, payload)
        except Exception as exc:
            logger.error("Failed to publish bridge summary: %s", exc)

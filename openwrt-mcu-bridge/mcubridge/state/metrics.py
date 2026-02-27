"""Formal metrics container for McuBridge using prometheus_client primitives."""

from __future__ import annotations

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Histogram,
)

class DaemonMetrics:
    """Formal metrics container using prometheus_client primitives."""

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()

        # MQTT Metrics
        self.mqtt_messages_published = Counter(
            "mcubridge_mqtt_messages_published_total",
            "Total MQTT messages published",
            registry=self.registry,
        )
        self.mqtt_messages_dropped = Counter(
            "mcubridge_mqtt_messages_dropped_total",
            "Total MQTT messages dropped due to queue overflow",
            registry=self.registry,
        )
        self.mqtt_spooled_messages = Counter(
            "mcubridge_mqtt_spooled_total",
            "Total messages written to durable spool",
            registry=self.registry,
        )
        self.mqtt_spool_errors = Counter(
            "mcubridge_mqtt_spool_errors_total",
            "Total errors during spool operations",
            registry=self.registry,
        )
        self.mqtt_spooled_replayed = Counter(
            "mcubridge_mqtt_spooled_replayed_total",
            "Total spooled messages successfully replayed",
            registry=self.registry,
        )

        # Console & Mailbox Overflow Metrics
        self.console_dropped_bytes = Counter(
            "mcubridge_console_dropped_bytes_total",
            "Total bytes dropped from console TX queue",
            registry=self.registry,
        )
        self.console_truncated_bytes = Counter(
            "mcubridge_console_truncated_bytes_total",
            "Total bytes truncated in console chunks",
            registry=self.registry,
        )
        self.mailbox_dropped_messages = Counter(
            "mcubridge_mailbox_dropped_messages_total",
            "Total mailbox messages dropped due to capacity limits",
            registry=self.registry,
        )
        self.mailbox_overflow_events = Counter(
            "mcubridge_mailbox_overflow_events_total",
            "Total overflow events in mailbox queues",
            registry=self.registry,
        )

        # FileSystem Metrics
        self.file_write_limit_rejections = Counter(
            "mcubridge_file_write_limit_rejections_total",
            "Total file writes rejected due to size limit",
            registry=self.registry,
        )
        self.file_storage_limit_rejections = Counter(
            "mcubridge_file_storage_limit_rejections_total",
            "Total file operations rejected due to storage quota",
            registry=self.registry,
        )

        # Serial Metrics
        self.serial_bytes_sent = Counter(
            "mcubridge_serial_bytes_sent_total",
            "Total bytes sent over serial link",
            registry=self.registry,
        )
        self.serial_bytes_received = Counter(
            "mcubridge_serial_bytes_received_total",
            "Total bytes received from serial link",
            registry=self.registry,
        )
        self.serial_frames_sent = Counter(
            "mcubridge_serial_frames_sent_total",
            "Total frames sent over serial link",
            registry=self.registry,
        )
        self.serial_frames_received = Counter(
            "mcubridge_serial_frames_received_total",
            "Total frames received from serial link",
            registry=self.registry,
        )
        self.serial_retries = Counter(
            "mcubridge_serial_retries_total",
            "Total RPC frame retransmissions",
            registry=self.registry,
        )
        self.serial_failures = Counter(
            "mcubridge_serial_failures_total",
            "Total RPC frame failures after retries",
            registry=self.registry,
        )
        self.serial_crc_errors = Counter(
            "mcubridge_serial_crc_errors_total",
            "Total frames rejected due to CRC mismatch",
            registry=self.registry,
        )
        # [SIL-2] Use Histogram for latency to get accurate percentiles
        self.serial_latency_ms = Histogram(
            "mcubridge_serial_latency_ms",
            "RPC command round-trip latency in milliseconds",
            buckets=(5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000),
            registry=self.registry,
        )

        # System Metrics
        self.handshake_attempts = Counter(
            "mcubridge_handshake_attempts_total",
            "Total serial handshake attempts",
            registry=self.registry,
        )
        self.handshake_successes = Counter(
            "mcubridge_handshake_success_total",
            "Total successful serial handshakes",
            registry=self.registry,
        )
        self.watchdog_beats = Counter(
            "mcubridge_watchdog_beats_total",
            "Total watchdog keepalive pulses emitted",
            registry=self.registry,
        )
        self.uptime_seconds = Counter(
            "mcubridge_uptime_seconds_total",
            "Total daemon uptime in seconds",
            registry=self.registry,
        )

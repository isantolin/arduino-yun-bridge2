"""Formal metrics container for McuBridge using prometheus_client primitives."""

from __future__ import annotations

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Enum,
    Histogram,
    Info,
)


class DaemonMetrics:
    """Formal metrics container using prometheus_client primitives."""

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()

        # Supervisor Metrics
        self.supervisor_failures = Counter(
            "mcubridge_supervisor_failures_total",
            "Total task failures managed by the orchestrator",
            ["task"],
            registry=self.registry,
        )

        # MQTT Metrics
        self.mqtt_messages_published = Counter(
            "mcubridge_mqtt_messages_published_total",
            "Total MQTT messages published",
            registry=self.registry,
        )
        self.mqtt_messages_received = Counter(
            "mcubridge_mqtt_messages_received_total",
            "Total MQTT messages received",
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
        self.serial_decode_errors = Counter(
            "mcubridge_serial_decode_errors_total",
            "Total frame decoding failures (COBS/Length)",
            registry=self.registry,
        )
        # [SIL-2] Use Histogram for latency to get accurate percentiles
        self.serial_latency_ms = Histogram(
            "mcubridge_serial_latency_ms",
            "RPC command round-trip latency in milliseconds",
            buckets=(5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000),
            registry=self.registry,
        )
        self.rpc_latency_ms = Histogram(
            "mcubridge_rpc_latency_ms",
            "MQTT -> MCU command round-trip latency in milliseconds",
            buckets=(50, 100, 250, 500, 1000, 2500, 5000),
            registry=self.registry,
        )

        # System Metrics
        self.unknown_command_count = Counter(
            "mcubridge_unknown_commands_total",
            "Total unknown commands received from MCU",
            registry=self.registry,
        )
        self.mcu_status_counts = Counter(
            "mcubridge_mcu_status_total",
            "Total status responses from MCU by status name",
            labelnames=["status"],
            registry=self.registry,
        )
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

        # Info Metric (build metadata — set once at startup)
        self.build_info = Info(
            "mcubridge_build",
            "Build and version information",
            registry=self.registry,
        )

        # FSM State Metrics (Enum — one gauge per state, only active state = 1.0)
        self.link_state = Enum(
            "mcubridge_link_state",
            "Serial link lifecycle state",
            states=["disconnected", "connected", "synchronized"],
            registry=self.registry,
        )
        self.handshake_state = Enum(
            "mcubridge_handshake_state",
            "Serial handshake FSM state",
            states=[
                "unsynchronized",
                "resetting",
                "syncing",
                "confirming",
                "synchronized",
                "fault",
            ],
            registry=self.registry,
        )

        # Connection/Operation Retry Metrics (labeled counter for all retry-equipped components)
        self.retries = Counter(
            "mcubridge_retries_total",
            "Total retry attempts by component",
            labelnames=["component"],
            registry=self.registry,
        )

        self._set_build_info()

    def _set_build_info(self) -> None:
        """Populate build info from package metadata."""
        import importlib.metadata

        try:
            version = importlib.metadata.version("mcubridge")
        except importlib.metadata.PackageNotFoundError:
            version = "dev"
        self.build_info.info({"version": version, "python": "3.13"})

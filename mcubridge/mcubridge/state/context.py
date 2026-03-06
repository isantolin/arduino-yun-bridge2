"""Runtime state container for the MCU Bridge daemon."""

from __future__ import annotations

import asyncio
import collections
import logging
import time
from typing import Any

import msgspec
from transitions import Machine

from ..config.const import (
    DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES,
    DEFAULT_FILE_STORAGE_QUOTA_BYTES,
    DEFAULT_FILE_SYSTEM_ROOT,
    DEFAULT_FILE_WRITE_MAX_BYTES,
    DEFAULT_MAILBOX_QUEUE_LIMIT,
    DEFAULT_MQTT_QUEUE_LIMIT,
    DEFAULT_PROCESS_MAX_CONCURRENT,
    DEFAULT_WATCHDOG_INTERVAL,
)
from ..config.settings import RuntimeConfig
from ..policy import AllowedCommandPolicy
from ..protocol.protocol import (
    DEFAULT_RETRY_LIMIT,
)
from ..protocol.topics import MQTT_DEFAULT_TOPIC_PREFIX
from ..protocol.structures import (
    BridgeSnapshot,
    HandshakeSnapshot,
    McuCapabilities,
    McuVersion,
    QueuedPublish,
    SerialLinkSnapshot,
    SerialPipelineSnapshot,
    SerialFlowStats,
    SerialThroughputStats,
    SerialLatencyStats,
    SupervisorStats,
)
from .metrics import DaemonMetrics
from .queues import BoundedByteDeque

logger = logging.getLogger("mcubridge.state")


class RuntimeState(msgspec.Struct):
    """Aggregated mutable state shared across the daemon layers."""

    metrics: DaemonMetrics = msgspec.field(default_factory=DaemonMetrics)
    serial_writer: Any | None = None

    # [SIL-2] Lifecycle FSM
    _machine: Machine = msgspec.field(
        default_factory=lambda: Machine(
            model="self",
            states=["init", "disconnected", "connecting", "subscribing", "ready"],
            initial="init",
            ignore_invalid_triggers=True,
        )
    )

    # Subscriptions & Routing
    mqtt_publish_queue: asyncio.Queue[QueuedPublish] = msgspec.field(default_factory=asyncio.Queue)
    pending_commands: dict[int, Any] = msgspec.field(default_factory=dict)
    active_processes: dict[int, Any] = msgspec.field(default_factory=dict)
    running_processes: dict[int, Any] = msgspec.field(default_factory=dict)
    process_lock: asyncio.Lock = msgspec.field(default_factory=asyncio.Lock)
    process_max_concurrent: int = DEFAULT_PROCESS_MAX_CONCURRENT
    next_pid: int = 1
    allowed_policy: AllowedCommandPolicy = msgspec.field(default_factory=lambda: AllowedCommandPolicy(entries=()))
    pending_pin_reads: dict[int, collections.deque[Any]] = msgspec.field(
        default_factory=lambda: collections.defaultdict(collections.deque)
    )

    # Buffers & Deques
    console_queue: BoundedByteDeque = msgspec.field(
        default_factory=lambda: BoundedByteDeque(max_bytes=DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES)
    )
    mailbox_queue: asyncio.Queue[bytes] = msgspec.field(
        default_factory=lambda: asyncio.Queue(maxsize=DEFAULT_MAILBOX_QUEUE_LIMIT)
    )

    # Configuration Cache
    mqtt_topic_prefix: str = MQTT_DEFAULT_TOPIC_PREFIX
    mqtt_queue_limit: int = DEFAULT_MQTT_QUEUE_LIMIT
    file_system_root: str = DEFAULT_FILE_SYSTEM_ROOT
    file_storage_quota_bytes: int = DEFAULT_FILE_STORAGE_QUOTA_BYTES
    file_write_max_bytes: int = DEFAULT_FILE_WRITE_MAX_BYTES
    watchdog_enabled: bool = True
    watchdog_interval: float = float(DEFAULT_WATCHDOG_INTERVAL)
    config_source: str = "default"

    # MCU Hardware Stats
    mcu_version: tuple[int, int] | None = None
    mcu_capabilities: McuCapabilities | None = None
    serial_tx_allowed: asyncio.Event = msgspec.field(default_factory=asyncio.Event)
    link_sync_event: asyncio.Event = msgspec.field(default_factory=asyncio.Event)

    # Handshake & Security
    link_handshake_nonce: bytes | None = None
    link_expected_tag: bytes | None = None
    link_nonce_length: int = 0
    link_nonce_counter: int = 0
    link_last_nonce_counter: int = 0
    handshake_attempts: int = 0
    handshake_successes: int = 0
    handshake_failures: int = 0
    handshake_last_error: str | None = None
    handshake_last_unix: float = 0.0
    handshake_fatal_unix: float = 0.0
    handshake_failure_streak: int = 0
    handshake_last_duration: float = 0.0
    handshake_backoff_until: float = 0.0
    handshake_fatal_detail: str | None = None
    handshake_rate_until: float = 0.0
    handshake_fatal_count: int = 0
    handshake_fatal_reason: str | None = None

    # Statistics (Mutable)
    serial_flow_stats: SerialFlowStats = msgspec.field(default_factory=SerialFlowStats)
    serial_throughput_stats: SerialThroughputStats = msgspec.field(default_factory=SerialThroughputStats)
    serial_latency_stats: SerialLatencyStats = msgspec.field(default_factory=SerialLatencyStats)
    supervisor_stats: dict[str, SupervisorStats] = msgspec.field(default_factory=dict)

    # MQTT Stats
    mqtt_dropped_messages: int = 0

    # Policies
    serial_retry_limit: int = DEFAULT_RETRY_LIMIT

    @property
    def is_connected(self) -> bool:
        return self._machine.state != "disconnected" and self.serial_writer is not None

    @property
    def is_synchronized(self) -> bool:
        return self.link_sync_event.is_set()

    def mark_synchronized(self, synchronized: bool = True) -> None:
        if synchronized:
            self.link_sync_event.set()
        else:
            self.link_sync_event.clear()

    def mark_transport_connected(self) -> None:
        pass

    def capture_snapshot(self) -> BridgeSnapshot:
        """Capture a point-in-time snapshot of the bridge state."""
        return BridgeSnapshot(
            serial_link=SerialLinkSnapshot(
                serial_connected=self.is_connected,
                link_synchronised=self.is_synchronized,
                handshake_attempts=self.handshake_attempts,
                handshake_successes=self.handshake_successes,
                handshake_failures=self.handshake_failures,
                handshake_last_error=self.handshake_last_error,
                handshake_last_unix=self.handshake_last_unix,
            ),
            handshake=HandshakeSnapshot(
                nonce=self.link_handshake_nonce.hex() if self.link_handshake_nonce else "",
                tag_verified=True if self.link_handshake_nonce else False,
            ),
            serial_pipeline=SerialPipelineSnapshot(
                tx_queue_size=self.mqtt_publish_queue.qsize(),
                rx_pending_acks=len(self.pending_commands),
            ),
            serial_flow=self.serial_flow_stats.as_snapshot(),
            mcu_version=McuVersion(
                major=self.mcu_version[0] if self.mcu_version else 0,
                minor=self.mcu_version[1] if self.mcu_version else 0,
            ) if self.mcu_version else None,
            capabilities=msgspec.to_builtins(self.mcu_capabilities) if self.mcu_capabilities else None,
        )

    def record_serial_tx(self, size: int) -> None:
        self.metrics.serial_tx_bytes.inc(size)
        self.metrics.serial_tx_frames.inc()
        self.serial_flow_stats.commands_sent += 1

    def record_serial_rx(self, size: int) -> None:
        self.metrics.serial_rx_bytes.inc(size)
        self.metrics.serial_rx_frames.inc()

    def record_serial_decode_error(self) -> None:
        self.metrics.decode_errors.inc()

    def record_mqtt_publish(self, topic: str) -> None:
        self.metrics.mqtt_messages_published.labels(topic=topic).inc()

    def record_watchdog_beat(self) -> None:
        self.metrics.mcu_uptime_seconds.inc(DEFAULT_WATCHDOG_INTERVAL)

    def record_task_failure(self, name: str) -> None:
        self.metrics.task_failures.labels(task=name).inc()

    def record_handshake_attempt(self) -> None:
        self.handshake_attempts += 1

    def record_handshake_fatal(self, reason: str) -> None:
        self.handshake_fatal_count += 1
        self.handshake_fatal_reason = reason
        self.handshake_fatal_unix = time.time()

    def record_handshake_success(self, duration: float) -> None:
        self.handshake_successes += 1
        self.handshake_failure_streak = 0
        self.handshake_last_duration = duration
        self.handshake_last_unix = time.time()

    def record_handshake_failure(self, reason: str) -> None:
        self.handshake_failures += 1
        self.handshake_failure_streak += 1
        self.handshake_last_error = reason
        self.handshake_last_unix = time.time()

    def record_supervisor_failure(self, name: str, backoff: float, exc: Exception) -> None:
        """Record an internal service task failure."""
        stats = self.supervisor_stats.setdefault(name, SupervisorStats())
        stats.restarts += 1
        stats.last_failure_unix = time.time()
        stats.last_failure_reason = str(exc)
        self.record_task_failure(name)

    def stash_mqtt_message(self, topic: str, payload: bytes) -> None:
        """Stash a message in the local spool (not implemented)."""
        pass

    async def flush_mqtt_spool(self, publish_callback: Any) -> int:
        """Flush spooled messages (not implemented)."""
        return 0


def create_runtime_state(config: RuntimeConfig) -> RuntimeState:
    """Initialize the runtime state with configuration limits."""
    return RuntimeState(
        serial_retry_limit=config.serial_retry_attempts,
        mqtt_topic_prefix=config.mqtt_topic,
        mqtt_queue_limit=config.mqtt_queue_limit,
        file_system_root=config.file_system_root,
        file_storage_quota_bytes=config.file_storage_quota_bytes,
        file_write_max_bytes=config.file_write_max_bytes,
        watchdog_enabled=config.watchdog_enabled,
        watchdog_interval=float(config.watchdog_interval),
    )

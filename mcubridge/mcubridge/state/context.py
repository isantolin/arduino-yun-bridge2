"""Centralised Runtime State management for the MCU Bridge (SIL-2).
This module coordinates metrics, task supervision, and MQTT/Serial state transitions.
"""

from __future__ import annotations

import asyncio
import collections
import os
import time
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Mapping,
    TypeVar,
    cast,
)

import msgspec
import structlog

from mcubridge.config.const import (
    DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES,
    DEFAULT_FILE_STORAGE_QUOTA_BYTES,
    DEFAULT_FILE_SYSTEM_ROOT,
    DEFAULT_FILE_WRITE_MAX_BYTES,
    DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT,
    DEFAULT_MAILBOX_QUEUE_LIMIT,
    DEFAULT_MQTT_QUEUE_LIMIT,
    DEFAULT_MQTT_SPOOL_DIR,
    DEFAULT_PENDING_PIN_REQUESTS,
    DEFAULT_PROCESS_TIMEOUT,
)
from mcubridge.protocol.protocol import Command, Status
from mcubridge.protocol.spec_model import ManagedProcess
from mcubridge.protocol.structures import (
    BridgeStatus,
    QueuedPublish,
    SerialPipelineSnapshot,
    SerialThroughputStats,
    SupervisorStats,
)

from .metrics import DaemonMetrics
from .queues import BridgeQueue

if TYPE_CHECKING:
    from mcubridge.config.settings import RuntimeConfig

logger = structlog.get_logger("mcubridge.state")

SpoolSnapshot = dict[str, int | float]
T = TypeVar("T")


def resolve_command_id(command_id: int) -> str:
    """Resolve command/status ID to human-readable name."""
    try:
        return Command(command_id).name
    except ValueError:
        pass
    try:
        return Status(command_id).name
    except ValueError:
        return f"0x{command_id:02X}"


class RuntimeState(msgspec.Struct):
    """Monolithic Runtime State for the MCU Bridge daemon (SIL-2)."""

    metrics: DaemonMetrics = msgspec.field(default_factory=DaemonMetrics)
    last_serial_activity: float = 0.0
    serial_rx_bytes: int = 0
    serial_tx_bytes: int = 0
    serial_errors: int = 0
    serial_ack_timeout_ms: int = 0
    serial_last_frame_unix: float = 0.0
    serial_connected: bool = False
    mcu_uptime_ms: int = 0
    mcu_free_ram: int = 0
    mcu_version: Any = "0.0.0"
    mcu_capabilities: int = 0
    mcu_id: str = ""
    mqtt_connected: bool = False
    mqtt_reconnects: int = 0
    mqtt_dropped_messages: int = 0
    mqtt_spooled_replayed: int = 0
    mqtt_spool_degraded: bool = False
    mqtt_spool_failure_reason: str | None = None
    mqtt_spool_dir: str = DEFAULT_MQTT_SPOOL_DIR
    mqtt_spool_limit: int = 0
    allow_non_tmp_paths: bool = False
    mqtt_spool_retry_attempts: int = 0
    mqtt_spool_backoff_until: float = 0.0
    mqtt_spool_last_error: str | None = None
    mqtt_spool_recoveries: int = 0
    mqtt_spool_last_trim_unix: float = 0.0
    mqtt_spool_dropped_limit: int = 0
    mqtt_spool_trim_events: int = 0
    mqtt_spool_corrupt_dropped: int = 0
    mqtt_queue_limit: int = DEFAULT_MQTT_QUEUE_LIMIT
    mcu_is_paused: bool = False
    next_pid: int = 1
    topic_authorization: Any | None = None
    process_timeout: int = DEFAULT_PROCESS_TIMEOUT
    file_system_root: str = DEFAULT_FILE_SYSTEM_ROOT
    file_write_max_bytes: int = DEFAULT_FILE_WRITE_MAX_BYTES
    file_storage_quota_bytes: int = DEFAULT_FILE_STORAGE_QUOTA_BYTES
    file_storage_bytes_used: int = 0
    file_write_limit_rejections: int = 0
    console_queue_limit_bytes: int = DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES
    console_queue_bytes: int = 0
    console_dropped_chunks: int = 0
    console_truncated_chunks: int = 0
    console_truncated_bytes: int = 0
    console_dropped_bytes: int = 0
    mailbox_queue_limit: int = DEFAULT_MAILBOX_QUEUE_LIMIT
    mailbox_queue_bytes_limit: int = DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT
    pending_pin_request_limit: int = DEFAULT_PENDING_PIN_REQUESTS
    config_source: str = "default"
    handshake_attempts: int = 0
    handshake_last_fail_reason: str | None = None
    handshake_backoff_until: float = 0.0
    handshake_successes: int = 0
    handshake_failures: int = 0
    handshake_failure_streak: int = 0
    handshake_last_duration: float = 0.0
    handshake_fatal_count: int = 0
    handshake_fatal_unix: float = 0.0
    handshake_fatal_reason: str | None = None
    handshake_fatal_detail: str | None = None
    handshake_rate_until: float = 0.0
    link_handshake_nonce: bytes | None = None
    link_nonce_counter: int = 0
    link_last_nonce_counter: int = 0
    link_expected_tag: bytes | None = None
    link_nonce_length: int = 0
    serial_decode_errors: int = 0
    watchdog_beats: int = 0
    mailbox_dropped_messages: int = 0
    mailbox_truncated_messages: int = 0
    mailbox_truncated_bytes: int = 0
    mailbox_dropped_bytes: int = 0
    mailbox_incoming_dropped_messages: int = 0
    mailbox_incoming_truncated_messages: int = 0
    mailbox_incoming_truncated_bytes: int = 0
    mailbox_incoming_dropped_bytes: int = 0
    mailbox_queue_bytes: int = 0
    mailbox_incoming_queue_bytes: int = 0
    mailbox_incoming_topic: str = ""
    file_storage_limit_rejections: int = 0
    watchdog_enabled: bool = False
    watchdog_interval: int = 0
    last_watchdog_beat: float = 0.0
    initialize_prometheus: bool = False
    link_state: str = "uninitialized"

    # --- Complex Structs ---
    supervisor_stats: dict[str, SupervisorStats] = msgspec.field(default_factory=dict)
    serial_flow_stats: SerialPipelineSnapshot = msgspec.field(
        default_factory=SerialPipelineSnapshot
    )
    serial_throughput_stats: SerialThroughputStats = msgspec.field(
        default_factory=SerialThroughputStats
    )
    serial_latency_stats: Any = msgspec.field(default_factory=dict)

    # --- Dynamic Collections ---
    mqtt_publish_queue: asyncio.Queue[QueuedPublish] = msgspec.field(
        default_factory=asyncio.Queue
    )
    mqtt_drop_counts: dict[str, int] = msgspec.field(default_factory=dict)
    last_spool_snapshot: dict[str, int | float] = msgspec.field(default_factory=dict)
    datastore: dict[str, str] = msgspec.field(default_factory=dict)
    mailbox_queue: BridgeQueue[bytes] = msgspec.field(default_factory=BridgeQueue)
    mailbox_incoming_queue: BridgeQueue[bytes] = msgspec.field(
        default_factory=BridgeQueue
    )
    serial_tx_allowed: asyncio.Event = msgspec.field(default_factory=asyncio.Event)
    console_to_mcu_queue: BridgeQueue[bytes] = msgspec.field(
        default_factory=BridgeQueue
    )
    running_processes: dict[int, ManagedProcess] = msgspec.field(default_factory=dict)
    process_lock: asyncio.Lock = msgspec.field(default_factory=asyncio.Lock)
    allowed_policy: Any = msgspec.field(default_factory=dict)
    pending_digital_reads: collections.deque[Any] = msgspec.field(
        default_factory=collections.deque
    )
    pending_analog_reads: collections.deque[Any] = msgspec.field(
        default_factory=collections.deque
    )

    # Internal components
    mqtt_spool: Any | None = None
    link_sync_event: asyncio.Event | None = None

    def __post_init__(self) -> None:
        """Initialize complex state machine components."""
        self.serial_tx_allowed.set()

    def configure(self, config: RuntimeConfig) -> None:
        """Apply validated configuration to runtime state."""
        self.mqtt_queue_limit = config.mqtt_queue_limit
        self.mqtt_spool_dir = config.mqtt_spool_dir
        self.mqtt_spool_limit = config.mqtt_queue_limit * 4
        self.allow_non_tmp_paths = config.allow_non_tmp_paths
        self.file_system_root = config.file_system_root
        self.file_write_max_bytes = config.file_write_max_bytes
        self.file_storage_quota_bytes = config.file_storage_quota_bytes
        self.process_timeout = config.process_timeout
        self.console_queue_limit_bytes = config.console_queue_limit_bytes
        self.mailbox_queue_limit = config.mailbox_queue_limit
        self.mailbox_queue_bytes_limit = config.mailbox_queue_bytes_limit
        self.pending_pin_request_limit = config.pending_pin_request_limit

        # [SIL-2] Re-initialize queues with correct limits
        self.console_to_mcu_queue = BridgeQueue[bytes](
            max_bytes=self.console_queue_limit_bytes,
        )

        def _create_spool(subdir: str) -> BridgeQueue[bytes]:
            directory = None
            if self.allow_non_tmp_paths or self.file_system_root.startswith("/tmp/"):
                directory = Path(self.file_system_root) / subdir
            return BridgeQueue[bytes](
                directory=directory, max_items=self.mailbox_queue_limit
            )

        self.mailbox_queue = _create_spool("mailbox_out")
        self.mailbox_incoming_queue = _create_spool("mailbox_in")

    def mark_synchronized(self) -> None:
        """Update link state."""
        self.link_state = "synchronized"
        if self.link_sync_event:
            self.link_sync_event.set()
        self.metrics.link_state.state("synchronized")

    def record_serial_rx(self, n: int) -> None:
        """Record serial receive activity."""
        self.serial_rx_bytes += n
        self.last_serial_activity = time.time()
        self.metrics.serial_rx_bytes.inc(n)

    def record_serial_tx(self, n: int) -> None:
        """Record serial transmit activity."""
        self.serial_tx_bytes += n
        self.last_serial_activity = time.time()
        self.metrics.serial_tx_bytes.inc(n)

    def record_serial_error(self) -> None:
        """Record serial communication error."""
        self.serial_errors += 1
        self.metrics.serial_errors.inc()

    def record_mqtt_drop(self, topic: str) -> None:
        """Record a dropped MQTT message due to overflow."""
        self.mqtt_drop_counts[topic] = self.mqtt_drop_counts.get(topic, 0) + 1
        self.mqtt_dropped_messages += 1
        self.metrics.mqtt_messages_dropped.inc()

    def record_mqtt_spool(self) -> None:
        """Update metrics for spooled messages."""
        self.metrics.mqtt_messages_spooled.inc()

    def record_mqtt_replay(self) -> None:
        """Update metrics for replayed messages."""
        self.mqtt_spooled_replayed += 1
        self.metrics.mqtt_messages_replayed.inc()

    def update_mcu_stats(self, ram: int, uptime: int) -> None:
        """Update MCU hardware statistics."""
        self.mcu_free_ram = ram
        self.mcu_uptime_ms = uptime
        self.metrics.mcu_free_ram.set(ram)
        self.metrics.mcu_uptime.set(uptime / 1000.0)

    def register_process(self, pid: int, proc: ManagedProcess) -> None:
        """Track a new background process."""
        self.running_processes[pid] = proc
        self.metrics.active_processes.set(len(self.running_processes))

    def pop_console_chunk(self) -> bytes | None:
        """Pops oldest console chunk from queue (SIL-2)."""
        chunk = self.console_to_mcu_queue.popleft()
        if chunk:
            self.console_queue_bytes = self.console_to_mcu_queue.bytes
        return chunk

    def build_metrics_snapshot(self) -> BridgeStatus:
        """Create a point-in-time metrics snapshot."""
        return BridgeStatus(
            uptime_daemon=0,  # Placeholder
            uptime_mcu_ms=self.mcu_uptime_ms,
            free_ram_mcu=self.mcu_free_ram,
            serial_rx_bytes=self.serial_rx_bytes,
            serial_tx_bytes=self.serial_tx_bytes,
            serial_errors=self.serial_errors,
            mqtt_connected=self.mqtt_connected,
            mqtt_dropped=self.mqtt_dropped_messages,
            mqtt_spooled=0,  # Placeholder
            active_processes=len(self.running_processes),
            mcu_version=self.mcu_version,
            mcu_id=self.mcu_id,
        )

    def initialize_spool(self) -> None:
        """Lazy initialization of MQTT persistent spool (SIL-2)."""
        if self.mqtt_spool:
            return
        from mcubridge.mqtt.spool import MQTTPublishSpool

        try:
            self.mqtt_spool = MQTTPublishSpool(
                self.mqtt_spool_dir, self.mqtt_spool_limit
            )
            self.mqtt_spool_degraded = False
            self.mqtt_spool_failure_reason = None
        except Exception as exc:
            self.disable_mqtt_spool(str(exc), schedule_retry=True)

    def disable_mqtt_spool(self, reason: str, schedule_retry: bool = True) -> None:
        """Safely disable spooling and record failure."""
        self.mqtt_spool_degraded = True
        self.mqtt_spool_failure_reason = reason
        self.mqtt_spool = None
        if schedule_retry:
            self.mqtt_spool_retry_attempts += 1
            self.mqtt_spool_backoff_until = time.time() + min(
                300, 5 * (2**self.mqtt_spool_retry_attempts)
            )

    def apply_spool_observation(self, observation: Mapping[str, Any]) -> None:
        """Update internal state from spool statistics."""
        if "corrupt_dropped" in observation:
            self.mqtt_spool_corrupt_dropped = int(observation["corrupt_dropped"])
        if "dropped_due_to_limit" in observation:
            self.mqtt_spool_dropped_limit = int(observation["dropped_due_to_limit"])
        if "trim_events" in observation:
            self.mqtt_spool_trim_events = int(observation["trim_events"])
        if "last_trim_unix" in observation:
            self.mqtt_spool_last_trim_unix = float(observation["last_trim_unix"])

    def record_supervisor_failure(self, name: str, exc: Exception) -> None:
        """Track a service failure in the supervisor."""
        stats = self.supervisor_stats.get(name, SupervisorStats())
        stats.restarts += 1
        stats.last_exception = str(exc)
        self.supervisor_stats[name] = stats

    def mark_supervisor_healthy(self, name: str) -> None:
        """Reset supervisor failure tracking."""
        if name in self.supervisor_stats:
            stats = self.supervisor_stats[name]
            stats.restarts = 0
            stats.last_exception = None

    def mark_transport_connected(self) -> None:
        """Update connection state."""
        self.serial_connected = True
        self.link_state = "connected"
        self.metrics.link_state.state("connected")

    def mark_transport_disconnected(self) -> None:
        """Update connection state."""
        self.serial_connected = False
        self.link_state = "uninitialized"
        self.metrics.link_state.state("uninitialized")

    def record_handshake_attempt(self) -> None:
        """Increment handshake retry counter."""
        self.handshake_attempts += 1

    @property
    def is_synchronized(self) -> bool:
        """True if the link state machine is in synchronized state."""
        return self.link_state == "synchronized"

    def record_serial_flow_event(self, commands_sent: int) -> None:
        """Update flow control statistics (stubbed for compatibility)."""
        pass

    def record_serial_pipeline_event(self, inflight: int) -> None:
        """Update pipeline depth statistics (stubbed for compatibility)."""
        pass

    def record_watchdog_beat(self) -> None:
        """Track watchdog activity."""
        self.watchdog_beats += 1
        self.last_watchdog_beat = time.time()

    def record_handshake_success(self) -> None:
        """Record a successful handshake completion."""
        self.handshake_successes += 1
        self.handshake_failure_streak = 0
        self.mark_synchronized()

    def record_handshake_failure(self, reason: str) -> None:
        """Record a failed handshake attempt."""
        self.handshake_failures += 1
        self.handshake_failure_streak += 1
        self.handshake_last_fail_reason = reason

    def record_handshake_fatal(self, reason: str, detail: str | None = None) -> None:
        """Record a fatal handshake failure."""
        self.handshake_fatal_count += 1
        self.handshake_fatal_unix = time.time()
        self.handshake_fatal_reason = reason
        self.handshake_fatal_detail = detail

    def record_serial_decode_error(self) -> None:
        """Track malformed serial frames."""
        self.serial_decode_errors += 1

    def record_serial_latency(self, latency_ms: float) -> None:
        """Update serial latency statistics."""
        # Simple moving average placeholder
        self.serial_flow_stats = msgspec.structs.replace(
            self.serial_flow_stats, avg_latency_ms=latency_ms
        )

    def cleanup(self) -> None:
        """Finalize and close all persistent resources."""
        if self.mqtt_spool:
            cast(Any, self.mqtt_spool).close()
            self.mqtt_spool = None
        self.mailbox_queue.close()
        self.mailbox_incoming_queue.close()
        self.console_to_mcu_queue.close()

    def configure_spool(self, directory: str, limit: int) -> None:
        """Dynamically update spool configuration."""
        self.mqtt_spool_dir = directory
        self.mqtt_spool_limit = limit
        if self.mqtt_spool:
            cast(Any, self.mqtt_spool).close()
            self.mqtt_spool = None

    def enqueue_mailbox_message(self, data: bytes) -> None:
        """Add message to outgoing mailbox queue."""
        self.mailbox_queue.append(data)

    def enqueue_mailbox_incoming(self, data: bytes) -> None:
        """Add message to incoming mailbox queue."""
        self.mailbox_incoming_queue.append(data)

    def pop_mailbox_message(self) -> bytes | None:
        """Retrieve oldest outgoing mailbox message."""
        return self.mailbox_queue.popleft()

    def requeue_mailbox_message_front(self, data: bytes) -> None:
        """Push message back to the front of outgoing queue."""
        self.mailbox_queue.appendleft(data)

    def record_mqtt_spool_error(self, reason: str = "") -> None:
        """Track spooling failures."""
        self.mqtt_spool_last_error = reason


def create_runtime_state(
    config: RuntimeConfig | dict[str, Any], initialize_spool: bool = False
) -> RuntimeState:
    """Bootstrap factory for the monolithic RuntimeState."""
    from mcubridge.config.settings import RuntimeConfig

    cfg = msgspec.convert(config, RuntimeConfig) if isinstance(config, dict) else config
    state = RuntimeState(
        mqtt_publish_queue=asyncio.Queue(cfg.mqtt_queue_limit),
        serial_tx_allowed=asyncio.Event(),
        process_lock=asyncio.Lock(),
        link_sync_event=asyncio.Event(),
    )
    state.serial_tx_allowed.set()
    state.configure(cfg)
    state.configure_spool(cfg.mqtt_spool_dir, cfg.mqtt_queue_limit * 4)
    if initialize_spool:
        state.initialize_spool()
    return state


def collect_system_metrics() -> dict[str, int | float | str]:
    """Gather Linux system host metrics."""
    import psutil

    return {
        "cpu_percent": psutil.cpu_percent(),
        "memory_percent": psutil.virtual_memory().percent,
        "load_avg_1": os.getloadavg()[0],
    }

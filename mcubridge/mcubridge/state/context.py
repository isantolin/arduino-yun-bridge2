"""Runtime state container for the MCU Bridge daemon."""

from __future__ import annotations

import asyncio
import collections
import contextlib
import functools
import os
import tempfile
import structlog
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

import msgspec
import psutil

from ..config.const import (
    DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES,
    DEFAULT_FILE_STORAGE_QUOTA_BYTES,
    DEFAULT_FILE_SYSTEM_ROOT,
    DEFAULT_FILE_WRITE_MAX_BYTES,
    DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT,
    DEFAULT_MAILBOX_QUEUE_LIMIT,
    DEFAULT_MQTT_QUEUE_LIMIT,
    DEFAULT_MQTT_SPOOL_DIR,
    DEFAULT_PENDING_PIN_REQUESTS,
    DEFAULT_PROCESS_MAX_CONCURRENT,
    DEFAULT_PROCESS_MAX_OUTPUT_BYTES,
    DEFAULT_PROCESS_TIMEOUT,
    DEFAULT_SERIAL_RESPONSE_TIMEOUT,
    DEFAULT_SERIAL_RETRY_TIMEOUT,
    DEFAULT_WATCHDOG_INTERVAL,
    SPOOL_BACKOFF_MAX_SECONDS,
    SPOOL_BACKOFF_MIN_SECONDS,
)
from ..config.settings import RuntimeConfig
from ..protocol.structures import QueuedPublish
from ..mqtt.spool import MQTTPublishSpool, MQTTSpoolError
from ..policy import AllowedCommandPolicy, TopicAuthorization
from ..protocol import protocol
from ..protocol.protocol import (
    DEFAULT_RETRY_LIMIT,
    Command,
    Status,
)
from ..protocol.structures import (
    BridgeSnapshot,
    HandshakeSnapshot,
    McuCapabilities,
    McuVersion,
    PendingPinRequest,
    SerialFlowStats,
    SerialLatencyStats,
    SerialLinkSnapshot,
    SerialPipelineSnapshot,
    SerialThroughputStats,
    SupervisorStats,
)
from .metrics import DaemonMetrics
from .queues import BridgeQueue
from transitions import Machine

logger = structlog.get_logger("mcubridge.state")

SpoolSnapshot = dict[str, int | float]


__all__: Final[tuple[str, ...]] = (
    "McuCapabilities",
    "RuntimeState",
    "PendingPinRequest",
    "ManagedProcess",
    "create_runtime_state",
    "HandshakeSnapshot",
    "SerialLinkSnapshot",
    "McuVersion",
    "SerialPipelineSnapshot",
    "BridgeSnapshot",
)

# FSM States for ManagedProcess
PROCESS_STATE_STARTING = "STARTING"
PROCESS_STATE_RUNNING = "RUNNING"
PROCESS_STATE_DRAINING = "DRAINING"
PROCESS_STATE_FINISHED = "FINISHED"
PROCESS_STATE_ZOMBIE = "ZOMBIE"


@functools.lru_cache(maxsize=256)
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


def _status_label(code: int | None) -> str:
    """Resolve status code to human-readable label using optimized Enum lookup."""
    if code is None:
        return "unknown"
    try:
        return Status(code).name
    except ValueError:
        return f"0x{code:02X}"


@dataclass
class ManagedProcess:
    """Managed subprocess with output buffers."""

    pid: int
    command: str = ""
    handle: Any | None = None
    stdout_buffer: collections.deque[int] = field(
        default_factory=lambda: collections.deque(maxlen=4096)
    )
    stderr_buffer: collections.deque[int] = field(
        default_factory=lambda: collections.deque(maxlen=4096)
    )
    exit_code: int | None = None
    io_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    fsm_state: str = PROCESS_STATE_STARTING
    _machine: Any = None

    def __post_init__(self) -> None:
        self._machine = Machine(
            model=self,
            states=[
                PROCESS_STATE_STARTING,
                PROCESS_STATE_RUNNING,
                PROCESS_STATE_DRAINING,
                PROCESS_STATE_FINISHED,
                PROCESS_STATE_ZOMBIE,
            ],
            transitions=[
                {
                    "trigger": "start",
                    "source": PROCESS_STATE_STARTING,
                    "dest": PROCESS_STATE_RUNNING,
                },
                {
                    "trigger": "sigchld",
                    "source": PROCESS_STATE_RUNNING,
                    "dest": PROCESS_STATE_DRAINING,
                },
                {
                    "trigger": "io_complete",
                    "source": PROCESS_STATE_DRAINING,
                    "dest": PROCESS_STATE_FINISHED,
                },
                {
                    "trigger": "finalize",
                    "source": PROCESS_STATE_FINISHED,
                    "dest": PROCESS_STATE_ZOMBIE,
                },
                {"trigger": "force_kill", "source": "*", "dest": PROCESS_STATE_ZOMBIE},
            ],
            initial=PROCESS_STATE_STARTING,
            queued=True,
            model_attribute="fsm_state",
            ignore_invalid_triggers=True,
            auto_transitions=False,
        )

    if TYPE_CHECKING:

        def trigger(self, event: str, *args: Any, **kwargs: Any) -> bool:
            """FSM trigger placeholder."""
            ...

    def append_output(
        self,
        stdout_chunk: bytes,
        stderr_chunk: bytes,
        *,
        limit: int,
    ) -> tuple[bool, bool]:
        if stdout_chunk:
            self.stdout_buffer.extend(stdout_chunk)
        if stderr_chunk:
            self.stderr_buffer.extend(stderr_chunk)
        return False, False

    def pop_payload(self, budget: int) -> tuple[bytes, bytes, bool, bool]:
        out_bytes = bytes(self.stdout_buffer)
        err_bytes = bytes(self.stderr_buffer)

        out_len = min(len(out_bytes), budget)
        stdout_chunk = out_bytes[:out_len]

        remaining = budget - out_len
        err_len = min(len(err_bytes), remaining)
        stderr_chunk = err_bytes[:err_len]

        for _ in range(out_len):
            self.stdout_buffer.popleft()
        for _ in range(err_len):
            self.stderr_buffer.popleft()

        return (
            stdout_chunk,
            stderr_chunk,
            bool(self.stdout_buffer),
            bool(self.stderr_buffer),
        )

    def is_drained(self) -> bool:
        # [FSM] Must be in FINISHED/ZOMBIE state to be drained
        if self.fsm_state not in (PROCESS_STATE_FINISHED, PROCESS_STATE_ZOMBIE):
            return False
        return not self.stdout_buffer and not self.stderr_buffer


def collect_system_metrics() -> dict[str, Any]:
    """Collect system-level metrics using native library conversions."""
    try:
        proc = psutil.Process()
        with proc.oneshot():
            mem = psutil.virtual_memory()
            load = (
                psutil.getloadavg()
                if hasattr(psutil, "getloadavg")
                else (0.0, 0.0, 0.0)
            )

            # Disk usage for root and /tmp (volatile RAM disk on OpenWrt)
            root_disk = psutil.disk_usage("/")
            try:
                tmp_disk = psutil.disk_usage("/tmp")
            except OSError:
                tmp_disk = None

            # [SIL-2] Direct mapping from native library structures
            result: dict[str, Any] = {
                "cpu_percent": psutil.cpu_percent(interval=None),
                "cpu_count": psutil.cpu_count() or 1,
                "memory_total_bytes": mem.total,
                "memory_available_bytes": mem.available,
                "memory_percent": mem.percent,
                "load_avg_1m": load[0],
                "load_avg_5m": load[1],
                "load_avg_15m": load[2],
                "disk_root_total_bytes": root_disk.total,
                "disk_root_used_bytes": root_disk.used,
                "disk_root_free_bytes": root_disk.free,
                "disk_root_percent": root_disk.percent,
                "temperature_celsius": (
                    next(
                        (
                            t[0].current
                            for n, t in psutil.sensors_temperatures().items()
                            if n in ("cpu_thermal", "coretemp", "soc_thermal") and t
                        ),
                        None,
                    )
                    if hasattr(psutil, "sensors_temperatures")
                    else None
                ),
            }
            if tmp_disk is not None:
                result["disk_tmp_total_bytes"] = tmp_disk.total
                result["disk_tmp_used_bytes"] = tmp_disk.used
                result["disk_tmp_free_bytes"] = tmp_disk.free
                result["disk_tmp_percent"] = tmp_disk.percent
            return result
    except (psutil.Error, RuntimeError, OSError):
        return {}


class RuntimeState(msgspec.Struct):
    """Aggregated mutable state shared across the daemon layers."""

    metrics: DaemonMetrics = msgspec.field(default_factory=DaemonMetrics)
    serial_writer: asyncio.BaseTransport | None = None

    # [SIL-2] Lifecycle FSM (Single Source of Truth)
    _machine: Any = msgspec.field(
        default_factory=lambda: Machine(
            model="self",
            states=["disconnected", "connected", "synchronized"],
            transitions=[
                {
                    "trigger": "connect",
                    "source": ["disconnected", "connected", "synchronized"],
                    "dest": "connected",
                },
                {
                    "trigger": "synchronize",
                    "source": ["connected", "synchronized"],
                    "dest": "synchronized",
                },
                {"trigger": "disconnect", "source": "*", "dest": "disconnected"},
            ],
            initial="disconnected",
            queued=True,
            model_attribute="state",
            ignore_invalid_triggers=True,
        )
    )

    if TYPE_CHECKING:

        def trigger(self, event: str, *args: Any, **kwargs: Any) -> bool: ...

    @property
    def is_connected(self) -> bool:
        return self._machine.state in {"connected", "synchronized"}

    @property
    def is_synchronized(self) -> bool:
        return self._machine.state == "synchronized"

    def mark_transport_connected(self) -> None:
        """Signal that serial connection is open but unsynchronized."""
        self._machine.trigger("connect")
        self.metrics.link_state.state("connected")

    def mark_transport_disconnected(self) -> None:
        """Signal that serial connection is lost."""
        self._machine.trigger("disconnect")
        self.metrics.link_state.state("disconnected")
        if self.link_sync_event:
            self.link_sync_event.clear()

    def mark_synchronized(self) -> None:
        """Signal that protocol handshake is successfully completed."""
        self._machine.trigger("synchronize")
        self.metrics.link_state.state("synchronized")
        if self.link_sync_event:
            self.link_sync_event.set()

    mqtt_publish_queue: asyncio.Queue[QueuedPublish] = msgspec.field(
        default_factory=lambda: asyncio.Queue[QueuedPublish](),  # noqa: PLW0108
    )
    mqtt_queue_limit: int = DEFAULT_MQTT_QUEUE_LIMIT
    mqtt_drop_counts: dict[str, int] = msgspec.field(
        default_factory=lambda: {}
    )  # noqa: PLW0108
    mqtt_spool: MQTTPublishSpool | None = None
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
    _last_spool_snapshot: SpoolSnapshot = msgspec.field(
        default_factory=lambda: {}
    )  # noqa: PLW0108
    datastore: dict[str, str] = msgspec.field(
        default_factory=lambda: {}
    )  # noqa: PLW0108

    # [SIL-2] Mailbox queues persist to /tmp through diskcache when enabled.
    mailbox_queue: BridgeQueue[bytes] = msgspec.field(
        default_factory=lambda: BridgeQueue[bytes](),  # noqa: PLW0108
    )
    mailbox_incoming_queue: BridgeQueue[bytes] = msgspec.field(
        default_factory=lambda: BridgeQueue[bytes](),  # noqa: PLW0108
    )

    mcu_is_paused: bool = False
    serial_tx_allowed: asyncio.Event = msgspec.field(default_factory=asyncio.Event)
    console_to_mcu_queue: BridgeQueue[bytes] = msgspec.field(
        default_factory=lambda: BridgeQueue[bytes](),  # noqa: PLW0108
    )
    console_queue_limit_bytes: int = DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES

    console_queue_bytes: int = 0
    console_dropped_chunks: int = 0
    console_truncated_chunks: int = 0
    console_truncated_bytes: int = 0
    console_dropped_bytes: int = 0
    running_processes: dict[int, ManagedProcess] = msgspec.field(
        default_factory=lambda: {}
    )  # noqa: PLW0108
    process_lock: asyncio.Lock = msgspec.field(default_factory=asyncio.Lock)
    next_pid: int = 1
    allowed_policy: AllowedCommandPolicy = msgspec.field(
        default_factory=lambda: AllowedCommandPolicy.create_empty(),  # noqa: PLW0108
    )
    topic_authorization: TopicAuthorization | None = None
    process_timeout: int = DEFAULT_PROCESS_TIMEOUT
    file_system_root: str = DEFAULT_FILE_SYSTEM_ROOT
    file_write_max_bytes: int = DEFAULT_FILE_WRITE_MAX_BYTES
    file_storage_quota_bytes: int = DEFAULT_FILE_STORAGE_QUOTA_BYTES
    file_storage_bytes_used: int = 0
    file_write_limit_rejections: int = 0
    file_storage_limit_rejections: int = 0
    mqtt_topic_prefix: str = protocol.MQTT_DEFAULT_TOPIC_PREFIX
    watchdog_enabled: bool = False
    watchdog_interval: float = DEFAULT_WATCHDOG_INTERVAL
    last_watchdog_beat: float = 0.0
    pending_digital_reads: collections.deque[PendingPinRequest] = msgspec.field(
        default_factory=lambda: collections.deque[PendingPinRequest](),  # noqa: PLW0108
    )
    pending_analog_reads: collections.deque[PendingPinRequest] = msgspec.field(
        default_factory=lambda: collections.deque[PendingPinRequest](),  # noqa: PLW0108
    )
    mailbox_incoming_topic: str = ""
    mailbox_queue_limit: int = DEFAULT_MAILBOX_QUEUE_LIMIT
    mailbox_queue_bytes_limit: int = DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT
    pending_pin_request_limit: int = DEFAULT_PENDING_PIN_REQUESTS
    mailbox_queue_bytes: int = 0
    mailbox_dropped_messages: int = 0
    mailbox_truncated_messages: int = 0
    mailbox_truncated_bytes: int = 0
    mailbox_dropped_bytes: int = 0
    mailbox_outgoing_overflow_events: int = 0
    mailbox_incoming_queue_bytes: int = 0
    mailbox_incoming_dropped_messages: int = 0
    mailbox_incoming_truncated_messages: int = 0
    mailbox_incoming_truncated_bytes: int = 0
    mailbox_incoming_dropped_bytes: int = 0
    mailbox_incoming_overflow_events: int = 0
    mcu_version: tuple[int, int, int] | None = None
    mcu_capabilities: McuCapabilities | None = None
    link_handshake_nonce: bytes | None = None
    link_sync_event: asyncio.Event = msgspec.field(default_factory=asyncio.Event)
    link_expected_tag: bytes | None = None
    link_nonce_length: int = 0
    link_nonce_counter: int = 0
    link_last_nonce_counter: int = 0
    handshake_failure_streak: int = 0
    handshake_backoff_until: float = 0.0
    handshake_rate_until: float = 0.0
    last_handshake_error: str | None = None
    last_handshake_unix: float = 0.0
    handshake_last_duration: float = 0.0
    handshake_fatal_count: int = 0
    handshake_fatal_reason: str | None = None
    handshake_fatal_detail: str | None = None
    handshake_fatal_unix: float = 0.0
    _handshake_last_started: float = 0.0
    serial_flow_stats: SerialFlowStats = msgspec.field(default_factory=SerialFlowStats)
    serial_throughput_stats: SerialThroughputStats = msgspec.field(
        default_factory=SerialThroughputStats
    )
    serial_latency_stats: SerialLatencyStats = msgspec.field(
        default_factory=SerialLatencyStats
    )
    serial_pipeline_inflight: dict[str, Any] | None = None
    serial_pipeline_last: dict[str, Any] | None = None
    process_output_limit: int = DEFAULT_PROCESS_MAX_OUTPUT_BYTES
    process_max_concurrent: int = DEFAULT_PROCESS_MAX_CONCURRENT
    unknown_command_ids: int = 0
    config_source: str = "uci"
    serial_ack_timeout_ms: int = int(DEFAULT_SERIAL_RETRY_TIMEOUT * 1000)
    serial_response_timeout_ms: int = int(DEFAULT_SERIAL_RESPONSE_TIMEOUT * 1000)
    serial_retry_limit: int = DEFAULT_RETRY_LIMIT
    mcu_status_counters: dict[str, int] = msgspec.field(default_factory=lambda: {})
    supervisor_stats: dict[str, SupervisorStats] = msgspec.field(
        default_factory=lambda: {}
    )

    # Metrics (Synchronized with Prometheus)
    mqtt_messages_published: int = 0
    mqtt_dropped_messages: int = 0
    mqtt_spooled_messages: int = 0
    mqtt_spool_errors: int = 0
    serial_bytes_sent: int = 0
    serial_bytes_received: int = 0
    serial_frames_sent: int = 0
    serial_frames_received: int = 0
    serial_crc_errors: int = 0
    serial_decode_errors: int = 0
    handshake_attempts: int = 0
    handshake_successes: int = 0
    watchdog_beats: int = 0

    @property
    def handshake_failures(self) -> int:
        """Total handshake failures (Calculated)."""
        return self.handshake_attempts - self.handshake_successes

    @property
    def allowed_commands(self) -> tuple[str, ...]:
        """Return the current allowed command list from policy."""
        return self.allowed_policy.as_tuple()

    def record_mqtt_publish(self) -> None:
        """Increment MQTT publish counter."""
        self.mqtt_messages_published += 1
        self.metrics.mqtt_messages_published.inc()

    def record_mqtt_drop(self, topic: str) -> None:
        """Record a dropped MQTT message due to overflow."""
        self.mqtt_drop_counts[topic] = self.mqtt_drop_counts.get(topic, 0) + 1
        self.mqtt_dropped_messages += 1
        self.metrics.mqtt_messages_dropped.inc()

    def record_mqtt_spool(self) -> None:
        """Record message written to durable spool."""
        self.mqtt_spooled_messages += 1
        self.metrics.mqtt_spooled_messages.inc()

    def record_mqtt_spool_error(self) -> None:
        """Record error during spool operation."""
        self.mqtt_spool_errors += 1
        self.metrics.mqtt_spool_errors.inc()

    def record_serial_tx(self, nbytes: int) -> None:
        """Record serial transmission metrics."""
        self.serial_bytes_sent += nbytes
        self.serial_frames_sent += 1
        self.metrics.serial_bytes_sent.inc(nbytes)
        self.metrics.serial_frames_sent.inc()
        self.serial_throughput_stats.record_tx(nbytes)

    def record_serial_rx(self, nbytes: int) -> None:
        """Record serial reception metrics."""
        self.serial_bytes_received += nbytes
        self.serial_frames_received += 1
        self.metrics.serial_bytes_received.inc(nbytes)
        self.metrics.serial_frames_received.inc()
        self.serial_throughput_stats.record_rx(nbytes)

    def record_serial_crc_error(self) -> None:
        """Record serial frame CRC mismatch."""
        self.serial_crc_errors += 1
        self.metrics.serial_crc_errors.inc()

    def record_serial_decode_error(self) -> None:
        """Record serial frame decoding failure."""
        self.serial_decode_errors += 1
        self.metrics.serial_decode_errors.inc()

    def record_handshake_attempt(self) -> None:
        """Start tracking a handshake attempt."""
        self.last_handshake_unix = time.time()
        self._handshake_last_started = time.monotonic()
        self.handshake_attempts += 1
        self.metrics.handshake_attempts.inc()

    def record_handshake_success(self) -> None:
        """Record successful link synchronization."""
        self.handshake_failure_streak = 0
        self.handshake_backoff_until = 0.0
        self.last_handshake_error = None
        self.last_handshake_unix = time.time()
        self.handshake_last_duration = self._handshake_duration_since_start()
        self.mark_synchronized()
        self.handshake_successes += 1
        self.metrics.handshake_successes.inc()

    def record_handshake_failure(self, reason: str) -> None:
        """Record failed link synchronization."""
        self.handshake_failure_streak += 1
        self.last_handshake_error = reason
        self.last_handshake_unix = time.time()
        self.handshake_last_duration = self._handshake_duration_since_start()
        self.mark_transport_connected()

    def record_watchdog_beat(self, timestamp: float | None = None) -> None:
        self.watchdog_beats += 1
        self.metrics.watchdog_beats.inc()
        self.last_watchdog_beat = timestamp or time.time()

    def record_supervisor_failure(
        self, name: str, backoff: float, exc: Exception
    ) -> None:
        """Record an internal service task failure."""
        stats = self.supervisor_stats.setdefault(name, SupervisorStats())
        stats.restarts += 1
        stats.last_failure_unix = time.time()
        stats.last_exception = f"{exc.__class__.__name__}: {exc}"
        stats.backoff_seconds = backoff

    def configure(self, config: RuntimeConfig) -> None:
        # [SIL-2] Close existing persistent queues if they are being replaced
        # to ensure that resources (like diskcache files) are released.
        self.mailbox_queue.close()
        self.mailbox_incoming_queue.close()
        self.console_to_mcu_queue.close()

        if config.allowed_policy is not None:
            self.allowed_policy = config.allowed_policy
        self.process_timeout = config.process_timeout
        self.file_system_root = config.file_system_root
        self.allow_non_tmp_paths = config.allow_non_tmp_paths
        self.file_write_max_bytes = config.file_write_max_bytes
        self.file_storage_quota_bytes = config.file_storage_quota_bytes
        self.mqtt_topic_prefix = config.mqtt_topic
        self.console_queue_limit_bytes = config.console_queue_limit_bytes
        self.mailbox_queue_limit = config.mailbox_queue_limit
        self.mailbox_queue_bytes_limit = config.mailbox_queue_bytes_limit
        self.mqtt_queue_limit = config.mqtt_queue_limit
        self.watchdog_enabled = config.watchdog_enabled
        self.watchdog_interval = config.watchdog_interval
        self.pending_pin_request_limit = config.pending_pin_request_limit
        self.topic_authorization = config.topic_authorization
        self.process_output_limit = config.process_max_output_bytes
        self.process_max_concurrent = config.process_max_concurrent

        self.console_to_mcu_queue = BridgeQueue[bytes](
            max_bytes=self.console_queue_limit_bytes,
        )

        def _create_spool(subdir: str) -> BridgeQueue[bytes]:
            directory = None
            if self.allow_non_tmp_paths or self.file_system_root.startswith("/tmp/"):
                directory = Path(self.file_system_root) / subdir

            return BridgeQueue[bytes](
                directory=directory,
                max_items=self.mailbox_queue_limit,
            )

        self.mailbox_queue = _create_spool("mailbox_out")
        self.mailbox_incoming_queue = _create_spool("mailbox_in")

    def mark_supervisor_healthy(self, name: str) -> None:
        """Reset backoff status for a healthy supervisor."""
        stats = self.supervisor_stats.get(name)
        if stats:
            stats.backoff_seconds = 0.0
            stats.fatal = False

    def enqueue_console_chunk(self, chunk: bytes) -> None:
        if not chunk:
            return
        evt = self.console_to_mcu_queue.append(chunk)
        if evt.truncated_bytes:
            self.console_truncated_chunks += 1
            self.console_truncated_bytes += evt.truncated_bytes
        if evt.dropped_chunks:
            self.console_dropped_chunks += evt.dropped_chunks
            self.console_dropped_bytes += evt.dropped_bytes
        if not evt.success:
            self.console_dropped_chunks += 1
            self.console_dropped_bytes += len(chunk)
        else:
            self.console_queue_bytes = self.console_to_mcu_queue.bytes

    def pop_console_chunk(self) -> bytes:
        chunk = self.console_to_mcu_queue.popleft()
        self.console_queue_bytes = self.console_to_mcu_queue.bytes
        return chunk or b""

    def requeue_console_chunk_front(self, chunk: bytes) -> None:
        if not chunk:
            return
        self.console_to_mcu_queue.appendleft(chunk)
        self.console_queue_bytes = self.console_to_mcu_queue.bytes

    def _mailbox_overflow(
        self, queue_len: int, payload_len: int, *, incoming: bool
    ) -> bool:
        """Return True if the mailbox queue is full. Updates overflow counters."""
        if queue_len < self.mailbox_queue_limit:
            return False
        if incoming:
            self.mailbox_incoming_dropped_messages += 1
            self.mailbox_incoming_dropped_bytes += payload_len
            self.mailbox_incoming_overflow_events += 1
        else:
            self.mailbox_dropped_messages += 1
            self.mailbox_dropped_bytes += payload_len
            self.mailbox_outgoing_overflow_events += 1
        return True

    def enqueue_mailbox_message(self, payload: bytes) -> bool:
        if self._mailbox_overflow(
            len(self.mailbox_queue), len(payload), incoming=False
        ):
            return False
        evt = self.mailbox_queue.append(payload)
        if evt.success:
            self.mailbox_queue_bytes += len(payload)
            return True
        return False

    def pop_mailbox_message(self) -> bytes | None:
        msg = self.mailbox_queue.popleft()
        if msg is not None:
            self.mailbox_queue_bytes = max(0, self.mailbox_queue_bytes - len(msg))
        return msg

    def requeue_mailbox_message_front(self, payload: bytes) -> None:
        evt = self.mailbox_queue.appendleft(payload)
        if evt.success:
            self.mailbox_queue_bytes += len(payload)

    def enqueue_mailbox_incoming(self, payload: bytes) -> bool:
        if self._mailbox_overflow(
            len(self.mailbox_incoming_queue), len(payload), incoming=True
        ):
            return False
        evt = self.mailbox_incoming_queue.append(payload)
        if evt.success:
            self.mailbox_incoming_queue_bytes += len(payload)
            return True
        return False

    def pop_mailbox_incoming(self) -> bytes | None:
        msg = self.mailbox_incoming_queue.popleft()
        if msg is not None:
            self.mailbox_incoming_queue_bytes = max(
                0, self.mailbox_incoming_queue_bytes - len(msg)
            )
        return msg

    def sync_console_queue_limits(self) -> None:
        # Managed automatically by BoundedByteDeque
        self.console_queue_bytes = self.console_to_mcu_queue.bytes

    def sync_mailbox_limits(self, queue: Any) -> None:
        # Limits are enforced on enqueue and update_limits handles console trimming.
        pass

    def update_mailbox_bytes(self) -> None:
        # Automatically tracked in our wrappers
        pass

    def record_handshake_fatal(self, reason: str, detail: str | None = None) -> None:
        self.handshake_fatal_count += 1
        self.handshake_fatal_reason = reason
        self.handshake_fatal_detail = detail
        self.handshake_fatal_unix = time.time()

    def record_serial_flow_event(self, event: str) -> None:
        stats = self.serial_flow_stats
        if event == "sent":
            stats.commands_sent += 1
        elif event == "ack":
            stats.commands_acked += 1
        elif event == "retry":
            stats.retries += 1
            self.metrics.serial_retries.inc()
        elif event == "failure":
            stats.failures += 1
            self.metrics.serial_failures.inc()
        else:
            return
        stats.last_event_unix = time.time()

    def record_serial_pipeline_event(self, event: Mapping[str, Any]) -> None:
        name = str(event.get("event", ""))
        command_id = int(event.get("command_id", 0))
        attempt = int(event.get("attempt", 1) or 1)
        timestamp = float(event.get("timestamp") or time.time())
        acked = bool(event.get("ack_received"))
        status_code = event.get("status")

        if name == "start":
            self.serial_pipeline_inflight = {
                "command_id": command_id,
                "command_name": resolve_command_id(command_id),
                "attempt": attempt,
                "started_unix": timestamp,
                "acknowledged": False,
                "last_event": "start",
                "last_event_unix": timestamp,
            }
            return

        inf = self.serial_pipeline_inflight
        if name == "ack" and inf:
            inf.update(
                {
                    "acknowledged": True,
                    "ack_unix": timestamp,
                    "last_event": "ack",
                    "last_event_unix": timestamp,
                }
            )
            return

        if name in {"success", "failure", "abandoned"}:
            payload = {
                "command_id": command_id,
                "command_name": resolve_command_id(command_id),
                "attempt": attempt,
                "event": name,
                "completed_unix": timestamp,
                "status_code": status_code,
                "status_name": _status_label(cast(int, status_code)),
                "acknowledged": acked or bool(inf and inf.get("acknowledged")),
            }
            if inf:
                payload["started_unix"] = inf.get("started_unix")
                try:
                    start_val = float(inf.get("started_unix", timestamp))
                    payload["duration"] = max(0.0, timestamp - start_val)
                except (ValueError, TypeError):
                    payload["duration"] = 0.0
            self.serial_pipeline_last = payload
            self.serial_pipeline_inflight = None

    def record_unknown_command_id(self, command_id: int) -> None:
        self.unknown_command_ids += 1

    def record_rpc_latency_ms(self, latency_ms: float) -> None:
        self.serial_latency_stats.record(latency_ms)
        self.metrics.serial_latency_ms.observe(latency_ms)

    def build_serial_pipeline_snapshot(self) -> SerialPipelineSnapshot:
        return SerialPipelineSnapshot(
            inflight=self.serial_pipeline_inflight,
            last_completion=self.serial_pipeline_last,
        )

    def _current_spool_snapshot(self) -> SpoolSnapshot:
        """Fetch current spool statistics or fallback to cached snapshot."""
        if self.mqtt_spool:
            self._last_spool_snapshot = self.mqtt_spool.snapshot()
        return self._last_spool_snapshot

    def record_mcu_status(self, status: Status) -> None:
        self.mcu_status_counters[status.name] = (
            self.mcu_status_counters.get(status.name, 0) + 1
        )

    def apply_handshake_stats(self, observation: Mapping[str, Any]) -> None:
        """Update internal state from external handshake statistics."""
        # [SIL-2] Bulk conversion using msgspec to eliminate manual coercion
        try:
            snap = msgspec.convert(observation, HandshakeSnapshot, strict=False)
            self.handshake_attempts = snap.attempts
            self.handshake_successes = snap.successes
            self.handshake_failure_streak = snap.failure_streak
            self.handshake_last_duration = snap.last_duration
            self.last_handshake_unix = snap.last_unix
            self.handshake_backoff_until = snap.backoff_until
            self.handshake_rate_until = snap.rate_limit_until
        except (msgspec.MsgspecError, ValueError, TypeError):
            pass

    def _apply_spool_observation(self, observation: Mapping[str, Any]) -> None:
        """Update internal state from spool statistics."""
        # [SIL-2] Static assignment to avoid reflection overhead and string manipulation
        if "corrupt_dropped" in observation:
            self.mqtt_spool_corrupt_dropped = msgspec.convert(
                observation["corrupt_dropped"], int
            )
        if "dropped_due_to_limit" in observation:
            self.mqtt_spool_dropped_limit = msgspec.convert(
                observation["dropped_due_to_limit"], int
            )
        if "trim_events" in observation:
            self.mqtt_spool_trim_events = msgspec.convert(
                observation["trim_events"], int
            )
        if "last_trim_unix" in observation:
            self.mqtt_spool_last_trim_unix = msgspec.convert(
                observation["last_trim_unix"], float
            )

    def configure_spool(self, directory: str, limit: int) -> None:
        if self.mqtt_spool:
            self.mqtt_spool.close()
            self.mqtt_spool = None
        self.mqtt_spool_dir = directory
        self.mqtt_spool_limit = max(0, limit)

    def initialize_spool(self) -> None:
        if not self.mqtt_spool_dir or self.mqtt_spool_limit <= 0:
            self._disable_mqtt_spool("disabled", schedule_retry=False)
            return
        try:
            if self.mqtt_spool:
                self.mqtt_spool.close()
                self.mqtt_spool = None
            spool_obj = MQTTPublishSpool(
                self.mqtt_spool_dir,
                self.mqtt_spool_limit,
                on_fallback=self._on_spool_fallback,
            )
            self.mqtt_spool = spool_obj
            if spool_obj.is_degraded:
                self.mqtt_spool_degraded = True
                self.mqtt_spool_failure_reason = (
                    spool_obj.failure_reason or "initialization_failed"
                )
                self.mqtt_spool_last_error = spool_obj.last_error
            else:
                self.mqtt_spool_degraded = False
                self.mqtt_spool_failure_reason = None
        except (OSError, MQTTSpoolError) as exc:
            self._handle_mqtt_spool_failure("initialization_failed", exc=exc)

    async def ensure_spool(self) -> bool:
        if self.mqtt_spool:
            return True
        if (
            not self.mqtt_spool_dir
            or self.mqtt_spool_limit <= 0
            or self._spool_backoff_remaining() > 0
        ):
            return False
        try:
            self.mqtt_spool = await asyncio.to_thread(
                MQTTPublishSpool,
                self.mqtt_spool_dir,
                self.mqtt_spool_limit,
                on_fallback=self._on_spool_fallback,
            )
            if self.mqtt_spool.is_degraded:
                self.mqtt_spool_degraded = True
                self.mqtt_spool_failure_reason = (
                    self.mqtt_spool.failure_reason or "reactivation_failed"
                )
                self.mqtt_spool_last_error = self.mqtt_spool.last_error
                return False
            self.mqtt_spool_degraded = False
            self.mqtt_spool_failure_reason = None
            self.mqtt_spool_recoveries += 1
            return True
        except (OSError, MQTTSpoolError) as exc:
            self._handle_mqtt_spool_failure("reactivation_failed", exc=exc)
            return False

    def _spool_backoff_remaining(self) -> float:
        return (
            max(0.0, self.mqtt_spool_backoff_until - time.monotonic())
            if self.mqtt_spool_backoff_until > 0
            else 0.0
        )

    def _disable_mqtt_spool(self, reason: str, schedule_retry: bool = True) -> None:
        if self.mqtt_spool:
            with contextlib.suppress(OSError, AttributeError):
                self.mqtt_spool.close()
        self.mqtt_spool = None
        self.mqtt_spool_degraded = True
        self.mqtt_spool_failure_reason = reason
        if schedule_retry:
            self._schedule_spool_retry()

    def _schedule_spool_retry(self) -> None:
        """Calculate and set exponential backoff for spool retry."""
        self.mqtt_spool_retry_attempts = min(self.mqtt_spool_retry_attempts + 1, 6)
        delay = min(
            SPOOL_BACKOFF_MIN_SECONDS * (2 ** (self.mqtt_spool_retry_attempts - 1)),
            SPOOL_BACKOFF_MAX_SECONDS,
        )
        self.mqtt_spool_backoff_until = time.monotonic() + delay

    def _handle_mqtt_spool_failure(
        self, reason: str, exc: BaseException | None = None
    ) -> None:
        self.record_mqtt_spool_error()
        if exc:
            self.mqtt_spool_last_error = str(exc)
        self._disable_mqtt_spool(reason)

    def _on_spool_fallback(self, reason: str, exc: BaseException | None = None) -> None:
        self.mqtt_spool_degraded = True
        self.mqtt_spool_failure_reason = reason
        if exc:
            self.mqtt_spool_last_error = str(exc)
        self.record_mqtt_spool_error()

    async def stash_mqtt_message(self, message: QueuedPublish) -> bool:
        if not await self.ensure_spool():
            return False
        spool = self.mqtt_spool
        if spool is None:
            return False
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, spool.append, message)
            self.record_mqtt_spool()
            return True
        except (MQTTSpoolError, OSError) as exc:
            self._handle_mqtt_spool_failure("append_failed", exc=exc)
            return False

    async def flush_mqtt_spool(self) -> None:
        if not await self.ensure_spool():
            return
        spool = self.mqtt_spool
        if spool is None:
            return
        while self.mqtt_publish_queue.qsize() < self.mqtt_queue_limit:
            try:
                msg = await asyncio.to_thread(spool.pop_next)
                if not msg:
                    break
                props = list(msg.user_properties) + [("bridge-spooled", "1")]
                final_msg = msgspec.structs.replace(msg, user_properties=props)
                try:
                    self.mqtt_publish_queue.put_nowait(final_msg)
                    self.mqtt_spooled_replayed += 1
                except asyncio.QueueFull:
                    # Re-spool if queue became full between qsize check and put
                    await asyncio.to_thread(spool.requeue, msg)
                    break
            except (MQTTSpoolError, OSError) as exc:
                self._handle_mqtt_spool_failure("pop_failed", exc=exc)
                break

    def build_metrics_snapshot(self) -> dict[str, Any]:
        # [SIL-2] Return rich objects where possible to preserve attribute-based API
        return {
            "serial": self.serial_flow_stats,
            "serial_throughput": self.serial_throughput_stats,
            "serial_latency": self.serial_latency_stats.as_dict(),
            "mqtt_drop_counts": dict(self.mqtt_drop_counts),
            "queue_depths": {
                "mqtt": self.mqtt_publish_queue.qsize(),
                "console": self.console_to_mcu_queue.bytes,
                "mailbox_outgoing": len(self.mailbox_queue),
                "mailbox_incoming": len(self.mailbox_incoming_queue),
                "running_processes": len(self.running_processes),
            },
            "handshake": self.build_handshake_snapshot(),
            "link_synchronised": self.is_synchronized,
            "system": collect_system_metrics(),
            "spool_pending": self.mqtt_spool.pending if self.mqtt_spool else 0,
            "spool_limit": self.mqtt_spool.limit if self.mqtt_spool else 0,
            "bridge": self.build_bridge_snapshot(),
        }

    def build_handshake_snapshot(self) -> HandshakeSnapshot:
        return HandshakeSnapshot(
            synchronised=self.is_synchronized,
            attempts=self.handshake_attempts,
            successes=self.handshake_successes,
            failures=self.handshake_failures,
            failure_streak=self.handshake_failure_streak,
            last_error=self.last_handshake_error,
            last_unix=self.last_handshake_unix,
            last_duration=self.handshake_last_duration,
            backoff_until=self.handshake_backoff_until,
            rate_limit_until=self.handshake_rate_until,
            fatal_count=self.handshake_fatal_count,
            fatal_reason=self.handshake_fatal_reason,
            fatal_detail=self.handshake_fatal_detail,
            fatal_unix=self.handshake_fatal_unix,
            pending_nonce=bool(self.link_handshake_nonce),
            nonce_length=self.link_nonce_length,
        )

    def build_bridge_snapshot(self) -> BridgeSnapshot:
        return BridgeSnapshot(
            serial_link=SerialLinkSnapshot(
                connected=self.is_connected,
                writer_attached=self.serial_writer is not None,
                synchronised=self.is_synchronized,
            ),
            handshake=self.build_handshake_snapshot(),
            serial_pipeline=self.build_serial_pipeline_snapshot(),
            serial_flow=self.serial_flow_stats.as_snapshot(),
            mcu_version=McuVersion(*self.mcu_version) if self.mcu_version else None,
            capabilities=(
                self.mcu_capabilities.as_dict() if self.mcu_capabilities else None
            ),
        )

    def _handshake_duration_since_start(self) -> float:
        if self._handshake_last_started <= 0.0:
            return 0.0
        return max(0.0, time.monotonic() - self._handshake_last_started)

    def cleanup(self) -> None:
        _sup = contextlib.suppress(OSError, RuntimeError, AttributeError)

        with _sup:
            if self.mqtt_spool is not None:
                self.mqtt_spool.close()
                self.mqtt_spool = None

        # [SIL-2] Close persistent queues to release file handles
        with _sup:
            self.mailbox_queue.close()
        with _sup:
            self.mailbox_incoming_queue.close()

        with _sup:
            # Drain the MQTT queue instead of nullifying
            while not self.mqtt_publish_queue.empty():
                try:
                    self.mqtt_publish_queue.get_nowait()
                except (asyncio.QueueEmpty, ValueError, RuntimeError):
                    break

        # [SIL-2] Terminate all running processes to release pipes/sockets
        with _sup:
            if self.running_processes:
                for slot in list(self.running_processes.values()):
                    handle = getattr(slot, "handle", None)
                    if handle:
                        with contextlib.suppress(OSError, ProcessLookupError):
                            handle.terminate()

        with _sup:
            self.serial_tx_allowed.clear()
            self.link_sync_event.clear()
            self.running_processes.clear()


def create_runtime_state(
    config: RuntimeConfig | dict[str, Any], initialize_spool: bool = False
) -> RuntimeState:
    from ..config.settings import RuntimeConfig

    cfg = msgspec.convert(config, RuntimeConfig) if isinstance(config, dict) else config

    if os.environ.get("MCUBRIDGE_TEST_MODE") == "1":
        # [SIL-2] Automated isolation for test environment.
        # Force unique paths if they match system defaults or current const defaults.
        from ..config import const
        _SYS_FS = "/tmp/yun_files"
        _SYS_SPOOL = "/tmp/mcubridge/spool"

        if cfg.file_system_root in (_SYS_FS, const.DEFAULT_FILE_SYSTEM_ROOT):
            new_fs = tempfile.mkdtemp(prefix="mcubridge-test-fs-")
            cfg = msgspec.structs.replace(cfg, file_system_root=new_fs)
        if cfg.mqtt_spool_dir in (_SYS_SPOOL, const.DEFAULT_MQTT_SPOOL_DIR):
            new_spool = tempfile.mkdtemp(prefix="mcubridge-test-spool-")
            cfg = msgspec.structs.replace(cfg, mqtt_spool_dir=new_spool)

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

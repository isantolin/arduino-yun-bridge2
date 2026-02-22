"""Runtime state container for the MCU Bridge daemon."""

from __future__ import annotations

import asyncio
import collections
import logging
import time
from asyncio.subprocess import Process
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

import msgspec
import psutil
from aiomqtt.message import Message
from prometheus_client import CollectorRegistry, Summary
from transitions import Machine

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
from ..mqtt.messages import QueuedPublish
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
    CapabilitiesFeatures,
    HandshakeSnapshot,
    McuVersion,
    SerialLinkSnapshot,
    SerialPipelineSnapshot,
)
from .queues import BoundedByteDeque

logger = logging.getLogger("mcubridge.state")

SpoolSnapshot = dict[str, int | float]


def _serial_pipeline_base_payload(command_id: int, attempt: int) -> dict[str, Any]:
    return {
        "command_id": command_id,
        "command_name": resolve_command_id(command_id),
        "attempt": attempt,
    }


def _coerce_snapshot_int(snapshot: Mapping[str, Any], name: str, current: int) -> int:
    value = snapshot.get(name)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return current
    return current


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
    if code is None:
        return "unknown"
    try:
        return Status(code).name
    except ValueError:
        return f"0x{code:02X}"


class McuCapabilities(msgspec.Struct):
    """Hardware capabilities reported by the MCU."""

    protocol_version: int = 0
    board_arch: int = 0
    num_digital_pins: int = 0
    num_analog_inputs: int = 0
    features: CapabilitiesFeatures | None = None

    @property
    def has_watchdog(self) -> bool:
        return bool(self.features and self.features.watchdog)

    @property
    def has_rle(self) -> bool:
        return bool(self.features and self.features.rle)

    @property
    def debug_frames(self) -> bool:
        return bool(self.features and self.features.debug_frames)

    @property
    def debug_io(self) -> bool:
        return bool(self.features and self.features.debug_io)

    @property
    def has_eeprom(self) -> bool:
        return bool(self.features and self.features.eeprom)

    @property
    def has_dac(self) -> bool:
        return bool(self.features and self.features.dac)

    @property
    def has_hw_serial1(self) -> bool:
        return bool(self.features and self.features.hw_serial1)

    @property
    def has_fpu(self) -> bool:
        return bool(self.features and self.features.fpu)

    @property
    def is_3v3_logic(self) -> bool:
        return bool(self.features and self.features.logic_3v3)

    @property
    def has_large_buffer(self) -> bool:
        return bool(self.features and self.features.large_buffer)

    @property
    def has_i2c(self) -> bool:
        return bool(self.features and self.features.i2c)

    def as_dict(self) -> dict[str, Any]:
        """Convert to dictionary including expanded boolean flags."""
        # [SIL-2] Combined static and dynamic data for telemetry.
        res = msgspec.structs.asdict(self)
        res.update({
            "has_watchdog": self.has_watchdog,
            "has_rle": self.has_rle,
            "has_eeprom": self.has_eeprom,
            "has_dac": self.has_dac,
            "has_hw_serial1": self.has_hw_serial1,
            "has_fpu": self.has_fpu,
            "is_3v3_logic": self.is_3v3_logic,
            "has_large_buffer": self.has_large_buffer,
            "has_i2c": self.has_i2c,
        })
        return res


class PendingPinRequest(msgspec.Struct):
    """Pending pin read request."""

    pin: int
    reply_context: Message | None = None


def _bytearray_factory() -> bytearray:
    return bytearray()


def _asyncio_lock_factory() -> asyncio.Lock:
    return asyncio.Lock()


def _asyncio_event_factory() -> asyncio.Event:
    return asyncio.Event()


@dataclass
class ManagedProcess:
    """Managed subprocess with output buffers."""

    pid: int
    command: str = ""
    handle: Process | None = None
    stdout_buffer: bytearray = field(default_factory=_bytearray_factory)
    stderr_buffer: bytearray = field(default_factory=_bytearray_factory)
    exit_code: int | None = None
    io_lock: asyncio.Lock = field(default_factory=_asyncio_lock_factory)
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
            initial=PROCESS_STATE_STARTING,
            model_attribute="fsm_state",
            auto_transitions=False,
            ignore_invalid_triggers=True,
        )
        self._machine.add_transition("start", PROCESS_STATE_STARTING, PROCESS_STATE_RUNNING)
        self._machine.add_transition("sigchld", PROCESS_STATE_RUNNING, PROCESS_STATE_DRAINING)
        self._machine.add_transition("io_complete", PROCESS_STATE_DRAINING, PROCESS_STATE_FINISHED)
        self._machine.add_transition("finalize", PROCESS_STATE_FINISHED, PROCESS_STATE_ZOMBIE)
        # Allow force cleanup from any state
        self._machine.add_transition("force_kill", "*", PROCESS_STATE_ZOMBIE)

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
        truncated_stdout = _append_with_limit(
            self.stdout_buffer,
            stdout_chunk,
            limit,
        )
        truncated_stderr = _append_with_limit(
            self.stderr_buffer,
            stderr_chunk,
            limit,
        )
        return truncated_stdout, truncated_stderr

    def pop_payload(
        self,
        budget: int,
    ) -> tuple[bytes, bytes, bool, bool]:
        return _trim_process_buffers(
            self.stdout_buffer,
            self.stderr_buffer,
            budget,
        )

    def is_drained(self) -> bool:
        """Return True if both stdout and stderr buffers are empty."""
        return not self.stdout_buffer and not self.stderr_buffer


def _append_with_limit(
    buffer: bytearray,
    chunk: bytes,
    limit: int,
) -> bool:
    if not chunk:
        return False
    buffer.extend(chunk)
    if limit <= 0 or len(buffer) <= limit:
        return False
    excess = len(buffer) - limit
    del buffer[:excess]
    return True


def _trim_process_buffers(
    stdout_buffer: bytearray,
    stderr_buffer: bytearray,
    budget: int,
) -> tuple[bytes, bytes, bool, bool]:
    stdout_len = min(len(stdout_buffer), budget)
    stdout_chunk = bytes(stdout_buffer[:stdout_len])
    del stdout_buffer[:stdout_len]

    remaining = budget - len(stdout_chunk)
    stderr_len = min(len(stderr_buffer), remaining)
    stderr_chunk = bytes(stderr_buffer[:stderr_len])
    del stderr_buffer[:stderr_len]

    truncated_out = len(stdout_buffer) > 0
    truncated_err = len(stderr_buffer) > 0
    return stdout_chunk, stderr_chunk, truncated_out, truncated_err


def _latency_bucket_counts_factory() -> list[int]:
    return [0] * len(LATENCY_BUCKETS_MS)


def _mqtt_publish_queue_factory() -> asyncio.Queue[QueuedPublish]:
    return asyncio.Queue()


def _mqtt_drop_counts_factory() -> dict[str, int]:
    return {}


def _last_spool_snapshot_factory() -> SpoolSnapshot:
    return {}


def _datastore_factory() -> dict[str, str]:
    return {}


def _running_processes_factory() -> dict[int, ManagedProcess]:
    return {}


def _pending_pin_reads_factory() -> collections.deque[PendingPinRequest]:
    return collections.deque()


def _mcu_status_counters_factory() -> dict[str, int]:
    return {}


def _supervisor_stats_factory() -> dict[str, SupervisorStats]:
    return {}


def _serial_tx_allowed_factory() -> asyncio.Event:
    evt = asyncio.Event()
    evt.set()
    return evt


def _policy_factory() -> AllowedCommandPolicy:
    return AllowedCommandPolicy.from_iterable(())


def _bounded_byte_deque_factory() -> BoundedByteDeque:
    return BoundedByteDeque()


def _topic_authorization_factory() -> TopicAuthorization:
    return TopicAuthorization()


def _serial_flow_stats_factory() -> SerialFlowStats:
    return SerialFlowStats()


def _serial_throughput_stats_factory() -> SerialThroughputStats:
    return SerialThroughputStats()


def _serial_latency_stats_factory() -> SerialLatencyStats:
    return SerialLatencyStats()


# [EXTENDED METRICS] Latency histogram bucket boundaries in milliseconds
LATENCY_BUCKETS_MS: tuple[float, ...] = (5.0, 10.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1000.0, 2500.0)


class SerialThroughputStats(msgspec.Struct):
    """Serial link throughput counters for observability.

    [SIL-2] Simple counters with monotonic increments only.
    """

    bytes_sent: int = 0
    bytes_received: int = 0
    frames_sent: int = 0
    frames_received: int = 0
    last_tx_unix: float = 0.0
    last_rx_unix: float = 0.0

    def record_tx(self, nbytes: int) -> None:
        self.bytes_sent += nbytes
        self.frames_sent += 1
        self.last_tx_unix = time.time()

    def record_rx(self, nbytes: int) -> None:
        self.bytes_received += nbytes
        self.frames_received += 1
        self.last_rx_unix = time.time()

    def as_dict(self) -> dict[str, Any]:
        return msgspec.structs.asdict(self)


class SerialLatencyStats(msgspec.Struct):
    """RPC command latency histogram for performance monitoring.

    [SIL-2] Fixed bucket boundaries, no dynamic allocation.
    Buckets represent cumulative counts (Prometheus histogram style).

    [OPTIMIZATION] Now also tracks prometheus Summary for native percentiles.
    The Summary provides p50/p90/p99 without manual bucket management.
    """

    # Histogram bucket counts (cumulative, le=bucket_ms) - kept for JSON snapshot compatibility
    bucket_counts: list[int] = msgspec.field(default_factory=_latency_bucket_counts_factory)
    # Total observations above largest bucket
    overflow_count: int = 0
    # Running totals for average calculation
    total_observations: int = 0
    total_latency_ms: float = 0.0
    # Min/max tracking
    min_latency_ms: float = float("inf")
    max_latency_ms: float = 0.0
    # [NEW] Prometheus Summary for native percentiles
    _summary: Summary | None = None
    _registry: CollectorRegistry | None = None

    def initialize_prometheus(self, registry: CollectorRegistry | None = None) -> None:
        """Initialize prometheus Summary metrics.

        Args:
            registry: Optional custom registry. If None, uses default.
        """
        self._registry = registry
        # Summary provides p50 (0.5), p90 (0.9), p99 (0.99) quantiles automatically
        self._summary = Summary(
            "mcubridge_rpc_latency_seconds",
            "RPC command round-trip latency",
            registry=registry,
        )

    def record(self, latency_ms: float) -> None:
        """Record a latency observation into histogram buckets and Summary."""
        self.total_observations += 1
        self.total_latency_ms += latency_ms
        if latency_ms < self.min_latency_ms:
            self.min_latency_ms = latency_ms
        if latency_ms > self.max_latency_ms:
            self.max_latency_ms = latency_ms

        # Cumulative bucket counts (le style) for JSON compatibility
        for i, bucket in enumerate(LATENCY_BUCKETS_MS):
            if latency_ms <= bucket:
                self.bucket_counts[i] += 1
        if latency_ms > LATENCY_BUCKETS_MS[-1]:
            self.overflow_count += 1

        # Record to prometheus Summary (in seconds)
        if self._summary is not None:
            self._summary.observe(latency_ms / 1000.0)

    def as_dict(self) -> dict[str, Any]:
        avg = self.total_latency_ms / self.total_observations if self.total_observations > 0 else 0.0
        return {
            "buckets": {f"le_{int(b)}ms": self.bucket_counts[i] for i, b in enumerate(LATENCY_BUCKETS_MS)},
            "overflow": self.overflow_count,
            "count": self.total_observations,
            "sum_ms": self.total_latency_ms,
            "avg_ms": avg,
            "min_ms": self.min_latency_ms if self.total_observations > 0 else 0.0,
            "max_ms": self.max_latency_ms,
        }


class SerialFlowStats(msgspec.Struct):
    """Serial flow control statistics."""

    commands_sent: int = 0
    commands_acked: int = 0
    retries: int = 0
    failures: int = 0
    last_event_unix: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return msgspec.structs.asdict(self)


class SupervisorStats(msgspec.Struct):
    """Task supervisor statistics."""

    restarts: int = 0
    last_failure_unix: float = 0.0
    last_exception: str | None = None
    backoff_seconds: float = 0.0
    fatal: bool = False

    def as_dict(self) -> dict[str, Any]:
        return msgspec.structs.asdict(self)


def _collect_system_metrics() -> dict[str, Any]:
    """Collect system-level metrics using psutil.

    Returns a dictionary with CPU, memory and load average metrics.
    Gracefully handles errors to avoid breaking metrics collection.
    """
    result: dict[str, Any] = {}
    try:
        # CPU metrics (non-blocking, percentage since last call)
        result["cpu_percent"] = psutil.cpu_percent(interval=None)
        result["cpu_count"] = psutil.cpu_count() or 1
    except (OSError, AttributeError):
        result["cpu_percent"] = None
        result["cpu_count"] = None

    try:
        # Memory metrics
        mem = psutil.virtual_memory()
        result["memory_total_bytes"] = mem.total
        result["memory_available_bytes"] = mem.available
        result["memory_percent"] = mem.percent
    except (OSError, AttributeError):
        result["memory_total_bytes"] = None
        result["memory_available_bytes"] = None
        result["memory_percent"] = None

    try:
        # Load average (1, 5, 15 minutes) - Unix only
        load = psutil.getloadavg()
        result["load_avg_1m"] = load[0]
        result["load_avg_5m"] = load[1]
        result["load_avg_15m"] = load[2]
    except (OSError, AttributeError):
        result["load_avg_1m"] = None
        result["load_avg_5m"] = None
        result["load_avg_15m"] = None

    return result


class RuntimeState(msgspec.Struct):
    """Aggregated mutable state shared across the daemon layers."""

    serial_writer: asyncio.BaseTransport | None = None
    serial_link_connected: bool = False
    mqtt_publish_queue: asyncio.Queue[QueuedPublish] = msgspec.field(default_factory=_mqtt_publish_queue_factory)
    mqtt_queue_limit: int = DEFAULT_MQTT_QUEUE_LIMIT
    mqtt_dropped_messages: int = 0
    mqtt_drop_counts: dict[str, int] = msgspec.field(default_factory=_mqtt_drop_counts_factory)
    mqtt_spool: MQTTPublishSpool | None = None
    mqtt_spooled_messages: int = 0
    mqtt_spooled_replayed: int = 0
    mqtt_spool_errors: int = 0
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
    _last_spool_snapshot: SpoolSnapshot = msgspec.field(default_factory=_last_spool_snapshot_factory)
    datastore: dict[str, str] = msgspec.field(default_factory=_datastore_factory)
    mailbox_queue: BoundedByteDeque = msgspec.field(default_factory=_bounded_byte_deque_factory)
    mcu_is_paused: bool = False
    serial_tx_allowed: asyncio.Event = msgspec.field(default_factory=_serial_tx_allowed_factory)
    console_to_mcu_queue: BoundedByteDeque = msgspec.field(default_factory=_bounded_byte_deque_factory)
    console_queue_limit_bytes: int = DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES
    console_queue_bytes: int = 0
    console_dropped_chunks: int = 0
    console_truncated_chunks: int = 0
    console_truncated_bytes: int = 0
    console_dropped_bytes: int = 0
    running_processes: dict[int, ManagedProcess] = msgspec.field(default_factory=_running_processes_factory)
    process_lock: asyncio.Lock = msgspec.field(default_factory=_asyncio_lock_factory)
    next_pid: int = 1
    allowed_policy: AllowedCommandPolicy = msgspec.field(default_factory=_policy_factory)
    topic_authorization: TopicAuthorization = msgspec.field(default_factory=_topic_authorization_factory)
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
    watchdog_beats: int = 0
    last_watchdog_beat: float = 0.0
    pending_digital_reads: collections.deque[PendingPinRequest] = msgspec.field(
        default_factory=_pending_pin_reads_factory,
    )
    pending_analog_reads: collections.deque[PendingPinRequest] = msgspec.field(
        default_factory=_pending_pin_reads_factory,
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
    mailbox_incoming_queue: BoundedByteDeque = msgspec.field(default_factory=_bounded_byte_deque_factory)
    mailbox_incoming_queue_bytes: int = 0
    mailbox_incoming_dropped_messages: int = 0
    mailbox_incoming_truncated_messages: int = 0
    mailbox_incoming_truncated_bytes: int = 0
    mailbox_incoming_dropped_bytes: int = 0
    mailbox_incoming_overflow_events: int = 0
    mcu_version: tuple[int, int] | None = None
    mcu_capabilities: McuCapabilities | None = None
    link_handshake_nonce: bytes | None = None
    link_is_synchronized: bool = False
    link_sync_event: asyncio.Event = msgspec.field(default_factory=_asyncio_event_factory)
    link_expected_tag: bytes | None = None
    link_nonce_length: int = 0
    # [MIL-SPEC] Anti-replay nonce counters
    link_nonce_counter: int = 0  # Counter for generating outbound nonces
    link_last_nonce_counter: int = 0  # Last accepted inbound nonce counter
    handshake_attempts: int = 0
    handshake_successes: int = 0
    handshake_failures: int = 0
    handshake_failure_streak: int = 0
    handshake_backoff_until: float = 0.0
    handshake_rate_limit_until: float = 0.0
    last_handshake_error: str | None = None
    last_handshake_unix: float = 0.0
    handshake_last_duration: float = 0.0
    handshake_fatal_count: int = 0
    handshake_fatal_reason: str | None = None
    handshake_fatal_detail: str | None = None
    handshake_fatal_unix: float = 0.0
    _handshake_last_started: float = 0.0
    serial_flow_stats: SerialFlowStats = msgspec.field(default_factory=_serial_flow_stats_factory)
    serial_throughput_stats: SerialThroughputStats = msgspec.field(default_factory=_serial_throughput_stats_factory)
    serial_latency_stats: SerialLatencyStats = msgspec.field(default_factory=_serial_latency_stats_factory)
    serial_pipeline_inflight: dict[str, Any] | None = None
    serial_pipeline_last: dict[str, Any] | None = None
    process_output_limit: int = DEFAULT_PROCESS_MAX_OUTPUT_BYTES
    process_max_concurrent: int = DEFAULT_PROCESS_MAX_CONCURRENT
    serial_decode_errors: int = 0
    serial_crc_errors: int = 0
    unknown_command_ids: int = 0
    config_source: str = "uci"
    serial_ack_timeout_ms: int = int(DEFAULT_SERIAL_RETRY_TIMEOUT * 1000)
    serial_response_timeout_ms: int = int(DEFAULT_SERIAL_RESPONSE_TIMEOUT * 1000)
    serial_retry_limit: int = DEFAULT_RETRY_LIMIT
    mcu_status_counters: dict[str, int] = msgspec.field(default_factory=_mcu_status_counters_factory)
    supervisor_stats: dict[str, SupervisorStats] = msgspec.field(default_factory=_supervisor_stats_factory)

    def configure(self, config: RuntimeConfig) -> None:
        if config.allowed_policy is not None:
            self.allowed_policy = config.allowed_policy
        self.process_timeout = config.process_timeout
        self.file_system_root = config.file_system_root
        self.allow_non_tmp_paths = config.allow_non_tmp_paths
        self.file_write_max_bytes = config.file_write_max_bytes
        self.file_storage_quota_bytes = config.file_storage_quota_bytes
        self.file_storage_bytes_used = 0
        self.file_write_limit_rejections = 0
        self.file_storage_limit_rejections = 0
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
        self.console_to_mcu_queue = BoundedByteDeque(
            max_items=None,
            max_bytes=self.console_queue_limit_bytes,
        )
        self.mailbox_queue = BoundedByteDeque(
            max_items=self.mailbox_queue_limit,
            max_bytes=self.mailbox_queue_bytes_limit,
        )
        self.mailbox_incoming_queue = BoundedByteDeque(
            max_items=self.mailbox_queue_limit,
            max_bytes=self.mailbox_queue_bytes_limit,
        )

    @property
    def allowed_commands(self) -> tuple[str, ...]:
        return self.allowed_policy.as_tuple()

    def enqueue_console_chunk(self, chunk: bytes, logger: logging.Logger) -> None:
        if not chunk:
            return

        self._sync_console_queue_limits()
        evt = self.console_to_mcu_queue.append(chunk)
        if evt.truncated_bytes:
            logger.warning(
                "Console chunk truncated by %d byte(s) to respect limit.",
                evt.truncated_bytes,
            )
            self.console_truncated_chunks += 1
            self.console_truncated_bytes += evt.truncated_bytes
        if evt.dropped_chunks:
            logger.warning(
                ("Dropping oldest console chunk(s): %d item(s), %d " "bytes to respect limit."),
                evt.dropped_chunks,
                evt.dropped_bytes,
            )
            self.console_dropped_chunks += evt.dropped_chunks
            self.console_dropped_bytes += evt.dropped_bytes
        if not evt.accepted:
            logger.error(
                "Console queue overflow; rejected chunk of %d bytes.",
                len(chunk),
            )
            self.console_dropped_chunks += 1
            self.console_dropped_bytes += len(chunk)
        else:
            self.console_queue_bytes = self.console_to_mcu_queue.bytes_used

    def pop_console_chunk(self) -> bytes:
        self._sync_console_queue_limits()
        chunk = self.console_to_mcu_queue.popleft()
        self.console_queue_bytes = self.console_to_mcu_queue.bytes_used
        return chunk

    def requeue_console_chunk_front(self, chunk: bytes) -> None:
        if not chunk:
            return

        chunk_len = len(chunk)
        # The caller should ensure the chunk fits within the configured limit.
        if chunk_len > self.console_queue_limit_bytes:
            data = bytes(chunk[-self.console_queue_limit_bytes :])
            chunk_len = len(data)
        else:
            data = bytes(chunk)

        self._sync_console_queue_limits()
        evt = self.console_to_mcu_queue.appendleft(data)
        if evt.truncated_bytes:
            self.console_truncated_chunks += 1
            self.console_truncated_bytes += evt.truncated_bytes
        if evt.dropped_chunks:
            self.console_dropped_chunks += evt.dropped_chunks
            self.console_dropped_bytes += evt.dropped_bytes
        if evt.accepted:
            self.console_queue_bytes = self.console_to_mcu_queue.bytes_used

    def enqueue_mailbox_message(self, payload: bytes, logger: logging.Logger) -> bool:
        return self._enqueue_mailbox(
            payload, logger, self.mailbox_queue, "outgoing",
        )

    def pop_mailbox_message(self) -> bytes | None:
        return self._pop_mailbox(self.mailbox_queue)

    def requeue_mailbox_message_front(self, payload: bytes) -> None:
        self._sync_mailbox_limits(self.mailbox_queue)
        evt = self.mailbox_queue.appendleft(payload)
        self._update_mailbox_bytes()
        if evt.dropped_chunks:
            self.mailbox_dropped_messages += evt.dropped_chunks
            self.mailbox_dropped_bytes += evt.dropped_bytes

    def enqueue_mailbox_incoming(self, payload: bytes, logger: logging.Logger) -> bool:
        return self._enqueue_mailbox(
            payload, logger, self.mailbox_incoming_queue, "incoming",
        )

    def pop_mailbox_incoming(self) -> bytes | None:
        return self._pop_mailbox(self.mailbox_incoming_queue)

    def _sync_console_queue_limits(self) -> None:
        self.console_to_mcu_queue.update_limits(
            max_items=None,
            max_bytes=self.console_queue_limit_bytes,
        )
        self.console_queue_bytes = self.console_to_mcu_queue.bytes_used

    def _sync_mailbox_limits(self, queue: BoundedByteDeque) -> None:
        queue.update_limits(
            max_items=self.mailbox_queue_limit,
            max_bytes=self.mailbox_queue_bytes_limit,
        )

    def _update_mailbox_bytes(self) -> None:
        self.mailbox_queue_bytes = self.mailbox_queue.bytes_used
        self.mailbox_incoming_queue_bytes = self.mailbox_incoming_queue.bytes_used

    def _enqueue_mailbox(
        self,
        payload: bytes,
        logger: logging.Logger,
        queue: BoundedByteDeque,
        direction: str,
    ) -> bool:
        """Unified mailbox enqueue for both outgoing and incoming queues."""
        self._sync_mailbox_limits(queue)
        evt = queue.append(payload)
        self._update_mailbox_bytes()
        is_incoming = direction == "incoming"
        if evt.truncated_bytes:
            logger.warning(
                "Mailbox %s message truncated by %d bytes to respect limit.",
                direction, evt.truncated_bytes,
            )
            if is_incoming:
                self.mailbox_incoming_truncated_messages += 1
                self.mailbox_incoming_truncated_bytes += evt.truncated_bytes
            else:
                self.mailbox_truncated_messages += 1
                self.mailbox_truncated_bytes += evt.truncated_bytes
        if evt.dropped_chunks:
            logger.warning(
                "Dropping oldest mailbox %s message(s): %d item(s), %d bytes.",
                direction, evt.dropped_chunks, evt.dropped_bytes,
            )
            if is_incoming:
                self.mailbox_incoming_dropped_messages += evt.dropped_chunks
                self.mailbox_incoming_dropped_bytes += evt.dropped_bytes
            else:
                self.mailbox_dropped_messages += evt.dropped_chunks
                self.mailbox_dropped_bytes += evt.dropped_bytes
        if not evt.accepted:
            logger.error(
                "Mailbox %s queue overflow; rejecting message (%d bytes).",
                direction, len(payload),
            )
            if is_incoming:
                self.mailbox_incoming_dropped_messages += 1
                self.mailbox_incoming_dropped_bytes += len(payload)
                self.mailbox_incoming_overflow_events += 1
            else:
                self.mailbox_dropped_messages += 1
                self.mailbox_dropped_bytes += len(payload)
                self.mailbox_outgoing_overflow_events += 1
            return False
        return True

    def _pop_mailbox(self, queue: BoundedByteDeque) -> bytes | None:
        """Unified mailbox pop for both outgoing and incoming queues."""
        self._sync_mailbox_limits(queue)
        if not queue:
            return None
        message = queue.popleft()
        self._update_mailbox_bytes()
        return message

    def record_mqtt_drop(self, topic: str) -> None:
        self.mqtt_dropped_messages += 1
        self.mqtt_drop_counts[topic] = self.mqtt_drop_counts.get(topic, 0) + 1

    def record_watchdog_beat(self, timestamp: float | None = None) -> None:
        self.watchdog_beats += 1
        self.last_watchdog_beat = timestamp if timestamp is not None else time.monotonic()

    def record_handshake_attempt(self) -> None:
        self.handshake_attempts += 1
        self.last_handshake_unix = time.time()
        self._handshake_last_started = time.monotonic()

    def record_handshake_success(self) -> None:
        self.handshake_successes += 1
        self.last_handshake_error = None
        self.handshake_failure_streak = 0
        self.handshake_backoff_until = 0.0
        self.handshake_last_duration = self._handshake_duration_since_start()
        self._handshake_last_started = 0.0

    def record_handshake_failure(self, reason: str) -> None:
        self.handshake_failures += 1
        if self.last_handshake_error == reason:
            self.handshake_failure_streak += 1
        else:
            self.handshake_failure_streak = 1
        self.last_handshake_error = reason
        self.last_handshake_unix = time.time()
        self.handshake_last_duration = self._handshake_duration_since_start()
        self._handshake_last_started = 0.0

    def record_handshake_fatal(self, reason: str, detail: str | None = None) -> None:
        self.handshake_fatal_count += 1
        self.handshake_fatal_reason = reason
        self.handshake_fatal_detail = detail
        self.handshake_fatal_unix = time.time()

    def record_serial_flow_event(self, event: str) -> None:
        """Record a serial flow control event for metrics.

        This method updates internal counters for serial link health monitoring.
        Events are recorded for telemetry/metrics purposes and exposed via the
        Prometheus exporter and status snapshots.

        Valid events: 'sent', 'ack', 'retry', 'failure'.

        Note:
            This is also exercised by unit tests to verify counter logic,
            but it is part of the public API for metrics collection.
        """
        stats = self.serial_flow_stats
        if event == "sent":
            stats.commands_sent += 1
        elif event == "ack":
            stats.commands_acked += 1
        elif event == "retry":
            stats.retries += 1
        elif event == "failure":
            stats.failures += 1
        else:
            return
        stats.last_event_unix = time.time()

    def record_serial_pipeline_event(self, event: Mapping[str, Any]) -> None:
        """Record a serial pipeline state transition for diagnostics.

        This method tracks the lifecycle of serial commands (start → ack → success/failure)
        and is used by the BridgeService to maintain visibility into inflight operations.
        The recorded data is exposed via status snapshots and metrics.

        Expected event keys:
            - event: 'start', 'ack', 'success', 'failure', 'abandoned'
            - command_id: The RPC command ID (int)
            - attempt: Retry attempt number (int, default 1)
            - timestamp: Unix timestamp (float, defaults to now)
            - ack_received: Whether ACK was received (bool)
            - status: Status code if completed (int or None)

        Note:
            Exercised by tests to verify state machine logic; part of the
            observability API for serial link debugging.
        """
        name = str(event.get("event", ""))
        command_id = int(event.get("command_id", 0))
        attempt = int(event.get("attempt", 1) or 1)
        timestamp = float(event.get("timestamp") or time.time())
        acked = bool(event.get("ack_received"))
        status_code = event.get("status")

        if name == "start":
            payload = _serial_pipeline_base_payload(command_id, attempt)
            payload.update(
                started_unix=timestamp,
                acknowledged=False,
                last_event="start",
                last_event_unix=timestamp,
            )
            self.serial_pipeline_inflight = payload
            return

        inflight = self.serial_pipeline_inflight
        if name == "ack" and inflight is not None:
            inflight["acknowledged"] = True
            inflight["ack_unix"] = timestamp
            inflight["last_event"] = "ack"
            inflight["last_event_unix"] = timestamp
            return

        if name in {"success", "failure", "abandoned"}:
            payload = _serial_pipeline_base_payload(command_id, attempt)
            payload.update(
                event=name,
                completed_unix=timestamp,
                status_code=status_code,
                status_name=_status_label(status_code),
                acknowledged=acked or bool(inflight and inflight.get("acknowledged")),
            )
            if inflight is not None:
                started = inflight.get("started_unix")
                if isinstance(started, (int, float)) and started >= 0:
                    payload["started_unix"] = float(started)
                    payload["duration"] = max(0.0, timestamp - float(started))
                payload.setdefault("ack_unix", inflight.get("ack_unix"))
                payload["acknowledged"] = inflight.get(
                    "acknowledged",
                    payload["acknowledged"],
                )
            self.serial_pipeline_last = payload
            self.serial_pipeline_inflight = None

    def record_serial_decode_error(self) -> None:
        self.serial_decode_errors += 1

    def record_serial_crc_error(self) -> None:
        self.serial_crc_errors += 1

    def record_unknown_command_id(self, command_id: int) -> None:
        """Record receipt of an unrecognized command/status ID.

        This metric helps detect protocol version drift between MCU and daemon.
        """
        self.unknown_command_ids += 1
        logger.warning(
            "Unknown command/status ID received: 0x%02X (total: %d)",
            command_id,
            self.unknown_command_ids,
        )

    def record_serial_tx(self, nbytes: int) -> None:
        """Record bytes transmitted on serial link for throughput metrics."""
        self.serial_throughput_stats.record_tx(nbytes)

    def record_serial_rx(self, nbytes: int) -> None:
        """Record bytes received on serial link for throughput metrics."""
        self.serial_throughput_stats.record_rx(nbytes)

    def record_rpc_latency_ms(self, latency_ms: float) -> None:
        """Record RPC command round-trip latency for histogram metrics.

        Args:
            latency_ms: Command round-trip time in milliseconds.
        """
        self.serial_latency_stats.record(latency_ms)

    def record_mcu_status(self, status: Status) -> None:
        key = status.name
        self.mcu_status_counters[key] = self.mcu_status_counters.get(key, 0) + 1

    def record_supervisor_failure(
        self,
        name: str,
        *,
        backoff: float,
        exc: BaseException,
        fatal: bool = False,
    ) -> None:
        stats = self.supervisor_stats.get(name)
        if stats is None:
            stats = SupervisorStats()
            self.supervisor_stats[name] = stats
        stats.restarts += 1
        stats.last_failure_unix = time.time()
        stats.last_exception = f"{exc.__class__.__name__}: {exc}"
        stats.backoff_seconds = backoff
        stats.fatal = fatal

    def mark_supervisor_healthy(self, name: str) -> None:
        stats = self.supervisor_stats.get(name)
        if stats is None:
            return
        stats.backoff_seconds = 0.0
        stats.fatal = False

    def configure_spool(self, directory: str, limit: int) -> None:
        self.mqtt_spool_dir = directory
        self.mqtt_spool_limit = max(0, limit)

    def initialize_spool(self) -> None:
        if not self.mqtt_spool_dir or self.mqtt_spool_limit <= 0:
            self._disable_mqtt_spool("disabled", schedule_retry=False)
            return
        try:
            self.mqtt_spool = MQTTPublishSpool(
                self.mqtt_spool_dir,
                self.mqtt_spool_limit,
                on_fallback=self._on_spool_fallback,
            )
            self.mqtt_spool_degraded = False
            self.mqtt_spool_failure_reason = None
            self.mqtt_spool_retry_attempts = 0
            self.mqtt_spool_backoff_until = 0.0
            self.mqtt_spool_last_error = None
        except (OSError, MQTTSpoolError) as exc:
            self._handle_mqtt_spool_failure("initialization_failed", exc=exc)

    async def ensure_spool(self) -> bool:
        if self.mqtt_spool is not None:
            return True
        if not self.mqtt_spool_dir or self.mqtt_spool_limit <= 0:
            return False
        if self._spool_backoff_remaining() > 0:
            return False
        try:
            spool = await asyncio.to_thread(
                MQTTPublishSpool,
                self.mqtt_spool_dir,
                self.mqtt_spool_limit,
                on_fallback=self._on_spool_fallback,
            )
        except (OSError, MQTTSpoolError) as exc:
            self._handle_mqtt_spool_failure("reactivation_failed", exc=exc)
            return False
        self.mqtt_spool = spool
        self.mqtt_spool_degraded = False
        self.mqtt_spool_failure_reason = None
        self.mqtt_spool_retry_attempts = 0
        self.mqtt_spool_backoff_until = 0.0
        self.mqtt_spool_last_error = None
        self.mqtt_spool_recoveries += 1
        return True

    def _spool_backoff_remaining(self) -> float:
        if self.mqtt_spool_backoff_until <= 0:
            return 0.0
        return max(0.0, self.mqtt_spool_backoff_until - time.monotonic())

    def _schedule_spool_retry(self) -> None:
        self.mqtt_spool_retry_attempts = min(
            self.mqtt_spool_retry_attempts + 1,
            6,
        )
        # Exponential backoff calculation: min * (2 ** (attempt - 1))
        delay = min(SPOOL_BACKOFF_MIN_SECONDS * (2 ** (self.mqtt_spool_retry_attempts - 1)), SPOOL_BACKOFF_MAX_SECONDS)
        self.mqtt_spool_backoff_until = time.monotonic() + delay

    def _disable_mqtt_spool(
        self,
        reason: str,
        *,
        schedule_retry: bool = True,
    ) -> None:
        spool = self.mqtt_spool
        if spool is not None:
            try:
                spool.close()
            except (OSError, MQTTSpoolError, RuntimeError):
                logger.debug(
                    "Failed to close MQTT spool during disable.",
                    exc_info=True,
                )
        self.mqtt_spool = None
        self.mqtt_spool_degraded = True
        self.mqtt_spool_failure_reason = reason
        if schedule_retry:
            self._schedule_spool_retry()

    def _handle_mqtt_spool_failure(
        self,
        reason: str,
        *,
        exc: BaseException | None = None,
    ) -> None:
        detail = reason if exc is None else f"{reason}:{exc}"
        logger.warning("MQTT spool failure (%s); disabling durable spool.", detail)
        self.mqtt_spool_errors += 1
        self.mqtt_spool_last_error = detail
        self._disable_mqtt_spool(reason)

    def _on_spool_fallback(self, reason: str) -> None:
        self.mqtt_spool_degraded = True
        self.mqtt_spool_failure_reason = reason
        self.mqtt_spool_last_error = reason
        self.mqtt_spool_errors += 1

    async def stash_mqtt_message(self, message: QueuedPublish) -> bool:
        if self.mqtt_spool is None:
            await self.ensure_spool()
        spool = self.mqtt_spool
        if spool is None:
            return False
        try:
            await asyncio.to_thread(spool.append, message)
            self.mqtt_spooled_messages += 1
            return True
        except (OSError, msgspec.MsgspecError, MQTTSpoolError) as exc:
            reason = "append_failed"
            if isinstance(exc, MQTTSpoolError):
                reason = exc.reason
            self._handle_mqtt_spool_failure(reason, exc=exc)
            return False

    async def flush_mqtt_spool(self) -> None:
        if self.mqtt_spool is None:
            await self.ensure_spool()
        spool = self.mqtt_spool
        if spool is None:
            return
        while True:
            if self.mqtt_publish_queue.qsize() >= self.mqtt_queue_limit:
                break
            try:
                message = await asyncio.to_thread(spool.pop_next)
            except (OSError, msgspec.MsgspecError, MQTTSpoolError) as exc:
                reason = "pop_failed"
                if isinstance(exc, MQTTSpoolError):
                    reason = exc.reason
                self._handle_mqtt_spool_failure(reason, exc=exc)
                break
            if message is None:
                break
            enriched = msgspec.structs.replace(
                message,
                user_properties=message.user_properties + (("bridge-spooled", "1"),),
            )
            try:
                self.mqtt_publish_queue.put_nowait(enriched)
                self.mqtt_spooled_replayed += 1
            except asyncio.QueueFull:
                try:
                    await asyncio.to_thread(spool.requeue, message)
                except (OSError, msgspec.MsgspecError, MQTTSpoolError) as exc:
                    reason = "requeue_failed"
                    if isinstance(exc, MQTTSpoolError):
                        reason = exc.reason
                    self._handle_mqtt_spool_failure(reason, exc=exc)
                    break
                break

    def _current_spool_snapshot(self) -> dict[str, Any]:
        spool = self.mqtt_spool
        if spool is None:
            if self._last_spool_snapshot:
                return dict(self._last_spool_snapshot)
            return {
                "pending": 0,
                "limit": self.mqtt_spool_limit,
                "dropped_due_to_limit": self.mqtt_spool_dropped_limit,
                "trim_events": self.mqtt_spool_trim_events,
                "last_trim_unix": self.mqtt_spool_last_trim_unix,
                "corrupt_dropped": self.mqtt_spool_corrupt_dropped,
            }
        snapshot = spool.snapshot()
        self._last_spool_snapshot = dict(snapshot)
        self._apply_spool_observation(snapshot)
        return snapshot

    def _apply_spool_observation(self, snapshot: Mapping[str, Any]) -> None:
        self.mqtt_spool_dropped_limit = _coerce_snapshot_int(
            snapshot,
            "dropped_due_to_limit",
            self.mqtt_spool_dropped_limit,
        )
        self.mqtt_spool_trim_events = _coerce_snapshot_int(
            snapshot,
            "trim_events",
            self.mqtt_spool_trim_events,
        )
        corrupt = snapshot.get("corrupt_dropped")
        if isinstance(corrupt, (int, float)):
            self.mqtt_spool_corrupt_dropped = int(corrupt)
        last_trim = snapshot.get("last_trim_unix")
        if isinstance(last_trim, (int, float)):
            self.mqtt_spool_last_trim_unix = float(last_trim)

    def build_metrics_snapshot(self) -> dict[str, Any]:
        spool_snapshot = self._current_spool_snapshot()
        snapshot: dict[str, Any] = {
            "serial": self.serial_flow_stats.as_dict(),
            # [EXTENDED METRICS] Throughput and latency
            "serial_throughput": self.serial_throughput_stats.as_dict(),
            "serial_latency": self.serial_latency_stats.as_dict(),
            # Queue depths for real-time monitoring
            "queue_depths": {
                "mqtt": self.mqtt_publish_queue.qsize(),
                "console": len(self.console_to_mcu_queue),
                "mailbox_outgoing": len(self.mailbox_queue),
                "mailbox_incoming": len(self.mailbox_incoming_queue),
                "pending_digital_reads": len(self.pending_digital_reads),
                "pending_analog_reads": len(self.pending_analog_reads),
                "running_processes": len(self.running_processes),
            },
            "mqtt_queue_size": self.mqtt_publish_queue.qsize(),
            "mqtt_queue_limit": self.mqtt_queue_limit,
            "mqtt_dropped": self.mqtt_dropped_messages,
            "mqtt_drop_counts": dict(self.mqtt_drop_counts),
            "mqtt_spooled": self.mqtt_spooled_messages,
            "mqtt_spool_replayed": self.mqtt_spooled_replayed,
            "mqtt_spool_errors": self.mqtt_spool_errors,
            "mqtt_spool_degraded": self.mqtt_spool_degraded,
            "mqtt_spool_failure_reason": self.mqtt_spool_failure_reason,
            "mqtt_spool_retry_attempts": self.mqtt_spool_retry_attempts,
            "mqtt_spool_backoff_until": self.mqtt_spool_backoff_until,
            "mqtt_spool_last_error": self.mqtt_spool_last_error,
            "mqtt_spool_recoveries": self.mqtt_spool_recoveries,
            "file_storage_root": self.file_system_root,
            "file_storage_bytes_used": self.file_storage_bytes_used,
            "file_storage_quota_bytes": self.file_storage_quota_bytes,
            "file_write_max_bytes": self.file_write_max_bytes,
            "file_write_limit_rejections": self.file_write_limit_rejections,
            "file_storage_limit_rejections": (self.file_storage_limit_rejections),
            "handshake_attempts": self.handshake_attempts,
            "handshake_successes": self.handshake_successes,
            "handshake_failures": self.handshake_failures,
            "handshake_failure_streak": self.handshake_failure_streak,
            "handshake_backoff_until": self.handshake_backoff_until,
            "handshake_last_error": self.last_handshake_error,
            "handshake_last_unix": self.last_handshake_unix,
            "handshake_last_duration": self.handshake_last_duration,
            "handshake_fatal_count": self.handshake_fatal_count,
            "handshake_fatal_reason": self.handshake_fatal_reason,
            "handshake_fatal_detail": self.handshake_fatal_detail,
            "handshake_fatal_unix": self.handshake_fatal_unix,
            "link_synchronised": self.link_is_synchronized,
            "serial_decode_errors": self.serial_decode_errors,
            "serial_crc_errors": self.serial_crc_errors,
            "unknown_command_ids": self.unknown_command_ids,
            "config_source": self.config_source,
            "mcu_status": dict(self.mcu_status_counters),
            "watchdog_enabled": self.watchdog_enabled,
            "watchdog_interval": self.watchdog_interval,
            "watchdog_beats": self.watchdog_beats,
            "watchdog_last_unix": self.last_watchdog_beat,
            "supervisors": {name: stats.as_dict() for name, stats in self.supervisor_stats.items()},
            "bridge": self.build_bridge_snapshot(),
        }
        snapshot.update(
            mailbox_outgoing_len=len(self.mailbox_queue),
            mailbox_outgoing_bytes=self.mailbox_queue_bytes,
            mailbox_outgoing_dropped_messages=self.mailbox_dropped_messages,
            mailbox_outgoing_dropped_bytes=self.mailbox_dropped_bytes,
            mailbox_outgoing_truncated_messages=(self.mailbox_truncated_messages),
            mailbox_outgoing_truncated_bytes=(self.mailbox_truncated_bytes),
            mailbox_outgoing_overflow_events=(self.mailbox_outgoing_overflow_events),
            mailbox_incoming_len=len(self.mailbox_incoming_queue),
            mailbox_incoming_bytes=self.mailbox_incoming_queue_bytes,
            mailbox_incoming_dropped_messages=(self.mailbox_incoming_dropped_messages),
            mailbox_incoming_dropped_bytes=(self.mailbox_incoming_dropped_bytes),
            mailbox_incoming_truncated_messages=(self.mailbox_incoming_truncated_messages),
            mailbox_incoming_truncated_bytes=(self.mailbox_incoming_truncated_bytes),
            mailbox_incoming_overflow_events=(self.mailbox_incoming_overflow_events),
            mqtt_spool_dropped_limit=self.mqtt_spool_dropped_limit,
            mqtt_spool_trim_events=self.mqtt_spool_trim_events,
            mqtt_spool_last_trim_unix=self.mqtt_spool_last_trim_unix,
            mqtt_spool_corrupt_dropped=self.mqtt_spool_corrupt_dropped,
            mqtt_spool_degraded=self.mqtt_spool_degraded,
            mqtt_spool_failure_reason=self.mqtt_spool_failure_reason,
            mqtt_spool_retry_attempts=self.mqtt_spool_retry_attempts,
            mqtt_spool_backoff_until=self.mqtt_spool_backoff_until,
            mqtt_spool_last_error=self.mqtt_spool_last_error,
            mqtt_spool_recoveries=self.mqtt_spool_recoveries,
        )
        snapshot.update({f"spool_{k}": v for k, v in spool_snapshot.items()})
        # [EXTENDED METRICS] System-level metrics via psutil
        snapshot["system"] = _collect_system_metrics()
        return snapshot

    def build_handshake_snapshot(self) -> HandshakeSnapshot:
        return HandshakeSnapshot(
            synchronised=self.link_is_synchronized,
            attempts=self.handshake_attempts,
            successes=self.handshake_successes,
            failures=self.handshake_failures,
            failure_streak=self.handshake_failure_streak,
            last_error=self.last_handshake_error,
            last_unix=self.last_handshake_unix,
            last_duration=self.handshake_last_duration,
            backoff_until=self.handshake_backoff_until,
            rate_limit_until=self.handshake_rate_limit_until,
            fatal_count=self.handshake_fatal_count,
            fatal_reason=self.handshake_fatal_reason,
            fatal_detail=self.handshake_fatal_detail,
            fatal_unix=self.handshake_fatal_unix,
            pending_nonce=bool(self.link_handshake_nonce),
            nonce_length=self.link_nonce_length,
        )

    def build_serial_pipeline_snapshot(self) -> SerialPipelineSnapshot:
        inflight = self.serial_pipeline_inflight.copy() if self.serial_pipeline_inflight else None
        last = self.serial_pipeline_last.copy() if self.serial_pipeline_last else None
        return SerialPipelineSnapshot(
            inflight=inflight,
            last_completion=last,
        )

    def build_bridge_snapshot(self) -> BridgeSnapshot:
        mcu_version = None
        if self.mcu_version is not None:
            mcu_version = McuVersion(
                major=self.mcu_version[0],
                minor=self.mcu_version[1],
            )

        caps = self.mcu_capabilities.as_dict() if self.mcu_capabilities else None

        return BridgeSnapshot(
            serial_link=SerialLinkSnapshot(
                connected=self.serial_link_connected,
                writer_attached=self.serial_writer is not None,
                synchronised=self.link_is_synchronized,
            ),
            handshake=self.build_handshake_snapshot(),
            serial_pipeline=self.build_serial_pipeline_snapshot(),
            serial_flow=self.serial_flow_stats.as_dict(),
            mcu_version=mcu_version,
            capabilities=caps,
        )

    def _handshake_duration_since_start(self) -> float:
        if self._handshake_last_started <= 0.0:
            return 0.0
        return max(0.0, time.monotonic() - self._handshake_last_started)


def create_runtime_state(config: RuntimeConfig | dict[str, Any]) -> RuntimeState:
    from ..config.settings import RuntimeConfig as RC
    if isinstance(config, dict):
        config = msgspec.convert(config, RC)

    state = RuntimeState()
    state.mqtt_publish_queue = asyncio.Queue(config.mqtt_queue_limit)
    state.mqtt_queue_limit = config.mqtt_queue_limit
    state.configure(config)
    state.configure_spool(
        config.mqtt_spool_dir,
        config.mqtt_queue_limit * 4,
    )
    state.initialize_spool()
    if state.mqtt_spool is None:
        state.mqtt_spool_degraded = True
        if not state.mqtt_spool_failure_reason:
            state.mqtt_spool_failure_reason = "initialization_failed"
    return state

"""Runtime state container for the MCU Bridge daemon."""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import time
from asyncio.subprocess import Process
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, cast

import msgspec
import psutil
from aiomqtt.message import Message
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
    HandshakeSnapshot,
    McuCapabilities,
    McuVersion,
    SerialFlowStats,
    SerialLatencyStats,
    SerialLinkSnapshot,
    SerialPipelineSnapshot,
    SerialThroughputStats,
    SupervisorStats,
)
from .metrics import DaemonMetrics
from .queues import BoundedByteDeque

logger = logging.getLogger("mcubridge.state")

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
    """Resolve status code to human-readable label."""
    return "unknown" if code is None else next((s.name for s in Status if s.value == code), f"0x{code:02X}")


class PendingPinRequest(msgspec.Struct):
    """Pending pin read request."""

    pin: int
    reply_context: Message | None = None


@dataclass
class ManagedProcess:
    """Managed subprocess with output buffers."""

    pid: int
    command: str = ""
    handle: Process | None = None
    stdout_buffer: bytearray = field(default_factory=bytearray)
    stderr_buffer: bytearray = field(default_factory=bytearray)
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
        truncated_stdout = _append_with_limit(self.stdout_buffer, stdout_chunk, limit)
        truncated_stderr = _append_with_limit(self.stderr_buffer, stderr_chunk, limit)
        return truncated_stdout, truncated_stderr

    def pop_payload(self, budget: int) -> tuple[bytes, bytes, bool, bool]:
        return _trim_process_buffers(self.stdout_buffer, self.stderr_buffer, budget)

    def is_drained(self) -> bool:
        return not self.stdout_buffer and not self.stderr_buffer


def _append_with_limit(buffer: bytearray, chunk: bytes, limit: int) -> bool:
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

    truncated_out = bool(stdout_buffer)
    truncated_err = bool(stderr_buffer)
    return stdout_chunk, stderr_chunk, truncated_out, truncated_err


def collect_system_metrics() -> dict[str, Any]:
    """Collect system-level metrics using psutil."""
    result: dict[str, Any] = {}
    try:
        proc = psutil.Process()
        with proc.oneshot():
            # CPU metrics (non-blocking, percentage since last call)
            result["cpu_percent"] = psutil.cpu_percent(interval=None)
            result["cpu_count"] = psutil.cpu_count() or 1

            # Memory metrics
            mem = psutil.virtual_memory()
            result["memory_total_bytes"] = mem.total
            result["memory_available_bytes"] = mem.available
            result["memory_percent"] = mem.percent

            # Load average (1, 5, 15 minutes) - Unix only
            load = psutil.getloadavg()
            result["load_avg_1m"] = load[0]
            result["load_avg_5m"] = load[1]
            result["load_avg_15m"] = load[2]

            # Temperature metrics
            temps = psutil.sensors_temperatures()
            names = ("cpu_thermal", "coretemp", "soc_thermal")
            cpu_temp = next(
                (temps[n][0].current for n in names if n in temps and temps[n]),
                next((t[0].current for t in temps.values() if t), None) if temps else None,
            )
            result["temperature_celsius"] = cpu_temp
    except (OSError, AttributeError):
        _fill_missing_metrics(result)
    except Exception:
        _fill_missing_metrics(result)

    return result


def _fill_missing_metrics(result: dict[str, Any]) -> None:
    keys = (
        "cpu_percent",
        "cpu_count",
        "memory_total_bytes",
        "memory_available_bytes",
        "memory_percent",
        "load_avg_1m",
        "load_avg_5m",
        "load_avg_15m",
        "temperature_celsius",
    )
    for k in keys:
        result.setdefault(k, None)


class RuntimeState(msgspec.Struct):
    """Aggregated mutable state shared across the daemon layers."""

    metrics: DaemonMetrics = msgspec.field(default_factory=DaemonMetrics)
    serial_writer: asyncio.BaseTransport | None = None

    # [SIL-2] Lifecycle FSM (Single Source of Truth)
    _machine: Machine = msgspec.field(
        default_factory=lambda: Machine(
            states=["disconnected", "connected", "synchronized"],
            initial="disconnected",
            ignore_invalid_triggers=True,
        )
    )

    @property
    def is_connected(self) -> bool:
        return self._machine.state in {"connected", "synchronized"}

    @property
    def is_synchronized(self) -> bool:
        return self._machine.state == "synchronized"

    def mark_transport_connected(self) -> None:
        self._machine.set_state("connected")

    def mark_transport_disconnected(self) -> None:
        self._machine.set_state("disconnected")
        if self.link_sync_event:
            self.link_sync_event.clear()

    def mark_synchronized(self) -> None:
        self._machine.set_state("synchronized")
        if self.link_sync_event:
            self.link_sync_event.set()

    mqtt_publish_queue: asyncio.Queue[QueuedPublish] = msgspec.field(default_factory=lambda: asyncio.Queue())
    mqtt_queue_limit: int = DEFAULT_MQTT_QUEUE_LIMIT
    mqtt_drop_counts: dict[str, int] = msgspec.field(default_factory=lambda: {})
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
    _last_spool_snapshot: SpoolSnapshot = msgspec.field(default_factory=lambda: {})
    datastore: dict[str, str] = msgspec.field(default_factory=lambda: {})
    mailbox_queue: BoundedByteDeque = msgspec.field(default_factory=BoundedByteDeque)
    mcu_is_paused: bool = False
    serial_tx_allowed: asyncio.Event = msgspec.field(default_factory=asyncio.Event)
    console_to_mcu_queue: BoundedByteDeque = msgspec.field(default_factory=BoundedByteDeque)
    console_queue_limit_bytes: int = DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES
    console_queue_bytes: int = 0
    console_dropped_chunks: int = 0
    console_truncated_chunks: int = 0
    console_truncated_bytes: int = 0
    console_dropped_bytes: int = 0
    running_processes: dict[int, ManagedProcess] = msgspec.field(default_factory=lambda: {})
    process_lock: asyncio.Lock = msgspec.field(default_factory=asyncio.Lock)
    next_pid: int = 1
    allowed_policy: AllowedCommandPolicy = msgspec.field(default_factory=lambda: AllowedCommandPolicy.from_iterable(()))
    topic_authorization: TopicAuthorization = msgspec.field(default_factory=TopicAuthorization)
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
        default_factory=lambda: collections.deque(),
    )
    pending_analog_reads: collections.deque[PendingPinRequest] = msgspec.field(
        default_factory=lambda: collections.deque(),
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
    mailbox_incoming_queue: BoundedByteDeque = msgspec.field(default_factory=BoundedByteDeque)
    mailbox_incoming_queue_bytes: int = 0
    mailbox_incoming_dropped_messages: int = 0
    mailbox_incoming_truncated_messages: int = 0
    mailbox_incoming_truncated_bytes: int = 0
    mailbox_incoming_dropped_bytes: int = 0
    mailbox_incoming_overflow_events: int = 0
    mcu_version: tuple[int, int] | None = None
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
    serial_throughput_stats: SerialThroughputStats = msgspec.field(default_factory=SerialThroughputStats)
    serial_latency_stats: SerialLatencyStats = msgspec.field(default_factory=SerialLatencyStats)
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
    supervisor_stats: dict[str, SupervisorStats] = msgspec.field(default_factory=lambda: {})

    @property
    def mqtt_messages_published(self) -> int:
        return int(self.metrics.mqtt_messages_published.get())

    @property
    def mqtt_dropped_messages(self) -> int:
        return int(self.metrics.mqtt_messages_dropped.get())

    @property
    def mqtt_spooled_messages(self) -> int:
        return int(self.metrics.mqtt_spooled_messages.get())

    @property
    def mqtt_spool_errors(self) -> int:
        return int(self.metrics.mqtt_spool_errors.get())

    @property
    def serial_bytes_sent(self) -> int:
        return int(self.metrics.serial_bytes_sent.get())

    @property
    def serial_bytes_received(self) -> int:
        return int(self.metrics.serial_bytes_received.get())

    @property
    def serial_frames_sent(self) -> int:
        return int(self.metrics.serial_frames_sent.get())

    @property
    def serial_frames_received(self) -> int:
        return int(self.metrics.serial_frames_received.get())

    @property
    def serial_crc_errors(self) -> int:
        return int(self.metrics.serial_crc_errors.get())

    @property
    def serial_decode_errors(self) -> int:
        return int(self.metrics.serial_decode_errors.get())

    @property
    def handshake_attempts(self) -> int:
        return int(self.metrics.handshake_attempts.get())

    @property
    def handshake_successes(self) -> int:
        return int(self.metrics.handshake_successes.get())

    @property
    def handshake_failures(self) -> int:
        return self.handshake_attempts - self.handshake_successes

    @property
    def watchdog_beats(self) -> int:
        return int(self.metrics.watchdog_beats.get())

    @property
    def allowed_commands(self) -> tuple[str, ...]:
        return self.allowed_policy.as_tuple()

    def record_mqtt_publish(self) -> None:
        self.metrics.mqtt_messages_published.inc()

    def record_mqtt_drop(self, topic: str) -> None:
        self.mqtt_drop_counts[topic] = self.mqtt_drop_counts.get(topic, 0) + 1
        self.metrics.mqtt_messages_dropped.inc()

    def record_mqtt_spool(self) -> None:
        self.metrics.mqtt_spooled_messages.inc()

    def record_mqtt_spool_error(self) -> None:
        self.metrics.mqtt_spool_errors.inc()

    def record_serial_tx(self, nbytes: int) -> None:
        self.metrics.serial_bytes_sent.inc(nbytes)
        self.metrics.serial_frames_sent.inc()
        self.serial_throughput_stats.record_tx(nbytes)

    def record_serial_rx(self, nbytes: int) -> None:
        self.metrics.serial_bytes_received.inc(nbytes)
        self.metrics.serial_frames_received.inc()
        self.serial_throughput_stats.record_rx(nbytes)

    def record_serial_crc_error(self) -> None:
        self.metrics.serial_crc_errors.inc()

    def record_serial_decode_error(self) -> None:
        self.metrics.serial_decode_errors.inc()

    def record_handshake_attempt(self) -> None:
        self.last_handshake_unix = time.time()
        self._handshake_last_started = time.monotonic()
        self.metrics.handshake_attempts.inc()

    def record_handshake_success(self) -> None:
        self.handshake_failure_streak = 0
        self.last_handshake_unix = time.time()
        self.handshake_last_duration = self.last_handshake_unix - self._handshake_last_started
        self.mark_synchronized()
        self.metrics.handshake_successes.inc()

    def record_handshake_failure(self, reason: str) -> None:
        self.handshake_failure_streak += 1
        self.last_handshake_error = reason
        self.last_handshake_unix = time.time()
        self.handshake_last_duration = self.last_handshake_unix - self._handshake_last_started
        self.mark_transport_connected()

    def record_watchdog_beat(self, timestamp: float | None = None) -> None:
        self.metrics.watchdog_beats.inc()
        self.last_watchdog_beat = timestamp or time.time()

    def record_supervisor_failure(self, name: str, backoff: float, exc: Exception) -> None:
        stats = self.supervisor_stats.setdefault(name, SupervisorStats())
        stats.restarts += 1
        stats.last_failure_unix = time.time()
        stats.last_exception = f"{exc.__class__.__name__}: {exc}"
        stats.backoff_seconds = backoff

    def configure(self, config: RuntimeConfig) -> None:
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

    def mark_supervisor_healthy(self, name: str) -> None:
        stats = self.supervisor_stats.get(name)
        if stats:
            stats.backoff_seconds = 0.0
            stats.fatal = False

    def enqueue_console_chunk(self, chunk: bytes, logger: logging.Logger) -> None:
        if not chunk:
            return
        self._sync_console_queue_limits()
        evt = self.console_to_mcu_queue.append(chunk)
        if evt.truncated_bytes:
            self.console_truncated_chunks += 1
            self.console_truncated_bytes += evt.truncated_bytes
        if evt.dropped_chunks:
            self.console_dropped_chunks += evt.dropped_chunks
            self.console_dropped_bytes += evt.dropped_bytes
        if not evt.accepted:
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
        limit = self.console_queue_limit_bytes
        data = bytes(chunk[-limit:]) if len(chunk) > limit else bytes(chunk)
        self._sync_console_queue_limits()
        evt = self.console_to_mcu_queue.appendleft(data)
        if evt.accepted:
            self.console_queue_bytes = self.console_to_mcu_queue.bytes_used

    def enqueue_mailbox_message(self, payload: bytes, logger: logging.Logger) -> bool:
        return self._enqueue_mailbox(payload, logger, self.mailbox_queue, "outgoing")

    def pop_mailbox_message(self) -> bytes | None:
        return self._pop_mailbox(self.mailbox_queue)

    def requeue_mailbox_message_front(self, payload: bytes) -> None:
        self._sync_mailbox_limits(self.mailbox_queue)
        self.mailbox_queue.appendleft(payload)
        self._update_mailbox_bytes()

    def enqueue_mailbox_incoming(self, payload: bytes, logger: logging.Logger) -> bool:
        return self._enqueue_mailbox(payload, logger, self.mailbox_incoming_queue, "incoming")

    def pop_mailbox_incoming(self) -> bytes | None:
        return self._pop_mailbox(self.mailbox_incoming_queue)

    def _sync_console_queue_limits(self) -> None:
        self.console_to_mcu_queue.update_limits(max_items=None, max_bytes=self.console_queue_limit_bytes)
        self.console_queue_bytes = self.console_to_mcu_queue.bytes_used

    def _sync_mailbox_limits(self, queue: BoundedByteDeque) -> None:
        queue.update_limits(max_items=self.mailbox_queue_limit, max_bytes=self.mailbox_queue_bytes_limit)

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
        self._sync_mailbox_limits(queue)
        evt = queue.append(payload)
        self._update_mailbox_bytes()
        is_inc = direction == "incoming"
        if evt.truncated_bytes:
            if is_inc:
                self.mailbox_incoming_truncated_messages += 1
                self.mailbox_incoming_truncated_bytes += evt.truncated_bytes
            else:
                self.mailbox_truncated_messages += 1
                self.mailbox_truncated_bytes += evt.truncated_bytes
        if evt.dropped_chunks:
            if is_inc:
                self.mailbox_incoming_dropped_messages += evt.dropped_chunks
                self.mailbox_incoming_dropped_bytes += evt.dropped_bytes
            else:
                self.mailbox_dropped_messages += evt.dropped_chunks
                self.mailbox_dropped_bytes += evt.dropped_bytes
        if not evt.accepted:
            if is_inc:
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
        self._sync_mailbox_limits(queue)
        if not queue:
            return None
        msg = queue.popleft()
        self._update_mailbox_bytes()
        return msg

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
                payload["duration"] = max(
                    0.0,
                    timestamp - float(inf.get("started_unix", timestamp)),
                )
            self.serial_pipeline_last = payload
            self.serial_pipeline_inflight = None

    def record_unknown_command_id(self, command_id: int) -> None:
        self.unknown_command_ids += 1

    def record_rpc_latency_ms(self, latency_ms: float) -> None:
        self.serial_latency_stats.record(latency_ms)
        self.metrics.serial_latency_ms.observe(latency_ms)

    def record_mcu_status(self, status: Status) -> None:
        self.mcu_status_counters[status.name] = self.mcu_status_counters.get(status.name, 0) + 1

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
        except (OSError, MQTTSpoolError) as exc:
            self._handle_mqtt_spool_failure("initialization_failed", exc=exc)

    async def ensure_spool(self) -> bool:
        if self.mqtt_spool:
            return True
        if not self.mqtt_spool_dir or self.mqtt_spool_limit <= 0 or self._spool_backoff_remaining() > 0:
            return False
        try:
            self.mqtt_spool = await asyncio.to_thread(
                MQTTPublishSpool,
                self.mqtt_spool_dir,
                self.mqtt_spool_limit,
                on_fallback=self._on_spool_fallback,
            )
            self.mqtt_spool_degraded = False
            self.mqtt_spool_recoveries += 1
            return True
        except (OSError, MQTTSpoolError) as exc:
            self._handle_mqtt_spool_failure("reactivation_failed", exc=exc)
            return False

    def _spool_backoff_remaining(self) -> float:
        return max(0.0, self.mqtt_spool_backoff_until - time.monotonic()) if self.mqtt_spool_backoff_until > 0 else 0.0

    def _disable_mqtt_spool(self, reason: str, schedule_retry: bool = True) -> None:
        if self.mqtt_spool:
            with contextlib.suppress(Exception):
                self.mqtt_spool.close()
        self.mqtt_spool = None
        self.mqtt_spool_degraded = True
        self.mqtt_spool_failure_reason = reason
        if schedule_retry:
            self.mqtt_spool_retry_attempts = min(self.mqtt_spool_retry_attempts + 1, 6)
            delay = min(
                SPOOL_BACKOFF_MIN_SECONDS * (2 ** (self.mqtt_spool_retry_attempts - 1)),
                SPOOL_BACKOFF_MAX_SECONDS,
            )
            self.mqtt_spool_backoff_until = time.monotonic() + delay

    def _handle_mqtt_spool_failure(self, reason: str, exc: BaseException | None = None) -> None:
        self.metrics.mqtt_spool_errors.inc()
        self._disable_mqtt_spool(reason)

    def _on_spool_fallback(self, reason: str) -> None:
        self.mqtt_spool_degraded = True
        self.metrics.mqtt_spool_errors.inc()

    async def stash_mqtt_message(self, message: QueuedPublish) -> bool:
        if not await self.ensure_spool():
            return False
        spool = self.mqtt_spool
        if spool is None:
            return False
        try:
            await asyncio.to_thread(spool.append, message)
            self.metrics.mqtt_spooled_messages.inc()
            return True
        except Exception as exc:
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
                self.mqtt_publish_queue.put_nowait(msgspec.structs.replace(msg, user_properties=props))
                self.mqtt_spooled_replayed += 1
            except Exception as exc:
                self._handle_mqtt_spool_failure("pop_failed", exc=exc)
                break

    def build_metrics_snapshot(self) -> dict[str, Any]:
        return {
            "serial": msgspec.structs.asdict(self.serial_flow_stats),
            "serial_throughput": msgspec.structs.asdict(self.serial_throughput_stats),
            "serial_latency": self.serial_latency_stats.as_dict(),
            "queue_depths": {
                "mqtt": self.mqtt_publish_queue.qsize(),
                "console": len(self.console_to_mcu_queue),
                "mailbox_outgoing": len(self.mailbox_queue),
                "mailbox_incoming": len(self.mailbox_incoming_queue),
                "running_processes": len(self.running_processes),
            },
            "handshake": self.build_handshake_snapshot(),
            "link_synchronised": self.is_synchronized,
            "system": collect_system_metrics(),
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
            serial_pipeline=SerialPipelineSnapshot(
                inflight=self.serial_pipeline_inflight,
                last_completion=self.serial_pipeline_last,
            ),
            serial_flow=msgspec.structs.asdict(self.serial_flow_stats),
            mcu_version=McuVersion(*self.mcu_version) if self.mcu_version else None,
            capabilities=self.mcu_capabilities.as_dict() if self.mcu_capabilities else None,
        )

    def _handshake_duration_since_start(self) -> float:
        if self._handshake_last_started <= 0.0:
            return 0.0
        return max(0.0, time.monotonic() - self._handshake_last_started)

    def cleanup(self) -> None:
        with contextlib.suppress(Exception):
            self.mqtt_publish_queue = None  # type: ignore
            self.serial_tx_allowed = None  # type: ignore
            self.process_lock = None  # type: ignore
            self.link_sync_event = None  # type: ignore
            self.running_processes.clear()


def create_runtime_state(config: RuntimeConfig | dict[str, Any]) -> RuntimeState:
    from ..config.settings import RuntimeConfig as RC

    cfg = msgspec.convert(config, RC) if isinstance(config, dict) else config
    state = RuntimeState(
        mqtt_publish_queue=asyncio.Queue(cfg.mqtt_queue_limit),
        serial_tx_allowed=asyncio.Event(),
        process_lock=asyncio.Lock(),
        link_sync_event=asyncio.Event(),
    )
    state.serial_tx_allowed.set()
    state.configure(cfg)
    state.configure_spool(cfg.mqtt_spool_dir, cfg.mqtt_queue_limit * 4)
    state.initialize_spool()
    return state

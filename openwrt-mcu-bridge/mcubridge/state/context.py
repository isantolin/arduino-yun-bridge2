"""Runtime state container for the MCU Bridge daemon."""

from __future__ import annotations

import asyncio
import collections
import logging
import time
import pickle
try:
    import sqlite3
    SqliteError = sqlite3.Error  # type: ignore[assignment]
except ImportError:
    # Fallback for systems without sqlite3 to prevent NameError in except blocks
    sqlite3 = None  # type: ignore
    class SqliteError(Exception):  # type: ignore
        pass

from asyncio.subprocess import Process
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Any, Deque, cast
from collections.abc import Mapping

from aiomqtt.message import Message

from ..mqtt.messages import QueuedPublish
from ..mqtt.spool import MQTTPublishSpool, MQTTSpoolError
from ..policy import AllowedCommandPolicy, TopicAuthorization
from .queues import BoundedByteDeque
from ..config.settings import RuntimeConfig

from ..const import (
    DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES,
    DEFAULT_FILE_SYSTEM_ROOT,
    DEFAULT_FILE_STORAGE_QUOTA_BYTES,
    DEFAULT_FILE_WRITE_MAX_BYTES,
    DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT,
    DEFAULT_MAILBOX_QUEUE_LIMIT,
    DEFAULT_PENDING_PIN_REQUESTS,
    DEFAULT_MQTT_QUEUE_LIMIT,
    DEFAULT_MQTT_SPOOL_DIR,
    DEFAULT_PROCESS_MAX_CONCURRENT,
    DEFAULT_PROCESS_MAX_OUTPUT_BYTES,
    DEFAULT_PROCESS_TIMEOUT,
    DEFAULT_SERIAL_RESPONSE_TIMEOUT,
    DEFAULT_SERIAL_RETRY_TIMEOUT,
    DEFAULT_WATCHDOG_INTERVAL,
    SPOOL_BACKOFF_MAX_SECONDS,
    SPOOL_BACKOFF_MIN_SECONDS,
    SPOOL_BACKOFF_MULTIPLIER,
)
from ..rpc import protocol
from ..rpc.protocol import (
    Command,
    Status,
    DEFAULT_RETRY_LIMIT,
)

logger = logging.getLogger("mcubridge.state")

SpoolSnapshot = dict[str, int | float]


def _mqtt_queue_factory() -> asyncio.Queue[QueuedPublish]:
    return asyncio.Queue()


def _serial_tx_event_factory() -> asyncio.Event:
    evt = asyncio.Event()
    evt.set()
    return evt


def _empty_spool_snapshot_factory() -> SpoolSnapshot:
    return cast(SpoolSnapshot, {})


def _spool_wait_strategy_factory() -> Any:
    return _ExponentialBackoff(
        multiplier=SPOOL_BACKOFF_MULTIPLIER,
        min_val=SPOOL_BACKOFF_MIN_SECONDS,
        max_val=SPOOL_BACKOFF_MAX_SECONDS,
    )


def _serial_pipeline_base_payload(command_id: int, attempt: int) -> dict[str, Any]:
    return {
        "command_id": command_id,
        "command_name": _command_name(command_id),
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


class _ExponentialBackoff:
    def __init__(self, min_val: float, max_val: float, multiplier: float) -> None:
        self.min = min_val
        self.max = max_val
        self.multiplier = multiplier

    def __call__(self, retry_state: Any) -> float:
        attempt = getattr(retry_state, "attempt_number", 1)
        # Simple exponential backoff: multiplier * 2^(attempt-1)
        delay = self.multiplier * (2 ** (attempt - 1))
        return max(self.min, min(delay, self.max))


def _command_name(command_id: int) -> str:
    try:
        return Command(command_id).name
    except ValueError:
        return f"0x{command_id:02X}"


def _status_label(code: int | None) -> str:
    if code is None:
        return "unknown"
    try:
        return Status(code).name
    except ValueError:
        return f"0x{code:02X}"


@dataclass(slots=True)
class PendingPinRequest:
    pin: int
    reply_context: Message | None


@dataclass(slots=True)
class ManagedProcess:
    pid: int
    command: str = ""
    handle: Process | None = None
    stdout_buffer: bytearray = field(default_factory=bytearray)
    stderr_buffer: bytearray = field(default_factory=bytearray)
    exit_code: int | None = None
    io_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock,
        repr=False,
    )

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


def _pending_pin_deque_factory() -> Deque[PendingPinRequest]:
    return collections.deque()


def _str_dict_factory() -> dict[str, str]:
    return {}


def _policy_factory() -> AllowedCommandPolicy:
    return AllowedCommandPolicy.from_iterable(())


def _str_int_dict_factory() -> dict[str, int]:
    return {}


def _process_map_factory() -> dict[int, ManagedProcess]:
    return {}


def _supervisor_stats_factory() -> dict[str, SupervisorStats]:
    return {}


# [EXTENDED METRICS] Latency histogram bucket boundaries in milliseconds
LATENCY_BUCKETS_MS: tuple[float, ...] = (5.0, 10.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1000.0, 2500.0)


@dataclass(slots=True)
class SerialThroughputStats:
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

    def as_dict(self) -> dict[str, float | int]:
        return {
            "bytes_sent": self.bytes_sent,
            "bytes_received": self.bytes_received,
            "frames_sent": self.frames_sent,
            "frames_received": self.frames_received,
            "last_tx_unix": self.last_tx_unix,
            "last_rx_unix": self.last_rx_unix,
        }


def _latency_bucket_counts_factory() -> list[int]:
    """Factory for SerialLatencyStats bucket_counts field."""
    return [0] * len(LATENCY_BUCKETS_MS)


@dataclass(slots=True)
class SerialLatencyStats:
    """RPC command latency histogram for performance monitoring.

    [SIL-2] Fixed bucket boundaries, no dynamic allocation.
    Buckets represent cumulative counts (Prometheus histogram style).
    """
    # Histogram bucket counts (cumulative, le=bucket_ms)
    bucket_counts: list[int] = field(default_factory=_latency_bucket_counts_factory)
    # Total observations above largest bucket
    overflow_count: int = 0
    # Running totals for average calculation
    total_observations: int = 0
    total_latency_ms: float = 0.0
    # Min/max tracking
    min_latency_ms: float = float("inf")
    max_latency_ms: float = 0.0

    def record(self, latency_ms: float) -> None:
        """Record a latency observation into histogram buckets."""
        self.total_observations += 1
        self.total_latency_ms += latency_ms
        if latency_ms < self.min_latency_ms:
            self.min_latency_ms = latency_ms
        if latency_ms > self.max_latency_ms:
            self.max_latency_ms = latency_ms

        # Cumulative bucket counts (le style)
        for i, bucket in enumerate(LATENCY_BUCKETS_MS):
            if latency_ms <= bucket:
                self.bucket_counts[i] += 1
        if latency_ms > LATENCY_BUCKETS_MS[-1]:
            self.overflow_count += 1

    def as_dict(self) -> dict[str, Any]:
        avg = (
            self.total_latency_ms / self.total_observations
            if self.total_observations > 0
            else 0.0
        )
        return {
            "buckets": {
                f"le_{int(b)}ms": self.bucket_counts[i]
                for i, b in enumerate(LATENCY_BUCKETS_MS)
            },
            "overflow": self.overflow_count,
            "count": self.total_observations,
            "sum_ms": self.total_latency_ms,
            "avg_ms": avg,
            "min_ms": self.min_latency_ms if self.total_observations > 0 else 0.0,
            "max_ms": self.max_latency_ms,
        }


@dataclass(slots=True)
class SerialFlowStats:
    commands_sent: int = 0
    commands_acked: int = 0
    retries: int = 0
    failures: int = 0
    last_event_unix: float = 0.0

    def as_dict(self) -> dict[str, float | int]:
        return {
            "commands_sent": self.commands_sent,
            "commands_acked": self.commands_acked,
            "retries": self.retries,
            "failures": self.failures,
            "last_event_unix": self.last_event_unix,
        }


@dataclass(slots=True)
class SupervisorStats:
    restarts: int = 0
    last_failure_unix: float = 0.0
    last_exception: str | None = None
    backoff_seconds: float = 0.0
    fatal: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "restarts": self.restarts,
            "last_failure_unix": self.last_failure_unix,
            "last_exception": self.last_exception,
            "backoff_seconds": self.backoff_seconds,
            "fatal": self.fatal,
        }


@dataclass(slots=True)
class RuntimeState:
    """Aggregated mutable state shared across the daemon layers."""

    serial_writer: asyncio.StreamWriter | None = None
    serial_link_connected: bool = False
    mqtt_publish_queue: asyncio.Queue[QueuedPublish] = field(
        default_factory=_mqtt_queue_factory
    )
    mqtt_queue_limit: int = DEFAULT_MQTT_QUEUE_LIMIT
    mqtt_dropped_messages: int = 0
    mqtt_drop_counts: dict[str, int] = field(default_factory=_str_int_dict_factory)
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
    _last_spool_snapshot: SpoolSnapshot = field(
        default_factory=_empty_spool_snapshot_factory,
        repr=False,
    )
    _spool_wait_strategy: Any = field(
        default_factory=_spool_wait_strategy_factory,
        init=False,
        repr=False,
    )
    datastore: dict[str, str] = field(default_factory=_str_dict_factory)
    mailbox_queue: BoundedByteDeque = field(default_factory=BoundedByteDeque)
    mcu_is_paused: bool = False
    serial_tx_allowed: asyncio.Event = field(default_factory=_serial_tx_event_factory)
    console_to_mcu_queue: BoundedByteDeque = field(default_factory=BoundedByteDeque)
    console_queue_limit_bytes: int = DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES
    console_queue_bytes: int = 0
    console_dropped_chunks: int = 0
    console_truncated_chunks: int = 0
    console_truncated_bytes: int = 0
    console_dropped_bytes: int = 0
    running_processes: dict[int, ManagedProcess] = field(
        default_factory=_process_map_factory
    )
    process_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    next_pid: int = 1
    allowed_policy: AllowedCommandPolicy = field(default_factory=_policy_factory)
    topic_authorization: TopicAuthorization = field(default_factory=TopicAuthorization)
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
    pending_digital_reads: Deque[PendingPinRequest] = field(
        default_factory=_pending_pin_deque_factory
    )
    pending_analog_reads: Deque[PendingPinRequest] = field(
        default_factory=_pending_pin_deque_factory
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
    mailbox_incoming_queue: BoundedByteDeque = field(default_factory=BoundedByteDeque)
    mailbox_incoming_queue_bytes: int = 0
    mailbox_incoming_dropped_messages: int = 0
    mailbox_incoming_truncated_messages: int = 0
    mailbox_incoming_truncated_bytes: int = 0
    mailbox_incoming_dropped_bytes: int = 0
    mailbox_incoming_overflow_events: int = 0
    mcu_version: tuple[int, int] | None = None
    link_handshake_nonce: bytes | None = None
    link_is_synchronized: bool = False
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
    serial_flow_stats: SerialFlowStats = field(default_factory=SerialFlowStats)
    serial_throughput_stats: SerialThroughputStats = field(default_factory=SerialThroughputStats)
    serial_latency_stats: SerialLatencyStats = field(default_factory=SerialLatencyStats)
    serial_pipeline_inflight: dict[str, Any] | None = None
    serial_pipeline_last: dict[str, Any] | None = None
    process_output_limit: int = DEFAULT_PROCESS_MAX_OUTPUT_BYTES
    process_max_concurrent: int = DEFAULT_PROCESS_MAX_CONCURRENT
    serial_decode_errors: int = 0
    serial_crc_errors: int = 0
    serial_ack_timeout_ms: int = int(DEFAULT_SERIAL_RETRY_TIMEOUT * 1000)
    serial_response_timeout_ms: int = int(DEFAULT_SERIAL_RESPONSE_TIMEOUT * 1000)
    serial_retry_limit: int = DEFAULT_RETRY_LIMIT
    mcu_status_counters: dict[str, int] = field(default_factory=_str_int_dict_factory)
    supervisor_stats: dict[str, SupervisorStats] = field(
        default_factory=_supervisor_stats_factory
    )

    def configure(self, config: RuntimeConfig) -> None:
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
                (
                    "Dropping oldest console chunk(s): %d item(s), %d "
                    "bytes to respect limit."
                ),
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
            data = bytes(chunk[-self.console_queue_limit_bytes:])
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
        self._sync_mailbox_queue_limits()
        evt = self.mailbox_queue.append(payload)
        self.mailbox_queue_bytes = self.mailbox_queue.bytes_used
        if evt.truncated_bytes:
            logger.warning(
                "Mailbox message truncated by %d bytes to respect limit.",
                evt.truncated_bytes,
            )
            self.mailbox_truncated_messages += 1
            self.mailbox_truncated_bytes += evt.truncated_bytes
        if evt.dropped_chunks:
            logger.warning(
                (
                    "Dropping oldest mailbox message(s): %d item(s), %d "
                    "bytes to honor limits."
                ),
                evt.dropped_chunks,
                evt.dropped_bytes,
            )
            self.mailbox_dropped_messages += evt.dropped_chunks
            self.mailbox_dropped_bytes += evt.dropped_bytes
        if not evt.accepted:
            logger.error(
                ("Mailbox queue overflow; rejecting incoming message " "(%d bytes)."),
                len(payload),
            )
            self.mailbox_dropped_messages += 1
            self.mailbox_dropped_bytes += len(payload)
            self.mailbox_outgoing_overflow_events += 1
            return False
        return True

    def pop_mailbox_message(self) -> bytes | None:
        self._sync_mailbox_queue_limits()
        if not self.mailbox_queue:
            return None
        message = self.mailbox_queue.popleft()
        self.mailbox_queue_bytes = self.mailbox_queue.bytes_used
        return message

    def requeue_mailbox_message_front(self, payload: bytes) -> None:
        self._sync_mailbox_queue_limits()
        evt = self.mailbox_queue.appendleft(payload)
        self.mailbox_queue_bytes = self.mailbox_queue.bytes_used
        if evt.dropped_chunks:
            self.mailbox_dropped_messages += evt.dropped_chunks
            self.mailbox_dropped_bytes += evt.dropped_bytes

    def enqueue_mailbox_incoming(self, payload: bytes, logger: logging.Logger) -> bool:
        self._sync_mailbox_incoming_limits()
        evt = self.mailbox_incoming_queue.append(payload)
        self.mailbox_incoming_queue_bytes = self.mailbox_incoming_queue.bytes_used
        if evt.truncated_bytes:
            logger.warning(
                ("Mailbox incoming message truncated by %d bytes to " "respect limit."),
                evt.truncated_bytes,
            )
            self.mailbox_incoming_truncated_messages += 1
            self.mailbox_incoming_truncated_bytes += evt.truncated_bytes
        if evt.dropped_chunks:
            logger.warning(
                (
                    "Dropping oldest mailbox incoming message(s): %d "
                    "item(s), %d bytes to honor limits."
                ),
                evt.dropped_chunks,
                evt.dropped_bytes,
            )
            self.mailbox_incoming_dropped_messages += evt.dropped_chunks
            self.mailbox_incoming_dropped_bytes += evt.dropped_bytes
        if not evt.accepted:
            logger.error(
                ("Mailbox incoming queue overflow; rejecting message " "(%d bytes)."),
                len(payload),
            )
            self.mailbox_incoming_dropped_messages += 1
            self.mailbox_incoming_dropped_bytes += len(payload)
            self.mailbox_incoming_overflow_events += 1
            return False
        return True

    def pop_mailbox_incoming(self) -> bytes | None:
        self._sync_mailbox_incoming_limits()
        if not self.mailbox_incoming_queue:
            return None
        message = self.mailbox_incoming_queue.popleft()
        self.mailbox_incoming_queue_bytes = self.mailbox_incoming_queue.bytes_used
        return message

    def _sync_console_queue_limits(self) -> None:
        self.console_to_mcu_queue.update_limits(
            max_items=None,
            max_bytes=self.console_queue_limit_bytes,
        )
        self.console_queue_bytes = self.console_to_mcu_queue.bytes_used

    def _sync_mailbox_queue_limits(self) -> None:
        self.mailbox_queue.update_limits(
            max_items=self.mailbox_queue_limit,
            max_bytes=self.mailbox_queue_bytes_limit,
        )
        self.mailbox_queue_bytes = self.mailbox_queue.bytes_used

    def _sync_mailbox_incoming_limits(self) -> None:
        self.mailbox_incoming_queue.update_limits(
            max_items=self.mailbox_queue_limit,
            max_bytes=self.mailbox_queue_bytes_limit,
        )
        self.mailbox_incoming_queue_bytes = self.mailbox_incoming_queue.bytes_used

    def record_mqtt_drop(self, topic: str) -> None:
        self.mqtt_dropped_messages += 1
        self.mqtt_drop_counts[topic] = self.mqtt_drop_counts.get(topic, 0) + 1

    def record_watchdog_beat(self, timestamp: float | None = None) -> None:
        self.watchdog_beats += 1
        self.last_watchdog_beat = (
            timestamp if timestamp is not None else time.monotonic()
        )

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
        except (OSError, SqliteError) as exc:
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
        except (OSError, SqliteError) as exc:
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
        retry_state = SimpleNamespace(
            attempt_number=max(1, self.mqtt_spool_retry_attempts)
        )
        delay = self._spool_wait_strategy(retry_state)
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
            except (OSError, SqliteError):
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
        except (OSError, SqliteError, pickle.PickleError) as exc:
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
            except (OSError, SqliteError, pickle.PickleError) as exc:
                reason = "pop_failed"
                if isinstance(exc, MQTTSpoolError):
                    reason = exc.reason
                self._handle_mqtt_spool_failure(reason, exc=exc)
                break
            if message is None:
                break
            enriched = replace(
                message,
                user_properties=message.user_properties + (("bridge-spooled", "1"),),
            )
            try:
                self.mqtt_publish_queue.put_nowait(enriched)
                self.mqtt_spooled_replayed += 1
            except asyncio.QueueFull:
                try:
                    await asyncio.to_thread(spool.requeue, message)
                except (OSError, SqliteError, pickle.PickleError) as exc:
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
            "mcu_status": dict(self.mcu_status_counters),
            "watchdog_enabled": self.watchdog_enabled,
            "watchdog_interval": self.watchdog_interval,
            "watchdog_beats": self.watchdog_beats,
            "watchdog_last_unix": self.last_watchdog_beat,
            "supervisors": {
                name: stats.as_dict() for name, stats in self.supervisor_stats.items()
            },
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
            mailbox_incoming_truncated_messages=(
                self.mailbox_incoming_truncated_messages
            ),
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
        return snapshot

    def build_handshake_snapshot(self) -> dict[str, Any]:
        return {
            "synchronised": self.link_is_synchronized,
            "attempts": self.handshake_attempts,
            "successes": self.handshake_successes,
            "failures": self.handshake_failures,
            "failure_streak": self.handshake_failure_streak,
            "last_error": self.last_handshake_error,
            "last_unix": self.last_handshake_unix,
            "last_duration": self.handshake_last_duration,
            "backoff_until": self.handshake_backoff_until,
            "rate_limit_until": self.handshake_rate_limit_until,
            "fatal_count": self.handshake_fatal_count,
            "fatal_reason": self.handshake_fatal_reason,
            "fatal_detail": self.handshake_fatal_detail,
            "fatal_unix": self.handshake_fatal_unix,
            "pending_nonce": bool(self.link_handshake_nonce),
            "nonce_length": self.link_nonce_length,
        }

    def build_serial_pipeline_snapshot(self) -> dict[str, Any]:
        inflight = (
            self.serial_pipeline_inflight.copy()
            if self.serial_pipeline_inflight
            else None
        )
        last = self.serial_pipeline_last.copy() if self.serial_pipeline_last else None
        return {
            "inflight": inflight,
            "last_completion": last,
        }

    def build_bridge_snapshot(self) -> dict[str, Any]:
        mcu_version = None
        if self.mcu_version is not None:
            mcu_version = {
                "major": self.mcu_version[0],
                "minor": self.mcu_version[1],
            }
        return {
            "serial_link": {
                "connected": self.serial_link_connected,
                "writer_attached": self.serial_writer is not None,
                "synchronised": self.link_is_synchronized,
            },
            "handshake": self.build_handshake_snapshot(),
            "serial_pipeline": self.build_serial_pipeline_snapshot(),
            "serial_flow": self.serial_flow_stats.as_dict(),
            "mcu_version": mcu_version,
        }

    def _handshake_duration_since_start(self) -> float:
        if self._handshake_last_started <= 0.0:
            return 0.0
        return max(0.0, time.monotonic() - self._handshake_last_started)


def create_runtime_state(config: RuntimeConfig) -> RuntimeState:
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

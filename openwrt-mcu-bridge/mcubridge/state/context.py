"""Runtime state container for the MCU Bridge daemon."""

from __future__ import annotations

import asyncio
import collections
import logging
import time
from asyncio.subprocess import Process
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, cast

import msgspec
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
    DEFAULT_WATCHDOG_INTERVAL,
)
from ..mqtt.messages import QueuedPublish
from ..mqtt.spool import MQTTPublishSpool
from ..policy import AllowedCommandPolicy, TopicAuthorization
from ..protocol import protocol
from ..protocol.protocol import (
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
    collect_system_metrics,
)
from .metrics import DaemonMetrics
from .queues import BoundedByteDeque

logger = logging.getLogger("mcubridge.state")

SpoolSnapshot = dict[str, int | float]


def _coerce_snapshot_int(snapshot: Mapping[str, Any], name: str, current: int) -> int:
    val = snapshot.get(name)
    try:
        return int(val) if val is not None else current
    except (ValueError, TypeError):
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
    "collect_system_metrics",
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
    return (
        "unknown"
        if code is None
        else next((s.name for s in Status if s.value == code), f"0x{code:02X}")
    )


class PendingPinRequest(msgspec.Struct):
    """Pending pin read request."""

    pin: int
    future: asyncio.Future[int] = msgspec.field(default_factory=lambda: asyncio.Future())
    timestamp: float = 0.0
    reply_context: Any | None = None


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
        self._machine.add_transition(
            "start", PROCESS_STATE_STARTING, PROCESS_STATE_RUNNING
        )
        self._machine.add_transition(
            "sigchld", PROCESS_STATE_RUNNING, PROCESS_STATE_DRAINING
        )
        self._machine.add_transition(
            "io_complete", PROCESS_STATE_DRAINING, PROCESS_STATE_FINISHED
        )
        self._machine.add_transition(
            "finalize", PROCESS_STATE_FINISHED, PROCESS_STATE_ZOMBIE
        )
        # Allow force cleanup from any state
        self._machine.add_transition("force_kill", "*", PROCESS_STATE_ZOMBIE)

    def trigger(self, event: str, *args: Any, **kwargs: Any) -> bool:
        """FSM trigger placeholder."""
        return cast(bool, self._machine.dispatch(event, *args, **kwargs))

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


def _serial_tx_allowed_factory() -> asyncio.Event:
    evt = asyncio.Event()
    evt.set()
    return evt


class RuntimeState(msgspec.Struct):
    """Aggregated mutable state shared across the daemon layers."""

    metrics: DaemonMetrics = msgspec.field(default_factory=DaemonMetrics)
    serial_writer: asyncio.BaseTransport | None = None
    config_source: str = "defaults"
    link_is_synchronized: bool = False
    link_sync_event: asyncio.Event = msgspec.field(default_factory=asyncio.Event)

    # [SIL-2] Lifecycle FSM (Single Source of Truth)
    # Note: Machine is not serializable, so it must be handled carefully.
    _fsm: Any = msgspec.field(default=None)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_fsm", Machine(
            states=["disconnected", "connected", "synchronized"],
            initial="disconnected",
            ignore_invalid_triggers=True
        ))
        self._fsm.on_enter_synchronized(self._on_fsm_synchronized)
        self._fsm.on_enter_connected(self._on_fsm_connected)
        self._fsm.on_enter_disconnected(self._on_fsm_disconnect)

    def _on_fsm_synchronized(self) -> None:
        self.link_is_synchronized = True
        if self.link_sync_event:
            self.link_sync_event.set()

    def _on_fsm_connected(self) -> None:
        self.link_is_synchronized = False
        if self.link_sync_event:
            self.link_sync_event.clear()

    def _on_fsm_disconnect(self) -> None:
        self.link_is_synchronized = False
        if self.link_sync_event:
            self.link_sync_event.clear()

    @property
    def is_connected(self) -> bool:
        return self._fsm.state in {"connected", "synchronized"}

    @property
    def is_synchronized(self) -> bool:
        return self._fsm.state == "synchronized"

    def mark_transport_connected(self) -> None:
        self._fsm.set_state("connected")

    def mark_transport_disconnected(self) -> None:
        self._fsm.set_state("disconnected")

    def mark_synchronized(self) -> None:
        self._fsm.set_state("synchronized")

    mqtt_publish_queue: asyncio.Queue[QueuedPublish] = msgspec.field(
        default_factory=lambda: asyncio.Queue[QueuedPublish]()
    )
    mqtt_queue_limit: int = DEFAULT_MQTT_QUEUE_LIMIT
    mqtt_dropped_messages: int = 0
    mqtt_drop_counts: dict[str, int] = msgspec.field(
        default_factory=lambda: cast(dict[str, int], {})
    )
    mqtt_spooled_messages: int = 0
    mqtt_spooled_replayed: int = 0
    mqtt_spool: MQTTPublishSpool | None = None
    mqtt_spool_degraded: bool = False
    mqtt_spool_failure_reason: str | None = None
    mqtt_spool_dir: str = DEFAULT_MQTT_SPOOL_DIR
    mqtt_spool_limit: int = 0
    mqtt_spool_errors: int = 0
    mqtt_spool_retry_attempts: int = 0
    mqtt_spool_backoff_until: float = 0.0
    mqtt_spool_last_error: str | None = None
    mqtt_spool_recoveries: int = 0
    mqtt_spool_dropped_limit: int = 0
    mqtt_spool_trim_events: int = 0
    mqtt_spool_last_trim_unix: float = 0.0
    mqtt_spool_corrupt_dropped: int = 0
    _last_spool_snapshot: dict[str, Any] = msgspec.field(
        default_factory=lambda: cast(dict[str, Any], {})
    )

    allow_non_tmp_paths: bool = False
    datastore: dict[str, str] = msgspec.field(
        default_factory=lambda: cast(dict[str, str], {})
    )
    mailbox_queue: BoundedByteDeque = msgspec.field(default_factory=BoundedByteDeque)
    mcu_is_paused: bool = False
    serial_tx_allowed: asyncio.Event = msgspec.field(default_factory=_serial_tx_allowed_factory)
    console_to_mcu_queue: BoundedByteDeque = msgspec.field(default_factory=BoundedByteDeque)
    console_queue_limit_bytes: int = DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES
    console_queue_bytes: int = 0
    console_dropped_chunks: int = 0
    console_dropped_bytes: int = 0
    console_truncated_chunks: int = 0
    console_truncated_bytes: int = 0
    running_processes: dict[int, ManagedProcess] = msgspec.field(
        default_factory=lambda: cast(dict[int, ManagedProcess], {})
    )
    process_lock: asyncio.Lock = msgspec.field(default_factory=asyncio.Lock)
    next_pid: int = 1
    allowed_policy: AllowedCommandPolicy = msgspec.field(default_factory=lambda: AllowedCommandPolicy(entries=()))
    topic_authorization: TopicAuthorization = msgspec.field(default_factory=lambda: TopicAuthorization())
    process_timeout: int = DEFAULT_PROCESS_TIMEOUT
    process_output_limit: int = DEFAULT_PROCESS_MAX_OUTPUT_BYTES
    process_max_concurrent: int = DEFAULT_PROCESS_MAX_CONCURRENT
    file_system_root: str = DEFAULT_FILE_SYSTEM_ROOT
    file_write_max_bytes: int = DEFAULT_FILE_WRITE_MAX_BYTES
    file_storage_quota_bytes: int = DEFAULT_FILE_STORAGE_QUOTA_BYTES
    file_storage_bytes_used: int = 0
    file_write_limit_rejections: int = 0
    file_storage_limit_rejections: int = 0
    mqtt_topic_prefix: str = protocol.MQTT_DEFAULT_TOPIC_PREFIX
    mailbox_incoming_topic: str | None = None
    watchdog_enabled: bool = False
    watchdog_interval: float = DEFAULT_WATCHDOG_INTERVAL
    last_watchdog_beat: float = 0.0
    watchdog_beats: int = 0
    pending_digital_reads: collections.deque[PendingPinRequest] = msgspec.field(
        default_factory=lambda: collections.deque[PendingPinRequest]()
    )
    pending_analog_reads: collections.deque[PendingPinRequest] = msgspec.field(
        default_factory=lambda: collections.deque[PendingPinRequest]()
    )
    mailbox_queue_limit: int = DEFAULT_MAILBOX_QUEUE_LIMIT
    mailbox_queue_bytes_limit: int = DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT
    pending_pin_request_limit: int = DEFAULT_PENDING_PIN_REQUESTS
    mailbox_queue_bytes: int = 0
    mailbox_dropped_messages: int = 0
    mailbox_dropped_bytes: int = 0
    mailbox_truncated_messages: int = 0
    mailbox_truncated_bytes: int = 0
    mailbox_outgoing_overflow_events: int = 0
    mailbox_incoming_queue: BoundedByteDeque = msgspec.field(default_factory=BoundedByteDeque)
    mailbox_incoming_queue_bytes: int = 0
    mailbox_incoming_dropped_messages: int = 0
    mailbox_incoming_dropped_bytes: int = 0
    mailbox_incoming_truncated_messages: int = 0
    mailbox_incoming_truncated_bytes: int = 0
    mailbox_incoming_overflow_events: int = 0
    mcu_version: tuple[int, int] | None = None
    mcu_capabilities: McuCapabilities | None = None
    mcu_status_counters: dict[str, int] = msgspec.field(
        default_factory=lambda: cast(dict[str, int], {})
    )
    link_handshake_nonce: bytes | None = None
    link_expected_tag: bytes | None = None
    link_nonce_length: int = 0
    link_nonce_counter: int = 0
    link_last_nonce_counter: int = 0
    serial_ack_timeout_ms: int = 0
    serial_response_timeout_ms: int = 0
    serial_retry_limit: int = 0
    handshake_attempts: int = 0
    handshake_successes: int = 0
    handshake_failures: int = 0
    handshake_failure_streak: int = 0
    handshake_backoff_until: float = 0.0
    handshake_rate_limit_until: float = 0.0
    last_handshake_unix: float = 0.0
    last_handshake_error: str | None = None
    handshake_last_duration: float = 0.0
    handshake_fatal_count: int = 0
    handshake_fatal_reason: str | None = None
    handshake_fatal_detail: str | None = None
    handshake_fatal_unix: float = 0.0
    _handshake_last_started: float = 0.0
    supervisor_stats: dict[str, SupervisorStats] = msgspec.field(
        default_factory=lambda: cast(dict[str, SupervisorStats], {})
    )
    serial_flow_stats: SerialFlowStats = msgspec.field(default_factory=SerialFlowStats)
    serial_throughput_stats: SerialThroughputStats = msgspec.field(default_factory=SerialThroughputStats)
    serial_latency_stats: SerialLatencyStats = msgspec.field(default_factory=SerialLatencyStats)
    serial_pipeline_inflight: dict[str, Any] | None = None
    serial_pipeline_last: dict[str, Any] | None = None
    serial_decode_errors: int = 0
    serial_crc_errors: int = 0
    unknown_command_ids: int = 0

    @property
    def allowed_commands(self) -> list[str]: return list(self.allowed_policy.entries)

    def record_serial_decode_error(self) -> None:
        self.serial_decode_errors += 1
        self.metrics.serial_failures.inc()

    def record_serial_crc_error(self) -> None:
        self.serial_crc_errors += 1
        self.metrics.serial_crc_errors.inc()

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

        inflight = self.serial_pipeline_inflight
        if name == "ack" and inflight is not None:
            inflight["acknowledged"] = True
            inflight["ack_unix"] = timestamp
            inflight["last_event"] = "ack"
            return

        if name in {"success", "failure", "abandoned"}:
            payload: dict[str, Any] = {
                "command_id": command_id,
                "command_name": resolve_command_id(command_id),
                "event": name,
                "completed_unix": timestamp,
                "status_code": status_code,
                "status_name": _status_label(cast(int, status_code)),
                "acknowledged": acked or (inflight["acknowledged"] if inflight else False),
            }
            if inflight:
                payload["started_unix"] = inflight["started_unix"]
                payload["duration"] = timestamp - inflight["started_unix"]
                payload["ack_unix"] = inflight.get("ack_unix")
            self.serial_pipeline_last = payload
            self.serial_pipeline_inflight = None

    def record_unknown_command_id(self, command_id: int) -> None:
        self.unknown_command_ids += 1
        self.metrics.serial_failures.inc()

    def record_rpc_latency_ms(self, latency_ms: float) -> None:
        self.serial_latency_stats.record(latency_ms)
        self.metrics.serial_latency_ms.observe(latency_ms)

    def record_mcu_status(self, status: Status) -> None:
        self.mcu_status_counters[status.name] = self.mcu_status_counters.get(status.name, 0) + 1

    def record_mqtt_drop(self, topic: str) -> None:
        self.mqtt_dropped_messages += 1
        self.mqtt_drop_counts[topic] = self.mqtt_drop_counts.get(topic, 0) + 1
        self.metrics.mqtt_messages_dropped.inc()

    def record_watchdog_beat(self) -> None:
        self.watchdog_beats += 1
        self.last_watchdog_beat = time.time()
        self.metrics.watchdog_beats.inc()

    def record_handshake_attempt(self) -> None:
        self.handshake_attempts += 1
        self.last_handshake_unix = time.time()
        self._handshake_last_started = time.monotonic()
        self.metrics.handshake_attempts.inc()

    def record_handshake_success(self) -> None:
        self.handshake_successes += 1
        self.handshake_failure_streak = 0
        self.handshake_last_duration = time.monotonic() - self._handshake_last_started
        self.metrics.handshake_successes.inc()

    def record_handshake_failure(self, error: str) -> None:
        self.handshake_failures += 1
        self.handshake_failure_streak += 1
        self.last_handshake_error = error
        self.metrics.serial_failures.inc()

    def record_handshake_fatal(self, reason: str, detail: str | None = None) -> None:
        self.handshake_fatal_count += 1
        self.handshake_fatal_reason = reason
        self.handshake_fatal_detail = detail
        self.handshake_fatal_unix = time.time()

    def apply_handshake_stats(self, stats: Mapping[str, Any]) -> None:
        """Apply statistics from the handshake retryer."""
        self.handshake_attempts = _coerce_snapshot_int(stats, "attempt_number", self.handshake_attempts)

    def stash_mqtt_message(self, message: QueuedPublish) -> bool:
        if self.mqtt_spool:
            try:
                self.mqtt_spool.append(message)
                self.mqtt_spooled_messages += 1
                self.metrics.mqtt_spooled_messages.inc()
                return True
            except Exception:
                self.metrics.mqtt_spool_errors.inc()
        return False

    async def flush_mqtt_spool(self) -> None:
        if self.mqtt_spool:
            while self.mqtt_publish_queue.qsize() < self.mqtt_queue_limit:
                msg = self.mqtt_spool.pop_next()
                if not msg:
                    break
                try:
                    self.mqtt_publish_queue.put_nowait(msg)
                    self.mqtt_spooled_replayed += 1
                except asyncio.QueueFull:
                    self.mqtt_spool.requeue(msg)
                    break

    def enqueue_console_chunk(self, chunk: bytes) -> bool:
        evt = self.console_to_mcu_queue.append(chunk)
        self.console_queue_bytes = self.console_to_mcu_queue.bytes_used
        if not evt.accepted:
            self.metrics.console_dropped_bytes.inc(len(chunk))
        return evt.accepted

    def pop_console_chunk(self) -> bytes | None:
        chunk = self.console_to_mcu_queue.popleft()
        self.console_queue_bytes = self.console_to_mcu_queue.bytes_used
        return chunk

    def requeue_console_chunk_front(self, chunk: bytes) -> None:
        self.console_to_mcu_queue.appendleft(chunk)
        self.console_queue_bytes = self.console_to_mcu_queue.bytes_used

    def enqueue_mailbox_message(self, payload: bytes, logger_: Any) -> bool:
        evt = self.mailbox_queue.append(payload)
        self.mailbox_queue_bytes = self.mailbox_queue.bytes_used
        return evt.accepted

    def enqueue_mailbox_incoming(self, payload: bytes, logger_: Any) -> bool:
        evt = self.mailbox_incoming_queue.append(payload)
        self.mailbox_incoming_queue_bytes = self.mailbox_incoming_queue.bytes_used
        return evt.accepted

    def pop_mailbox_message(self) -> bytes | None:
        if not self.mailbox_queue:
            return None
        msg = self.mailbox_queue.popleft()
        self.mailbox_queue_bytes = self.mailbox_queue.bytes_used
        return msg

    def pop_mailbox_incoming(self) -> bytes | None:
        if not self.mailbox_incoming_queue:
            return None
        msg = self.mailbox_incoming_queue.popleft()
        self.mailbox_incoming_queue_bytes = self.mailbox_incoming_queue.bytes_used
        return msg

    def requeue_mailbox_message_front(self, payload: bytes) -> None:
        self.mailbox_queue.appendleft(payload)
        self.mailbox_queue_bytes = self.mailbox_queue.bytes_used

    def build_metrics_snapshot(self) -> dict[str, Any]:
        uptime_val = getattr(self.metrics.uptime_seconds, "_value", 0)
        uptime_num = uptime_val.get() if hasattr(uptime_val, "get") else float(uptime_val)  # type: ignore
        return {
            "uptime_seconds": int(cast(float, uptime_num)),
            "is_connected": self.is_connected,
            "is_synchronized": self.is_synchronized,
            "mcu_paused": self.mcu_is_paused,
            "mqtt_queue_len": self.mqtt_publish_queue.qsize(),
            "mqtt_dropped_messages": self.mqtt_dropped_messages,
            "mqtt_spooled_messages": self.mqtt_spooled_messages,
            "mqtt_spool_degraded": self.mqtt_spool_degraded,
            "console_queue_bytes": self.console_queue_bytes,
            "console_dropped_bytes": self.console_dropped_bytes,
            "mailbox_queue_len": len(self.mailbox_queue),
            "mailbox_dropped_messages": self.mailbox_dropped_messages,
            "watchdog_beats": self.watchdog_beats,
            "system": collect_system_metrics(),
        }

    def build_handshake_snapshot(self) -> HandshakeSnapshot:
        return HandshakeSnapshot(
            synchronised=self.is_synchronized,
            attempts=self.handshake_attempts,
            successes=self.handshake_successes,
            failures=self.handshake_failures,
            last_error=self.last_handshake_error,
            last_duration=self.handshake_last_duration,
        )

    def build_bridge_snapshot(self) -> BridgeSnapshot:
        mcu_version = None
        if self.mcu_version:
            mcu_version = McuVersion(major=self.mcu_version[0], minor=self.mcu_version[1])
        return BridgeSnapshot(
            serial_link=SerialLinkSnapshot(
                connected=self.is_connected,
                synchronised=self.is_synchronized,
            ),
            handshake=self.build_handshake_snapshot(),
            serial_pipeline=SerialPipelineSnapshot(
                inflight=self.serial_pipeline_inflight,
                last_completion=self.serial_pipeline_last,
            ),
            serial_flow=msgspec.structs.asdict(self.serial_flow_stats.as_snapshot()),
            mcu_version=mcu_version,
            capabilities=self.mcu_capabilities.as_dict() if self.mcu_capabilities else None,
        )

    def record_supervisor_failure(self, name: str, backoff: float, exc: BaseException, fatal: bool = False) -> None:
        stats = self.supervisor_stats.get(name) or SupervisorStats()
        self.supervisor_stats[name] = stats
        stats.restarts += 1
        stats.last_failure_unix = time.time()
        stats.last_exception = f"{exc.__class__.__name__}: {exc}"
        stats.backoff_seconds = backoff
        stats.fatal = fatal

    def mark_supervisor_healthy(self, name: str) -> None:
        if name in self.supervisor_stats:
            self.supervisor_stats[name].backoff_seconds = 0.0
            self.supervisor_stats[name].fatal = False

    def initialize_spool(self) -> None:
        if self.mqtt_spool_dir and self.mqtt_spool_limit > 0:
            try:
                self.mqtt_spool = MQTTPublishSpool(self.mqtt_spool_dir, self.mqtt_spool_limit)
            except Exception:
                pass


def create_runtime_state(config: Any) -> RuntimeState:
    state = RuntimeState()

    if isinstance(config, dict):
        state.mqtt_queue_limit = config.get("mqtt_queue_limit", DEFAULT_MQTT_QUEUE_LIMIT)
        state.mqtt_spool_dir = config.get("mqtt_spool_dir", DEFAULT_MQTT_SPOOL_DIR)
        state.mqtt_spool_limit = state.mqtt_queue_limit * 4
        state.file_system_root = config.get("file_system_root", DEFAULT_FILE_SYSTEM_ROOT)
        state.file_write_max_bytes = config.get("file_write_max_bytes", DEFAULT_FILE_WRITE_MAX_BYTES)
        state.file_storage_quota_bytes = config.get("file_storage_quota_bytes", DEFAULT_FILE_STORAGE_QUOTA_BYTES)
        state.process_timeout = config.get("process_timeout", DEFAULT_PROCESS_TIMEOUT)
        state.process_output_limit = config.get("process_max_output_bytes", DEFAULT_PROCESS_MAX_OUTPUT_BYTES)
        state.process_max_concurrent = config.get("process_max_concurrent", DEFAULT_PROCESS_MAX_CONCURRENT)

        policy_data = config.get("allowed_policy")
        if isinstance(policy_data, AllowedCommandPolicy):
            state.allowed_policy = policy_data
        elif policy_data and isinstance(policy_data, dict) and "entries" in policy_data:
            state.allowed_policy = AllowedCommandPolicy(entries=tuple(policy_data["entries"]))
        else:
            state.allowed_policy = AllowedCommandPolicy(entries=())

        auth_data = config.get("topic_authorization")
        if isinstance(auth_data, TopicAuthorization):
            state.topic_authorization = auth_data
        elif auth_data and isinstance(auth_data, dict):
            state.topic_authorization = TopicAuthorization(**auth_data)
        else:
            state.topic_authorization = TopicAuthorization()
    else:
        state.mqtt_queue_limit = getattr(config, "mqtt_queue_limit", DEFAULT_MQTT_QUEUE_LIMIT)
        state.mqtt_spool_dir = getattr(config, "mqtt_spool_dir", DEFAULT_MQTT_SPOOL_DIR)
        state.mqtt_spool_limit = state.mqtt_queue_limit * 4
        state.file_system_root = getattr(config, "file_system_root", DEFAULT_FILE_SYSTEM_ROOT)
        state.file_write_max_bytes = getattr(config, "file_write_max_bytes", DEFAULT_FILE_WRITE_MAX_BYTES)
        state.file_storage_quota_bytes = getattr(config, "file_storage_quota_bytes", DEFAULT_FILE_STORAGE_QUOTA_BYTES)
        state.process_timeout = getattr(config, "process_timeout", DEFAULT_PROCESS_TIMEOUT)
        state.process_output_limit = getattr(config, "process_max_output_bytes", DEFAULT_PROCESS_MAX_OUTPUT_BYTES)
        state.process_max_concurrent = getattr(config, "process_max_concurrent", DEFAULT_PROCESS_MAX_CONCURRENT)
        state.allowed_policy = getattr(config, "allowed_policy", AllowedCommandPolicy(entries=()))
        state.topic_authorization = getattr(config, "topic_authorization", TopicAuthorization())

    state.initialize_spool()
    return state

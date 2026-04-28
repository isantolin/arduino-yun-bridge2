"""Runtime state container for the MCU Bridge daemon."""

from __future__ import annotations

import asyncio
import collections
import contextlib
import functools
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol, TypeVar, cast

import diskcache
import msgspec
import psutil
import structlog

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
)
from ..config.settings import RuntimeConfig
from ..protocol.structures import QueuedPublish
from ..mqtt.spool import MQTTPublishSpool
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

T = TypeVar("T")


class DequeLike(Protocol[T]):
    """Protocol for deque-like objects (collections.deque, diskcache.Deque)."""

    def append(self, __x: T) -> None: ...
    def appendleft(self, __x: T) -> None: ...
    def pop(self) -> T: ...
    def popleft(self) -> T: ...
    def clear(self) -> None: ...
    def __len__(self) -> int: ...
    def __getitem__(self, __i: int) -> T: ...
    def __setitem__(self, __i: int, __v: T) -> None: ...


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
    """Managed subprocess with direct stream access."""

    pid: int
    command: str = ""
    handle: asyncio.subprocess.Process | None = None
    exit_code: int | None = None
    io_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def is_drained(self) -> bool:
        """[SIL-2] Non-blocking EOF check using native library state."""
        if not self.handle:
            return True

        # Process is considered drained only if it's finished and IO is EOF
        if self.handle.returncode is None:
            return False

        out_eof = getattr(self.handle.stdout, "at_eof", lambda: True)()
        err_eof = getattr(self.handle.stderr, "at_eof", lambda: True)()
        return out_eof and err_eof


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
            root_disk = psutil.disk_usage("/")
            tmp_disk = None
            with contextlib.suppress(OSError):
                tmp_disk = psutil.disk_usage("/tmp")

            # [SIL-2] Direct mapping from native library structures
            result = {
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
            }

            if hasattr(psutil, "sensors_temperatures"):
                temps = psutil.sensors_temperatures()
                # [SIL-2] Functional lookup for first available thermal sensor.
                sensor_name = next(
                    filter(
                        lambda n: n in temps and temps[n],
                        ("cpu_thermal", "coretemp", "soc_thermal"),
                    ),
                    None,
                )
                if sensor_name:
                    result["temperature_celsius"] = temps[sensor_name][0].current

            if tmp_disk:
                result.update(
                    {
                        "disk_tmp_total_bytes": tmp_disk.total,
                        "disk_tmp_used_bytes": tmp_disk.used,
                        "disk_tmp_free_bytes": tmp_disk.free,
                        "disk_tmp_percent": tmp_disk.percent,
                    }
                )
            return result
    except (psutil.Error, RuntimeError, OSError):
        return {}


def _make_mqtt_queue() -> asyncio.Queue[QueuedPublish]:
    return asyncio.Queue[QueuedPublish]()


def _make_str_int_dict() -> dict[str, int]:
    return {}


def _make_snapshot_dict() -> SpoolSnapshot:
    return {}


def _make_bytes_deque() -> DequeLike[bytes]:
    return cast(DequeLike[bytes], collections.deque[bytes]())


def _make_int_process_dict() -> dict[int, ManagedProcess]:
    return {}


def _make_pin_request_deque() -> collections.deque[PendingPinRequest]:
    return collections.deque[PendingPinRequest]()


class RuntimeState(msgspec.Struct):
    """Aggregated mutable state shared across the daemon layers."""

    metrics: DaemonMetrics = msgspec.field(default_factory=DaemonMetrics)
    serial_writer: asyncio.BaseTransport | None = None
    state: str = "disconnected"

    @property
    def is_connected(self) -> bool:
        return self.state in {"connected", "synchronized"}

    @property
    def is_synchronized(self) -> bool:
        return self.state == "synchronized"

    def mark_transport_connected(self) -> None:
        """Signal that serial connection is open but unsynchronized."""
        self.state = "connected"
        self.metrics.link_state.state("connected")

    def mark_transport_disconnected(self) -> None:
        """Signal that serial connection is lost."""
        self.state = "disconnected"
        self.metrics.link_state.state("disconnected")
        if self.link_sync_event:
            self.link_sync_event.clear()

    def mark_synchronized(self) -> None:
        """Signal that protocol handshake is successfully completed."""
        self.state = "synchronized"
        self.metrics.link_state.state("synchronized")
        if self.link_sync_event:
            self.link_sync_event.set()

    mqtt_publish_queue: asyncio.Queue[QueuedPublish] = msgspec.field(
        default_factory=_make_mqtt_queue,
    )
    mqtt_queue_limit: int = DEFAULT_MQTT_QUEUE_LIMIT
    mqtt_drop_counts: dict[str, int] = msgspec.field(default_factory=_make_str_int_dict)
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
        default_factory=_make_snapshot_dict
    )
    datastore: dict[str, str] = msgspec.field(default_factory=dict)  # type: ignore

    # [SIL-2] Mailbox queues persist to /tmp through diskcache when enabled.
    mailbox_queue: DequeLike[bytes] = msgspec.field(
        default_factory=_make_bytes_deque,
    )
    mailbox_incoming_queue: DequeLike[bytes] = msgspec.field(
        default_factory=_make_bytes_deque,
    )

    _mailbox_queue_cache: diskcache.Cache | None = None
    _mailbox_incoming_queue_cache: diskcache.Cache | None = None

    mcu_is_paused: bool = False
    serial_tx_allowed: asyncio.Event = msgspec.field(default_factory=asyncio.Event)
    console_to_mcu_queue: DequeLike[bytes] = msgspec.field(
        default_factory=_make_bytes_deque,
    )
    console_queue_limit_bytes: int = DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES

    console_queue_bytes: int = 0
    console_dropped_chunks: int = 0
    console_truncated_chunks: int = 0
    running_processes: dict[int, ManagedProcess] = msgspec.field(
        default_factory=_make_int_process_dict
    )
    process_lock: asyncio.Lock = msgspec.field(default_factory=asyncio.Lock)
    next_pid: int = 1
    allowed_policy: AllowedCommandPolicy = msgspec.field(
        default_factory=AllowedCommandPolicy.create_empty,
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
        default_factory=_make_pin_request_deque,
    )
    pending_analog_reads: collections.deque[PendingPinRequest] = msgspec.field(
        default_factory=_make_pin_request_deque,
    )
    mailbox_incoming_topic: str = ""
    mailbox_queue_limit: int = DEFAULT_MAILBOX_QUEUE_LIMIT
    mailbox_queue_bytes_limit: int = DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT
    pending_pin_request_limit: int = DEFAULT_PENDING_PIN_REQUESTS
    mailbox_queue_bytes: int = 0
    mailbox_dropped_messages: int = 0
    mailbox_truncated_messages: int = 0
    mailbox_outgoing_overflow_events: int = 0
    mailbox_incoming_queue_bytes: int = 0
    mailbox_incoming_dropped_messages: int = 0
    mailbox_incoming_truncated_messages: int = 0
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
    rpc_latency_stats: SerialLatencyStats = msgspec.field(
        default_factory=SerialLatencyStats
    )
    serial_pipeline_inflight: dict[str, Any] | None = None
    serial_pipeline_last: dict[str, Any] | None = None
    process_output_limit: int = DEFAULT_PROCESS_MAX_OUTPUT_BYTES
    process_max_concurrent: int = DEFAULT_PROCESS_MAX_CONCURRENT
    unknown_command_count: int = 0
    unknown_command_last_id: int = 0
    config_source: str = "uci"
    serial_ack_timeout_ms: int = int(DEFAULT_SERIAL_RETRY_TIMEOUT * 1000)
    serial_response_timeout_ms: int = int(DEFAULT_SERIAL_RESPONSE_TIMEOUT * 1000)
    serial_retry_limit: int = DEFAULT_RETRY_LIMIT
    mcu_status_counts: dict[str, int] = msgspec.field(default_factory=lambda: {})
    supervisor_stats: dict[str, SupervisorStats] = msgspec.field(
        default_factory=lambda: {}
    )
    supervisor_failures: int = 0
    last_supervisor_error: str | None = None

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

    def record_supervisor_failure(
        self, name: str, backoff: float, exc: BaseException | None
    ) -> None:
        """Record an internal service task failure."""
        stats = self.supervisor_stats.setdefault(name, SupervisorStats())
        stats.restarts += 1
        stats.last_failure_unix = time.time()
        stats.last_exception = f"{exc.__class__.__name__}: {exc}" if exc else "unknown"
        stats.backoff_seconds = backoff

        self.supervisor_failures += 1
        self.metrics.supervisor_failures.labels(task=name).inc()
        self.last_supervisor_error = f"{name}: {exc}" if exc else f"{name}: unknown"
        logger.warning(
            "Supervisor task '%s' failed. Backoff: %.2fs. Error: %s",
            name,
            backoff,
            exc,
        )

    def configure(self, config: RuntimeConfig) -> None:
        # [SIL-2] Close existing persistent queues if they are being replaced
        # to ensure that resources (like diskcache files) are released.
        if self._mailbox_queue_cache:
            self._mailbox_queue_cache.close()  # type: ignore
            self._mailbox_queue_cache = None
        if self._mailbox_incoming_queue_cache:
            self._mailbox_incoming_queue_cache.close()  # type: ignore
            self._mailbox_incoming_queue_cache = None

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

        self.console_to_mcu_queue = cast(
            DequeLike[bytes],
            collections.deque[bytes](maxlen=self.mailbox_queue_limit),
        )

        def _create_spool(
            subdir: str,
        ) -> tuple[DequeLike[bytes], diskcache.Cache | None]:
            directory = None
            if self.allow_non_tmp_paths or self.file_system_root.startswith("/tmp/"):
                directory = Path(self.file_system_root) / subdir

            if directory:
                try:
                    directory.mkdir(parents=True, exist_ok=True)
                    cache = diskcache.Cache(str(directory))
                    return (
                        cast(
                            DequeLike[bytes],
                            diskcache.Deque.fromcache(cache),  # type: ignore
                        ),
                        cache,
                    )
                except (OSError, RuntimeError, sqlite3.Error):
                    logger.warning("Spool '%s' falling back to RAM", subdir)

            return (
                cast(
                    DequeLike[bytes],
                    collections.deque[bytes](maxlen=self.mailbox_queue_limit),
                ),
                None,
            )

        self.mailbox_queue, self._mailbox_queue_cache = _create_spool("mailbox_out")
        self.mailbox_incoming_queue, self._mailbox_incoming_queue_cache = _create_spool(
            "mailbox_in"
        )

    def mark_supervisor_healthy(self, name: str) -> None:
        """Reset backoff status for a healthy supervisor."""
        stats = self.supervisor_stats.get(name)
        if stats:
            stats.backoff_seconds = 0.0
            stats.fatal = False

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

            if "duration" in payload:
                duration_val = float(cast(float, payload["duration"]))
                self.metrics.serial_latency_ms.observe(duration_val * 1000.0)

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

    def build_metrics_snapshot(self) -> dict[str, Any]:
        # [SIL-2] Return rich objects where possible to preserve attribute-based API
        return {
            "serial": self.serial_flow_stats,
            "serial_throughput": self.serial_throughput_stats,
            "serial_latency": self.serial_latency_stats.as_dict(),
            "mqtt_drop_counts": dict(self.mqtt_drop_counts),
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
                msgspec.structs.asdict(self.mcu_capabilities)
                if self.mcu_capabilities
                else None
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
        if self._mailbox_queue_cache:
            with _sup:
                self._mailbox_queue_cache.close()  # type: ignore
            self._mailbox_queue_cache = None

        if self._mailbox_incoming_queue_cache:
            with _sup:
                self._mailbox_incoming_queue_cache.close()  # type: ignore
            self._mailbox_incoming_queue_cache = None

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

    state = RuntimeState(
        mqtt_publish_queue=asyncio.Queue(cfg.mqtt_queue_limit),
        serial_tx_allowed=asyncio.Event(),
        process_lock=asyncio.Lock(),
        link_sync_event=asyncio.Event(),
    )
    state.serial_tx_allowed.set()
    state.configure(cfg)

    from ..transport.mqtt import MqttTransport

    transport = MqttTransport(cfg, state)
    transport.configure_spool(cfg.mqtt_spool_dir, cfg.mqtt_queue_limit * 4)
    if initialize_spool:
        transport.initialize_spool()

    return state

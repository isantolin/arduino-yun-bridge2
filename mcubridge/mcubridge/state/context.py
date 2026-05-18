"""Runtime state container for the MCU Bridge daemon."""

from __future__ import annotations

import asyncio
import collections
import contextlib
import sqlite3
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final, TypeVar, cast

import diskcache
import msgspec
import structlog

from ..config.const import (
    DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES,
    DEFAULT_FILE_STORAGE_QUOTA_BYTES,
    DEFAULT_FILE_SYSTEM_ROOT,
    DEFAULT_FILE_WRITE_MAX_BYTES,
    DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT,
    DEFAULT_MAILBOX_QUEUE_LIMIT,
    DEFAULT_MQTT_QUEUE_LIMIT,
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
    SerialLinkSnapshot,
    SerialPipelineSnapshot,
    SerialThroughputStats,
    SupervisorStats,
    PipelineEvent,
)
from .metrics import DaemonMetrics

T = TypeVar("T")

logger = structlog.get_logger("mcubridge.state")

SpoolSnapshot = dict[str, int | float]


__all__: Final[tuple[str, ...]] = (
    "McuCapabilities",
    "RuntimeState",
    "PendingPinRequest",
    "create_runtime_state",
    "HandshakeSnapshot",
    "SerialLinkSnapshot",
    "McuVersion",
    "SerialPipelineSnapshot",
    "BridgeSnapshot",
    "Status",
)


def collect_system_metrics() -> dict[str, Any]:
    """Collect system-level metrics using native library conversions."""
    return {}


class RuntimeState(msgspec.Struct, weakref=True):
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
        default_factory=lambda: cast(asyncio.Queue[QueuedPublish], asyncio.Queue())
    )
    mqtt_queue_limit: int = DEFAULT_MQTT_QUEUE_LIMIT
    mqtt_drop_counts: dict[str, int] = msgspec.field(default_factory=lambda: cast(dict[str, int], {}))
    allow_non_tmp_paths: bool = False
    datastore_cache: diskcache.Cache | None = None

    # [SIL-2] Mailbox queues persist to /tmp through diskcache when enabled.
    mailbox_queue: Any = msgspec.field(
        default_factory=lambda: cast(Any, collections.deque()),
    )
    mailbox_incoming_queue: Any = msgspec.field(
        default_factory=lambda: cast(Any, collections.deque()),
    )

    _mailbox_queue_cache: diskcache.Cache | None = None
    _mailbox_incoming_queue_cache: diskcache.Cache | None = None

    mcu_is_paused: bool = False
    serial_tx_allowed: asyncio.Event = msgspec.field(default_factory=asyncio.Event)
    console_to_mcu_queue: Any = msgspec.field(
        default_factory=lambda: cast(Any, collections.deque()),
    )
    console_queue_limit_bytes: int = DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES

    console_queue_bytes: int = 0
    console_dropped_chunks: int = 0
    console_truncated_chunks: int = 0
    running_processes: dict[int, asyncio.subprocess.Process] = msgspec.field(
        default_factory=lambda: cast(dict[int, asyncio.subprocess.Process], {})
    )
    process_io_locks: dict[int, asyncio.Lock] = msgspec.field(default_factory=lambda: cast(dict[int, asyncio.Lock], {}))
    process_exit_codes: dict[int, int] = msgspec.field(default_factory=lambda: cast(dict[int, int], {}))
    process_lock: asyncio.Lock = msgspec.field(default_factory=asyncio.Lock)
    next_pid: int = 1
    allowed_policy: AllowedCommandPolicy = msgspec.field(
        default_factory=AllowedCommandPolicy,
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
        default_factory=lambda: cast(collections.deque[PendingPinRequest], collections.deque()),
    )
    pending_analog_reads: collections.deque[PendingPinRequest] = msgspec.field(
        default_factory=lambda: cast(collections.deque[PendingPinRequest], collections.deque()),
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
    link_session_key: bytes | None = None
    link_aead_cipher: Any | None = None
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
    handshake_last_started: float = 0.0
    serial_flow_stats: SerialFlowStats = msgspec.field(default_factory=SerialFlowStats)
    serial_throughput_stats: SerialThroughputStats = msgspec.field(default_factory=SerialThroughputStats)
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
    mcu_status_counts: dict[str, int] = msgspec.field(default_factory=lambda: cast(dict[str, int], {}))
    supervisor_stats: dict[str, SupervisorStats] = msgspec.field(
        default_factory=lambda: cast(dict[str, SupervisorStats], {})
    )
    supervisor_failures: int = 0
    last_supervisor_error: str | None = None

    # Fields read by business logic / tests (Prometheus counters are the export source of truth)
    mqtt_dropped_messages: int = 0
    serial_decode_errors: int = 0
    handshake_attempts: int = 0
    handshake_successes: int = 0
    watchdog_beats: int = 0

    # Spool metrics
    mqtt_spool_corrupt_dropped: int = 0
    mqtt_spool_dropped_limit: int = 0
    mqtt_spool_trim_events: int = 0
    mqtt_spool_last_trim_unix: float = 0.0

    @property
    def handshake_failures(self) -> int:
        """Total handshake failures (Calculated)."""
        return self.handshake_attempts - self.handshake_successes

    @property
    def allowed_commands(self) -> tuple[str, ...]:
        """Return the current allowed command list from policy."""
        return self.allowed_policy.entries

    def record_supervisor_failure(self, name: str, backoff: float, exc: BaseException | None) -> None:
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

    def configure(self) -> None:
        _sup = contextlib.suppress(OSError, RuntimeError, AttributeError)

        # [SIL-2] Resource Lifecycle: Close persistent queues before replacement.
        if self.datastore_cache is not None:
            with _sup:
                cast(Any, self.datastore_cache).close()
            self.datastore_cache = None

        if self._mailbox_queue_cache is not None:
            cast(Any, self._mailbox_queue_cache).close()
            self._mailbox_queue_cache = None
        if self._mailbox_incoming_queue_cache is not None:
            cast(Any, self._mailbox_incoming_queue_cache).close()
            self._mailbox_incoming_queue_cache = None

        # Re-initialize transient queues
        self.console_to_mcu_queue = collections.deque[bytes](maxlen=self.mailbox_queue_limit)

        def _create_spool(
            subdir: str,
        ) -> tuple[Any, diskcache.Cache | None]:
            directory = None
            if self.allow_non_tmp_paths or self.file_system_root.startswith("/tmp/"):
                directory = Path(self.file_system_root) / subdir

            if directory:
                cache = None
                try:
                    directory.mkdir(parents=True, exist_ok=True)
                    cache = diskcache.Cache(str(directory))
                    dc_class: Any = diskcache.Deque
                    dq: Any = dc_class.fromcache(cache)
                    return (
                        dq,
                        cache,
                    )
                except (OSError, RuntimeError, sqlite3.Error):
                    if cache is not None:
                        cast(Any, cache).close()
                    logger.warning("Spool '%s' falling back to RAM", subdir)

            return (
                cast(
                    Any,
                    collections.deque[bytes](maxlen=self.mailbox_queue_limit),
                ),
                None,
            )

        self.mailbox_queue, self._mailbox_queue_cache = _create_spool("mailbox_out")
        self.mailbox_incoming_queue, self._mailbox_incoming_queue_cache = _create_spool("mailbox_in")

        # [SIL-2] Initialize datastore with diskcache for ACID persistence
        ds_dir = None
        if self.allow_non_tmp_paths or self.file_system_root.startswith("/tmp/"):
            ds_dir = Path(self.file_system_root) / "datastore"

        if ds_dir:
            try:
                ds_dir.mkdir(parents=True, exist_ok=True)
                self.datastore_cache = diskcache.Cache(str(ds_dir), size_limit=1024 * 1024)
            except (OSError, RuntimeError) as e:
                logger.warning("Could not initialize datastore diskcache: %s", e)

    def mark_supervisor_healthy(self, name: str) -> None:
        """Reset backoff status for a healthy supervisor."""
        stats = self.supervisor_stats.get(name)
        if stats:
            stats.backoff_seconds = 0.0
            stats.fatal = False

    def record_serial_pipeline_event(self, event: PipelineEvent) -> None:
        name = event.event
        command_id = event.command_id
        attempt = event.attempt
        timestamp = event.timestamp
        acked = event.ack_received
        status_code = event.status

        # [SIL-2] Direct Enum resolution to avoid wrapper overhead
        def _res_cmd(cid: int) -> str:
            try:
                return Command(cid).name
            except ValueError:
                try:
                    return Status(cid).name
                except ValueError:
                    return f"0x{cid:02X}"

        if name == "start":
            # [SIL-2] Unified metrics increment
            self.serial_flow_stats.commands_sent += 1
            self.serial_flow_stats.last_event_unix = timestamp

            self.serial_pipeline_inflight = {
                "command_id": command_id,
                "command_name": _res_cmd(command_id),
                "attempt": attempt,
                "started_unix": timestamp,
                "acknowledged": False,
                "last_event": "start",
                "last_event_unix": timestamp,
            }
            return

        inf = self.serial_pipeline_inflight
        if name == "ack" and inf:
            # [SIL-2] Unified metrics increment
            self.serial_flow_stats.commands_acked += 1
            self.serial_flow_stats.last_event_unix = timestamp

            inf.update(
                {
                    "acknowledged": True,
                    "ack_unix": timestamp,
                    "last_event": "ack",
                    "last_event_unix": timestamp,
                }
            )
            return

        if name == "retry":
            self.serial_flow_stats.retries += 1
            self.metrics.serial_retries.inc()
            self.serial_flow_stats.last_event_unix = timestamp

        if name in {"success", "failure", "abandoned"}:
            if name in {"failure", "abandoned"}:
                self.serial_flow_stats.failures += 1
                self.metrics.serial_failures.inc()
                self.serial_flow_stats.last_event_unix = timestamp

            # [SIL-2] Direct Status resolution
            s_name = "unknown"
            if status_code is not None:
                try:
                    s_name = Status(status_code).name
                except ValueError:
                    s_name = f"0x{status_code:02X}"

            payload = {
                "command_id": command_id,
                "command_name": _res_cmd(command_id),
                "attempt": attempt,
                "event": name,
                "completed_unix": timestamp,
                "status_code": status_code,
                "status_name": s_name,
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
            self.mqtt_spool_corrupt_dropped = msgspec.convert(observation["corrupt_dropped"], int)
        if "dropped_due_to_limit" in observation:
            self.mqtt_spool_dropped_limit = msgspec.convert(observation["dropped_due_to_limit"], int)
        if "trim_events" in observation:
            self.mqtt_spool_trim_events = msgspec.convert(observation["trim_events"], int)
        if "last_trim_unix" in observation:
            self.mqtt_spool_last_trim_unix = msgspec.convert(observation["last_trim_unix"], float)

    def build_metrics_snapshot(self) -> dict[str, Any]:
        # [SIL-2] Return rich objects where possible to preserve attribute-based API
        return {
            "serial": self.serial_flow_stats,
            "serial_throughput": self.serial_throughput_stats,
            "mqtt_drop_counts": dict(self.mqtt_drop_counts),
            "queue_depths": {
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
        # [SIL-2] Atomic field extraction from self to avoid manual boilerplate.
        # We leverage the fact that most fields match by name and type.
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
        # [SIL-2] Converge disparate state metrics into a unified snapshot structure
        # using msgspec for guaranteed validation and performance.
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
            capabilities=(msgspec.structs.asdict(self.mcu_capabilities) if self.mcu_capabilities else None),
        )

    def handshake_duration_since_start(self) -> float:
        if self.handshake_last_started <= 0.0:
            return 0.0
        return max(0.0, time.monotonic() - self.handshake_last_started)

    def __del__(self) -> None:
        """Last-resort cleanup to prevent ResourceWarning from unclosed diskcache connections."""
        self.cleanup()

    def cleanup(self) -> None:
        _sup = contextlib.suppress(OSError, RuntimeError, AttributeError)

        # [SIL-2] Aggressive Resource Eradication to prevent ResourceWarnings.
        # 1. Nullify high-level wrappers first to drop references to the underlying caches.
        self.mailbox_queue = collections.deque()
        self.mailbox_incoming_queue = collections.deque()
        self.console_to_mcu_queue = collections.deque()

        # 2. Explicitly close and nullify persistent caches.
        if self.datastore_cache is not None:
            with _sup:
                self.datastore_cache.close()
            self.datastore_cache = None

        if self._mailbox_queue_cache is not None:
            with _sup:
                self._mailbox_queue_cache.close()
            self._mailbox_queue_cache = None

        if self._mailbox_incoming_queue_cache is not None:
            with _sup:
                self._mailbox_incoming_queue_cache.close()
            self._mailbox_incoming_queue_cache = None

        # 3. Drain and reset the MQTT queue.
        while not self.mqtt_publish_queue.empty():
            with _sup:
                self.mqtt_publish_queue.get_nowait()
        self.mqtt_publish_queue = asyncio.Queue()

        # 4. Terminate all running processes to release pipes/sockets.
        if self.running_processes:
            for handle in list(self.running_processes.values()):
                if handle:
                    with contextlib.suppress(OSError, ProcessLookupError):
                        handle.terminate()
            self.running_processes.clear()

        # 5. Clear other complex objects and state indicators.
        with _sup:
            self.process_io_locks.clear()
            self.process_exit_codes.clear()
            self.serial_tx_allowed.clear()
            self.link_sync_event.clear()
            self.pending_digital_reads.clear()
            self.pending_analog_reads.clear()

        # 6. Final GC hint to ensure SQLite connections are truly closed.
        import gc

        gc.collect()


def create_runtime_state(config: RuntimeConfig | dict[str, Any]) -> RuntimeState:
    from ..config.settings import RuntimeConfig

    cfg = msgspec.convert(config, RuntimeConfig) if isinstance(config, dict) else config

    cfg_dict = {k: v for k, v in msgspec.structs.asdict(cfg).items() if v is not None}
    if "mqtt_topic" in cfg_dict:
        cfg_dict["mqtt_topic_prefix"] = cfg_dict.pop("mqtt_topic")
    if "process_max_output_bytes" in cfg_dict:
        cfg_dict["process_output_limit"] = cfg_dict.pop("process_max_output_bytes")
    if "allowed_commands" in cfg_dict and cfg_dict["allowed_commands"] is not None:
        from ..protocol.structures import AllowedCommandPolicy

        cfg_dict["allowed_policy"] = AllowedCommandPolicy(entries=cfg_dict.pop("allowed_commands"))

    state = msgspec.convert(cfg_dict, RuntimeState, strict=False)
    state.serial_tx_allowed.set()
    state.configure()

    return state

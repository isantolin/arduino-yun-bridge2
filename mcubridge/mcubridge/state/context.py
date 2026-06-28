"""Runtime state container for the MCU Bridge daemon."""

from __future__ import annotations

import asyncio
import collections
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final, TypeVar, cast

from .storage import SqliteDeque, SqliteCache, InMemoryDeque
import structlog

from ..config.const import (
    DEFAULT_FILE_STORAGE_QUOTA_BYTES,
    DEFAULT_FILE_SYSTEM_ROOT,
    DEFAULT_FILE_WRITE_MAX_BYTES,
    DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT,
    DEFAULT_MAILBOX_QUEUE_LIMIT,
    DEFAULT_MQTT_QUEUE_LIMIT,
    DEFAULT_PENDING_PIN_REQUESTS,
    DEFAULT_PROCESS_MAX_CONCURRENT,
    DEFAULT_PROCESS_TIMEOUT,
    DEFAULT_SERIAL_RESPONSE_TIMEOUT,
    DEFAULT_SERIAL_RETRY_TIMEOUT,
    DEFAULT_WATCHDOG_INTERVAL,
)
from ..config.settings import RuntimeConfig
from ..protocol import protocol
from ..protocol.protocol import (
    DEFAULT_RETRY_LIMIT,
    Status,
)
from ..protocol.structures import (
    PendingPinRequest,
    create_allowed_policy,
)
from ..protocol import mcubridge_pb2 as pb
from .metrics import DaemonMetrics

T = TypeVar("T")

logger = structlog.get_logger("mcubridge.state")

SpoolSnapshot = dict[str, int | float]


def _make_mqtt_publish_queue(maxsize: int = 0) -> asyncio.Queue[pb.MqttQueuedPublish]:
    normalized = max(0, int(maxsize))
    return cast(asyncio.Queue[pb.MqttQueuedPublish], asyncio.Queue(maxsize=normalized))


__all__: Final[tuple[str, ...]] = (
    "RuntimeState",
    "PendingPinRequest",
    "create_runtime_state",
    "Status",
)


class ProcessContext:
    __slots__ = ("handle", "io_lock", "exit_code")

    def __init__(self, handle: asyncio.subprocess.Process) -> None:
        self.handle = handle
        self.io_lock = asyncio.Lock()
        self.exit_code = 0


class RuntimeState:
    """Aggregated mutable state shared across the daemon layers. [SIL-2]"""

    def __init__(self, **kwargs: Any) -> None:
        self.metrics: DaemonMetrics = kwargs.get("metrics") or DaemonMetrics()
        self.serial_writer: asyncio.BaseTransport | None = kwargs.get("serial_writer")
        self.state: str = kwargs.get("state", "disconnected")

        self.mqtt_queue_limit: int = kwargs.get("mqtt_queue_limit", DEFAULT_MQTT_QUEUE_LIMIT)
        self.mqtt_publish_queue: asyncio.Queue[pb.MqttQueuedPublish] = kwargs.get(
            "mqtt_publish_queue"
        ) or _make_mqtt_publish_queue(self.mqtt_queue_limit)
        self.mqtt_drop_counts: dict[str, int] = kwargs.get("mqtt_drop_counts") or {}
        self.allow_non_tmp_paths: bool = kwargs.get("allow_non_tmp_paths", False)
        self.datastore_cache: SqliteCache | None = kwargs.get("datastore_cache")

        self.mailbox_queue: SqliteDeque | InMemoryDeque = kwargs.get("mailbox_queue") or InMemoryDeque()
        self.mailbox_incoming_queue: SqliteDeque | InMemoryDeque = (
            kwargs.get("mailbox_incoming_queue") or InMemoryDeque()
        )

        self.mcu_is_paused: bool = kwargs.get("mcu_is_paused", False)
        self.serial_tx_allowed: asyncio.Event = kwargs.get("serial_tx_allowed") or asyncio.Event()
        self.console_to_mcu_queue: collections.deque[bytes] = kwargs.get("console_to_mcu_queue") or collections.deque()
        self.console_queue_limit_bytes: int = kwargs.get(
            "console_queue_limit_bytes", protocol.DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES
        )

        self.console_queue_bytes: int = kwargs.get("console_queue_bytes", 0)
        self.console_dropped_chunks: int = kwargs.get("console_dropped_chunks", 0)
        self.console_truncated_chunks: int = kwargs.get("console_truncated_chunks", 0)

        self.running_processes: dict[int, ProcessContext] = kwargs.get("running_processes") or {}
        self.process_lock: asyncio.Lock = kwargs.get("process_lock") or asyncio.Lock()
        self.next_pid: int = kwargs.get("next_pid", 1)
        self.allowed_policy: pb.AllowedCommandPolicy = kwargs.get("allowed_policy") or create_allowed_policy([])
        self.topic_authorization: pb.TopicAuthorization | None = kwargs.get("topic_authorization")
        self.process_timeout: int = kwargs.get("process_timeout", DEFAULT_PROCESS_TIMEOUT)
        self.file_system_root: str = kwargs.get("file_system_root", DEFAULT_FILE_SYSTEM_ROOT)
        self.file_write_max_bytes: int = kwargs.get("file_write_max_bytes", DEFAULT_FILE_WRITE_MAX_BYTES)
        self.file_storage_quota_bytes: int = kwargs.get("file_storage_quota_bytes", DEFAULT_FILE_STORAGE_QUOTA_BYTES)
        self.file_storage_bytes_used: int = kwargs.get("file_storage_bytes_used", 0)
        self.file_write_limit_rejections: int = kwargs.get("file_write_limit_rejections", 0)
        self.file_storage_limit_rejections: int = kwargs.get("file_storage_limit_rejections", 0)
        self.mqtt_topic_prefix: str = kwargs.get("mqtt_topic_prefix", protocol.MQTT_DEFAULT_TOPIC_PREFIX)
        self.watchdog_enabled: bool = kwargs.get("watchdog_enabled", False)
        self.watchdog_interval: float = kwargs.get("watchdog_interval", DEFAULT_WATCHDOG_INTERVAL)
        self.last_watchdog_beat: float = kwargs.get("last_watchdog_beat", 0.0)

        self.pending_digital_reads: collections.deque[PendingPinRequest] = (
            kwargs.get("pending_digital_reads") or collections.deque()
        )
        self.pending_analog_reads: collections.deque[PendingPinRequest] = (
            kwargs.get("pending_analog_reads") or collections.deque()
        )

        self.mailbox_queue_limit: int = kwargs.get("mailbox_queue_limit", DEFAULT_MAILBOX_QUEUE_LIMIT)
        self.mailbox_queue_bytes_limit: int = kwargs.get("mailbox_queue_bytes_limit", DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT)
        self.pending_pin_request_limit: int = kwargs.get("pending_pin_request_limit", DEFAULT_PENDING_PIN_REQUESTS)
        self.mailbox_queue_bytes: int = kwargs.get("mailbox_queue_bytes", 0)
        self.mailbox_dropped_messages: int = kwargs.get("mailbox_dropped_messages", 0)
        self.mailbox_truncated_messages: int = kwargs.get("mailbox_truncated_messages", 0)

        self.mailbox_incoming_queue_bytes: int = kwargs.get("mailbox_incoming_queue_bytes", 0)
        self.mailbox_incoming_dropped_messages: int = kwargs.get("mailbox_incoming_dropped_messages", 0)
        self.mailbox_incoming_truncated_messages: int = kwargs.get("mailbox_incoming_truncated_messages", 0)

        self.mcu_version: tuple[int, int, int] | None = kwargs.get("mcu_version")
        self.mcu_capabilities: dict[str, Any] | None = kwargs.get("mcu_capabilities")
        self.link_handshake_nonce: bytes | None = kwargs.get("link_handshake_nonce")
        self.link_sync_event: asyncio.Event = kwargs.get("link_sync_event") or asyncio.Event()
        self.link_expected_tag: bytes | None = kwargs.get("link_expected_tag")
        self.link_session_key: bytes | None = kwargs.get("link_session_key")
        self.link_aead_cipher: Any | None = kwargs.get("link_aead_cipher")
        self.link_nonce_length: int = kwargs.get("link_nonce_length", 0)
        self.link_nonce_counter: int = kwargs.get("link_nonce_counter", 0)
        self.link_last_nonce_counter: int = kwargs.get("link_last_nonce_counter", 0)
        self.handshake_failure_streak: int = kwargs.get("handshake_failure_streak", 0)
        self.handshake_backoff_until: float = kwargs.get("handshake_backoff_until", 0.0)
        self.handshake_rate_until: float = kwargs.get("handshake_rate_until", 0.0)
        self.last_handshake_error: str | None = kwargs.get("last_handshake_error")
        self.last_handshake_unix: float = kwargs.get("last_handshake_unix", 0.0)
        self.handshake_last_duration: float = kwargs.get("handshake_last_duration", 0.0)
        self.handshake_fatal_count: int = kwargs.get("handshake_fatal_count", 0)
        self.handshake_fatal_reason: str | None = kwargs.get("handshake_fatal_reason")
        self.handshake_fatal_detail: str | None = kwargs.get("handshake_fatal_detail")
        self.handshake_fatal_unix: float = kwargs.get("handshake_fatal_unix", 0.0)
        self.handshake_last_started: float = kwargs.get("handshake_last_started", 0.0)
        self.serial_flow_stats: pb.SerialFlowSnapshot = kwargs.get("serial_flow_stats") or pb.SerialFlowSnapshot()
        self.serial_throughput_stats: pb.SerialThroughputStats = (
            kwargs.get("serial_throughput_stats") or pb.SerialThroughputStats()
        )
        self.serial_pipeline_inflight: dict[str, Any] | None = kwargs.get("serial_pipeline_inflight")
        self.serial_pipeline_last: dict[str, Any] | None = kwargs.get("serial_pipeline_last")
        self.process_output_limit: int = kwargs.get("process_output_limit", protocol.DEFAULT_PROCESS_MAX_OUTPUT_BYTES)
        self.process_max_concurrent: int = kwargs.get("process_max_concurrent", DEFAULT_PROCESS_MAX_CONCURRENT)
        self.unknown_command_count: int = kwargs.get("unknown_command_count", 0)
        self.unknown_command_last_id: int = kwargs.get("unknown_command_last_id", 0)
        self.config_source: str = kwargs.get("config_source", "uci")
        self.serial_ack_timeout_ms: int = kwargs.get("serial_ack_timeout_ms", int(DEFAULT_SERIAL_RETRY_TIMEOUT * 1000))
        self.serial_response_timeout_ms: int = kwargs.get(
            "serial_response_timeout_ms", int(DEFAULT_SERIAL_RESPONSE_TIMEOUT * 1000)
        )
        self.serial_retry_limit: int = kwargs.get("serial_retry_limit", DEFAULT_RETRY_LIMIT)
        self.mcu_status_counts: dict[str, int] = kwargs.get("mcu_status_counts") or {}
        self.supervisor_stats: dict[str, pb.SupervisorSnapshot] = kwargs.get("supervisor_stats") or {}
        self.supervisor_failures: int = kwargs.get("supervisor_failures", 0)
        self.last_supervisor_error: str | None = kwargs.get("last_supervisor_error")

        self.mqtt_dropped_messages: int = kwargs.get("mqtt_dropped_messages", 0)
        self.serial_decode_errors: int = kwargs.get("serial_decode_errors", 0)
        self.handshake_attempts: int = kwargs.get("handshake_attempts", 0)
        self.handshake_successes: int = kwargs.get("handshake_successes", 0)
        self.watchdog_beats: int = kwargs.get("watchdog_beats", 0)

        self.mqtt_spool_corrupt_dropped: int = kwargs.get("mqtt_spool_corrupt_dropped", 0)
        self.mqtt_spool_dropped_limit: int = kwargs.get("mqtt_spool_dropped_limit", 0)
        self.mqtt_spool_trim_events: int = kwargs.get("mqtt_spool_trim_events", 0)
        self.mqtt_spool_last_trim_unix: float = kwargs.get("mqtt_spool_last_trim_unix", 0.0)
        self.mqtt_spool_degraded: bool = kwargs.get("mqtt_spool_degraded", False)
        self.mqtt_spool_failure_reason: str | None = kwargs.get("mqtt_spool_failure_reason")
        self.mqtt_spool_pending_messages: int = kwargs.get("mqtt_spool_pending_messages", 0)

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

    @property
    def handshake_failures(self) -> int:
        """Total handshake failures (Calculated)."""
        return self.handshake_attempts - self.handshake_successes

    @property
    def allowed_commands(self) -> tuple[str, ...]:
        """Return the current allowed command list from policy."""
        return tuple(self.allowed_policy.entries)

    def record_supervisor_failure(self, name: str, backoff: float, exc: BaseException | None) -> None:
        """Record an internal service task failure."""
        stats = self.supervisor_stats.setdefault(name, pb.SupervisorSnapshot())
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

    def mark_supervisor_healthy(self, name: str) -> None:
        """Mark a task supervisor healthy, resetting its backoff. [SIL-2]"""
        if name in self.supervisor_stats:
            self.supervisor_stats[name].backoff_seconds = 0.0

    def mailbox_queue_depth(self) -> int:
        return int(len(self.mailbox_queue))

    def mailbox_incoming_queue_depth(self) -> int:
        return int(len(self.mailbox_incoming_queue))

    def configure(self) -> None:
        def _safe_close(resource: Any) -> None:
            try:
                if hasattr(resource, "close"):
                    res = resource.close()
                    if asyncio.iscoroutine(res):
                        try:
                            res.send(None)
                        except StopIteration:
                            pass
            except (OSError, RuntimeError, AttributeError) as e:
                logger.debug("Resource closure notice during reconfiguration", error=e)

        _safe_close(self.mailbox_queue)
        _safe_close(self.mailbox_incoming_queue)

        # [SIL-2] Resource Lifecycle: Close persistent queues before replacement.
        if self.datastore_cache is not None:
            _safe_close(self.datastore_cache)
            self.datastore_cache = None

        # Re-initialize transient queues
        self.mqtt_publish_queue = _make_mqtt_publish_queue(self.mqtt_queue_limit)
        self.console_to_mcu_queue = collections.deque[bytes](maxlen=self.mailbox_queue_limit)

        def _create_spool(
            subdir: str,
        ) -> Any:
            directory = None
            if self.allow_non_tmp_paths or self.file_system_root.startswith("/tmp/"):
                directory = Path(self.file_system_root) / subdir

            if directory and self.file_system_root:
                try:
                    directory.mkdir(parents=True, exist_ok=True)
                    return SqliteDeque(path=str(directory / "spool.db"), maxlen=self.mailbox_queue_limit)
                except (OSError, RuntimeError):
                    logger.warning("Spool '%s' falling back to RAM", subdir)

            return InMemoryDeque(maxlen=self.mailbox_queue_limit)

        self.mailbox_queue = _create_spool("mailbox_out")
        self.mailbox_incoming_queue = _create_spool("mailbox_in")

        # [SIL-2] Initialize datastore with dbm for ACID persistence
        ds_dir = None
        if self.allow_non_tmp_paths or self.file_system_root.startswith("/tmp/"):
            ds_dir = Path(self.file_system_root) / "datastore"

        if ds_dir and self.file_system_root:
            try:
                ds_dir.mkdir(parents=True, exist_ok=True)
                self.datastore_cache = SqliteCache(str(ds_dir / "data.db"))
            except (OSError, RuntimeError):
                logger.warning("Datastore falling back to RAM cache")
                self.datastore_cache = None

    def build_serial_pipeline_snapshot(self) -> pb.SerialPipelineSnapshot:
        def _dict_topb_obj_pipeline_event(ev_dict: dict[str, Any] | None) -> pb.PipelineEvent:
            if not ev_dict:
                return pb.PipelineEvent(event="none")
            return pb.PipelineEvent(
                event=str(ev_dict.get("event", "none")),
                command_id=int(ev_dict.get("command_id", 0)),
                attempt=int(ev_dict.get("attempt", 0)),
                ack_received=bool(ev_dict.get("ack_received", False)),
                status=int(ev_dict.get("status", 0)),
                timestamp=float(ev_dict.get("timestamp", 0.0)),
            )

        inflightpb_obj = pb.PipelineEvent(event="none")
        if self.serial_pipeline_inflight is not None:
            inflightpb_obj = _dict_topb_obj_pipeline_event(self.serial_pipeline_inflight)

        lastpb_obj = pb.PipelineEvent(event="none")
        if self.serial_pipeline_last is not None:
            lastpb_obj = _dict_topb_obj_pipeline_event(self.serial_pipeline_last)

        return pb.SerialPipelineSnapshot(
            inflight=inflightpb_obj,
            last_completion=lastpb_obj,
        )

    def apply_handshake_stats(self, observation: Mapping[str, Any]) -> None:
        """Update internal state from external handshake statistics."""
        try:
            self.handshake_attempts = int(observation.get("attempts") or 0)
            self.handshake_successes = int(observation.get("successes") or 0)
            self.handshake_failure_streak = int(observation.get("failure_streak") or 0)
            self.handshake_last_duration = float(observation.get("last_duration") or 0.0)
            self.last_handshake_unix = float(observation.get("last_unix") or 0.0)
            self.handshake_backoff_until = float(observation.get("backoff_until") or 0.0)
            self.handshake_rate_until = float(observation.get("rate_limit_until") or 0.0)
        except (ValueError, TypeError) as exc:
            logger.warning("Failed to apply handshake stats", error=exc)

    def _apply_spool_observation(self, observation: Mapping[str, Any]) -> None:
        """Update internal state from spool statistics."""
        if "corrupt_dropped" in observation:
            self.mqtt_spool_corrupt_dropped = int(observation["corrupt_dropped"])
        if "dropped_due_to_limit" in observation:
            self.mqtt_spool_dropped_limit = int(observation["dropped_due_to_limit"])
        if "trim_events" in observation:
            self.mqtt_spool_trim_events = int(observation["trim_events"])
        if "last_trim_unix" in observation:
            self.mqtt_spool_last_trim_unix = float(observation["last_trim_unix"])

    def build_metrics_snapshot(self) -> pb.DaemonMetrics:
        """Build a concrete metrics snapshot for telemetry. [SIL-2]"""
        supervisors = [pb.SupervisorEntry(name=name, stats=stats) for name, stats in self.supervisor_stats.items()]
        mqtt_drop_counts = [
            pb.MqttDropCount(topic=topic, count=count) for topic, count in self.mqtt_drop_counts.items()
        ]

        return pb.DaemonMetrics(
            mqtt_queue_depth=self.mqtt_publish_queue.qsize(),
            mqtt_dropped_messages=self.mqtt_dropped_messages,
            mqtt_drop_counts=mqtt_drop_counts,
            mqtt_spool_corrupt_dropped=self.mqtt_spool_corrupt_dropped,
            mqtt_spool_dropped_limit=self.mqtt_spool_dropped_limit,
            mqtt_spool_trim_events=self.mqtt_spool_trim_events,
            mqtt_spool_last_trim_unix=self.mqtt_spool_last_trim_unix,
            mqtt_spool_degraded=self.mqtt_spool_degraded,
            mqtt_spool_failure_reason=self.mqtt_spool_failure_reason or "",
            mqtt_spool_pending_messages=self.mqtt_spool_pending_messages,
            queue_depths=pb.QueueDepths(
                mqtt_publish=self.mqtt_publish_queue.qsize(),
                console=len(self.console_to_mcu_queue),
                mailbox_outgoing=len(self.mailbox_queue),
                mailbox_incoming=len(self.mailbox_incoming_queue),
                running_processes=len(self.running_processes),
            ),
            link_synchronised=self.is_synchronized,
            unknown_command_count=self.unknown_command_count,
            unknown_command_last_id=self.unknown_command_last_id,
            supervisors=supervisors,
            heartbeat_unix=time.time(),
            watchdog_enabled=self.watchdog_enabled,
            watchdog_interval=self.watchdog_interval,
        )

    def build_status_snapshot(self) -> pb.BridgeStatus:
        """Build a holistic snapshot of the bridge status. [SIL-2]"""
        return pb.BridgeStatus(
            metrics=self.build_metrics_snapshot(),
            bridge=self.build_bridge_snapshot(),
        )

    def build_handshake_snapshot(self) -> pb.HandshakeSnapshot:
        return pb.HandshakeSnapshot(
            synchronised=self.is_synchronized,
            attempts=self.handshake_attempts,
            successes=self.handshake_successes,
            failures=self.handshake_failures,
            failure_streak=self.handshake_failure_streak,
            last_error=self.last_handshake_error or "",
            last_unix=self.last_handshake_unix,
            last_duration=self.handshake_last_duration,
            backoff_until=self.handshake_backoff_until,
            rate_limit_until=self.handshake_rate_until,
            fatal_count=self.handshake_fatal_count,
            fatal_reason=self.handshake_fatal_reason or "",
            fatal_detail=self.handshake_fatal_detail or "",
            fatal_unix=self.handshake_fatal_unix,
            pending_nonce=bool(self.link_handshake_nonce),
            nonce_length=self.link_nonce_length,
        )

    def build_bridge_snapshot(self) -> pb.BridgeSnapshot:
        from google.protobuf.json_format import ParseDict

        versionpb_obj = None
        if self.mcu_version is not None:
            versionpb_obj = pb.VersionResponse(
                major=self.mcu_version[0],
                minor=self.mcu_version[1],
                patch=self.mcu_version[2],
            )

        capabilitiespb_obj = None
        if self.mcu_capabilities is not None:
            capabilitiespb_obj = pb.Capabilities()
            ParseDict(self.mcu_capabilities, capabilitiespb_obj)

        return pb.BridgeSnapshot(
            serial_link=pb.SerialLinkSnapshot(
                connected=self.is_connected,
                writer_attached=self.serial_writer is not None,
                synchronised=self.is_synchronized,
            ),
            handshake=self.build_handshake_snapshot(),
            serial_pipeline=self.build_serial_pipeline_snapshot(),
            serial_flow=self.serial_flow_stats,
            mcu_version=versionpb_obj,
            capabilities=capabilitiespb_obj,
        )

    def handshake_duration_since_start(self) -> float:
        if self.handshake_last_started <= 0.0:
            return 0.0
        return max(0.0, time.monotonic() - self.handshake_last_started)

    def __del__(self) -> None:
        """Last-resort cleanup to prevent ResourceWarning from unclosed dbm connections."""
        self.cleanup()

    def cleanup(self) -> None:
        try:
            if hasattr(self.mailbox_queue, "close"):
                res = cast(Any, self.mailbox_queue).close()
                if asyncio.iscoroutine(res):
                    try:
                        res.send(None)
                    except StopIteration:
                        pass
        except (OSError, RuntimeError, AttributeError) as e:
            logger.debug("Mailbox queue cleanup notice", error=e)

        try:
            if hasattr(self.mailbox_incoming_queue, "close"):
                res = cast(Any, self.mailbox_incoming_queue).close()
                if asyncio.iscoroutine(res):
                    try:
                        res.send(None)
                    except StopIteration:
                        pass
        except (OSError, RuntimeError, AttributeError) as e:
            logger.debug("Mailbox incoming queue cleanup notice", error=e)

        self.mailbox_queue = InMemoryDeque()
        self.mailbox_incoming_queue = InMemoryDeque()
        self.console_to_mcu_queue = collections.deque()

        if self.datastore_cache is not None:
            try:
                res = self.datastore_cache.close()
                if asyncio.iscoroutine(res):
                    try:
                        res.send(None)
                    except StopIteration:
                        pass
            except (OSError, RuntimeError, AttributeError) as e:
                logger.debug("Resource cleanup notice", error=e)
            self.datastore_cache = None

        self.mailbox_queue = InMemoryDeque()
        self.mailbox_incoming_queue = InMemoryDeque()

        import gc

        gc.collect()

        while not self.mqtt_publish_queue.empty():
            try:
                self.mqtt_publish_queue.get_nowait()
            except (OSError, RuntimeError, AttributeError) as e:
                logger.debug("Resource cleanup notice", error=e)
        self.mqtt_publish_queue = _make_mqtt_publish_queue(self.mqtt_queue_limit)

        if self.running_processes:
            for ctx in list(self.running_processes.values()):
                if ctx and ctx.handle:
                    try:
                        ctx.handle.terminate()
                    except (OSError, ProcessLookupError) as e:
                        logger.debug("Process termination cleanup notice", error=e)
            self.running_processes.clear()

        try:
            self.serial_tx_allowed.clear()
            self.link_sync_event.clear()
            self.pending_digital_reads.clear()
            self.pending_analog_reads.clear()
        except (OSError, RuntimeError, AttributeError) as e:
            logger.debug("State indicators cleanup notice", error=e)


def create_runtime_state(config: RuntimeConfig | dict[str, Any]) -> RuntimeState:
    from ..config.settings import load_runtime_config
    from google.protobuf import json_format

    if isinstance(config, dict):
        cfg = load_runtime_config(config)
    else:
        cfg = config

    cfg_dict = json_format.MessageToDict(cfg.pb_obj, preserving_proto_field_name=True)

    if "mqtt_topic" in cfg_dict:
        cfg_dict["mqtt_topic_prefix"] = cfg_dict.pop("mqtt_topic")
    if "process_max_output_bytes" in cfg_dict:
        cfg_dict["process_output_limit"] = cfg_dict.pop("process_max_output_bytes")

    cfg_dict["allowed_policy"] = cfg.allowed_policy
    cfg_dict["topic_authorization"] = cfg.topic_authorization

    state = RuntimeState(**cfg_dict)
    state.serial_tx_allowed.set()
    state.configure()

    return state

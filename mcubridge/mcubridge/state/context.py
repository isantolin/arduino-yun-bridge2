"""Runtime state container for the MCU Bridge daemon."""

from __future__ import annotations

import asyncio
import collections
import socket
import time
from pathlib import Path
from typing import Any, TypeVar, cast

from google.protobuf.json_format import ParseDict
from .storage import LmdbDeque, LmdbCache
import structlog

from ..config.const import (
    DEFAULT_FILE_STORAGE_QUOTA_BYTES,
    DEFAULT_FILE_SYSTEM_ROOT,
    DEFAULT_FILE_WRITE_MAX_BYTES,
    DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT,
    DEFAULT_MAILBOX_QUEUE_LIMIT,
    DEFAULT_CLOUD_QUEUE_LIMIT,
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
)
from ..protocol.structures import (
    PendingPinRequest,
    create_allowed_policy,
)
from ..protocol import mcubridge_pb2 as pb
from .metrics import DaemonMetrics

T = TypeVar("T")

logger = structlog.get_logger("mcubridge.state")


def _make_cloud_publish_queue(maxsize: int = 0) -> asyncio.Queue[pb.CloudQueuedPublish]:
    normalized = max(0, maxsize)
    return asyncio.Queue[pb.CloudQueuedPublish](maxsize=normalized)


class ProcessContext:
    __slots__ = ("handle", "io_lock", "exit_code")

    def __init__(self, handle: asyncio.subprocess.Process) -> None:
        self.handle = handle
        self.io_lock = asyncio.Lock()
        self.exit_code = 0


class RuntimeState:
    """Aggregated mutable state shared across the daemon layers. [SIL-2]"""

    metrics: DaemonMetrics
    serial_writer: asyncio.BaseTransport | None
    state: str
    cloud_queue_limit: int
    cloud_publish_queue: asyncio.Queue[pb.CloudQueuedPublish]
    cloud_drop_counts: dict[str, int]
    allow_non_tmp_paths: bool
    datastore_cache: LmdbCache | None
    connected_via_http3: bool
    mailbox_queue: LmdbDeque
    mailbox_incoming_queue: LmdbDeque
    mcu_is_paused: bool
    serial_tx_allowed: asyncio.Event
    console_to_mcu_queue: collections.deque[bytes]
    console_queue_limit_bytes: int
    console_queue_bytes: int
    console_dropped_chunks: int
    console_truncated_chunks: int
    running_processes: dict[int, ProcessContext]
    process_lock: asyncio.Lock
    next_pid: int
    allowed_policy: pb.AllowedCommandPolicy
    topic_authorization: pb.TopicAuthorization | None
    process_timeout: int
    file_system_root: str
    file_write_max_bytes: int
    file_storage_quota_bytes: int
    file_storage_bytes_used: int
    file_write_limit_rejections: int
    file_storage_limit_rejections: int
    cloud_topic_prefix: str
    watchdog_enabled: bool
    watchdog_interval: float
    last_watchdog_beat: float
    pending_digital_reads: collections.deque[PendingPinRequest]
    pending_analog_reads: collections.deque[PendingPinRequest]
    mailbox_queue_limit: int
    mailbox_queue_bytes_limit: int
    pending_pin_request_limit: int
    mailbox_queue_bytes: int
    mailbox_dropped_messages: int
    mailbox_truncated_messages: int
    mailbox_incoming_queue_bytes: int
    mailbox_incoming_dropped_messages: int
    mailbox_incoming_truncated_messages: int
    mcu_version: tuple[int, int, int] | None
    mcu_capabilities: pb.Capabilities | dict[str, Any] | None
    link_handshake_nonce: bytes | None
    link_sync_event: asyncio.Event
    link_expected_tag: bytes | None
    link_session_key: bytes | None
    link_aead_cipher: Any | None
    link_nonce_length: int
    link_nonce_counter: int
    link_last_nonce_counter: int
    handshake_failure_streak: int
    handshake_backoff_until: float
    handshake_rate_until: float
    last_handshake_error: str | None
    last_handshake_unix: float
    handshake_last_duration: float
    handshake_fatal_count: int
    handshake_fatal_reason: str | None
    handshake_fatal_detail: str | None
    handshake_fatal_unix: float
    handshake_last_started: float
    serial_flow_stats: pb.SerialFlowSnapshot
    serial_throughput_stats: pb.SerialThroughputStats
    serial_pipeline_inflight: dict[str, Any] | None
    serial_pipeline_last: dict[str, Any] | None
    process_output_limit: int
    process_max_concurrent: int
    unknown_command_count: int
    unknown_command_last_id: int
    config_source: str
    serial_ack_timeout_ms: int
    serial_response_timeout_ms: int
    serial_retry_limit: int
    mcu_status_counts: dict[str, int]
    supervisor_stats: dict[str, pb.SupervisorSnapshot]
    supervisor_failures: int
    last_supervisor_error: str | None
    cloud_dropped_messages: int
    serial_decode_errors: int
    handshake_attempts: int
    handshake_successes: int
    watchdog_beats: int
    cloud_spool_corrupt_dropped: int
    cloud_spool_dropped_limit: int
    cloud_spool_trim_events: int
    cloud_spool_last_trim_unix: float
    cloud_spool_degraded: bool
    cloud_spool_failure_reason: str | None
    cloud_spool_pending_messages: int

    def __init__(self, **kwargs: Any) -> None:
        self.metrics: DaemonMetrics = kwargs.get("metrics") or DaemonMetrics()
        self.serial_writer: asyncio.BaseTransport | None = kwargs.get("serial_writer")
        self.state: str = kwargs.get("state", "disconnected")

        self.cloud_queue_limit: int = kwargs.get("cloud_queue_limit", DEFAULT_CLOUD_QUEUE_LIMIT)
        self.cloud_publish_queue: asyncio.Queue[pb.CloudQueuedPublish] = kwargs.get(
            "cloud_publish_queue"
        ) or _make_cloud_publish_queue(self.cloud_queue_limit)
        self.cloud_drop_counts: dict[str, int] = kwargs.get("cloud_drop_counts") or {}
        self.allow_non_tmp_paths: bool = kwargs.get("allow_non_tmp_paths", False)
        self.datastore_cache: LmdbCache | None = kwargs.get("datastore_cache")
        self.connected_via_http3: bool = False

        self.mailbox_queue: LmdbDeque = kwargs.get("mailbox_queue") or LmdbDeque(path=":memory:")
        self.mailbox_incoming_queue: LmdbDeque = kwargs.get("mailbox_incoming_queue") or LmdbDeque(path=":memory:")

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
        self.cloud_topic_prefix: str = kwargs.get("cloud_topic_prefix", protocol.CLOUD_DEFAULT_TOPIC_PREFIX)
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
        self.mcu_capabilities: pb.Capabilities | dict[str, Any] | None = kwargs.get("mcu_capabilities")
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

        self.cloud_dropped_messages: int = kwargs.get("cloud_dropped_messages", 0)
        self.serial_decode_errors: int = kwargs.get("serial_decode_errors", 0)
        self.handshake_attempts: int = kwargs.get("handshake_attempts", 0)
        self.handshake_successes: int = kwargs.get("handshake_successes", 0)
        self.watchdog_beats: int = kwargs.get("watchdog_beats", 0)

        self.cloud_spool_corrupt_dropped: int = kwargs.get("cloud_spool_corrupt_dropped", 0)
        self.cloud_spool_dropped_limit: int = kwargs.get("cloud_spool_dropped_limit", 0)
        self.cloud_spool_trim_events: int = kwargs.get("cloud_spool_trim_events", 0)
        self.cloud_spool_last_trim_unix: float = kwargs.get("cloud_spool_last_trim_unix", 0.0)
        self.cloud_spool_degraded: bool = kwargs.get("cloud_spool_degraded", False)
        self.cloud_spool_failure_reason: str | None = kwargs.get("cloud_spool_failure_reason")
        self.cloud_spool_pending_messages: int = kwargs.get("cloud_spool_pending_messages", 0)

    @property
    def device_id(self) -> str:
        return socket.gethostname()

    @property
    def topic_prefix(self) -> str:
        return self.cloud_topic_prefix

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
        return max(0, self.handshake_attempts - self.handshake_successes)

    @property
    def allowed_commands(self) -> tuple[str, ...]:
        """Return the current allowed command list from policy."""
        return tuple(self.allowed_policy.entries)

    def mailbox_queue_depth(self) -> int:
        return len(self.mailbox_queue)

    def mailbox_incoming_queue_depth(self) -> int:
        return len(self.mailbox_incoming_queue)

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
        self.cloud_publish_queue = _make_cloud_publish_queue(self.cloud_queue_limit)
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
                    return LmdbDeque(path=str(directory / "spool_lmdb"), maxlen=self.mailbox_queue_limit)
                except (OSError, RuntimeError) as exc:
                    logger.warning("Spool falling back to RAM", spool=subdir, error=str(exc))

            return LmdbDeque(path=":memory:", maxlen=self.mailbox_queue_limit)

        self.mailbox_queue = _create_spool("mailbox_out")
        self.mailbox_incoming_queue = _create_spool("mailbox_in")

        # [SIL-2] Initialize datastore with LMDB for ACID persistence
        ds_dir = None
        if self.allow_non_tmp_paths or self.file_system_root.startswith("/tmp/"):
            ds_dir = Path(self.file_system_root) / "datastore"

        if ds_dir and self.file_system_root:
            try:
                ds_dir.mkdir(parents=True, exist_ok=True)
                self.datastore_cache = LmdbCache(str(ds_dir / "data_lmdb"))
            except (OSError, RuntimeError):
                logger.warning("Datastore falling back to RAM cache")
                self.datastore_cache = None

    def build_serial_pipeline_snapshot(self) -> pb.SerialPipelineSnapshot:
        def _to_pipeline_event(ev_dict: dict[str, Any] | None) -> pb.PipelineEvent:
            return pb.PipelineEvent(**ev_dict) if ev_dict else pb.PipelineEvent(event="none")

        return pb.SerialPipelineSnapshot(
            inflight=_to_pipeline_event(self.serial_pipeline_inflight),
            last_completion=_to_pipeline_event(self.serial_pipeline_last),
        )

    def build_metrics_snapshot(self) -> pb.DaemonMetrics:
        """Build a concrete metrics snapshot for telemetry. [SIL-2]"""
        supervisors = [pb.SupervisorEntry(name=name, stats=stats) for name, stats in self.supervisor_stats.items()]
        cloud_drop_counts = [
            pb.CloudDropCount(topic=topic, count=count) for topic, count in self.cloud_drop_counts.items()
        ]

        return pb.DaemonMetrics(
            cloud_queue_depth=self.cloud_publish_queue.qsize(),
            cloud_dropped_messages=self.cloud_dropped_messages,
            cloud_drop_counts=cloud_drop_counts,
            cloud_spool_corrupt_dropped=self.cloud_spool_corrupt_dropped,
            cloud_spool_dropped_limit=self.cloud_spool_dropped_limit,
            cloud_spool_trim_events=self.cloud_spool_trim_events,
            cloud_spool_last_trim_unix=self.cloud_spool_last_trim_unix,
            cloud_spool_degraded=self.cloud_spool_degraded,
            cloud_spool_failure_reason=self.cloud_spool_failure_reason or "",
            cloud_spool_pending_messages=self.cloud_spool_pending_messages,
            queue_depths=pb.QueueDepths(
                cloud_publish=self.cloud_publish_queue.qsize(),
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
        versionpb_obj = None
        if self.mcu_version is not None:
            versionpb_obj = pb.VersionResponse(
                major=self.mcu_version[0],
                minor=self.mcu_version[1],
                patch=self.mcu_version[2],
            )

        capabilitiespb_obj = None
        if self.mcu_capabilities is not None:
            if isinstance(self.mcu_capabilities, pb.Capabilities):
                capabilitiespb_obj = self.mcu_capabilities
            else:
                capabilitiespb_obj = pb.Capabilities()
                ParseDict(self.mcu_capabilities, cast(Any, capabilitiespb_obj))

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
        self.mailbox_queue = LmdbDeque(path=":memory:")
        self.mailbox_incoming_queue = LmdbDeque(path=":memory:")
        self.console_to_mcu_queue = collections.deque()
        self.datastore_cache = None

        while not self.cloud_publish_queue.empty():
            try:
                self.cloud_publish_queue.get_nowait()
            except (OSError, RuntimeError, AttributeError) as e:
                logger.debug("Resource cleanup notice", error=e)
        self.cloud_publish_queue = _make_cloud_publish_queue(self.cloud_queue_limit)

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

    cfg = load_runtime_config(config) if isinstance(config, dict) else config

    state = RuntimeState(
        cloud_topic_prefix=cfg.topic_prefix,
        cloud_queue_limit=cfg.cloud_queue_limit,
        process_output_limit=cfg.process_max_output_bytes,
        process_timeout=cfg.process_timeout,
        process_max_concurrent=cfg.process_max_concurrent,
        file_system_root=cfg.file_system_root,
        file_write_max_bytes=cfg.file_write_max_bytes,
        file_storage_quota_bytes=cfg.file_storage_quota_bytes,
        watchdog_enabled=cfg.watchdog_enabled,
        watchdog_interval=cfg.watchdog_interval,
        allowed_policy=cfg.allowed_policy,
        topic_authorization=cfg.topic_authorization,
    )
    state.serial_tx_allowed.set()
    state.configure()

    return state

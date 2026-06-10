"""Runtime state container for the MCU Bridge daemon."""

from __future__ import annotations

import asyncio
import collections
from collections.abc import Mapping
import time
from typing import Any, TypeVar, cast, TYPE_CHECKING

from .storage import DbmDeque, DbmCache
import msgspec
import structlog

from ..protocol import mcubridge_pb2 as pb
from ..protocol.protocol import Status
from ..protocol.structures import (
    SerialFlowStats,
    SerialThroughputStats,
    SupervisorStats,
)

if TYPE_CHECKING:
    from ..config.settings import RuntimeConfig
    from .metrics import Metrics

T = TypeVar("T")
logger = structlog.get_logger("mcubridge.state.context")


def _res_cmd(command_id: int) -> str:
    """Resolve command name for logging."""
    from ..protocol.protocol import Command

    try:
        return Command(command_id).name
    except ValueError:
        return f"0x{command_id:04X}"


class RuntimeState:
    """Central state management for the MCU Bridge daemon. [SIL-2]"""

    config: RuntimeConfig
    metrics: Metrics
    serial_flow_stats: SerialFlowStats
    serial_throughput_stats: SerialThroughputStats
    supervisor_stats: dict[str, SupervisorStats]

    mqtt_publish_queue: DbmDeque
    console_to_mcu_queue: collections.deque[bytes]
    mailbox_queue: DbmDeque
    mailbox_incoming_queue: DbmDeque
    running_processes: dict[int, Any]
    datastore_cache: DbmCache
    pending_digital_reads: list[Any]
    pending_analog_reads: list[Any]
    mcu_status_counts: collections.Counter[int]
    mqtt_drop_counts: collections.Counter[str]

    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config
        self.mqtt_topic_prefix: str = config.mqtt_topic
        self.serial_port: str = config.serial_port
        self.serial_baud: int = config.serial_baud
        self.is_connected = False
        self.is_synchronized = False
        self.serial_writer: asyncio.StreamWriter | None = None
        self.mcu_version: tuple[int, int, int] | None = None
        self.mcu_capabilities: dict[str, Any] = {}

        # FSM State
        self.state = "unsynchronized"
        self.serial_tx_allowed = asyncio.Event()
        self.serial_tx_allowed.set()

        # [SIL-2] Deterministic Handshake Telemetry
        self.handshake_attempts = 0
        self.handshake_successes = 0
        self.handshake_failures = 0
        self.handshake_failure_streak = 0
        self.last_handshake_error: str | None = None
        self.last_handshake_unix = 0.0
        self.handshake_last_duration = 0.0
        self.handshake_last_started = 0.0
        self.handshake_backoff_until = 0.0
        self.handshake_rate_until = 0.0
        self.handshake_fatal_count = 0
        self.handshake_fatal_reason: str | None = None
        self.handshake_fatal_detail: str | None = None
        self.handshake_fatal_unix = 0.0

        # Link Layer
        self.link_nonce_counter = 0
        self.link_last_nonce_counter = 0
        self.link_session_key: bytes | None = None
        self.link_handshake_nonce: bytes | None = None
        self.link_nonce_length = 0
        self.link_expected_tag: bytes | None = None
        self.link_sync_event = asyncio.Event()

        # Control Plane
        self.mcu_is_paused = False
        self.watchdog_enabled: bool = config.watchdog_enabled
        self.watchdog_interval: float = config.watchdog_interval
        self.last_watchdog_beat = 0.0
        self.watchdog_beats = 0

        # Stats & Metrics
        from .metrics import Metrics

        self.metrics = Metrics()
        self.serial_flow_stats = SerialFlowStats()
        self.serial_throughput_stats = SerialThroughputStats()
        self.supervisor_stats = collections.defaultdict(SupervisorStats)
        self.mcu_status_counts = collections.Counter()
        self.mqtt_drop_counts = collections.Counter()
        self.serial_decode_errors = 0

        # Queues & Storage
        self.mqtt_publish_limit: int = config.mqtt_queue_limit
        self.mqtt_spool_dir: str | None = config.mqtt_spool_dir
        self.mqtt_spool_corrupt_dropped = 0
        self.mqtt_spool_dropped_limit = 0
        self.mqtt_spool_trim_events = 0
        self.mqtt_spool_last_trim_unix = 0.0
        self.mqtt_spool_degraded = False
        self.mqtt_spool_failure_reason: str | None = None
        self.mqtt_spool_pending_messages = 0

        self.console_limit: int = config.console_queue_limit_bytes
        self.mailbox_limit: int = config.mailbox_queue_limit
        self.mailbox_bytes_limit: int = config.mailbox_queue_bytes_limit
        self.pending_pin_limit: int = config.pending_pin_request_limit
        self.process_max_concurrent: int = config.process_max_concurrent
        self.process_max_output: int = config.process_max_output_bytes
        self.process_timeout: int = config.process_timeout

        self.file_write_max: int = config.file_write_max_bytes
        self.file_storage_quota: int = config.file_storage_quota_bytes
        self.file_storage_limit_rejections = 0
        self.file_write_limit_rejections = 0
        self.file_system_root: str = config.file_system_root

        # Internal collections
        self.console_to_mcu_queue = collections.deque()
        self.running_processes = {}
        self.pending_digital_reads = []
        self.pending_analog_reads = []

        # Pipeline Tracking
        self.serial_pipeline_inflight = None
        self.serial_pipeline_last = None

    def cleanup(self) -> None:
        """Resource finalization."""
        self.metrics.cleanup()

    def mark_transport_connected(self) -> None:
        """Update state after serial transport is established."""
        self.is_connected = True
        self.is_synchronized = False
        self.state = "unsynchronized"
        self.metrics.serial_connected.set(1.0)
        self.metrics.handshake_state.state("unsynchronized")

    def mark_transport_disconnected(self) -> None:
        """Update state after serial transport is lost."""
        self.is_connected = False
        self.is_synchronized = False
        self.state = "disconnected"
        self.link_session_key = None
        self.mcu_is_paused = False
        self.serial_tx_allowed.set()
        self.metrics.serial_connected.set(0.0)
        self.metrics.handshake_state.state("disconnected")

    def mark_synchronized(self) -> None:
        """Update state after handshake success."""
        self.is_connected = True
        self.is_synchronized = True
        self.state = "synchronized"
        self.handshake_failure_streak = 0
        self.handshake_successes += 1
        self.metrics.serial_connected.set(1.0)
        self.metrics.handshake_state.state("synchronized")
        self.metrics.handshake_successes.inc()

    def record_watchdog_beat(self) -> None:
        """Record a successful heartbeat from the MCU."""
        self.last_watchdog_beat = time.time()
        self.watchdog_beats += 1
        self.metrics.watchdog_beats.inc()

    def handshake_duration_since_start(self) -> float:
        """Return time elapsed since last handshake began."""
        if self.handshake_last_started <= 0:
            return 0.0
        return max(0.0, time.monotonic() - self.handshake_last_started)

    def mark_supervisor_healthy(self, name: str) -> None:
        """Reset failure metrics for a supervisor worker."""
        stats = self.supervisor_stats.get(name)
        if stats:
            stats.backoff_seconds = 0.0
            stats.fatal = False

    def record_supervisor_failure(self, name: str, exc: Exception) -> None:
        """Record a supervisor worker failure."""
        stats = self.supervisor_stats[name]
        stats.restarts += 1
        stats.last_failure_unix = time.time()
        stats.last_exception = str(exc)

    def record_serial_pipeline_event(self, event: Any) -> None:
        """Update pipeline snapshots from transport events. [SIL-2]"""
        name = getattr(event, "event", "unknown")
        command_id = getattr(event, "command_id", 0)
        attempt = getattr(event, "attempt", 1)
        timestamp = getattr(event, "timestamp", time.time())
        status_code = getattr(event, "status", None)
        acked = getattr(event, "ack_received", False)

        if name == "sent":
            self.serial_pipeline_inflight = {
                "command_id": command_id,
                "command_name": _res_cmd(command_id),
                "attempt": attempt,
                "started_unix": timestamp,
                "acknowledged": False,
            }
            self.serial_flow_stats.commands_sent += 1
            self.metrics.serial_commands_sent.inc()
        elif name in ("ack", "failure"):
            inf = cast(dict[str, Any], self.serial_pipeline_inflight) if self.serial_pipeline_inflight else None
            if name == "ack":
                self.serial_flow_stats.commands_acked += 1
                self.metrics.serial_commands_acked.inc()
            else:
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
                payload["started_unix"] = inf.get("started_unix", 0.0)
                try:
                    start_val = float(inf.get("started_unix", timestamp))
                    payload["duration"] = max(0.0, timestamp - start_val)
                except (ValueError, TypeError):
                    payload["duration"] = 0.0
            self.serial_pipeline_last = payload
            self.serial_pipeline_inflight = None

    def build_serial_pipeline_snapshot(self) -> pb.SerialPipelineSnapshot:
        """Construct binary snapshot of the serial pipeline."""
        snapshot = pb.SerialPipelineSnapshot()
        if self.serial_pipeline_inflight:
            inf = dict(cast(dict[str, Any], self.serial_pipeline_inflight))
            inf.pop("command_name", None)
            inf.pop("started_unix", None)
            inf.pop("acknowledged", None)
            if inf.get("status") is None:
                inf["status"] = 0
            snapshot.inflight.CopyFrom(pb.PipelineEvent(event="sent", **inf))
        if self.serial_pipeline_last:
            last = dict(cast(dict[str, Any], self.serial_pipeline_last))
            if last.get("status_code") is not None:
                last["status"] = last.pop("status_code")
            if last.get("status") is None:
                last["status"] = 0
            if last.get("completed_unix") is not None:
                last["timestamp"] = last.pop("completed_unix")
            if last.get("acknowledged") is not None:
                last["ack_received"] = last.pop("acknowledged")

            last.pop("command_name", None)
            last.pop("status_name", None)
            last.pop("started_unix", None)
            last.pop("duration", None)
            snapshot.last_completion.CopyFrom(pb.PipelineEvent(**last))
        return snapshot

    def apply_handshake_stats(self, observation: Mapping[str, Any] | pb.HandshakeSnapshot) -> None:
        """Update internal state from external handshake statistics."""
        try:
            from google.protobuf.json_format import ParseDict

            snap = pb.HandshakeSnapshot()
            if isinstance(observation, pb.HandshakeSnapshot):
                snap.CopyFrom(observation)
            elif isinstance(observation, dict):
                ParseDict(observation, snap, ignore_unknown_fields=True)

            self.handshake_attempts = snap.attempts
            self.handshake_successes = snap.successes
            self.handshake_failure_streak = snap.failure_streak
            self.handshake_last_duration = snap.last_duration
            self.last_handshake_unix = snap.last_unix
            self.handshake_backoff_until = snap.backoff_until
            self.handshake_rate_until = snap.rate_limit_until
        except (msgspec.MsgspecError, ValueError, TypeError) as exc:
            logger.warning("Failed to apply handshake stats", error=exc)

    def _apply_spool_observation(self, observation: Mapping[str, Any] | pb.DaemonMetrics) -> None:
        """Update internal state from spool statistics."""
        if isinstance(observation, pb.DaemonMetrics):
            self.mqtt_spool_corrupt_dropped = observation.mqtt_spool_corrupt_dropped
            self.mqtt_spool_dropped_limit = observation.mqtt_spool_dropped_limit
            self.mqtt_spool_trim_events = observation.mqtt_spool_trim_events
            self.mqtt_spool_last_trim_unix = observation.mqtt_spool_last_trim_unix
        elif isinstance(observation, Mapping):
            if "corrupt_dropped" in observation:
                self.mqtt_spool_corrupt_dropped = int(observation["corrupt_dropped"])
            if "dropped_due_to_limit" in observation:
                self.mqtt_spool_dropped_limit = int(observation["dropped_due_to_limit"])
            if "trim_events" in observation:
                self.mqtt_spool_trim_events = int(observation["trim_events"])
            if "last_trim_unix" in observation:
                self.mqtt_spool_last_trim_unix = float(observation["last_trim_unix"])

    def build_metrics_snapshot(self) -> pb.DaemonMetrics:
        """Construct binary snapshot of all daemon metrics."""
        metrics = pb.DaemonMetrics(
            serial=self.serial_flow_stats.as_snapshot(),
            serial_throughput=self.serial_throughput_stats.as_snapshot(),
            mqtt_spool_corrupt_dropped=self.mqtt_spool_corrupt_dropped,
            mqtt_spool_dropped_limit=self.mqtt_spool_dropped_limit,
            mqtt_spool_trim_events=self.mqtt_spool_trim_events,
            mqtt_spool_last_trim_unix=self.mqtt_spool_last_trim_unix,
            mqtt_spool_degraded=self.mqtt_spool_degraded,
            mqtt_spool_failure_reason=self.mqtt_spool_failure_reason or "",
            mqtt_spool_pending_messages=self.mqtt_spool_pending_messages,
            queue_depths=pb.QueueDepths(
                mqtt_publish=self.mqtt_publish_queue.qsize() if hasattr(self, "mqtt_publish_queue") else 0,
                console=len(self.console_to_mcu_queue),
                mailbox_outgoing=self.mailbox_queue.qsize() if hasattr(self, "mailbox_queue") else 0,
                mailbox_incoming=self.mailbox_incoming_queue.qsize() if hasattr(self, "mailbox_incoming_queue") else 0,
                running_processes=len(self.running_processes),
            ),
            handshake=self.build_handshake_snapshot(),
            link_synchronised=self.is_synchronized,
            file_storage_limit_rejections=self.file_storage_limit_rejections,
            file_write_limit_rejections=self.file_write_limit_rejections,
        )
        for n, s in self.supervisor_stats.items():
            metrics.supervisors[n].CopyFrom(s.as_snapshot())
        return metrics

    def build_handshake_snapshot(self) -> pb.HandshakeSnapshot:
        """Construct binary snapshot of the link handshake state."""
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
        """Construct binary snapshot of the entire bridge state."""
        return pb.BridgeSnapshot(
            serial_link=pb.SerialLinkSnapshot(
                connected=self.is_connected,
                writer_attached=self.serial_writer is not None,
                synchronised=self.is_synchronized,
            ),
            handshake=self.build_handshake_snapshot(),
            serial_pipeline=self.build_serial_pipeline_snapshot(),
            serial_flow=self.serial_flow_stats.as_snapshot(),
            mcu_version=(
                pb.VersionResponse(
                    major=self.mcu_version[0],
                    minor=self.mcu_version[1],
                    patch=self.mcu_version[2],
                )
                if self.mcu_version
                else None
            ),
            capabilities=pb.Capabilities(**self.mcu_capabilities) if self.mcu_capabilities else None,
        )

    def record_mqtt_drop(self, reason: str) -> None:
        """Record an MQTT message drop event."""
        self.mqtt_drop_counts[reason] += 1
        self.metrics.mqtt_dropped_messages.labels(reason=reason).inc()


def create_runtime_state(config: RuntimeConfig) -> RuntimeState:
    """Factory for RuntimeState instances."""
    return RuntimeState(config)

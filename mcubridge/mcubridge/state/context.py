"""SIL-2 compliant runtime state context management for McuBridge."""

from __future__ import annotations

import asyncio
import collections
import time
from typing import Any

import structlog

from mcubridge.config.settings import RuntimeConfig
from mcubridge.protocol import protocol_pb2 as pb
from mcubridge.protocol.structures import PendingPinRequest
from mcubridge.state.storage import SqliteCache, SqliteDeque

logger = structlog.get_logger(__name__)


def _make_cloud_publish_queue(limit: int) -> asyncio.Queue[pb.CloudQueuedPublish]:
    """Instantiate a bounded async queue for spooled cloud messages [SIL-2]."""
    return asyncio.Queue(maxsize=limit)


class RuntimeState:
    """Encapsulates execution state, active subscriptions, and pending hardware requests."""

    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config
        self.state: str = "init"
        self.connected: bool = False
        self.cloud_connected: bool = False
        self.mcu_id: str = ""
        self.mcu_version: str = ""
        self.session_key: bytes | None = None
        self.tx_paused: bool = False

        self.link_sync_event: asyncio.Event = asyncio.Event()

        self.mailbox_queue: SqliteDeque = SqliteDeque(path=":memory:")
        self.mailbox_incoming_queue: SqliteDeque = SqliteDeque(path=":memory:")
        self.console_to_mcu_queue: collections.deque[bytes] = collections.deque()
        self.datastore_cache: SqliteCache | None = None

        self.cloud_queue_limit: int = getattr(config, "cloud_spool_max_messages", 1000)
        self.cloud_publish_queue: asyncio.Queue[pb.CloudQueuedPublish] = _make_cloud_publish_queue(
            self.cloud_queue_limit
        )

        self.pending_digital_reads: collections.deque[PendingPinRequest] = collections.deque()
        self.pending_analog_reads: collections.deque[PendingPinRequest] = collections.deque()
        self.pin_modes: dict[int, int] = {}
        self.running_processes: dict[int, Any] = {}

        self.subscribers: dict[asyncio.Queue[pb.LocalEvent], str | None] = {}

        self.handshake_last_started: float = 0.0
        self.handshake_failed_count: int = 0
        self.pending_pin_request_limit: int = 256

        self.cloud_spool_pending_messages: int = 0

    @property
    def is_handshake_complete(self) -> bool:
        return self.state == "synchronized"

    def subscribe(self, queue: asyncio.Queue[pb.LocalEvent], topic_filter: str | None = None) -> None:
        """Register a subscriber queue with optional topic filtering."""
        self.subscribers[queue] = topic_filter

    def unsubscribe(self, queue: asyncio.Queue[pb.LocalEvent]) -> None:
        """Remove a subscriber queue."""
        self.subscribers.pop(queue, None)

    async def notify_subscribers(self, event: pb.LocalEvent) -> None:
        """Dispatch event to all matching subscriber queues."""
        for queue, topic_filter in list(self.subscribers.items()):
            if topic_filter is None or event.topic.startswith(topic_filter):
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    logger.warning("Subscriber queue full, dropping event", topic=event.topic)

    async def publish_to_subscriber(self, queue: object, event: pb.LocalEvent) -> None:
        """Publish event directly to a single subscriber target."""
        if isinstance(queue, asyncio.Queue):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("Target subscriber queue full, dropping event", topic=event.topic)

    def record_handshake_start(self) -> None:
        self.handshake_last_started = time.monotonic()

    def record_handshake_failure(self) -> None:
        self.handshake_failed_count += 1

    def record_handshake_success(self) -> None:
        self.handshake_failed_count = 0

    def time_since_handshake_start(self) -> float:
        if self.handshake_last_started == 0.0:
            return 0.0
        return max(0.0, time.monotonic() - self.handshake_last_started)

    def __del__(self) -> None:
        """Last-resort cleanup to prevent ResourceWarning from unclosed dbm connections."""
        self.cleanup()

    def cleanup(self) -> None:
        self.mailbox_queue.detach_connection()
        self.mailbox_incoming_queue.detach_connection()

        if self.datastore_cache is not None:
            self.datastore_cache.detach_connection()

        self.mailbox_queue = SqliteDeque(path=":memory:")
        self.mailbox_incoming_queue = SqliteDeque(path=":memory:")
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
                proc = getattr(ctx, "process", None)
                if proc is not None and getattr(proc, "returncode", None) is None:
                    try:
                        proc.terminate()
                    except (OSError, ProcessLookupError) as exc:
                        logger.debug("Process termination notice", error=exc)
            self.running_processes.clear()

        self.pending_digital_reads.clear()
        self.pending_analog_reads.clear()
        self.pin_modes.clear()
        self.subscribers.clear()


def create_runtime_state(config: RuntimeConfig) -> RuntimeState:
    """Factory function creating an initialized RuntimeState instance."""
    return RuntimeState(config)

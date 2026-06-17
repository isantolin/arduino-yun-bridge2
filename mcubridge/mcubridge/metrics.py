"""Periodic metrics publisher and Prometheus exporter for MCU Bridge."""

from __future__ import annotations

import asyncio
import math
import weakref
from collections.abc import Awaitable, Callable, Iterable, Sequence
from typing import Any

from prometheus_client.core import Metric
from prometheus_client.registry import Collector
import structlog

from .protocol import mcubridge_pb2 as pb
from .protocol import structures
from .protocol.structures import PROTOBUF_CONTENT_TYPE, create_queued_publish
from .protocol.topics import Topic, topic_path
from .state.context import RuntimeState

logger = structlog.get_logger("mcubridge.metrics")
_BRIDGE_SNAPSHOT_EXPIRY_SECONDS = 30

PublishEnqueue = Callable[[pb.MqttQueuedPublish], Awaitable[None]]


def _build_metrics_message(
    state: RuntimeState,
    snapshot: pb.DaemonMetrics,
    *,
    expiry_seconds: float,
) -> pb.MqttQueuedPublish:
    topic = topic_path(
        state.mqtt_topic_prefix,
        Topic.SYSTEM,
        "metrics",
    )
    # [SIL-2] Direct Protobuf serialization without StructuredPayload overhead
    message = create_queued_publish(
        topic_name=topic,
        payload=snapshot.SerializeToString(),
        content_type=PROTOBUF_CONTENT_TYPE,
        message_expiry_interval=int(expiry_seconds),
        user_properties=(),
    )

    extra_props: list[tuple[str, str]] = []
    if snapshot.mqtt_spool_degraded:
        extra_props.append(("bridge-spool", snapshot.mqtt_spool_failure_reason or "unknown"))

    # Extra props for files
    if state.file_storage_limit_rejections > 0:
        extra_props.append(("bridge-files", "quota-blocked"))
    elif state.file_write_limit_rejections > 0:
        extra_props.append(("bridge-files", "write-limit"))

    extra_props.append(("bridge-watchdog-enabled", "1" if snapshot.watchdog_enabled else "0"))
    if snapshot.watchdog_enabled:
        extra_props.append(("bridge-watchdog-interval", str(snapshot.watchdog_interval)))

    if extra_props:
        user_props = [(p.key, p.value) for p in message.user_properties]
        user_props.extend(extra_props)
        message = structures.replace_mqtt_publish(message, user_properties=user_props)

    return message


async def _emit_metrics_snapshot(
    state: RuntimeState,
    enqueue: PublishEnqueue,
    *,
    expiry_seconds: float,
) -> None:
    snapshot = state.build_metrics_snapshot()
    await enqueue(
        _build_metrics_message(
            state,
            snapshot,
            expiry_seconds=expiry_seconds,
        )
    )


async def _emit_bridge_snapshot(
    state: RuntimeState,
    enqueue: PublishEnqueue,
    flavor: str,
) -> None:
    try:
        snapshot = state.build_handshake_snapshot() if flavor == "handshake" else state.build_bridge_snapshot()
        await enqueue(
            _build_bridge_snapshot_message(
                state,
                flavor,
                snapshot,
            )
        )
    except asyncio.CancelledError:
        raise
    except (TypeError, ValueError, OSError) as e:
        logger.error(
            "Failed to publish bridge snapshot (serialization/IO): %s",
            e,
            flavor=flavor,
        )
    except AttributeError as e:
        logger.critical(
            "Unexpected error in bridge snapshot builder: %s",
            e,
            exc_info=True,
            flavor=flavor,
        )


async def publish_metrics(
    state: RuntimeState,
    enqueue: PublishEnqueue,
    interval: float,
    *,
    min_interval: float = 5.0,
) -> None:
    """Publish runtime metrics to MQTT at a fixed cadence."""

    if interval <= 0:
        raise ValueError("interval must be greater than zero")
    tick_seconds = max(1, math.ceil(max(min_interval, interval)))
    expiry = float(tick_seconds * 2)

    async def _metrics_tick() -> None:
        try:
            await _emit_metrics_snapshot(state, enqueue, expiry_seconds=expiry)
        except asyncio.CancelledError:
            raise
        except (OSError, RuntimeError) as e:
            logger.error("Periodic metrics emit failed: %s", e)

    try:
        while True:
            await _metrics_tick()
            await asyncio.sleep(tick_seconds)
    except asyncio.CancelledError:
        logger.info("Metrics publisher cancelled.")
        raise


async def publish_bridge_snapshots(
    state: RuntimeState,
    enqueue: PublishEnqueue,
    *,
    summary_interval: float,
    handshake_interval: float,
    min_interval: float = 5.0,
) -> None:
    """Periodically publish bridge summary and handshake snapshots."""

    summary_seconds = max(1, math.ceil(max(min_interval, summary_interval))) if summary_interval > 0 else None
    handshake_seconds = max(1, math.ceil(max(min_interval, handshake_interval))) if handshake_interval > 0 else None

    if summary_seconds is None and handshake_seconds is None:
        logger.info("Bridge snapshot loops disabled; awaiting cancellation.")
        # Equivalent to waiting forever until cancelled
        await asyncio.Event().wait()
        return

    async with asyncio.TaskGroup() as tg:
        if summary_seconds is not None:

            async def _summary_loop() -> None:
                while True:
                    try:
                        await _emit_bridge_snapshot(state, enqueue, flavor="summary")
                    except asyncio.CancelledError:
                        raise
                    except (OSError, RuntimeError) as e:
                        logger.error("Bridge summary emit failed: %s", e)
                    await asyncio.sleep(summary_seconds)

            tg.create_task(_summary_loop())

        if handshake_seconds is not None:

            async def _handshake_loop() -> None:
                while True:
                    try:
                        await _emit_bridge_snapshot(state, enqueue, flavor="handshake")
                    except asyncio.CancelledError:
                        raise
                    except (OSError, RuntimeError) as e:
                        logger.error("Bridge handshake emit failed: %s", e)
                    await asyncio.sleep(handshake_seconds)

            tg.create_task(_handshake_loop())


class RuntimeStateCollector(Collector):
    """[SIL-2] Dynamic collector for Prometheus dimensional metrics.

    Provides on-demand mapping of RuntimeState attributes to Prometheus Gauge
    and Info families, using labels for grouping related metrics.
    """

    def __init__(self, state: RuntimeState) -> None:
        self._state_ref = weakref.ref(state)

    def collect(self) -> Iterable[Metric]:
        """Collect dimensional metrics from the current daemon state."""
        state = self._state_ref()
        if state is None:
            return

        from prometheus_client.core import GaugeMetricFamily

        # 1. Queue Depths (Dimensional)
        q_depths = GaugeMetricFamily(
            "mcubridge_queue_depth",
            "Current number of items in internal asynchronous queues",
            labels=["queue"],
        )
        q_depths.add_metric(["mqtt_publish"], float(state.mqtt_publish_queue.qsize()))
        q_depths.add_metric(["console_tx"], float(len(state.console_to_mcu_queue)))

        q_depths.add_metric(["mailbox_tx"], float(state.mailbox_queue_depth()))
        q_depths.add_metric(["mailbox_rx"], float(state.mailbox_incoming_queue_depth()))

        q_depths.add_metric(["pending_digital_read"], float(len(state.pending_digital_reads)))
        q_depths.add_metric(["pending_analog_read"], float(len(state.pending_analog_reads)))
        q_depths.add_metric(["running_process"], float(len(state.running_processes)))
        yield q_depths

        # 2. System Status (Gauges)
        fs_usage = GaugeMetricFamily(
            "mcubridge_file_storage_bytes_used",
            "Current filesystem usage in bytes (volatile storage)",
        )
        fs_usage.add_metric([], float(state.file_storage_bytes_used))
        yield fs_usage

        link_sync = GaugeMetricFamily(
            "mcubridge_link_synchronized",
            "Binary status of serial link synchronization (1=sync, 0=unsync)",
        )
        link_sync.add_metric([], 1.0 if state.is_synchronized else 0.0)
        yield link_sync

        # 4. Supervisor Health (Dimensional)
        super_health = GaugeMetricFamily(
            "mcubridge_supervisor_worker_restarts",
            "Total restarts per internal worker task",
            labels=["worker"],
        )
        # [SIL-2] Iterative reduction
        [super_health.add_metric([k], float(v.restarts)) for k, v in state.supervisor_stats.items()]
        yield super_health


class PrometheusExporter:
    """Expose RuntimeState snapshots via the official Prometheus HTTP server."""

    def __init__(self, state: RuntimeState, host: str, port: int) -> None:
        from prometheus_client import ProcessCollector
        import prometheus_client
        from wsgiref.simple_server import make_server

        pc: Any = prometheus_client
        make_wsgi_app: Any = pc.make_wsgi_app

        self._state: RuntimeState | None = state
        self._host = host if host else "0.0.0.0"
        self._port = port
        self._registry = state.metrics.registry
        self._server: Any = None
        self._collector: RuntimeStateCollector | None = RuntimeStateCollector(state)

        # [SIL-2 / Library-First] Use native ProcessCollector to get CPU/RAM/FDs for free
        ProcessCollector(registry=self._registry)

        # Register the dynamic state collector
        self._registry.register(self._collector)

        # [Library-First] Use official prometheus_client WSGI app factory.
        # Provides content negotiation, gzip, OPTIONS/405 handling, and name[] filtering.
        self._server = make_server(
            self._host,
            self._port,
            make_wsgi_app(registry=self._registry),
        )

    @property
    def port(self) -> int:
        """Return the actually bound port (useful for port 0)."""
        if self._server:
            # server_address is (host, port)
            return int(self._server.server_address[1])
        return self._port

    async def run(self) -> None:
        """Start the Prometheus HTTP server and keep it running."""
        log = logger.bind(host=self._host, port=self.port)
        log.info("Prometheus exporter starting (official make_server)")

        try:
            # We use an executor to run the blocking serve_forever()
            # while maintaining the asyncio task alive for signal handling.
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._server.serve_forever)
        except asyncio.CancelledError:
            log.info("Prometheus exporter shutdown requested.")
            raise
        finally:
            # Unregister the collector to break circular reference
            if self._server and self._collector:
                try:
                    self._registry.unregister(self._collector)
                except KeyError:
                    logger.debug("Collector already unregistered from registry")

            # Shutdown stops the serve_forever loop
            if self._server:
                self._server.shutdown()
                # server_close releases the socket (avoids ResourceWarning)
                self._server.server_close()

            # Help GC by clearing references
            self._state = None
            self._collector = None
            self._server = None
            log.info("Prometheus exporter stopped")


def _build_bridge_snapshot_message(
    state: RuntimeState,
    flavor: str,
    snapshot: Any,
) -> pb.MqttQueuedPublish:
    segments: Sequence[str] = (
        ("bridge", "handshake", "value") if flavor == "handshake" else ("bridge", "summary", "value")
    )
    topic = topic_path(
        state.mqtt_topic_prefix,
        Topic.SYSTEM,
        *segments,
    )
    return create_queued_publish(
        topic_name=topic,
        payload=snapshot.SerializeToString(),
        content_type=PROTOBUF_CONTENT_TYPE,
        message_expiry_interval=_BRIDGE_SNAPSHOT_EXPIRY_SECONDS,
        user_properties=(("bridge-snapshot", flavor),),
    )


__all__ = [
    "PrometheusExporter",
    "publish_bridge_snapshots",
    "publish_metrics",
]

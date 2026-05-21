"""Periodic metrics publisher and Prometheus exporter for MCU Bridge."""

from __future__ import annotations

import asyncio
import contextlib
import math
import weakref
from collections.abc import Awaitable, Callable, Iterable, Sequence
from typing import (
    Any,
    cast,
)
from wsgiref.types import WSGIApplication

import msgspec
from prometheus_client.core import Metric
from prometheus_client.registry import Collector
import structlog

from .protocol import structures
from .protocol.structures import PROTOBUF_CONTENT_TYPE, QueuedPublish
from .protocol.topics import Topic, topic_path
from .state.context import RuntimeState

logger = structlog.get_logger("mcubridge.metrics")
_BRIDGE_SNAPSHOT_EXPIRY_SECONDS = 30

PublishEnqueue = Callable[[QueuedPublish], Awaitable[None]]


def _build_metrics_message(
    state: RuntimeState,
    snapshot: dict[str, Any],
    *,
    expiry_seconds: float,
) -> QueuedPublish:
    topic = topic_path(
        state.mqtt_topic_prefix,
        Topic.SYSTEM,
        "metrics",
    )
    message = QueuedPublish(
        topic_name=topic,
        payload=structures.encode_structured_payload(snapshot),
        content_type=PROTOBUF_CONTENT_TYPE,
        message_expiry_interval=int(expiry_seconds),
        user_properties=(),
    )

    mqtt_spool_failure = snapshot.get("mqtt_spool_failure_reason")
    extra_props: list[tuple[str, str]] = []
    if snapshot.get("mqtt_spool_degraded"):
        extra_props.append(("bridge-spool", mqtt_spool_failure or "unknown"))

    file_status = next(
        (
            label
            for key, label in (
                ("file_storage_limit_rejections", "quota-blocked"),
                ("file_write_limit_rejections", "write-limit"),
            )
            if (val := snapshot.get(key)) is not None and isinstance(val, (int, float)) and val > 0
        ),
        None,
    )
    if file_status is not None:
        extra_props.append(("bridge-files", file_status))

    if snapshot.get("watchdog_enabled") is not None:
        enabled = bool(snapshot.get("watchdog_enabled"))
        extra_props.append(("bridge-watchdog-enabled", "1" if enabled else "0"))
        watchdog_interval = snapshot.get("watchdog_interval")
        if isinstance(watchdog_interval, (int, float)):
            extra_props.append(("bridge-watchdog-interval", str(watchdog_interval)))

    if extra_props:
        message = msgspec.structs.replace(
            message,
            user_properties=(*message.user_properties, *extra_props),
        )

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
        except (OSError, RuntimeError, msgspec.MsgspecError) as e:
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
                    except (OSError, RuntimeError, msgspec.MsgspecError) as e:
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
                    except (OSError, RuntimeError, msgspec.MsgspecError) as e:
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

        # 3. System Health (Dimensional)
        from .state.context import collect_system_metrics

        health = GaugeMetricFamily(
            "mcubridge_system_health",
            "System-level resource utilization metrics",
            labels=["resource"],
        )
        sys_metrics = collect_system_metrics()
        # [SIL-2] Iterative reduction: filter and add metrics without raw for-loops
        [health.add_metric([k], float(v)) for k, v in sys_metrics.items() if isinstance(v, (int, float))]
        yield health

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
        from prometheus_client import CONTENT_TYPE_LATEST, ProcessCollector, generate_latest
        from wsgiref.simple_server import make_server

        self._state = state
        self._host = host if host else "0.0.0.0"
        self._port = port
        self._registry = state.metrics.registry
        self._server: Any = None
        self._collector = RuntimeStateCollector(state)

        # [SIL-2 / Library-First] Use native ProcessCollector to get CPU/RAM/FDs for free
        ProcessCollector(registry=self._registry)

        # Register the dynamic state collector
        self._registry.register(self._collector)

        # [Library-First] Use direct registry rendering with native prometheus APIs.
        def _app(environ: dict[str, Any], start_response: Callable[..., Any]) -> list[bytes]:
            payload = generate_latest(self._registry)
            start_response("200 OK", [("Content-Type", CONTENT_TYPE_LATEST)])

            # [SIL-2] Root-cause fix: diskcache creates thread-local sqlite3 connections
            # when read from this WSGI thread. Close them to prevent ResourceWarnings
            # when the diskcache object is destroyed.
            with contextlib.suppress(Exception):
                if self._state is not None:
                    mq: Any = self._state.mailbox_queue
                    if hasattr(mq, "cache"):
                        cache: Any = mq.cache
                        local: Any = getattr(cache, "_local", None)
                        if local and hasattr(local, "con"):
                            local.con.close()
                            del local.con
                    miq: Any = self._state.mailbox_incoming_queue
                    if hasattr(miq, "cache"):
                        cache: Any = miq.cache
                        local: Any = getattr(cache, "_local", None)
                        if local and hasattr(local, "con"):
                            local.con.close()
                            del local.con

            return [payload]

        app: WSGIApplication = cast(WSGIApplication, _app)

        self._server = make_server(
            self._host,
            self._port,
            app,
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
                with contextlib.suppress(KeyError):
                    self._registry.unregister(self._collector)

            # Shutdown stops the serve_forever loop
            if self._server:
                self._server.shutdown()
                # server_close releases the socket (avoids ResourceWarning)
                self._server.server_close()

            # Help GC by clearing references
            self._state = None  # type: ignore
            self._collector = None  # type: ignore
            self._server = None
            log.info("Prometheus exporter stopped")


def _build_bridge_snapshot_message(
    state: RuntimeState,
    flavor: str,
    snapshot: Any,
) -> QueuedPublish:
    segments: Sequence[str] = (
        ("bridge", "handshake", "value") if flavor == "handshake" else ("bridge", "summary", "value")
    )
    topic = topic_path(
        state.mqtt_topic_prefix,
        Topic.SYSTEM,
        *segments,
    )
    return QueuedPublish(
        topic_name=topic,
        payload=structures.encode_structured_payload(snapshot),
        content_type=PROTOBUF_CONTENT_TYPE,
        message_expiry_interval=_BRIDGE_SNAPSHOT_EXPIRY_SECONDS,
        user_properties=(("bridge-snapshot", flavor),),
    )


__all__ = [
    "PrometheusExporter",
    "publish_bridge_snapshots",
    "publish_metrics",
]

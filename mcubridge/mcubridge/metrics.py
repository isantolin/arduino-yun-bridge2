"""Periodic metrics publisher and Prometheus exporter for MCU Bridge."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Iterable, Sequence
from typing import (
    Any,
    cast,
)

import msgspec
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from prometheus_client.core import Metric
from prometheus_client.registry import Collector
import structlog

from .protocol.structures import QueuedPublish
from .protocol.topics import Topic, topic_path
from .state.context import RuntimeState

logger = structlog.get_logger("mcubridge.metrics")
_BRIDGE_SNAPSHOT_EXPIRY_SECONDS = 30
_msgpack_enc = msgspec.msgpack.Encoder()

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
        # [SIL-2] Fast serialization using msgspec.msgpack.encode handles Structs directly
        payload=_msgpack_enc.encode(snapshot),
        content_type="application/msgpack",
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
        self._state = state

    def collect(self) -> Iterable[Metric]:
        """Collect dimensional metrics from the current daemon state."""
        from prometheus_client.core import GaugeMetricFamily

        # 1. Queue Depths (Dimensional)
        q_depths = GaugeMetricFamily(
            "mcubridge_queue_depth",
            "Current number of items in internal asynchronous queues",
            labels=["queue"],
        )
        q_depths.add_metric(["mqtt_publish"], float(self._state.mqtt_publish_queue.qsize()))
        q_depths.add_metric(["console_tx"], float(len(self._state.console_to_mcu_queue)))
        q_depths.add_metric(["mailbox_tx"], float(len(self._state.mailbox_queue)))
        q_depths.add_metric(["mailbox_rx"], float(len(self._state.mailbox_incoming_queue)))
        q_depths.add_metric(["pending_digital_read"], float(len(self._state.pending_digital_reads)))
        q_depths.add_metric(["pending_analog_read"], float(len(self._state.pending_analog_reads)))
        q_depths.add_metric(["running_process"], float(len(self._state.running_processes)))
        yield q_depths

        # 2. System Status (Gauges)
        fs_usage = GaugeMetricFamily(
            "mcubridge_file_storage_bytes_used",
            "Current filesystem usage in bytes (volatile storage)",
        )
        fs_usage.add_metric([], float(self._state.file_storage_bytes_used))
        yield fs_usage

        link_sync = GaugeMetricFamily(
            "mcubridge_link_synchronized",
            "Binary status of serial link synchronization (1=sync, 0=unsync)",
        )
        link_sync.add_metric([], 1.0 if self._state.is_synchronized else 0.0)
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
        list(
            map(
                lambda item: health.add_metric([item[0]], float(item[1])),
                filter(lambda item: isinstance(item[1], (int, float)), sys_metrics.items()),
            )
        )
        yield health

        # 4. Supervisor Health (Dimensional)
        super_health = GaugeMetricFamily(
            "mcubridge_supervisor_worker_restarts",
            "Total restarts per internal worker task",
            labels=["worker"],
        )
        # [SIL-2] Iterative reduction
        list(
            map(
                lambda item: super_health.add_metric([item[0]], float(item[1].restarts)),
                self._state.supervisor_stats.items(),
            )
        )
        yield super_health


class PrometheusExporter:
    """Expose RuntimeState snapshots via the Prometheus text format."""

    def __init__(self, state: RuntimeState, host: str, port: int) -> None:
        from prometheus_client import ProcessCollector

        self._state = state
        # Defensive normalization for tests/injected configs.
        self._host = host if host else "127.0.0.1"
        self._port = port
        self._server: asyncio.AbstractServer | None = None
        self._resolved_port: int | None = None
        self._registry = state.metrics.registry

        # [SIL-2 / Library-First] Use native ProcessCollector to get CPU/RAM/FDs for free
        ProcessCollector(registry=self._registry)

        # Register the dynamic state collector
        self._registry.register(RuntimeStateCollector(state))

    @property
    def port(self) -> int:
        return self._resolved_port or self._port

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(
            self._handle_client,
            host=self._host,
            port=self._port,
        )
        sockets = self._server.sockets or []
        if sockets:
            sockname = sockets[0].getsockname()
            if isinstance(sockname, tuple):
                typed_sockname = cast(tuple[object, ...], sockname)
                if len(typed_sockname) >= 2:
                    port_candidate = typed_sockname[1]
                    if isinstance(port_candidate, int):
                        self._resolved_port = port_candidate
        logger.info(
            "Prometheus exporter listening",
            host=self._host,
            port=self.port,
        )

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None
        logger.info("Prometheus exporter stopped")

    async def run(self) -> None:
        await self.start()
        assert self._server is not None
        try:
            await self._server.serve_forever()
        except asyncio.CancelledError:
            raise
        finally:
            await self.stop()

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        phrases = {200: "OK", 400: "Bad Request", 404: "Not Found"}

        def _respond(status: int, body: bytes, *, content_type: str = "text/plain; charset=utf-8") -> None:
            status_line = f"HTTP/1.1 {status} {phrases.get(status, 'Error')}\r\n"
            headers = f"Content-Type: {content_type}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n"
            writer.write(status_line.encode("ascii") + headers.encode("ascii") + body)

        try:
            request_data = await reader.readuntil(b"\r\n\r\n")
            request_line = request_data.split(b"\r\n", 1)[0]
            parts = request_line.decode("ascii", errors="ignore").split()
            if len(parts) < 2:
                _respond(400, b"")
                await writer.drain()
                return
            method, path = parts[0], parts[1]
            if method != "GET" or path not in {"/metrics", "/"}:
                _respond(404, b"")
                await writer.drain()
                return
            payload = generate_latest(self._registry)
            _respond(200, payload, content_type=CONTENT_TYPE_LATEST)
            await writer.drain()
        except asyncio.CancelledError:
            raise
        except (OSError, ValueError, IndexError) as e:
            logger.warning("Prometheus client request error: %s", e)
        except (TypeError, AttributeError, RuntimeError) as e:
            logger.critical("Unexpected error in Prometheus handler: %s", e, exc_info=True)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except (OSError, ValueError, RuntimeError):
                # [SIL-2] Connection close errors are non-fatal during cleanup.
                logger.debug("Error closing metrics client connection", exc_info=True)


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
        payload=_msgpack_enc.encode(snapshot),
        content_type="application/msgpack",
        message_expiry_interval=_BRIDGE_SNAPSHOT_EXPIRY_SECONDS,
        user_properties=(("bridge-snapshot", flavor),),
    )


__all__ = [
    "PrometheusExporter",
    "publish_bridge_snapshots",
    "publish_metrics",
]

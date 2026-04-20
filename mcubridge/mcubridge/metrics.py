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
    if snapshot.get("mqtt_spool_degraded"):
        message = _with_user_property(
            message,
            "bridge-spool",
            mqtt_spool_failure or "unknown",
        )

    file_status = _file_status_property(snapshot)
    if file_status is not None:
        message = _with_user_property(
            message,
            "bridge-files",
            file_status,
        )

    if snapshot.get("watchdog_enabled") is not None:
        enabled = bool(snapshot.get("watchdog_enabled"))
        message = _with_user_property(
            message,
            "bridge-watchdog-enabled",
            "1" if enabled else "0",
        )
        watchdog_interval = snapshot.get("watchdog_interval")
        if isinstance(watchdog_interval, (int, float)):
            message = _with_user_property(
                message,
                "bridge-watchdog-interval",
                str(watchdog_interval),
            )

    return message


def _with_user_property(
    message: QueuedPublish,
    key: str,
    value: str,
) -> QueuedPublish:
    user_properties = list(message.user_properties)
    user_properties.append((key, value))
    return msgspec.structs.replace(
        message,
        user_properties=tuple(user_properties),
    )


def _file_status_property(snapshot: dict[str, Any]) -> str | None:
    checks = (
        ("file_storage_limit_rejections", "quota-blocked"),
        ("file_write_limit_rejections", "write-limit"),
    )
    return next(
        (
            label
            for key, label in checks
            if (val := snapshot.get(key)) is not None and isinstance(val, (int, float)) and val > 0
        ),
        None,
    )


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

    tick_seconds = _normalize_interval(interval, min_interval)
    if tick_seconds is None:
        raise ValueError("interval must be greater than zero")
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

    summary_seconds = _normalize_interval(summary_interval, min_interval)
    handshake_seconds = _normalize_interval(handshake_interval, min_interval)

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
        for key, val in sys_metrics.items():
            if isinstance(val, (int, float)):
                health.add_metric([key], float(val))
        yield health

        # 4. Supervisor Health (Dimensional)
        super_health = GaugeMetricFamily(
            "mcubridge_supervisor_worker_restarts",
            "Total restarts per internal worker task",
            labels=["worker"],
        )
        for name, stats in self._state.supervisor_stats.items():
            super_health.add_metric([name], float(stats.restarts))
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

        # [OPTIMIZATION] Initialize native prometheus Summary for percentiles
        state.serial_latency_stats.initialize_prometheus(self._registry)

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
        try:
            request_line = await reader.readline()
            if not request_line:
                return
            parts = request_line.decode("ascii", errors="ignore").split()
            if len(parts) < 2:
                await self._write_response(writer, 400, b"")
                return
            method, path = parts[0], parts[1]

            # Read and discard headers until empty line
            while line := await reader.readline():
                if line in {b"\r\n", b"\n"}:
                    break

            if method != "GET" or path not in {"/metrics", "/"}:
                await self._write_response(writer, 404, b"")
                return
            payload = self._render_metrics()
            await self._write_response(
                writer,
                200,
                payload,
                content_type=CONTENT_TYPE_LATEST,
            )
        except asyncio.CancelledError:
            raise
        except (OSError, ValueError, IndexError) as e:
            logger.warning("Prometheus client request error: %s", e)
        except (TypeError, ValueError, AttributeError, OSError, RuntimeError) as e:
            logger.critical("Unexpected error in Prometheus handler: %s", e, exc_info=True)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except (OSError, ValueError, RuntimeError):
                # [SIL-2] Connection close errors are non-fatal during cleanup.
                # Log at debug level to avoid noise during normal shutdown.
                logger.debug("Error closing metrics client connection", exc_info=True)

    async def _write_response(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        body: bytes,
        *,
        content_type: str = "text/plain; charset=utf-8",
    ) -> None:
        phrases = {
            200: "OK",
            400: "Bad Request",
            404: "Not Found",
        }
        status_line = f"HTTP/1.1 {status} {phrases.get(status, 'Error')}\r\n"
        headers = f"Content-Type: {content_type}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n"
        writer.write(status_line.encode("ascii") + headers.encode("ascii") + body)
        await writer.drain()

    def _render_metrics(self) -> bytes:
        return generate_latest(self._registry)


def _build_bridge_snapshot_message(
    state: RuntimeState,
    flavor: str,
    snapshot: Any,
) -> QueuedPublish:
    segments: Sequence[str]
    if flavor == "handshake":
        segments = ("bridge", "handshake", "value")
    else:
        segments = ("bridge", "summary", "value")
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


def _normalize_interval(interval: float, min_interval: float) -> int | None:
    """Normalize interval to a positive integer or None."""
    return max(1, math.ceil(max(min_interval, interval))) if interval > 0 else None


__all__ = [
    "PrometheusExporter",
    "publish_bridge_snapshots",
    "publish_metrics",
]

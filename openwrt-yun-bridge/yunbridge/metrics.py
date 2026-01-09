"""Periodic metrics publisher and Prometheus exporter for Yun Bridge."""

from __future__ import annotations

import asyncio
import logging
import math
import re
import json
from dataclasses import replace
from typing import TYPE_CHECKING
from typing import (
    Any,
    cast,
)
from collections.abc import Awaitable, Callable, Iterator, Sequence

try:
    import ujson  # type: ignore
except ImportError:  # pragma: no cover
    ujson = None  # type: ignore[assignment]

from .protocol.topics import Topic, topic_path
from .mqtt.messages import QueuedPublish
from .state.context import RuntimeState

logger = logging.getLogger("yunbridge.metrics")


if TYPE_CHECKING:
    from prometheus_client.registry import Collector
else:
    class Collector:  # pragma: no cover
        pass

_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9_]")
_INFO_METRIC = "yunbridge_info"
_GAUGE_DOC = "YunBridge auto-generated metric"
_INFO_DOC = "YunBridge informational metric"
_BRIDGE_SNAPSHOT_EXPIRY_SECONDS = 30

PublishEnqueue = Callable[[QueuedPublish], Awaitable[None]]


def _json_dumps(value: Any) -> str:
    if ujson is not None:
        return ujson.dumps(value)
    return json.dumps(value)


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
        payload=_json_dumps(snapshot).encode("utf-8"),
        content_type="application/json",
        message_expiry_interval=int(expiry_seconds),
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
    return replace(
        message,
        user_properties=message.user_properties + ((key, value),),
    )


def _file_status_property(snapshot: dict[str, Any]) -> str | None:
    if _is_positive_number(snapshot.get("file_storage_limit_rejections")):
        return "quota-blocked"
    if _is_positive_number(snapshot.get("file_write_limit_rejections")):
        return "write-limit"
    return None


def _is_positive_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and value > 0


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
        snapshot = (
            state.build_handshake_snapshot()
            if flavor == "handshake"
            else state.build_bridge_snapshot()
        )
        await enqueue(
            _build_bridge_snapshot_message(
                state,
                flavor,
                snapshot,
            )
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            "Failed to publish bridge snapshot",
            extra={"flavor": flavor},
        )


async def _bridge_snapshot_loop(
    state: RuntimeState,
    enqueue: PublishEnqueue,
    *,
    flavor: str,
    seconds: int,
) -> None:
    # Initial emit
    try:
        await _emit_bridge_snapshot(state, enqueue, flavor)
    except Exception:
        # Already logged in _emit_bridge_snapshot
        pass

    while True:
        await asyncio.sleep(seconds)
        await _emit_bridge_snapshot(state, enqueue, flavor)


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

    # Initial emit
    try:
        await _emit_metrics_snapshot(state, enqueue, expiry_seconds=expiry)
    except asyncio.CancelledError:
        logger.info("Metrics publisher cancelled.")
        raise
    except Exception:
        logger.exception("Failed to publish initial metrics payload")

    # Loop
    try:
        while True:
            await asyncio.sleep(tick_seconds)
            try:
                await _emit_metrics_snapshot(state, enqueue, expiry_seconds=expiry)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Failed to publish metrics payload")
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

    try:
        async with asyncio.TaskGroup() as tg:
            if summary_seconds is not None:
                tg.create_task(
                    _bridge_snapshot_loop(
                        state,
                        enqueue,
                        flavor="summary",
                        seconds=summary_seconds,
                    )
                )
            if handshake_seconds is not None:
                tg.create_task(
                    _bridge_snapshot_loop(
                        state,
                        enqueue,
                        flavor="handshake",
                        seconds=handshake_seconds,
                    )
                )
    except* asyncio.CancelledError:
        logger.info("Bridge snapshot publisher cancelled.")
        raise
    except* Exception:
        # Individual loop errors are caught inside _emit/_loop, but if something
        # escapes or TaskGroup raises, we log it.
        logger.critical("Fatal error in bridge snapshot publisher", exc_info=True)
        raise


class _RuntimeStateCollector(Collector):
    """Prometheus collector that projects RuntimeState snapshots.

    [EXTENDED METRICS] Supports histogram buckets for latency metrics.
    """

    def __init__(self, state: RuntimeState) -> None:
        self._state = state

    def collect(self) -> Iterator[Any]:
        # pragma: no cover - exercised via exporter
        from prometheus_client.core import (
            GaugeMetricFamily,
            HistogramMetricFamily,
            InfoMetricFamily,
        )

        snapshot = self._state.build_metrics_snapshot()

        # [EXTENDED METRICS] Emit latency histogram if present
        latency_data = snapshot.get("serial_latency")
        if isinstance(latency_data, dict) and latency_data.get("count", 0) > 0:
            yield from self._emit_latency_histogram(latency_data, HistogramMetricFamily)

        info_values: list[tuple[str, str]] = []
        for metric_type, name, value in self._flatten(
            "yunbridge",
            snapshot,
        ):
            if metric_type == "gauge":
                metric = GaugeMetricFamily(
                    _sanitize_metric_name(name),
                    _GAUGE_DOC,
                )
                metric.add_metric((), value)
                yield metric
            else:
                info_values.append((name, value))
        if info_values:
            info_metric = InfoMetricFamily(
                _INFO_METRIC,
                _INFO_DOC,
                labels=("key",),
            )
            for key, value in info_values:
                info_metric.add_metric(
                    (key,),
                    {"value": value},
                )
            yield info_metric

    def _emit_latency_histogram(
        self,
        latency_data: dict[str, Any],
        HistogramMetricFamily: type,
    ) -> Iterator[Any]:
        """Emit Prometheus histogram for RPC latency."""
        buckets_dict = latency_data.get("buckets", {})
        if not isinstance(buckets_dict, dict):
            return

        # Build bucket list: [(upper_bound, cumulative_count), ...]
        bucket_list: list[tuple[float, int]] = []
        for key, count in buckets_dict.items():
            if key.startswith("le_") and key.endswith("ms"):
                try:
                    bound_ms = float(key[3:-2])  # Extract number from "le_Xms"
                    bound_s = bound_ms / 1000.0  # Convert to seconds
                    bucket_list.append((bound_s, int(count)))
                except (ValueError, TypeError):
                    continue

        bucket_list.sort(key=lambda x: x[0])

        if not bucket_list:
            return

        total_count = latency_data.get("count", 0)
        total_sum = latency_data.get("sum_ms", 0.0) / 1000.0  # Convert to seconds

        # Add +Inf bucket
        bucket_list.append((float("inf"), int(total_count)))

        histogram = HistogramMetricFamily(
            "yunbridge_serial_rpc_latency_seconds",
            "RPC command round-trip latency histogram",
        )
        histogram.add_metric(
            [],
            buckets=bucket_list,
            sum_value=total_sum,
        )
        yield histogram

    def _flatten(
        self,
        prefix: str,
        value: Any,
    ) -> Iterator[tuple[str, str, Any]]:
        if isinstance(value, dict):
            typed_dict = cast(dict[Any, Any], value)
            for raw_key, sub_value in typed_dict.items():
                key = raw_key if isinstance(raw_key, str) else str(raw_key)
                next_prefix = f"{prefix}_{key}" if prefix else key
                yield from self._flatten(next_prefix, sub_value)
            return
        if isinstance(value, bool):
            yield ("gauge", prefix, 1.0 if value else 0.0)
            return
        if isinstance(value, (int, float)):
            yield ("gauge", prefix, float(value))
            return
        if value is None:
            yield ("info", prefix, "null")
            return
        yield ("info", prefix, str(value))


class PrometheusExporter:
    """Expose RuntimeState snapshots via the Prometheus text format."""

    def __init__(self, state: RuntimeState, host: str, port: int) -> None:
        from prometheus_client import CollectorRegistry

        self._state = state
        self._host = host
        self._port = port
        self._server: asyncio.AbstractServer | None = None
        self._resolved_port: int | None = None
        self._registry = CollectorRegistry()
        self._collector = _RuntimeStateCollector(state)
        self._registry.register(self._collector)

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
            extra={"host": self._host, "port": self.port},
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
            while True:
                line = await reader.readline()
                if not line or line in {b"\r\n", b"\n"}:
                    break
            if method != "GET" or path not in {"/metrics", "/"}:
                await self._write_response(writer, 404, b"")
                return
            payload = self._render_metrics()
            from prometheus_client import CONTENT_TYPE_LATEST
            await self._write_response(
                writer,
                200,
                payload,
                content_type=CONTENT_TYPE_LATEST,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Prometheus handler error")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                logger.debug("Error closing metrics client", exc_info=True)

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
        headers = (
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        )
        writer.write(status_line.encode("ascii") + headers.encode("ascii") + body)
        await writer.drain()

    def _render_metrics(self) -> bytes:
        from prometheus_client import generate_latest

        return generate_latest(self._registry)


def _sanitize_metric_name(name: str) -> str:
    cleaned = _SANITIZE_RE.sub("_", name.lower())
    cleaned = cleaned.strip("_") or "yunbridge_metric"
    if cleaned[0].isdigit():
        cleaned = f"_{cleaned}"
    return cleaned


def _build_bridge_snapshot_message(
    state: RuntimeState,
    flavor: str,
    snapshot: dict[str, Any],
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
        payload=_json_dumps(snapshot).encode("utf-8"),
        content_type="application/json",
        message_expiry_interval=_BRIDGE_SNAPSHOT_EXPIRY_SECONDS,
        user_properties=(("bridge-snapshot", flavor),),
    )


def _normalize_interval(
    interval: float,
    min_interval: float,
) -> int | None:
    if interval <= 0:
        return None
    tick = max(min_interval, interval)
    return max(1, math.ceil(tick))


__all__ = [
    "PrometheusExporter",
    "publish_bridge_snapshots",
    "publish_metrics",
]

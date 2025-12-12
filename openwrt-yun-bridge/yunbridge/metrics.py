"""Periodic metrics publisher and Prometheus exporter for Yun Bridge."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from dataclasses import replace
from typing import (
    Any,
    cast,
)
from collections.abc import Awaitable, Callable, Iterator, Sequence

import aiocron
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    generate_latest,
)
from prometheus_client.registry import Collector
from prometheus_client.core import GaugeMetricFamily, InfoMetricFamily

from .protocol.topics import Topic, topic_path
from .mqtt.messages import QueuedPublish
from .state.context import RuntimeState

logger = logging.getLogger("yunbridge.metrics")

_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9_]")
_INFO_METRIC = "yunbridge_info"
_GAUGE_DOC = "YunBridge auto-generated metric"
_INFO_DOC = "YunBridge informational metric"
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
        payload=json.dumps(snapshot).encode("utf-8"),
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

    async def _emit_snapshot() -> None:
        snapshot = state.build_metrics_snapshot()
        await enqueue(
            _build_metrics_message(
                state,
                snapshot,
                expiry_seconds=expiry,
            )
        )

    async def _tick() -> None:
        try:
            await _emit_snapshot()
        except asyncio.CancelledError:
            logger.info("Metrics publisher cancelled.")
            raise
        except Exception:
            logger.exception("Failed to publish metrics payload")

    cron: aiocron.Cron | None = None
    blocker = asyncio.Event()
    cron_spec = _cron_expression_from_interval(tick_seconds)

    try:
        await _tick()
        cron = aiocron.crontab(
            cron_spec,
            func=_tick,
            start=False,
            loop=asyncio.get_running_loop(),
        )
        cron.start()
        await blocker.wait()
    except asyncio.CancelledError:
        raise
    finally:
        if cron is not None:
            cron.stop()


def _cron_expression_from_interval(seconds: float) -> str:
    """Render a 6-field cron expression that fires every *seconds*."""

    rounded = max(1, int(math.ceil(seconds)))
    return f"*/{rounded} * * * * *"


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
        logger.info("Bridge snapshot cron disabled; awaiting cancellation.")
        blocker = asyncio.Event()
        try:
            await blocker.wait()
        except asyncio.CancelledError:
            raise
        return

    crons: list[aiocron.Cron] = []
    blocker = asyncio.Event()

    async def _emit(flavor: str) -> None:
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

    def _tick_factory(flavor: str) -> Callable[[], Awaitable[None]]:
        async def _tick() -> None:
            await _emit(flavor)

        return _tick

    async def _schedule(flavor: str, seconds: int) -> None:
        await _emit(flavor)
        cron = aiocron.crontab(
            _cron_expression_from_interval(seconds),
            func=_tick_factory(flavor),
            start=False,
            loop=asyncio.get_running_loop(),
        )
        cron.start()
        crons.append(cron)

    try:
        if summary_seconds is not None:
            await _schedule("summary", summary_seconds)
        if handshake_seconds is not None:
            await _schedule("handshake", handshake_seconds)
        await blocker.wait()
    except asyncio.CancelledError:
        raise
    finally:
        for cron in crons:
            cron.stop()


class _RuntimeStateCollector(Collector):
    """Prometheus collector that projects RuntimeState snapshots."""

    def __init__(self, state: RuntimeState) -> None:
        self._state = state

    def collect(self) -> Iterator[Any]:
        # pragma: no cover - exercised via exporter
        snapshot = self._state.build_metrics_snapshot()
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
        payload=json.dumps(snapshot).encode("utf-8"),
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

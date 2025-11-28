"""Periodic metrics publisher and Prometheus exporter for Yun Bridge."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Dict, Iterable, Optional

from .protocol.topics import Topic, topic_path
from .mqtt import PublishableMessage
from .state.context import RuntimeState

logger = logging.getLogger("yunbridge.metrics")

_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9_]")
_INFO_METRIC = "yunbridge_info"


async def publish_metrics(
    state: RuntimeState,
    enqueue,
    interval: float,
    *,
    min_interval: float = 5.0,
) -> None:
    """Publish runtime metrics to MQTT at a fixed cadence."""

    tick = max(min_interval, interval)
    while True:
        try:
            snapshot = state.build_metrics_snapshot()
            payload = json.dumps(snapshot).encode("utf-8")
            topic = topic_path(
                state.mqtt_topic_prefix,
                Topic.SYSTEM,
                "metrics",
            )
            message = (
                PublishableMessage(topic_name=topic, payload=payload)
                .with_content_type("application/json")
                .with_message_expiry(int(tick * 2))
            )
            await enqueue(message)
        except asyncio.CancelledError:
            logger.info("Metrics publisher cancelled.")
            raise
        except Exception:
            logger.exception("Failed to publish metrics payload")
        await asyncio.sleep(tick)


class _PrometheusFormatter:
    def __init__(self) -> None:
        self._lines: list[str] = []
        self._declared: set[str] = set()

    def render(self) -> str:
        return "\n".join(self._lines) + "\n"

    def add_snapshot(self, snapshot: Dict[str, Any]) -> None:
        self._flatten("yunbridge", snapshot)

    def _flatten(self, prefix: str, value: Any) -> None:
        if isinstance(value, dict):
            for key, sub_value in value.items():
                next_prefix = f"{prefix}_{key}" if prefix else key
                self._flatten(next_prefix, sub_value)
            return
        if isinstance(value, (int, float)):
            self._emit_metric(prefix, float(value))
            return
        if isinstance(value, bool):
            self._emit_metric(prefix, 1.0 if value else 0.0)
            return
        if value is None:
            self._emit_info(prefix, "null")
            return
        self._emit_info(prefix, str(value))

    def _emit_metric(self, name: str, value: float) -> None:
        metric = _sanitize_metric_name(name)
        self._declare(metric)
        self._lines.append(f"{metric} {value}")

    def _emit_info(self, key: str, value: str) -> None:
        self._declare(_INFO_METRIC)
        label_key = _escape_label(key)
        label_value = _escape_label(value)
        self._lines.append(
            f'{_INFO_METRIC}{{key="{label_key}",value="{label_value}"}} 1'
        )

    def _declare(self, metric: str) -> None:
        if metric in self._declared:
            return
        metric_type = "gauge"
        self._lines.append(
            f"# HELP {metric} YunBridge auto-generated metric"
        )
        self._lines.append(f"# TYPE {metric} {metric_type}")
        self._declared.add(metric)


def _sanitize_metric_name(name: str) -> str:
    cleaned = _SANITIZE_RE.sub("_", name.lower())
    cleaned = cleaned.strip("_") or "yunbridge_metric"
    if cleaned[0].isdigit():
        cleaned = f"_{cleaned}"
    return cleaned


def _escape_label(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace('"', '\\"')
    )


class PrometheusExporter:
    """Expose RuntimeState snapshots via the Prometheus text format."""

    def __init__(self, state: RuntimeState, host: str, port: int) -> None:
        self._state = state
        self._host = host
        self._port = port
        self._server: Optional[asyncio.AbstractServer] = None
        self._resolved_port: Optional[int] = None

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
                self._resolved_port = int(sockname[1])
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
            payload = self._render_metrics().encode("utf-8")
            await self._write_response(
                writer,
                200,
                payload,
                content_type="text/plain; version=0.0.4; charset=utf-8",
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
        writer.write(
            status_line.encode("ascii") + headers.encode("ascii") + body
        )
        await writer.drain()

    def _render_metrics(self) -> str:
        formatter = _PrometheusFormatter()
        formatter.add_snapshot(self._state.build_metrics_snapshot())
        return formatter.render()


__all__ = ["publish_metrics", "PrometheusExporter"]

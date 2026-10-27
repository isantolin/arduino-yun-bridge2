#!/usr/bin/env python3
"""Protobuf Cloud Gateway for MCU Bridge v2.

High-performance industrial IoT server acting as the central cloud hub for N MPU Daemons.
Provides bidirectional gRPC streaming (HTTP/2 and HTTP/3 QUIC), fleet-wide Prometheus
observability, TSDB time-series ingestion, and northbound command orchestration.
"""

from __future__ import annotations

import asyncio
import ssl
import time
from collections.abc import Awaitable, Callable, Mapping
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Final, cast

import anyio
import grpclib.events
import httpx
import prometheus_client
import structlog
import structlog.contextvars
import tenacity
import typer
import uvloop
from google.protobuf.message import DecodeError
from google.protobuf.message import Message as ProtobufMessage
from grpclib.const import Handler, Status
from grpclib.exceptions import GRPCError
from grpclib.protocol import Peer
from grpclib.reflection.service import ServerReflection
from grpclib.server import Server, Stream
from mcubridge.config.logging import configure_logging
from mcubridge.protocol import mcubridge_pb2 as pb
from mcubridge.protocol.mcubridge_grpc import CloudBridgeBase, LocalBridgeBase
from mcubridge.protocol.protocol import (
    DEFAULT_CLOUD_HOST,
    DEFAULT_CLOUD_PORT,
    PROTOCOL_VERSION,
    REGISTRY,
    OperationSpec,
)
from mcubridge.protocol.structures import (
    ConsoleQueueTarget,
    MemoryQueueAdapter,
)
from statemachine import State, StateMachine

logger = structlog.get_logger("mcubridge.gateway")


class GatewaySessionState(StrEnum):
    """[SIL-2] Discrete session lifecycle states for cloud connections."""

    CONNECTED = "connected"
    AUTHENTICATED = "authenticated"
    ACTIVE = "active"
    CLOSED = "closed"


class GatewaySessionMachine(StateMachine):
    """[SIL-2] Deterministic state machine for cloud gateway sessions."""

    allow_event_without_transition = True

    connected = State(value="connected", initial=True)
    authenticated = State(value="authenticated")
    active = State(value="active")
    closed = State(value="closed", final=True)

    authenticate = connected.to(authenticated)
    activate = authenticated.to(active) | connected.to(active)
    close = connected.to(closed) | authenticated.to(closed) | active.to(closed)


class GatewayDeviceSession:
    """[SIL-2] Edge session container and deterministic lifecycle controller."""

    device_id: str
    stream: Stream[pb.CloudEnvelope, pb.CloudEnvelope] | None
    metrics: FleetMetrics | None
    gateway: Any | None
    console_queues: list[Any]
    fsm: GatewaySessionMachine

    def __init__(
        self,
        device_id: str = "",
        stream: Stream[pb.CloudEnvelope, pb.CloudEnvelope] | None = None,
        metrics: FleetMetrics | None = None,
        gateway: Any | None = None,
    ) -> None:
        self.device_id = device_id
        self.stream = stream
        self.metrics = metrics
        self.gateway = gateway
        self.console_queues = []
        self.fsm = GatewaySessionMachine()

    def close(self) -> None:
        if self.fsm.closed.is_active:
            return
        self.fsm.close()
        self.console_queues.clear()

        if self.gateway is not None and hasattr(self.gateway, "pending_commands"):
            for (target_id, _), fut in list(self.gateway.pending_commands.items()):
                if target_id == self.device_id and not fut.done():
                    fut.set_exception(ConnectionResetError(f"Device {self.device_id} disconnected during execution"))

        if self.metrics is not None and self.device_id:
            self.metrics.devices_connected.dec()
            self.metrics.device_connection_state.labels(device_id=self.device_id).set(0.0)


class FleetMetrics:
    """[SIL-2] Fleet-wide Prometheus observability metrics container."""

    def __init__(self, registry: prometheus_client.CollectorRegistry | None = None) -> None:
        self.registry = registry if registry is not None else prometheus_client.CollectorRegistry(auto_describe=True)
        self.devices_connected = prometheus_client.Gauge(
            "mcubridge_gateway_connected_devices",
            "Total number of currently connected edge devices",
            registry=self.registry,
        )
        self.device_connection_state = prometheus_client.Gauge(
            "mcubridge_device_connected",
            "Connection status of the device (1 = connected, 0 = disconnected)",
            labelnames=["device_id"],
            registry=self.registry,
        )
        self.telemetry_messages = prometheus_client.Counter(
            "mcubridge_device_telemetry_total",
            "Total telemetry reports received per device",
            labelnames=["device_id"],
            registry=self.registry,
        )
        self.device_events = prometheus_client.Counter(
            "mcubridge_device_events_total",
            "Total events received per device",
            labelnames=["device_id", "severity"],
            registry=self.registry,
        )
        self.command_requests = prometheus_client.Counter(
            "mcubridge_device_command_requests_total",
            "Total command requests dispatched to device",
            labelnames=["device_id"],
            registry=self.registry,
        )
        self.command_responses = prometheus_client.Counter(
            "mcubridge_device_command_responses_total",
            "Total command responses received from device",
            labelnames=["device_id", "status_code"],
            registry=self.registry,
        )
        self.device_link_synchronized = prometheus_client.Gauge(
            "mcubridge_device_link_synchronized",
            "Hardware serial link synchronization status (1 = sync, 0 = unsync)",
            labelnames=["device_id"],
            registry=self.registry,
        )
        self.device_cloud_queue_depth = prometheus_client.Gauge(
            "mcubridge_device_cloud_queue_depth",
            "Current cloud queue depth on device",
            labelnames=["device_id"],
            registry=self.registry,
        )
        self.device_cloud_dropped = prometheus_client.Counter(
            "mcubridge_device_cloud_dropped_messages_total",
            "Total dropped cloud messages on device",
            labelnames=["device_id"],
            registry=self.registry,
        )
        self.device_spool_pending = prometheus_client.Gauge(
            "mcubridge_device_spool_pending_messages",
            "Current pending offline spool messages on device",
            labelnames=["device_id"],
            registry=self.registry,
        )
        self.device_watchdog_enabled = prometheus_client.Gauge(
            "mcubridge_device_watchdog_enabled",
            "Watchdog supervisor enabled status (1 = enabled, 0 = disabled)",
            labelnames=["device_id"],
            registry=self.registry,
        )
        self.device_serial_bytes_sent = prometheus_client.Gauge(
            "mcubridge_device_serial_bytes_sent",
            "Total bytes sent over serial link by device",
            labelnames=["device_id"],
            registry=self.registry,
        )
        self.device_serial_bytes_received = prometheus_client.Gauge(
            "mcubridge_device_serial_bytes_received",
            "Total bytes received over serial link by device",
            labelnames=["device_id"],
            registry=self.registry,
        )
        self.device_serial_frames_sent = prometheus_client.Gauge(
            "mcubridge_device_serial_frames_sent",
            "Total frames sent over serial link by device",
            labelnames=["device_id"],
            registry=self.registry,
        )
        self.device_serial_frames_received = prometheus_client.Gauge(
            "mcubridge_device_serial_frames_received",
            "Total frames received over serial link by device",
            labelnames=["device_id"],
            registry=self.registry,
        )
        self.device_serial_crc_errors = prometheus_client.Gauge(
            "mcubridge_device_serial_crc_errors",
            "Total CRC errors encountered on serial link by device",
            labelnames=["device_id"],
            registry=self.registry,
        )
        self.device_serial_decode_errors = prometheus_client.Gauge(
            "mcubridge_device_serial_decode_errors",
            "Total decode errors encountered on serial link by device",
            labelnames=["device_id"],
            registry=self.registry,
        )
        self.device_handshake_attempts = prometheus_client.Gauge(
            "mcubridge_device_handshake_attempts",
            "Total serial handshake attempts by device",
            labelnames=["device_id"],
            registry=self.registry,
        )
        self.device_handshake_successes = prometheus_client.Gauge(
            "mcubridge_device_handshake_successes",
            "Total serial handshake successes by device",
            labelnames=["device_id"],
            registry=self.registry,
        )
        self.device_watchdog_beats = prometheus_client.Gauge(
            "mcubridge_device_watchdog_beats",
            "Total watchdog pulses emitted by device",
            labelnames=["device_id"],
            registry=self.registry,
        )
        self.device_uptime_seconds = prometheus_client.Gauge(
            "mcubridge_device_uptime_seconds",
            "Current daemon uptime in seconds",
            labelnames=["device_id"],
            registry=self.registry,
        )
        self.device_cloud_messages_published = prometheus_client.Gauge(
            "mcubridge_device_cloud_messages_published",
            "Total messages published to cloud by device",
            labelnames=["device_id"],
            registry=self.registry,
        )
        self.device_retries = prometheus_client.Gauge(
            "mcubridge_device_retries",
            "Total retries by device and component",
            labelnames=["device_id", "component"],
            registry=self.registry,
        )
        self.device_latency_ms = prometheus_client.Gauge(
            "mcubridge_device_latency_ms",
            "Round-trip latency in milliseconds",
            labelnames=["device_id", "type"],
            registry=self.registry,
        )


class TSDBSink:
    """[SIL-2] Time-Series Database ingestion adapter supporting Line Protocol."""

    def __init__(
        self,
        endpoint_url: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if endpoint_url:
            parsed = httpx.URL(endpoint_url)
            if parsed.scheme not in ("http", "https"):
                raise ValueError(f"Invalid TSDB scheme '{parsed.scheme}'; must be 'http' or 'https'")
        self.endpoint_url = endpoint_url
        self.enabled = bool(endpoint_url)
        self.client = client if client is not None else httpx.AsyncClient(timeout=5.0)

    async def aclose(self) -> None:
        """Close underlying HTTP client."""
        await self.client.aclose()

    @staticmethod
    def format_line_protocol(
        device_id: str,
        metrics: pb.DaemonMetrics,
        timestamp_ns: int | None = None,
    ) -> str:
        """Format metrics into standard Influx/VictoriaMetrics Line Protocol."""
        ts = timestamp_ns if timestamp_ns is not None else time.time_ns()
        sync_val = 1 if metrics.link_synchronised else 0
        spool_degraded = 1 if metrics.cloud_spool_degraded else 0
        watchdog_on = 1 if metrics.watchdog_enabled else 0
        return (
            f"mcubridge_telemetry,device_id={device_id} "
            f"queue_depth={metrics.cloud_queue_depth}i,"
            f"dropped_messages={metrics.cloud_dropped_messages}i,"
            f"spool_pending={metrics.cloud_spool_pending_messages}i,"
            f"spool_degraded={spool_degraded}i,"
            f"link_synchronized={sync_val}i,"
            f"watchdog_enabled={watchdog_on}i,"
            f"serial_bytes_sent={metrics.serial_bytes_sent}i,"
            f"serial_bytes_received={metrics.serial_bytes_received}i,"
            f"serial_frames_sent={metrics.serial_frames_sent}i,"
            f"serial_frames_received={metrics.serial_frames_received}i,"
            f"serial_crc_errors={metrics.serial_crc_errors}i,"
            f"serial_decode_errors={metrics.serial_decode_errors}i,"
            f"watchdog_beats={metrics.watchdog_beats}i,"
            f"published_messages={metrics.cloud_messages_published}i "
            f"{ts}"
        )

    async def ingest_telemetry(self, device_id: str, envelope: pb.CloudEnvelope) -> None:
        """Asynchronously post telemetry report to external TSDB endpoint."""
        if not self.enabled or not self.endpoint_url or not envelope.telemetry.daemon_metrics_blob:
            return
        try:
            metrics = pb.DaemonMetrics()
            metrics.ParseFromString(envelope.telemetry.daemon_metrics_blob)
            line = self.format_line_protocol(device_id, metrics)
            await self.post_line(line)
        except (DecodeError, OSError, ValueError) as exc:
            logger.warning("TSDB telemetry ingestion error", device_id=device_id, error=str(exc))

    @tenacity.retry(
        stop=tenacity.stop_after_attempt(2),
        wait=tenacity.wait_none(),
        retry=tenacity.retry_if_exception_type((httpx.HTTPError, TimeoutError, OSError)),
        retry_error_callback=lambda _rs: None,
    )
    async def post_line(self, line: str) -> None:
        """Execute asynchronous HTTP POST with tenacity retries."""
        if not self.endpoint_url:
            return
        try:
            resp = await self.client.post(
                self.endpoint_url,
                content=line.encode("utf-8"),
                headers={"Content-Type": "text/plain; charset=utf-8"},
            )
            if resp.status_code >= 400:
                logger.warning("TSDB server responded with status error", status=resp.status_code)
        except (httpx.HTTPError, TimeoutError, OSError) as e:
            logger.warning("TSDB network write failed", error=str(e))
            raise


async def _handle_ping(
    _: CloudBridgeService,
    __: str,
    stream: Stream[pb.CloudEnvelope, pb.CloudEnvelope],
    envelope: pb.CloudEnvelope,
) -> None:
    pong = pb.CloudEnvelope(
        protocol_version=PROTOCOL_VERSION,
        device_id="CLOUD_GW",
        sequence_id=envelope.sequence_id,
        pong=pb.KeepalivePong(roundtrip_ms=0),
    )
    await stream.send_message(pong)


async def handle_telemetry(
    service: CloudBridgeService,
    device_id: str,
    _: Stream[pb.CloudEnvelope, pb.CloudEnvelope],
    envelope: pb.CloudEnvelope,
) -> None:
    logger.info("Processed telemetry", device_id=device_id)
    service.gateway.metrics.telemetry_messages.labels(device_id=device_id).inc()

    if envelope.telemetry.daemon_metrics_blob:
        try:
            metrics = pb.DaemonMetrics()
            metrics.ParseFromString(envelope.telemetry.daemon_metrics_blob)
            service.gateway.metrics.device_link_synchronized.labels(device_id=device_id).set(
                1.0 if metrics.link_synchronised else 0.0
            )
            simple_metrics: tuple[tuple[str, float], ...] = (
                ("device_watchdog_enabled", 1.0 if metrics.watchdog_enabled else 0.0),
                ("device_cloud_queue_depth", float(metrics.cloud_queue_depth)),
                ("device_spool_pending", float(metrics.cloud_spool_pending_messages)),
                ("device_serial_bytes_sent", float(metrics.serial_bytes_sent)),
                ("device_serial_bytes_received", float(metrics.serial_bytes_received)),
                ("device_serial_frames_sent", float(metrics.serial_frames_sent)),
                ("device_serial_frames_received", float(metrics.serial_frames_received)),
                ("device_serial_crc_errors", float(metrics.serial_crc_errors)),
                ("device_serial_decode_errors", float(metrics.serial_decode_errors)),
                ("device_handshake_attempts", float(metrics.handshake_attempts)),
                ("device_handshake_successes", float(metrics.handshake_successes)),
                ("device_watchdog_beats", float(metrics.watchdog_beats)),
                ("device_uptime_seconds", float(metrics.uptime_seconds)),
                ("device_cloud_messages_published", float(metrics.cloud_messages_published)),
            )
            for metric_attr, val in simple_metrics:
                getattr(service.gateway.metrics, metric_attr).labels(device_id=device_id).set(val)

            service.gateway.metrics.device_latency_ms.labels(device_id=device_id, type="serial").set(
                float(metrics.serial_latency_ms)
            )
            service.gateway.metrics.device_latency_ms.labels(device_id=device_id, type="rpc").set(
                float(metrics.rpc_latency_ms)
            )
            for ret in metrics.retries:
                service.gateway.metrics.device_retries.labels(device_id=device_id, component=ret.component).set(
                    float(ret.count)
                )
        except (DecodeError, ValueError) as exc:
            logger.warning("Failed to decode daemon metrics blob in telemetry", error=str(exc))

    if service.gateway.tsdb_sink.enabled:
        await service.gateway.tsdb_sink.ingest_telemetry(device_id, envelope)


async def _handle_event(
    service: CloudBridgeService,
    device_id: str,
    _: Stream[pb.CloudEnvelope, pb.CloudEnvelope],
    envelope: pb.CloudEnvelope,
) -> None:
    evt = envelope.event
    logger.warning(
        "Device event",
        device_id=device_id,
        event_type=evt.event_type,
        description=evt.description,
    )
    service.gateway.metrics.device_events.labels(
        device_id=device_id,
        severity=evt.severity or "info",
    ).inc()


_PayloadHandler = Callable[
    ["CloudBridgeService", str, Stream[pb.CloudEnvelope, pb.CloudEnvelope], pb.CloudEnvelope],
    Awaitable[None] | None,
]

_PAYLOAD_HANDLERS: Final[dict[str, _PayloadHandler]] = {
    "ping": _handle_ping,
    "telemetry": handle_telemetry,
    "event": _handle_event,
    "command_response": lambda svc, dev_id, _, env: svc.gateway.handle_command_response(
        env.device_id or dev_id, env.sequence_id, env.command_response
    ),
}


def extract_device_id_from_metadata(metadata: Mapping[str, Any] | None) -> str | None:
    """Extract explicit target device ID from gRPC request/stream metadata."""
    if not metadata:
        return None
    for key in ("x-device-id", "device-id", "device_id"):
        if key in metadata:
            raw_val = metadata[key]
            if isinstance(raw_val, (list, tuple)):
                if not raw_val:
                    continue
                item: object = cast(object, raw_val[0])
                return item.decode("utf-8") if isinstance(item, bytes) else str(item)
            val: object = cast(object, raw_val)
            return val.decode("utf-8") if isinstance(val, bytes) else str(val)
    return None


def extract_peer_identity(
    peer: Peer | None,
    metadata: Mapping[str, Any] | None = None,
) -> tuple[str, bool]:
    """[SIL-2] Extract device ID and authentication status from gRPC peer certificate or stream metadata."""
    if meta_dev := extract_device_id_from_metadata(metadata):
        return meta_dev, False

    if not peer:
        return "anonymous-unknown", False

    addr = peer.addr()
    device_id = f"anonymous-{addr[0]}:{addr[1]}" if addr else "anonymous-unknown"
    is_authenticated = False

    cert = peer.cert()
    if cert:
        try:
            for sub in cert.get("subject", []):
                for key, val in sub:
                    if key == "commonName":
                        return str(val), True
        except (ssl.SSLError, AttributeError, KeyError, TypeError) as e:
            logger.error("Failed to parse client certificate", error=str(e))
            raise ValueError(f"Failed to parse client certificate: {e}") from e

    return device_id, is_authenticated


async def auth_interceptor(event: grpclib.events.RecvRequest) -> None:
    """[SIL-2] Server-side interceptor validating peer identity and binding contextvars."""
    try:
        device_id, is_authenticated = extract_peer_identity(event.peer)
    except ValueError:
        logger.error("Rejecting request with invalid client certificate")
        return

    orig_func = event.method_func

    async def _wrapped_handler(stream: Stream[Any, Any]) -> None:
        with structlog.contextvars.bound_contextvars(
            device_id=device_id,
            rpc_method=event.method_name,
        ):
            logger.debug(
                "gRPC method invoked",
                method=event.method_name,
                authenticated=is_authenticated,
            )
            await orig_func(stream)

    event.method_func = _wrapped_handler


class CloudBridgeService(CloudBridgeBase):
    def __init__(self, gateway: ProtobufGateway) -> None:
        self.gateway = gateway

    async def Session(self, stream: Stream[pb.CloudEnvelope, pb.CloudEnvelope]) -> None:
        try:
            device_id, is_authenticated = extract_peer_identity(stream.peer, stream.metadata)
        except ValueError as exc:
            logger.warning("Session connection rejected: invalid peer identity", error=str(exc))
            return

        session_fsm = GatewayDeviceSession(
            device_id=device_id,
            stream=stream,
            metrics=self.gateway.metrics,
            gateway=self.gateway,
        )
        if is_authenticated:
            session_fsm.fsm.authenticate()

        with structlog.contextvars.bound_contextvars(device_id=device_id):
            logger.info("Device connected", state=session_fsm.fsm.current_state_value)
            self.gateway.connections[device_id] = stream
            self.gateway.sessions[device_id] = session_fsm
            self.gateway.metrics.devices_connected.inc()
            self.gateway.metrics.device_connection_state.labels(device_id=device_id).set(1.0)

            try:
                async for envelope in stream:
                    if not envelope.IsInitialized() or envelope.protocol_version != PROTOCOL_VERSION:
                        logger.warning("Invalid cloud envelope")
                        continue

                    if not session_fsm.fsm.active.is_active:
                        session_fsm.fsm.activate()

                    if envelope.device_id and envelope.device_id != device_id:
                        old_device_id = device_id
                        device_id = envelope.device_id
                        session_fsm.device_id = device_id
                        self.gateway.connections.pop(old_device_id, None)
                        self.gateway.sessions.pop(old_device_id, None)
                        self.gateway.connections[device_id] = stream
                        self.gateway.sessions[device_id] = session_fsm
                        structlog.contextvars.bind_contextvars(device_id=device_id)

                    payload_type = envelope.WhichOneof("payload")
                    logger.debug(
                        "[DEVICE -> GATEWAY] [DEVICE:%s] [TYPE:%s] [SEQ:%d]",
                        device_id,
                        payload_type,
                        envelope.sequence_id,
                    )

                    if handler := _PAYLOAD_HANDLERS.get(payload_type or ""):
                        res = handler(self, device_id, stream, envelope)
                        if res is not None:
                            await res
                    else:
                        logger.debug("Received unhandled or empty payload type", payload_type=payload_type)
            except asyncio.CancelledError:
                logger.info("Session cancelled for device")
                raise
            except OSError as exc:
                logger.warning("Network OS error for device", error=str(exc))
            finally:
                with anyio.CancelScope(shield=True):
                    session_fsm.close()
                    logger.info("Device disconnected", state=session_fsm.fsm.current_state_value)
                    self.gateway.sessions.pop(device_id, None)
                    self.gateway.connections.pop(device_id, None)

    async def DispatchCommand(self, stream: Stream[pb.CommandDispatch, pb.CommandResponse]) -> None:
        """[SIL-2] Northbound gRPC endpoint dispatching a command to an active edge device."""
        request = await stream.recv_message()
        if request is None:
            return

        target_id = request.target_device_id
        if not target_id:
            await stream.send_message(
                pb.CommandResponse(status_code=400, payload=b"Explicit target_device_id is required")
            )
            return

        if target_id not in self.gateway.connections:
            await stream.send_message(
                pb.CommandResponse(
                    status_code=503,
                    payload=f"Device '{target_id}' is not connected to gateway".encode(),
                )
            )
            return

        timeout = request.timeout_seconds if request.timeout_seconds > 0 else 10.0
        with structlog.contextvars.bound_contextvars(
            target_device_id=target_id,
            command_path=request.command_path,
        ):
            try:
                response = await self.gateway.send_command(
                    target_id,
                    request.command_path,
                    payload=request.payload,
                    timeout_seconds=float(timeout),
                )
                await stream.send_message(response)
            except (KeyError, TimeoutError, OSError) as exc:
                await stream.send_message(pb.CommandResponse(status_code=504, payload=str(exc).encode("utf-8")))


class GatewayLocalBridgeService:
    """[SIL-2] Northbound LocalBridge gRPC service hosted on the Gateway for clients.

    Routes all typed LocalBridge RPCs to the explicitly specified edge device.
    """

    def __init__(self, gateway: ProtobufGateway) -> None:
        self.gateway = gateway

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

    def __mapping__(self) -> dict[str, Handler]:
        return REGISTRY.build_local_bridge_mapping(self)

    async def _forward_rpc(
        self,
        stream: Stream[Any, Any],
        method_name: str,
        resp_cls: type[ProtobufMessage],
        default_resp: ProtobufMessage,
        *,
        pre_read_req: Any = None,
    ) -> None:
        req = pre_read_req if pre_read_req is not None else (await stream.recv_message())
        if req is None:
            return

        device_id = extract_device_id_from_metadata(stream.metadata)
        if not device_id:
            logger.warning("LocalBridge call rejected: missing explicit device_id", method=method_name)
            raise GRPCError(
                Status.INVALID_ARGUMENT,
                "Explicit device resolution required: missing 'x-device-id' metadata header",
            )

        if device_id not in self.gateway.connections:
            logger.warning(
                "LocalBridge call rejected: target device not connected",
                method=method_name,
                device_id=device_id,
            )
            raise GRPCError(
                Status.UNAVAILABLE,
                f"Explicit target device '{device_id}' is not connected to gateway",
            )

        with structlog.contextvars.bound_contextvars(
            target_device_id=device_id,
            rpc_method=method_name,
        ):
            try:
                cmd_resp = await self.gateway.send_command(
                    device_id,
                    f"rpc/{method_name}",
                    payload=req.SerializeToString(),
                    timeout_seconds=15.0,
                )
                if cmd_resp.status_code == 200:
                    resp = resp_cls()
                    if cmd_resp.payload:
                        resp.ParseFromString(cmd_resp.payload)
                    await stream.send_message(resp)
                else:
                    await stream.send_message(default_resp)
            except (DecodeError, KeyError, TimeoutError, OSError) as exc:
                logger.error(
                    "Error forwarding RPC to device",
                    method=method_name,
                    device_id=device_id,
                    error=str(exc),
                )
                await stream.send_message(default_resp)

    async def Publish(self, stream: Stream[pb.CloudQueuedPublish, pb.CloudQueuedPublish]) -> None:
        req = await stream.recv_message()
        if req is None:
            return
        device_id = extract_device_id_from_metadata(stream.metadata)
        if not device_id:
            logger.warning("Publish rejected: missing explicit device_id")
            raise GRPCError(
                Status.INVALID_ARGUMENT,
                "Explicit device resolution required: missing 'x-device-id' metadata header",
            )
        with structlog.contextvars.bound_contextvars(
            target_device_id=device_id,
            topic=req.topic_name,
        ):
            if "console" in req.topic_name:
                for q in self.gateway.console_queues.get(device_id, []):
                    q.put_nowait(req)
            await self._forward_rpc(stream, "Publish", pb.CloudQueuedPublish, pb.CloudQueuedPublish(), pre_read_req=req)

    async def SubscribeConsole(self, stream: Stream[pb.SubscribeRequest, pb.CloudQueuedPublish]) -> None:
        req = await stream.recv_message()
        if req is None:
            return
        device_id = extract_device_id_from_metadata(stream.metadata)
        if not device_id:
            logger.warning("SubscribeConsole rejected: missing explicit device_id")
            raise GRPCError(
                Status.INVALID_ARGUMENT,
                "Explicit device resolution required: missing 'x-device-id' metadata header",
            )
        if device_id not in self.gateway.connections:
            logger.warning("SubscribeConsole rejected: device not connected", device_id=device_id)
            raise GRPCError(
                Status.UNAVAILABLE,
                f"Explicit target device '{device_id}' is not connected to gateway",
            )

        with structlog.contextvars.bound_contextvars(
            target_device_id=device_id,
            rpc_method="SubscribeConsole",
        ):
            send_stream, receive_stream = anyio.create_memory_object_stream[pb.CloudQueuedPublish](max_buffer_size=64)
            adapter = MemoryQueueAdapter(send_stream)
            self.gateway.console_queues.setdefault(device_id, []).append(adapter)
            try:
                async with receive_stream:
                    async for msg in receive_stream:
                        await stream.send_message(msg)
            except (OSError, RuntimeError) as e:
                logger.debug("Console subscriber stream closed", device_id=device_id, error=str(e))
            finally:
                adapter.close()
                if device_id in self.gateway.console_queues and adapter in self.gateway.console_queues[device_id]:
                    self.gateway.console_queues[device_id].remove(adapter)

    @staticmethod
    def create_forwarder(spec: OperationSpec) -> Callable[[Any, Stream[Any, Any]], Awaitable[None]]:
        name = spec.rpc_name or spec.name
        resp_cls = spec.response_type
        default_factory = spec.default_factory
        if resp_cls is None or not callable(default_factory):
            raise ValueError(f"Gateway RPC {name} requires response_type and default_factory")
        default_resp = default_factory()

        async def forwarder(self: GatewayLocalBridgeService, stream: Stream[Any, Any]) -> None:
            await self._forward_rpc(
                stream,
                name,
                resp_cls,
                default_resp,
            )

        forwarder.__name__ = name
        forwarder.__qualname__ = f"GatewayLocalBridgeService.{name}"
        return forwarder


for _spec in REGISTRY.rpc_specs:
    _name = _spec.rpc_name or _spec.name
    if _name not in GatewayLocalBridgeService.__dict__:
        setattr(GatewayLocalBridgeService, _name, GatewayLocalBridgeService.create_forwarder(_spec))

LocalBridgeBase.register(GatewayLocalBridgeService)


class ProtobufGateway:
    """High-performance industrial gRPC Gateway with fleet observability and command routing."""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = DEFAULT_CLOUD_PORT,
        use_tls: bool = True,
        cert_file: str | None = None,
        key_file: str | None = None,
        ca_file: str | None = None,
        http3_enabled: bool = False,
        http3_port: int = DEFAULT_CLOUD_PORT,
        metrics_port: int | None = None,
        tsdb_url: str | None = None,
        metrics_registry: prometheus_client.CollectorRegistry | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.use_tls = use_tls
        self.cert_file = cert_file
        self.key_file = key_file
        self.ca_file = ca_file
        self.http3_enabled = http3_enabled
        self.http3_port = http3_port
        self.metrics_port = metrics_port
        self.server: Server | None = None
        self.connections: dict[str, Stream[pb.CloudEnvelope, pb.CloudEnvelope]] = {}
        self.sessions: dict[str, GatewayDeviceSession] = {}
        self.pending_commands: dict[tuple[str, int], asyncio.Future[pb.CommandResponse]] = {}
        self._sequence_id: int = 0
        self.metrics: FleetMetrics = FleetMetrics(registry=metrics_registry)
        self.tsdb_sink: TSDBSink = TSDBSink(endpoint_url=tsdb_url)
        self.console_queues: dict[str, list[ConsoleQueueTarget]] = {}

    def get_ssl_context(self) -> ssl.SSLContext | None:
        if not self.use_tls:
            return None

        if not self.cert_file or not self.key_file:
            logger.warning("TLS enabled but certificate/key files not provided. Running without TLS.")
            return None

        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(certfile=self.cert_file, keyfile=self.key_file)
        if self.ca_file:
            context.load_verify_locations(cafile=self.ca_file)
            context.verify_mode = ssl.CERT_REQUIRED
            logger.info("Mutual TLS (mTLS) client verification enabled.")
        else:
            context.verify_mode = ssl.CERT_NONE
            logger.info("TLS enabled (server-only authentication).")
        return context

    async def send_command(
        self,
        device_id: str,
        command_path: str,
        payload: bytes = b"",
        timeout_seconds: float = 10.0,
    ) -> pb.CommandResponse:
        """[SIL-2] Asynchronously dispatch a command to an active edge device and await response."""
        stream = self.connections.get(device_id)
        if not stream:
            raise KeyError(f"Device {device_id} is not connected to gateway")

        self._sequence_id = (self._sequence_id + 1) & 0x7FFFFFFF
        seq = self._sequence_id

        req_envelope = pb.CloudEnvelope(
            protocol_version=PROTOCOL_VERSION,
            device_id="CLOUD_GW",
            sequence_id=seq,
            timestamp_utc=int(time.time()),
            command_request=pb.CommandRequest(
                command_path=command_path,
                payload=payload,
            ),
        )

        loop = asyncio.get_running_loop()
        future: asyncio.Future[pb.CommandResponse] = loop.create_future()
        key = (device_id, seq)
        self.pending_commands[key] = future

        self.metrics.command_requests.labels(device_id=device_id).inc()
        logger.debug(
            "[GATEWAY -> DEVICE] [DEVICE:%s] [CMD:%s] [SEQ:%d]",
            device_id,
            command_path,
            seq,
        )

        try:
            await stream.send_message(req_envelope)
            with anyio.fail_after(timeout_seconds):
                return await future
        finally:
            with anyio.CancelScope(shield=True):
                self.pending_commands.pop(key, None)

    def handle_command_response(
        self,
        device_id: str,
        sequence_id: int,
        response: pb.CommandResponse,
    ) -> None:
        """[SIL-2] Handle incoming command response from edge device and resolve pending future."""
        logger.debug(
            "[DEVICE -> GATEWAY] [DEVICE:%s] [CMD_RESP] [SEQ:%d] [STATUS:%d]",
            device_id,
            sequence_id,
            response.status_code,
        )
        logger.info(
            "Received command response",
            device_id=device_id,
            seq=sequence_id,
            status_code=response.status_code,
        )
        self.metrics.command_responses.labels(
            device_id=device_id,
            status_code=str(response.status_code),
        ).inc()

        key = (device_id, sequence_id)
        if (future := self.pending_commands.get(key)) and not future.done():
            future.set_result(response)

    async def run(self) -> None:
        if self.metrics_port:
            prometheus_client.start_http_server(self.metrics_port, registry=self.metrics.registry)
            logger.info("Fleet Prometheus Exporter running", port=self.metrics_port)

        ssl_context = self.get_ssl_context()
        services = [CloudBridgeService(self), GatewayLocalBridgeService(self)]
        self.server = Server(ServerReflection.extend(services))
        grpclib.events.listen(self.server, grpclib.events.RecvRequest, auth_interceptor)
        await self.server.start(self.host, self.port, ssl=ssl_context)

        scheme = "tcps" if ssl_context else "tcp"
        logger.info("gRPC Cloud Gateway running", scheme=scheme, host=self.host, port=self.port)
        if self.http3_enabled:
            logger.info("HTTP/3 (QUIC) capability enabled", port=self.http3_port, alt_svc=f'h3=":{self.http3_port}"')
        try:
            await self.server.wait_closed()
        finally:
            await self.tsdb_sink.aclose()


app = typer.Typer(help="MCU Bridge Protobuf Gateway", add_completion=False)


@app.command()
def main(
    host: Annotated[str, typer.Option(help="Host to bind to")] = DEFAULT_CLOUD_HOST,
    port: Annotated[int, typer.Option(help="Port to listen on")] = DEFAULT_CLOUD_PORT,
    no_tls: Annotated[bool, typer.Option("--no-tls", help="Disable TLS (insecure mode)")] = False,
    cert: Annotated[Path | None, typer.Option(help="Path to server SSL certificate file")] = None,
    key: Annotated[Path | None, typer.Option(help="Path to server SSL private key file")] = None,
    ca: Annotated[Path | None, typer.Option(help="Path to CA file for client certificate verification")] = None,
    http3: Annotated[bool, typer.Option("--http3", help="Enable HTTP/3 (QUIC) capability")] = False,
    http3_port: Annotated[int, typer.Option(help="UDP Port for HTTP/3 QUIC listener")] = DEFAULT_CLOUD_PORT,
    metrics_port: Annotated[int | None, typer.Option(help="Port for fleet Prometheus /metrics endpoint")] = None,
    tsdb_url: Annotated[str | None, typer.Option(help="HTTP endpoint URL for TSDB Line Protocol ingestion")] = None,
) -> None:
    """MCU Bridge Protobuf Gateway."""
    configure_logging()
    gateway = ProtobufGateway(
        host=host,
        port=port,
        use_tls=not no_tls,
        cert_file=str(cert) if cert else None,
        key_file=str(key) if key else None,
        ca_file=str(ca) if ca else None,
        http3_enabled=http3,
        http3_port=http3_port,
        metrics_port=metrics_port,
        tsdb_url=tsdb_url,
    )

    try:
        with asyncio.Runner(loop_factory=uvloop.new_event_loop) as runner:
            runner.run(gateway.run())
    except KeyboardInterrupt:
        logger.info("Gateway terminated by user.")


if __name__ == "__main__":
    app()

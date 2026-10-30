#!/usr/bin/env python3
"""Protobuf Cloud Gateway for MCU Bridge v2.

High-performance industrial IoT server acting as the central cloud hub for N MPU Daemons.
Provides bidirectional gRPC streaming over HTTP/2, fleet-wide Prometheus observability,
TSDB time-series ingestion, and northbound command orchestration. The HTTP/3 flag is
retained for configuration compatibility but does not enable a QUIC listener.
"""

from __future__ import annotations

import asyncio
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable, Mapping
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Final, cast

import anyio
import grpclib.events
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
    LineProtocolFieldKind,
    MetricKind,
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
        self.fsm = GatewaySessionMachine(listeners=[self])

    def on_enter_closed(self) -> None:
        """[SIL-2] Native python-statemachine hook executed on session closure."""
        self.console_queues.clear()

        if self.gateway is not None and hasattr(self.gateway, "pending_commands"):
            for (target_id, _), fut in list(self.gateway.pending_commands.items()):
                if target_id == self.device_id and not fut.done():
                    fut.set_exception(ConnectionResetError(f"Device {self.device_id} disconnected during execution"))

        if self.metrics is not None and self.device_id:
            self.metrics.devices_connected.dec()
            self.metrics.device_connection_state.labels(device_id=self.device_id).set(0.0)

    def close(self) -> None:
        self.fsm.close()


class FleetMetrics:
    """[SIL-2] Fleet-wide Prometheus observability metrics container."""

    def __init__(self, registry: prometheus_client.CollectorRegistry | None = None) -> None:
        self.registry = registry if registry is not None else prometheus_client.CollectorRegistry(auto_describe=True)
        self.daemon_metrics: dict[str, prometheus_client.Gauge | prometheus_client.Counter] = {}
        self._counter_samples: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        metric_labels: dict[str, set[str]] = {}
        for spec in REGISTRY.metrics:
            metric_labels.setdefault(spec.metric_name, set()).update(label.name for label in spec.labels)

        for spec in REGISTRY.metrics:
            if spec.metric_name in self.daemon_metrics:
                continue
            labelnames = ["device_id", *sorted(metric_labels[spec.metric_name])]
            if spec.kind is MetricKind.GAUGE:
                collector = prometheus_client.Gauge(
                    spec.metric_name,
                    spec.help,
                    labelnames=labelnames,
                    registry=self.registry,
                )
            elif spec.kind is MetricKind.COUNTER:
                collector = prometheus_client.Counter(
                    spec.metric_name,
                    spec.help,
                    labelnames=labelnames,
                    registry=self.registry,
                )
            else:
                raise ValueError(f"Unsupported metric kind {spec.kind!r} for {spec.metric_name}")
            self.daemon_metrics[spec.metric_name] = collector

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

    def update_daemon_metrics(self, device_id: str, metrics: pb.DaemonMetrics) -> None:
        """Apply a decoded Protobuf snapshot using the generated metric catalog."""
        for spec in REGISTRY.metrics:
            source = getattr(metrics, spec.source_field)
            samples = source if spec.value_field else (metrics,)
            for sample in samples:
                value = float(getattr(sample, spec.value_field)) if spec.value_field else float(source)
                labels = {
                    "device_id": device_id,
                    **{
                        label.name: (
                            label.static_value if label.static_value is not None else str(getattr(sample, label.field))
                        )
                        for label in spec.labels
                    },
                }
                collector = self.daemon_metrics[spec.metric_name]
                if isinstance(collector, prometheus_client.Counter):
                    counter_key = (spec.metric_name, tuple(sorted(labels.items())))
                    previous = self._counter_samples.get(counter_key)
                    increment = value if previous is None or value < previous else value - previous
                    collector.labels(**labels).inc(increment)
                    self._counter_samples[counter_key] = value
                else:
                    collector.labels(**labels).set(value)


class TSDBSink:
    """[SIL-2] Time-Series Database ingestion adapter supporting Line Protocol."""

    def __init__(
        self,
        endpoint_url: str | None = None,
    ) -> None:
        if endpoint_url:
            parsed = urllib.parse.urlsplit(endpoint_url)
            if parsed.scheme not in ("http", "https"):
                raise ValueError(f"Invalid TSDB scheme '{parsed.scheme}'; must be 'http' or 'https'")
            if not parsed.netloc:
                raise ValueError("Invalid TSDB URL; expected an absolute HTTP or HTTPS URL")
        self.endpoint_url = endpoint_url
        self.enabled = bool(endpoint_url)

    @staticmethod
    def format_line_protocol(
        device_id: str,
        metrics: pb.DaemonMetrics,
        timestamp_ns: int | None = None,
    ) -> str:
        """Format metrics into standard Influx/VictoriaMetrics Line Protocol."""
        ts = timestamp_ns if timestamp_ns is not None else time.time_ns()
        value_formatters: dict[LineProtocolFieldKind, Callable[[Any], str]] = {
            LineProtocolFieldKind.INTEGER: lambda value: f"{int(value)}i",
            LineProtocolFieldKind.FLOAT: lambda value: str(float(value)),
            LineProtocolFieldKind.BOOLEAN: lambda value: str(bool(value)).lower(),
        }
        fields = ",".join(
            f"{spec.field_name}={value_formatters[spec.kind](getattr(metrics, spec.source_field))}"
            for spec in REGISTRY.line_protocol_fields
        )
        return f"mcubridge_telemetry,device_id={device_id} {fields} {ts}"

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
        retry=tenacity.retry_if_exception_type((urllib.error.URLError, TimeoutError, OSError)),
        reraise=True,
    )
    async def post_line(self, line: str) -> None:
        """Execute an HTTP POST without blocking the gateway event loop."""
        if not self.endpoint_url:
            return
        request = urllib.request.Request(
            self.endpoint_url,
            data=line.encode("utf-8"),
            headers={"Content-Type": "text/plain; charset=utf-8"},
            method="POST",
        )
        try:
            response = await asyncio.to_thread(urllib.request.urlopen, request, timeout=5.0)
        except urllib.error.HTTPError as exc:
            logger.warning("TSDB server responded with status error", status=exc.code)
            exc.close()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            logger.warning("TSDB network write failed", error=str(exc))
            raise
        else:
            response.close()


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
            service.gateway.metrics.update_daemon_metrics(device_id, metrics)
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
            logger.warning(
                "HTTP/3 was requested but grpclib.Server only serves HTTP/2; no QUIC listener was started",
                requested_udp_port=self.http3_port,
            )
        await self.server.wait_closed()


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
    http3_port: Annotated[
        int,
        typer.Option(help="Reserved UDP port for HTTP/3; current grpclib transport falls back to HTTP/2"),
    ] = DEFAULT_CLOUD_PORT,
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

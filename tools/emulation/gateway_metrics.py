"""Live Pigweed ``pw.metric.proto.MetricService`` verification for emulation gateways."""

from __future__ import annotations

import os
import select
import socket
import ssl
import threading
import time
from pathlib import Path
from typing import Any, cast

import pw_hdlc.decode
import pw_hdlc.encode
import structlog
from pw_metric import metric_parser
from pw_metric_proto import metric_service_pb2
from pw_rpc.callback_client.errors import RpcError as PwRpcError, RpcTimeout as PwRpcTimeout
from pw_rpc.callback_client.impl import Impl as PwCallbackClientImpl
from pw_rpc.client import Client as PwRpcClient
from pw_rpc.descriptors import Channel as PwRpcChannel, Service as PwRpcService, Services as PwRpcServices
from pw_tokenizer import detokenize

from tools.emulation.emulation_pki import get_emulation_pki

__all__ = [
    "audit_gateway_pw_metrics",
    "fetch_gateway_pw_metrics",
    "resolve_gateway_binary",
]

logger = structlog.get_logger("gateway_metrics")

repo_root = Path(__file__).resolve().parents[2]
CLOUD_HOST = "127.0.0.1"
CLOUD_PORT = 8443
METRIC_SERVICES = PwRpcServices(
    [
        PwRpcService.from_descriptor(desc)
        for desc in cast(Any, metric_service_pb2).DESCRIPTOR.services_by_name.values()
    ]
)


def resolve_gateway_binary() -> Path:
    """Return the standalone gateway ELF binary path used to detokenize ``pw_metric`` names."""
    return Path(
        os.environ.get("MCUBRIDGE_GATEWAY_BINARY") or repo_root / "build/mcubridge-gateway/mcubridge-gateway-cpp"
    )


class _TlsMetricPeer:
    """Minimal thread-safe HDLC/pw_rpc client over a non-blocking TLS 1.3 SSLSocket."""

    def __init__(self, sock: ssl.SSLSocket, *, timeout_s: float = 5.0) -> None:
        self._sock = sock
        self._sock.setblocking(False)
        self._ssl_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._decoder = pw_hdlc.decode.FrameDecoder()
        self._running = True
        self._closed = False
        self.errors: list[BaseException] = []
        self.rpc_client = PwRpcClient(
            PwCallbackClientImpl(default_unary_timeout_s=timeout_s),
            [PwRpcChannel(1, self.send_packet)],
            METRIC_SERVICES,
        )
        self._thread = threading.Thread(target=self._reader, name="gateway-metrics-reader", daemon=True)
        self._thread.start()

    def send_packet(self, payload: bytes) -> None:
        frame = pw_hdlc.encode.ui_frame(1, payload)
        view = memoryview(frame)
        deadline = time.monotonic() + 5.0
        with self._send_lock:
            while view:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Timed out sending TLS HDLC frame to gateway")
                wait_read = False
                wait_write = False
                with self._ssl_lock:
                    if self._closed:
                        raise OSError("TLS gateway metrics socket closed")
                    try:
                        sent = self._sock.send(view)
                    except ssl.SSLWantReadError:
                        wait_read = True
                    except (ssl.SSLWantWriteError, BlockingIOError):
                        wait_write = True
                    else:
                        if sent == 0:
                            raise OSError("TLS gateway metrics socket closed during send")
                        view = view[sent:]
                if wait_read:
                    select.select([self._sock], [], [], 0.05)
                elif wait_write:
                    select.select([], [self._sock], [], 0.05)

    def _reader(self) -> None:
        while self._running:
            with self._ssl_lock:
                if self._closed:
                    break
                has_pending = self._sock.pending() > 0
            if not has_pending:
                try:
                    readable, _, _ = select.select([self._sock], [], [], 0.05)
                except (OSError, ValueError):
                    break
                if not readable:
                    continue
            with self._ssl_lock:
                if self._closed:
                    break
                try:
                    data = self._sock.recv(4096)
                except (ssl.SSLWantReadError, ssl.SSLWantWriteError, BlockingIOError):
                    continue
                except OSError as exc:
                    if self._running:
                        self.errors.append(exc)
                    break
            if not data:
                break
            for frame in self._decoder.process(data):
                if frame.ok() and frame.address == 1:
                    self.rpc_client.process_packet(frame.data)

    def close(self) -> None:
        self._running = False
        self._thread.join(timeout=2.0)
        with self._ssl_lock:
            if not self._closed:
                self._closed = True
                self._sock.close()


def _open_tls_client_socket(host: str, port: int, timeout_s: float) -> ssl.SSLSocket:
    pki = get_emulation_pki()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.check_hostname = False
    ctx.load_verify_locations(cafile=str(pki.ca_cert))
    ctx.load_cert_chain(certfile=str(pki.client.cert), keyfile=str(pki.client.key))
    raw_sock = socket.create_connection((host, port), timeout=timeout_s)
    try:
        return ctx.wrap_socket(raw_sock, server_hostname=host)
    except BaseException:
        raw_sock.close()
        raise


def fetch_gateway_pw_metrics(
    host: str = CLOUD_HOST,
    port: int = CLOUD_PORT,
    *,
    timeout_s: float = 5.0,
    gateway_binary: Path | None = None,
) -> dict[str, Any]:
    """Query ``pw.metric.proto.MetricService/Get`` on the live emulation gateway."""
    binary = gateway_binary if gateway_binary is not None else resolve_gateway_binary()
    detokenizer = detokenize.Detokenizer(binary) if binary.is_file() else detokenize.Detokenizer(None)
    peer = _TlsMetricPeer(_open_tls_client_socket(host, port, timeout_s), timeout_s=timeout_s)
    try:
        parse_metrics_fn: Any = metric_parser.parse_metrics
        metrics: object = parse_metrics_fn(
            peer.rpc_client.channel(1).rpcs,
            detokenizer,
            timeout_s=timeout_s,
        )
        if peer.errors:
            raise RuntimeError(f"Gateway MetricService transport error: {peer.errors[0]}")
        if not isinstance(metrics, dict):
            raise ValueError(f"Unexpected MetricService payload type: {type(metrics).__name__}")
        return cast(dict[str, Any], metrics)
    finally:
        peer.close()


def audit_gateway_pw_metrics(
    host: str = CLOUD_HOST,
    port: int = CLOUD_PORT,
    *,
    timeout_s: float = 5.0,
    min_device_sessions: int = 1,
    min_dispatches: int = 0,
) -> bool:
    """Audit live gateway ``pw_metric`` counters after an emulation session."""
    try:
        metrics = fetch_gateway_pw_metrics(host=host, port=port, timeout_s=timeout_s)
    except (OSError, ssl.SSLError, TimeoutError, RuntimeError, ValueError, PwRpcError, PwRpcTimeout) as exc:
        logger.error("Pigweed MetricService gateway audit failed", error=str(exc))
        return False

    gateway_obj = metrics.get("gateway")
    if not isinstance(gateway_obj, dict):
        logger.error("Gateway MetricService response missing 'gateway' group", keys=sorted(metrics.keys()))
        return False

    gw = cast(dict[str, Any], gateway_obj)
    anomalies: list[str] = []
    if int(gw.get("device_sessions_opened", 0)) < min_device_sessions:
        anomalies.append(f"device_sessions_opened={gw.get('device_sessions_opened')} < {min_device_sessions}")
    if int(gw.get("client_sessions_opened", 0)) < 1:
        anomalies.append(f"client_sessions_opened={gw.get('client_sessions_opened')} < 1")
    if int(gw.get("dispatches_total", 0)) < min_dispatches:
        anomalies.append(f"dispatches_total={gw.get('dispatches_total')} < {min_dispatches}")
    if int(gw.get("client_rejections_total", 0)) != 0:
        anomalies.append(f"client_rejections_total={gw.get('client_rejections_total')} != 0")
    if int(gw.get("acl_denials_total", 0)) != 0:
        anomalies.append(f"acl_denials_total={gw.get('acl_denials_total')} != 0")

    if anomalies:
        logger.error("Pigweed MetricService gateway audit detected anomalies", anomalies=anomalies, gateway=gw)
        return False

    logger.info("Pigweed MetricService gateway audit passed", gateway=gw)
    return True

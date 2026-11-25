"""Live Pigweed ``pw.metric.proto.MetricService`` verification for emulation gateways."""

from __future__ import annotations

import os
import socket
import ssl
import threading
from pathlib import Path
from typing import Any, cast

import structlog
from pw_hdlc.decode import FrameDecoder
from pw_hdlc.encode import ui_frame
from pw_metric_proto import metric_service_pb2
from pw_rpc.callback_client.errors import RpcError as PwRpcError, RpcTimeout as PwRpcTimeout
from pw_rpc.callback_client.impl import Impl as PwCallbackClientImpl
from pw_rpc.client import Client as PwRpcClient
from pw_rpc.descriptors import Channel as PwRpcChannel

from tools.console.pw_bridge_console import build_detokenizer, collect_metrics_tree
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


def resolve_gateway_binary() -> Path:
    """Return the standalone gateway ELF binary path used to detokenize ``pw_metric`` names."""
    return Path(
        os.environ.get("MCUBRIDGE_GATEWAY_BINARY") or repo_root / "build/mcubridge-gateway/mcubridge-gateway-cpp"
    )


class _TlsMetricSession:
    """Serialized TLS 1.3 + HDLC + pw_rpc session dedicated to MetricService queries."""

    def __init__(self, sock: ssl.SSLSocket, *, timeout_s: float = 5.0) -> None:
        self._sock = sock
        self._sock.settimeout(0.05)
        self._io_lock = threading.Lock()
        self._decoder = FrameDecoder()
        self._stop = threading.Event()
        self._closed = False
        self.errors: list[OSError] = []
        self.rpc_client = PwRpcClient.from_modules(
            PwCallbackClientImpl(default_unary_timeout_s=timeout_s),
            [PwRpcChannel(1, self._send_rpc_packet)],
            [metric_service_pb2],
        )
        self._reader_thread = threading.Thread(
            target=self._pump_incoming,
            name="gateway-metrics-reader",
            daemon=True,
        )
        self._reader_thread.start()

    def _send_rpc_packet(self, payload: bytes) -> None:
        frame = ui_frame(1, payload)
        with self._io_lock:
            if self._closed:
                raise OSError("TLS gateway metrics socket is closed")
            self._sock.sendall(frame)

    def _pump_incoming(self) -> None:
        while not self._stop.is_set():
            with self._io_lock:
                if self._closed:
                    return
                try:
                    chunk = self._sock.recv(4096)
                except (TimeoutError, ssl.SSLWantReadError, ssl.SSLWantWriteError):
                    continue
                except OSError as exc:
                    if not self._stop.is_set():
                        self.errors.append(exc)
                    return
            if not chunk:
                return
            for frame in self._decoder.process(chunk):
                if frame.ok() and frame.address == 1:
                    self.rpc_client.process_packet(frame.data)

    def close(self) -> None:
        self._stop.set()
        self._reader_thread.join(timeout=2.0)
        with self._io_lock:
            if not self._closed:
                self._closed = True
                self._sock.close()


def _open_tls_client_socket(host: str, port: int, timeout_s: float) -> ssl.SSLSocket:
    pki = get_emulation_pki()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(cafile=str(pki.ca_cert))
    ctx.load_cert_chain(certfile=str(pki.client.cert), keyfile=str(pki.client.key))
    raw_sock = socket.create_connection((host, port), timeout=timeout_s)
    try:
        return ctx.wrap_socket(raw_sock, server_hostname=host)
    except (OSError, ssl.SSLError):
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
    detokenizer, _resolved = build_detokenizer(firmware=binary if binary.is_file() else None)
    session = _TlsMetricSession(_open_tls_client_socket(host, port, timeout_s), timeout_s=timeout_s)
    try:
        channel = session.rpc_client.channel(1)
        if channel is None:
            raise RuntimeError("Gateway MetricService RPC channel 1 unavailable")
        metrics = collect_metrics_tree(channel.rpcs, detokenizer, timeout_s=timeout_s)
        if session.errors:
            raise RuntimeError(f"Gateway MetricService transport error: {session.errors[0]}")
        return metrics
    finally:
        session.close()


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

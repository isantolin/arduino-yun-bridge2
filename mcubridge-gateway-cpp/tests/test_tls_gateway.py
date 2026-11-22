from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
import select
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from typing import Any
import unittest

from mcubridge.protocol import protocol
from mcubridge.protocol.gateway import gateway_control_pb2
import pw_hdlc.decode
import pw_hdlc.encode
import pw_rpc.packets
import pw_status
from pw_rpc import callback_client
from pw_rpc.client import Client as PwRpcClient
from pw_rpc.descriptors import Channel as PwRpcChannel
from pw_transfer.client import Error as PwTransferError
from pw_transfer.client import Manager as PwTransferManager
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


BINARY = Path(
    os.environ.get(
        "MCUBRIDGE_GATEWAY_BINARY",
        REPO_ROOT / "build/mcubridge-gateway/mcubridge-gateway-cpp",
    )
)


def _run(*args: str) -> None:
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


class _TlsRpcPeer:
    """Thread-safe HDLC/RPC peer over a non-blocking TLS 1.3 SSLSocket.

    OpenSSL SSL* objects are not safe for concurrent SSL_read/SSL_write calls
    from separate threads. This helper serializes all SSLSocket operations
    under a lock while polling readiness with select() outside the lock.
    """

    def __init__(
        self,
        sock: ssl.SSLSocket,
        *,
        default_unary_timeout_s: float = 5.0,
        on_server_packet: Callable[[Any], None] | None = None,
    ) -> None:
        self._sock = sock
        self._sock.setblocking(False)
        self._ssl_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._decoder = pw_hdlc.decode.FrameDecoder()
        self._running = True
        self._closed = False
        self.errors: list[BaseException] = []
        self.rpc_client = PwRpcClient(
            callback_client.Impl(default_unary_timeout_s=default_unary_timeout_s),
            [PwRpcChannel(1, self.send_packet)],
            protocol.PW_SERVICES,
        )
        self._on_server_packet = on_server_packet
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def send_packet(self, payload: bytes) -> None:
        frame = pw_hdlc.encode.ui_frame(1, payload)
        view = memoryview(frame)
        deadline = time.monotonic() + 5.0
        with self._send_lock:
            while view:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Timed out sending TLS HDLC frame")
                wait_read = False
                wait_write = False
                with self._ssl_lock:
                    if self._closed:
                        raise OSError("TLS peer socket closed")
                    try:
                        sent = self._sock.send(view)
                    except ssl.SSLWantReadError:
                        wait_read = True
                    except (ssl.SSLWantWriteError, BlockingIOError):
                        wait_write = True
                    else:
                        if sent == 0:
                            raise OSError("TLS peer socket closed during send")
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
                except (OSError, ssl.SSLError) as exc:
                    if self._running:
                        self.errors.append(exc)
                    break
            if not data:
                break
            for frame in self._decoder.process(data):
                if frame.ok() and frame.address == 1:
                    if self._on_server_packet is not None:
                        pkt = pw_rpc.packets.decode(frame.data)
                        if pw_rpc.packets.for_server(pkt):
                            self._on_server_packet(pkt)
                            continue
                    self.rpc_client.process_packet(frame.data)

    def close(self) -> None:
        self._running = False
        self._thread.join(timeout=2)
        with self._ssl_lock:
            if not self._closed:
                self._closed = True
                self._sock.close()


class Certificates:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.ca_key = root / "ca.key"
        self.ca_cert = root / "ca.crt"
        _run(
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(self.ca_key),
            "-out",
            str(self.ca_cert),
            "-subj",
            "/CN=MCUBridge Test CA",
            "-days",
            "2",
            "-sha256",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
        )
        self.server_cert, self.server_key = self._issue("gateway", "DNS:localhost", "serverAuth")
        self.valid_cert, self.valid_key = self._issue(
            "valid-device", "URI:spiffe://mcubridge/device/device-01", "clientAuth"
        )
        self.no_san_cert, self.no_san_key = self._issue("missing-san", None, "clientAuth")
        self.multi_san_cert, self.multi_san_key = self._issue(
            "multiple-san",
            "URI:spiffe://mcubridge/device/device-01,URI:spiffe://mcubridge/device/device-02",
            "clientAuth",
        )
        self.invalid_id_cert, self.invalid_id_key = self._issue(
            "invalid-id", "URI:spiffe://mcubridge/device/../etc", "clientAuth"
        )
        self.client_role_cert, self.client_role_key = self._issue(
            "client-role", "URI:spiffe://mcubridge/client/operator-01", "clientAuth"
        )
        self.wrong_eku_cert, self.wrong_eku_key = self._issue(
            "wrong-eku", "URI:spiffe://mcubridge/device/device-02", "serverAuth"
        )
        other = root / "other-ca"
        other.mkdir()
        other_key = other / "ca.key"
        other_cert = other / "ca.crt"
        _run(
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(other_key),
            "-out",
            str(other_cert),
            "-subj",
            "/CN=Untrusted Test CA",
            "-days",
            "2",
            "-sha256",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
        )
        self.untrusted_cert, self.untrusted_key = self._issue(
            "untrusted",
            "URI:spiffe://mcubridge/device/untrusted",
            "clientAuth",
            ca_cert=other_cert,
            ca_key=other_key,
            serial_dir=other,
        )

    def _issue(
        self,
        name: str,
        san: str | None,
        eku: str,
        *,
        ca_cert: Path | None = None,
        ca_key: Path | None = None,
        serial_dir: Path | None = None,
    ) -> tuple[Path, Path]:
        key = self.root / f"{name}.key"
        csr = self.root / f"{name}.csr"
        cert = self.root / f"{name}.crt"
        ext = self.root / f"{name}.ext"
        _run(
            "openssl",
            "req",
            "-new",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(csr),
            "-subj",
            f"/CN={name}",
        )
        lines = [
            "basicConstraints=critical,CA:FALSE",
            "keyUsage=critical,digitalSignature,keyEncipherment",
            f"extendedKeyUsage={eku}",
        ]
        if san is not None:
            lines.append(f"subjectAltName={san}")
        ext.write_text("\n".join(lines) + "\n", encoding="utf-8")
        issuer_cert = ca_cert or self.ca_cert
        issuer_key = ca_key or self.ca_key
        serial = serial_dir or self.root
        _run(
            "openssl",
            "x509",
            "-req",
            "-in",
            str(csr),
            "-CA",
            str(issuer_cert),
            "-CAkey",
            str(issuer_key),
            "-CAcreateserial",
            "-CAserial",
            str(serial / "ca.srl"),
            "-out",
            str(cert),
            "-days",
            "2",
            "-sha256",
            "-extfile",
            str(ext),
        )
        return cert, key


# ADR-0005/0007: MCU EEPROM resource ID and size (EEPROM minus the boot epoch).
EEPROM_RESOURCE_ID = 0x4D454741
EEPROM_RESOURCE_BYTES = 4080


class GatewayProcess:
    def __init__(self, root: Path, certs: Certificates) -> None:
        self._certs = certs
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        self.port = probe.getsockname()[1]
        probe.close()
        self.log_path = root / "gateway.log"
        self.log_file = self.log_path.open("w+", encoding="utf-8")
        # ADR-0007: dispatch is deny-by-default. operator-01 may Ping any
        # device and read the EEPROM of device-01; every other client/operation
        # pair is rejected.
        self.acl_path = root / "gateway.acl"
        self.acl_path.write_text(
            "# client device operation\n"
            "operator-01 * rpc.pb.SystemService/Ping  # probe\n"
            "operator-01 device-01 rpc.pb.EepromService/Read\n",
            encoding="utf-8",
        )
        # ADR-0007: private staging root (0700, owned by the gateway).
        self.staging_dir = root / "staging"
        self.process = subprocess.Popen(
            [
                str(BINARY),
                "--cert",
                str(certs.server_cert),
                "--key",
                str(certs.server_key),
                "--client-ca",
                str(certs.ca_cert),
                "--acl",
                str(self.acl_path),
                "--staging-dir",
                str(self.staging_dir),
                "--bind",
                "127.0.0.1",
                "--port",
                str(self.port),
            ],
            stdout=subprocess.DEVNULL,
            stderr=self.log_file,
        )
        self.wait_for("event=gateway_listening")

    def logs(self) -> str:
        self.log_file.flush()
        return self.log_path.read_text(encoding="utf-8")

    def wait_for(self, text: str, count: int = 1) -> None:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self.logs().count(text) >= count:
                return
            if self.process.poll() is not None:
                raise AssertionError(f"Gateway exited early: {self.logs()}")
            time.sleep(0.025)
        raise AssertionError(f"Timed out waiting for {text!r}: {self.logs()}")

    def connect(
        self,
        cert: Path | None,
        key: Path | None,
        *,
        minimum: ssl.TLSVersion = ssl.TLSVersion.TLSv1_3,
        maximum: ssl.TLSVersion = ssl.TLSVersion.TLSv1_3,
    ) -> ssl.SSLSocket:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(cafile=str(self.ca_cert))
        context.minimum_version = minimum
        context.maximum_version = maximum
        if cert is not None and key is not None:
            context.load_cert_chain(str(cert), str(key))
        raw = socket.create_connection(("127.0.0.1", self.port), timeout=4)
        raw.settimeout(4)
        try:
            return context.wrap_socket(raw, server_hostname="localhost")
        except Exception:
            raw.close()
            raise

    @property
    def ca_cert(self) -> Path:
        return self._certs.ca_cert

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=8)
        self.log_file.close()


class GatewayTlsIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not BINARY.is_file():
            raise AssertionError(f"Gateway binary not found: {BINARY}")
        cls.temp = tempfile.TemporaryDirectory(prefix="mcubridge-gateway-test-")
        cls.root = Path(cls.temp.name)
        cls.certs = Certificates(cls.root)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def setUp(self) -> None:
        self.gateway = GatewayProcess(self.root, self.certs)

    def tearDown(self) -> None:
        self.gateway.stop()

    def test_valid_device_certificate_opens_session(self) -> None:
        client = self.gateway.connect(self.certs.valid_cert, self.certs.valid_key)
        self.gateway.wait_for("device=device-01 event=session_open")
        client.close()
        self.gateway.wait_for("device=device-01 event=session_closed")

    def assert_tls_handshake_rejected(self, cert: Path | None, key: Path | None) -> None:
        client: ssl.SSLSocket | None = None
        try:
            client = self.gateway.connect(cert, key)
        except (ssl.SSLError, OSError):
            client = None
        if client is not None:
            try:
                received = client.recv(1)
            except (ssl.SSLError, OSError):
                received = b""
            finally:
                client.close()
            assert received == b""
        self.gateway.wait_for("event=client_rejected reason=tls_handshake_failed")
        assert "event=session_open" not in self.gateway.logs()

    def test_missing_client_certificate_is_rejected(self) -> None:
        self.assert_tls_handshake_rejected(None, None)

    def test_untrusted_client_ca_is_rejected(self) -> None:
        self.assert_tls_handshake_rejected(self.certs.untrusted_cert, self.certs.untrusted_key)

    def test_tls_12_is_rejected(self) -> None:
        with pytest.raises((ssl.SSLError, OSError)):
            self.gateway.connect(
                self.certs.valid_cert,
                self.certs.valid_key,
                minimum=ssl.TLSVersion.TLSv1_2,
                maximum=ssl.TLSVersion.TLSv1_2,
            )

    def test_wrong_extended_key_usage_is_rejected(self) -> None:
        self.assert_tls_handshake_rejected(self.certs.wrong_eku_cert, self.certs.wrong_eku_key)

    def test_certificate_without_uri_san_is_rejected(self) -> None:
        client = self.gateway.connect(self.certs.no_san_cert, self.certs.no_san_key)
        client_err: Exception | None = None
        try:
            data = client.recv(1)
            assert data == b""
        except (ssl.SSLError, OSError) as error:
            client_err = error
        finally:
            client.close()
        if client_err is not None:
            assert isinstance(client_err, (ssl.SSLError, OSError))
        self.gateway.wait_for("event=client_rejected reason=missing_or_invalid_uri_san")

    def test_multiple_uri_sans_are_rejected(self) -> None:
        client = self.gateway.connect(self.certs.multi_san_cert, self.certs.multi_san_key)
        client_err: Exception | None = None
        try:
            data = client.recv(1)
            assert data == b""
        except (ssl.SSLError, OSError) as error:
            client_err = error
        finally:
            client.close()
        if client_err is not None:
            assert isinstance(client_err, (ssl.SSLError, OSError))
        self.gateway.wait_for("event=client_rejected reason=missing_or_invalid_uri_san")

    def test_invalid_device_id_is_rejected(self) -> None:
        client = self.gateway.connect(self.certs.invalid_id_cert, self.certs.invalid_id_key)
        client_err: Exception | None = None
        try:
            data = client.recv(1)
            assert data == b""
        except (ssl.SSLError, OSError) as error:
            client_err = error
        finally:
            client.close()
        if client_err is not None:
            assert isinstance(client_err, (ssl.SSLError, OSError))
        self.gateway.wait_for("event=client_rejected reason=missing_or_invalid_uri_san")

    def test_client_role_certificate_opens_northbound_session(self) -> None:
        client = self.gateway.connect(self.certs.client_role_cert, self.certs.client_role_key)
        self.gateway.wait_for("client=operator-01 event=session_open")
        client.close()
        self.gateway.wait_for("client=operator-01 event=session_closed")
        assert "device=operator-01 event=session_open" not in self.gateway.logs()

    def test_duplicate_client_session_is_rejected_and_released(self) -> None:
        first = self.gateway.connect(self.certs.client_role_cert, self.certs.client_role_key)
        self.gateway.wait_for("client=operator-01 event=session_open")
        second = self.gateway.connect(self.certs.client_role_cert, self.certs.client_role_key)
        client_err: Exception | None = None
        try:
            data = second.recv(1)
            assert data == b""
        except (ssl.SSLError, OSError) as error:
            client_err = error
        finally:
            second.close()
        if client_err is not None:
            assert isinstance(client_err, (ssl.SSLError, OSError))
        self.gateway.wait_for("client=operator-01 event=session_rejected reason=duplicate_session")
        first.close()
        self.gateway.wait_for("client=operator-01 event=session_closed")
        self.gateway.wait_for("client=operator-01 event=session_unregistered")
        third = self.gateway.connect(self.certs.client_role_cert, self.certs.client_role_key)
        self.gateway.wait_for("client=operator-01 event=session_open", count=2)
        third.close()
        self.gateway.wait_for("client=operator-01 event=session_closed", count=2)

    def test_duplicate_device_session_is_rejected_and_released(self) -> None:
        first = self.gateway.connect(self.certs.valid_cert, self.certs.valid_key)
        self.gateway.wait_for("device=device-01 event=session_open")
        second = self.gateway.connect(self.certs.valid_cert, self.certs.valid_key)
        client_err: Exception | None = None
        try:
            data = second.recv(1)
            assert data == b""
        except (ssl.SSLError, OSError) as error:
            client_err = error
        finally:
            second.close()
        if client_err is not None:
            assert isinstance(client_err, (ssl.SSLError, OSError))
        self.gateway.wait_for("device=device-01 event=session_rejected reason=duplicate_session")
        first.close()
        self.gateway.wait_for("device=device-01 event=session_closed")
        self.gateway.wait_for("device=device-01 event=session_unregistered")
        third = self.gateway.connect(self.certs.valid_cert, self.certs.valid_key)
        self.gateway.wait_for("device=device-01 event=session_open", count=2)
        third.close()
        self.gateway.wait_for("device=device-01 event=session_closed", count=2)

    def test_gateway_control_health_rpc(self) -> None:
        client_peer = _TlsRpcPeer(self.gateway.connect(self.certs.client_role_cert, self.certs.client_role_key))
        try:
            health_method = client_peer.rpc_client.channel(1).rpcs.mcubridge.gateway.GatewayControl.Health
            res = health_method()
            assert not client_peer.errors
            assert res.status == pw_status.Status.OK
            assert res.response.ready
            assert res.response.active_device_sessions == 0
            assert res.response.active_client_sessions == 1
        finally:
            client_peer.close()
            self.gateway.wait_for("client=operator-01 event=session_closed")

    def test_device_telemetry_reporting(self) -> None:
        dev_peer = _TlsRpcPeer(self.gateway.connect(self.certs.valid_cert, self.certs.valid_key))
        try:
            report_method = dev_peer.rpc_client.channel(1).rpcs.mcubridge.gateway.DeviceTelemetry.ReportTelemetry
            req = gateway_control_pb2.TelemetryReport(
                daemon_metrics_blob=b"test_metrics_blob",
                system_status_blob=b"test_status_blob",
            )
            res = report_method(req)
            assert not dev_peer.errors
            assert res.status == pw_status.Status.OK
            assert res.response.acknowledged
            self.gateway.wait_for("device=device-01 event=telemetry_reported metrics_bytes=17 status_bytes=16")
        finally:
            dev_peer.close()
            self.gateway.wait_for("device=device-01 event=session_closed")

    def _dispatch_status(self, target_device_id: str, operation: str) -> Any:
        client_peer = _TlsRpcPeer(self.gateway.connect(self.certs.client_role_cert, self.certs.client_role_key))
        try:
            dispatch_method = client_peer.rpc_client.channel(1).rpcs.mcubridge.gateway.GatewayControl.Dispatch
            req = gateway_control_pb2.DispatchRequest(
                target_device_id=target_device_id,
                operation=operation,
                request_payload=b"",
                timeout_ms=1000,
            )
            status = dispatch_method(req).status
            assert not client_peer.errors
            return status
        finally:
            client_peer.close()
            self.gateway.wait_for("client=operator-01 event=session_closed")

    def test_gateway_control_dispatch_device_not_found(self) -> None:
        assert self._dispatch_status("missing-device", "rpc.pb.SystemService/Ping") == pw_status.Status.NOT_FOUND

    def test_gateway_control_dispatch_denied_by_acl(self) -> None:
        """ADR-0007: an operation the ACL does not grant is rejected before the device lookup."""
        status = self._dispatch_status("missing-device", "rpc.pb.SystemService/EnterBootloader")
        assert status == pw_status.Status.PERMISSION_DENIED
        self.gateway.wait_for("client=operator-01 event=dispatch_denied target=missing-device")

    def test_gateway_control_dispatch_end_to_end(self) -> None:
        client_peer = _TlsRpcPeer(self.gateway.connect(self.certs.client_role_cert, self.certs.client_role_key))
        dev_peer_ref: list[_TlsRpcPeer] = []

        def handle_edge_dispatch(pkt: Any) -> None:
            edge_req = gateway_control_pb2.EdgeDispatchRequest()
            edge_req.ParseFromString(pkt.payload)
            edge_resp = gateway_control_pb2.EdgeDispatchResponse(
                status_code=0,
                response_payload=edge_req.operation.encode("utf-8"),
            )
            resp_pkt = pw_rpc.packets.encode_response(pkt, response=edge_resp, status=pw_status.Status.OK)
            dev_peer_ref[0].send_packet(resp_pkt)

        dev_peer = _TlsRpcPeer(
            self.gateway.connect(self.certs.valid_cert, self.certs.valid_key),
            on_server_packet=handle_edge_dispatch,
        )
        dev_peer_ref.append(dev_peer)

        self.gateway.wait_for("device=device-01 event=session_open")
        self.gateway.wait_for("client=operator-01 event=session_open")

        try:
            dispatch_method = client_peer.rpc_client.channel(1).rpcs.mcubridge.gateway.GatewayControl.Dispatch
            req = gateway_control_pb2.DispatchRequest(
                target_device_id="device-01",
                operation="rpc.pb.SystemService/Ping",
                request_payload=b"test_payload",
                timeout_ms=3000,
            )
            res = dispatch_method(req)
            assert not client_peer.errors
            assert not dev_peer.errors
            assert res.status == pw_status.Status.OK
            assert res.response.status_code == 0
            # The canonical operation name reaches the device unchanged.
            assert res.response.response_payload == b"rpc.pb.SystemService/Ping"
        finally:
            client_peer.close()
            dev_peer.close()
            self.gateway.wait_for("client=operator-01 event=session_closed")
            self.gateway.wait_for("device=device-01 event=session_closed")

    def test_staged_fetch_device_uploads_during_dispatch(self) -> None:
        """ADR-0007: EepromService/Read hands the device's upload to the client.

        The daemon answers the dispatch only after writing eeprom.bin to its
        device session, so the gateway must serve that transfer while the
        client's Dispatch call is still waiting for the device response.
        """
        client_peer = _TlsRpcPeer(
            self.gateway.connect(self.certs.client_role_cert, self.certs.client_role_key),
            default_unary_timeout_s=10.0,
        )
        eeprom_image = bytes((i * 7) & 0xFF for i in range(EEPROM_RESOURCE_BYTES))
        device_errors: list[BaseException] = []
        dev_peer_ref: list[_TlsRpcPeer] = []
        dev_transfer_ref: list[PwTransferManager] = []

        def serve_edge_dispatch(pkt: Any) -> None:
            try:
                dev_transfer_ref[0].write(EEPROM_RESOURCE_ID, eeprom_image)
                edge_resp = gateway_control_pb2.EdgeDispatchResponse(status_code=200)
                resp_pkt = pw_rpc.packets.encode_response(pkt, response=edge_resp, status=pw_status.Status.OK)
                dev_peer_ref[0].send_packet(resp_pkt)
            except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
                device_errors.append(exc)

        dev_peer = _TlsRpcPeer(
            self.gateway.connect(self.certs.valid_cert, self.certs.valid_key),
            default_unary_timeout_s=10.0,
            on_server_packet=lambda pkt: threading.Thread(target=serve_edge_dispatch, args=(pkt,), daemon=True).start(),
        )
        dev_peer_ref.append(dev_peer)
        dev_transfer_ref.append(
            PwTransferManager(dev_peer.rpc_client.channel(1).rpcs.pw.transfer.Transfer, default_response_timeout_s=5.0)
        )

        self.gateway.wait_for("device=device-01 event=session_open")
        self.gateway.wait_for("client=operator-01 event=session_open")

        try:
            dispatch_method = client_peer.rpc_client.channel(1).rpcs.mcubridge.gateway.GatewayControl.Dispatch
            req = gateway_control_pb2.DispatchRequest(
                target_device_id="device-01",
                operation="rpc.pb.EepromService/Read",
                timeout_ms=8000,
            )
            res = dispatch_method(req)
            assert not device_errors
            assert not client_peer.errors
            assert not dev_peer.errors
            assert res.status == pw_status.Status.OK
            assert res.response.status_code == 200
            client_transfer = PwTransferManager(
                client_peer.rpc_client.channel(1).rpcs.pw.transfer.Transfer, default_response_timeout_s=5.0
            )
            assert client_transfer.read(EEPROM_RESOURCE_ID) == eeprom_image
        finally:
            client_peer.close()
            dev_peer.close()
            self.gateway.wait_for("client=operator-01 event=session_closed")
            self.gateway.wait_for("device=device-01 event=session_closed")

    def test_gateway_transfer_bounded_file_read_and_write(self) -> None:
        client_peer = _TlsRpcPeer(self.gateway.connect(self.certs.client_role_cert, self.certs.client_role_key))
        self.gateway.wait_for("client=operator-01 event=session_open")

        try:
            transfer_service = client_peer.rpc_client.channel(1).rpcs.pw.transfer.Transfer
            transfer_manager = PwTransferManager(transfer_service, default_response_timeout_s=5.0)
            payload = b"test payload for bounded file transfer via pw_transfer"
            transfer_manager.write(2, payload)
            read_back = transfer_manager.read(2)
            assert read_back == payload

            # ADR-0007: staged in the client's private directory.
            client_dir = self.gateway.staging_dir / "clients" / "operator-01"
            staging_file = client_dir / "staging.bin"
            assert staging_file.is_file()
            assert staging_file.read_bytes() == payload
            assert (self.gateway.staging_dir.stat().st_mode & 0o777) == 0o700

            # The EEPROM staging resource accepts exactly the MCU resource size.
            eeprom_image = bytes(range(256)) * 15 + bytes(240)
            assert len(eeprom_image) == EEPROM_RESOURCE_BYTES
            transfer_manager.write(EEPROM_RESOURCE_ID, eeprom_image)
            assert transfer_manager.read(EEPROM_RESOURCE_ID) == eeprom_image
            with pytest.raises(PwTransferError):
                transfer_manager.write(EEPROM_RESOURCE_ID, eeprom_image + b"\x00")
            assert not client_peer.errors
        finally:
            client_peer.close()
            self.gateway.wait_for("client=operator-01 event=session_closed")
        # Staged data is not kept after the session closes.
        assert not (self.gateway.staging_dir / "clients" / "operator-01").exists()

    def test_device_session_registers_transfer_handlers(self) -> None:
        """Regression: device sessions must serve pw_transfer resources 1 and 2.

        The daemon reads the staged file (resource 2) from its device session
        during FileTransferService/PushToLinux. Registering handlers before the
        transfer thread runs made RegisterHandler() time out, so every device
        transfer failed with NOT_FOUND.
        """
        dev_peer = _TlsRpcPeer(self.gateway.connect(self.certs.valid_cert, self.certs.valid_key))
        self.gateway.wait_for("device=device-01 event=session_open")

        try:
            assert "event=transfer_handler_registration_failed" not in self.gateway.logs()

            transfer_service = dev_peer.rpc_client.channel(1).rpcs.pw.transfer.Transfer
            transfer_manager = PwTransferManager(transfer_service, default_response_timeout_s=5.0)

            buffer_payload = b"device session buffer resource"
            transfer_manager.write(1, buffer_payload)
            assert transfer_manager.read(1) == buffer_payload

            file_payload = b"device session staged file resource"
            transfer_manager.write(2, file_payload)
            assert transfer_manager.read(2) == file_payload

            # ADR-0007: devices stage under their own namespace.
            staging_file = self.gateway.staging_dir / "devices" / "device-01" / "staging.bin"
            assert staging_file.is_file()
            assert staging_file.read_bytes() == file_payload

            # Multi-chunk reads: before PW_RPC_ENCODING_BUFFER_SIZE_BYTES was
            # raised, any read above one small chunk aborted with DATA_LOSS.
            large_payload = bytes(i & 0xFF for i in range(8 * 1024))
            transfer_manager.write(2, large_payload)
            assert transfer_manager.read(2) == large_payload
            eeprom_image = bytes(range(256)) * 15 + bytes(240)
            transfer_manager.write(EEPROM_RESOURCE_ID, eeprom_image)
            assert transfer_manager.read(EEPROM_RESOURCE_ID) == eeprom_image
            assert not dev_peer.errors
        finally:
            dev_peer.close()
            self.gateway.wait_for("device=device-01 event=session_closed")
        assert not (self.gateway.staging_dir / "devices" / "device-01").exists()


if __name__ == "__main__":
    pytest.main([__file__, "-vv"])


def test_gateway_refuses_symlinked_staging_root(tmp_path: Path) -> None:
    """ADR-0007: a staging root that is a symlink could redirect staged files; startup must fail."""
    if not BINARY.is_file():
        pytest.skip(f"Gateway binary not found: {BINARY}")
    certs = Certificates(tmp_path)
    target = tmp_path / "elsewhere"
    target.mkdir()
    link = tmp_path / "staging-link"
    link.symlink_to(target, target_is_directory=True)
    result = subprocess.run(
        [
            str(BINARY),
            "--cert",
            str(certs.server_cert),
            "--key",
            str(certs.server_key),
            "--client-ca",
            str(certs.ca_cert),
            "--staging-dir",
            str(link),
            "--bind",
            "127.0.0.1",
            "--port",
            "0",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 1
    assert "staging path is not a directory (or is a symlink)" in result.stderr

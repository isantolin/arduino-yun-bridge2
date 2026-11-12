from __future__ import annotations

import os
import socket
import ssl
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
BINARY = Path(
    os.environ.get(
        "MCUBRIDGE_GATEWAY_BINARY",
        REPO_ROOT / "build/mcubridge-gateway/mcubridge-gateway-cpp",
    )
)


def _run(*args: str) -> None:
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


class Certificates:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.ca_key = root / "ca.key"
        self.ca_cert = root / "ca.crt"
        _run(
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(self.ca_key), "-out", str(self.ca_cert),
            "-subj", "/CN=MCUBridge Test CA", "-days", "2", "-sha256",
            "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign",
        )
        self.server_cert, self.server_key = self._issue(
            "gateway", "DNS:localhost", "serverAuth"
        )
        self.valid_cert, self.valid_key = self._issue(
            "valid-device", "URI:spiffe://mcubridge/device/device-01", "clientAuth"
        )
        self.no_san_cert, self.no_san_key = self._issue(
            "missing-san", None, "clientAuth"
        )
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
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(other_key), "-out", str(other_cert),
            "-subj", "/CN=Untrusted Test CA", "-days", "2", "-sha256",
            "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign",
        )
        self.untrusted_cert, self.untrusted_key = self._issue(
            "untrusted", "URI:spiffe://mcubridge/device/untrusted", "clientAuth",
            ca_cert=other_cert, ca_key=other_key, serial_dir=other,
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
            "openssl", "req", "-new", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(csr), "-subj", f"/CN={name}",
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
            "openssl", "x509", "-req", "-in", str(csr),
            "-CA", str(issuer_cert), "-CAkey", str(issuer_key),
            "-CAcreateserial", "-CAserial", str(serial / "ca.srl"),
            "-out", str(cert), "-days", "2", "-sha256",
            "-extfile", str(ext),
        )
        return cert, key


class GatewayProcess:
    def __init__(self, root: Path, certs: Certificates) -> None:
        self._certs = certs
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        self.port = probe.getsockname()[1]
        probe.close()
        self.log_path = root / "gateway.log"
        self.log_file = self.log_path.open("w+", encoding="utf-8")
        self.process = subprocess.Popen(
            [
                str(BINARY), "--cert", str(certs.server_cert),
                "--key", str(certs.server_key), "--client-ca", str(certs.ca_cert),
                "--bind", "127.0.0.1", "--port", str(self.port),
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
            self.assertEqual(received, b"")
        self.gateway.wait_for("event=client_rejected reason=tls_handshake_failed")
        self.assertNotIn("event=session_open", self.gateway.logs())

    def test_missing_client_certificate_is_rejected(self) -> None:
        self.assert_tls_handshake_rejected(None, None)

    def test_untrusted_client_ca_is_rejected(self) -> None:
        self.assert_tls_handshake_rejected(self.certs.untrusted_cert, self.certs.untrusted_key)

    def test_tls_12_is_rejected(self) -> None:
        with self.assertRaises((ssl.SSLError, OSError)):
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
        try:
            client.recv(1)
        except (ssl.SSLError, OSError) as error:
            self.assertIsInstance(error, (ssl.SSLError, OSError))
        finally:
            client.close()
        self.gateway.wait_for("event=client_rejected reason=missing_or_invalid_uri_san")

    def test_multiple_uri_sans_are_rejected(self) -> None:
        client = self.gateway.connect(self.certs.multi_san_cert, self.certs.multi_san_key)
        try:
            client.recv(1)
        except (ssl.SSLError, OSError) as error:
            self.assertIsInstance(error, (ssl.SSLError, OSError))
        finally:
            client.close()
        self.gateway.wait_for("event=client_rejected reason=missing_or_invalid_uri_san")

    def test_invalid_device_id_is_rejected(self) -> None:
        client = self.gateway.connect(self.certs.invalid_id_cert, self.certs.invalid_id_key)
        try:
            client.recv(1)
        except (ssl.SSLError, OSError) as error:
            self.assertIsInstance(error, (ssl.SSLError, OSError))
        finally:
            client.close()
        self.gateway.wait_for("event=client_rejected reason=missing_or_invalid_uri_san")

    def test_client_role_certificate_opens_northbound_session(self) -> None:
        client = self.gateway.connect(self.certs.client_role_cert, self.certs.client_role_key)
        self.gateway.wait_for("client=operator-01 event=session_open")
        client.close()
        self.gateway.wait_for("client=operator-01 event=session_closed")
        self.assertNotIn("device=operator-01 event=session_open", self.gateway.logs())

    def test_duplicate_client_session_is_rejected_and_released(self) -> None:
        first = self.gateway.connect(self.certs.client_role_cert, self.certs.client_role_key)
        self.gateway.wait_for("client=operator-01 event=session_open")
        second = self.gateway.connect(self.certs.client_role_cert, self.certs.client_role_key)
        try:
            second.recv(1)
        except (ssl.SSLError, OSError) as error:
            self.assertIsInstance(error, (ssl.SSLError, OSError))
        finally:
            second.close()
        self.gateway.wait_for("client=operator-01 event=session_rejected reason=duplicate_session")
        first.close()
        self.gateway.wait_for("client=operator-01 event=session_closed")
        self.gateway.wait_for("client=operator-01 event=session_unregistered")

    def test_duplicate_device_session_is_rejected_and_released(self) -> None:
        first = self.gateway.connect(self.certs.valid_cert, self.certs.valid_key)
        self.gateway.wait_for("device=device-01 event=session_open")
        second = self.gateway.connect(self.certs.valid_cert, self.certs.valid_key)
        try:
            second.recv(1)
        except (ssl.SSLError, OSError) as error:
            self.assertIsInstance(error, (ssl.SSLError, OSError))
        finally:
            second.close()
        self.gateway.wait_for("device=device-01 event=session_rejected reason=duplicate_session")
        first.close()
        self.gateway.wait_for("device=device-01 event=session_closed")
        self.gateway.wait_for("device=device-01 event=session_unregistered")
        third = self.gateway.connect(self.certs.valid_cert, self.certs.valid_key)
        self.gateway.wait_for("device=device-01 event=session_open", count=2)
        third.close()
        self.gateway.wait_for("device=device-01 event=session_closed", count=2)


if __name__ == "__main__":
    unittest.main()

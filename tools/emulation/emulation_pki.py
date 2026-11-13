"""Ephemeral mTLS PKI for emulation runs against the standalone Pigweed Gateway.

The Gateway (ADR-0003) only accepts TLS 1.3 peers that present a certificate
issued by its client CA with exactly one SPIFFE URI SAN:
``spiffe://mcubridge/device/<id>`` for edge daemons and
``spiffe://mcubridge/client/<id>`` for northbound clients. Emulation runs build
a throwaway CA and the matching leaf certificates with ``cryptography`` (already
a runtime dependency) so the daemon, the Gateway and the example clients use the
same trust chain as production.
"""

from __future__ import annotations

import datetime
import ipaddress
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

DEVICE_URI_PREFIX: Final[str] = "spiffe://mcubridge/device/"
CLIENT_URI_PREFIX: Final[str] = "spiffe://mcubridge/client/"
EMULATION_CLIENT_ID: Final[str] = "emulation"
_VALIDITY: Final[datetime.timedelta] = datetime.timedelta(days=2)


@dataclass(frozen=True, slots=True)
class Identity:
    """Certificate and private key files for one TLS peer."""

    cert: Path
    key: Path


class EmulationPki:
    """Throwaway CA plus Gateway, device and client identities for one emulation run."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._ca_key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.datetime.now(datetime.UTC)
        ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "MCUBridge Emulation CA")])
        ca_public = self._ca_key.public_key()
        self._ca_cert = (
            x509.CertificateBuilder()
            .subject_name(ca_name)
            .issuer_name(ca_name)
            .public_key(ca_public)
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + _VALIDITY)
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=False,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_public), critical=False)
            .sign(self._ca_key, hashes.SHA256())
        )
        self.ca_cert = self.root / "ca.crt"
        self.ca_cert.write_bytes(self._ca_cert.public_bytes(serialization.Encoding.PEM))
        self.gateway = self._issue(
            "gateway",
            [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))],
            ExtendedKeyUsageOID.SERVER_AUTH,
        )
        self.client = self._issue(
            f"client-{EMULATION_CLIENT_ID}",
            [x509.UniformResourceIdentifier(f"{CLIENT_URI_PREFIX}{EMULATION_CLIENT_ID}")],
            ExtendedKeyUsageOID.CLIENT_AUTH,
        )
        self._devices: dict[str, Identity] = {}

    def device(self, device_id: str) -> Identity:
        """Return (issuing on first use) the device identity bound to ``device_id``."""
        identity = self._devices.get(device_id)
        if identity is None:
            identity = self._issue(
                f"device-{device_id}",
                [x509.UniformResourceIdentifier(f"{DEVICE_URI_PREFIX}{device_id}")],
                ExtendedKeyUsageOID.CLIENT_AUTH,
            )
            self._devices[device_id] = identity
        return identity

    def _issue(self, name: str, sans: list[x509.GeneralName], eku: x509.ObjectIdentifier) -> Identity:
        key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.datetime.now(datetime.UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
            .issuer_name(self._ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + _VALIDITY)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=False,
                    crl_sign=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(x509.ExtendedKeyUsage([eku]), critical=False)
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(self._ca_key.public_key()), critical=False
            )
            .sign(self._ca_key, hashes.SHA256())
        )
        cert_path = self.root / f"{name}.crt"
        key_path = self.root / f"{name}.key"
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        key_path.chmod(0o600)
        return Identity(cert=cert_path, key=key_path)

    def export_client_env(self, env: dict[str, str] | None = None) -> None:
        """Point example clients (``bridge_session``) at the emulation client identity."""
        target = os.environ if env is None else env
        target["MCUBRIDGE_GATEWAY_CAFILE"] = str(self.ca_cert)
        target["MCUBRIDGE_GATEWAY_CERTFILE"] = str(self.client.cert)
        target["MCUBRIDGE_GATEWAY_KEYFILE"] = str(self.client.key)


class _PkiHolder:
    instance: EmulationPki | None = None


def get_emulation_pki() -> EmulationPki:
    """Return the per-process emulation PKI shared by the Gateway, daemons and clients."""
    if _PkiHolder.instance is None:
        root = Path(os.environ.get("MCUBRIDGE_EMULATION_PKI_DIR") or tempfile.mkdtemp(prefix="mcubridge_pki_"))
        _PkiHolder.instance = EmulationPki(root)
    return _PkiHolder.instance

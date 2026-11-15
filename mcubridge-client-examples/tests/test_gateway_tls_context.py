"""mTLS client context for the standalone Gateway, built from the emulation PKI."""

from __future__ import annotations

import ssl
from pathlib import Path

import pytest
from cryptography import x509
from mcubridge_client.cli import build_gateway_ssl_context

from tools.emulation.emulation_pki import CLIENT_URI_PREFIX, DEVICE_URI_PREFIX, EmulationPki


def _uris(cert_path: Path) -> list[str]:
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return san.get_values_for_type(x509.UniformResourceIdentifier)


def test_emulation_pki_issues_single_spiffe_identities(tmp_path: Path) -> None:
    pki = EmulationPki(tmp_path)
    assert _uris(pki.client.cert) == [f"{CLIENT_URI_PREFIX}emulation"]
    device = pki.device("yun-01")
    assert _uris(device.cert) == [f"{DEVICE_URI_PREFIX}yun-01"]
    assert pki.device("yun-01") is device
    assert (device.key.stat().st_mode & 0o777) == 0o600


def test_build_gateway_ssl_context_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("MCUBRIDGE_GATEWAY_CAFILE", "MCUBRIDGE_GATEWAY_CERTFILE", "MCUBRIDGE_GATEWAY_KEYFILE"):
        monkeypatch.delenv(name, raising=False)
    assert build_gateway_ssl_context() is None

    pki = EmulationPki(tmp_path)
    env: dict[str, str] = {}
    pki.export_client_env(env)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    context = build_gateway_ssl_context()
    assert context is not None
    assert context.minimum_version == ssl.TLSVersion.TLSv1_3
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True

    monkeypatch.delenv("MCUBRIDGE_GATEWAY_KEYFILE")
    with pytest.raises(ValueError, match="must be set together"):
        build_gateway_ssl_context()

"""Reusable TLS helpers for YunBridge components."""
from __future__ import annotations

import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..const import MQTT_TLS_MIN_VERSION
from .settings import RuntimeConfig


@dataclass(slots=True)
class TLSMaterial:
    cafile: str
    certfile: Optional[str]
    keyfile: Optional[str]


def resolve_tls_material(
    config: RuntimeConfig,
    *,
    cafile_override: Optional[str] = None,
    cert_override: Optional[str] = None,
    key_override: Optional[str] = None,
) -> TLSMaterial:
    cafile = cafile_override or config.mqtt_cafile or ""
    if not cafile:
        raise RuntimeError("MQTT TLS CA file is required")

    certfile = cert_override or config.mqtt_certfile
    keyfile = key_override or config.mqtt_keyfile

    return TLSMaterial(cafile=cafile, certfile=certfile, keyfile=keyfile)


def build_tls_context(material: TLSMaterial) -> ssl.SSLContext:
    cafile_path = Path(material.cafile)
    if not cafile_path.exists():
        raise FileNotFoundError(f"TLS CA file does not exist: {cafile_path}")

    context = ssl.create_default_context(
        ssl.Purpose.SERVER_AUTH,
        cafile=str(cafile_path),
    )
    context.minimum_version = MQTT_TLS_MIN_VERSION
    if material.certfile and material.keyfile:
        context.load_cert_chain(material.certfile, material.keyfile)
    return context


__all__ = ["TLSMaterial", "resolve_tls_material", "build_tls_context"]

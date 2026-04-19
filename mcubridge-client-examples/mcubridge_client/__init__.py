"""Minimalistic helpers for MCU Bridge examples."""

from __future__ import annotations

import os
import ssl
from pathlib import Path

from aiomqtt import Client, ProtocolVersion

from .definitions import (
    DEFAULT_MQTT_HOST,
    DEFAULT_MQTT_PORT,
    DEFAULT_MQTT_TOPIC,
    SpiBitOrder,
    SpiMode,
    build_bridge_args,
)
from .env import dump_client_env, read_uci_general
from .protocol import Topic
from .spi import SpiDevice

__all__ = [
    "DEFAULT_MQTT_HOST",
    "DEFAULT_MQTT_PORT",
    "DEFAULT_MQTT_TOPIC",
    "Topic",
    "get_client",
    "build_bridge_args",
    "dump_client_env",
    "SpiBitOrder",
    "SpiMode",
    "SpiDevice",
]

_UCI_GENERAL = read_uci_general()

def _default_tls_context() -> ssl.SSLContext | None:
    mqtt_tls = _UCI_GENERAL.get("mqtt_tls", "0")
    if str(mqtt_tls).strip() not in {"1", "true", "yes", "on"}:
        return None
    try:
        cafile = (_UCI_GENERAL.get("mqtt_cafile") or "").strip()
        if not cafile and Path("/etc/ssl/certs/ca-certificates.crt").exists():
            cafile = "/etc/ssl/certs/ca-certificates.crt"

        ctx = (
            ssl.create_default_context(cafile=cafile)
            if cafile
            else ssl.create_default_context()
        )
        return ctx
    except (ssl.SSLError, OSError, ValueError):
        return None

def get_client(
    host: str = DEFAULT_MQTT_HOST,
    port: int = DEFAULT_MQTT_PORT,
    username: str | None = None,
    password: str | None = None,
    tls_context: ssl.SSLContext | None = None,
) -> Client:
    """Return a configured aiomqtt.Client for MCU Bridge examples."""
    return Client(
        hostname=host,
        port=port,
        username=username,
        password=password,
        protocol=ProtocolVersion.V5,
        tls_context=tls_context or _default_tls_context(),
    )

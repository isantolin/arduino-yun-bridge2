"""MQTT utility helpers for MCU Bridge components.

Provides shared logic for TLS configuration and MQTT client setup
between the daemon and external scripts (like CGI).
"""

from __future__ import annotations

import logging
import ssl
from pathlib import Path
from typing import Any

from mcubridge.config.const import MQTT_TLS_MIN_VERSION
from mcubridge.config.settings import RuntimeConfig

logger = logging.getLogger("mcubridge.util.mqtt")


def configure_tls_context(config: RuntimeConfig) -> ssl.SSLContext | None:
    """Create an ssl.SSLContext based on the provided RuntimeConfig."""
    if not config.tls_enabled:
        return None

    try:
        if config.mqtt_cafile:
            if not Path(config.mqtt_cafile).exists():
                raise RuntimeError(f"MQTT TLS CA file missing: {config.mqtt_cafile}")
            context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=config.mqtt_cafile)
        else:
            context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)

        context.minimum_version = MQTT_TLS_MIN_VERSION

        if config.mqtt_tls_insecure:
            context.check_hostname = False
            # Verify_mode could also be set to ssl.CERT_NONE if needed,
            # but usually check_hostname=False is enough for "insecure" in this context.

        if config.mqtt_certfile or config.mqtt_keyfile:
            if not (config.mqtt_certfile and config.mqtt_keyfile):
                raise ValueError("Both mqtt_certfile and mqtt_keyfile must be provided for mTLS.")
            context.load_cert_chain(config.mqtt_certfile, config.mqtt_keyfile)

        return context
    except (OSError, ssl.SSLError, ValueError) as exc:
        raise RuntimeError(f"TLS setup failed: {exc}") from exc


def apply_tls_to_paho(client: Any, config: RuntimeConfig) -> None:
    """Apply TLS settings to a paho-mqtt Client instance."""
    if not config.tls_enabled:
        return

    cafile = config.mqtt_cafile
    certfile = config.mqtt_certfile
    keyfile = config.mqtt_keyfile
    tls_insecure = config.mqtt_tls_insecure

    tls_kwargs = {}
    if cafile:
        tls_kwargs["ca_certs"] = cafile
    if certfile:
        tls_kwargs["certfile"] = certfile
    if keyfile:
        tls_kwargs["keyfile"] = keyfile

    client.tls_set(**tls_kwargs)

    if tls_insecure:
        client.tls_insecure_set(True)

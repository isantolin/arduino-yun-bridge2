"""Helpers to introspect MCU Bridge client configuration.

The ecosystem uses UCI as the single source of truth; example scripts should
not rely on environment variables for configuration.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterable
from pathlib import Path


def _is_openwrt() -> bool:
    return Path("/etc/openwrt_release").exists() or Path("/etc/openwrt_version").exists()


def _read_uci_general() -> dict[str, str]:
    try:
        from mcubridge.config.common import get_uci_config
    except (ImportError, RuntimeError):
        return {}

    if not _is_openwrt():
        return {}

    try:
        config = get_uci_config()
    except RuntimeError:
        return {}

    clean: dict[str, str] = {}
    for key, value in config.items():
        if str(key).startswith((".", "_")):
            continue
        clean[str(key)] = str(value)
    return clean


def dump_client_env(logger: logging.Logger | None = None) -> None:
    """Log the MQTT-related UCI settings for quick diagnostics."""

    def _emit(message: str) -> None:
        if logger is not None:
            logger.info(message)
        else:
            sys.stdout.write(message + "\n")
            sys.stdout.flush()

    _emit("MCU Bridge client configuration snapshot (UCI):")
    cfg = _read_uci_general()
    if not cfg:
        _emit("  <UCI unavailable or mcubridge.general missing>")
        return

    for key in (
        "mqtt_host",
        "mqtt_port",
        "mqtt_tls",
        "mqtt_tls_insecure",
        "mqtt_user",
        "mqtt_topic",
        "mqtt_cafile",
    ):
        value = cfg.get(key)
        if not value:
            _emit(f"  {key}=<unset>")
        elif key == "mqtt_user":
            _emit(f"  {key}='{value}'")
        else:
            _emit(f"  {key}='{value}'")


__all__: Iterable[str] = ("dump_client_env",)

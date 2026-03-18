"""Helpers to introspect MCU Bridge client configuration.

The ecosystem uses UCI as the single source of truth; example scripts should
not rely on environment variables for configuration.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, cast


def _is_openwrt() -> bool:
    if os.environ.get("MCUBRIDGE_FORCE_UCI") == "1":
        return True
    return Path("/etc/openwrt_release").exists() or Path("/etc/openwrt_version").exists()


def read_uci_general() -> dict[str, str]:
    if not _is_openwrt():
        return {}

    spec = importlib.util.find_spec("mcubridge.config.common")
    if spec is None:
        return {}

    module = importlib.import_module("mcubridge.config.common")
    get_uci_config = cast(Callable[[], dict[str, Any]] | None, getattr(module, "get_uci_config", None))
    if not callable(get_uci_config):
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
    cfg = read_uci_general()
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


__all__: Iterable[str] = ("dump_client_env", "read_uci_general")

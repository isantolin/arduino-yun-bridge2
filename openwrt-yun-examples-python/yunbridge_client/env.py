"""Helpers to introspect Yun Bridge client configuration.

The ecosystem uses UCI as the single source of truth; example scripts should
not rely on environment variables for configuration.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterable


def _read_uci_general() -> dict[str, str]:
    try:
        from uci import Uci  # type: ignore
    except ImportError:
        return {}

    try:
        with Uci() as cursor:
            section = cursor.get_all("yunbridge", "general")
            if not section:
                return {}
            clean: dict[str, str] = {}
            for key, value in section.items():
                if str(key).startswith((".", "_")):
                    continue
                clean[str(key)] = str(value)
            return clean
    except Exception:
        return {}


def dump_client_env(logger: logging.Logger | None = None) -> None:
    """Log the MQTT-related UCI settings for quick diagnostics."""

    def _emit(message: str) -> None:
        if logger is not None:
            logger.info(message)
        else:
            sys.stdout.write(message + "\n")
            sys.stdout.flush()

    _emit("Yun Bridge client configuration snapshot (UCI):")
    cfg = _read_uci_general()
    if not cfg:
        _emit("  <UCI unavailable or yunbridge.general missing>")
        return

    for key in ("mqtt_host", "mqtt_port", "mqtt_tls", "mqtt_user", "mqtt_topic", "mqtt_cafile"):
        value = cfg.get(key)
        if not value:
            _emit(f"  {key}=<unset>")
        elif key == "mqtt_user":
            _emit(f"  {key}='{value}'")
        else:
            _emit(f"  {key}='{value}'")


__all__: Iterable[str] = ("dump_client_env",)

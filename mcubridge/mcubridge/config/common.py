"""Utility helpers shared across MCU Bridge packages."""

from __future__ import annotations

import logging
from typing import Any, Final, cast

logger = logging.getLogger(__name__)

_UCI_PACKAGE: Final[str] = "mcubridge"
_UCI_SECTION: Final[str] = "general"


def get_uci_config() -> dict[str, Any]:
    """Fetch configuration from OpenWrt UCI system with safe fallbacks."""
    try:
        import uci  # type: ignore
        with uci.Uci() as cursor:
            try:
                section = cursor.get_all(_UCI_PACKAGE, _UCI_SECTION)
            except Exception as e:
                # On OpenWrt, missing section is a critical config error
                logger.warning("UCI section %s.%s missing or unreadable: %s", _UCI_PACKAGE, _UCI_SECTION, e)
                return get_default_config()

            if not section:
                return get_default_config()

            # Clean and cast the UCI dictionary
            from collections.abc import Iterable
            clean_config: dict[str, Any] = {
                str(k): (" ".join(map(str, cast(Iterable[Any], v))) if isinstance(v, (list, tuple)) else v)
                for k, v in cast(dict[Any, Any], section).items()
                if not str(k).startswith((".", "_"))
            }
            return clean_config
    except (ImportError, AttributeError, Exception):
        # Fallback for non-OpenWrt environments (e.g. dev/test)
        return get_default_config()


def get_default_config() -> dict[str, Any]:
    """Return the default configuration as a dictionary."""
    from mcubridge.config import const
    from mcubridge.protocol import protocol
    return {
        "serial_port": const.DEFAULT_SERIAL_PORT,
        "serial_baud": protocol.DEFAULT_BAUDRATE,
        "serial_safe_baud": protocol.DEFAULT_SAFE_BAUDRATE,
        "serial_retry_attempts": protocol.DEFAULT_RETRY_LIMIT,
        "serial_retry_timeout": const.DEFAULT_SERIAL_RETRY_TIMEOUT,
        "serial_response_timeout": const.DEFAULT_SERIAL_RESPONSE_TIMEOUT,
        "mqtt_host": const.DEFAULT_MQTT_HOST,
        "mqtt_port": const.DEFAULT_MQTT_PORT,
        "debug": const.DEFAULT_DEBUG_LOGGING,
    }


__all__: Final[tuple[str, ...]] = (
    "get_default_config",
    "get_uci_config",
)

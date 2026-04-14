"""Utility helpers shared across MCU Bridge packages."""

from __future__ import annotations

from typing import Any, Final, cast

import structlog

logger = structlog.get_logger(__name__)

_UCI_PACKAGE: Final[str] = "mcubridge"
_UCI_SECTION: Final[str] = "general"


def get_uci_config() -> dict[str, Any]:
    """Fetch configuration from OpenWrt UCI system with safe fallbacks.

    [SIL-2] Tipado estricto de excepciones y aislamiento de fallos para garantizar
    la integridad del sistema de configuración.
    """
    try:
        import uci

        # [SIL-2] Verify this is the real OpenWrt UCI library and not a host collision
        if not hasattr(uci, "Uci") and not hasattr(uci, "UCI"):
            raise ImportError("Incompatible uci library")
    except ImportError:
        # Fallback for non-OpenWrt environments (e.g. dev/test)
        return get_default_config()

    # [SIL-2] Dynamic exception detection to avoid hard dependency
    UciException = getattr(uci, "UciException", RuntimeError)

    try:
        # [SIL-2] Dynamic class detection to handle library variations
        UciClass = getattr(uci, "Uci", None) or getattr(uci, "UCI")
        with UciClass() as cursor:
            # Verify it's a real cursor with get_all method
            if not hasattr(cursor, "get_all"):
                return get_default_config()
            section = cursor.get_all(_UCI_PACKAGE, _UCI_SECTION)
            if not section:
                return get_default_config()

            # Clean and cast the UCI dictionary
            from collections.abc import Iterable

            config_dict: dict[str, Any] = {
                str(k): (
                    " ".join(map(str, cast(Iterable[Any], v)))
                    if isinstance(v, (list, tuple))
                    else v
                )
                for k, v in section.items()
                if not str(k).startswith((".", "_"))
            }
            return config_dict
    except (RuntimeError, ValueError, OSError, AttributeError, UciException) as err:  # type: ignore[reportUnknownVariableType]
        # [SIL-2] Log only specific configuration/system errors to syslog.
        logger.warning("UCI configuration error, falling back to safe defaults: %s", err)

    return get_default_config()


def get_default_config() -> dict[str, Any]:
    """Return the default configuration as a dictionary (SIL 2)."""
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
        "mqtt_tls": True,
        "mqtt_topic": protocol.MQTT_DEFAULT_TOPIC_PREFIX,
        "mqtt_spool_dir": const.DEFAULT_MQTT_SPOOL_DIR,
        "mqtt_spool_limit": const.DEFAULT_MQTT_QUEUE_LIMIT,
        "file_system_root": const.DEFAULT_FILE_SYSTEM_ROOT,
        "debug": const.DEFAULT_DEBUG_LOGGING,
        "status_interval": const.DEFAULT_STATUS_INTERVAL,
        "process_timeout": const.DEFAULT_PROCESS_TIMEOUT,
    }


__all__: Final[tuple[str, ...]] = (
    "get_default_config",
    "get_uci_config",
)

"""Utility helpers shared across MCU Bridge packages."""

from __future__ import annotations

from typing import Any, Final
import structlog
from mcubridge.protocol import protocol

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

        # [SIL-2] Dynamic class detection to handle library variations
        UciClass = getattr(uci, "Uci", None) or getattr(uci, "UCI", None)
        if UciClass is None:
            return get_default_config()

        cursor_obj = UciClass()

        with cursor_obj as cursor:
            # Verify it's a real cursor with get_all method
            if not hasattr(cursor, "get_all"):
                return get_default_config()
            section = cursor.get_all(_UCI_PACKAGE, _UCI_SECTION)
            if not section:
                return get_default_config()

            # Clean UCI dictionary (remove internal keys)
            return {str(k): v for k, v in section.items() if not str(k).startswith((".", "_"))}
    except ImportError:
        return get_default_config()
    except (RuntimeError, ValueError, OSError) as err:
        # [SIL-2] Log only specific configuration/system errors to syslog.
        logger.error("UCI system system error", error=err)

    return get_default_config()


def get_default_config() -> dict[str, Any]:
    """Return the complete default configuration as a dictionary (SIL 2).

    Derives reflexively from protocol.RUNTIME_CONFIG_DEFAULTS (SSOT in mcubridge.proto).
    """
    return dict(protocol.RUNTIME_CONFIG_DEFAULTS)

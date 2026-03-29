"""Utility helpers shared across MCU Bridge packages."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Final, cast

import msgspec

from mcubridge.protocol.structures import RuntimeConfig

logger = logging.getLogger("mcubridge.common")

_UCI_PACKAGE: Final[str] = "mcubridge"
_UCI_SECTION: Final[str] = "general"


def get_uci_config() -> dict[str, Any]:
    """Read MCU Bridge configuration directly from OpenWrt uci system."""
    is_openwrt = Path("/etc/openwrt_release").exists() or Path("/etc/openwrt_version").exists()
    try:
        import uci
        with uci.Uci() as cursor:
            try:
                # [SIL-2] Ensure atomic read of entire package
                # Use Any cast to avoid unknown member issues from manual stub
                pkg_data = cast(Any, cursor).get_all(_UCI_PACKAGE)
                section = cast(dict[str, Any], pkg_data).get(_UCI_SECTION)
            except Exception as e:
                if is_openwrt:
                    logger.critical("UCI failure reading %s.%s: %s", _UCI_PACKAGE, _UCI_SECTION, e)
                    raise RuntimeError(f"Critical UCI failure: {e}") from e
                return get_default_config()

            if not section:
                if is_openwrt:
                    raise RuntimeError(f"UCI section {_UCI_PACKAGE}.{_UCI_SECTION} missing!")
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
        pass
    return get_default_config()


def get_default_config() -> dict[str, Any]:
    """Return the default configuration as a dictionary."""
    # msgspec.to_builtins ensures defaults are always in sync with the schema without manual iteration.
    try:
        return cast(dict[str, Any], msgspec.to_builtins(RuntimeConfig()))
    except Exception:
        return {"debug": False}


__all__: Final[tuple[str, ...]] = (
    "get_default_config",
    "get_uci_config",
)

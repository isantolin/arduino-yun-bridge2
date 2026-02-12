"""Utility helpers shared across MCU Bridge packages."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Final, Any, cast

# [SIL-2] STRICT DEPENDENCY: On OpenWrt, uci is a mandatory system package.
import uci

from mcubridge.util import parse_bool, normalise_allowed_commands
from mcubridge.mqtt import build_mqtt_connect_properties, build_mqtt_properties
from mcubridge.protocol.encoding import encode_status_reason
from mcubridge.util import log_hexdump

logger = logging.getLogger(__name__)

_UCI_PACKAGE: Final[str] = "mcubridge"
_UCI_SECTION: Final[str] = "general"


def get_uci_config() -> dict[str, Any]:
    """Read MCU Bridge configuration directly from OpenWrt uci system."""
    is_openwrt = Path("/etc/openwrt_release").exists() or Path("/etc/openwrt_version").exists()
    try:
        with uci.Uci() as cursor:
            try:
                section = cursor.get_all(_UCI_PACKAGE, _UCI_SECTION)
            except uci.UciException as e:
                if is_openwrt:
                    logger.critical("UCI failure reading %s.%s: %s", _UCI_PACKAGE, _UCI_SECTION, e)
                    raise RuntimeError(f"Critical UCI failure: {e}") from e
                return get_default_config()

            if not section:
                if is_openwrt:
                    raise RuntimeError(f"UCI section {_UCI_PACKAGE}.{_UCI_SECTION} missing!")
                return get_default_config()

            clean_config: dict[str, Any] = get_default_config()
            for k, v in section.items():
                if k.startswith((".", "_")):
                    continue
                if isinstance(v, (list, tuple)):
                    clean_config[k] = " ".join(str(item) for item in cast(Iterable[Any], v))
                else:
                    clean_config[k] = v
            return clean_config
    except (OSError, ValueError) as e:
        if is_openwrt:
            raise RuntimeError(f"Critical UCI failure: {e}") from e
        return get_default_config()


def get_default_config() -> dict[str, Any]:
    import msgspec.structs as _structs
    from mcubridge.config.settings import RuntimeConfig
    _SKIP_FIELDS = frozenset({"allowed_policy", "topic_authorization"})
    defaults: dict[str, Any] = {}
    for fi in _structs.fields(RuntimeConfig):
        if fi.name in _SKIP_FIELDS:
            continue
        defaults[fi.name] = fi.default
    defaults["debug"] = False
    return defaults


__all__: Final[tuple[str, ...]] = (
    "normalise_allowed_commands",
    "parse_bool",
    "encode_status_reason",
    "get_default_config",
    "get_uci_config",
    "build_mqtt_connect_properties",
    "build_mqtt_properties",
    "log_hexdump",
)

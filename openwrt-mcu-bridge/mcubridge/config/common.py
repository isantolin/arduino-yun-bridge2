"""Utility helpers shared across MCU Bridge packages."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path
from typing import (
    Final,
    Any,
    cast,
)

# [SIL-2] STRICT DEPENDENCY: On OpenWrt, 'uci' is a mandatory system package.
# We do not use try-import here to enforce fail-fast behavior in production.
import uci

from .const import (
    ALLOWED_COMMAND_WILDCARD,
)

# ------------------------------------------------------------------
# Backward-compatible re-exports.
# Canonical homes: mcubridge.mqtt, mcubridge.protocol.encoding,
#                  mcubridge.util
# ------------------------------------------------------------------
from mcubridge.mqtt import build_mqtt_connect_properties, build_mqtt_properties  # noqa: F401
from mcubridge.protocol.encoding import encode_status_reason  # noqa: F401
from mcubridge.util import log_hexdump  # noqa: F401


logger = logging.getLogger(__name__)

_TRUE_STRINGS: Final[frozenset[str]] = frozenset({"1", "yes", "on", "true", "enable", "enabled"})
_UCI_PACKAGE: Final[str] = "mcubridge"
_UCI_SECTION: Final[str] = "general"


def parse_bool(value: object) -> bool:
    """Parse a boolean value safely from various types."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if value is None:
        return False
    s = str(value).lower().strip()
    return s in _TRUE_STRINGS


def normalise_allowed_commands(commands: Iterable[str]) -> tuple[str, ...]:
    """Return a deduplicated, lower-cased allow-list preserving wildcards."""
    seen: set[str] = set()
    normalised: list[str] = []
    for item in commands:
        candidate = item.strip()
        if not candidate:
            continue
        lowered = candidate.lower()
        if lowered == ALLOWED_COMMAND_WILDCARD:
            return (ALLOWED_COMMAND_WILDCARD,)
        if lowered in seen:
            continue
        seen.add(lowered)
        normalised.append(lowered)
    return tuple(normalised)


def get_uci_config() -> dict[str, Any]:
    """Read MCU Bridge configuration directly from OpenWrt's UCI system."""

    is_openwrt = Path("/etc/openwrt_release").exists() or Path("/etc/openwrt_version").exists()

    try:
        with uci.Uci() as cursor:
            # OpenWrt's python3-uci returns a native dict in modern versions.
            try:
                section = cursor.get_all(_UCI_PACKAGE, _UCI_SECTION)
            except uci.UciException as e:
                if is_openwrt:
                    logger.critical("UCI failure reading %s.%s: %s", _UCI_PACKAGE, _UCI_SECTION, e)
                    raise RuntimeError(f"Critical UCI failure: {e}") from e
                logger.warning("UCI section '%s.%s' read failed: %s; using defaults.", _UCI_PACKAGE, _UCI_SECTION, e)
                return get_default_config()

            if not section:
                if is_openwrt:
                    raise RuntimeError(
                        f"UCI section '{_UCI_PACKAGE}.{_UCI_SECTION}' missing! "
                        "Re-install package to restore defaults."
                    )
                logger.warning("UCI section '%s.%s' not found; using defaults.", _UCI_PACKAGE, _UCI_SECTION)
                return get_default_config()

            # Clean internal UCI metadata
            clean_config: dict[str, Any] = get_default_config()
            for k, v in section.items():
                if k.startswith((".", "_")):
                    continue
                if isinstance(v, (list, tuple)):
                    clean_config[k] = " ".join(str(item) for item in cast(Iterable[Any], v))
                else:
                    # msgspec handles type conversion better if we pass raw strings where possible,
                    # but UCI returns strings anyway.
                    clean_config[k] = v

            return clean_config

    except (OSError, ValueError) as e:
        if is_openwrt:
            logger.critical("Failed to load UCI configuration on OpenWrt: %s", e)
            raise RuntimeError(f"Critical UCI failure: {e}") from e

        logger.error("Failed to load UCI configuration: %s. Using defaults.", e)
        return get_default_config()


def get_default_config() -> dict[str, Any]:
    """Provide default MCU Bridge configuration values.

    Derived programmatically from ``RuntimeConfig`` field defaults via
    ``msgspec.structs.fields()`` to ensure a **single source of truth**.

    Extra keys that exist only in the UCI schema (``mqtt_allow_*``,
    ``debug``) are appended here because they are consumed *before*
    ``msgspec.convert()`` builds the struct.
    """
    import msgspec.structs as _structs

    # Lazy import to break circular dependency (settings → common → settings).
    from mcubridge.config.settings import RuntimeConfig

    # Fields that are computed at __post_init__ time — not serialisable.
    _SKIP_FIELDS = frozenset({"allowed_policy", "topic_authorization"})

    defaults: dict[str, Any] = {}
    for fi in _structs.fields(RuntimeConfig):
        if fi.name in _SKIP_FIELDS:
            continue
        defaults[fi.name] = fi.default

    # --- Keys consumed by pre-processing in load_runtime_config() ---
    # 'debug' maps to 'debug_logging'; UCI exposes 'debug' only.
    defaults["debug"] = False
    # Topic authorisation flags (consumed by TopicAuthorization construction).
    defaults["mqtt_allow_file_read"] = True
    defaults["mqtt_allow_file_write"] = True
    defaults["mqtt_allow_file_remove"] = True
    defaults["mqtt_allow_datastore_get"] = True
    defaults["mqtt_allow_datastore_put"] = True
    defaults["mqtt_allow_mailbox_read"] = True
    defaults["mqtt_allow_mailbox_write"] = True
    defaults["mqtt_allow_shell_run"] = True
    defaults["mqtt_allow_shell_run_async"] = True
    defaults["mqtt_allow_shell_poll"] = True
    defaults["mqtt_allow_shell_kill"] = True
    defaults["mqtt_allow_console_input"] = True
    defaults["mqtt_allow_digital_write"] = True
    defaults["mqtt_allow_digital_read"] = True
    defaults["mqtt_allow_digital_mode"] = True
    defaults["mqtt_allow_analog_write"] = True
    defaults["mqtt_allow_analog_read"] = True
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

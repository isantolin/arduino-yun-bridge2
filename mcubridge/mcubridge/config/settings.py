"""Settings loader for the MCU Bridge daemon.

Configuration is loaded from OpenWrt UCI (package `mcubridge`, section
`general`) with sane defaults for non-OpenWrt environments.

Runtime configuration is intentionally **UCI-only**: environment variables are
not used.
"""

from __future__ import annotations

import structlog
from typing import Any
from pathlib import Path

import msgspec

from ..config.common import (
    get_default_config,
    get_uci_config,
)
from mcubridge.protocol.structures import RuntimeConfig

logger = structlog.get_logger(__name__)


def _dec_hook(type_type: Any, obj: Any) -> Any:
    """[SIL-2] msgspec dec_hook for zero-wrapper coercion."""
    types = getattr(type_type, "__args__", (type_type,))
    if bytes in types and isinstance(obj, str):
        return obj.strip().encode("utf-8")
    if tuple in types or getattr(type_type, "__origin__", type_type) is tuple:
        if isinstance(obj, str):
            return tuple(obj.split())
    if str in types and isinstance(obj, str):
        val = obj.strip()
        return str(Path(val).expanduser().resolve()) if ("~" in val or "/" in val) and "\n" not in val else val or None
    raise TypeError(f"Cannot coerce {obj!r} to {type_type}")

def _load_raw_config() -> tuple[dict[str, Any], str]:
    """Load configuration from defaults and UCI (SIL 2).

    Precedence (highest first): UCI -> Defaults.
    Fallback: Defaults are used if UCI is missing, locked, or corrupt.
    """
    source = "defaults"
    config = get_default_config()

    try:
        # [SIL-2] Resilient load: fail-safe to defaults on any system error
        uci_values = get_uci_config()
        if uci_values:
            config.update(uci_values)
            source = "uci"
    except (OSError, ValueError, RuntimeError, ImportError) as err:
        # [SIL-2] UCI is optional for system survival. Log error and continue with defaults.
        logger.warning(
            "UCI configuration unavailable or locked (using safe defaults): %s", err
        )

    return config, source


# [SIL-2] Module-level config source for observability — mutable list avoids `global`
_config_source: list[str] = ["uci"]


def get_config_source() -> str:
    """Return the source of the last loaded configuration ('uci' or 'defaults')."""
    return _config_source[0]


def load_runtime_config(overrides: dict[str, Any] | None = None) -> RuntimeConfig:
    """Load, normalize, and validate the daemon configuration (SIL 2).

    This is the primary entry point for configuration loading. It ensures that
    the returned RuntimeConfig is valid and follows all flash protection rules.

    Args:
        overrides: Optional dictionary of configuration overrides (e.g. from CLI).
    """
    raw_values, source = _load_raw_config()
    if overrides:
        raw_values.update(overrides)
        source = "cli"
    _config_source[0] = source

    if isinstance(raw_values.get("allowed_commands"), str):
        raw_values["allowed_commands"] = raw_values["allowed_commands"].split()

    if isinstance(raw_values.get("serial_shared_secret"), str):
        raw_values["serial_shared_secret"] = raw_values["serial_shared_secret"].strip().encode("utf-8")

    try:
        # [SIL-2] Holistic Validation via msgspec.Struct.
        return msgspec.convert(raw_values, RuntimeConfig, strict=False, dec_hook=_dec_hook)
    except (msgspec.ValidationError, ValueError) as e:
        if source == "uci":
            # [SIL-2] Deterministic Failure: If UCI is present but invalid, abort.
            # This prevents running with a partially invalid security config.
            logger.critical("FATAL: UCI configuration is invalid: %s", e)
            raise RuntimeError(f"Invalid system configuration: {e}") from e

        # During tests or fallback, let the error propagate if it's a structural/logic error
        logger.critical("Configuration validation failed: %s", e)
        raise

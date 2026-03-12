"""Settings loader for the MCU Bridge daemon.

Configuration is loaded from OpenWrt UCI (package `mcubridge`, section
`general`) with sane defaults for non-OpenWrt environments.

Runtime configuration is intentionally **UCI-only**: environment variables are
not used.
"""

from __future__ import annotations

import logging
from typing import Any

import msgspec

from ..config.common import (
    get_default_config,
    get_uci_config,
)
from mcubridge.protocol.structures import RuntimeConfig

logger = logging.getLogger(__name__)


def _load_raw_config() -> tuple[dict[str, Any], str]:
    """Load configuration from UCI with robust error handling (SIL 2).

    Returns:
        Tuple of (config_dict, source) where source is 'uci' or 'defaults'.
    """
    try:
        uci_values = get_uci_config()
        if uci_values:
            return uci_values, "uci"
    except (OSError, ValueError) as err:
        # [SIL-2] Catch specific errors to differentiate operational issues from bugs.
        # Fallback to defaults is acceptable here to ensure Fail-Operational behavior.
        logger.error("Failed to load UCI configuration (Operational Error): %s", err)

    logger.warning("Using default configuration (UCI unavailable)")
    return get_default_config(), "defaults"


# Module-level variable to track config source for observability
class _ConfigState:
    source: str = "uci"


def get_config_source() -> str:
    """Return the source of the last loaded configuration ('uci' or 'defaults')."""
    return _ConfigState.source


def load_runtime_config() -> RuntimeConfig:
    """Load, normalize, and validate the daemon configuration (SIL 2).

    This is the primary entry point for configuration loading. It ensures that
    the returned RuntimeConfig is valid and follows all flash protection rules.
    """
    raw_values, source = _load_raw_config()
    _ConfigState.source = source

    try:
        # [SIL-2] Holistic Validation via msgspec.Struct.
        # This performs type checking, range validation, and normalization in one pass.
        return msgspec.convert(raw_values, RuntimeConfig, strict=True)
    except (msgspec.ValidationError, ValueError) as e:
        if source == "uci":
            # [SIL-2] Deterministic Failure: If UCI is present but invalid, abort.
            # This prevents running with a partially invalid security config.
            logger.critical("FATAL: UCI configuration is invalid: %s", e)
            raise RuntimeError(f"Invalid system configuration: {e}") from e
        logger.critical("Configuration validation failed: %s", e)
        logger.warning("Falling back to safe defaults due to validation error.")
        return msgspec.convert(get_default_config(), RuntimeConfig, strict=False)

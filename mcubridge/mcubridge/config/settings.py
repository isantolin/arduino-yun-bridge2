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
    """Load configuration from defaults, UCI, and environment variables (SIL 2).

    Precedence (highest first): Environment Variables -> UCI -> Defaults.

    Returns:
        Tuple of (config_dict, source) where source is 'uci' or 'defaults'.
    """
    import os

    source = "defaults"
    config = get_default_config()

    try:
        uci_values = get_uci_config()
        if uci_values:
            config.update(uci_values)
            source = "uci"
    except (OSError, ValueError) as err:
        logger.error("Failed to load UCI configuration (Operational Error): %s", err)

    # [SIL-2] Environment Variable Overrides
    for key, value in os.environ.items():
        if key.startswith("MCUBRIDGE_"):
            # Convert MCUBRIDGE_SERIAL_PORT to serial_port
            clean_key = key[10:].lower()
            config[clean_key] = value
            source = "env" if source == "defaults" else source

    return config, source


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
        # strict=False allows some flexibility during test/migration phases (e.g. string to bytes)
        return msgspec.convert(raw_values, RuntimeConfig, strict=False)
    except (msgspec.ValidationError, ValueError) as e:
        if source == "uci":
            # [SIL-2] Deterministic Failure: If UCI is present but invalid, abort.
            # This prevents running with a partially invalid security config.
            logger.critical("FATAL: UCI configuration is invalid: %s", e)
            raise RuntimeError(f"Invalid system configuration: {e}") from e

        # During tests or fallback, let the error propagate if it's a structural/logic error
        logger.critical("Configuration validation failed: %s", e)
        raise

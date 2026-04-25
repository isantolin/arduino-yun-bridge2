"""Settings loader for the MCU Bridge daemon.

Configuration is loaded from OpenWrt UCI (package `mcubridge`, section
`general`) with sane defaults for non-OpenWrt environments.

Runtime configuration is intentionally **UCI-only**: environment variables are
not used.
"""

from __future__ import annotations

import structlog
from typing import Any

import msgspec

from ..config.common import (
    get_default_config,
    get_uci_config,
)
from mcubridge.protocol.structures import RuntimeConfig

logger = structlog.get_logger(__name__)


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


# Module-level variable to track config source for observability
class _ConfigState:
    source: str = "uci"


def get_config_source() -> str:
    """Return the source of the last loaded configuration ('uci' or 'defaults')."""
    return _ConfigState.source


def load_runtime_config(overrides: dict[str, Any] | None = None) -> RuntimeConfig:
    """Load, normalize, and validate the daemon configuration (SIL 2).

    This is the primary entry point for configuration loading. It ensures that
    the returned RuntimeConfig is valid and follows all flash protection rules.

    Normalization (Path resolution, secret coercion, etc.) is handled automatically 
    by the RuntimeConfig.__post_init__ method.

    Args:
        overrides: Optional dictionary of configuration overrides (e.g. from CLI).
    """
    raw_values, source = _load_raw_config()
    if overrides:
        raw_values.update(overrides)
        source = "cli"
    _ConfigState.source = source

    try:
        # [SIL-2] Holistic Validation via msgspec.Struct.
        # Normalization occurs inside RuntimeConfig.__post_init__
        return msgspec.convert(raw_values, RuntimeConfig, strict=True)
    except (msgspec.ValidationError, ValueError) as e:
        if source == "uci":
            # [SIL-2] Deterministic Failure: If UCI is present but invalid, abort.
            logger.critical("FATAL: UCI configuration is invalid: %s", e)
            raise RuntimeError(f"Invalid system configuration: {e}") from e

        # During tests or fallback, let the error propagate
        logger.critical("Configuration validation failed: %s", e)
        raise

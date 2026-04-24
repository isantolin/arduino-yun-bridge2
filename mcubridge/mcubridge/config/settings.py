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


def _normalize_raw_config(values: dict[str, Any]) -> dict[str, Any]:
    """[SIL-2] Normalize raw configuration values before schema validation."""
    normalized = values.copy()

    # 1. String Stripping & Optional normalization
    str_keys = (
        "serial_port",
        "mqtt_host",
        "mqtt_user",
        "mqtt_pass",
        "mqtt_cafile",
        "mqtt_certfile",
        "mqtt_keyfile",
    )
    for key in str_keys:
        if key in normalized and isinstance(normalized[key], str):
            normalized[key] = normalized[key].strip() or None

    # 2. Path Resolution (Atomic expansion)
    path_keys = ("file_system_root", "mqtt_spool_dir")
    for key in path_keys:
        if key in normalized and isinstance(normalized[key], str):
            normalized[key] = str(Path(normalized[key]).expanduser().resolve())

    # 3. Secret Coercion
    if "serial_shared_secret" in normalized:
        secret = normalized["serial_shared_secret"]
        if isinstance(secret, str):
            normalized["serial_shared_secret"] = secret.strip().encode("utf-8")

    # 4. MQTT Topic Normalization
    if "mqtt_topic" in normalized:
        raw_topic = str(normalized["mqtt_topic"]).strip()
        segments = tuple(filter(None, raw_topic.split("/")))
        if segments:
            normalized["mqtt_topic"] = "/".join(segments)

    # 5. Allowed Commands Normalization (Atomic coercion)
    if "allowed_commands" in normalized:
        cmds = normalized["allowed_commands"]
        if isinstance(cmds, str):
            normalized["allowed_commands"] = tuple(cmds.split())
        elif cmds is None:
            normalized["allowed_commands"] = ()

    return normalized


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

    Args:
        overrides: Optional dictionary of configuration overrides (e.g. from CLI).
    """
    raw_values, source = _load_raw_config()
    if overrides:
        raw_values.update(overrides)
        source = "cli"
    _ConfigState.source = source

    # [SIL-2] Pre-conversion Normalization
    normalized_values = _normalize_raw_config(raw_values)

    try:
        # [SIL-2] Holistic Validation via msgspec.Struct.
        # strict=True ensures configuration integrity.
        return msgspec.convert(normalized_values, RuntimeConfig, strict=True)
    except (msgspec.ValidationError, ValueError) as e:
        if source == "uci":
            # [SIL-2] Deterministic Failure: If UCI is present but invalid, abort.
            # This prevents running with a partially invalid security config.
            logger.critical("FATAL: UCI configuration is invalid: %s", e)
            raise RuntimeError(f"Invalid system configuration: {e}") from e

        # During tests or fallback, let the error propagate if it's a structural/logic error
        logger.critical("Configuration validation failed: %s", e)
        raise

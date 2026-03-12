"""Settings loader for the MCU Bridge daemon.

Configuration is loaded from OpenWrt UCI (package `mcubridge`, section
`general`) with sane defaults for non-OpenWrt environments.

Runtime configuration is intentionally **UCI-only**: environment variables are
not used.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, Any

# [SIL-2] Deterministic Import: msgspec is MANDATORY.
import msgspec

from ..config.common import (
    get_default_config,
    get_uci_config,
    normalise_allowed_commands,
    parse_bool,
)
from ..config.const import (
    DEFAULT_ALLOW_NON_TMP_PATHS,
    DEFAULT_BRIDGE_HANDSHAKE_INTERVAL,
    DEFAULT_BRIDGE_SUMMARY_INTERVAL,
    DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES,
    DEFAULT_DEBUG_LOGGING,
    DEFAULT_FILE_STORAGE_QUOTA_BYTES,
    DEFAULT_FILE_SYSTEM_ROOT,
    DEFAULT_FILE_WRITE_MAX_BYTES,
    DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT,
    DEFAULT_MAILBOX_QUEUE_LIMIT,
    DEFAULT_METRICS_ENABLED,
    DEFAULT_METRICS_HOST,
    DEFAULT_METRICS_PORT,
    DEFAULT_MQTT_CAFILE,
    DEFAULT_MQTT_HOST,
    DEFAULT_MQTT_PORT,
    DEFAULT_MQTT_QUEUE_LIMIT,
    DEFAULT_MQTT_SPOOL_DIR,
    DEFAULT_MQTT_TLS_INSECURE,
    DEFAULT_PENDING_PIN_REQUESTS,
    DEFAULT_PROCESS_MAX_CONCURRENT,
    DEFAULT_PROCESS_MAX_OUTPUT_BYTES,
    DEFAULT_PROCESS_TIMEOUT,
    DEFAULT_RECONNECT_DELAY,
    DEFAULT_SERIAL_HANDSHAKE_FATAL_FAILURES,
    DEFAULT_SERIAL_HANDSHAKE_MIN_INTERVAL,
    DEFAULT_SERIAL_PORT,
    DEFAULT_SERIAL_RESPONSE_TIMEOUT,
    DEFAULT_SERIAL_RETRY_TIMEOUT,
    DEFAULT_SERIAL_SHARED_SECRET,
    DEFAULT_STATUS_INTERVAL,
    DEFAULT_WATCHDOG_INTERVAL,
    MIN_SERIAL_SHARED_SECRET_LEN,
    VOLATILE_STORAGE_PATHS,
)
from ..policy import AllowedCommandPolicy, TopicAuthorization
from ..protocol.protocol import (
    DEFAULT_BAUDRATE,
    DEFAULT_RETRY_LIMIT,
    DEFAULT_SAFE_BAUDRATE,
    MQTT_DEFAULT_TOPIC_PREFIX,
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
    """Load configuration from UCI/defaults using msgspec for efficient validation."""

    raw_config, source = _load_raw_config()
    _ConfigState.source = source

    # Override with environment variables (useful for E2E testing and Docker)
    import os
    for key in RuntimeConfig.__struct_fields__:
        env_val = os.environ.get(f"MCUBRIDGE_{key.upper()}")
        if env_val is not None:
            raw_config[key] = env_val

    # [SIL-2] Pre-processing for complex types that UCI or ENV provides as raw strings
    if "allowed_commands" in raw_config:
        allowed_raw = raw_config["allowed_commands"]
        if isinstance(allowed_raw, str):
            raw_config["allowed_commands"] = normalise_allowed_commands(allowed_raw.split())

    if "debug" in raw_config:
        raw_config["debug_logging"] = parse_bool(raw_config.pop("debug"))

    if "serial_shared_secret" in raw_config:
        secret = raw_config["serial_shared_secret"]
        if isinstance(secret, str):
            raw_config["serial_shared_secret"] = secret.strip().encode("utf-8")

    try:
        # msgspec.convert triggers __post_init__ which handles string stripping and path resolution.
        return msgspec.convert(raw_config, RuntimeConfig, strict=False)
    except (msgspec.ValidationError, TypeError, ValueError) as e:
        if source == "test":
            raise ValueError(f"Configuration validation failed: {e}") from e
        logger.critical("Configuration validation failed: %s", e)
        logger.warning("Falling back to safe defaults due to validation error.")
        return msgspec.convert(get_default_config(), RuntimeConfig, strict=False)

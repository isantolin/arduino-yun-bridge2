"""Settings loader for the MCU Bridge daemon.

Configuration is loaded from OpenWrt UCI (package `mcubridge`, section
`general`) with sane defaults for non-OpenWrt environments.

Runtime configuration is intentionally **UCI-only**: environment variables are
not used as overrides.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from typing import Any

from ..common import (
    get_default_config,
    get_uci_config,
    normalise_allowed_commands,
    parse_bool,
    parse_float,
    parse_int,
)
from ..const import (
    DEFAULT_MQTT_CAFILE,
    DEFAULT_MQTT_HOST,
    DEFAULT_MQTT_PORT,
    DEFAULT_MQTT_SPOOL_DIR,
    DEFAULT_SERIAL_PORT,
)
from ..rpc import protocol
from ..rpc.protocol import DEFAULT_BAUDRATE, DEFAULT_SAFE_BAUDRATE
from .model import RuntimeConfig
from .schema import RuntimeConfigSchema, ValidationError

logger = logging.getLogger(__name__)


def _load_raw_config() -> dict[str, str]:
    """Load configuration from UCI with robust error handling (SIL 2)."""
    try:
        uci_values = get_uci_config()
        if uci_values:
            return uci_values
    except (OSError, ValueError) as err:
        logger.error("Failed to load UCI configuration (Operational Error): %s", err)

    return get_default_config()


def _optional_path(path: str | None) -> str | None:
    if not path:
        return None
    candidate = path.strip()
    return candidate or None


def configure_logging(config: RuntimeConfig) -> None:
    """Configure logging for OpenWrt environment (Syslog)."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if config.debug_logging else logging.INFO)

    for h in root.handlers[:]:
        root.removeHandler(h)

    formatter = logging.Formatter(
        "%(name)s: %(levelname)s %(message)s"
    )

    handlers: list[logging.Handler] = []

    if os.path.exists("/dev/log"):
        try:
            syslog_handler = logging.handlers.SysLogHandler(
                address="/dev/log",
                facility=logging.handlers.SysLogHandler.LOG_DAEMON
            )
            syslog_handler.setFormatter(formatter)
            handlers.append(syslog_handler)
        except (OSError, ConnectionError) as e:
            sys.stderr.write(f"Failed to connect to syslog: {e}\n")

    if not handlers or sys.stdout.isatty():
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        handlers.append(stream_handler)

    for handler in handlers:
        root.addHandler(handler)


def load_runtime_config() -> RuntimeConfig:
    """Load configuration from UCI/defaults using Marshmallow schema."""

    raw = _load_raw_config()
    
    # Pre-process raw dictionary to match Schema expected types/structure
    # Schema handles basic type conversion (e.g. "1" -> 1 for Int fields)
    # but we need to map UCI keys to Schema keys where they differ slightly
    # or handle complex structures.

    data: dict[str, Any] = {}
    
    # Direct mappings (UCI key == Schema key)
    direct_keys = [
        "serial_port", "serial_baud", "serial_safe_baud", 
        "mqtt_host", "mqtt_port", "mqtt_user", "mqtt_pass",
        "mqtt_tls", "mqtt_tls_insecure", "mqtt_cafile", "mqtt_certfile", "mqtt_keyfile",
        "mqtt_topic", "file_system_root", "process_timeout",
        "file_write_max_bytes", "file_storage_quota_bytes", "mqtt_queue_limit",
        "reconnect_delay", "status_interval", "debug",
        "console_queue_limit_bytes", "mailbox_queue_limit", "mailbox_queue_bytes_limit",
        "pending_pin_request_limit", "serial_retry_timeout", "serial_response_timeout",
        "serial_retry_attempts", "serial_handshake_min_interval", "serial_handshake_fatal_failures",
        "watchdog_enabled", "watchdog_interval", "serial_shared_secret",
        "mqtt_spool_dir", "process_max_output_bytes", "process_max_concurrent",
        "metrics_enabled", "metrics_host", "metrics_port",
        "bridge_summary_interval", "bridge_handshake_interval", "allow_non_tmp_paths"
    ]
    
    for key in direct_keys:
        if key in raw:
            # Handle empty strings as None for optional fields where appropriate
            val = raw[key]
            if isinstance(val, str):
                val = val.strip()

            if val == "" and key in ("mqtt_user", "mqtt_pass", "mqtt_cafile", "mqtt_certfile", "mqtt_keyfile"):
                val = None
            
            # Map 'debug' to 'debug_logging'
            target_key = "debug_logging" if key == "debug" else key
            data[target_key] = val

    # Handle defaults logic for critical paths that might be missing in raw
    if "mqtt_topic" not in data:
        data["mqtt_topic"] = protocol.MQTT_DEFAULT_TOPIC_PREFIX
        
    if "serial_port" not in data:
        data["serial_port"] = DEFAULT_SERIAL_PORT

    # Handle allowed_commands list
    if "allowed_commands" in raw:
        # Normalize: split string by spaces
        cmds = normalise_allowed_commands(raw["allowed_commands"].split())
        data["allowed_commands"] = list(cmds)

    # Handle Topic Authorization (Nested)
    topic_auth = {}
    auth_keys = [
        ("mqtt_allow_file_read", "file_read"),
        ("mqtt_allow_file_write", "file_write"),
        ("mqtt_allow_file_remove", "file_remove"),
        ("mqtt_allow_datastore_get", "datastore_get"),
        ("mqtt_allow_datastore_put", "datastore_put"),
        ("mqtt_allow_mailbox_read", "mailbox_read"),
        ("mqtt_allow_mailbox_write", "mailbox_write"),
        ("mqtt_allow_shell_run", "shell_run"),
        ("mqtt_allow_shell_run_async", "shell_run_async"),
        ("mqtt_allow_shell_poll", "shell_poll"),
        ("mqtt_allow_shell_kill", "shell_kill"),
        ("mqtt_allow_console_input", "console_input"),
        ("mqtt_allow_digital_write", "digital_write"),
        ("mqtt_allow_digital_read", "digital_read"),
        ("mqtt_allow_digital_mode", "digital_mode"),
        ("mqtt_allow_analog_write", "analog_write"),
        ("mqtt_allow_analog_read", "analog_read"),
    ]
    
    for uci_key, schema_key in auth_keys:
        if uci_key in raw:
            # UCI returns "0"/"1", "true"/"false". parse_bool handles this.
            # Schema expects boolean.
            topic_auth[schema_key] = parse_bool(raw[uci_key])
            
    if topic_auth:
        data["topic_authorization"] = topic_auth

    # TLS Default Logic (Legacy behavior preservation)
    # If mqtt_tls is True (default if missing in Schema, but here we construct input),
    # and cafile missing, set default.
    # Actually Schema 'load_default' handles defaults.
    # But settings.py logic had: if mqtt_tls and not cafile -> default_cafile.
    # We replicate this *before* loading if possible, or let Schema handle?
    # Schema load_defaults are static. We need dynamic default based on mqtt_tls.
    # It's cleaner to handle this prep.
    
    _mqtt_tls = parse_bool(data.get("mqtt_tls", "1")) # Default true in legacy loader
    _mqtt_cafile = data.get("mqtt_cafile")
    
    if _mqtt_tls and not _mqtt_cafile:
        data["mqtt_cafile"] = DEFAULT_MQTT_CAFILE

    try:
        schema = RuntimeConfigSchema()
        config = schema.load(data)
        return config
    except ValidationError as err:
        logger.critical("Configuration validation failed: %s", err.messages)
        # Re-raise as ValueError to maintain compatibility with existing error handling
        # or allow it to bubble up. Existing code expects ValueErrors for config issues.
        # We flatten the validation errors into a string.
        flat_errors = "; ".join(f"{k}: {v}" for k, v in err.messages.items())
        raise ValueError(f"Invalid configuration: {flat_errors}") from err

__all__ = [
    "RuntimeConfig",
    "load_runtime_config",
    "configure_logging",
]
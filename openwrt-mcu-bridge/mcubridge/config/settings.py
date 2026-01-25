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
from typing import Any, cast

from ..common import (
    get_default_config,
    get_uci_config,
    normalise_allowed_commands,
    parse_bool,
)
from ..const import (
    DEFAULT_MQTT_CAFILE,
    DEFAULT_SERIAL_PORT,
)
from ..rpc import protocol
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
            val = raw[key]
            # Strip whitespace safely
            if val is not None:
                val = str(val).strip()

            if val == "" and key in ("mqtt_user", "mqtt_pass", "mqtt_cafile", "mqtt_certfile", "mqtt_keyfile"):
                val = None

            target_key = "debug_logging" if key == "debug" else key
            data[target_key] = val

    if "mqtt_topic" not in data:
        data["mqtt_topic"] = protocol.MQTT_DEFAULT_TOPIC_PREFIX

    if "serial_port" not in data:
        data["serial_port"] = DEFAULT_SERIAL_PORT

    if "allowed_commands" in raw:
        cmds = normalise_allowed_commands(raw["allowed_commands"].split())
        data["allowed_commands"] = list(cmds)

    topic_auth: dict[str, bool] = {}
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
            topic_auth[schema_key] = parse_bool(raw[uci_key])

    if topic_auth:
        data["topic_authorization"] = topic_auth

    _mqtt_tls = parse_bool(data.get("mqtt_tls", "1"))
    _mqtt_cafile = data.get("mqtt_cafile")

    if _mqtt_tls and not _mqtt_cafile:
        data["mqtt_cafile"] = DEFAULT_MQTT_CAFILE

    try:
        schema = RuntimeConfigSchema()
        config = cast(RuntimeConfig, schema.load(data))
        return config
    except ValidationError as err:
        logger.critical("Configuration validation failed: %s", err.messages) # type: ignore
        # Format messages for the ValueError
        if isinstance(err.messages, dict): # type: ignore
            flat_errors = "; ".join(f"{k}: {v}" for k, v in err.messages.items()) # type: ignore
        else:
            flat_errors = str(err.messages) # type: ignore
        raise ValueError(f"Invalid configuration: {flat_errors}") from err


__all__ = [
    "RuntimeConfig",
    "load_runtime_config",
    "configure_logging",
]

"""Utility helpers shared across Yun Bridge packages."""

from __future__ import annotations

import logging
import os  # Added for environment variable access
from collections.abc import Iterable
from typing import (
    Final,
    TypeVar,
    TYPE_CHECKING,
    cast,
)

from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties
from yunbridge.rpc import protocol
from yunbridge.rpc.protocol import (
    DEFAULT_BAUDRATE as DEFAULT_SERIAL_BAUD,
    DEFAULT_RETRY_LIMIT as DEFAULT_SERIAL_RETRY_ATTEMPTS,
)

from .const import (
    ALLOWED_COMMAND_WILDCARD,
    DEFAULT_BRIDGE_HANDSHAKE_INTERVAL,
    DEFAULT_BRIDGE_SUMMARY_INTERVAL,
    DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES,
    DEFAULT_FILE_SYSTEM_ROOT,
    DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT,
    DEFAULT_MAILBOX_QUEUE_LIMIT,
    DEFAULT_METRICS_HOST,
    DEFAULT_METRICS_PORT,
    DEFAULT_MQTT_CAFILE,
    DEFAULT_MQTT_HOST,
    DEFAULT_MQTT_PORT,
    DEFAULT_MQTT_QUEUE_LIMIT,
    DEFAULT_MQTT_SPOOL_DIR,
    DEFAULT_MQTT_TOPIC,
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
    DEFAULT_STATUS_INTERVAL,
)


logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from yunbridge.mqtt.messages import QueuedPublish

T = TypeVar("T")

_TRUE_STRINGS: Final[frozenset[str]] = frozenset(
    {"1", "yes", "on", "true", "enable", "enabled"}
)
_UCI_PACKAGE: Final[str] = "yunbridge"
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


def parse_int(value: object, default: int) -> int:
    """Parse an integer value safely, handling floats and strings."""
    try:
        return int(float(value))  # type: ignore
    except (ValueError, TypeError):
        return default


def parse_float(value: object, default: float) -> float:
    """Parse a float value safely."""
    try:
        return float(value)  # type: ignore
    except (ValueError, TypeError):
        return default


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


def encode_status_reason(reason: str | None) -> bytes:
    """Return a UTF-8 encoded payload trimming to MAX frame limits."""
    if not reason:
        return b""
    payload = reason.encode("utf-8", errors="ignore")
    return payload[: protocol.MAX_PAYLOAD_SIZE]


def build_mqtt_properties(message: QueuedPublish) -> Properties | None:
    """Construct Paho MQTT v5 properties from a message object."""
    has_props = any(
        [
            message.content_type,
            message.payload_format_indicator is not None,
            message.message_expiry_interval is not None,
            message.response_topic,
            message.correlation_data is not None,
            message.user_properties,
        ]
    )

    if not has_props:
        return None

    props = Properties(PacketTypes.PUBLISH)

    if message.content_type is not None:
        props.ContentType = message.content_type

    if message.payload_format_indicator is not None:
        props.PayloadFormatIndicator = message.payload_format_indicator

    if message.message_expiry_interval is not None:
        props.MessageExpiryInterval = int(message.message_expiry_interval)

    if message.response_topic:
        props.ResponseTopic = message.response_topic

    if message.correlation_data is not None:
        props.CorrelationData = message.correlation_data

    if message.user_properties:
        props.UserProperty = list(message.user_properties)

    return props


def build_mqtt_connect_properties() -> Properties:
    """Return default CONNECT properties for aiomqtt/paho clients."""

    props = Properties(PacketTypes.CONNECT)
    props.SessionExpiryInterval = 0
    props.RequestResponseInformation = 1
    props.RequestProblemInformation = 1
    return props


def get_uci_config() -> dict[str, str]:
    """Read Yun Bridge configuration directly from OpenWrt's UCI system."""
    try:
        from uci import Uci  # type: ignore
    except ImportError:
        # In test environments (e.g. CI/Emulation), missing UCI is expected.
        if os.environ.get("YUNBRIDGE_NO_UCI_WARNING") == "1":
            logger.debug("UCI module not found (warning suppressed by env).")
        else:
            logger.warning(
                "UCI module not found (not running on OpenWrt?); using default configuration."
            )
        return get_default_config()

    try:
        with Uci() as cursor:
            # OpenWrt's python3-uci returns a native dict in modern versions.
            # We strictly expect the package 'yunbridge' and section 'general'.
            section = cursor.get_all(_UCI_PACKAGE, _UCI_SECTION)

            if not section:
                logger.warning("UCI section '%s.%s' not found; using defaults.", _UCI_PACKAGE, _UCI_SECTION)
                return get_default_config()

            # Clean internal UCI metadata (keys starting with dot/underscore)
            clean_config = get_default_config()
            for k, v in section.items():
                if k.startswith((".", "_")):
                    continue
                if isinstance(v, (list, tuple)):
                    # Explicitly cast v to Iterable to help type checker
                    items = cast(Iterable[object], v)
                    clean_config[k] = " ".join(str(item) for item in items)
                else:
                    clean_config[k] = str(v)

            return clean_config

    except Exception as exc:
        logger.warning("Failed to load UCI configuration: %s", exc)
        return get_default_config()


def get_default_config() -> dict[str, str]:
    """Provide default Yun Bridge configuration values."""
    return {
        "mqtt_host": DEFAULT_MQTT_HOST,
        "mqtt_port": str(DEFAULT_MQTT_PORT),
        "mqtt_tls": "1",
        "mqtt_cafile": DEFAULT_MQTT_CAFILE,
        "mqtt_certfile": "",
        "mqtt_keyfile": "",
        "mqtt_user": "",
        "mqtt_pass": "",
        "mqtt_topic": DEFAULT_MQTT_TOPIC,
        "mqtt_spool_dir": DEFAULT_MQTT_SPOOL_DIR,
        "mqtt_queue_limit": str(DEFAULT_MQTT_QUEUE_LIMIT),
        "serial_port": DEFAULT_SERIAL_PORT,
        "serial_baud": str(DEFAULT_SERIAL_BAUD),
        "serial_shared_secret": "",
        "serial_retry_timeout": str(DEFAULT_SERIAL_RETRY_TIMEOUT),
        "serial_response_timeout": str(DEFAULT_SERIAL_RESPONSE_TIMEOUT),
        "serial_retry_attempts": str(DEFAULT_SERIAL_RETRY_ATTEMPTS),
        "serial_handshake_min_interval": str(DEFAULT_SERIAL_HANDSHAKE_MIN_INTERVAL),
        "serial_handshake_fatal_failures": str(DEFAULT_SERIAL_HANDSHAKE_FATAL_FAILURES),
        "debug": "0",
        "allowed_commands": "",
        "file_system_root": DEFAULT_FILE_SYSTEM_ROOT,
        "process_timeout": str(DEFAULT_PROCESS_TIMEOUT),
        "process_max_output_bytes": str(DEFAULT_PROCESS_MAX_OUTPUT_BYTES),
        "process_max_concurrent": str(DEFAULT_PROCESS_MAX_CONCURRENT),
        "console_queue_limit_bytes": str(DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES),
        "mailbox_queue_limit": str(DEFAULT_MAILBOX_QUEUE_LIMIT),
        "mailbox_queue_bytes_limit": str(DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT),
        "pending_pin_request_limit": str(DEFAULT_PENDING_PIN_REQUESTS),
        "reconnect_delay": str(DEFAULT_RECONNECT_DELAY),
        "status_interval": str(DEFAULT_STATUS_INTERVAL),
        "bridge_summary_interval": str(DEFAULT_BRIDGE_SUMMARY_INTERVAL),
        "bridge_handshake_interval": str(DEFAULT_BRIDGE_HANDSHAKE_INTERVAL),
        "mqtt_allow_file_read": "1",
        "mqtt_allow_file_write": "1",
        "mqtt_allow_file_remove": "1",
        "mqtt_allow_datastore_get": "1",
        "mqtt_allow_datastore_put": "1",
        "mqtt_allow_mailbox_read": "1",
        "mqtt_allow_mailbox_write": "1",
        "mqtt_allow_shell_run": "1",
        "mqtt_allow_shell_run_async": "1",
        "mqtt_allow_shell_poll": "1",
        "mqtt_allow_shell_kill": "1",
        "mqtt_allow_console_input": "1",
        "mqtt_allow_digital_write": "1",
        "mqtt_allow_digital_read": "1",
        "mqtt_allow_digital_mode": "1",
        "mqtt_allow_analog_write": "1",
        "mqtt_allow_analog_read": "1",
        "metrics_enabled": "0",
        "metrics_host": DEFAULT_METRICS_HOST,
        "metrics_port": str(DEFAULT_METRICS_PORT),
    }


__all__: Final[tuple[str, ...]] = (
    "normalise_allowed_commands",
    "parse_bool",
    "parse_int",
    "parse_float",
    "encode_status_reason",
    "get_default_config",
    "get_uci_config",
    "build_mqtt_connect_properties",
    "build_mqtt_properties",
)

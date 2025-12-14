"""Utility helpers shared across Yun Bridge packages."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, fields
from typing import (
    Any,
    Final,
    Iterable as TypingIterable,
    Self,
    TypeVar,
    cast,
)

from paho.mqtt.packettypes import PacketTypes
from yunbridge.config.uci_model import UciConfigModel
from yunbridge.rpc.protocol import MAX_PAYLOAD_SIZE

from .const import (
    ALLOWED_COMMAND_WILDCARD,
    DEFAULT_BRIDGE_HANDSHAKE_INTERVAL,
    DEFAULT_BRIDGE_SUMMARY_INTERVAL,
    DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES,
    DEFAULT_FILE_STORAGE_QUOTA_BYTES,
    DEFAULT_FILE_SYSTEM_ROOT,
    DEFAULT_FILE_WRITE_MAX_BYTES,
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
    DEFAULT_SERIAL_BAUD,
    DEFAULT_SERIAL_HANDSHAKE_FATAL_FAILURES,
    DEFAULT_SERIAL_HANDSHAKE_MIN_INTERVAL,
    DEFAULT_SERIAL_PORT,
    DEFAULT_SERIAL_RESPONSE_TIMEOUT,
    DEFAULT_SERIAL_RETRY_ATTEMPTS,
    DEFAULT_SERIAL_RETRY_TIMEOUT,
    DEFAULT_STATUS_INTERVAL,
)


logger = logging.getLogger(__name__)

T = TypeVar("T")


def clamp(value: int, minimum: int, maximum: int) -> int:
    """Return *value* constrained to the ``[minimum, maximum]`` range."""
    return max(minimum, min(maximum, value))


def chunk_payload(data: bytes, max_size: int) -> tuple[bytes, ...]:
    """Split *data* in chunks of at most ``max_size`` bytes."""
    if max_size <= 0:
        raise ValueError("max_size must be positive")
    if not data:
        return tuple()
    return tuple(data[i : i + max_size] for i in range(0, len(data), max_size))


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


def deduplicate(sequence: Sequence[T]) -> tuple[T, ...]:
    """Return ``sequence`` without duplicates, preserving order."""
    seen: set[T] = set()
    result: list[T] = []
    for item in sequence:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return tuple(result)


def encode_status_reason(reason: str | None) -> bytes:
    """Return a UTF-8 encoded payload trimming to MAX frame limits."""
    if not reason:
        return b""
    payload = reason.encode("utf-8", errors="ignore")
    return payload[:MAX_PAYLOAD_SIZE]


def build_mqtt_properties(message: Any) -> Properties | None:
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


def apply_mqtt_connect_properties(client: Any) -> None:
    """Best-effort application of CONNECT properties onto paho clients."""
    if client is None:
        return

    try:
        props = build_mqtt_connect_properties()

        raw_client = getattr(client, "_client", None)
        native = getattr(raw_client, "_client", raw_client)
        if native is not None and hasattr(native, "_connect_properties"):
            setattr(native, "_connect_properties", props)
    except Exception:
        logger.debug(
            "Unable to apply MQTT CONNECT properties; continuing without",
            exc_info=True,
        )


def get_uci_config() -> dict[str, str]:
    """Read Yun Bridge configuration directly from OpenWrt's UCI system."""
    try:
        from uci import Uci  # type: ignore
    except ImportError:
        logger.warning(
            "UCI module not found (not running on OpenWrt?); using default configuration."
        )
        return get_default_config()

    try:
        with Uci() as cursor:
            # Assume 'yunbridge' package and 'general' section
            section = cursor.get_all("yunbridge", "general")
    except Exception as exc:
        logger.warning("Failed to load UCI configuration: %s", exc)
        return get_default_config()

    options = _extract_uci_options(section)
    if not options:
        logger.warning("UCI returned no options for 'yunbridge'; using defaults.")
        return get_default_config()

    return UciConfigModel.from_mapping(options).as_dict()


def _as_option_dict(candidate: Mapping[Any, Any]) -> dict[str, Any]:
    typed: dict[str, Any] = {}
    for key, value in candidate.items():
        typed[str(key)] = value
    return typed


def _extract_uci_options(section: Any) -> dict[str, Any]:
    """Normalise python3-uci section structures into a flat options dict."""
    if not isinstance(section, Mapping) or not section:
        return {}

    typed_section = _as_option_dict(cast(Mapping[str, Any], section))

    # Direct Key-Value dictionary (Simple case)
    # We accept any non-empty dictionary as a valid section,
    # filtering out internal UCI metadata (keys starting with dot).
    if typed_section:
        flattened: dict[str, Any] = {}
        for key, value in typed_section.items():
            if key.startswith("."):
                continue
            flattened[key] = value
        return flattened

    # Attempt to handle nested or complex responses (legacy shim fallback removed)
    # We now strictly expect a valid UCI section dict or we fail to defaults.

    return {}


def get_default_config() -> dict[str, str]:
    """Provide default Yun Bridge configuration values."""
    return UciConfigModel.defaults()


__all__: Final[tuple[str, ...]] = (
    "normalise_allowed_commands",
    "clamp",
    "chunk_payload",
    "deduplicate",
    "encode_status_reason",
    "get_default_config",
    "get_uci_config",
    "apply_mqtt_connect_properties",
    "build_mqtt_connect_properties",
    "build_mqtt_properties",
)

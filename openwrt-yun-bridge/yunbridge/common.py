"""Utility helpers shared across Yun Bridge packages."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import (
    Any,
    Final,
    TypeVar,
    cast,
)

from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties
from yunbridge.rpc.protocol import MAX_PAYLOAD_SIZE

from .const import (
    ALLOWED_COMMAND_WILDCARD,
)


logger = logging.getLogger(__name__)

T = TypeVar("T")


def parse_bool(value: Any) -> bool:
    """Parse a boolean value safely from various types."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if value is None:
        return False
    s = str(value).lower().strip()
    return s in ("1", "yes", "on", "true", "enable", "enabled")


def parse_int(value: Any, default: int) -> int:
    """Parse an integer value safely, handling floats and strings."""
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return default


def parse_float(value: Any, default: float) -> float:
    """Parse a float value safely."""
    try:
        return float(value)
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
    from yunbridge.config.uci_model import UciConfigModel

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
    from yunbridge.config.uci_model import UciConfigModel

    return UciConfigModel.defaults()


__all__: Final[tuple[str, ...]] = (
    "normalise_allowed_commands",
    "parse_bool",
    "parse_int",
    "parse_float",
    "encode_status_reason",
    "get_default_config",
    "get_uci_config",
    "apply_mqtt_connect_properties",
    "build_mqtt_connect_properties",
    "build_mqtt_properties",
)

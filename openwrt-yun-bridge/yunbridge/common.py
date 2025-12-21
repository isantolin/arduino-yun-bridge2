"""Utility helpers shared across Yun Bridge packages."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import (
    Final,
    TypeVar,
    TYPE_CHECKING,
)

from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties
from yunbridge.rpc import protocol

from .const import (
    ALLOWED_COMMAND_WILDCARD,
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
            # OpenWrt's python3-uci returns a native dict in modern versions.
            # We strictly expect the package 'yunbridge' and section 'general'.
            section = cursor.get_all(_UCI_PACKAGE, _UCI_SECTION)

            if not section:
                logger.warning("UCI section '%s.%s' not found; using defaults.", _UCI_PACKAGE, _UCI_SECTION)
                return get_default_config()

            # Clean internal UCI metadata (keys starting with dot/underscore)
            clean_config = {
                k: v for k, v in section.items()
                if not k.startswith((".", "_"))
            }

            return UciConfigModel.from_mapping(clean_config).as_dict()

    except Exception as exc:
        logger.warning("Failed to load UCI configuration: %s", exc)
        return get_default_config()


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
    "build_mqtt_connect_properties",
    "build_mqtt_properties",
)

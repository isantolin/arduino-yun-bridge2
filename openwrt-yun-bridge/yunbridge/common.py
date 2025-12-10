"""Utility helpers shared across Yun Bridge packages."""
from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping as MappingABC, Sequence
from dataclasses import dataclass, field, fields
from struct import pack as struct_pack, unpack as struct_unpack
from typing import (
    Any,
    Final,
    Iterable as TypingIterable,
    Self,
    TypeVar,
    cast,
)
from collections.abc import Mapping

# REMOVED: importlib (simplified UCI logic)
# REMOVED: more_itertools (native implementation provided)

from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

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


def pack_u16(value: int) -> bytes:
    """Pack ``value`` as big-endian unsigned 16-bit."""
    return struct_pack(">H", value & 0xFFFF)


def unpack_u16(data: bytes) -> int:
    """Decode the first two bytes of ``data`` as big-endian unsigned 16-bit."""
    if len(data) < 2:
        raise ValueError("payload shorter than 2 bytes for u16 unpack")
    return struct_unpack(">H", data[:2])[0]


def clamp(value: int, minimum: int, maximum: int) -> int:
    """Return *value* constrained to the ``[minimum, maximum]`` range."""
    return max(minimum, min(maximum, value))


def chunk_payload(data: bytes, max_size: int) -> tuple[bytes, ...]:
    """Split *data* in chunks of at most ``max_size`` bytes."""
    if max_size <= 0:
        raise ValueError("max_size must be positive")
    if not data:
        return tuple()
    # Optimized: Native slicing is faster and removes 'more_itertools' dependency
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
    # Optimized: Native set tracking removes 'more_itertools' dependency
    seen = set()
    result = []
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
    # Check if we have any property to set
    has_props = any([
        message.content_type,
        message.payload_format_indicator is not None,
        message.message_expiry_interval is not None,
        message.response_topic,
        message.correlation_data is not None,
        message.user_properties,
    ])

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
    # Modernization: Direct import preferred. Fail fast or fallback silently.
    try:
        from uci import Uci  # type: ignore
    except ImportError:
        logger.warning(
            "UCI module not found (not running on OpenWrt?); using default configuration."
        )
        return get_default_config()

    try:
        with Uci() as cursor:
            # We assume 'yunbridge' package and 'general' section type/name
            # Typically config is in /etc/config/yunbridge
            # We fetch all sections of type 'general' (or named 'general')
            # Assuming standard OpenWrt config structure: config general
            section = cursor.get_all("yunbridge", "general")
    except Exception as exc:
        logger.warning(
            "Failed to load UCI configuration: %s",
            exc,
        )
        return get_default_config()

    options: dict[str, Any] = _extract_uci_options(section)
    if not options:
        logger.warning(
            "UCI returned no options for 'yunbridge'; using defaults."
        )
        return get_default_config()
    
    return UciConfigModel.from_mapping(options).as_dict()


def _as_option_dict(candidate: Mapping[Any, Any]) -> dict[str, Any]:
    typed: dict[str, Any] = {}
    for key, value in candidate.items():
        typed[str(key)] = value
    return typed


def _extract_uci_options(section: Any) -> dict[str, Any]:
    """Normalise python3-uci section structures into a flat options dict."""
    if not isinstance(section, MappingABC) or not section:
        return {}

    typed_section = _as_option_dict(cast(Mapping[Any, Any], section))
    
    # Fast path: if it's already a flat dict of values, return it
    # Check for UCI specific metadata keys
    if ".name" in typed_section or ".type" in typed_section:
        flattened: dict[str, Any] = {}
        for key, value in typed_section.items():
            if key.startswith("."):
                continue
            flattened[key] = value
        return flattened

    # If it is nested (e.g. from uci.get_all returning complex structs)
    # We attempt to unwrap it.
    stack: list[dict[str, Any]] = [typed_section]
    while stack:
        current = stack.pop()
        
        # Check for standard UCI python binding nesting
        for key in ("options", "values"):
            nested = current.get(key)
            if isinstance(nested, MappingABC) and nested:
                return _as_option_dict(cast(Mapping[Any, Any], nested))

        # Flatten current
        flattened = {}
        for key, value in current.items():
            if str(key).startswith(".") or str(key).startswith("@"):
                continue
            if not isinstance(value, dict):
                 flattened[str(key)] = value
        
        if flattened:
            return flattened
            
    return {}


def _stringify_iterable(values: Iterable[Any]) -> str:
    return " ".join(str(item) for item in values)


def _stringify_value(value: Any) -> str:
    attr_value = getattr(value, "value", None)
    if attr_value is not None and not isinstance(value, (str, bytes)):
        return _stringify_value(attr_value)

    if isinstance(value, Mapping):
        # Flatten simple dicts if they appear in config
        return str(value)

    if isinstance(value, (tuple, list, set)):
        iterable_value = cast(TypingIterable[Any], value)
        return _stringify_iterable(iterable_value)

    return str(value) if value is not None else ""


def _extras_default() -> dict[str, str]:
    return {}


@dataclass(slots=True)
class UciConfigModel:
    """Structured representation of UCI options with sane defaults."""
    # Note: Logic logic moved to typed parsing in uci_model.py or kept simple here.
    # This class mirrors the properties of config.settings.RuntimeConfig but as strings/raw.

    mqtt_host: str = DEFAULT_MQTT_HOST
    mqtt_port: str = str(DEFAULT_MQTT_PORT)
    mqtt_tls: str = "1"
    mqtt_cafile: str = DEFAULT_MQTT_CAFILE
    mqtt_certfile: str = ""
    mqtt_keyfile: str = ""
    mqtt_user: str = ""
    mqtt_pass: str = ""
    mqtt_topic: str = DEFAULT_MQTT_TOPIC
    mqtt_spool_dir: str = DEFAULT_MQTT_SPOOL_DIR
    mqtt_queue_limit: str = str(DEFAULT_MQTT_QUEUE_LIMIT)
    serial_port: str = DEFAULT_SERIAL_PORT
    serial_baud: str = str(DEFAULT_SERIAL_BAUD)
    serial_shared_secret: str = ""
    serial_retry_timeout: str = str(DEFAULT_SERIAL_RETRY_TIMEOUT)
    serial_response_timeout: str = str(DEFAULT_SERIAL_RESPONSE_TIMEOUT)
    serial_retry_attempts: str = str(DEFAULT_SERIAL_RETRY_ATTEMPTS)
    serial_handshake_min_interval: str = str(
        DEFAULT_SERIAL_HANDSHAKE_MIN_INTERVAL
    )
    serial_handshake_fatal_failures: str = str(
        DEFAULT_SERIAL_HANDSHAKE_FATAL_FAILURES
    )
    debug: str = "0"
    allowed_commands: str = ""
    file_system_root: str = DEFAULT_FILE_SYSTEM_ROOT
    file_write_max_bytes: str = str(DEFAULT_FILE_WRITE_MAX_BYTES)
    file_storage_quota_bytes: str = str(DEFAULT_FILE_STORAGE_QUOTA_BYTES)
    process_timeout: str = str(DEFAULT_PROCESS_TIMEOUT)
    process_max_output_bytes: str = str(DEFAULT_PROCESS_MAX_OUTPUT_BYTES)
    process_max_concurrent: str = str(DEFAULT_PROCESS_MAX_CONCURRENT)
    console_queue_limit_bytes: str = str(DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES)
    mailbox_queue_limit: str = str(DEFAULT_MAILBOX_QUEUE_LIMIT)
    mailbox_queue_bytes_limit: str = str(DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT)
    pending_pin_request_limit: str = str(DEFAULT_PENDING_PIN_REQUESTS)
    reconnect_delay: str = str(DEFAULT_RECONNECT_DELAY)
    status_interval: str = str(DEFAULT_STATUS_INTERVAL)
    bridge_summary_interval: str = str(DEFAULT_BRIDGE_SUMMARY_INTERVAL)
    bridge_handshake_interval: str = str(DEFAULT_BRIDGE_HANDSHAKE_INTERVAL)
    mqtt_allow_file_read: str = "1"
    mqtt_allow_file_write: str = "1"
    mqtt_allow_file_remove: str = "1"
    mqtt_allow_datastore_get: str = "1"
    mqtt_allow_datastore_put: str = "1"
    mqtt_allow_mailbox_read: str = "1"
    mqtt_allow_mailbox_write: str = "1"
    mqtt_allow_shell_run: str = "1"
    mqtt_allow_shell_run_async: str = "1"
    mqtt_allow_shell_poll: str = "1"
    mqtt_allow_shell_kill: str = "1"
    mqtt_allow_console_input: str = "1"
    mqtt_allow_digital_write: str = "1"
    mqtt_allow_digital_read: str = "1"
    mqtt_allow_digital_mode: str = "1"
    mqtt_allow_analog_write: str = "1"
    mqtt_allow_analog_read: str = "1"
    metrics_enabled: str = "0"
    metrics_host: str = DEFAULT_METRICS_HOST
    metrics_port: str = str(DEFAULT_METRICS_PORT)
    extras: dict[str, str] = field(default_factory=_extras_default)

    def __post_init__(self) -> None:
        for dataclass_field in fields(self):
            if dataclass_field.name == "extras":
                continue
            value = getattr(self, dataclass_field.name)
            setattr(self, dataclass_field.name, _stringify_value(value))

        if self.extras:
            self.extras = {
                str(key): _stringify_value(value)
                for key, value in self.extras.items()
            }

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> Self:
        known = cls._known_fields()
        kwargs: dict[str, Any] = {}
        extras: dict[str, str] = {}
        for key, value in mapping.items():
            key_str = str(key)
            if key_str in known:
                kwargs[key_str] = value
            else:
                extras[key_str] = _stringify_value(value)
        return cls(extras=extras, **kwargs)

    def as_dict(self) -> dict[str, str]:
        values = {name: getattr(self, name) for name in self._known_fields()}
        values.update(self.extras)
        return values

    @classmethod
    def defaults(cls) -> dict[str, str]:
        return cls().as_dict()

    @classmethod
    def _known_fields(cls) -> set[str]:
        return {f.name for f in fields(cls) if f.name != "extras"}


def get_default_config() -> dict[str, str]:
    """Provide default Yun Bridge configuration values."""
    return UciConfigModel.defaults()


__all__: Final[tuple[str, ...]] = (
    "normalise_allowed_commands",
    "pack_u16",
    "unpack_u16",
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
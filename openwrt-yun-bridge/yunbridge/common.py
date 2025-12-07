"""Utility helpers shared across Yun Bridge packages."""
from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping as MappingABC, Sequence
from struct import pack as struct_pack, unpack as struct_unpack
from types import TracebackType
from typing import (
    Any,
    Dict,
    Final,
    Mapping,
    Self,
    Optional,
    Protocol,
    Tuple,
    TypeVar,
    cast,
)

from more_itertools import chunked, unique_everseen

try:
    from paho.mqtt.packettypes import PacketTypes
    from paho.mqtt.properties import Properties
except ImportError:
    PacketTypes = None
    Properties = None

from yunbridge.rpc.protocol import MAX_PAYLOAD_SIZE

from .config.uci_model import UciConfigModel
from .const import ALLOWED_COMMAND_WILDCARD


logger = logging.getLogger(__name__)

T = TypeVar("T")


class _UciCursor(Protocol):
    def __enter__(self) -> Self:
        ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        ...

    def get_all(self, package: str, section: str) -> Mapping[str, Any]:
        ...


class _UciModule(Protocol):
    UciException: type[Exception]

    def Uci(self) -> _UciCursor:
        ...


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
    return tuple(bytes(chunk) for chunk in chunked(data, max_size))


def normalise_allowed_commands(commands: Iterable[str]) -> Tuple[str, ...]:
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
    return tuple(unique_everseen(sequence))


def encode_status_reason(reason: Optional[str]) -> bytes:
    """Return a UTF-8 encoded payload trimming to MAX frame limits."""
    if not reason:
        return b""
    payload = reason.encode("utf-8", errors="ignore")
    return payload[:MAX_PAYLOAD_SIZE]


def build_mqtt_properties(message: Any) -> Any | None:
    """Construct Paho MQTT v5 properties from a message object."""
    if Properties is None or PacketTypes is None:
        return None

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


def get_uci_config() -> Dict[str, str]:
    """Read Yun Bridge configuration from OpenWrt's UCI system."""
    try:
        import uci as uci_runtime  # type: ignore[reportMissingImports]
    except ImportError as exc:  # pragma: no cover - fail fast in dev envs
        raise RuntimeError(
            "python3-uci is required to load Yun Bridge configuration."
        ) from exc

    uci_module = cast(_UciModule, uci_runtime)
    uci_exception = getattr(uci_module, "UciException", Exception)

    try:
        with uci_module.Uci() as cursor:
            section: Any = cursor.get_all("yunbridge", "general")
    except uci_exception as exc:
        logger.warning(
            "Failed to load UCI configuration via python3-uci: %s",
            exc,
        )
        return get_default_config()
    except Exception as exc:  # pragma: no cover - defensive catch-all
        logger.exception(
            "Unexpected error while reading UCI configuration: %s",
            exc,
        )
        return get_default_config()

    options: Dict[str, Any] = _extract_uci_options(section)
    if not options:
        logger.warning(
            "python3-uci returned no options for 'yunbridge'; using defaults."
        )
        return get_default_config()
    return UciConfigModel.from_mapping(options).as_dict()


def _as_option_dict(candidate: Mapping[Any, Any]) -> Dict[str, Any]:
    typed: Dict[str, Any] = {}
    for key, value in candidate.items():
        typed[str(key)] = value
    return typed


def _extract_uci_options(section: Any) -> Dict[str, Any]:
    """Normalise python3-uci section structures into a flat options dict."""
    if not isinstance(section, MappingABC) or not section:
        empty: Dict[str, Any] = {}
        return empty

    typed_section = _as_option_dict(cast(Mapping[Any, Any], section))
    stack: list[Dict[str, Any]] = [typed_section]
    while stack:
        current = stack.pop()
        for key in ("options", "values"):
            nested = current.get(key)
            if isinstance(nested, MappingABC) and nested:
                return _as_option_dict(cast(Mapping[Any, Any], nested))

        flattened: Dict[str, Any] = {}
        for key, value in current.items():
            if (
                key in {"name", "type", ".name", ".type"}
                or key.startswith("@")
            ):
                continue
            if not isinstance(value, dict) or any(
                nested_key in value for nested_key in ("value", "values")
            ):
                flattened[str(key)] = value

        if flattened:
            return flattened

        for nested in current.values():
            if isinstance(nested, MappingABC) and nested:
                stack.append(_as_option_dict(cast(Mapping[Any, Any], nested)))

    empty: Dict[str, Any] = {}
    return empty


def get_default_config() -> Dict[str, str]:
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
    "build_mqtt_properties",
)

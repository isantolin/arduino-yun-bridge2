"""Utility helpers shared across Yun Bridge packages."""
from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping as MappingABC, Sequence
from struct import pack as struct_pack, unpack as struct_unpack
from typing import Any, Dict, Final, Mapping, Optional, Protocol, Tuple, TypeVar, cast

from cobs import cobs as _cobs
from more_itertools import chunked, unique_everseen

from yunbridge.rpc.protocol import MAX_PAYLOAD_SIZE

from .const import (
    ALLOWED_COMMAND_WILDCARD,
    DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES,
    DEFAULT_FILE_SYSTEM_ROOT,
    DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT,
    DEFAULT_MAILBOX_QUEUE_LIMIT,
    DEFAULT_MQTT_HOST,
    DEFAULT_MQTT_CAFILE,
    DEFAULT_MQTT_PORT,
    DEFAULT_MQTT_QUEUE_LIMIT,
    DEFAULT_MQTT_TOPIC,
    DEFAULT_MQTT_SPOOL_DIR,
    DEFAULT_PROCESS_TIMEOUT,
    DEFAULT_PROCESS_MAX_CONCURRENT,
    DEFAULT_PROCESS_MAX_OUTPUT_BYTES,
    DEFAULT_SERIAL_BAUD,
    DEFAULT_SERIAL_PORT,
    DEFAULT_SERIAL_RESPONSE_TIMEOUT,
    DEFAULT_SERIAL_RETRY_ATTEMPTS,
    DEFAULT_SERIAL_RETRY_TIMEOUT,
)


logger = logging.getLogger(__name__)


class _CobsCodec(Protocol):
    def encode(self, data: bytes) -> bytes:
        ...

    def decode(self, data: bytes) -> bytes:
        ...


_COBC_MODULE: _CobsCodec = cast(_CobsCodec, _cobs)

DecodeError = getattr(_COBC_MODULE, "DecodeError", ValueError)

T = TypeVar("T")


def cobs_encode(data: bytes) -> bytes:
    """COBS-encode *data* using the upstream library."""

    return _COBC_MODULE.encode(data)


def cobs_decode(data: bytes) -> bytes:
    """COBS-decode *data* using the upstream library."""

    return _COBC_MODULE.decode(data)


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


def get_uci_config() -> Dict[str, str]:
    """Read Yun Bridge configuration from OpenWrt's UCI system."""

    try:
        import uci
    except ImportError as exc:  # pragma: no cover - fail fast in dev envs
        raise RuntimeError(
            "python3-uci is required to load Yun Bridge configuration."
        ) from exc

    uci_exception = getattr(uci, "UciException", Exception)

    try:
        with uci.Uci() as cursor:
            cursor = cast(Any, cursor)
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

    normalised: Dict[str, str] = {}
    for key, value in options.items():
        normalised[str(key)] = _stringify_value(value)
    return normalised


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


def _stringify_iterable(values: Iterable[Any]) -> str:
    parts: list[str] = []
    for item in values:
        parts.append(str(item))
    return " ".join(parts)


def _stringify_value(value: Any) -> str:
    """Convert UCI values (strings or sequences) to space-separated text."""

    attr_value = getattr(value, "value", None)
    if attr_value is not None and not isinstance(value, (str, bytes)):
        return _stringify_value(attr_value)

    if isinstance(value, dict):
        dict_value = cast(dict[Any, Any], value)
        if "value" in dict_value:
            return _stringify_value(dict_value["value"])
        values_candidate = dict_value.get("values")
        if isinstance(values_candidate, Iterable):
            iterable_value = cast(Iterable[Any], values_candidate)
            return _stringify_iterable(iterable_value)
        dict_items: Iterable[Any] = cast(Iterable[Any], dict_value.values())
        return _stringify_iterable(dict_items)

    if isinstance(value, (tuple, list)):
        iterable_value = cast(Iterable[Any], value)
        return _stringify_iterable(iterable_value)
    return str(value)


def get_default_config() -> Dict[str, str]:
    """Provide default Yun Bridge configuration values."""

    return {
        "mqtt_host": DEFAULT_MQTT_HOST,
        "mqtt_port": str(DEFAULT_MQTT_PORT),
        "mqtt_tls": "1",
        "mqtt_cafile": DEFAULT_MQTT_CAFILE,
        "mqtt_certfile": "",
        "mqtt_keyfile": "",
        "serial_port": DEFAULT_SERIAL_PORT,
        "serial_baud": str(DEFAULT_SERIAL_BAUD),
        "debug": "0",
        "allowed_commands": "",
        "mqtt_topic": DEFAULT_MQTT_TOPIC,
        "file_system_root": DEFAULT_FILE_SYSTEM_ROOT,
        "process_timeout": str(DEFAULT_PROCESS_TIMEOUT),
        "serial_shared_secret": "",
        "console_queue_limit_bytes": str(
            DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES
        ),
        "mailbox_queue_limit": str(DEFAULT_MAILBOX_QUEUE_LIMIT),
        "mailbox_queue_bytes_limit": str(
            DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT
        ),
        "mqtt_queue_limit": str(DEFAULT_MQTT_QUEUE_LIMIT),
        "serial_retry_timeout": str(DEFAULT_SERIAL_RETRY_TIMEOUT),
        "serial_response_timeout": str(DEFAULT_SERIAL_RESPONSE_TIMEOUT),
        "serial_retry_attempts": str(DEFAULT_SERIAL_RETRY_ATTEMPTS),
        "mqtt_allow_file_read": "1",
        "mqtt_allow_file_write": "1",
        "mqtt_allow_file_remove": "1",
        "mqtt_allow_datastore_get": "1",
        "mqtt_allow_datastore_put": "1",
        "mqtt_allow_mailbox_read": "1",
        "mqtt_allow_mailbox_write": "1",
        "mqtt_spool_dir": DEFAULT_MQTT_SPOOL_DIR,
        "process_max_output_bytes": str(DEFAULT_PROCESS_MAX_OUTPUT_BYTES),
        "process_max_concurrent": str(DEFAULT_PROCESS_MAX_CONCURRENT),
    }


__all__: Final[tuple[str, ...]] = (
    "DecodeError",
    "cobs_encode",
    "cobs_decode",
    "normalise_allowed_commands",
    "pack_u16",
    "unpack_u16",
    "clamp",
    "chunk_payload",
    "deduplicate",
    "encode_status_reason",
    "get_default_config",
    "get_uci_config",
)

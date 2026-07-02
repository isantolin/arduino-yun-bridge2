"""MCU Bridge Data Structures and Schemas.

SINGLE SOURCE OF TRUTH for all data structures.
Binary parsing uses stdlib struct; high-level schemas use Protobuf (SIL-2) [TESTED].
"""

from __future__ import annotations
from google.protobuf.message import Message as ProtobufMessage
from . import mcubridge_pb2 as pb


import asyncio
import enum
import functools
import itertools
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Final,
    NamedTuple,
    cast,
)


def iter_chunks(data: bytes, chunk_size: int) -> Iterable[bytes]:
    """Chunk bytes into fixed-size pieces using itertools.batched. [SIL-2]"""
    if not data:
        return
    yield from (bytes(chunk) for chunk in itertools.batched(data, chunk_size))


PROTOBUF_CONTENT_TYPE: Final[str] = "application/x-protobuf"


# [SIL-2] Compiled once at module load; reused across all AllowedCommandPolicy instances.
_TOKEN_SEP: Final = re.compile(r"[,\s]+")


@functools.lru_cache(maxsize=1)
def _get_action_lookup_map() -> dict[str, Any]:
    from .protocol import FileAction, ShellAction, SystemAction

    return {e.value: e for e in itertools.chain(FileAction, ShellAction, SystemAction)}


class TopicRoute(NamedTuple):
    """Parsed representation of an MQTT topic targeting the daemon."""

    raw: str
    prefix: str
    topic: Any  # Avoid circular import with .protocol.Topic
    segments: tuple[str, ...]

    @property
    def identifier(self) -> str:
        return self.segments[0] if self.segments else ""

    @property
    def action(self) -> Any:
        """Infer the service action from the first segment if applicable.
        Ignore segments that indicate a response flavor.
        """
        if not self.segments or "response" in self.segments or "value" in self.segments:
            return None
        val = self.segments[0]
        return _get_action_lookup_map().get(val, val)

    @property
    def remainder(self) -> tuple[str, ...]:
        return self.segments[1:] if len(self.segments) > 1 else ()


# =============================================================================
# 2. Security and Policy Helpers (Direct Protobuf)
# =============================================================================


def is_command_allowed(policy: pb.AllowedCommandPolicy, command: str) -> bool:
    """Check if a shell/process command is allowed by the policy. [SIL-2]"""
    import fnmatch
    from mcubridge.config.const import ALLOWED_COMMAND_WILDCARD

    pieces = command.strip().split()
    if not pieces:
        return False
    return ALLOWED_COMMAND_WILDCARD in policy.entries or any(
        fnmatch.fnmatch(pieces[0].lower(), p) for p in policy.entries
    )


def create_allowed_policy(entries: Iterable[str]) -> pb.AllowedCommandPolicy:
    """Create a normalized AllowedCommandPolicy Protobuf message. [SIL-2]"""
    all_tokens = list(
        itertools.chain.from_iterable(filter(None, _TOKEN_SEP.split(c.strip().lower())) for c in entries if c)
    )
    items: set[str] = set(all_tokens)
    normalised = ["*"] if "*" in items else sorted(items)
    return pb.AllowedCommandPolicy(entries=normalised)


@functools.lru_cache(maxsize=1)
def _get_topic_auth_mapping_v3() -> dict[tuple[str, str], str]:
    import mcubridge.protocol.protocol as proto
    from mcubridge.protocol.topics import Topic

    mapping: dict[tuple[str, str], str] = {}
    fields = [f.name for f in pb.TopicAuthorization.DESCRIPTOR.fields]
    for field, t in itertools.product(fields, Topic):
        prefix = t.name.lower()
        if not field.startswith(f"{prefix}_"):
            continue
        suffix = field[len(prefix) + 1 :]
        action_class_name = "SpiAction" if t == Topic.SPI else f"{t.name.title()}Action"
        action_cls = getattr(proto, action_class_name, None)
        if action_cls is None:
            continue
        for act in action_cls:
            if act.value == suffix or (suffix == "input" and act.value == "in"):
                mapping[(t.value, act.value)] = field
                break
    return mapping


def allows_topic(auth: pb.TopicAuthorization, topic: str, action: str) -> bool:
    """Check if a specific topic/action combination is authorized. [SIL-2]"""
    mapping = _get_topic_auth_mapping_v3()
    field_name = mapping.get((topic.lower(), action.lower()))
    if field_name is not None:
        return bool(getattr(auth, field_name))
    return False


# =============================================================================
# 3. Runtime Configuration Structures (Protobuf-backed)
# =============================================================================


# Type alias — RuntimeConfig IS pb.RuntimeConfig, no proxy layer needed.
RuntimeConfig = pb.RuntimeConfig


def validate_config(cfg: pb.RuntimeConfig) -> None:
    """Validate and normalize a RuntimeConfig in-place. [SIL-2]"""
    from mcubridge.config.const import (
        DEFAULT_SERIAL_SHARED_SECRET,
        VOLATILE_STORAGE_PATHS,
    )

    cfg.allowed_policy.CopyFrom(create_allowed_policy(cfg.allowed_commands))
    del cfg.allowed_commands[:]
    cfg.allowed_commands.extend(cfg.allowed_policy.entries)

    if not any(getattr(cfg.topic_authorization, f.name) for f in cfg.topic_authorization.DESCRIPTOR.fields):
        for field in [f.name for f in cfg.topic_authorization.DESCRIPTOR.fields]:
            setattr(cfg.topic_authorization, field, True)

    if not cfg.mqtt_topic or not any(filter(None, cfg.mqtt_topic.split("/"))):
        raise ValueError("mqtt_topic must contain at least one segment")

    if cfg.serial_response_timeout < cfg.serial_retry_timeout * 2:
        raise ValueError("serial_response_timeout must be at least 2x serial_retry_timeout")

    if cfg.watchdog_enabled and cfg.watchdog_interval < 0.5:
        raise ValueError("watchdog_interval must be >= 0.5s when enabled")

    if not cfg.serial_shared_secret:
        raise ValueError("serial_shared_secret must be configured")

    if cfg.serial_shared_secret == b"changeme123":
        raise ValueError("serial_shared_secret placeholder is insecure")

    unique_symbols = {byte for byte in cfg.serial_shared_secret}
    if len(unique_symbols) < 4 and cfg.serial_shared_secret != DEFAULT_SERIAL_SHARED_SECRET:
        raise ValueError("serial_shared_secret must contain at least four distinct bytes")

    if cfg.file_storage_quota_bytes < cfg.file_write_max_bytes:
        raise ValueError("file_storage_quota_bytes must be greater than or equal to file_write_max_bytes")

    if cfg.mailbox_queue_bytes_limit < cfg.mailbox_queue_limit:
        raise ValueError("mailbox_queue_bytes_limit must be greater than or equal to mailbox_queue_limit")

    if not cfg.allow_non_tmp_paths:
        if not any(cfg.mqtt_spool_dir.startswith(p) for p in VOLATILE_STORAGE_PATHS):
            msg = f"FLASH PROTECTION: mqtt_spool_dir ({cfg.mqtt_spool_dir}) must be in volatile storage"
            raise ValueError(msg)
        if not any(cfg.file_system_root.startswith(p) for p in VOLATILE_STORAGE_PATHS):
            raise ValueError(f"FLASH PROTECTION: file_system_root ({cfg.file_system_root}) must be in volatile storage")


def get_ssl_context(cfg: pb.RuntimeConfig) -> Any | None:
    """Create an ssl.SSLContext based on cfg. [SIL-2]"""
    if not cfg.mqtt_tls:
        return None

    import ssl
    from mcubridge.config.const import MQTT_TLS_MIN_VERSION

    try:
        if cfg.mqtt_cafile:
            ca_path = Path(cfg.mqtt_cafile)
            if not ca_path.exists():
                raise RuntimeError(f"MQTT TLS CA file missing: {cfg.mqtt_cafile}")
            context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(ca_path))
        else:
            context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)

        context.minimum_version = MQTT_TLS_MIN_VERSION

        if cfg.mqtt_tls_insecure:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE

        if cfg.mqtt_certfile or cfg.mqtt_keyfile:
            if not (cfg.mqtt_certfile and cfg.mqtt_keyfile):
                raise ValueError("Both mqtt_certfile and mqtt_keyfile must be provided for mTLS.")
            context.load_cert_chain(cfg.mqtt_certfile, cfg.mqtt_keyfile)

        return context
    except (OSError, ssl.SSLError, ValueError) as exc:
        raise RuntimeError(f"TLS setup failed: {exc}") from exc


# =============================================================================
# 3. Operational Structures
# =============================================================================


def _flatten_structured_value(
    key_prefix: str,
    value: Any,
    entries: list[pb.StructuredEntry],
) -> None:

    if isinstance(value, ProtobufMessage):
        from google.protobuf.json_format import MessageToDict

        proto_fields = MessageToDict(value, preserving_proto_field_name=True)
        for key, nested in proto_fields.items():
            _flatten_structured_value(f"{key_prefix}.{key}" if key_prefix else key, nested, entries)
        return

    if isinstance(value, (list, tuple)):
        nested: Any
        for i, nested in enumerate(cast(list[Any] | tuple[Any, ...], value)):
            _flatten_structured_value(f"{key_prefix}.{i}" if key_prefix else str(i), nested, entries)
        return
    if isinstance(value, Mapping):
        mapped_value = cast(Mapping[str, Any], value)
        for key, nested in mapped_value.items():
            key_name = str(key)
            _flatten_structured_value(f"{key_prefix}.{key_name}" if key_prefix else key_name, nested, entries)
        return

    entry = pb.StructuredEntry(key=key_prefix)
    if value is None:
        entry.null_value = True
    elif isinstance(value, bytes):
        entry.bytes_value = value
    elif isinstance(value, str):
        entry.string_value = value
    elif isinstance(value, bool):
        entry.bool_value = value
    elif isinstance(value, enum.IntEnum):
        entry.int_value = int(value)
    elif isinstance(value, int):
        entry.int_value = value
    elif isinstance(value, float):
        entry.float_value = value
    else:
        raise TypeError(f"Unsupported structured payload value for '{key_prefix}': {type(value)!r}")
    entries.append(entry)


def encode_structured_payload(payload: Mapping[str, Any]) -> bytes:
    message = pb.StructuredPayload()
    entries: list[pb.StructuredEntry] = []
    for key, value in payload.items():
        _flatten_structured_value(str(key), value, entries)
    message.entries.extend(entries)
    return message.SerializeToString()


def _entry_value(entry: pb.StructuredEntry) -> Any:
    match entry.WhichOneof("value"):
        case "string_value":
            return entry.string_value
        case "bytes_value":
            return bytes(entry.bytes_value)
        case "bool_value":
            return entry.bool_value
        case "int_value":
            return entry.int_value
        case "float_value":
            return entry.float_value
        case "null_value":
            return None
        case _:
            raise ValueError(f"StructuredEntry '{entry.key}' missing value")


def decode_structured_payload(data: bytes) -> dict[str, Any]:
    message = pb.StructuredPayload()
    message.ParseFromString(data)
    decoded: dict[str, Any] = {}
    for entry in message.entries:
        cursor: dict[str, Any] = decoded
        parts = entry.key.split(".")
        for part in parts[:-1]:
            next_cursor_obj = cursor.get(part)
            if not isinstance(next_cursor_obj, dict):
                next_cursor: dict[str, Any] = {}
                cursor[part] = next_cursor
            else:
                next_cursor = cast(dict[str, Any], next_cursor_obj)
            cursor = next_cursor
        cursor[parts[-1]] = _entry_value(entry)
    return decoded


# --- Binary Protocol Packets ---


class PayloadValidationError(ValueError):
    """Raised when an inbound MQTT payload cannot be validated."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# --- High-Level Structure ---


@dataclass
class PendingPinRequest:
    pin: int
    reply_context: Any | None = None


# --- MQTT Spool Helpers ---


UserProperty = tuple[str, str]


def replace_mqtt_publish(message: pb.MqttQueuedPublish, **kwargs: Any) -> pb.MqttQueuedPublish:
    """Create a new MqttQueuedPublish with fields replaced."""
    newpb_obj = pb.MqttQueuedPublish()
    newpb_obj.CopyFrom(message)
    for k, v in kwargs.items():
        if k == "user_properties":
            del newpb_obj.user_properties[:]
            for pk, pv in v:
                newpb_obj.user_properties.add(key=pk, value=pv)
        elif k == "subscription_identifier":
            del newpb_obj.subscription_identifier[:]
            if v is not None:
                newpb_obj.subscription_identifier.extend(v)
        else:
            setattr(newpb_obj, k, v)
    return newpb_obj


def resolve_mqtt_context(message: pb.MqttQueuedPublish, context: Any | None) -> pb.MqttQueuedPublish:
    """Resolve MQTT request-reply context into the publish message."""
    if context is None:
        return message

    updates: dict[str, Any] = {}

    rt = getattr(context, "response_topic", None)
    if rt is None:
        props = getattr(context, "properties", None)
        if props:
            rt = getattr(props, "ResponseTopic", None)
    if rt is not None:
        updates["topic_name"] = str(rt)

    cd = getattr(context, "correlation_data", None)
    if cd is None:
        props = getattr(context, "properties", None)
        if props:
            cd = getattr(props, "CorrelationData", None)
    if cd is not None:
        updates["correlation_data"] = bytes(cd)

    user_props = [(p.key, p.value) for p in message.user_properties]
    if req_topic := getattr(context, "topic", None):
        user_props.append(("bridge-request-topic", str(req_topic)))

    newpb_obj = pb.MqttQueuedPublish()
    newpb_obj.CopyFrom(message)
    if "topic_name" in updates:
        newpb_obj.topic_name = updates["topic_name"]
    if "correlation_data" in updates:
        newpb_obj.correlation_data = updates["correlation_data"]

    del newpb_obj.user_properties[:]
    for k, v in user_props:
        newpb_obj.user_properties.add(key=k, value=v)

    return newpb_obj


def create_queued_publish(
    topic_name: str,
    payload: bytes,
    content_type: str | None = None,
    message_expiry_interval: int | None = None,
    user_properties: Iterable[tuple[str, str]] = (),
    qos: int = 1,
) -> pb.MqttQueuedPublish:
    """Factory to create a MqttQueuedPublish message. [SIL-2]"""
    msg = pb.MqttQueuedPublish(
        topic_name=topic_name,
        payload=payload,
        content_type=content_type or "",
        qos=qos,
    )
    if message_expiry_interval is not None:
        msg.message_expiry_interval = message_expiry_interval
    for k, v in user_properties:
        msg.user_properties.add(key=k, value=v)
    return msg


def encode_queued_publish(message: pb.MqttQueuedPublish) -> bytes:
    """Helper to serialize MqttQueuedPublish to Protobuf binary format."""
    return message.SerializeToString()


def decode_queued_publish(data: bytes) -> pb.MqttQueuedPublish:
    """Helper to deserialize MqttQueuedPublish from Protobuf binary format."""
    pb_msg = pb.MqttQueuedPublish()
    pb_msg.ParseFromString(data)
    return pb_msg


# --- Serial Flow Structures ---


class PendingCommand:
    """Book-keeping for a tracked command in flight. [SIL-2]"""

    def __init__(
        self,
        command_id: int,
        expected_resp_ids: Iterable[int] = (),
        reply_topic: str | None = None,
        correlation_data: bytes | None = None,
    ) -> None:
        self.command_id = command_id
        self.expected_resp_ids = list(expected_resp_ids)
        self.reply_topic = reply_topic
        self.correlation_data = correlation_data
        self.attempts = 0
        self.success: bool | None = None
        self.failure_status: int | None = None
        self.ack_received = False
        self.completion = asyncio.Event()
        self.response_payload: bytes | ProtobufMessage | None = None

    def mark_success(self, payload: bytes | ProtobufMessage | None = None) -> None:
        self.response_payload = payload
        self.success = True
        if not self.completion.is_set():
            self.completion.set()

    def mark_failure(self, status: int | None) -> None:
        self.success = False
        if status is not None:
            self.failure_status = status
        if not self.completion.is_set():
            self.completion.set()

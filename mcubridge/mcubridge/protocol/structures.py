"""MCU Bridge Data Structures and Schemas.

SINGLE SOURCE OF TRUTH for all data structures.
Binary parsing uses stdlib struct; high-level schemas use Protobuf (SIL-2) [TESTED].
"""

from __future__ import annotations

import asyncio
import fnmatch
import re
import ssl
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Final,
    Protocol,
    overload,
)

import anyio
from anyio.streams.memory import MemoryObjectSendStream
import structlog
from google.protobuf.descriptor import FieldDescriptor
from google.protobuf.message import DecodeError as ProtobufDecodeError
from google.protobuf.message import Message as ProtobufMessage

from mcubridge.config.const import (
    ALLOWED_COMMAND_WILDCARD,
    CLOUD_TLS_MIN_VERSION,
    PROP_KEY_BRIDGE_REQUEST_TOPIC,
    VOLATILE_STORAGE_PATHS,
)

from . import mcubridge_pb2 as pb
from . import protocol

logger = structlog.get_logger(__name__)


def iter_chunks(data: bytes | memoryview, chunk_size: int) -> Iterable[bytes]:
    """Chunk bytes into fixed-size pieces using memoryview zero-copy slicing. [SIL-2]"""
    mv = memoryview(data)
    for i in range(0, len(mv), chunk_size):
        end = i + chunk_size
        yield bytes(mv[i:end])


PROTOBUF_CONTENT_TYPE: Final[str] = "application/x-protobuf"

_TOKEN_SEP: Final = re.compile(r"[,\s]+")
_TOPIC_AUTH_FIELDS: Final[tuple[str, ...]] = tuple(f.name for f in pb.TopicAuthorization.DESCRIPTOR.fields)


# =============================================================================
# 2. Security and Policy Helpers (Direct Protobuf)
# =============================================================================


def is_command_allowed(policy: pb.AllowedCommandPolicy, command: str) -> bool:
    """Check if a shell/process command is allowed by the policy. [SIL-2]"""
    pieces = command.split()
    if not pieces:
        return False
    return ALLOWED_COMMAND_WILDCARD in policy.entries or any(
        fnmatch.fnmatch(pieces[0].lower(), p) for p in policy.entries
    )


def create_allowed_policy(entries: Iterable[str]) -> pb.AllowedCommandPolicy:
    """Create a normalized AllowedCommandPolicy Protobuf message. [SIL-2]"""
    items = {item for c in entries if c for item in _TOKEN_SEP.split(c.strip().lower()) if item}
    normalised = ["*"] if "*" in items else sorted(items)
    return pb.AllowedCommandPolicy(entries=normalised)


# =============================================================================
# 3. Runtime Configuration Structures (Protobuf-backed)
# =============================================================================


# Type alias — RuntimeConfig IS pb.RuntimeConfig, no proxy layer needed.
RuntimeConfig = pb.RuntimeConfig


_NUMERIC_FIELD_TYPES: Final[frozenset[int]] = frozenset(
    {
        FieldDescriptor.TYPE_INT32,
        FieldDescriptor.TYPE_INT64,
        FieldDescriptor.TYPE_UINT32,
        FieldDescriptor.TYPE_UINT64,
        FieldDescriptor.TYPE_SINT32,
        FieldDescriptor.TYPE_SINT64,
        FieldDescriptor.TYPE_FIXED32,
        FieldDescriptor.TYPE_FIXED64,
        FieldDescriptor.TYPE_SFIXED32,
        FieldDescriptor.TYPE_SFIXED64,
        FieldDescriptor.TYPE_FLOAT,
        FieldDescriptor.TYPE_DOUBLE,
    }
)


def _validate_metadata_bounds(cfg: pb.RuntimeConfig) -> None:
    """Enforce scalar bounds declared in Protobuf field metadata (single source of truth)."""
    fields = cfg.DESCRIPTOR.fields_by_name
    for field_name, metadata in protocol.RUNTIME_CONFIG_METADATA.items():
        field = fields.get(field_name)
        if field is None:
            raise RuntimeError(f"RuntimeConfig metadata references unknown field: {field_name}")
        minimum, maximum = metadata.get("min"), metadata.get("max")
        if minimum is None and maximum is None:
            continue
        if field.is_repeated or field.type not in _NUMERIC_FIELD_TYPES:
            raise RuntimeError(f"Numeric bounds declared for non-scalar numeric field: {field_name}")

        value = getattr(cfg, field_name)
        # The watchdog lower bound only applies while watchdog monitoring is enabled.
        effective_minimum = None if field_name == "watchdog_interval" and not cfg.watchdog_enabled else minimum
        if effective_minimum is not None and value < effective_minimum:
            raise ValueError(f"{field_name}: must be >= {effective_minimum} (got {value})")
        if maximum is not None and value > maximum:
            raise ValueError(f"{field_name}: must be <= {maximum} (got {value})")


def validate_config(cfg: pb.RuntimeConfig) -> None:
    """Validate and normalize a RuntimeConfig in-place natively. [SIL-2]"""
    _validate_metadata_bounds(cfg)
    if not cfg.serial_port:
        raise ValueError("serial_port: value length must be at least 1")
    if not cfg.topic_prefix:
        raise ValueError("topic_prefix: value length must be at least 1")
    if not re.search(r"[^/]", cfg.topic_prefix):
        raise ValueError("topic_prefix: does not match regex pattern '[^/]'")
    if not cfg.allow_non_tmp_paths:
        for field_name, meta in protocol.RUNTIME_CONFIG_METADATA.items():
            if meta.get("volatile"):
                val = getattr(cfg, field_name, "")
                if val and not str(val).startswith(tuple(VOLATILE_STORAGE_PATHS)):
                    raise ValueError(
                        f"{field_name}: {field_name} must be in volatile storage "
                        "(/tmp, /var/run, /run, /dev/shm) unless allow_non_tmp_paths is set"
                    )
    if bool(cfg.cloud_certfile) != bool(cfg.cloud_keyfile):
        raise ValueError("cloud_certfile: cloud_certfile and cloud_keyfile must both be set or both be empty")
    if not cfg.serial_shared_secret:
        raise ValueError("serial_shared_secret: value length must be at least 1")

    cfg.allowed_policy.CopyFrom(create_allowed_policy(cfg.allowed_commands))
    del cfg.allowed_commands[:]
    cfg.allowed_commands.extend(cfg.allowed_policy.entries)

    if not any(getattr(cfg.topic_authorization, name) for name in _TOPIC_AUTH_FIELDS):
        for name in _TOPIC_AUTH_FIELDS:
            setattr(cfg.topic_authorization, name, True)


def get_ssl_context(cfg: pb.RuntimeConfig) -> ssl.SSLContext | None:
    """Create a standard ssl.SSLContext based on cfg. [SIL-2]"""
    if not cfg.cloud_tls:
        return None

    try:
        if cfg.cloud_cafile:
            ca_path = Path(cfg.cloud_cafile)
            if not ca_path.exists():
                raise RuntimeError(f"Cloud TLS CA file missing: {cfg.cloud_cafile}")
            context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(ca_path))
        else:
            context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)

        context.minimum_version = CLOUD_TLS_MIN_VERSION

        if cfg.cloud_tls_insecure:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE

        if cfg.cloud_certfile:
            context.load_cert_chain(cfg.cloud_certfile, cfg.cloud_keyfile)

        return context
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"TLS setup failed: {exc}") from exc


# =============================================================================
# 3. Operational Structures
# =============================================================================


# --- High-Level Structure ---


@dataclass(slots=True)
class PendingPinRequest:
    pin: int
    reply_context: Any | None = None


# --- CLOUD Spool Helpers ---


def resolve_cloud_context(message: pb.CloudQueuedPublish, context: Any | None) -> pb.CloudQueuedPublish:
    """Resolve CLOUD request-reply context into the publish message."""
    if context is None:
        return message

    newpb_obj = pb.CloudQueuedPublish()
    newpb_obj.CopyFrom(message)

    props = getattr(context, "properties", None)
    has_rt = (
        context.HasField("response_topic")
        if hasattr(context, "HasField")
        else bool(getattr(context, "response_topic", None))
    )
    rt = getattr(context, "response_topic", None) if has_rt else None
    if not rt and props:
        rt = getattr(props, "ResponseTopic", None)
    if rt:
        newpb_obj.topic_name = str(rt)

    has_cd = (
        context.HasField("correlation_data")
        if hasattr(context, "HasField")
        else bool(getattr(context, "correlation_data", None))
    )
    cd = getattr(context, "correlation_data", None) if has_cd else None
    if not cd and props:
        cd = getattr(props, "CorrelationData", None)
    if cd:
        newpb_obj.correlation_data = bytes(cd)

    if req_topic := getattr(context, "topic", None):
        newpb_obj.user_properties.add(key=PROP_KEY_BRIDGE_REQUEST_TOPIC, value=str(req_topic))

    return newpb_obj


def create_queued_publish(
    topic_name: str,
    payload: bytes,
    content_type: str | None = None,
    message_expiry_interval: int | None = None,
    user_properties: Iterable[tuple[str, str]] = (),
    qos: int = 1,
) -> pb.CloudQueuedPublish:
    """Factory to create a CloudQueuedPublish message. [SIL-2]"""
    msg = pb.CloudQueuedPublish(
        topic_name=topic_name,
        payload=payload,
        content_type=content_type or "",
        qos=qos,
        user_properties=[pb.UserProperty(key=k, value=v) for k, v in user_properties],
    )
    if message_expiry_interval is not None:
        msg.message_expiry_interval = message_expiry_interval
    return msg


# --- Serial Flow Structures ---


@dataclass(slots=True)
class PendingCommand:
    """Book-keeping for a tracked command in flight. [SIL-2]"""

    command_id: int
    sequence_id: int | None = None
    expected_resp_ids: list[int] = field(default_factory=list[int])
    reply_topic: str | None = None
    correlation_data: bytes | None = None
    attempts: int = 0
    success: bool | None = None
    failure_status: int | None = None
    ack_received: bool = False
    completion: asyncio.Event = field(default_factory=asyncio.Event)
    response_payload: bytes | ProtobufMessage | None = None

    def mark_success(self, payload: bytes | ProtobufMessage | None = None) -> None:
        self.response_payload = payload
        self.success = True
        self.completion.set()

    def mark_failure(self, status: int | None) -> None:
        self.success = False
        if status is not None:
            self.failure_status = status
        self.completion.set()


class ConsoleQueueTarget(Protocol):
    def put_nowait(self, item: pb.CloudQueuedPublish) -> None: ...
    async def put(self, item: pb.CloudQueuedPublish) -> None: ...


class MemoryQueueAdapter:
    """Adapts an AnyIO MemoryObjectSendStream to queue-compatible API. [SIL-2]"""

    __slots__ = ("_send_stream",)
    _send_stream: MemoryObjectSendStream[pb.CloudQueuedPublish]

    def __init__(self, send_stream: MemoryObjectSendStream[pb.CloudQueuedPublish]) -> None:
        self._send_stream = send_stream

    def put_nowait(self, item: pb.CloudQueuedPublish) -> None:
        try:
            self._send_stream.send_nowait(item)
        except (anyio.BrokenResourceError, anyio.ClosedResourceError) as exc:
            logger.debug("Console stream subscriber disconnected during put_nowait", error=str(exc))
        except anyio.WouldBlock as exc:
            logger.warning("Console stream subscriber buffer full, dropping message", error=str(exc))

    async def put(self, item: pb.CloudQueuedPublish) -> None:
        try:
            await self._send_stream.send(item)
        except (anyio.BrokenResourceError, anyio.ClosedResourceError) as exc:
            logger.debug("Console stream subscriber disconnected during put", error=str(exc))

    def close(self) -> None:
        self._send_stream.close()


@overload
def parse_serial_response[T: ProtobufMessage](res: Any, target_type: type[T], default: T) -> T: ...


@overload
def parse_serial_response[T: ProtobufMessage](res: Any, target_type: type[T], default: None = None) -> T | None: ...


def parse_serial_response[T: ProtobufMessage](res: Any, target_type: type[T], default: T | None = None) -> T | None:
    """Safely decode serial response payload to target Protobuf message type. [SIL-2]"""
    if isinstance(res, target_type):
        return res
    if isinstance(res, (bytes, bytearray)):
        try:
            return target_type.FromString(bytes(res))
        except (ProtobufDecodeError, TypeError, ValueError) as exc:
            logger.warning(
                "Failed to decode serial response into protobuf",
                target=target_type.__name__,
                error=str(exc),
            )
            return default
    return default

"""MCU Bridge Data Structures and Schemas.

SINGLE SOURCE OF TRUTH for all data structures.
Binary parsing uses stdlib struct; high-level schemas use Protobuf (SIL-2) [TESTED].
"""

from __future__ import annotations

import fnmatch
import re
import shlex
import ssl
from collections.abc import Iterable
from pathlib import Path
from typing import (
    Any,
    Final,
    Protocol,
)

import anyio
from anyio.streams.memory import MemoryObjectSendStream
import structlog
from google.protobuf.descriptor import FieldDescriptor

from ssl import TLSVersion

from . import cloud_gateway_pb2, config_schema_pb2
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
_TOPIC_AUTH_FIELDS: Final[tuple[str, ...]] = tuple(
    f.name for f in config_schema_pb2.TopicAuthorization.DESCRIPTOR.fields
)


# =============================================================================
# 2. Security and Policy Helpers (Direct Protobuf)
# =============================================================================


def _matches_program(entry: str, program: str) -> bool:
    """Match one policy entry against argv[0], case-sensitively.

    An entry containing "/" names one exact executable path (no globbing, so
    "/usr/bin/*" cannot reach "/usr/bin/../../tmp/x"). Any other entry is a
    glob over a bare program name and never matches a path.
    """
    if "/" in entry:
        return program == entry
    return "/" not in program and fnmatch.fnmatchcase(program, entry)


def parse_allowed_command(policy: config_schema_pb2.AllowedCommandPolicy, command: str) -> list[str] | None:
    """Return the argv to execute if the policy allows it, otherwise None. [SIL-2]

    The command is tokenized once with shlex and the same argv is both checked
    and executed, so validation cannot diverge from execution. Malformed
    quoting and empty commands are rejected.
    """
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    if not argv:
        return None
    program = argv[0]
    if "*" in policy.entries or any(_matches_program(entry, program) for entry in policy.entries):
        return argv
    return None


def create_allowed_policy(entries: Iterable[str]) -> config_schema_pb2.AllowedCommandPolicy:
    """Create a normalized AllowedCommandPolicy Protobuf message (case preserved). [SIL-2]"""
    items = {item for c in entries if c for item in _TOKEN_SEP.split(c.strip()) if item}
    normalised = ["*"] if "*" in items else sorted(items)
    return config_schema_pb2.AllowedCommandPolicy(entries=normalised)


# =============================================================================
# 3. Runtime Configuration Structures (Protobuf-backed)
# =============================================================================


# Type alias — RuntimeConfig IS config_schema_pb2.RuntimeConfig, no proxy layer needed.
RuntimeConfig = config_schema_pb2.RuntimeConfig


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


def _validate_metadata_bounds(cfg: config_schema_pb2.RuntimeConfig) -> None:
    """Enforce scalar bounds declared in Protobuf field metadata (single source of truth)."""
    fields = cfg.DESCRIPTOR.fields_by_name
    for field_name, metadata in protocol.RUNTIME_CONFIG_METADATA.items():
        field = fields.get(field_name)
        if field is None:
            raise RuntimeError(f"RuntimeConfig metadata references unknown field: {field_name}")
        minimum, maximum = metadata.get("min"), metadata.get("max")
        if minimum is None and maximum is None:
            continue
        is_repeated = (
            getattr(field, "is_repeated", False) or getattr(field, "label", None) == FieldDescriptor.LABEL_REPEATED
        )
        if is_repeated or field.type not in _NUMERIC_FIELD_TYPES:
            raise RuntimeError(f"Numeric bounds declared for non-scalar numeric field: {field_name}")

        value = getattr(cfg, field_name)
        # The watchdog lower bound only applies while watchdog monitoring is enabled.
        effective_minimum = None if field_name == "watchdog_interval" and not cfg.watchdog_enabled else minimum
        if effective_minimum is not None and value < effective_minimum:
            if field_name == "watchdog_interval" and cfg.watchdog_enabled:
                raise ValueError(f"{field_name} must be >= {effective_minimum}s when enabled")
            raise ValueError(f"{field_name}: must be >= {effective_minimum} (got {value})")
        if maximum is not None and value > maximum:
            raise ValueError(f"{field_name}: must be <= {maximum} (got {value})")


def validate_config(cfg: config_schema_pb2.RuntimeConfig) -> None:
    """Validate and normalize a RuntimeConfig in-place natively. [SIL-2]"""
    # Validate required identifiers first, preserving actionable error precedence.
    if not cfg.serial_port:
        raise ValueError("serial_port: value length must be at least 1")
    if not cfg.topic_prefix:
        raise ValueError("topic_prefix: value length must be at least 1")
    if not re.search(r"[^/]", cfg.topic_prefix):
        raise ValueError("topic_prefix: does not match regex pattern '[^/]'")
    _validate_metadata_bounds(cfg)
    if not cfg.allow_non_tmp_paths:
        for field_name, meta in protocol.RUNTIME_CONFIG_METADATA.items():
            if meta.get("volatile"):
                val = getattr(cfg, field_name, "")
                if val and not protocol.path_within_roots(str(val), protocol.VOLATILE_STORAGE_PATHS):
                    raise ValueError(
                        f"{field_name}: {field_name} must be in volatile storage "
                        "(/tmp, /var/run, /run, /dev/shm) unless allow_non_tmp_paths is set"
                    )
        # ADR-0007: file storage is persistent by default, confined to the
        # approved persistent roots or RAM unless explicitly opted out.
        storage_roots = protocol.VOLATILE_STORAGE_PATHS | protocol.PERSISTENT_STORAGE_PATHS
        if cfg.file_system_root and not protocol.path_within_roots(cfg.file_system_root, storage_roots):
            raise ValueError(
                "file_system_root: file_system_root must be under /srv/mcubridge, /mnt or volatile "
                "storage unless allow_non_tmp_paths is set"
            )
    if bool(cfg.cloud_certfile) != bool(cfg.cloud_keyfile):
        raise ValueError("cloud_certfile: cloud_certfile and cloud_keyfile must both be set or both be empty")
    # An empty secret leaves the MCU link unauthenticated and unencrypted. It
    # is only accepted as an explicit opt-in (allow_insecure_link); the daemon
    # then disables the cloud transport.
    if not cfg.serial_shared_secret and not cfg.allow_insecure_link:
        raise ValueError(
            "serial_shared_secret: an empty secret disables link authentication and encryption; "
            "set allow_insecure_link to permit it"
        )

    cfg.allowed_policy.CopyFrom(create_allowed_policy(cfg.allowed_commands))
    del cfg.allowed_commands[:]
    cfg.allowed_commands.extend(cfg.allowed_policy.entries)

    if not any(getattr(cfg.topic_authorization, name) for name in _TOPIC_AUTH_FIELDS):
        for name in _TOPIC_AUTH_FIELDS:
            setattr(cfg.topic_authorization, name, True)


def get_ssl_context(cfg: config_schema_pb2.RuntimeConfig) -> ssl.SSLContext | None:
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

        context.minimum_version = TLSVersion.TLSv1_2

        if cfg.cloud_tls_insecure:
            # Only hostname matching is relaxed (IP endpoints with DNS SANs); the CA chain is
            # still verified so a forged certificate is rejected.
            context.check_hostname = False
            context.verify_mode = ssl.CERT_REQUIRED

        if cfg.cloud_certfile:
            context.load_cert_chain(cfg.cloud_certfile, cfg.cloud_keyfile)

        return context
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"TLS setup failed: {exc}") from exc


# =============================================================================
# 3. Operational Structures
# =============================================================================


# --- CLOUD Spool Helpers ---


def resolve_cloud_context(
    message: cloud_gateway_pb2.CloudQueuedPublish, context: Any | None
) -> cloud_gateway_pb2.CloudQueuedPublish:
    """Resolve CLOUD request-reply context into the publish message."""
    if context is None:
        return message

    newpb_obj = cloud_gateway_pb2.CloudQueuedPublish()
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
        newpb_obj.user_properties.add(key="bridge-request-topic", value=str(req_topic))

    return newpb_obj


def create_queued_publish(
    topic_name: str,
    payload: bytes,
    content_type: str | None = None,
    message_expiry_interval: int | None = None,
    user_properties: Iterable[tuple[str, str]] = (),
    qos: int = 1,
) -> cloud_gateway_pb2.CloudQueuedPublish:
    """Factory to create a CloudQueuedPublish message. [SIL-2]"""
    msg = cloud_gateway_pb2.CloudQueuedPublish(
        topic_name=topic_name,
        payload=payload,
        content_type=content_type or "",
        qos=qos,
        user_properties=[cloud_gateway_pb2.UserProperty(key=k, value=v) for k, v in user_properties],
    )
    if message_expiry_interval is not None:
        msg.message_expiry_interval = message_expiry_interval
    return msg


# --- Serial Flow Structures ---


class ConsoleQueueTarget(Protocol):
    def put_nowait(self, item: cloud_gateway_pb2.CloudQueuedPublish) -> None: ...
    async def put(self, item: cloud_gateway_pb2.CloudQueuedPublish) -> None: ...


class MemoryQueueAdapter:
    """Adapts an AnyIO MemoryObjectSendStream to queue-compatible API. [SIL-2]"""

    __slots__ = ("_send_stream",)
    _send_stream: MemoryObjectSendStream[cloud_gateway_pb2.CloudQueuedPublish]

    def __init__(self, send_stream: MemoryObjectSendStream[cloud_gateway_pb2.CloudQueuedPublish]) -> None:
        self._send_stream = send_stream

    def put_nowait(self, item: cloud_gateway_pb2.CloudQueuedPublish) -> None:
        try:
            self._send_stream.send_nowait(item)
        except (anyio.BrokenResourceError, anyio.ClosedResourceError) as exc:
            logger.debug("Console stream subscriber disconnected during put_nowait", error=str(exc))
        except anyio.WouldBlock as exc:
            logger.warning("Console stream subscriber buffer full, dropping message", error=str(exc))

    async def put(self, item: cloud_gateway_pb2.CloudQueuedPublish) -> None:
        try:
            await self._send_stream.send(item)
        except (anyio.BrokenResourceError, anyio.ClosedResourceError) as exc:
            logger.debug("Console stream subscriber disconnected during put", error=str(exc))

    def close(self) -> None:
        self._send_stream.close()

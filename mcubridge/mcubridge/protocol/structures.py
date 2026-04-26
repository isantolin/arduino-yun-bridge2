"""MCU Bridge Data Structures and Schemas.

SINGLE SOURCE OF TRUTH for all data structures.
Binary parsing uses stdlib struct; high-level schemas use Msgspec (SIL-2).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from enum import IntEnum
from pathlib import Path
from typing import (
    Annotated,
    Any,
    ClassVar,
    Final,
    TypeVar,
    cast,
)

import msgspec
import msgspec.msgpack
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties
from construct import BitStruct, Flag, Padding, Construct

# [SIL-2] Declarative bitmask definition for MCU capabilities.
# This ensures atomic bit-level parsing/building via Construct's C-backed engine.
# Order matches the protocol specification (bit 0 to bit 15).
FEATURES_STRUCT: Final = cast(
    Construct,
    BitStruct(
        "sd" / Flag,
        "spi" / Flag,
        "i2c" / Flag,
        "big_buffer" / Flag,
        "logic_3v3" / Flag,
        "fpu" / Flag,
        "hw_serial1" / Flag,
        "dac" / Flag,
        "eeprom" / Flag,
        "debug_io" / Flag,
        "debug_frames" / Flag,
        "rle" / Flag,
        "watchdog" / Flag,
        Padding(3),
    ),
)

def _capabilities_to_int(feat_dict: dict[str, Any]) -> int:
    """Convert a capability feature dict to its integer bitmask using Construct."""
    try:
        # Build raw bytes from dict and parse back as 16-bit integer
        from construct import Int16ul

        return int(Int16ul.parse(FEATURES_STRUCT.build(feat_dict)))
    except (ImportError, AttributeError, msgspec.MsgspecError, ValueError):
        return 0

def _int_to_capabilities(val: int) -> dict[str, bool]:
    """Convert an integer bitmask to a capability feature dict using Construct."""
    try:
        from construct import Int16ul

        # Convert integer to bytes then parse via BitStruct
        data = Int16ul.build(int(val))
        res: Any = FEATURES_STRUCT.parse(data)
        # Convert Container to plain dict and remove internal metadata
        return {str(k): bool(v) for k, v in dict(res).items() if not str(k).startswith("_")}
    except (ImportError, AttributeError, msgspec.MsgspecError, ValueError):
        return {}

class TopicRoute(msgspec.Struct, frozen=True):
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
        from .protocol import FileAction, ShellAction, SystemAction

        if not self.segments or "response" in self.segments or "value" in self.segments:
            return None
        val = self.segments[0]
        # Attempt to map to known action enums
        for enum_cls in (FileAction, ShellAction, SystemAction):
            try:
                return enum_cls(val)
            except ValueError:
                continue
        return val

    @property
    def remainder(self) -> tuple[str, ...]:
        return self.segments[1:] if len(self.segments) > 1 else ()

class RLEPayload(msgspec.Struct, frozen=True):
    """Encapsulates RLE-compressed data."""

    data: bytes

    @classmethod
    def from_uncompressed(cls, uncompressed: bytes) -> RLEPayload:
        """Factory to create RLEPayload from raw bytes."""
        from .rle import RLE_TRANSFORM

        return cls(data=RLE_TRANSFORM.build(uncompressed))

    def decode(self) -> bytes:
        """Decompress data using declarative Construct decoder."""
        from .rle import RLE_DECODER

        if not self.data:
            return b""
        from construct.core import ConstructError

        try:
            parsed: Any = RLE_DECODER.parse(self.data)
            return b"".join(parsed)
        except ConstructError as e:
            # Fallback or raise for protocol integrity
            raise ValueError(f"RLE decompression failed: {e}") from e

# =============================================================================
# 2. Security and Policy Structures (msgspec)
# =============================================================================

class AllowedCommandPolicy(msgspec.Struct, frozen=True):
    """Normalised allow-list for shell/process commands."""

    entries: tuple[str, ...]

    @property
    def allow_all(self) -> bool:
        from mcubridge.config.const import ALLOWED_COMMAND_WILDCARD

        return ALLOWED_COMMAND_WILDCARD in self.entries

    def is_allowed(self, command: str) -> bool:
        import fnmatch

        pieces = command.strip().split()
        if not pieces:
            return False
        return self.allow_all or any(fnmatch.fnmatch(pieces[0].lower(), p) for p in self.entries)

    def __contains__(self, item: str) -> bool:
        return item.lower() in self.entries

    def as_tuple(self) -> tuple[str, ...]:
        return self.entries

    @classmethod
    def from_iterable(
        cls,
        entries: Iterable[str],
    ) -> AllowedCommandPolicy:
        """Return a deduplicated, lower-cased and sorted allow-list preserving wildcards."""
        import re

        all_tokens: list[str] = []
        for c in entries:
            if not c:
                continue
            # [SIL-2] Robust splitting by common delimiters (comma, space)
            tokens = re.split(r"[, \s]+", c.strip().lower())
            all_tokens.extend(t for t in tokens if t)

        items: set[str] = set(all_tokens)
        normalised = ("*",) if "*" in items else tuple(sorted(list(items)))
        return cls(entries=normalised)

    @classmethod
    def create_empty(cls) -> AllowedCommandPolicy:
        """Create an empty policy with no allowed commands."""
        return cls(entries=())

class TopicAuthorization(msgspec.Struct, frozen=True):
    """Per-topic allow flags for MQTT-driven actions.

    Optimized for lookup speed using a pre-calculated frozenset of allowed (topic, action) tuples.
    """

    file_read: bool = True
    file_write: bool = True
    file_remove: bool = True
    datastore_get: bool = True
    datastore_put: bool = True
    mailbox_read: bool = True
    mailbox_write: bool = True
    shell_run_async: bool = True
    shell_poll: bool = True
    shell_kill: bool = True
    console_input: bool = True
    digital_write: bool = True
    digital_read: bool = True
    digital_mode: bool = True
    analog_write: bool = True
    analog_read: bool = True
    system_version: bool = True
    system_free_memory: bool = True
    system_bootloader: bool = True
    spi_begin: bool = True
    spi_end: bool = True
    spi_transfer: bool = True
    spi_config: bool = True

    # Cache for allowed permissions (not serialized)
    _allowed_cache: Final[frozenset[tuple[str, str]]] = frozenset()

    def __post_init__(self) -> None:
        """Build the optimized lookup cache."""
        from mcubridge.protocol.protocol import (
            AnalogAction,
            ConsoleAction,
            DatastoreAction,
            DigitalAction,
            FileAction,
            MailboxAction,
            ShellAction,
            SpiAction,
            SystemAction,
        )
        from mcubridge.protocol.topics import Topic

        # Static mapping to avoid recreation in __post_init__
        _TOPIC_AUTH_MAPPING: Final[dict[tuple[str, str], str]] = {
            (Topic.FILE.value, FileAction.READ.value): "file_read",
            (Topic.FILE.value, FileAction.WRITE.value): "file_write",
            (Topic.FILE.value, FileAction.REMOVE.value): "file_remove",
            (Topic.DATASTORE.value, DatastoreAction.GET.value): "datastore_get",
            (Topic.DATASTORE.value, DatastoreAction.PUT.value): "datastore_put",
            (Topic.MAILBOX.value, MailboxAction.READ.value): "mailbox_read",
            (Topic.MAILBOX.value, MailboxAction.WRITE.value): "mailbox_write",
            (Topic.SHELL.value, ShellAction.RUN_ASYNC.value): "shell_run_async",
            (Topic.SHELL.value, ShellAction.POLL.value): "shell_poll",
            (Topic.SHELL.value, ShellAction.KILL.value): "shell_kill",
            (Topic.CONSOLE.value, ConsoleAction.IN.value): "console_input",
            (Topic.DIGITAL.value, DigitalAction.WRITE.value): "digital_write",
            (Topic.DIGITAL.value, DigitalAction.READ.value): "digital_read",
            (Topic.DIGITAL.value, DigitalAction.MODE.value): "digital_mode",
            (Topic.ANALOG.value, AnalogAction.WRITE.value): "analog_write",
            (Topic.ANALOG.value, AnalogAction.READ.value): "analog_read",
            (Topic.SYSTEM.value, SystemAction.VERSION.value): "system_version",
            (Topic.SYSTEM.value, SystemAction.FREE_MEMORY.value): "system_free_memory",
            (Topic.SYSTEM.value, SystemAction.BOOTLOADER.value): "system_bootloader",
            (Topic.SPI.value, SpiAction.BEGIN.value): "spi_begin",
            (Topic.SPI.value, SpiAction.END.value): "spi_end",
            (Topic.SPI.value, SpiAction.TRANSFER.value): "spi_transfer",
            (Topic.SPI.value, SpiAction.CONFIG.value): "spi_config",
        }

        allowed = [k for k, a in _TOPIC_AUTH_MAPPING.items() if getattr(self, a)]
        object.__setattr__(self, "_allowed_cache", frozenset(allowed))

    def allows(self, topic: str, action: str) -> bool:
        """Check if action is allowed on topic. O(1) complexity."""
        return (topic.lower(), action.lower()) in self._allowed_cache

# =============================================================================
# 3. Runtime Configuration Structures (msgspec)
# =============================================================================

class RuntimeConfig(msgspec.Struct, kw_only=True):
    """Strongly typed configuration for the daemon."""

    # Imports moved inside __post_init__ or methods to avoid circularity
    # but we need constants for defaults.
    from mcubridge.config.const import (
        DEFAULT_ALLOW_NON_TMP_PATHS,
        DEFAULT_BRIDGE_HANDSHAKE_INTERVAL,
        DEFAULT_BRIDGE_SUMMARY_INTERVAL,
        DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES,
        DEFAULT_DEBUG,
        DEFAULT_FILE_STORAGE_QUOTA_BYTES,
        DEFAULT_FILE_SYSTEM_ROOT,
        DEFAULT_FILE_WRITE_MAX_BYTES,
        DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT,
        DEFAULT_MAILBOX_QUEUE_LIMIT,
        DEFAULT_METRICS_ENABLED,
        DEFAULT_METRICS_HOST,
        DEFAULT_METRICS_PORT,
        DEFAULT_MQTT_CAFILE,
        DEFAULT_MQTT_HOST,
        DEFAULT_MQTT_PORT,
        DEFAULT_MQTT_QUEUE_LIMIT,
        DEFAULT_MQTT_SPOOL_DIR,
        DEFAULT_MQTT_TLS_INSECURE,
        DEFAULT_PENDING_PIN_REQUESTS,
        DEFAULT_PROCESS_MAX_CONCURRENT,
        DEFAULT_PROCESS_MAX_OUTPUT_BYTES,
        DEFAULT_PROCESS_TIMEOUT,
        DEFAULT_RECONNECT_DELAY,
        DEFAULT_SERIAL_FALLBACK_THRESHOLD,
        DEFAULT_SERIAL_HANDSHAKE_FATAL_FAILURES,
        DEFAULT_SERIAL_HANDSHAKE_MIN_INTERVAL,
        DEFAULT_SERIAL_PORT,
        DEFAULT_SERIAL_RESPONSE_TIMEOUT,
        DEFAULT_SERIAL_RETRY_TIMEOUT,
        DEFAULT_SERIAL_SHARED_SECRET,
        DEFAULT_STATUS_INTERVAL,
        DEFAULT_WATCHDOG_INTERVAL,
        MIN_SERIAL_SHARED_SECRET_LEN,
    )
    from mcubridge.protocol.protocol import (
        DEFAULT_BAUDRATE,
        DEFAULT_RETRY_LIMIT,
        DEFAULT_SAFE_BAUDRATE,
        MQTT_DEFAULT_TOPIC_PREFIX,
    )

    serial_port: str = DEFAULT_SERIAL_PORT
    serial_baud: Annotated[int, msgspec.Meta(ge=300)] = DEFAULT_BAUDRATE
    serial_safe_baud: Annotated[int, msgspec.Meta(ge=300)] = DEFAULT_SAFE_BAUDRATE
    mqtt_host: str = DEFAULT_MQTT_HOST
    mqtt_port: Annotated[int, msgspec.Meta(ge=1, le=65535)] = DEFAULT_MQTT_PORT
    mqtt_user: str | None = None
    mqtt_pass: str | None = None
    mqtt_tls: bool = True
    mqtt_cafile: str | None = DEFAULT_MQTT_CAFILE
    mqtt_certfile: str | None = None
    mqtt_keyfile: str | None = None
    mqtt_topic: str = MQTT_DEFAULT_TOPIC_PREFIX

    # [SIL-2] Accept Any to allow raw strings from UCI/Tests, then coerce in __post_init__
    allowed_commands: Any = ()

    file_system_root: str = DEFAULT_FILE_SYSTEM_ROOT
    process_timeout: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_PROCESS_TIMEOUT

    mqtt_tls_insecure: bool = DEFAULT_MQTT_TLS_INSECURE
    file_write_max_bytes: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_FILE_WRITE_MAX_BYTES
    file_storage_quota_bytes: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_FILE_STORAGE_QUOTA_BYTES

    allowed_policy: AllowedCommandPolicy | None = None

    mqtt_queue_limit: Annotated[int, msgspec.Meta(ge=0)] = DEFAULT_MQTT_QUEUE_LIMIT
    reconnect_delay: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_RECONNECT_DELAY
    status_interval: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_STATUS_INTERVAL
    debug: bool = DEFAULT_DEBUG
    console_queue_limit_bytes: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_CONSOLE_QUEUE_LIMIT_BYTES
    mailbox_queue_limit: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_MAILBOX_QUEUE_LIMIT
    mailbox_queue_bytes_limit: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_MAILBOX_QUEUE_BYTES_LIMIT
    pending_pin_request_limit: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_PENDING_PIN_REQUESTS
    serial_retry_timeout: Annotated[float, msgspec.Meta(ge=0.01, le=30.0)] = DEFAULT_SERIAL_RETRY_TIMEOUT
    serial_response_timeout: Annotated[float, msgspec.Meta(ge=0.02, le=120.0)] = DEFAULT_SERIAL_RESPONSE_TIMEOUT
    serial_retry_attempts: Annotated[int, msgspec.Meta(ge=0)] = DEFAULT_RETRY_LIMIT
    serial_fallback_threshold: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_SERIAL_FALLBACK_THRESHOLD
    serial_handshake_min_interval: Annotated[float, msgspec.Meta(ge=0.0, le=30.0)] = (
        DEFAULT_SERIAL_HANDSHAKE_MIN_INTERVAL
    )
    serial_handshake_fatal_failures: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_SERIAL_HANDSHAKE_FATAL_FAILURES
    mqtt_enabled: bool = True
    watchdog_enabled: bool = True
    watchdog_interval: Annotated[float, msgspec.Meta(ge=0.1, le=60.0)] = DEFAULT_WATCHDOG_INTERVAL
    topic_authorization: TopicAuthorization | None = None

    # [SIL-2] Security: Accept Any to allow raw strings from UCI/Tests,
    # then coerce to bytes in __post_init__ to avoid msgspec base64 errors.
    serial_shared_secret: Any = DEFAULT_SERIAL_SHARED_SECRET

    mqtt_spool_dir: str = DEFAULT_MQTT_SPOOL_DIR
    process_max_output_bytes: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_PROCESS_MAX_OUTPUT_BYTES
    process_max_concurrent: Annotated[int, msgspec.Meta(ge=1)] = DEFAULT_PROCESS_MAX_CONCURRENT
    metrics_enabled: bool = DEFAULT_METRICS_ENABLED
    metrics_host: str = DEFAULT_METRICS_HOST
    metrics_port: Annotated[int, msgspec.Meta(ge=1, le=65535)] = DEFAULT_METRICS_PORT
    bridge_summary_interval: Annotated[float, msgspec.Meta(ge=0.0)] = DEFAULT_BRIDGE_SUMMARY_INTERVAL
    bridge_handshake_interval: Annotated[float, msgspec.Meta(ge=0.0)] = DEFAULT_BRIDGE_HANDSHAKE_INTERVAL
    allow_non_tmp_paths: bool = DEFAULT_ALLOW_NON_TMP_PATHS

    def get_ssl_context(self) -> Any | None:
        """Create an ssl.SSLContext based on the current configuration (SIL-2)."""
        if not self.mqtt_tls:
            return None

        import ssl
        from mcubridge.config.const import MQTT_TLS_MIN_VERSION

        try:
            if self.mqtt_cafile:
                ca_path = Path(self.mqtt_cafile)
                if not ca_path.exists():
                    raise RuntimeError(f"MQTT TLS CA file missing: {self.mqtt_cafile}")
                context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(ca_path))
            else:
                context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)

            context.minimum_version = MQTT_TLS_MIN_VERSION

            if self.mqtt_tls_insecure:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE

            if self.mqtt_certfile or self.mqtt_keyfile:
                if not (self.mqtt_certfile and self.mqtt_keyfile):
                    raise ValueError("Both mqtt_certfile and mqtt_keyfile must be provided for mTLS.")
                context.load_cert_chain(self.mqtt_certfile, self.mqtt_keyfile)

            return context
        except (OSError, ssl.SSLError, ValueError) as exc:
            raise RuntimeError(f"TLS setup failed: {exc}") from exc

    @property
    def tls_enabled(self) -> bool:
        return self.mqtt_tls

    def __post_init__(self) -> None:
        from mcubridge.config.const import (
            DEFAULT_SERIAL_SHARED_SECRET,
            VOLATILE_STORAGE_PATHS,
        )

        # [SIL-2] Semantic Policy Derivation
        self.allowed_policy = AllowedCommandPolicy.from_iterable(self.allowed_commands)
        self.allowed_commands = self.allowed_policy.entries if self.allowed_policy else ()

        if self.topic_authorization is None or isinstance(self.topic_authorization, dict):
            self.topic_authorization = (
                msgspec.convert(self.topic_authorization, TopicAuthorization)
                if self.topic_authorization
                else TopicAuthorization()
            )

        # [SIL-2] Strict Semantic Validations
        if not self.mqtt_topic or not any(filter(None, self.mqtt_topic.split("/"))):
            raise ValueError("mqtt_topic must contain at least one segment")

        if self.serial_response_timeout < self.serial_retry_timeout * 2:
            raise ValueError("serial_response_timeout must be at least 2x serial_retry_timeout")

        if self.watchdog_enabled and self.watchdog_interval < 0.5:
            raise ValueError("watchdog_interval must be >= 0.5s when enabled")

        if not self.serial_shared_secret:
            raise ValueError("serial_shared_secret must be configured")

        if self.serial_shared_secret == b"changeme123":
            raise ValueError("serial_shared_secret placeholder is insecure")

        # Unique symbol check for minimum entropy
        if isinstance(self.serial_shared_secret, bytes):
            unique_symbols = {byte for byte in self.serial_shared_secret}
            if len(unique_symbols) < 4 and self.serial_shared_secret != DEFAULT_SERIAL_SHARED_SECRET:
                raise ValueError("serial_shared_secret must contain at least four distinct bytes")

        # Logic-based cross-field validations
        if self.file_storage_quota_bytes < self.file_write_max_bytes:
            raise ValueError("file_storage_quota_bytes must be greater than or equal to file_write_max_bytes")

        if self.mailbox_queue_bytes_limit < self.mailbox_queue_limit:
            raise ValueError("mailbox_queue_bytes_limit must be greater than or equal to mailbox_queue_limit")

        # [SIL-2] Flash Protection: Spooling must ALWAYS be in volatile RAM.
        if not any(self.mqtt_spool_dir.startswith(p) for p in VOLATILE_STORAGE_PATHS):
            raise ValueError(
                f"FLASH PROTECTION: mqtt_spool_dir ({self.mqtt_spool_dir}) must be in a volatile location (e.g. /tmp)"
            )

        if not self.allow_non_tmp_paths:
            if not any(self.file_system_root.startswith(p) for p in VOLATILE_STORAGE_PATHS):
                raise ValueError(
                    f"FLASH PROTECTION: file_system_root ({self.file_system_root}) must be in a volatile location"
                )

# =============================================================================
# 3. Operational Structures
# =============================================================================

T = TypeVar("T", bound="BaseStruct")

# [SIL-2] Shared encoder/decoder — reuse avoids per-call allocation overhead.
_msgpack_encoder = msgspec.msgpack.Encoder()
_msgpack_decoder = msgspec.msgpack.Decoder()

class BaseStruct(msgspec.Struct, frozen=True, array_like=True):
    """Base class for all serial payload packets.

    Encoded as MsgPack arrays (positional fields) for compact wire format.
    """

# --- Binary Protocol Packets ---

# --- BEGIN GENERATED PACKETS --- DO NOT EDIT (auto-generated from spec.toml)


class VersionResponsePacket(BaseStruct, frozen=True):
    major: Annotated[int, msgspec.Meta(ge=0)]
    minor: Annotated[int, msgspec.Meta(ge=0)]
    patch: Annotated[int, msgspec.Meta(ge=0)]


class FreeMemoryResponsePacket(BaseStruct, frozen=True):
    value: Annotated[int, msgspec.Meta(ge=0)]


class PinModePacket(BaseStruct, frozen=True):
    pin: Annotated[int, msgspec.Meta(ge=0)]
    mode: Annotated[int, msgspec.Meta(ge=0)]


class DigitalWritePacket(BaseStruct, frozen=True):
    pin: Annotated[int, msgspec.Meta(ge=0)]
    value: Annotated[int, msgspec.Meta(ge=0)]


class AnalogWritePacket(BaseStruct, frozen=True):
    pin: Annotated[int, msgspec.Meta(ge=0)]
    value: Annotated[int, msgspec.Meta(ge=0)]


class PinReadPacket(BaseStruct, frozen=True):
    pin: Annotated[int, msgspec.Meta(ge=0)]


class DigitalReadResponsePacket(BaseStruct, frozen=True):
    value: Annotated[int, msgspec.Meta(ge=0)]


class AnalogReadResponsePacket(BaseStruct, frozen=True):
    value: Annotated[int, msgspec.Meta(ge=0)]


class ConsoleWritePacket(BaseStruct, frozen=True):
    data: bytes


class DatastorePutPacket(BaseStruct, frozen=True):
    key: str
    value: bytes


class DatastoreGetPacket(BaseStruct, frozen=True):
    key: str


class DatastoreGetResponsePacket(BaseStruct, frozen=True):
    value: bytes


class MailboxPushPacket(BaseStruct, frozen=True):
    data: bytes


class MailboxProcessedPacket(BaseStruct, frozen=True):
    message_id: Annotated[int, msgspec.Meta(ge=0)]


class MailboxAvailableResponsePacket(BaseStruct, frozen=True):
    count: Annotated[int, msgspec.Meta(ge=0)]


class MailboxReadResponsePacket(BaseStruct, frozen=True):
    content: bytes


class FileWritePacket(BaseStruct, frozen=True):
    path: str
    data: bytes


class FileReadPacket(BaseStruct, frozen=True):
    path: str


class FileRemovePacket(BaseStruct, frozen=True):
    path: str


class FileReadResponsePacket(BaseStruct, frozen=True):
    content: bytes


class ProcessRunAsyncPacket(BaseStruct, frozen=True):
    command: str


class ProcessRunAsyncResponsePacket(BaseStruct, frozen=True):
    pid: Annotated[int, msgspec.Meta(ge=0)]


class ProcessPollPacket(BaseStruct, frozen=True):
    pid: Annotated[int, msgspec.Meta(ge=0)]


class ProcessPollResponsePacket(BaseStruct, frozen=True):
    status: Annotated[int, msgspec.Meta(ge=0)]
    exit_code: Annotated[int, msgspec.Meta(ge=0)]
    stdout_data: bytes
    stderr_data: bytes


class ProcessKillPacket(BaseStruct, frozen=True):
    pid: Annotated[int, msgspec.Meta(ge=0)]


class AckPacket(BaseStruct, frozen=True):
    command_id: Annotated[int, msgspec.Meta(ge=0)]


class HandshakeConfigPacket(BaseStruct, frozen=True):
    ack_timeout_ms: Annotated[int, msgspec.Meta(ge=0)]
    ack_retry_limit: Annotated[int, msgspec.Meta(ge=0)]
    response_timeout_ms: Annotated[int, msgspec.Meta(ge=0)]


class SetBaudratePacket(BaseStruct, frozen=True):
    baudrate: Annotated[int, msgspec.Meta(ge=0)]


class LinkSyncPacket(BaseStruct, frozen=True):
    nonce: bytes
    tag: bytes


class EnterBootloaderPacket(BaseStruct, frozen=True):
    magic: Annotated[int, msgspec.Meta(ge=0)]


class SpiTransferPacket(BaseStruct, frozen=True):
    data: bytes


class SpiTransferResponsePacket(BaseStruct, frozen=True):
    data: bytes


class SpiConfigPacket(BaseStruct, frozen=True):
    bit_order: Annotated[int, msgspec.Meta(ge=0)]
    data_mode: Annotated[int, msgspec.Meta(ge=0)]
    frequency: Annotated[int, msgspec.Meta(ge=0)]


# --- END GENERATED PACKETS ---


class GenericResponsePacket(msgspec.Struct, frozen=True):
    """Generic high-level API response packet."""

    status: str
    message: str | None = None
    data: dict[str, Any] | None = None

# --- Manual Packet Classes (require special handling) ---

class CapabilitiesFeatures(msgspec.Struct, frozen=True):
    """Features bitmask parsed via BitStruct."""

    watchdog: bool
    rle: bool
    debug_frames: bool
    debug_io: bool
    eeprom: bool
    dac: bool
    hw_serial1: bool
    fpu: bool
    logic_3v3: bool
    big_buffer: bool
    i2c: bool
    spi: bool
    sd: bool

class CapabilitiesPacket(BaseStruct, frozen=True):
    ver: Annotated[int, msgspec.Meta(ge=0)]
    arch: Annotated[int, msgspec.Meta(ge=0)]
    dig: Annotated[int, msgspec.Meta(ge=0)]
    ana: Annotated[int, msgspec.Meta(ge=0)]
    feat_mask: int

    @property
    def features(self) -> CapabilitiesFeatures:
        """Expand bitmask into structured features object."""
        feat_dict = _int_to_capabilities(self.feat_mask)
        return msgspec.convert(feat_dict, CapabilitiesFeatures)

    @classmethod
    def from_parts(cls, ver: int, arch: int, dig: int, ana: int, features: CapabilitiesFeatures) -> CapabilitiesPacket:
        """Factory to create packet from expanded features."""
        mask = _capabilities_to_int(msgspec.structs.asdict(features))
        return cls(ver=ver, arch=arch, dig=dig, ana=ana, feat_mask=mask)

# [SIL-2] Payload Schema Map: Centralized registry for all command payloads.
# This eliminates manual if/elif dispatching across components.

# --- Operational Constants ---

MAX_COMMAND_LEN: Final[int] = 512

class PayloadValidationError(ValueError):
    """Raised when an inbound MQTT payload cannot be validated."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

# --- High-Level Structure (Msgspec Only) ---

class ShellCommandPayload(msgspec.Struct, frozen=True):
    """Represents a shell command request coming from MQTT.

    Accepts either plain text or MsgPack: {"command": "..."}.
    """

    command: Annotated[str, msgspec.Meta(min_length=1, max_length=MAX_COMMAND_LEN)]

    @classmethod
    def from_mqtt(cls, payload: bytes) -> ShellCommandPayload:
        """Parse MQTT payload into a validated ShellCommandPayload."""
        if not payload:
            raise PayloadValidationError("Shell command payload is empty")

        # Try msgpack format first
        try:
            result = msgspec.msgpack.decode(payload, type=cls)
            normalized = result.command.strip()
            if not normalized:
                raise PayloadValidationError("Shell command payload is empty")
            return cls(command=normalized)
        except (msgspec.ValidationError, msgspec.DecodeError):
            pass

        # Fallback to plain text command
        text = payload.decode("utf-8", errors="ignore").strip()
        if not text:
            raise PayloadValidationError("Shell command payload is empty")

        if len(text) > MAX_COMMAND_LEN:
            raise PayloadValidationError("Command cannot exceed 512 characters")
        return cls(command=text)

class ShellPidPayload(msgspec.Struct, frozen=True):
    """MQTT payload specifying an async shell PID to operate on."""

    pid: Annotated[int, msgspec.Meta(gt=0, le=65535)]  # UINT16_MAX

    @classmethod
    def from_topic_segment(cls, segment: str) -> ShellPidPayload:
        """Parse a topic segment into a validated ShellPidPayload."""
        try:
            value = int(segment, 10)
            return msgspec.convert({"pid": value}, cls, strict=True)
        except (ValueError, msgspec.ValidationError) as exc:
            raise PayloadValidationError(f"Invalid PID segment: {exc}") from exc

class SerialTimingWindow(msgspec.Struct, frozen=True):
    """Derived serial retry/response windows used by both MCU and MPU."""

    ack_timeout_ms: Annotated[int, msgspec.Meta(ge=10, le=50000)]
    response_timeout_ms: Annotated[int, msgspec.Meta(ge=100, le=50000)]
    retry_limit: Annotated[int, msgspec.Meta(ge=1, le=100)]

    @property
    def ack_timeout_seconds(self) -> float:
        return self.ack_timeout_ms / 1000.0

    @property
    def response_timeout_seconds(self) -> float:
        return self.response_timeout_ms / 1000.0

class MqttPayload(msgspec.Struct, frozen=True):
    topic: str
    payload: bytes
    qos: int = 1
    retain: bool = False
    properties: dict[str, Any] = {}

class PinRequest(msgspec.Struct, frozen=True):
    pin: int
    state: str

class PendingPinRequest(msgspec.Struct):
    """Pending pin read request."""

    pin: int
    reply_context: Any | None = None  # Message | None

class ServiceHealth(msgspec.Struct, frozen=True):
    name: str
    status: str
    restarts: int
    last_failure_unix: float
    last_exception: str | None = None

class SystemStatus(msgspec.Struct, frozen=True):
    cpu_percent: float | None
    memory_total_bytes: int | None
    memory_available_bytes: int | None
    load_avg_1m: float | None
    uptime_seconds: float

# --- MQTT Spool Structures ---

class QOSLevel(IntEnum):
    """MQTT Quality-of-Service levels."""

    QOS_0 = 0
    QOS_1 = 1
    QOS_2 = 2

UserProperty = tuple[str, str]

class QueuedPublish(msgspec.Struct, frozen=True):
    """Serializable MQTT publish packet used by the durable spool."""

    topic_name: str
    payload: bytes
    qos: Annotated[int, msgspec.Meta(ge=0, le=2)] = 0
    retain: bool = False
    content_type: str | None = None
    payload_format_indicator: int | None = None
    message_expiry_interval: int | None = None
    response_topic: str | None = None
    correlation_data: bytes | None = None
    user_properties: tuple[UserProperty, ...] = ()
    subscription_identifier: tuple[int, ...] | None = None

    def to_paho_properties(self) -> Properties | None:
        """Convert fields to native Paho MQTT v5 properties."""
        if not any(
            (
                self.content_type,
                self.payload_format_indicator is not None,
                self.message_expiry_interval is not None,
                self.response_topic,
                self.correlation_data is not None,
                self.user_properties,
            )
        ):
            return None

        props = Properties(PacketTypes.PUBLISH)
        if self.content_type:
            props.ContentType = self.content_type
        if self.payload_format_indicator is not None:
            props.PayloadFormatIndicator = self.payload_format_indicator
        if self.message_expiry_interval is not None:
            props.MessageExpiryInterval = self.message_expiry_interval
        if self.response_topic:
            props.ResponseTopic = self.response_topic
        if self.correlation_data:
            props.CorrelationData = self.correlation_data
        if self.user_properties:
            props.UserProperty = list(self.user_properties)

        return props

# --- Process Service Structures ---

class ProcessOutputBatch(msgspec.Struct):
    """Structured payload describing PROCESS_POLL results."""

    status_byte: Annotated[int, msgspec.Meta(ge=0, le=255)]
    exit_code: Annotated[int, msgspec.Meta(ge=0, le=255)]
    stdout_chunk: bytes
    stderr_chunk: bytes
    finished: bool
    stdout_truncated: bool
    stderr_truncated: bool

# --- Serial Flow Structures ---

class PendingCommand(msgspec.Struct):
    """Book-keeping for a tracked command in flight."""

    command_id: int
    expected_resp_ids: set[int] = msgspec.field(default_factory=lambda: set[int]())  # noqa: PLW0108
    completion: asyncio.Event = msgspec.field(default_factory=asyncio.Event)
    attempts: int = 0
    success: bool | None = None
    failure_status: int | None = None
    ack_received: bool = False
    reply_topic: str | None = None
    correlation_data: bytes | None = None

    def mark_success(self) -> None:
        self.success = True
        if not self.completion.is_set():
            self.completion.set()

    def mark_failure(self, status: int | None) -> None:
        self.success = False
        self.failure_status = status
        if not self.completion.is_set():
            self.completion.set()

# --- Status Structures ---

_SnapshotT = TypeVar("_SnapshotT", bound=msgspec.Struct)

class BaseStats(msgspec.Struct):
    """Base for statistics containers providing standard dict conversion.

    Subclasses that define ``SNAPSHOT_TYPE`` get a generic ``as_snapshot()``
    that converts all fields into the frozen snapshot class via msgspec.
    """

    SNAPSHOT_TYPE: ClassVar[type | None] = None

    def as_dict(self) -> dict[str, Any]:
        """Export internal state as a dictionary."""
        return msgspec.structs.asdict(self)

    def as_snapshot(self) -> msgspec.Struct:
        """Convert mutable stats to a frozen snapshot struct."""
        snap_cls = self.__class__.SNAPSHOT_TYPE
        if snap_cls is None:
            raise NotImplementedError(f"{self.__class__.__name__} has no SNAPSHOT_TYPE")
        return cast(msgspec.Struct, msgspec.convert(msgspec.structs.asdict(self), snap_cls))

class SupervisorSnapshot(msgspec.Struct):
    restarts: Annotated[int, msgspec.Meta(ge=0)]
    last_failure_unix: float
    last_exception: str | None
    backoff_seconds: Annotated[float, msgspec.Meta(ge=0.0)]
    fatal: bool

class SupervisorStats(BaseStats):
    """Task supervisor statistics."""

    SNAPSHOT_TYPE: ClassVar[type | None] = SupervisorSnapshot

    restarts: int = 0
    last_failure_unix: float = 0.0
    last_exception: str | None = None
    backoff_seconds: float = 0.0
    fatal: bool = False

    def as_snapshot(self) -> SupervisorSnapshot:
        return cast(SupervisorSnapshot, super().as_snapshot())

class McuCapabilities(msgspec.Struct):
    """Hardware capabilities reported by the MCU."""

    protocol_version: int = 0
    board_arch: int = 0
    num_digital_pins: int = 0
    num_analog_inputs: int = 0
    features: CapabilitiesFeatures | None = None

    @property
    def arch_name(self) -> str:
        from .protocol import ARCHITECTURE_DISPLAY_NAMES

        return ARCHITECTURE_DISPLAY_NAMES.get(self.board_arch, f"Unknown (0x{self.board_arch:02X})")

class SerialThroughputStats(BaseStats):
    """Serial link throughput counters."""

    bytes_sent: int = 0
    bytes_received: int = 0
    frames_sent: int = 0
    frames_received: int = 0
    last_tx_unix: float = 0.0
    last_rx_unix: float = 0.0

    def record_tx(self, nbytes: int) -> None:
        self.bytes_sent += nbytes
        self.frames_sent += 1
        self.last_tx_unix = time.time()

    def record_rx(self, nbytes: int) -> None:
        self.bytes_received += nbytes
        self.frames_received += 1
        self.last_rx_unix = time.time()

# [EXTENDED METRICS] Latency histogram bucket boundaries in milliseconds
LATENCY_BUCKETS_MS: tuple[float, ...] = (
    5.0,
    10.0,
    25.0,
    50.0,
    100.0,
    250.0,
    500.0,
    1000.0,
    2500.0,
)

class SerialLatencyStats(msgspec.Struct):
    """RPC command latency histogram."""

    bucket_counts: list[int] = msgspec.field(default_factory=lambda: [0] * len(LATENCY_BUCKETS_MS))
    overflow_count: int = 0
    total_observations: int = 0
    total_latency_ms: float = 0.0
    min_latency_ms: float = float("inf")
    max_latency_ms: float = 0.0
    _summary: Any | None = None  # Prometheus Summary

    def initialize_prometheus(self, registry: Any | None = None) -> None:
        from prometheus_client import Summary

        self._summary = Summary(
            "mcubridge_rpc_latency_seconds",
            "RPC command round-trip latency",
            registry=registry,
        )

    def record(self, latency_ms: float) -> None:
        self.total_observations += 1
        self.total_latency_ms += latency_ms
        if latency_ms < self.min_latency_ms:
            self.min_latency_ms = latency_ms
        if latency_ms > self.max_latency_ms:
            self.max_latency_ms = latency_ms

        for i, bucket in enumerate(LATENCY_BUCKETS_MS):
            if latency_ms <= bucket:
                self.bucket_counts[i] += 1
        if latency_ms > LATENCY_BUCKETS_MS[-1]:
            self.overflow_count += 1

        if self._summary is not None:
            self._summary.observe(latency_ms / 1000.0)

    def as_dict(self) -> dict[str, Any]:
        avg = self.total_latency_ms / self.total_observations if self.total_observations > 0 else 0.0
        return {
            "buckets": {f"le_{int(b)}ms": self.bucket_counts[i] for i, b in enumerate(LATENCY_BUCKETS_MS)},
            "overflow": self.overflow_count,
            "count": self.total_observations,
            "sum_ms": self.total_latency_ms,
            "avg_ms": avg,
            "min_ms": self.min_latency_ms if self.total_observations > 0 else 0.0,
            "max_ms": self.max_latency_ms,
        }

class McuVersion(msgspec.Struct):
    major: Annotated[int, msgspec.Meta(ge=0)]
    minor: Annotated[int, msgspec.Meta(ge=0)]
    patch: Annotated[int, msgspec.Meta(ge=0)] = 0

class SerialPipelineSnapshot(msgspec.Struct, frozen=True, kw_only=True):
    inflight: dict[str, Any] | None = None
    last_completion: dict[str, Any] | None = None

class SerialLinkSnapshot(msgspec.Struct, frozen=True, kw_only=True):
    connected: bool = False
    writer_attached: bool = False
    synchronised: bool = False

class HandshakeSnapshot(msgspec.Struct, frozen=True, kw_only=True):
    synchronised: bool = False
    attempts: Annotated[int, msgspec.Meta(ge=0)] = 0
    successes: Annotated[int, msgspec.Meta(ge=0)] = 0
    failures: Annotated[int, msgspec.Meta(ge=0)] = 0
    failure_streak: Annotated[int, msgspec.Meta(ge=0)] = 0
    last_error: str | None = None
    last_unix: Annotated[float, msgspec.Meta(ge=0.0)] = 0.0
    last_duration: float = 0.0
    backoff_until: Annotated[float, msgspec.Meta(ge=0.0)] = 0.0
    rate_limit_until: Annotated[float, msgspec.Meta(ge=0.0)] = 0.0
    fatal_count: Annotated[int, msgspec.Meta(ge=0)] = 0
    fatal_reason: str | None = None
    fatal_detail: str | None = None
    fatal_unix: Annotated[float, msgspec.Meta(ge=0.0)] = 0.0
    pending_nonce: bool = False
    nonce_length: Annotated[int, msgspec.Meta(ge=0)] = 0

class BridgeSnapshot(msgspec.Struct, frozen=True, kw_only=True):
    serial_link: SerialLinkSnapshot
    handshake: HandshakeSnapshot
    serial_pipeline: SerialPipelineSnapshot
    serial_flow: SerialFlowSnapshot
    mcu_version: McuVersion | None = None
    capabilities: dict[str, Any] | None = None

class SerialFlowSnapshot(msgspec.Struct):
    """Serial flow control statistics snapshot."""

    commands_sent: Annotated[int, msgspec.Meta(ge=0)]
    commands_acked: Annotated[int, msgspec.Meta(ge=0)]
    retries: Annotated[int, msgspec.Meta(ge=0)]
    failures: Annotated[int, msgspec.Meta(ge=0)]
    last_event_unix: float

class SerialFlowStats(BaseStats):
    """Serial flow control statistics (Mutable)."""

    SNAPSHOT_TYPE: ClassVar[type | None] = SerialFlowSnapshot

    commands_sent: int = 0
    commands_acked: int = 0
    retries: int = 0
    failures: int = 0
    last_event_unix: float = 0.0

    def as_snapshot(self) -> SerialFlowSnapshot:
        return cast(SerialFlowSnapshot, super().as_snapshot())

class ProcessStats(msgspec.Struct):
    """Resource usage statistics for a single process."""

    name: str
    cpu_percent: Annotated[float, msgspec.Meta(ge=0.0)]
    memory_rss_bytes: Annotated[int, msgspec.Meta(ge=0)]

class BridgeStatus(msgspec.Struct, kw_only=True):
    """Root structure for the daemon status file."""

    # Serial Link
    serial_connected: bool
    serial_flow: SerialFlowSnapshot
    link_synchronised: bool
    handshake_attempts: Annotated[int, msgspec.Meta(ge=0)]
    handshake_successes: Annotated[int, msgspec.Meta(ge=0)]
    handshake_failures: Annotated[int, msgspec.Meta(ge=0)]
    handshake_last_error: str | None
    handshake_last_unix: float

    # MQTT
    mqtt_queue_size: Annotated[int, msgspec.Meta(ge=0)]
    mqtt_queue_limit: Annotated[int, msgspec.Meta(ge=0)]
    mqtt_messages_dropped: Annotated[int, msgspec.Meta(ge=0)]
    mqtt_drop_counts: dict[str, int]

    # Spool
    mqtt_spooled_messages: Annotated[int, msgspec.Meta(ge=0)]
    mqtt_spooled_replayed: Annotated[int, msgspec.Meta(ge=0)]
    mqtt_spool_errors: Annotated[int, msgspec.Meta(ge=0)]
    mqtt_spool_degraded: bool


    mqtt_spool_pending: Annotated[int, msgspec.Meta(ge=0)]

    # Storage
    file_storage_root: str
    file_storage_bytes_used: Annotated[int, msgspec.Meta(ge=0)]
    file_storage_quota_bytes: Annotated[int, msgspec.Meta(ge=0)]
    file_write_max_bytes: Annotated[int, msgspec.Meta(ge=0)]
    file_write_limit_rejections: Annotated[int, msgspec.Meta(ge=0)]
    file_storage_limit_rejections: Annotated[int, msgspec.Meta(ge=0)]

    # Queues
    datastore_keys: list[str]
    mailbox_size: Annotated[int, msgspec.Meta(ge=0)]
    mailbox_bytes: Annotated[int, msgspec.Meta(ge=0)]
    mailbox_dropped_messages: Annotated[int, msgspec.Meta(ge=0)]
    mailbox_truncated_messages: Annotated[int, msgspec.Meta(ge=0)]
    mailbox_incoming_dropped_messages: Annotated[int, msgspec.Meta(ge=0)]
    mailbox_incoming_truncated_messages: Annotated[int, msgspec.Meta(ge=0)]
    console_queue_size: Annotated[int, msgspec.Meta(ge=0)]
    console_queue_bytes: Annotated[int, msgspec.Meta(ge=0)]
    console_dropped_chunks: Annotated[int, msgspec.Meta(ge=0)]
    console_truncated_chunks: Annotated[int, msgspec.Meta(ge=0)]

    # System
    mcu_paused: bool
    mcu_version: McuVersion | None
    watchdog_enabled: bool
    watchdog_interval: float
    watchdog_beats: Annotated[int, msgspec.Meta(ge=0)]
    watchdog_last_beat: float
    running_processes: list[str]
    allowed_commands: list[str]
    config_source: str
    process_stats: dict[str, ProcessStats] = msgspec.field(default_factory=lambda: cast(dict[str, ProcessStats], {}))

    # Snapshots
    bridge: BridgeSnapshot
    supervisors: dict[str, SupervisorSnapshot]
    heartbeat_unix: float

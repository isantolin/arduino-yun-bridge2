"""Protocol data structures and snapshots for MCU Bridge v2."""

from __future__ import annotations

import asyncio
import struct
import time
from typing import Any, Final, TypedDict

import msgspec

# --- Common Constants & Structs ---

class BinaryStruct:
    """Helper for binary parsing/building."""
    def __init__(self, fmt: str) -> None:
        self._struct = struct.Struct(fmt)
        self.size = self._struct.size

    def parse(self, data: bytes) -> tuple[Any, ...]:
        return self._struct.unpack(data[:self._struct.size])

    def build(self, *args: Any) -> bytes:
        return self._struct.pack(*args)

UINT16_STRUCT = BinaryStruct(">H")
FRAME_STRUCT = BinaryStruct(">BHH")
NONCE_COUNTER_STRUCT = BinaryStruct(">Q")

MIN_FRAME_SIZE: Final[int] = 9


class McuVersion(msgspec.Struct):
    major: int = 0
    minor: int = 0


class McuCapabilities(msgspec.Struct):
    protocol_version: int = 0
    board_arch: int = 0
    num_digital_pins: int = 0
    num_analog_inputs: int = 0


class QueueEvent(msgspec.Struct):
    accepted: bool = False
    truncated_bytes: int = 0
    dropped_chunks: int = 0
    dropped_bytes: int = 0


# --- Snapshots (Read-Only) ---

class SerialLinkSnapshot(msgspec.Struct):
    serial_connected: bool = False
    link_synchronised: bool = False
    handshake_attempts: int = 0
    handshake_successes: int = 0
    handshake_failures: int = 0
    handshake_last_error: str | None = None
    handshake_last_unix: float = 0.0


class HandshakeSnapshot(msgspec.Struct):
    nonce: str = ""
    tag_verified: bool = False


class SerialPipelineSnapshot(msgspec.Struct):
    tx_queue_size: int = 0
    rx_pending_acks: int = 0


class SerialFlowSnapshot(msgspec.Struct):
    commands_sent: int = 0
    commands_acked: int = 0
    retries: int = 0
    failures: int = 0
    last_event_unix: float = 0.0


class BridgeSnapshot(msgspec.Struct, frozen=True, kw_only=True):
    serial_link: SerialLinkSnapshot = msgspec.field(default_factory=SerialLinkSnapshot)
    handshake: HandshakeSnapshot = msgspec.field(default_factory=HandshakeSnapshot)
    serial_pipeline: SerialPipelineSnapshot = msgspec.field(default_factory=SerialPipelineSnapshot)
    serial_flow: SerialFlowSnapshot = msgspec.field(default_factory=SerialFlowSnapshot)
    mcu_version: McuVersion | None = None
    capabilities: dict[str, Any] | None = None


# --- Operational Data ---

class SpoolRecord(TypedDict):
    topic: str
    payload_base64: str
    qos: int
    retain: bool
    timestamp: float


class QueuedPublish(msgspec.Struct):
    topic: str
    payload: bytes
    qos: int = 1
    retain: bool = False
    properties: Any = None
    content_type: str | None = None
    payload_format_indicator: int | None = None
    message_expiry_interval: int | None = None
    response_topic: str | None = None
    correlation_data: bytes | None = None
    user_properties: list[tuple[str, str]] | None = None

    def to_record(self) -> SpoolRecord:
        import base64
        return {
            "topic": self.topic,
            "payload_base64": base64.b64encode(self.payload).decode("ascii"),
            "qos": self.qos,
            "retain": self.retain,
            "timestamp": time.time(),
        }

    @classmethod
    def from_record(cls, record: SpoolRecord) -> "QueuedPublish":
        import base64
        return cls(
            topic=record["topic"],
            payload=base64.b64decode(record["payload_base64"]),
            qos=record["qos"],
            retain=record["retain"],
        )


class SetBaudratePacket(msgspec.Struct):
    baudrate: int


class AckPacket(msgspec.Struct):
    command_id: int


class PendingCommand:
    def __init__(self, command_id: int, expected_resp_ids: set[int] | None = None) -> None:
        self.command_id = command_id
        self.expected_resp_ids = expected_resp_ids or set()
        self.attempts = 0
        self.ack_received = False
        self.success: bool | None = None
        self.failure_status: int | None = None
        self.completion = asyncio.Event()

    def mark_success(self) -> None:
        self.success = True
        self.completion.set()

    def mark_failure(self, status: int | None) -> None:
        self.success = False
        self.failure_status = status
        self.completion.set()


# --- Payload Packets ---

class VersionResponsePacket(msgspec.Struct):
    major: int
    minor: int

class FreeMemoryResponsePacket(msgspec.Struct):
    value: int

class PinModePacket(msgspec.Struct):
    pin: int
    mode: int

class DigitalWritePacket(msgspec.Struct):
    pin: int
    value: int

class AnalogWritePacket(msgspec.Struct):
    pin: int
    value: int

class PinReadPacket(msgspec.Struct):
    pin: int

class DigitalReadResponsePacket(msgspec.Struct):
    value: int

class AnalogReadResponsePacket(msgspec.Struct):
    value: int

class ProcessKillPacket(msgspec.Struct):
    pid: int

class ProcessPollPacket(msgspec.Struct):
    pid: int

class ProcessRunAsyncPacket(msgspec.Struct):
    command: str

class ProcessRunAsyncResponsePacket(msgspec.Struct):
    pid: int

class ProcessOutputBatch(msgspec.Struct):
    pid: int
    stdout: bytes
    stderr: bytes
    is_finished: bool
    exit_code: int | None

class CapabilitiesPacket(msgspec.Struct):
    protocol_version: int
    board_arch: int
    num_digital_pins: int
    num_analog_inputs: int
    features: int

class HandshakeConfigPacket(msgspec.Struct):
    ack_timeout_ms: int
    ack_retry_limit: int
    response_timeout_ms: int


PROCESS_STATE_FINISHED = "FINISHED"


# --- Statistics ---

class SerialFlowStats:
    def __init__(self) -> None:
        self.commands_sent: int = 0
        self.commands_acked: int = 0
        self.retries: int = 0
        self.failures: int = 0
        self.last_event_unix: float = 0.0

    def as_snapshot(self) -> SerialFlowSnapshot:
        return SerialFlowSnapshot(
            commands_sent=self.commands_sent,
            commands_acked=self.commands_acked,
            retries=self.retries,
            failures=self.failures,
            last_event_unix=self.last_event_unix,
        )


class SerialThroughputStats:
    def __init__(self) -> None:
        self.tx_bps: float = 0.0
        self.rx_bps: float = 0.0


class SerialLatencyStats:
    def __init__(self) -> None:
        self.p50_seconds: float = 0.0
        self.p90_seconds: float = 0.0
        self.p99_seconds: float = 0.0


class SupervisorStats:
    def __init__(self) -> None:
        self.restarts: int = 0
        self.last_failure_unix: float = 0.0
        self.last_failure_reason: str | None = None

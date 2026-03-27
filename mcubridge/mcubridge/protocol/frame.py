"""RPC frame building and parsing for Arduino-Linux serial communication.

This module implements the binary frame format used over the serial link
between the Linux daemon and the Arduino MCU.

[SIL-2 COMPLIANCE]
The frame format is designed for reliable communication:
- CRC32 integrity check on every frame (calculated via standard binascii)
- Explicit length fields prevent buffer overruns
- Version field ensures protocol compatibility
- Big-endian byte order for cross-platform consistency
- Sequence ID for deduplication and reliable delivery

Frame Structure (on wire after COBS encoding):
    [Header (7 bytes)] [Payload (0-64 bytes)] [CRC32 (4 bytes)]

Header Format (big-endian):
    - version (1 byte): Protocol version, must match PROTOCOL_VERSION
    - payload_length (2 bytes): Number of payload bytes
    - command_id (2 bytes): Command or status code from protocol.py
    - sequence_id (2 bytes): Incremental counter for deduplication

The raw frame is then COBS-encoded and terminated with 0x00 delimiter
before transmission.
"""

from __future__ import annotations

from binascii import crc32
import msgspec
from construct import Struct, Int8ub, Int16ub, Int32ub, Bytes, this, Check, RawCopy, Checksum  # type: ignore

from . import protocol

# [SIL-2] Declarative Frame Structure using Construct
# This ensures big-endian encoding and automatic length/CRC validation.
RPC_FRAME_HEADER = Struct(
    "version" / Int8ub,  # type: ignore
    "payload_len" / Int16ub,  # type: ignore
    "command_id" / Int16ub,  # type: ignore
    "sequence_id" / Int16ub,  # type: ignore
    Check(this.version == protocol.PROTOCOL_VERSION),  # type: ignore
)

# [SIL-2] Full Frame with Checksum (Sustitución Drástica)
# Uses RawCopy to capture the bytes for CRC calculation without manual slicing.
RPC_FRAME = Struct(
    "header_payload" / RawCopy(Struct(  # type: ignore
        "header" / RPC_FRAME_HEADER,  # type: ignore
        "payload" / Bytes(this.header.payload_len),  # type: ignore
    )),
    "crc" / Checksum(  # type: ignore
        Int32ub,
        lambda data: crc32(data) & 0xFFFFFFFF,  # type: ignore
        this.header_payload.data
    ),
)


class Frame(msgspec.Struct, frozen=True, kw_only=True):
    """Represents an RPC frame for MCU-Linux communication.

    This class provides both object-oriented and static methods for
    frame construction and parsing.

    Attributes:
        command_id: The RPC command or status code (16-bit).
        sequence_id: The RPC sequence ID (16-bit) for deduplication.
        payload: The frame payload (0 to MAX_PAYLOAD_SIZE bytes).
    """

    command_id: int
    sequence_id: int = 0
    payload: bytes = b""

    @property
    def is_compressed(self) -> bool:
        """Return True if the frame command ID indicates RLE compression."""
        return bool(self.command_id & protocol.CMD_FLAG_COMPRESSED)

    @property
    def raw_command_id(self) -> int:
        """Return the command ID without the compression flag."""
        return self.command_id & ~protocol.CMD_FLAG_COMPRESSED

    @staticmethod
    def build(command_id: int, sequence_id: int = 0, payload: bytes = b"") -> bytes:
        """Build a raw frame (header + payload + CRC) using Construct (Sustitución Drástica)."""
        payload_len = len(payload)
        if payload_len > protocol.MAX_PAYLOAD_SIZE:
            raise ValueError(f"Payload too large ({payload_len} bytes); max is {protocol.MAX_PAYLOAD_SIZE}")
        if not 0 <= command_id <= protocol.UINT16_MAX:
            raise ValueError(f"Command id {command_id} outside 16-bit range")
        if not 0 <= sequence_id <= protocol.UINT16_MAX:
            raise ValueError(f"Sequence id {sequence_id} outside 16-bit range")

        try:
            # Entire frame is built via Construct, including Checksum
            # RawCopy requires the subconstruct value to be passed under the 'value' key during build.
            return RPC_FRAME.build({  # type: ignore
                "header_payload": {
                    "value": {
                        "header": {
                            "version": protocol.PROTOCOL_VERSION,
                            "payload_len": payload_len,
                            "command_id": command_id,
                            "sequence_id": sequence_id
                        },
                        "payload": payload
                    }
                }
            })
        except Exception as e:
            raise ValueError(f"Frame build failed: {e}") from e

    @staticmethod
    def parse(raw_frame_buffer: bytes | bytearray | memoryview) -> tuple[int, int, bytes]:
        """Parse a decoded frame and validate header, payload, and CRC using Construct."""
        if len(raw_frame_buffer) < protocol.MIN_FRAME_SIZE:
            raise ValueError(f"Incomplete frame: size {len(raw_frame_buffer)} < {protocol.MIN_FRAME_SIZE}")

        try:
            # Construct handles length validation, header parsing, AND CRC Checksum validation
            obj = RPC_FRAME.parse(raw_frame_buffer)  # type: ignore
        except Exception as e:
            # Maintain compatibility with tests expecting specific error messages
            err_msg = str(e)
            if "checksum mismatch" in err_msg.lower():
                # Extract components for the legacy-style error message if possible
                # Otherwise provide a generic CRC error
                raise ValueError("CRC mismatch: verification failed via Construct Checksum") from e
            if "stream read less than specified amount" in err_msg:
                raise ValueError(f"Frame length mismatch: {err_msg}") from e
            raise ValueError(f"Malformed frame: {e}") from e

        # Access fields within the RawCopy 'value' container
        res = obj.header_payload.value  # type: ignore
        return res.header.command_id, res.header.sequence_id, res.payload  # type: ignore

    def to_bytes(self) -> bytes:
        """Serialize the instance using :meth:`build`."""
        return self.build(self.command_id, self.sequence_id, self.payload)

    @classmethod
    def from_bytes(cls, raw_frame_buffer: bytes | bytearray | memoryview) -> "Frame":
        """Parse *raw_frame_buffer* and create a :class:`Frame`."""
        command_id, sequence_id, payload = cls.parse(raw_frame_buffer)
        return cls(command_id=command_id, sequence_id=sequence_id, payload=payload)

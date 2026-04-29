"""RPC frame building and parsing for Arduino-Linux serial communication.

This module implements the binary frame format used over the serial link
between the Linux daemon and the Arduino MCU.

[SIL-2 COMPLIANCE]
The frame format is strictly defined to ensure:
- Deterministic memory layout.
- Explicit CRC32 validation.
- Zero boilerplate compatibility layers.
"""

from __future__ import annotations

from binascii import crc32
from construct import (
    Bytes,
    Int8ub,
    Int16ub,
    Int32ub,
    Struct,
    this,
    Checksum,
    RawCopy,
    ChecksumError,
)
from construct.core import ConstructError

from . import protocol

T = TypeVar("T")

# [SIL-2] Declarative Frame Header Structure
RPC_FRAME_HEADER = Struct(
    "version" / Int8ub,
    "payload_len" / Int16ub,
    "command_id" / Int16ub,
    "sequence_id" / Int16ub,
)

# [SIL-2] Full Frame Structure (Flat)
RPC_FRAME_BODY = Struct(
    "header" / RPC_FRAME_HEADER,
    "payload" / Bytes(this.header.payload_len),
)

# [SIL-2] Full Frame using native Checksum (Zero-Boilerplate)
RPC_FRAME = Struct(
    "body" / RawCopy(RPC_FRAME_BODY),
    "crc" / Checksum(
        Int32ub,
        lambda data: crc32(bytes(data)) & 0xFFFFFFFF,
        this.body.data
    )
)
class Frame(msgspec.Struct, frozen=True):
    """Represents an RPC frame for MCU-Linux communication."""

    command_id: int | protocol.Command | protocol.Status
    sequence_id: int
    payload: bytes = b""

    def __iter__(self):
        """Allow unpacking: cmd, seq, payload = frame."""
        yield self.command_id
        yield self.sequence_id
        yield self.payload

    @property
    def is_compressed(self) -> bool:
        """Check if the frame payload is compressed (bit 15)."""
        return bool(int(self.command_id) & protocol.CMD_FLAG_COMPRESSED)

    @property
    def raw_command_id(self) -> int:
        """Get the raw 15-bit command ID without the compression flag."""
        return (
            int(self.command_id) & ~protocol.CMD_FLAG_COMPRESSED & protocol.UINT16_MAX
        )

    def build(self) -> bytes:
        """Build the binary frame representation with explicit RLE compression."""
        from . import rle

        if len(self.payload) > protocol.MAX_PAYLOAD_SIZE:
            raise ValueError(
                f"Payload too large: {len(self.payload)} > {protocol.MAX_PAYLOAD_SIZE}"
            )

        cmd_id = int(self.command_id)
        working_payload = self.payload

        # [SIL-2] Explicit Compression Logic
        if working_payload and rle.should_compress(working_payload):
            compressed = rle.RLE_TRANSFORM.build(working_payload)
            if len(compressed) < len(working_payload):
                working_payload = compressed
                cmd_id |= protocol.CMD_FLAG_COMPRESSED

        try:
            return RPC_FRAME.build(
                {
                    "body": {
                        "value": {
                            "header": {
                                "version": protocol.PROTOCOL_VERSION,
                                "payload_len": len(working_payload),
                                "command_id": cmd_id,
                                "sequence_id": self.sequence_id,
                            },
                            "payload": working_payload,
                        }
                    }
                }
            )
        except (ConstructError, ValueError, TypeError) as e:
            raise ValueError(f"Failed to build frame: {e}") from e

    @classmethod
    def parse(cls, raw_frame_buffer: bytes | bytearray | memoryview) -> "Frame":
        """Parse *raw_frame_buffer* and create a :class:`Frame` with explicit RLE decompression."""
        try:
            if len(raw_frame_buffer) < 11:  # Header(7) + CRC(4)
                raise ValueError("Frame too short")

            obj = RPC_FRAME.parse(raw_frame_buffer)
            body = obj.body.value
            cmd_id = int(body.header.command_id)
            payload = body.payload

            # [SIL-2] Explicit Decompression Logic
            if cmd_id & protocol.CMD_FLAG_COMPRESSED:
                from .rle import RLE_TRANSFORM

                payload = RLE_TRANSFORM.parse(payload)
                cmd_id &= ~protocol.CMD_FLAG_COMPRESSED

            return cls(
                command_id=cmd_id,
                sequence_id=int(body.header.sequence_id),
                payload=payload,
            )
        except ChecksumError as e:
            raise ValueError(f"CRC mismatch: {e}") from e
        except (ConstructError, ValueError, TypeError, AttributeError, KeyError) as e:
            raise ValueError(f"Incomplete or malformed frame: {e}") from e

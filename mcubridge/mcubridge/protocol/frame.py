"""RPC frame building and parsing for Arduino-Linux serial communication.

This module implements the binary frame format used over the serial link
between the Linux daemon and the Arduino MCU.

[SIL-2 COMPLIANCE]
The frame format is strictly defined using the 'construct' library to ensure:
- Deterministic memory layout.
- Automatic CRC32 validation.
- Zero manual bit-shifting or pointer arithmetic.
"""

from __future__ import annotations

from binascii import crc32
from typing import Any, TypeVar, cast
import msgspec
from construct import (
    Adapter,
    BitStruct,
    BitsInteger,
    Bytes,
    Check,
    Checksum,
    Construct,
    Enum,
    Flag,
    Int8ub,
    Int16ub,
    Int32ub,
    RawCopy,
    Struct,
    this,
)
from construct.core import ConstructError

from . import protocol

T = TypeVar("T")


def _check_version(ctx: Any) -> bool:
    """SIL-2: Strictly validate protocol version in frame header."""
    return int(getattr(ctx, "version", 0)) == protocol.PROTOCOL_VERSION


def _calculate_crc32(data: Any) -> int:
    """SIL-2: Ensure 32-bit unsigned CRC calculation."""
    return crc32(cast(bytes, data)) & 0xFFFFFFFF


# [SIL-2] Declarative Command ID Codec: Handles Bit 15 (Compression Flag)
COMMAND_ID_CODEC: Construct = BitStruct(
    "is_compressed" / Flag,
    "raw_id" / BitsInteger(15),
)

# [SIL-2] Declarative Frame Structure using Construct
# This ensures big-endian encoding and automatic length/CRC validation.
RPC_FRAME_HEADER: Construct = Struct(
    "version" / Int8ub,
    "payload_len" / Int16ub,
    "command_id" / Enum(Int16ub, protocol.Command, protocol.Status),
    "sequence_id" / Int16ub,
    "version_check" / Check(_check_version),
)


class FrameAdapter(Adapter):
    """Transparently handles RLE compression encoding and decoding within Construct."""

    def _decode(self, obj: Any, context: Any, path: Any) -> Any:
        if int(obj.header.command_id) & protocol.CMD_FLAG_COMPRESSED:
            from .rle import RLE_TRANSFORM

            obj.payload = RLE_TRANSFORM.parse(obj.payload)
            obj.header.command_id = int(obj.header.command_id) & ~protocol.CMD_FLAG_COMPRESSED
            obj.header.payload_len = len(obj.payload)
        return obj

    def _encode(self, obj: Any, context: Any, path: Any) -> Any:
        from . import rle

        payload = obj.get("payload", b"")
        header = obj.get("header", {})
        command_id = header.get("command_id", 0)

        if payload and rle.should_compress(payload):
            try:
                compressed = rle.RLE_TRANSFORM.build(payload)
                if len(compressed) < len(payload):
                    new_header = dict(header)
                    new_header["command_id"] = command_id | protocol.CMD_FLAG_COMPRESSED
                    new_header["payload_len"] = len(compressed)
                    return {"header": new_header, "payload": compressed}
            except (ValueError, TypeError, OverflowError):
                pass

        new_header = dict(header)
        new_header["payload_len"] = len(payload)
        return {"header": new_header, "payload": payload}


# [SIL-2] Inner container for CRC calculation with transparent RLE Adapter
RPC_PAYLOAD_CONTAINER: Construct = FrameAdapter(
    Struct(
        "header" / RPC_FRAME_HEADER,
        "payload" / Bytes(this.header.payload_len),
    )
)

# [SIL-2] Full Frame with Checksum (Sustitución Drástica)
# Uses RawCopy to capture the bytes for CRC calculation without manual slicing.
RPC_FRAME: Construct = Struct(
    "header_payload" / RawCopy(RPC_PAYLOAD_CONTAINER),
    "crc" / Checksum(Int32ub, _calculate_crc32, this.header_payload.data),
).compile()


class Frame(msgspec.Struct, frozen=True):
    """Represents an RPC frame for MCU-Linux communication.

    This class provides both object-oriented and static methods for
    frame construction and parsing.

    Attributes:
        command_id: The RPC command or status code (16-bit).
        sequence_id: The RPC sequence ID (16-bit) for deduplication.
        payload: The frame payload (0 to MAX_PAYLOAD_SIZE bytes).
    """

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
        """Build the binary frame representation."""
        if len(self.payload) > protocol.MAX_PAYLOAD_SIZE:
            raise ValueError(
                f"Payload too large: {len(self.payload)} > {protocol.MAX_PAYLOAD_SIZE}"
            )
        try:
            # Use simple dictionary for building
            return RPC_FRAME.build(
                {
                    "header_payload": {
                        "value": {
                            "header": {
                                "version": protocol.PROTOCOL_VERSION,
                                "payload_len": len(self.payload),
                                "command_id": int(self.command_id),
                                "sequence_id": self.sequence_id,
                            },
                            "payload": self.payload,
                        }
                    }
                }
            )
        except (ConstructError, ValueError, TypeError) as e:
            raise ValueError(f"Failed to build frame: {e}") from e

    @classmethod
    def parse(cls, raw_frame_buffer: bytes | bytearray | memoryview) -> "Frame":
        """Parse *raw_frame_buffer* and create a :class:`Frame`."""
        try:
            obj: Any = RPC_FRAME.parse(raw_frame_buffer)
            return cls(
                command_id=int(obj.header_payload.value.header.command_id),
                sequence_id=int(obj.header_payload.value.header.sequence_id),
                payload=obj.header_payload.value.payload,
            )
        except (ConstructError, ValueError, TypeError, AttributeError, KeyError) as e:
            raise ValueError(f"Incomplete or malformed frame: {e}") from e

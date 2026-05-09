"""RPC frame building and parsing for Arduino-Linux serial communication.

This module implements the binary frame format used over the serial link
between the Linux daemon and the Arduino MCU using declarative Construct structures.

[SIL-2 COMPLIANCE]
The frame format is strictly defined to ensure:
- Deterministic memory layout.
- Explicit CRC32 validation.
- Zero manual orchestration logic.
- Native integration of RLE compression.
"""

from __future__ import annotations

from binascii import crc32
from typing import Any

import construct
import msgspec
from construct import (
    Adapter,
    Bytes,
    Checksum,
    Const,
    Construct,
    Int8ub,
    Int16ub,
    Int32ub,
    RawCopy,
    Rebuild,
    Struct,
    this,
)
from construct.core import ConstructError

from . import protocol
from .rle import RLE_TRANSFORM, should_compress


class FrameAdapter(Adapter):
    """[SIL-2] High-level adapter to map between internal Frame object and Construct dict."""

    def _decode(self, obj: Any, context: Any, path: Any) -> "Frame":
        # Extract fields from the declarative Body
        header = obj.body.value.header
        payload = obj.body.value.payload
        cmd_id = int(header.command_id)

        # Handle implicit RLE decompression if bit 15 is set
        if cmd_id & protocol.CMD_FLAG_COMPRESSED:
            payload = RLE_TRANSFORM.parse(payload)
            cmd_id &= ~protocol.CMD_FLAG_COMPRESSED

        return Frame(
            command_id=cmd_id,
            sequence_id=int(header.sequence_id),
            payload=payload,
        )

    def _encode(self, obj: "Frame", context: Any, path: Any) -> dict[str, Any]:
        cmd_id = int(obj.command_id)
        payload = obj.payload

        # Handle implicit RLE compression
        if payload and should_compress(payload):
            compressed = RLE_TRANSFORM.build(payload)
            if len(compressed) < len(payload):
                payload = compressed
                cmd_id |= protocol.CMD_FLAG_COMPRESSED

        return {
            "body": {
                "value": {
                    "header": {
                        "version": protocol.PROTOCOL_VERSION,
                        "payload_len": len(payload),
                        "command_id": cmd_id,
                        "sequence_id": obj.sequence_id,
                    },
                    "payload": payload,
                }
            }
        }


# --- DECLARATIVE STRUCTURES ---


def _get_payload_len(ctx: Any) -> int:
    """SIL-2: Explicitly typed payload length calculator."""
    return len(ctx._.payload)


RPC_FRAME_HEADER = Struct(
    "version" / Const(protocol.PROTOCOL_VERSION, Int8ub),
    "payload_len" / Rebuild(Int16ub, _get_payload_len),
    "command_id" / Int16ub,
    "sequence_id" / Int16ub,
)

RPC_FRAME_BODY = Struct(
    "header" / RPC_FRAME_HEADER,
    "payload" / Bytes(this.header.payload_len),
)


def _frame_crc(data: bytes) -> int:
    """CRC32 checksum for frame integrity (SIL-2)."""
    return crc32(data) & 0xFFFFFFFF


# [SIL-2] The Maestro: One structure to rule them all.
RPC_FRAME_SCHEMA: Construct = FrameAdapter(
    Struct(
        "body" / RawCopy(RPC_FRAME_BODY),
        "crc" / Checksum(Int32ub, _frame_crc, this.body.data),
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
        """Delegates frame building to the declarative schema."""
        try:
            return RPC_FRAME_SCHEMA.build(self)
        except (ConstructError, ValueError, TypeError) as e:
            raise ValueError(f"Failed to build frame: {e}") from e

    @classmethod
    def parse(cls, raw_frame_buffer: bytes | bytearray | memoryview) -> "Frame":
        """Delegates frame parsing to the declarative schema."""
        try:
            return RPC_FRAME_SCHEMA.parse(raw_frame_buffer)
        except getattr(construct, "ChecksumError", ConstructError) as e:
            raise ValueError(f"CRC mismatch: {e}") from e
        except (ConstructError, ValueError, TypeError, AttributeError, KeyError) as e:
            raise ValueError(f"Incomplete or malformed frame: {e}") from e
